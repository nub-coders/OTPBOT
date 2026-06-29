import os
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")
OTP_TIMEOUT = int(os.getenv("OTP_TIMEOUT", "300"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHAT_ID = int(os.getenv("CHAT_ID", "0")) or None
_updates_raw = os.getenv("UPDATES_CHANNEL", "").strip()
if _updates_raw.startswith(("https://", "http://")):
    UPDATES_CHANNEL = _updates_raw
elif _updates_raw.startswith("@"):
    UPDATES_CHANNEL = f"https://t.me/{_updates_raw[1:]}"
elif _updates_raw:
    UPDATES_CHANNEL = f"https://t.me/{_updates_raw}"
else:
    UPDATES_CHANNEL = ""

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USDT_TO_INR = float(os.getenv("USDT_TO_INR", "95"))

TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")
VERIFY_URL = os.getenv("VERIFY_URL", "")
VERIFY_PORT = int(os.getenv("VERIFY_PORT", "8888"))

REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "1"))

CREDIT_PLANS = {
    "10": {"credits": 10, "amount_inr": 1000, "label": "10 Credits — ₹10"},
    "25": {"credits": 25, "amount_inr": 2500, "label": "25 Credits — ₹25"},
    "50": {"credits": 50, "amount_inr": 5000, "label": "50 Credits — ₹50"},
    "100": {"credits": 100, "amount_inr": 10000, "label": "100 Credits — ₹100"},
}

SUPPORT_HANDLES = [
    "@VAULT_Store_admi",
    "@Panel_hightech_seller",
    "@Midnight_rider_UK00",
    "@Trusted_account1seller",
    "@just_a_dev",
]

CRYPTO_PLANS = {
    k: {
        **v,
        "amount_usdt": (Decimal(str(v["amount_inr"])) / Decimal("100") / Decimal(str(USDT_TO_INR))).quantize(Decimal("0.01")),
    }
    for k, v in CREDIT_PLANS.items()
}
