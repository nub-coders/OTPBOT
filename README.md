# OTP Bot

A Telegram bot that manages phone number sessions and forwards OTP codes to users via a credit-based system. Built with [Kurigram](https://github.com/AshokShau/Kurigram) (Pyrogram fork), MongoDB, and Docker.

## How It Works

1. **Admins** add Telegram phone numbers through the bot interface (phone verification + optional 2FA).
2. Each number gets a **price** (credits per OTP) set by the admin.
3. **Users** purchase credits via Razorpay (UPI) or Binance (USDT), then select an available number.
4. The bot connects the session on-demand, listens for messages from Telegram's service account (`777000`), extracts the OTP code, and forwards it to the user.
5. Credits are deducted and the number is marked as **sold** after delivery.

## Features

### User
- Browse available numbers with prices
- Select a number and receive OTPs automatically
- View 2FA password for the assigned number
- Purchase credits via Razorpay UPI QR or Binance USDT deposit
- OTP history

### Admin
- Add/remove phone number sessions via bot interface
- Set and modify per-number pricing
- Verify session status (connects and reports errors)
- Re-add failed sessions without losing configuration
- Update 2FA passwords on Telegram accounts
- Manage users and credits
- View stats (users, numbers, OTPs, payment breakdown)

### Technical
- **On-demand sessions** — numbers only connect when a user selects them, no persistent connections
- **Custom Emoji Engine** — automatically intercepts and renders Telegram custom emojis (animated/static) in message text via monkey-patched Pyrogram client methods
- **Dual Parse Mode** — supports mixed HTML and Markdown message parsing in Pyrogram, allowing custom emoji HTML tags to coexist with standard Markdown formatting
- **MongoDB** storage for sessions, users, OTP history, and payments
- **Razorpay** QR codes tagged with project identifier for multi-project key sharing
- **Binance** USDT deposit verification with TX hash validation
- **Docker Compose** deployment

## Project Structure

```
bot.py                  Telegram bot handlers and UI
clients.py              Userbot session management and OTP forwarding
database.py             MongoDB operations (Motor async driver)
payments.py             Razorpay and Binance payment integration
config.py               Environment config and credit plans
custom_emojis.py        Custom emoji registry and Pyrogram monkey-patching
utils.py                OTP extraction and country code helpers (including Eswatini)
main.py                 Entry point
extract_emoji_pack.py   Helper script to extract custom emoji sticker packs
send_emoji_preview.py   Helper script to send emoji previews to a target user
data/                   Directory containing custom emoji data
Dockerfile              Python 3.13-slim container
docker-compose.yml      Bot + MongoDB services
```

## Setup

### Prerequisites

- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram API ID and Hash (from [my.telegram.org](https://my.telegram.org))
- MongoDB instance (or use the bundled Docker container)
- Razorpay API keys (optional, for UPI payments)
- Binance API keys (optional, for USDT payments)

### Configuration

Copy the provided `.env.example` template to `.env`:

```bash
cp .env.example .env
```

Open `.env` and fill in the required configuration variables:

```env
BOT_TOKEN=your_bot_token
API_ID=your_api_id
API_HASH=your_api_hash
MONGODB_URI=mongodb://mongo:27017
ADMIN_IDS=123456789
OTP_TIMEOUT=300

# Razorpay (optional)
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=

# Binance (optional)
BINANCE_API_KEY=
BINANCE_API_SECRET=
USDT_TO_INR=95
```

### Run with Docker Compose

```bash
docker compose up -d --build
```

### Run manually

```bash
pip install -r requirements.txt
python main.py
```

## Custom Emojis

The bot can automatically render Telegram custom emojis in message text (e.g. replacing standard unicode emojis like `✅`, `❌`, `⏳`, etc. with premium custom equivalents).

To register and use a custom emoji pack:

1. Extract a custom emoji pack using the helper script (provide either the pack's short name or its Telegram link):
   ```bash
   python extract_emoji_pack.py tgsemoji112
   # OR
   python extract_emoji_pack.py https://t.me/addemoji/tgsemoji112
   ```
   This will save the emoji mapping to [custom_emoji_ids.json](file:///root/OTPBOT/data/custom_emoji_ids.json).

2. The bot will automatically intercept outgoing messages and replace registered unicode characters with `<emoji id="...">` HTML tags.

3. To preview flag emojis from the registered packs, you can run:
   ```bash
   python send_emoji_preview.py
   ```

## Credit Plans

Default pricing (1 INR = 1 credit):

| Plan | Credits | Price (INR) |
|------|---------|-------------|
| 10   | 10      | 10          |
| 25   | 25      | 25          |
| 50   | 50      | 50          |
| 100  | 100     | 100         |

USDT prices are auto-calculated from the `USDT_TO_INR` rate.

## Number Lifecycle

```
active --> assigned (user selects) --> sold (OTP delivered)
                |
                v
          released (timeout or manual)
```

## License

[MIT](LICENSE)
