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
- **On-demand sessions** -- numbers only connect when a user selects them, no persistent connections
- **MongoDB** storage for sessions, users, OTP history, and payments
- **Razorpay** QR codes tagged with project identifier for multi-project key sharing
- **Binance** USDT deposit verification with TX hash validation
- **Docker Compose** deployment

## Project Structure

```
bot.py          Telegram bot handlers and UI
clients.py      Userbot session management and OTP forwarding
database.py     MongoDB operations (Motor async driver)
payments.py     Razorpay and Binance payment integration
config.py       Environment config and credit plans
utils.py        OTP extraction from message text
main.py         Entry point
Dockerfile      Python 3.13-slim container
docker-compose.yml  Bot + MongoDB services
```

## Setup

### Prerequisites

- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram API ID and Hash (from [my.telegram.org](https://my.telegram.org))
- MongoDB instance (or use the bundled Docker container)
- Razorpay API keys (optional, for UPI payments)
- Binance API keys (optional, for USDT payments)

### Configuration

Create a `.env` file:

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
