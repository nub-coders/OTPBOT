import os
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

_MISSING = object()

def get_env(key: str, default=_MISSING) -> str:
    val = os.getenv(key)
    if val:
        return val
    if default is _MISSING:
        raise RuntimeError(f"Required env var {key!r} is not set")
    return default

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")
OTP_TIMEOUT = int(os.getenv("OTP_TIMEOUT", "300"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
MODERATOR_IDS = [int(x) for x in os.getenv("MODERATOR_IDS", "6076474757").split(",") if x.strip()]
MODERATOR_ID = MODERATOR_IDS[0] if MODERATOR_IDS else 6076474757
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

# Postable form of the updates channel. UPDATES_CHANNEL above is a t.me URL
# used for an inline button and cannot be passed to send_message; this holds
# the raw @username / -100 chat id. Empty disables the coupon broadcast.
_updates_id = os.getenv("UPDATES_CHANNEL_ID", "").strip() or _updates_raw
if _updates_id.startswith(("https://t.me/", "http://t.me/")):
    _updates_id = _updates_id.split("/", 3)[-1].rstrip("/")
_updates_id = _updates_id.lstrip("@")
if _updates_id.lstrip("-").isdigit():
    UPDATES_CHANNEL_ID = _updates_id
elif (4 <= len(_updates_id) <= 32 and _updates_id.isascii()
        and _updates_id.replace("_", "").isalnum()):
    UPDATES_CHANNEL_ID = f"@{_updates_id}"
else:
    # A private invite link (t.me/+hash, /joinchat/…) has no @username form.
    # Leave this empty so the broadcast disables itself instead of failing
    # every night against an unroutable peer; set UPDATES_CHANNEL_ID to the
    # numeric -100… id to post into a private channel.
    UPDATES_CHANNEL_ID = ""

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

REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "0"))
REFERRAL_VERIFY_BONUS = int(os.getenv("REFERRAL_VERIFY_BONUS", "1"))

# ── Purchased-account securing (seller submissions) ──
# When the store "buys" a seller's account we rotate its 2FA password and, if the
# account already has a login email, switch it to our own so the seller can't
# recover the account. The email OTP is read from our inbox API.
NEW_LOGIN_EMAIL = os.getenv("NEW_LOGIN_EMAIL", "")
INBOX_API_BASE = os.getenv("INBOX_API_BASE", "https://mails.nubcoders.com/api/emails/inbox-api")
INBOX_API_KEY = os.getenv("INBOX_API_KEY", "")  # secret — env only, never hardcode

# ── Seller Marketplace ──
# Percentage of the sale price credited to the seller (rest goes to admin/platform).
# E.g. 80 means seller keeps 80 credits for every 100-credit sale.
SELLER_PAYOUT_PERCENT = int(os.getenv("SELLER_PAYOUT_PERCENT", "80"))

# The one Telegram user who handles manual WhatsApp orders. All WA notifications
# go here only (not CHAT_ID, not the other admins), and this user can use the WA
# admin panel even if they are not in ADMIN_IDS.
WA_ADMIN_ID = int(os.getenv("WA_ADMIN_ID", "8220538605"))

# ── Random time-limited discount offers ──
# A random flat credit discount (biased toward the minimum) is granted to a
# user when they /start, valid for a random window, then locked out for a
# cooldown period before another can be granted. The effective price is always
# clamped to a minimum of 1 credit so cheap numbers never become free.
# Set OFFER_GRANT_CHANCE to a value between 0.0 and 1.0 to make offer grants
# happen probabilistically instead of every eligible /start.
# OFFER_DISCOUNT_SKEW controls the discount distribution. Larger values make
# higher discounts much rarer. A value around 31.387 makes the maximum
# discount appear with about 0.07% probability.
OFFER_MIN_CREDITS = int(os.getenv("OFFER_MIN_CREDITS", "2"))
OFFER_MAX_CREDITS = int(os.getenv("OFFER_MAX_CREDITS", "25"))
OFFER_MIN_HOURS = float(os.getenv("OFFER_MIN_HOURS", "4"))
OFFER_MAX_HOURS = float(os.getenv("OFFER_MAX_HOURS", "6"))
OFFER_COOLDOWN_HOURS = float(os.getenv("OFFER_COOLDOWN_HOURS", "24"))
OFFER_GRANT_CHANCE = float(os.getenv("OFFER_GRANT_CHANCE", "1.0"))
OFFER_RECENT_PURCHASE_DAYS = int(os.getenv("OFFER_RECENT_PURCHASE_DAYS", "14"))
OFFER_DISCOUNT_SKEW = float(os.getenv("OFFER_DISCOUNT_SKEW", "31.387447433766166"))
OFFER_GRANT_INTERVAL_SECONDS = int(os.getenv("OFFER_GRANT_INTERVAL_SECONDS", "300"))

# ── Nightly coupon codes ──
# COUPON_COUNT codes are posted to UPDATES_CHANNEL_ID at 00:00 UTC. Any user
# may redeem any code once; a redemption grants a discount offer worth a
# random COUPON_MIN_CREDITS..COUPON_MAX_CREDITS credits off for
# COUPON_OFFER_HOURS. Codes stop working COUPON_TTL_HOURS after posting.
# The alphabet excludes O/0/I/1 so a mistyped code cannot hit another coupon.
COUPON_COUNT = int(os.getenv("COUPON_COUNT", "10"))
COUPON_CODE_LENGTH = int(os.getenv("COUPON_CODE_LENGTH", "6"))
COUPON_TTL_HOURS = float(os.getenv("COUPON_TTL_HOURS", "24"))
COUPON_OFFER_HOURS = float(os.getenv("COUPON_OFFER_HOURS", "6"))
COUPON_MIN_CREDITS = int(os.getenv("COUPON_MIN_CREDITS", "1"))
COUPON_MAX_CREDITS = int(os.getenv("COUPON_MAX_CREDITS", "10"))
COUPON_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

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

STARS_PER_CREDIT = float(os.getenv("STARS_PER_CREDIT", "1.0"))
STARS_PLANS = {
    k: {
        **v,
        "stars": int(v["credits"] * STARS_PER_CREDIT),
    }
    for k, v in CREDIT_PLANS.items()
}

