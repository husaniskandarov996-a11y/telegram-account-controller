import os
import sys
import asyncio
import logging
from typing import Dict, Any, Optional

# Ensure UTF-8 output on Windows / Linux consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from telethon import TelegramClient, events, Button
from telethon.tl.custom import Message

import config
import database
from account_manager import (
    QRLoginManager,
    PhoneLoginManager,
    BatchExecutor,
    parse_telegram_link,
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AccountControllerBot")

# Initialize database
database.init_db()

# Create master bot client
bot = TelegramClient("master_bot_session", config.API_ID, config.API_HASH)

# In-memory session state for conversational flow per user
user_states: Dict[int, Dict[str, Any]] = {}
active_qr_logins: Dict[int, QRLoginManager] = {}
active_phone_logins: Dict[int, PhoneLoginManager] = {}

def is_admin(user_id: int) -> bool:
    """Verifies if the sender is the system administrator (for stats/global management)."""
    admin_id = config.ADMIN_USER_ID
    if admin_id is None:
        saved_admin = database.get_setting("admin_user_id")
        if saved_admin:
            admin_id = int(saved_admin)
            config.ADMIN_USER_ID = admin_id
        else:
            database.save_setting("admin_user_id", str(user_id))
            config.ADMIN_USER_ID = user_id
            return True
    return user_id == admin_id

def main_menu_keyboard():
    """Generates the interactive main dashboard buttons."""
    return [
        [
            Button.inline("➕ Akkaunt qo'shish", b"menu_add_choice"),
            Button.inline("📋 Akkauntlar ro'yxati", b"menu_list_accounts")
        ],
        [
            Button.inline("🔗 Mass Join Link", b"menu_mass_join"),
            Button.inline("🤖 Mass Start Bot", b"menu_mass_bot")
        ],
        [
            Button.inline("🚪 Mass Leave Chat", b"menu_mass_leave"),
            Button.inline("🩺 Akkauntlarni tekshirish", b"menu_check_health")
        ],
        [
            Button.inline("ℹ️ Yordam / Buyruqlar", b"menu_help")
        ]
    ]

def add_method_keyboard():
    """Buttons to choose how to link a Telegram account."""
    return [
        [
            Button.inline("📱 QR Code orqali", b"add_via_qr"),
            Button.inline("📩 SMS / Telefon orqali", b"add_via_phone")
        ],
        [
            Button.inline("🔙 Orqaga", b"menu_back")
        ]
    ]

# ----------------- PUBLIC COMMAND HANDLERS ----------------- #

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event: Message):
    user_id = event.sender_id
    sender = await event.get_sender()
    first_name = sender.first_name if sender else "Foydalanuvchi"
    count = database.count_accounts(owner_id=user_id)

    text = (
        "🤖 **Telegram Multi-Account Controller**\n\n"
        f"👤 **Foydalanuvchi:** {first_name} (`{user_id}`)\n"
        f"📱 **Sizning ulangan akkauntlaringiz:** `{count}` ta faol\n\n"
        "Quyidagi menyudan kerakli amalni tanlang yoki buyruq yuboring:"
    )
    await event.reply(text, buttons=main_menu_keyboard())

@bot.on(events.NewMessage(pattern=r"^/help$"))
async def help_handler(event: Message):
    help_text = (
        "📖 **Buyruqlar ro'yxati**\n\n"
        "• `/add` - Yangi akkaunt ulash (QR kod yoki SMS)\n"
        "• `/accounts` - O'zingiz ulagan akkauntlarni ko'rish va boshqarish\n"
        "• `/join <havola>` - Hamma akkauntlaringizni kanal/guruhga obuna qilish\n"
        "• `/startbot <@bot_user> [param]` - Hamma akkauntlaringizdan botga `/start` yuborish\n"
        "• `/leave <@kanal>` - Barcha akkauntlaringizdan kanalni tark etish\n"
        "• `/health` - Akkauntlaringiz sessiyasini tekshirish\n"
        "• `/cancel` - Joriy amalni bekor qilish"
    )
    await event.reply(help_text, buttons=[[Button.inline("🔙 Bosh menyu", b"menu_back")]])

@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel_handler(event: Message):
    user_id = event.sender_id
    if user_id in user_states:
        del user_states[user_id]
    if user_id in active_qr_logins:
        await active_qr_logins[user_id].cleanup()
        del active_qr_logins[user_id]
    if user_id in active_phone_logins:
        await active_phone_logins[user_id].cleanup()
        del active_phone_logins[user_id]
    await event.reply("❌ Joriy amal bekor qilindi.", buttons=main_menu_keyboard())

@bot.on(events.NewMessage(pattern=r"^/accounts$"))
async def accounts_handler(event: Message):
    await show_accounts_list(event)

async def show_accounts_list(event):
    user_id = event.sender_id
    accounts = database.get_all_accounts(owner_id=user_id)
    if not accounts:
        text = "📭 **Sizda hali birorta ham akkaunt ulanmagan.**\n\nAkkaunt qo'shish uchun quyidagi tugmani bosing:"
        buttons = [[Button.inline("➕ Akkaunt qo'shish", b"menu_add_choice")], [Button.inline("🔙 Bosh menyu", b"menu_back")]]
        if isinstance(event, Message):
            await event.reply(text, buttons=buttons)
        else:
            await event.edit(text, buttons=buttons)
        return

    text = f"📋 **Sizning ulangan akkauntlaringiz ({len(accounts)} ta):**\n\n"
    buttons = []
    for acc in accounts:
        status_icon = "🟢" if acc["is_active"] else "🔴"
        name = acc["first_name"] or "No Name"
        username_str = f"@{acc['username']}" if acc['username'] else (acc['phone'] or f"ID:{acc['tg_id']}")
        text += f"{status_icon} **{acc['id']}.** {name} ({username_str}) — `{acc['tg_id']}`\n"
        buttons.append([Button.inline(f"🗑 O'chirish #{acc['id']} ({name[:12]})", f"del_{acc['id']}".encode())])

    buttons.append([Button.inline("➕ Yana akkaunt qo'shish", b"menu_add_choice"), Button.inline("🔙 Bosh menyu", b"menu_back")])
    if isinstance(event, Message):
        await event.reply(text, buttons=buttons)
    else:
        await event.edit(text, buttons=buttons)

# ----------------- ADD ACCOUNT (QR / SMS) FLOW ----------------- #

@bot.on(events.NewMessage(pattern=r"^/add$"))
async def add_account_handler(event: Message):
    text = (
        "➕ **Yangi Telegram akkaunt qo'shish**\n\n"
        "Qanday usulda ulamoqchisiz? Quyidagilardan birini tanlang:\n\n"
        "1️⃣ **QR Code:** Telegram ilovangizdan kamerada skaner qilasiz (tez va oson)\n"
        "2️⃣ **SMS / Raqam:** Telefon raqamingizni kiritasiz va Telegramingizga kelgan kodni yozasiz"
    )
    await event.reply(text, buttons=add_method_keyboard())

async def trigger_phone_login_start(user_id: int, responder):
    """Prompts user for phone number."""
    if user_id in active_phone_logins:
        await active_phone_logins[user_id].cleanup()
    if user_id in active_qr_logins:
        await active_qr_logins[user_id].cleanup()

    user_states[user_id] = {"action": "waiting_phone_number"}
    text = (
        "📞 **Telefon raqam (SMS) orqali ulanish:**\n\n"
        "Iltimos, Telegram akkauntingiz telefon raqamini xalqaro formatda yozib yuboring:\n"
        "Misol uchun: `+998901234567`\n\n"
        "Yoki bekor qilish uchun `/cancel` yozing."
    )
    buttons = [[Button.inline("❌ Bekor qilish", b"cancel_action")]]
    if isinstance(responder, Message):
        await responder.reply(text, buttons=buttons)
    else:
        await responder.edit(text, buttons=buttons)

async def trigger_qr_login(user_id: int, responder):
    """Starts the QR login procedure and displays the QR code to the user."""
    if user_id in active_qr_logins:
        await active_qr_logins[user_id].cleanup()
    if user_id in active_phone_logins:
        await active_phone_logins[user_id].cleanup()

    qr_mgr = QRLoginManager(owner_id=user_id)
    active_qr_logins[user_id] = qr_mgr

    status_msg = None
    if isinstance(responder, Message):
        status_msg = await responder.reply("⏳ QR Login tayyorlanmoqda... Token olinmoqda...")
    else:
        status_msg = await responder.edit("⏳ QR Login tayyorlanmoqda... Token olinmoqda...")

    try:
        img_bytes, qr_url = await qr_mgr.start()
        caption = (
            "📱 **Telegram ilovangizdan QR kodni skanerlang:**\n\n"
            "1. Telegramni oching\n"
            "2. **Sozlamalar (Settings)** ➡️ **Qurilmalar (Devices)** ➡️ **Qurilmani ulash**\n"
            "3. Kamerani ushbu QR kodga qarating\n\n"
            "⏱ _Muddati ~30 soniya. Skanerlash kutilmoqda..._"
        )
        photo_msg = await bot.send_file(
            user_id,
            file=img_bytes,
            caption=caption,
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_qr")]]
        )

        success, msg, acc_info = await qr_mgr.wait_for_scan(timeout=45.0)

        if success and acc_info:
            first_name = acc_info.get("first_name") or "Nomalum"
            u_name = f"@{acc_info['username']}" if acc_info.get("username") else "Mavjud emas"
            phone_num = acc_info.get("phone") or "Yashirin"
            congrats_text = (
                f"✅ **Akkaunt muvaffaqiyatli ulandi!**\n\n"
                f"👤 **Ism:** {first_name}\n"
                f"🏷 **Username:** {u_name}\n"
                f"🆔 **Telegram ID:** `{acc_info['tg_id']}`\n"
                f"📞 **Telefon:** `{phone_num}`\n\n"
                f"Endi bu akkaunt buyruqlaringizni bajarishga tayyor!"
            )
            await bot.send_message(user_id, congrats_text, buttons=main_menu_keyboard())
            if user_id in active_qr_logins:
                del active_qr_logins[user_id]

        elif msg == "2FA_NEEDED":
            user_states[user_id] = {"action": "waiting_2fa_password"}
            await bot.send_message(
                user_id,
                "🔒 **Ikki bosqichli tekshiruv (2FA) aniqlandi!**\n\n"
                "Ushbu Telegram akkauntda 2FA Cloud Password bor.\n"
                "Ulanishni yakunlash uchun **2FA parolingizni** yozib yuboring:"
            )
        else:
            await bot.send_message(
                user_id,
                f"⚠️ {msg}",
                buttons=[[Button.inline("🔄 Qaytadan urinish", b"menu_add_choice"), Button.inline("🔙 Bosh menyu", b"menu_back")]]
            )
            if user_id in active_qr_logins:
                del active_qr_logins[user_id]

    except Exception as e:
        logger.error(f"Error in trigger_qr_login: {e}", exc_info=True)
        await bot.send_message(user_id, f"❌ QR Login xatosi: {str(e)}")
        if user_id in active_qr_logins:
            await active_qr_logins[user_id].cleanup()
            del active_qr_logins[user_id]

# ----------------- BATCH ACTION COMMANDS ----------------- #

@bot.on(events.NewMessage(pattern=r"^/join(?:\s+(.+))?$"))
async def join_cmd_handler(event: Message):
    link = event.pattern_match.group(1)
    if not link:
        user_states[event.sender_id] = {"action": "waiting_join_link"}
        await event.reply(
            "🔗 **Mass Join Channel / Group**\n\n"
            "Kanal/guruh havolasi yoki usernamesini yuboring:\n"
            "• `https://t.me/kanal_nomi` yoki `@kanal_nomi`\n"
            "• `https://t.me/+AbCdEfGh` (Yopiq kanal taklif havolasi)\n\n"
            "Yoki bekor qilish uchun `/cancel` yozing.",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )
        return

    await execute_mass_join(event.sender_id, link.strip())

async def execute_mass_join(user_id: int, target_link: str):
    accounts = database.get_all_accounts(owner_id=user_id, only_active=True)
    if not accounts:
        await bot.send_message(user_id, "⚠️ Sizda faol akkauntlar topilmadi. Avval `/add` orqali akkaunt qo'shing.")
        return

    await bot.send_message(
        user_id,
        f"⏳ **Obuna bo'linmoqda:** `{target_link}`\n"
        f"Sizning `{len(accounts)}` ta akkauntingiz navbat bilan obuna bo'lmoqda..."
    )

    results = await BatchExecutor.join_chat(owner_id=user_id, target_link=target_link)

    report = f"📊 **Natija:**\n🎯 Manzil: `{target_link}`\n\n"
    success_cnt = sum(1 for r in results if r["status"] in ("success", "already_joined"))

    for r in results:
        icon = "✅" if r["status"] == "success" else ("ℹ️" if r["status"] == "already_joined" else "❌")
        report += f"{icon} **{r['account_name']}:** {r['message']}\n"

    report += f"\n🏁 **Yakunlandi:** {success_cnt}/{len(results)} ta akkaunt muvaffaqiyatli obuna bo'ldi."
    await bot.send_message(user_id, report, buttons=main_menu_keyboard())

@bot.on(events.NewMessage(pattern=r"^/startbot(?:\s+(@?[A-Za-z0-9_]+)(?:\s+(.+))?)?$"))
async def startbot_cmd_handler(event: Message):
    bot_target = event.pattern_match.group(1)
    param = event.pattern_match.group(2)

    if not bot_target:
        user_states[event.sender_id] = {"action": "waiting_startbot_target"}
        await event.reply(
            "🤖 **Mass Start Bot**\n\n"
            "Bot usernamesi yoki start havolasini yuboring:\n"
            "• `@misol_bot`\n"
            "• `https://t.me/misol_bot?start=ref123`\n\n"
            "Yoki bekor qilish uchun `/cancel` yozing.",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )
        return

    await execute_mass_startbot(event.sender_id, bot_target, param)

async def execute_mass_startbot(user_id: int, bot_target: str, param: Optional[str] = None):
    accounts = database.get_all_accounts(owner_id=user_id, only_active=True)
    if not accounts:
        await bot.send_message(user_id, "⚠️ Sizda faol akkauntlar topilmadi. Avval `/add` orqali akkaunt qo'shing.")
        return

    await bot.send_message(
        user_id,
        f"⏳ **Bot ishga tushirilmoqda:** `{bot_target}`\n"
        f"Sizning `{len(accounts)}` ta akkauntingizdan start yuborilmoqda..."
    )

    results = await BatchExecutor.start_bot(owner_id=user_id, target=bot_target, param=param)

    report = f"📊 **Start natijasi:**\n🎯 Bot: `{bot_target}`\n\n"
    success_cnt = sum(1 for r in results if r["status"] == "success")

    for r in results:
        icon = "✅" if r["status"] == "success" else "❌"
        report += f"{icon} **{r['account_name']}:** {r['message']}\n"

    report += f"\n🏁 **Yakunlandi:** {success_cnt}/{len(results)} ta akkaunt start yubordi."
    await bot.send_message(user_id, report, buttons=main_menu_keyboard())

@bot.on(events.NewMessage(pattern=r"^/leave(?:\s+(.+))?$"))
async def leave_cmd_handler(event: Message):
    target = event.pattern_match.group(1)
    if not target:
        user_states[event.sender_id] = {"action": "waiting_leave_target"}
        await event.reply(
            "🚪 **Mass Leave Chat**\n\n"
            "Chiqmoqchi bo'lgan kanal yoki guruh usernamesini yuboring:\n"
            "• `@kanal_nomi`\n"
            "• `https://t.me/kanal_nomi`\n\n"
            "Yoki bekor qilish uchun `/cancel` yozing.",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )
        return

    await execute_mass_leave(event.sender_id, target.strip())

async def execute_mass_leave(user_id: int, target: str):
    accounts = database.get_all_accounts(owner_id=user_id, only_active=True)
    if not accounts:
        await bot.send_message(user_id, "⚠️ Sizda faol akkauntlar topilmadi.")
        return

    await bot.send_message(user_id, f"⏳ `{target}` dan `{len(accounts)}` ta akkauntingiz chiqmoqda...")
    results = await BatchExecutor.leave_chat(owner_id=user_id, target=target)

    report = f"📊 **Chiqish natijasi:**\n🎯 Manzil: `{target}`\n\n"
    for r in results:
        icon = "✅" if r["status"] == "success" else "❌"
        report += f"{icon} **{r['account_name']}:** {r['message']}\n"

    await bot.send_message(user_id, report, buttons=main_menu_keyboard())

@bot.on(events.NewMessage(pattern=r"^/health$"))
async def health_cmd_handler(event: Message):
    await execute_health_check(event.sender_id)

async def execute_health_check(user_id: int):
    accounts = database.get_all_accounts(owner_id=user_id)
    if not accounts:
        await bot.send_message(user_id, "📭 Sizda ulanmagan akkauntlar mavjud emas.", buttons=main_menu_keyboard())
        return

    await bot.send_message(user_id, f"🩺 Sizning `{len(accounts)}` ta akkauntingiz holati tekshirilmoqda...")
    results = await BatchExecutor.check_health(owner_id=user_id)

    report = f"🩺 **Akkauntlaringiz holati ({len(results)} ta):**\n\n"
    for r in results:
        icon = "🟢" if r["status"] == "active" else "🔴"
        report += f"{icon} **#{r['id']} {r['name']}** (@{r['username'] or r['phone'] or r['tg_id']}): {r['details']}\n"

    await bot.send_message(user_id, report, buttons=main_menu_keyboard())

# ----------------- ADMIN COMMANDS ----------------- #

@bot.on(events.NewMessage(pattern=r"^/stats$"))
async def stats_cmd_handler(event: Message):
    if not is_admin(event.sender_id):
        return
    stats = database.get_global_stats()
    text = (
        "📊 **Bot Global Statistikasi (Admin):**\n\n"
        f"👥 **Jami foydalanuvchilar:** `{stats['total_users']}` ta\n"
        f"📱 **Jami ulangan akkauntlar:** `{stats['total_accounts']}` ta\n"
        f"🟢 **Faol akkauntlar:** `{stats['active_accounts']}` ta\n"
    )
    await event.reply(text)

# ----------------- CONVERSATION / TEXT INPUT HANDLER ----------------- #

@bot.on(events.NewMessage)
async def text_input_dispatcher(event: Message):
    if event.raw_text.startswith("/"):
        return  # Handled by command handlers

    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state:
        return

    action = state.get("action")

    if action == "waiting_phone_number":
        phone_raw = event.raw_text.strip()
        phone_mgr = PhoneLoginManager(owner_id=user_id)
        active_phone_logins[user_id] = phone_mgr
        
        status_msg = await event.reply("⏳ Telegramga so'rov yuborilmoqda, kuting...")
        success, msg = await phone_mgr.send_code(phone_raw)
        
        if success:
            user_states[user_id] = {"action": "waiting_phone_code"}
            await event.reply(
                f"📩 **Tasdiqlash kodi yuborildi!** (`{phone_mgr.phone_number}`)\n\n"
                "Telegram ilovangizga (yoki SMS orqali) 5 xonali tasdiqlash kodi bordi.\n"
                "Kodni bu yerga yozib yuboring (Masalan: `12345`):\n\n"
                "_Bekor qilish uchun /cancel yozing._",
                buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
            )
        else:
            del user_states[user_id]
            if user_id in active_phone_logins:
                del active_phone_logins[user_id]
            await event.reply(
                f"❌ {msg}",
                buttons=[[Button.inline("🔄 Qaytadan urinish", b"add_via_phone"), Button.inline("🔙 Bosh menyu", b"menu_back")]]
            )

    elif action == "waiting_phone_code":
        code_raw = event.raw_text.strip()
        phone_mgr = active_phone_logins.get(user_id)
        if not phone_mgr:
            del user_states[user_id]
            await event.reply("⚠️ Sessiya eskirgan. Iltimos, qaytadan /add bosing.")
            return

        status_msg = await event.reply("⏳ Kod tekshirilmoqda...")
        success, msg, acc_info = await phone_mgr.submit_code(code_raw)

        if success and acc_info:
            del user_states[user_id]
            if user_id in active_phone_logins:
                del active_phone_logins[user_id]
            first_name = acc_info.get("first_name") or "Nomalum"
            u_name = f"@{acc_info['username']}" if acc_info.get("username") else "Mavjud emas"
            phone_num = acc_info.get("phone") or "Yashirin"
            congrats_text = (
                f"✅ **Akkaunt muvaffaqiyatli ulandi!**\n\n"
                f"👤 **Ism:** {first_name}\n"
                f"🏷 **Username:** {u_name}\n"
                f"🆔 **Telegram ID:** `{acc_info['tg_id']}`\n"
                f"📞 **Telefon:** `{phone_num}`\n\n"
                f"Endi bu akkaunt buyruqlaringizni bajarishga tayyor!"
            )
            await bot.send_message(user_id, congrats_text, buttons=main_menu_keyboard())

        elif msg == "2FA_NEEDED":
            user_states[user_id] = {"action": "waiting_phone_2fa_password"}
            await event.reply(
                "🔒 **Ikki bosqichli tekshiruv (2FA) aniqlandi!**\n\n"
                "Ushbu Telegram akkauntda 2FA Cloud Password o'rnatilgan.\n"
                "Ulanishni yakunlash uchun **2FA parolingizni** yozib yuboring:"
            )
        else:
            await event.reply(f"❌ {msg}\n\nIltimos, kodni to'g'ri kiriting yoki `/cancel` bosing.")

    elif action == "waiting_phone_2fa_password":
        password = event.raw_text.strip()
        phone_mgr = active_phone_logins.get(user_id)
        if not phone_mgr:
            del user_states[user_id]
            await event.reply("⚠️ Sessiya eskirgan. Qaytadan /add bosing.")
            return

        status_msg = await event.reply("⏳ 2FA parol tekshirilmoqda...")
        success, msg, acc_info = await phone_mgr.submit_password(password)

        if success and acc_info:
            del user_states[user_id]
            if user_id in active_phone_logins:
                del active_phone_logins[user_id]
            first_name = acc_info.get("first_name") or "Nomalum"
            u_name = f"@{acc_info['username']}" if acc_info.get("username") else "Mavjud emas"
            phone_num = acc_info.get("phone") or "Yashirin"
            congrats_text = (
                f"✅ **Akkaunt 2FA bilan muvaffaqiyatli ulandi!**\n\n"
                f"👤 **Ism:** {first_name}\n"
                f"🏷 **Username:** {u_name}\n"
                f"🆔 **Telegram ID:** `{acc_info['tg_id']}`\n"
                f"📞 **Telefon:** `{phone_num}`"
            )
            await bot.send_message(user_id, congrats_text, buttons=main_menu_keyboard())
        else:
            await event.reply(f"❌ {msg}\n\nParolni qaytadan yozing yoki `/cancel` bosing.")

    elif action == "waiting_2fa_password":
        password = event.raw_text.strip()
        qr_mgr = active_qr_logins.get(user_id)
        if not qr_mgr:
            del user_states[user_id]
            await event.reply("⚠️ Login session expired. Please start over with `/add`.")
            return

        del user_states[user_id]
        await event.reply("⏳ Verifying 2FA password...")
        success, msg, acc_info = await qr_mgr.submit_password(password)

        if success and acc_info:
            first_name = acc_info.get("first_name") or "Nomalum"
            u_name = f"@{acc_info['username']}" if acc_info.get("username") else "Mavjud emas"
            phone_num = acc_info.get("phone") or "Yashirin"
            congrats_text = (
                f"✅ **Akkaunt 2FA bilan muvaffaqiyatli ulandi!**\n\n"
                f"👤 **Ism:** {first_name}\n"
                f"🏷 **Username:** {u_name}\n"
                f"🆔 **Telegram ID:** `{acc_info['tg_id']}`\n"
                f"📞 **Telefon:** `{phone_num}`"
            )
            await bot.send_message(user_id, congrats_text, buttons=main_menu_keyboard())
            if user_id in active_qr_logins:
                del active_qr_logins[user_id]
        else:
            await event.reply(f"❌ {msg}", buttons=[[Button.inline("🔄 Qaytadan urinish", b"menu_add_choice"), Button.inline("🔙 Bosh menyu", b"menu_back")]])
            if user_id in active_qr_logins:
                del active_qr_logins[user_id]

    elif action == "waiting_join_link":
        del user_states[user_id]
        await execute_mass_join(user_id, event.raw_text.strip())

    elif action == "waiting_startbot_target":
        del user_states[user_id]
        raw = event.raw_text.strip()
        parts = raw.split(maxsplit=1)
        target = parts[0]
        param = parts[1] if len(parts) > 1 else None
        await execute_mass_startbot(user_id, target, param)

    elif action == "waiting_leave_target":
        del user_states[user_id]
        await execute_mass_leave(user_id, event.raw_text.strip())

# ----------------- CALLBACK QUERY HANDLER ----------------- #

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode("utf-8")
    user_id = event.sender_id

    if data == "menu_back":
        if user_id in user_states:
            del user_states[user_id]
        sender = await event.get_sender()
        first_name = sender.first_name if sender else "Foydalanuvchi"
        count = database.count_accounts(owner_id=user_id)
        await event.edit(
            f"🤖 **Telegram Multi-Account Controller**\n\n"
            f"👤 **Foydalanuvchi:** {first_name} (`{user_id}`)\n"
            f"📱 **Sizning ulangan akkauntlaringiz:** `{count}` ta faol\n\n"
            "Quyidagi menyudan kerakli amalni tanlang:",
            buttons=main_menu_keyboard()
        )

    elif data in ("menu_add_choice", "menu_add_account"):
        await event.edit(
            "➕ **Yangi Telegram akkaunt qo'shish**\n\n"
            "Qanday usulda ulamoqchisiz? Quyidagilardan birini tanlang:\n\n"
            "1️⃣ **QR Code:** Telegram ilovangizdan kamerada skaner qilasiz\n"
            "2️⃣ **SMS / Raqam:** Telefon raqamingizni kiritasiz va Telegramingizga kelgan kodni yozasiz",
            buttons=add_method_keyboard()
        )

    elif data == "add_via_qr":
        await trigger_qr_login(user_id, event)

    elif data == "add_via_phone":
        await trigger_phone_login_start(user_id, event)

    elif data == "menu_list_accounts":
        await show_accounts_list(event)

    elif data == "menu_mass_join":
        user_states[user_id] = {"action": "waiting_join_link"}
        await event.edit(
            "🔗 **Mass Join Channel / Group**\n\n"
            "Kanal/guruh havolasi yoki usernamesini yuboring:\n"
            "• `https://t.me/kanal_nomi` yoki `@kanal_nomi`\n"
            "• `https://t.me/+AbCdEfGh` (Yopiq kanal taklif havolasi)\n\n"
            "Yozib yuboring yoki quyidagi tugmani bosing:",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )

    elif data == "menu_mass_bot":
        user_states[user_id] = {"action": "waiting_startbot_target"}
        await event.edit(
            "🤖 **Mass Start Bot**\n\n"
            "Bot usernamesi yoki start havolasini yuboring:\n"
            "• `@misol_bot`\n"
            "• `https://t.me/misol_bot?start=ref123`\n\n"
            "Yozib yuboring yoki bekor qiling:",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )

    elif data == "menu_mass_leave":
        user_states[user_id] = {"action": "waiting_leave_target"}
        await event.edit(
            "🚪 **Mass Leave Chat**\n\n"
            "Chiqmoqchi bo'lgan kanal yoki guruh usernamesini yuboring:\n"
            "• `@kanal_nomi`\n"
            "• `https://t.me/kanal_nomi`",
            buttons=[[Button.inline("❌ Bekor qilish", b"cancel_action")]]
        )

    elif data == "menu_check_health":
        await event.answer("Akkauntlar tekshirilmoqda...", alert=False)
        await execute_health_check(user_id)

    elif data == "menu_help":
        help_text = (
            "📖 **Buyruqlar ro'yxati**\n\n"
            "• `/add` - Yangi akkaunt ulash (QR kod yoki SMS)\n"
            "• `/accounts` - O'zingiz ulagan akkauntlarni ko'rish va boshqarish\n"
            "• `/join <havola>` - Hamma akkauntlaringizni kanal/guruhga obuna qilish\n"
            "• `/startbot <@bot_user> [param]` - Hamma akkauntlaringizdan botga `/start` yuborish\n"
            "• `/leave <@kanal>` - Barcha akkauntlaringizdan kanalni tark etish\n"
            "• `/health` - Akkauntlaringiz sessiyasini tekshirish\n"
            "• `/cancel` - Joriy amalni bekor qilish"
        )
        await event.edit(help_text, buttons=[[Button.inline("🔙 Bosh menyu", b"menu_back")]])

    elif data == "cancel_action":
        if user_id in user_states:
            del user_states[user_id]
        if user_id in active_phone_logins:
            await active_phone_logins[user_id].cleanup()
            del active_phone_logins[user_id]
        if user_id in active_qr_logins:
            await active_qr_logins[user_id].cleanup()
            del active_qr_logins[user_id]
        await event.edit("❌ Amal bekor qilindi.", buttons=main_menu_keyboard())

    elif data == "cancel_qr":
        if user_id in active_qr_logins:
            await active_qr_logins[user_id].cleanup()
            del active_qr_logins[user_id]
        await event.edit("❌ QR Login bekor qilindi.", buttons=main_menu_keyboard())

    elif data.startswith("del_"):
        acc_id = int(data.split("_")[1])
        acc = database.get_account(acc_id, owner_id=user_id)
        if acc:
            database.delete_account(acc_id, owner_id=user_id)
            await event.answer(f"#{acc_id} raqamli akkaunt ({acc['first_name']}) o'chirildi", alert=True)
        else:
            await event.answer("Akkaunt topilmadi yoki bu sizga tegishli emas.", alert=True)
        await show_accounts_list(event)

# ----------------- MAIN RUNNER ----------------- #

async def main():
    logger.info("Starting Telegram Multi-Account Controller Master Bot (Public Multi-Tenant)...")
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"Bot successfully started as @{me.username} (ID: {me.id})")
    print(f"\n=======================================================")
    print(f"🤖 Bot is running online: @{me.username}")
    print(f"👉 Open Telegram and send /start to @{me.username}")
    print(f"=======================================================\n")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
