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

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USDT_TO_INR = float(os.getenv("USDT_TO_INR", "95"))

CREDIT_PLANS = {
    "10": {"credits": 10, "amount_inr": 1000, "label": "10 Credits — ₹10"},
    "25": {"credits": 25, "amount_inr": 2500, "label": "25 Credits — ₹25"},
    "50": {"credits": 50, "amount_inr": 5000, "label": "50 Credits — ₹50"},
    "100": {"credits": 100, "amount_inr": 10000, "label": "100 Credits — ₹100"},
}

CRYPTO_PLANS = {
    k: {
        **v,
        "amount_usdt": (Decimal(str(v["amount_inr"])) / Decimal("100") / Decimal(str(USDT_TO_INR))).quantize(Decimal("0.01")),
    }
    for k, v in CREDIT_PLANS.items()
}
