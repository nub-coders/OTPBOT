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

ENABLE_VERIFICATION = os.getenv("ENABLE_VERIFICATION", "True").lower() in ("true", "1", "yes")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")
VERIFY_URL = os.getenv("VERIFY_URL", "")
VERIFY_PORT = int(os.getenv("VERIFY_PORT", "8888"))

REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "5"))
REFERRAL_VERIFY_BONUS = int(os.getenv("REFERRAL_VERIFY_BONUS", "1"))

# ── Purchased-account securing (seller submissions) ──
# When the store "buys" a seller's account we rotate its 2FA password and, if the
# account already has a login email, switch it to our own so the seller can't
# recover the account. The email OTP is read from our inbox API.
NEW_LOGIN_EMAIL = os.getenv("NEW_LOGIN_EMAIL", "dev@nubcoders.com")
INBOX_API_BASE = os.getenv("INBOX_API_BASE", "https.nubcoders.com/api/emails/inbox-api")
INBOX_API_KEY = os.getenv("INBOX_API_KEY", "nm_live_738ed6dc1adc36a54768e29a77869c5d8419c9a79ea0ec95ec9446283688b1e3")

# ── Seller Marketplace ──
# Percentage of the sale price credited to the seller (rest goes to admin/platform).
# E.g. 80 means seller keeps 80 credits for every 100-credit sale.
SELLER_PAYOUT_PERCENT = int(os.getenv("SELLER_PAYOUT_PERCENT", "80"))

# ── Random time-limited discount offers ──
# A random flat credit discount (biased toward the minimum) is granted to a
# user when they /start, valid for a random window, then locked out for a
# cooldown period before another can be granted. The effective price is always
# clamped to a minimum of 1 credit so cheap numbers never become free.
OFFER_MIN_CREDITS = int(os.getenv("OFFER_MIN_CREDITS", "2"))
OFFER_MAX_CREDITS = int(os.getenv("OFFER_MAX_CREDITS", "25"))
OFFER_MIN_HOURS = float(os.getenv("OFFER_MIN_HOURS", "4"))
OFFER_MAX_HOURS = float(os.getenv("OFFER_MAX_HOURS", "6"))
OFFER_COOLDOWN_HOURS = float(os.getenv("OFFER_COOLDOWN_HOURS", "24"))

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
