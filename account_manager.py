import io
import re
import asyncio
import random
from typing import Optional, Dict, Any, List, Tuple
import qrcode
from PIL import Image

from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession

import config
import database

def create_qr_image_bytes(data: str) -> io.BytesIO:
    """Generates a high-quality QR code image buffer."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    buf.name = "qr_login.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def parse_telegram_link(raw_link: str) -> Tuple[str, Optional[str]]:
    """
    Parses a Telegram link or username.
    Returns (type, value/target) where type is:
    - 'private_invite': value is the hash (e.g., from t.me/+hash)
    - 'bot_start': value is (bot_username, start_param)
    - 'public': value is the username
    """
    cleaned = raw_link.strip()
    
    # Check for bot start link with parameter: https://t.me/somebot?start=123
    bot_param_match = re.search(r'(?:https?://)?(?:t\.me/|telegram\.me/)([A-Za-z0-9_]+)\?start=([A-Za-z0-9_\-]+)', cleaned)
    if bot_param_match:
        bot_user = bot_param_match.group(1)
        param = bot_param_match.group(2)
        return 'bot_start', f"{bot_user}:{param}"

    # Check for private invite links: t.me/+hash or t.me/joinchat/hash
    invite_match = re.search(r'(?:https?://)?(?:t\.me/|telegram\.me/)(?:\+|joinchat/)([A-Za-z0-9_\-]+)', cleaned)
    if invite_match:
        return 'private_invite', invite_match.group(1)

    # Check for general t.me/username
    public_match = re.search(r'(?:https?://)?(?:t\.me/|telegram\.me/)([A-Za-z0-9_]{4,})', cleaned)
    if public_match:
        return 'public', public_match.group(1)

    # Bare username or @username
    if cleaned.startswith('@'):
        return 'public', cleaned[1:]
    
    if re.match(r'^[A-Za-z0-9_]{4,}$', cleaned):
        return 'public', cleaned

    return 'unknown', cleaned

class QRLoginManager:
    """Manages an active QR login session for adding a new Telegram account for a specific user."""
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.client: Optional[TelegramClient] = None
        self.qr_login_obj = None
        self.is_waiting_password = False
        self.task: Optional[asyncio.Task] = None
        self.result_future: Optional[asyncio.Future] = None

    async def start(self) -> Tuple[io.BytesIO, str]:
        """Starts the QR login procedure and returns (qr_image_bytes, qr_url)."""
        existing = database.get_all_accounts(owner_id=self.owner_id)
        ignored_ids = [acc['tg_id'] for acc in existing]

        self.client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await self.client.connect()

        self.qr_login_obj = await self.client.qr_login(ignored_ids=ignored_ids)
        qr_url = self.qr_login_obj.url
        img_bytes = create_qr_image_bytes(qr_url)
        return img_bytes, qr_url

    async def wait_for_scan(self, timeout: float = 60.0) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Awaits the user scanning the QR code in their Telegram app.
        Returns (success, message, account_info).
        If 2FA is needed, returns (False, '2FA_NEEDED', None).
        """
        try:
            user = await self.qr_login_obj.wait(timeout=timeout)
            session_str = self.client.session.save()
            
            acc_id = database.add_account(
                owner_id=self.owner_id,
                tg_id=user.id,
                first_name=user.first_name,
                username=user.username,
                phone=user.phone,
                session_string=session_str
            )
            await self.client.disconnect()
            return True, "Account connected successfully!", {
                "id": acc_id,
                "tg_id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "phone": user.phone
            }
        except errors.SessionPasswordNeededError:
            self.is_waiting_password = True
            return False, "2FA_NEEDED", None
        except asyncio.TimeoutError:
            await self.cleanup()
            return False, "QR Code expired. Please generate a new code.", None
        except Exception as e:
            await self.cleanup()
            return False, f"Login failed: {str(e)}", None

    async def submit_password(self, password: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Submits the 2FA cloud password if requested."""
        if not self.client or not self.is_waiting_password:
            return False, "No login session waiting for a 2FA password.", None
        try:
            user = await self.client.sign_in(password=password)
            session_str = self.client.session.save()
            acc_id = database.add_account(
                owner_id=self.owner_id,
                tg_id=user.id,
                first_name=user.first_name,
                username=user.username,
                phone=user.phone,
                session_string=session_str
            )
            await self.client.disconnect()
            self.is_waiting_password = False
            return True, "2FA Password accepted! Account connected successfully.", {
                "id": acc_id,
                "tg_id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "phone": user.phone
            }
        except errors.PasswordHashInvalidError:
            return False, "Incorrect 2FA password. Please try again.", None
        except Exception as e:
            await self.cleanup()
            return False, f"2FA verification failed: {str(e)}", None

    async def cleanup(self):
        """Disconnects and cleans up resources."""
        self.is_waiting_password = False
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.qr_login_obj = None

class PhoneLoginManager:
    """Manages an active Phone/SMS login session for a specific user."""
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.client: Optional[TelegramClient] = None
        self.phone_number: Optional[str] = None
        self.phone_code_hash: Optional[str] = None
        self.is_waiting_code = False
        self.is_waiting_password = False

    async def send_code(self, phone: str) -> Tuple[bool, str]:
        """Connects and sends a login verification code to the phone/Telegram."""
        try:
            cleaned_phone = re.sub(r'[^0-9+]', '', phone.strip())
            if not cleaned_phone.startswith('+'):
                cleaned_phone = '+' + cleaned_phone

            self.client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await self.client.connect()

            res = await self.client.send_code_request(cleaned_phone)
            self.phone_number = cleaned_phone
            self.phone_code_hash = res.phone_code_hash
            self.is_waiting_code = True
            return True, "Tasdiqlash kodi yuborildi."
        except errors.PhoneNumberInvalidError:
            await self.cleanup()
            return False, "Telefon raqam formati noto'g'ri. Xalqaro formatda kiriting (masalan: +998901234567)."
        except errors.FloodWaitError as e:
            await self.cleanup()
            return False, f"Telegram cheklov qo'ydi (FloodWait): {e.seconds} soniya kuting."
        except Exception as e:
            await self.cleanup()
            return False, f"Xatolik: {str(e)}"

    async def submit_code(self, code: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Submits the SMS/Telegram login code."""
        if not self.client or not self.is_waiting_code:
            return False, "Faol sessiya topilmadi. Qaytadan /add bosing.", None
        
        cleaned_code = re.sub(r'[^0-9]', '', code.strip())
        try:
            user = await self.client.sign_in(
                phone=self.phone_number,
                code=cleaned_code,
                phone_code_hash=self.phone_code_hash
            )
            session_str = self.client.session.save()
            acc_id = database.add_account(
                owner_id=self.owner_id,
                tg_id=user.id,
                first_name=user.first_name,
                username=user.username,
                phone=user.phone or self.phone_number,
                session_string=session_str
            )
            await self.client.disconnect()
            self.is_waiting_code = False
            return True, "Akkaunt muvaffaqiyatli ulandi!", {
                "id": acc_id,
                "tg_id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "phone": user.phone or self.phone_number
            }
        except errors.SessionPasswordNeededError:
            self.is_waiting_code = False
            self.is_waiting_password = True
            return False, "2FA_NEEDED", None
        except errors.PhoneCodeInvalidError:
            return False, "Kiritilgan tasdiqlash kodi noto'g'ri! Qaytadan kiriting:", None
        except errors.PhoneCodeExpiredError:
            await self.cleanup()
            return False, "Kodning amal qilish muddati tugadi. Qaytadan /add bosing.", None
        except Exception as e:
            await self.cleanup()
            return False, f"Kirishda xatolik: {str(e)}", None

    async def submit_password(self, password: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Submits the 2FA password."""
        if not self.client or not self.is_waiting_password:
            return False, "2FA sessiyasi topilmadi.", None
        try:
            user = await self.client.sign_in(password=password)
            session_str = self.client.session.save()
            acc_id = database.add_account(
                owner_id=self.owner_id,
                tg_id=user.id,
                first_name=user.first_name,
                username=user.username,
                phone=user.phone or self.phone_number,
                session_string=session_str
            )
            await self.client.disconnect()
            self.is_waiting_password = False
            return True, "2FA parol tasdiqlandi! Akkaunt ulandi.", {
                "id": acc_id,
                "tg_id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "phone": user.phone or self.phone_number
            }
        except errors.PasswordHashInvalidError:
            return False, "2FA paroli noto'g'ri. Qaytadan kiriting:", None
        except Exception as e:
            await self.cleanup()
            return False, f"2FA xatolik: {str(e)}", None

    async def cleanup(self):
        self.is_waiting_code = False
        self.is_waiting_password = False
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

class BatchExecutor:
    """Executes actions across a user's registered accounts with rate-limiting and reporting."""

    @staticmethod
    async def join_chat(owner_id: int, target_link: str, delay_range: Tuple[float, float] = (3.0, 6.0)) -> List[Dict[str, Any]]:
        """Makes all active accounts of the specified owner join the channel or group."""
        accounts = database.get_all_accounts(owner_id=owner_id, only_active=True)
        if not accounts:
            return []

        link_type, value = parse_telegram_link(target_link)
        results = []

        for idx, acc in enumerate(accounts):
            client = TelegramClient(StringSession(acc["session_string"]), config.API_ID, config.API_HASH)
            acc_label = f"{acc['first_name'] or 'Account'} (@{acc['username'] or acc['phone'] or acc['tg_id']})"
            result_item = {
                "account_id": acc["id"],
                "account_name": acc_label,
                "status": "pending",
                "message": ""
            }

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    database.set_account_active(acc["id"], False, owner_id=owner_id)
                    result_item["status"] = "error"
                    result_item["message"] = "Session revoked/expired. Account deactivated."
                    results.append(result_item)
                    continue

                if link_type == 'private_invite':
                    try:
                        await client(functions.messages.ImportChatInviteRequest(hash=value))
                        result_item["status"] = "success"
                        result_item["message"] = "Joined via private invite link."
                    except errors.UserAlreadyParticipantError:
                        result_item["status"] = "already_joined"
                        result_item["message"] = "Already a member."
                    except errors.InviteRequestSentError:
                        result_item["status"] = "request_sent"
                        result_item["message"] = "Join request sent (pending admin approval)."

                elif link_type in ('public', 'unknown'):
                    entity = await client.get_entity(value)
                    await client(functions.channels.JoinChannelRequest(channel=entity))
                    result_item["status"] = "success"
                    result_item["message"] = f"Joined public chat @{value}."

                else:
                    result_item["status"] = "error"
                    result_item["message"] = f"Unsupported link format: {target_link}"

            except errors.UserAlreadyParticipantError:
                result_item["status"] = "already_joined"
                result_item["message"] = "Already a member."
            except errors.FloodWaitError as e:
                result_item["status"] = "flood_wait"
                result_item["message"] = f"Rate limited: FloodWait for {e.seconds}s."
            except errors.InviteHashExpiredError:
                result_item["status"] = "error"
                result_item["message"] = "Invite link is expired or invalid."
            except errors.InviteHashInvalidError:
                result_item["status"] = "error"
                result_item["message"] = "Invalid invite link."
            except errors.ChannelPrivateError:
                result_item["status"] = "error"
                result_item["message"] = "Channel is private or account is banned."
            except Exception as e:
                result_item["status"] = "error"
                result_item["message"] = str(e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            results.append(result_item)

            if idx < len(accounts) - 1:
                sleep_sec = random.uniform(*delay_range)
                await asyncio.sleep(sleep_sec)

        return results

    @staticmethod
    async def start_bot(owner_id: int, target: str, param: Optional[str] = None, delay_range: Tuple[float, float] = (2.0, 4.0)) -> List[Dict[str, Any]]:
        """Makes all active accounts of the specified owner send /start to a bot."""
        accounts = database.get_all_accounts(owner_id=owner_id, only_active=True)
        if not accounts:
            return []

        link_type, value = parse_telegram_link(target)
        bot_username = value
        start_param = param

        if link_type == 'bot_start':
            parts = value.split(':', 1)
            bot_username = parts[0]
            start_param = parts[1] if len(parts) > 1 else None

        bot_username = bot_username.lstrip('@')
        command_text = f"/start {start_param}" if start_param else "/start"

        results = []
        for idx, acc in enumerate(accounts):
            client = TelegramClient(StringSession(acc["session_string"]), config.API_ID, config.API_HASH)
            acc_label = f"{acc['first_name'] or 'Account'} (@{acc['username'] or acc['phone'] or acc['tg_id']})"
            result_item = {
                "account_id": acc["id"],
                "account_name": acc_label,
                "status": "pending",
                "message": ""
            }

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    database.set_account_active(acc["id"], False, owner_id=owner_id)
                    result_item["status"] = "error"
                    result_item["message"] = "Session revoked/expired."
                    results.append(result_item)
                    continue

                bot_entity = await client.get_input_entity(bot_username)
                await client.send_message(bot_entity, command_text)
                result_item["status"] = "success"
                result_item["message"] = f"Sent '{command_text}' to @{bot_username}."

            except errors.FloodWaitError as e:
                result_item["status"] = "flood_wait"
                result_item["message"] = f"FloodWait: {e.seconds}s."
            except Exception as e:
                result_item["status"] = "error"
                result_item["message"] = str(e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            results.append(result_item)

            if idx < len(accounts) - 1:
                await asyncio.sleep(random.uniform(*delay_range))

        return results

    @staticmethod
    async def leave_chat(owner_id: int, target: str, delay_range: Tuple[float, float] = (2.0, 4.0)) -> List[Dict[str, Any]]:
        """Makes all active accounts of the specified owner leave the chat."""
        accounts = database.get_all_accounts(owner_id=owner_id, only_active=True)
        if not accounts:
            return []

        link_type, value = parse_telegram_link(target)
        chat_identifier = value.lstrip('@')

        results = []
        for idx, acc in enumerate(accounts):
            client = TelegramClient(StringSession(acc["session_string"]), config.API_ID, config.API_HASH)
            acc_label = f"{acc['first_name'] or 'Account'} (@{acc['username'] or acc['phone'] or acc['tg_id']})"
            result_item = {
                "account_id": acc["id"],
                "account_name": acc_label,
                "status": "pending",
                "message": ""
            }

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    database.set_account_active(acc["id"], False, owner_id=owner_id)
                    result_item["status"] = "error"
                    result_item["message"] = "Session revoked/expired."
                    results.append(result_item)
                    continue

                entity = await client.get_entity(chat_identifier)
                await client(functions.channels.LeaveChannelRequest(channel=entity))
                result_item["status"] = "success"
                result_item["message"] = f"Left chat successfully."

            except errors.FloodWaitError as e:
                result_item["status"] = "flood_wait"
                result_item["message"] = f"FloodWait: {e.seconds}s."
            except Exception as e:
                result_item["status"] = "error"
                result_item["message"] = str(e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            results.append(result_item)

            if idx < len(accounts) - 1:
                await asyncio.sleep(random.uniform(*delay_range))

        return results

    @staticmethod
    async def check_health(owner_id: int) -> List[Dict[str, Any]]:
        """Checks the connection status and validity of accounts belonging to this owner."""
        accounts = database.get_all_accounts(owner_id=owner_id, only_active=False)
        results = []

        for acc in accounts:
            client = TelegramClient(StringSession(acc["session_string"]), config.API_ID, config.API_HASH)
            status_item = {
                "id": acc["id"],
                "tg_id": acc["tg_id"],
                "name": acc["first_name"] or "Account",
                "username": acc["username"],
                "phone": acc["phone"],
                "is_active": acc["is_active"],
                "status": "offline",
                "details": ""
            }

            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    status_item["status"] = "active"
                    status_item["details"] = f"OK (DC {client.session.dc_id})"
                    # update profile if changed
                    if me.first_name != acc["first_name"] or me.username != acc["username"]:
                        database.add_account(owner_id, me.id, me.first_name, me.username, me.phone, acc["session_string"])
                    database.set_account_active(acc["id"], True, owner_id=owner_id)
                else:
                    status_item["status"] = "expired"
                    status_item["details"] = "Session revoked by user/Telegram"
                    database.set_account_active(acc["id"], False, owner_id=owner_id)
            except Exception as e:
                status_item["status"] = "error"
                status_item["details"] = str(e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            results.append(status_item)

        return results
