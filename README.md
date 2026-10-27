# Telegram Multi-Account Controller Bot

A multi-account Telegram controller built with **Telethon** and **Python**. It allows you to link your Telegram accounts without typing phone numbers (using **QR Code login**, just like Telegram Desktop / Web), and command all linked accounts to join channels, groups, or start bots.

---

## 🚀 Quick Start

1. Double-click **`run.bat`** (or run `.venv\Scripts\python.exe bot.py` in your terminal).
2. Open Telegram and send `/start` to your bot.
3. The first person to send `/start` is automatically set as the authorized admin.

---

## 📱 How to Add Accounts (No Phone Number Needed)

1. Click **➕ Add Account (QR Code)** or send `/add`.
2. The bot will send a fresh QR code image.
3. On your phone:
   - Open Telegram
   - Go to **Settings** ➡️ **Devices** ➡️ **Link Desktop Device**
   - Point your camera at the QR code.
4. If your account has a **2-Step Verification (2FA)** cloud password, the bot will ask for it. Reply with the password to finish.
5. Your account is now connected and ready to receive commands! Repeat to add as many accounts as you want.

---

## ⚡ Features & Commands

| Command | Action |
| :--- | :--- |
| `/start` | Open the main interactive menu dashboard |
| `/add` | Generate a new QR code to link an account |
| `/accounts` | View all connected accounts with status and delete options |
| `/join <link>` | Mass join public channels (`@channel`) or private invite links (`t.me/+hash`) |
| `/startbot <@bot> [param]` | Send `/start` (or `/start ref_param`) to a target bot |
| `/leave <chat>` | Mass leave a channel or group |
| `/health` | Check if any account sessions have expired or been revoked |
| `/cancel` | Cancel the ongoing operation |

---

## 🛡️ Anti-Flood / Rate-Limit Protection
Actions across accounts are executed with a randomized delay (3–6 seconds) between accounts to prevent Telegram from flagging your IP or placing FloodWait restrictions on your accounts.
