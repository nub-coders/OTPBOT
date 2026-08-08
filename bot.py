import asyncio
import time
import logging
import io
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pyrogram import Client, filters, enums

# Shorthand for button style
S = enums.ButtonStyle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)
from pyrogram.errors import (
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
    SessionPasswordNeeded,
    PhoneNumberInvalid,
    FloodWait,
    MessageNotModified,
    PeerFlood,
)
from pyrogram.raw.functions.users import GetFullUser
from decimal import Decimal
from config import API_ID, API_HASH, BOT_TOKEN, OTP_TIMEOUT, CREDIT_PLANS, CRYPTO_PLANS, STARS_PLANS, STARS_PER_CREDIT, SUPPORT_HANDLES, CHAT_ID, ADMIN_IDS, MODERATOR_ID, UPDATES_CHANNEL, USDT_TO_INR, TURNSTILE_SITE_KEY, VERIFY_URL, REFERRAL_BONUS, REFERRAL_VERIFY_BONUS, ENABLE_VERIFICATION, OFFER_MIN_CREDITS, OFFER_MAX_CREDITS, OFFER_MIN_HOURS, OFFER_MAX_HOURS, OFFER_GRANT_CHANCE, OFFER_RECENT_PURCHASE_DAYS, OFFER_DISCOUNT_SKEW, SELLER_PAYOUT_PERCENT, WA_ADMIN_ID, COUPON_CODE_LENGTH, COUPON_ALPHABET, COUPON_OFFER_HOURS, UPDATES_CHANNEL_ID, COUPON_TTL_HOURS
import database as db
import clients
import payments
import verification
from utils import detect_country, get_country_flag, get_country_name, search_country, estimate_account_year, mask_phone, mask_secret, extract_year_from_reg_month, parse_reg_month, get_active_sessions_info, format_timestamp, format_account_year
import custom_emojis as em
em.patch_pyrogram_for_custom_emojis()

log = logging.getLogger(__name__)

bot: Client = None
auth_states: dict[int, dict] = {}
pay_states: dict[int, dict] = {}
sell_states: dict[int, dict] = {}   # tracks user-side sell-account auth flow
sell_recheck_states: dict[int, dict] = {}  # holds submission data for a pending session re-check
feedback_states: dict[int, dict] = {}  # tracks user feedback rating & 1-min waiting window
admin_withdraw_states: dict[int, dict] = {}  # tracks admin withdrawal creation flow


def get_credit_plan(plan_key: str) -> dict | None:
    if plan_key.startswith("custom_"):
        try:
            credits = int(plan_key.split("_")[1])
            if credits < 10:
                return None
            return {
                "credits": credits,
                "amount_inr": credits * 100,  # in paisa
                "label": f"{credits} Credits — ₹{credits}",
            }
        except Exception:
            return None
    return CREDIT_PLANS.get(plan_key)


def get_crypto_plan(plan_key: str) -> dict | None:
    if plan_key.startswith("custom_"):
        try:
            credits = int(plan_key.split("_")[1])
            if credits < 10:
                return None
            amount_inr = credits * 100
            amount_usdt = (Decimal(str(amount_inr)) / Decimal("100") / Decimal(str(USDT_TO_INR))).quantize(Decimal("0.01"))
            return {
                "credits": credits,
                "amount_usdt": amount_usdt,
            }
        except Exception:
            return None
    return CRYPTO_PLANS.get(plan_key)


def get_stars_plan(plan_key: str) -> dict | None:
    if plan_key.startswith("custom_"):
        try:
            credits = int(plan_key.split("_")[1])
            if credits < 10:
                return None
            stars = int(credits * STARS_PER_CREDIT)
            return {
                "credits": credits,
                "stars": stars,
                "label": f"{credits} Credits — ⭐{stars}",
            }
        except Exception:
            return None
    return STARS_PLANS.get(plan_key)



def _random_discount_credits() -> int:
    """Pick a random flat credit discount biased toward the minimum.

    Higher skew values make larger discounts rarer. The default exponent of
    OFFER_DISCOUNT_SKEW is 3.0 to reduce the chance of getting the higher
    discount values near OFFER_MAX_CREDITS.
    """
    import random

    lo, hi = OFFER_MIN_CREDITS, OFFER_MAX_CREDITS
    if hi <= lo:
        return lo
    span = hi - lo
    biased = random.random() ** OFFER_DISCOUNT_SKEW  # skew toward 0
    return lo + int(round(biased * span))


def _random_offer_hours() -> float:
    import random

    return random.uniform(OFFER_MIN_HOURS, OFFER_MAX_HOURS)


def apply_discount(price: int, offer: dict | None) -> int:
    """Return the effective per-OTP price after applying an active offer.

    Discount is a flat number of credits off. If the discount meets or exceeds
    the price the number is free (0 credits); the result never goes negative.
    """
    if not offer or not price:
        return price
    credits_off = offer.get("credits", 0)
    if credits_off <= 0:
        return price
    return max(0, price - credits_off)


async def maybe_grant_offer(telegram_id: int) -> tuple[dict | None, bool]:
    """Try to grant a new random active discount offer.

    Returns (offer, granted).
    `offer` is the active offer if one exists or was created.
    `granted` is True only when a new offer was created now.
    """
    active = await db.get_active_offer(telegram_id)
    if active:
        return active, False
    if not await db.can_grant_offer(telegram_id):
        return None, False

    if OFFER_RECENT_PURCHASE_DAYS > 0 and await db.has_recent_purchase(telegram_id, OFFER_RECENT_PURCHASE_DAYS):
        return None, False

    import random

    if OFFER_GRANT_CHANCE < 1.0 and random.random() >= OFFER_GRANT_CHANCE:
        return None, False
    credits = _random_discount_credits()
    hours = _random_offer_hours()
    return await db.set_offer(telegram_id, credits, hours), True


def offer_banner(offer: dict | None) -> str:
    """A short banner line describing the active offer, or empty string."""
    if not offer:
        return ""
    from datetime import datetime, timezone
    expires_at = offer.get("expires_at")
    mins_left = ""
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        delta = expires_at - datetime.now(timezone.utc)
        total_min = max(0, int(delta.total_seconds() // 60))
        h, m = divmod(total_min, 60)
        mins_left = f" • ends in {h}h {m}m" if h else f" • ends in {m}m"
    return f"{em.GIFT} <u>**Limited-time offer: {offer.get('credits', 0)} credits OFF{mins_left}**</u>"


async def safe_edit(message, text, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except MessageNotModified:
        pass


async def safe_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    """Send photo by URL. Fall back to direct byte download if Telegram cURL fails, then to text message."""
    try:
        return await bot.send_photo(chat_id, photo=photo_url, caption=caption, reply_markup=reply_markup)
    except Exception as e:
        log.warning("send_photo via URL (%s) failed: %s. Fetching bytes directly...", photo_url, e)
        try:
            req = urllib.request.Request(photo_url, headers={"User-Agent": "Mozilla/5.0"})
            data = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=10).read())
            bio = io.BytesIO(data)
            bio.name = "qr.png"
            return await bot.send_photo(chat_id, photo=bio, caption=caption, reply_markup=reply_markup)
        except Exception as e2:
            log.error("send_photo via bytes failed: %s. Falling back to text message.", e2)
            return await bot.send_message(chat_id, caption, reply_markup=reply_markup)


async def _answer_cq(cq: CallbackQuery):
    """Dismiss Telegram button loading spinner immediately so UI feels instant."""
    try:
        await cq.answer()
    except Exception:
        pass


async def alert(bot: Client, text: str, reply_markup=None):
    """Send an alert to CHAT_ID channel, or to all admins if not configured."""
    if CHAT_ID:
        try:
            await bot.send_message(CHAT_ID, text, reply_markup=reply_markup)
        except Exception as e:
            log.error("Failed to send alert to CHAT_ID %s: %s", CHAT_ID, e)
    else:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, reply_markup=reply_markup)
            except Exception as e:
                log.error("Failed to send alert to admin %d: %s", admin_id, e)


async def post_coupon_batch(bot, codes: list[str]) -> bool:
    """Post tonight's coupon codes to the updates channel. Returns True on success.

    Codes only — never the reward amounts, which are rolled at redeem time.
    """
    if not UPDATES_CHANNEL_ID or not codes:
        return False
    listed = "\n".join(f"`{c}`" for c in codes)
    hours = int(COUPON_TTL_HOURS)
    try:
        await bot.send_message(
            UPDATES_CHANNEL_ID,
            f"{em.GIFT} **Tonight's Coupon Codes**\n\n"
            f"{listed}\n\n"
            f"Send any code to the bot to claim a discount on your next number.\n"
            f"Each code works once per user. Valid {hours}h.",
        )
        return True
    except Exception as e:
        log.error("Coupon batch post failed: %s", e)
        return False


async def _wa_notify(bot: Client, text: str, reply_markup=None):
    """WhatsApp orders are worked by one operator — notify only them.

    Deliberately not alert(): WA notices must not fan out to CHAT_ID or the
    other admins.
    """
    try:
        await bot.send_message(WA_ADMIN_ID, text, reply_markup=reply_markup)
    except Exception as e:
        log.error("Failed to send WA alert to %d: %s", WA_ADMIN_ID, e)


async def _is_wa_admin(user_id: int) -> bool:
    """Who may act on a WhatsApp order: the WA operator, plus the real admins."""
    return user_id == WA_ADMIN_ID or await db.is_admin(user_id)


VERIFICATION_ENABLED = bool(ENABLE_VERIFICATION and TURNSTILE_SITE_KEY and VERIFY_URL)


async def _check_referral_reward(user_id: int, purchased_credits: int):
    if not user_id or purchased_credits <= 0:
        return
    user = await db.get_user(user_id)
    if not user:
        return
    referrer_id = user.get("referred_by")
    if not referrer_id:
        return
    referrer = await db.get_user(referrer_id)
    if not referrer:
        return

    commission = int(purchased_credits * 0.05)
    if commission > 0:
        await db.add_referral_withdrawable_earning(referrer_id, commission)
        try:
            uname = user.get("first_name") or user.get("username") or str(user_id)
            new_withdrawable = await db.get_balance(referrer_id)
            await bot.send_message(
                referrer_id,
                f"{em.GIFT} **Referral Purchase Reward! (5%)**\n\n"
                f"Your referral **{uname}** purchased **{purchased_credits} credits**.\n"
                f"{em.MONEY} +{commission} credits added to your **Withdrawable Balance**!\n"
                f"💰 Withdrawable Balance: **{new_withdrawable}**",
            )
            log.info("Referral commission: %d credits to referrer %d for user %d purchase of %d credits", commission, referrer_id, user_id, purchased_credits)
        except Exception as e:
            log.warning("Failed to notify referrer %d of commission: %s", referrer_id, e)


def verified(func):
    from functools import wraps
    @wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        # ponytail: start per-task user cache so get_user/get_credits/is_admin
        # etc hit ctx-var instead of separate Mongo round-trips.
        db.begin_user_cache()

        if VERIFICATION_ENABLED:
            tg_user = update.from_user
            user_id = tg_user.id
            if not await db.is_admin(user_id) and user_id != WA_ADMIN_ID:
                if not await db.get_user(user_id):
                    role = "admin" if await db.admin_count() == 0 else "user"
                    referrer_id = None
                    if isinstance(update, Message) and update.text:
                        parts = update.text.split(None, 1)
                        if len(parts) > 1 and parts[1].startswith("ref_"):
                            try:
                                ref_id = int(parts[1][4:])
                                if ref_id != user_id:
                                    referrer_id = ref_id
                            except ValueError:
                                pass
                    await db.create_user(user_id, tg_user.username, tg_user.first_name, role, referred_by=referrer_id)
                    if role == "admin":
                        return await func(client, update, *args, **kwargs)
                    display_name = tg_user.first_name or ""
                    username = tg_user.username
                    name_line = f"📛 Name: {display_name}"
                    user_line = f"\n👤 Username: @{username}" if username else ""
                    ref_line = f"\n{em.LINK} Referred by: `{referrer_id}`" if referrer_id else ""
                    await alert(client,
                        f"{em.USER} **New User Joined**\n\n"
                        f"{em.ID_BADGE} ID: `{user_id}`\n"
                        f"{name_line}{user_line}{ref_line}"
                    )
                if not await db.is_verified(user_id):
                    url = await verification.create_verification_link(user_id)
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.SHIELD} Verify", url=url, style=S.PRIMARY)],
                    ])
                    text = (
                        f"{em.SHIELD} **Verification Required**\n\n"
                        "Complete a quick human verification to access the bot.\n"
                        "Tap the button below to verify, then send /start again."
                    )
                    if isinstance(update, CallbackQuery):
                        await safe_edit(update.message, text, reply_markup=kb)
                    else:
                        await update.reply(text, reply_markup=kb)
                    return
        return await func(client, update, *args, **kwargs)
    return wrapper


def create_bot() -> Client:
    global bot
    bot = Client(
        name="otpbot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
    _register_handlers(bot)
    return bot


# ── Keyboards ──

def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(f"{em.PHONE} Buy Account", callback_data="get_number", style=S.PRIMARY),
            InlineKeyboardButton(f"{em.DOLLAR} Sell Account", callback_data="sell_account", style=S.SUCCESS),
        ],
        [InlineKeyboardButton(f"{em.SMS} Buy WhatsApp", callback_data="wa_portal", style=S.PRIMARY)],
        [InlineKeyboardButton(f"{em.CREDIT} Buy Credits", callback_data="buy_credits", style=S.SUCCESS)],
        [
            InlineKeyboardButton(f"{em.LOGS} My History", callback_data="my_history", style=S.PRIMARY),
            InlineKeyboardButton(f"{em.GIFT} Refer & Earn", callback_data="referral", style=S.PRIMARY),
        ],
        [
            InlineKeyboardButton(f"{em.TUTORIAL} How to Use", callback_data="how_to_use", style=S.DEFAULT),
            InlineKeyboardButton(f"{em.SUPPORT} Support", callback_data="support", style=S.PRIMARY),
            InlineKeyboardButton(f"{em.HELP} Help", callback_data="help", style=S.DEFAULT),
        ],
    ]
    if UPDATES_CHANNEL:
        buttons.append(
            [InlineKeyboardButton(f"{em.BROADCAST} Updates", url=UPDATES_CHANNEL, style=S.SUCCESS)]
        )
    if is_admin:
        buttons.append(
            [InlineKeyboardButton(f"{em.GEAR} Admin Panel", callback_data="admin_panel", style=S.DANGER)]
        )
    return InlineKeyboardMarkup(buttons)


def admin_kb(is_moderator: bool = False) -> InlineKeyboardMarkup:
    withdraw_btn = InlineKeyboardButton(f"{em.DOLLAR} Withdrawals", callback_data="seller_withdrawals", style=S.DANGER) if is_moderator else InlineKeyboardButton(f"{em.DOLLAR} Request Withdrawal", callback_data="admin_withdrawal_req", style=S.PRIMARY)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{em.ADD} Add Number", callback_data="add_number", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.PLAN} List Numbers", callback_data="list_numbers", style=S.PRIMARY),
        ],
        [
            InlineKeyboardButton(f"{em.MONEY} Country Pricing", callback_data="country_pricing", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.USERS} Users", callback_data="users_list", style=S.PRIMARY),
        ],
        [
            InlineKeyboardButton(f"{em.SMS} WhatsApp Numbers", callback_data="wa_admin", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.OFFLINE} Sold", callback_data="sold_list", style=S.PRIMARY),
        ],
        [
            InlineKeyboardButton(f"{em.INBOX} Seller Submissions", callback_data="seller_submissions", style=S.SUCCESS),
            withdraw_btn,
        ],
        [InlineKeyboardButton(f"{em.STATS} Stats", callback_data="stats", style=S.PRIMARY)],
        [InlineKeyboardButton(f"{em.BROADCAST} Broadcast", callback_data="broadcast_help", style=S.PRIMARY)],
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DANGER)],
    ])


def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=target, style=S.PRIMARY)],
    ])


def _feedback_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣", callback_data="rate_1"),
            InlineKeyboardButton("2️⃣", callback_data="rate_2"),
            InlineKeyboardButton("3️⃣", callback_data="rate_3"),
            InlineKeyboardButton("4️⃣", callback_data="rate_4"),
            InlineKeyboardButton("5️⃣", callback_data="rate_5"),
        ],
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu")],
    ])


def _wa_live_text(order: dict) -> str:
    """Buyer-facing view of their one live WhatsApp order."""
    phone = order["phone_number"]
    cc = order.get("country_code", "XX")
    head = (
        f"{em.RECEIPT} Order ID: `{order['order_id']}`\n"
        f"{get_country_flag(cc)} {get_country_name(cc)}\n"
        f"{em.MONEY} Paid: **{order.get('price', 0)}** credits\n"
    )
    if order.get("status") == "confirmed":
        support = " | ".join(SUPPORT_HANDLES)
        return (
            f"{em.SUCCESS} **Admin connected — send your OTP now**\n\n"
            f"{head}"
            f"{em.PHONE} Number: `{phone}`\n\n"
            f"{em.OTP} Request the OTP on this number, then wait here. The admin "
            f"reads the code off the device and forwards it to you.\n\n"
            f"{em.WARNING} Issues? Contact support:\n{support}"
        )
    return (
        f"{em.LOADING} **Waiting for an admin to connect...**\n\n"
        f"{head}"
        f"{em.PHONE} Number: `{mask_phone(phone)}`\n\n"
        f"WhatsApp orders are fulfilled by hand. An admin has been notified and "
        f"will connect to the device shortly.\n\n"
        f"{em.INFO} You'll get the full number here once they confirm. "
        f"If they can't fulfil it, you're refunded in full."
    )


def _wa_live_kb(order: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{em.RESTART} Refresh", callback_data="wa_mine", style=S.PRIMARY)]]
    if order.get("status") == "pending":
        rows.append([InlineKeyboardButton(
            f"{em.CANCELLED} Cancel & Refund", callback_data=f"wa_drop:{order['order_id']}", style=S.DANGER,
        )])
    rows.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)])
    return InlineKeyboardMarkup(rows)


async def _wa_refund(released: dict) -> int:
    """Hand back exactly the credits/balance split that was charged.

    Takes the pre-reset doc returned by cancel_wa_order — that call is the
    atomic gate, so this only ever runs once per order.
    """
    cd = released.get("credits_deducted", 0)
    bd = released.get("balance_deducted", 0)
    if cd or bd:
        await db.restore_purchase_funds(released["buyer_id"], cd, bd)
    return cd + bd


def _confirm_country_kb(cflag: str, cname: str, cc: str, year: int | None, month: int | None = None, *, pick: bool = False) -> InlineKeyboardMarkup:
    """Country-confirm keyboard with an inline account-year adjuster row."""
    yes_cb = f"cc_pick:{cc}" if pick else "cc_yes"
    year_label = format_account_year(year, month)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{em.SUCCESS} Yes, {cflag} {cname}", callback_data=yes_cb, style=S.SUCCESS),
            InlineKeyboardButton(f"{em.ERROR} No", callback_data="cc_no", style=S.DANGER),
        ],
        [
            InlineKeyboardButton(f"{em.CALENDAR} Year Old: " + year_label, callback_data="ay_edit", style=S.PRIMARY),
        ],
    ])


PAGE_SIZE = 25


def paginate_buttons(items, page, cb_prefix, back_target):
    """Slice items for the current page and add nav buttons.
    items: list of InlineKeyboardButton rows (each a list).
    Returns (page_items, nav_keyboard_rows) for the current page."""
    total = len(items)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"{em.BACK} Prev", callback_data=f"{cb_prefix}:{page - 1}", style=S.PRIMARY))
    if end < total:
        nav.append(InlineKeyboardButton(f"{em.NEXT} Next", callback_data=f"{cb_prefix}:{page + 1}", style=S.SUCCESS))

    footer = []
    if nav:
        footer.append(nav)
    footer.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data=back_target, style=S.PRIMARY)])

    page_label = f"\n\n{em.LIST} Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    return page_items, footer, page_label


# ── Handlers ──

def _register_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    @verified
    async def cmd_start(_, message: Message):
        user_id = message.from_user.id

        referrer_id = None
        args = message.text.split(None, 1)
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                ref_id = int(args[1][4:])
                if ref_id != user_id:
                    referrer_id = ref_id
            except ValueError:
                pass

        user = await db.get_user(user_id)
        if not user:
            role = "admin" if await db.admin_count() == 0 else "user"
            await db.create_user(
                user_id,
                message.from_user.username,
                message.from_user.first_name,
                role,
                referred_by=referrer_id,
            )
            if role != "admin":
                display_name = message.from_user.first_name or ""
                username = message.from_user.username
                name_line = f"📛 Name: {display_name}"
                user_line = f"\n👤 Username: @{username}" if username else ""
                ref_line = f"\n{em.LINK} Referred by: `{referrer_id}`" if referrer_id else ""
                await alert(app,
                    f"{em.USER} **New User Joined**\n\n"
                    f"{em.ID_BADGE} ID: `{user_id}`\n"
                    f"{name_line}{user_line}{ref_line}"
                )
                if not VERIFICATION_ENABLED and referrer_id and REFERRAL_VERIFY_BONUS > 0:
                    referrer = await db.get_user(referrer_id)
                    if referrer:
                        await db.mark_referral_rewarded(user_id)
                        await db.add_referral_earning(referrer_id, REFERRAL_VERIFY_BONUS)
                        try:
                            new_balance = await db.get_credits(referrer_id)
                            await bot.send_message(
                                referrer_id,
                                f"{em.GIFT} **Referral Reward!**\n\n"
                                f"Your referral **{display_name or username or user_id}** joined the bot.\n"
                                f"{em.MONEY} +{REFERRAL_VERIFY_BONUS} credits added!\n"
                                f"{em.MONEY} Balance: **{new_balance}**",
                            )
                        except Exception:
                            pass

            if role == "admin":
                await message.reply(
                    f"{em.OWNER} **Welcome, Admin!**\n\n"
                    "You are the first user — you've been set as admin.\n"
                    "Use the panel below to manage numbers and users.",
                    reply_markup=main_menu_kb(True),
                )
                return

        is_adm = await db.is_admin(user_id)
        credits, balance, total_funds = await db.get_total_funds(user_id)

        fname = (message.from_user.first_name or "there").strip()
        is_returning = user is not None  # user was fetched above; None means brand new

        offer_block = ""
        if not is_adm:
            offer = await db.get_active_offer(user_id)
            banner = offer_banner(offer)
            if banner:
                offer_block = (
                    f"\n\n<blockquote>"
                    f"{banner}\n"
                    f"{em.ZAP} Auto-applied on every account you buy — grab it before it's gone!"
                    f"</blockquote>"
                )

        greeting = (
            f"{em.WAVE} Welcome back, **{fname}**!" if is_returning
            else f"{em.SPARK} Hey **{fname}**, welcome aboard! {em.ROCKET}"
        )

        await message.reply(
            f"{greeting}\n"
            f"{em.STAR} **OTP Bot** — buy ready Telegram accounts, delivered instantly.\n\n"
            f"<blockquote>"
            f"{em.PHONE} **Buy Account** — pick a number and own it in seconds\n"
            f"{em.OTP} **Instant login OTP** — the code lands the moment it arrives\n"
            f"{em.GLOBE} **Global** — accounts across many countries\n"
            f"{em.GIFT} **Referrals & offers** — earn and save as you go"
            f"</blockquote>\n\n"
            f"{em.CREDIT} Credits: **{credits}** (purchase only)\n"
            f"{em.MONEY} Withdrawable Balance: **{balance}** credits (purchase & withdrawal){offer_block}\n\n"
            f"{em.IDEA} Pick an option below to begin:",
            reply_markup=main_menu_kb(is_adm),
        )

    @app.on_callback_query(filters.regex("^main_menu$"))
    @verified
    async def cb_main_menu(_, cq: CallbackQuery):
        is_adm = await db.is_admin(cq.from_user.id)
        credits, balance, total_funds = await db.get_total_funds(cq.from_user.id)
        offer_notice = ""
        credit_line = (
            f"\n{em.CREDIT} Credits: **{credits}** (purchase only)\n"
            f"{em.MONEY} Withdrawable Balance: **{balance}** credits (purchase & withdrawal)"
        )
        if cq.message.video or cq.message.photo:
            try:
                await cq.message.delete()
            except Exception:
                pass
            await app.send_message(
                chat_id=cq.from_user.id,
                text=(
                    f"{em.WAVE} **OTP Bot — Main Menu**\n\n"
                    f"{offer_notice}"
                    f"Buy credits, grab a Telegram account, and get its login OTP instantly.{credit_line}"
                ),
                reply_markup=main_menu_kb(is_adm),
            )
        else:
            await safe_edit(cq.message,
                f"{em.WAVE} **OTP Bot — Main Menu**\n\n"
                f"{offer_notice}"
                f"Buy credits, grab a Telegram account, and get its login OTP instantly.{credit_line}",
                reply_markup=main_menu_kb(is_adm),
            )


    @app.on_callback_query(filters.regex("^support$"))
    @verified
    async def cb_support(_, cq: CallbackQuery):
        lines = "\n".join(f"  • [{h.lstrip('@')}](https://t.me/{h.lstrip('@')})" for h in SUPPORT_HANDLES)
        await safe_edit(cq.message,
            f"{em.PHONE} **Support**\n\n"
            f"Having issues? Contact any of our support agents:\n\n"
            f"<blockquote>{lines}</blockquote>\n\n"
            "We're here to help with purchases, login issues, or any questions.",
            reply_markup=back_kb(),
        )

    @app.on_callback_query(filters.regex("^referral$"))
    @verified
    async def cb_referral(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        bot_me = await app.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{user_id}"
        ref_count = await db.get_referral_count(user_id, verified_only=VERIFICATION_ENABLED)
        ref_earned = await db.get_referral_earned(user_id)

        await safe_edit(cq.message,
            f"{em.GIFT} **Refer & Earn**\n\n"
            f"Share your referral link and earn credits!\n\n"
            f"<blockquote>"
            f"{em.SHIELD} **{REFERRAL_VERIFY_BONUS} credit** (non-withdrawable) when your friend {'verifies' if VERIFICATION_ENABLED else 'joins'}\n"
            f"{em.MONEY} **5% of purchase amount** (withdrawable balance) every time your friend buys credits"
            f"</blockquote>\n\n"
            f"{em.LINK} **Your link:**\n`{ref_link}`\n\n"
            f"{em.USERS} Referrals: **{ref_count}**\n"
            f"{em.MONEY} Total earned: **{ref_earned}** credits",
            reply_markup=back_kb(),
        )

    @app.on_callback_query(filters.regex("^admin_panel$"))
    @verified
    async def cb_admin_panel(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        is_mod = await db.is_moderator(cq.from_user.id)
        await safe_edit(cq.message, f"{em.GEAR} **Admin Panel**\n\n"
            f"Manage numbers, users, pricing, and broadcasts.",
            reply_markup=admin_kb(is_mod))

    # ── Add Number Flow ──

    @app.on_callback_query(filters.regex("^add_number$"))
    @verified
    async def cb_add_number(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        auth_states[cq.from_user.id] = {"step": "phone"}
        await safe_edit(cq.message,
            f"{em.PHONE} **Add Number**\n\n"
            "Send the phone number in international format:\n"
            "Example: `+1234567890`\n\n"
            "Country and pricing will be detected automatically.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex("^cancel_auth$"))
    @verified
    async def cb_cancel_auth(_, cq: CallbackQuery):
        state = auth_states.pop(cq.from_user.id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await safe_edit(cq.message, f"{em.ERROR} Operation cancelled.", reply_markup=back_kb("admin_panel"))

    # ── Country confirmation after adding number ──

    # ── Account Year Adjuster ──

    @app.on_callback_query(filters.regex("^ay_edit$"))
    @verified
    async def cb_ay_edit(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return
        year = state.get("account_year")
        month = state.get("account_month")
        year_label = format_account_year(year, month)
        await safe_edit(cq.message,
            f"{em.CALENDAR} **Adjust Year Old**\n\n"
            f"Auto-detected: **{year_label}**\n"
            f"Use + / − to correct it, then tap **Set**.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data="ay_adj:-1", style=S.DEFAULT),
                    InlineKeyboardButton(year_label, callback_data="noop", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.ADD}", callback_data="ay_adj:+1", style=S.DEFAULT),
                ],
                [InlineKeyboardButton(f"{em.SUCCESS} Set", callback_data="ay_set", style=S.SUCCESS)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^ay_adj:"))
    @verified
    async def cb_ay_adj(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return
        delta = int(cq.data.split(":")[1])
        current = state.get("account_year") or 2013
        new_year = current + delta
        state["account_year"] = new_year
        month = state.get("account_month")
        year_label = format_account_year(new_year, month)
        await safe_edit(cq.message,
            f"{em.CALENDAR} **Adjust Year Old**\n\n"
            f"Use + / − to correct it, then tap **Set**.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data="ay_adj:-1", style=S.DEFAULT),
                    InlineKeyboardButton(year_label, callback_data="noop", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.ADD}", callback_data="ay_adj:+1", style=S.DEFAULT),
                ],
                [InlineKeyboardButton(f"{em.SUCCESS} Set", callback_data="ay_set", style=S.SUCCESS)],
            ]),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex("^ay_set$"))
    @verified
    async def cb_ay_set(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return
        cc = state["country_code"]
        year = state.get("account_year")
        month = state.get("account_month")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        phone = state["phone"]
        await safe_edit(cq.message,
            f"{em.SUCCESS} **Year Old set to {format_account_year(year, month)}**\n\n"
            f"{em.PHONE} `{phone}`\n"
            f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n\n"
            "Confirm and save?",
            reply_markup=_confirm_country_kb(flag, name, cc, year, month),
        )
        await cq.answer(f"Year Old set to {format_account_year(year, month)}")

    # ── Country confirmation ──

    @app.on_callback_query(filters.regex("^cc_yes$"))
    @verified
    async def cb_cc_yes(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return

        phone = state["phone"]
        cc = state["country_code"]
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        year = state.get("account_year")
        month = state.get("account_month")
        email_added = state.get("email_added", False)

        price = await db.get_category_price(cc, year, email_added)
        if price is None:
            state["step"] = "set_new_category_price"
            state["pending_cc"] = cc
            await safe_edit(cq.message,
                f"{em.MONEY} **New Category Detected!**\n\n"
                f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
                f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
                f"{em.MAIL} Email Added: **{'Yes' if email_added else 'No'}**\n\n"
                f"This combination has no set price. Please send the price (in credits) for this category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)]
                ])
            )
            return

        await db.save_session(phone, state["session_string"], cq.from_user.id,
                              password=state.get("password", ""), country_code=cc,
                              account_id=state.get("account_id"), account_year=year,
                              account_month=month, email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added, account_month=month)
        auth_states.pop(cq.from_user.id, None)

        await alert(app,
            f"{em.ADD} **Number Added**\n\n"
            f"{em.SHIELD} Admin: `{cq.from_user.id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {name}\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
            f"{em.MAIL} Email Added: **{'Yes' if email_added else 'No'}**\n"
            f"{em.MONEY} Price: {price} credits"
        )

        await safe_edit(cq.message,
            f"{em.SUCCESS} **Number added successfully!**\n\n"
            f"{em.PHONE} `{phone}` — {flag} {name}\n"
            f"{em.MONEY} Price: **{price}** credits per OTP",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_callback_query(filters.regex("^cc_no$"))
    @verified
    async def cb_cc_no(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return

        auth_states[cq.from_user.id]["step"] = "manual_country"
        await safe_edit(cq.message,
            f"{em.GLOBE} **Select Country for** `{state['phone']}`\n\n"
            "Type the country name or send its flag emoji:\n"
            "Example: `India` or `🇮🇳`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^cc_pick:"))
    @verified
    async def cb_cc_pick(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") not in ("manual_country", "confirm_country"):
            await cq.answer("No pending action.", show_alert=True)
            return

        cc = cq.data.split(":", 1)[1]
        phone = state["phone"]
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        year = state.get("account_year")
        month = state.get("account_month")
        email_added = state.get("email_added", False)

        price = await db.get_category_price(cc, year, email_added)
        if price is None:
            state["step"] = "set_new_category_price"
            state["pending_cc"] = cc
            await safe_edit(cq.message,
                f"{em.MONEY} **New Category Detected!**\n\n"
                f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
                f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
                f"{em.MAIL} Email Added: **{'Yes' if email_added else 'No'}**\n\n"
                f"This combination has no set price. Please send the price (in credits) for this category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)]
                ])
            )
            return

        await db.save_session(phone, state["session_string"], cq.from_user.id,
                              password=state.get("password", ""), country_code=cc,
                              account_id=state.get("account_id"), account_year=year,
                              account_month=month, email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added, account_month=month)
        auth_states.pop(cq.from_user.id, None)

        await alert(app,
            f"{em.ADD} **Number Added**\n\n"
            f"{em.SHIELD} Admin: `{cq.from_user.id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {name}\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
            f"{em.MAIL} Email Added: **{'Yes' if email_added else 'No'}**\n"
            f"{em.MONEY} Price: {price} credits"
        )

        await safe_edit(cq.message,
            f"{em.SUCCESS} **Number added successfully!**\n\n"
            f"{em.PHONE} `{phone}` — {flag} {name}\n"
            f"{em.MONEY} Price: **{price}** credits per OTP",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.text & filters.private & ~filters.command([
        "start", "help", "cancel", "broadcast", "info", "feedback", "wotp",
    ]))
    async def on_text(_, message: Message):
        db.begin_user_cache()
        user_id = message.from_user.id
        text = message.text.strip()

        # ── Feedback response flow ──
        fstate = feedback_states.get(user_id)
        if fstate:
            if time.time() <= fstate["expiry"]:
                rating = fstate["rating"]
                feedback_states.pop(user_id, None)
                username = message.from_user.username
                user_mention = message.from_user.mention or message.from_user.first_name or f"User {user_id}"
                user_str = f"{user_mention} (@{username})" if username else user_mention

                alert_text = (
                    f"📩 **New User Feedback Received**\n\n"
                    f"{em.USER} **User:** {user_str} (`{user_id}`)\n"
                    f"⭐ **Rating:** {rating}/5\n"
                    f"💬 **Feedback:**\n{text}"
                )
                await alert(app, alert_text)
                await message.reply(
                    f"{em.CHECK} **Thank you for your feedback!**\n\nYour message has been sent to our team.",
                    reply_markup=main_menu_kb(await db.is_admin(user_id)),
                )
                return
            else:
                feedback_states.pop(user_id, None)

        pstate = pay_states.get(user_id)
        if pstate:
            await _handle_tx_hash(message, text, pstate)
            return

        # ── Sell Account auth flow ──
        sstate = sell_states.get(user_id)
        if sstate:
            step = sstate["step"]
            if step == "sell_phone":
                await _handle_sell_phone(message, text)
            elif step == "sell_code":
                await _handle_sell_code(message, text)
            elif step == "sell_password":
                await _handle_sell_password(message, text)
            elif step == "sell_withdrawal_details":
                await _handle_sell_withdrawal_details(message, text)
            return

        # ── Admin Withdrawal flow ──
        awstate = admin_withdraw_states.get(user_id)
        if awstate:
            step = awstate["step"]
            if step == "admin_withdrawal_amount":
                await _handle_admin_withdrawal_amount(message, text)
            elif step == "admin_withdrawal_details":
                await _handle_admin_withdrawal_details(message, text)
            return

        state = auth_states.get(user_id)
        if not state:
            # Checked last: a bare coupon code must never shadow an active flow.
            await _handle_coupon_text(message, text)
            return

        step = state["step"]
        if step == "phone":
            await _handle_phone(message, text)
        elif step == "code":
            await _handle_code(message, text)
        elif step == "password":
            await _handle_password(message, text)
        elif step == "update_category_price_input":
            await _handle_update_category_price(message, text)
        elif step == "manual_country":
            await _handle_manual_country(message, text)
        elif step == "update_password_old":
            await _handle_update_password_old(message, text)
        elif step == "update_password_new":
            await _handle_update_password_new(message, text)
        elif step == "rz_custom_amount":
            await _handle_rz_custom_amount(message, text)
        elif step == "cr_custom_amount":
            await _handle_cr_custom_amount(message, text)
        elif step == "stars_custom_amount":
            await _handle_stars_custom_amount(message, text)
        elif step == "set_new_category_price":
            await _handle_set_new_category_price(message, text)
        elif step == "edit_num_country":
            await _handle_edit_num_country(message, text)
        elif step == "edit_num_set_price":
            await _handle_edit_num_set_price(message, text)
        elif step == "wa_add_phone":
            await _handle_wa_add_phone(message, text)
        elif step == "wa_add_price":
            await _handle_wa_add_price(message, text)
        elif step == "wa_set_price":
            await _handle_wa_set_price(message, text)


    # ── Country Pricing ──

    @app.on_callback_query(filters.regex(r"^country_pricing$|^pg_cp:\d+$"))
    @verified
    async def cb_country_pricing(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_cp:") else 0

        sessions = await db.get_all_sessions()

        countries = {}
        for s in sessions:
            cc = s.get("country_code", "XX")
            if cc not in countries:
                countries[cc] = {"total": 0, "active": 0}
            countries[cc]["total"] += 1
            if s.get("status") == "active":
                countries[cc]["active"] += 1

        if not countries:
            await safe_edit(cq.message,
                f"{em.MONEY} **Country Pricing**\n\nNo numbers added yet.",
                reply_markup=back_kb("admin_panel"),
            )
            return

        all_lines = []
        all_buttons = []
        for cc in sorted(countries.keys()):
            flag = get_country_flag(cc)
            name = get_country_name(cc)
            info = countries[cc]
            
            cat_prices = await db.get_category_prices(cc)
            if cat_prices:
                prices_list = [c["price"] for c in cat_prices]
                min_p = min(prices_list)
                max_p = max(prices_list)
                range_str = f"{min_p}-{max_p}" if min_p != max_p else f"{min_p}"
                display_str = f"{range_str} credits per OTP"
                btn_str = f"{range_str} cr"
            else:
                display_str = "No price set"
                btn_str = "No price set"

            all_lines.append(f"{flag} **{name}** ({cc}) — **({display_str})** — {info['active']}/{info['total']} numbers")
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {name} — {btn_str}",
                callback_data=f"setcprice:{cc}", style=S.DEFAULT,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_cp", "admin_panel")
        start = page * PAGE_SIZE
        page_lines = all_lines[start:start + PAGE_SIZE]
        await safe_edit(cq.message,
            f"{em.MONEY} **Country Pricing**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^setcprice:"))
    @verified
    async def cb_setcprice(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        cc = cq.data.split(":", 1)[1]
        flag = get_country_flag(cc)
        name = get_country_name(cc)

        cat_prices = await db.get_category_prices(cc)
        
        buttons = []
        lines = []
        
        if not cat_prices:
            lines.append("No category prices configured yet.")
        else:
            for cat in cat_prices:
                year = cat.get("year", 2025)
                month = cat.get("month")
                email = cat.get("email_added", False)
                price = cat.get("price", 1)
                email_str = "Yes" if email else "No"
                
                lines.append(f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}** | {em.MAIL} Email: **{email_str}** — **{price}** cr")
                buttons.append([InlineKeyboardButton(
                    f"{em.EDIT} {format_account_year(year, month)} | Email: {email_str} — {price} cr",
                    callback_data=f"editcat:{cc}:{year}:{email}", style=S.DEFAULT,
                )])
        
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="country_pricing", style=S.DEFAULT)])

        await safe_edit(cq.message,
            f"{em.MONEY} **Category Pricing — {flag} {name} ({cc})**\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^editcat:"))
    @verified
    async def cb_editcat(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        parts = cq.data.split(":")
        cc = parts[1]
        year = int(parts[2])
        email = parts[3] == "True"
        
        auth_states[cq.from_user.id] = {
            "step": "update_category_price_input",
            "country_code": cc,
            "year": year,
            "email_added": email,
        }
        
        email_str = "Yes" if email else "No"
        await safe_edit(cq.message,
            f"{em.MONEY} **Update Category Price**\n\n"
            f"{em.GLOBE} Country: {get_country_flag(cc)} {get_country_name(cc)}\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(year)}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n\n"
            f"Send the new price (in credits) for this category:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data=f"setcprice:{cc}", style=S.DANGER)]
            ])
        )

    # ── List Numbers (Admin) ──

    @app.on_callback_query(filters.regex(r"^list_numbers$|^pg_ln:\d+$"))
    @verified
    async def cb_list_numbers(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ln:") else 0

        sessions = [s for s in await db.get_all_sessions() if s.get("status") != "sold"]
        if not sessions:
            await safe_edit(cq.message,
                f"{em.PLAN} **No numbers added yet.**\n\n"
                f"Tap **Add Number** in the admin panel to get started.",
                reply_markup=back_kb("admin_panel"),
            )
            return

        by_country = {}
        for s in sessions:
            cc = s.get("country_code", "XX")
            by_country.setdefault(cc, []).append(s)

        country_lines = []
        for cc in sorted(by_country.keys()):
            flag = get_country_flag(cc)
            name = get_country_name(cc)
            count = len(by_country[cc])
            country_lines.append(f"{flag} {name}: **{count}**")

        summary = (
            f"{em.PLAN} **Registered Numbers:** {len(sessions)} total\n\n"
            + "\n".join(country_lines)
        )

        all_buttons = []
        # ponytail: one category_pricing query for the whole page instead of
        # one per session. Unpriced sessions come back as None, same as before.
        prices = await db.get_session_prices(sessions)
        for cc in sorted(by_country.keys()):
            flag = get_country_flag(cc)
            for s in by_country[cc]:
                phone = s["phone_number"]
                status_icon = {"active": f"{em.ONLINE}", "sold": f"{em.OFFLINE}", "error": f"{em.WARNING}", "unlisted": f"{em.BLOCKED}"}.get(s.get("status"), f"{em.IDLE}")
                acc_year = s.get("account_year")
                acc_month = s.get("account_month")
                year_str = f" ~{format_account_year(acc_year, acc_month)}" if acc_year else ""
                p = prices.get(phone)
                price_str = f"{p} cr" if p is not None else "No price"
                all_buttons.append([InlineKeyboardButton(
                    f"{status_icon} {flag} {phone}{year_str} — {price_str}",
                    callback_data=f"num_actions:{phone}", style=S.DEFAULT,
                )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_ln", "admin_panel")

        await safe_edit(cq.message,
            summary + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^rm:"))
    @verified
    async def cb_remove_number(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        await safe_edit(cq.message,
            f"{em.WARNING} Remove `{phone}` and disconnect its session?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{em.SUCCESS} Yes", callback_data=f"confirm_rm:{phone}", style=S.SUCCESS),
                    InlineKeyboardButton(f"{em.ERROR} No", callback_data="list_numbers", style=S.DANGER),
                ],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^confirm_rm:"))
    @verified
    async def cb_confirm_remove(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        cc = session.get("country_code", "XX") if session else "XX"
        flag = get_country_flag(cc)
        cname = get_country_name(cc)
        await clients.remove_client(phone)
        await alert(app,
            f"{em.DELETE} **Number Removed**\n\n"
            f"{em.SHIELD} Admin: `{cq.from_user.id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {cname}"
        )
        await safe_edit(cq.message,
            f"{em.SUCCESS} `{phone}` removed and session disconnected.",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Per-number actions ──

    @app.on_callback_query(filters.regex(r"^num_actions:"))
    @verified
    async def cb_num_actions(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        cc = session.get("country_code", "XX")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        price = await db.get_session_price(session)
        price_str = f"{price} credits" if price is not None else "Not set (not for sale)"
        status = session.get("status", "unknown")
        pwd = session.get("password", "")
        error = session.get("last_error", "")
        acc_year = session.get("account_year")
        acc_month = session.get("account_month")
        age_line = f"{em.CALENDAR} **Year Old:** ~{format_account_year(acc_year, acc_month)}\n" if acc_year else ""
        email_added = session.get("email_added", False)
        email_line = f"{em.MAIL} **Email Added:** {'Yes' if email_added else 'No'}\n"

        info = (
            f"{em.PHONE} **Number Details**\n\n"
            f"<blockquote>"
            f"{em.PHONE} **Number:** `{phone}`\n"
            f"{flag} **Country:** {name} ({cc})\n"
            f"{em.STATS} Status: **{status}**\n"
            f"{em.MONEY} Price: **{price_str}**\n"
            f"{age_line}"
            f"{email_line}"
            f"{em.PASSWORD} Password: {'`' + pwd + '`' if pwd else 'Not set'}"
            f"</blockquote>\n"
        )
        if error:
            info += f"❗ Last error: `{error[:120]}`\n"

        buttons = [
            [
                InlineKeyboardButton(f"{em.SEARCH} Verify", callback_data=f"verify:{phone}", style=S.PRIMARY),
                InlineKeyboardButton(f"{em.PASSWORD} Update Password", callback_data=f"updpwd:{phone}", style=S.DEFAULT),
            ],
            [
                InlineKeyboardButton(f"{em.CONFIG} Edit Category", callback_data=f"editnum:{phone}", style=S.DEFAULT),
                InlineKeyboardButton(f"{em.ERROR} Remove", callback_data=f"rm:{phone}", style=S.DANGER),
            ],
        ]
        if status == "active":
            buttons.insert(0, [InlineKeyboardButton(
                f"{em.OFFLINE} Unlist from Sale", callback_data=f"unlist:{phone}", style=S.DANGER,
            )])
        elif status != "sold":
            buttons.insert(0, [InlineKeyboardButton(
                f"{em.PENDING} Re-list for Sale", callback_data=f"relist:{phone}", style=S.SUCCESS,
            )])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="list_numbers", style=S.DEFAULT)])
        await safe_edit(cq.message, info, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(filters.regex(r"^relist:"))
    @verified
    async def cb_relist(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        if session.get("status") == "sold":
            await cq.answer(f"{em.ERROR} Cannot re-list a sold number.", show_alert=True)
            return

        await safe_edit(cq.message, f"{em.LOADING} Verifying `{phone}` before re-listing...")

        ok, error = await clients.verify_session(phone, session["session_string"])
        if ok:
            await db.set_session_status(phone, "active")
            await safe_edit(cq.message,
                f"{em.SUCCESS} `{phone}` — session is **valid** and ready for sale!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                ]),
            )
        else:
            await db.set_session_status(phone, "error", error)
            await safe_edit(cq.message,
                f"{em.ERROR} `{phone}` — verification failed during re-listing.\n\n"
                f"❗ Error: `{error[:200]}`\n\n"
                "Would you like to re-add this number?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.PENDING} Re-add Number", callback_data=f"readd:{phone}", style=S.PRIMARY)],
                    [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                ]),
            )

        cc = session.get("country_code", "XX")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        await safe_edit(cq.message,
            f"{em.SUCCESS} `{phone}` ({flag} {name}) is now **active** and available for sale.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^unlist:"))
    @verified
    async def cb_unlist(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await db.set_session_status(phone, "unlisted")
        await cq.answer(f"{em.SUCCESS} Number unlisted!")

        cc = session.get("country_code", "XX")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        await safe_edit(cq.message,
            f"{em.OFFLINE} `{phone}` ({flag} {name}) is now **unlisted** and hidden from buyers.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
            ]),
        )

    # ── Sold Numbers ──

    @app.on_callback_query(filters.regex(r"^sold_list$|^pg_sl:\d+$"))
    @verified
    async def cb_sold_list(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_sl:") else 0

        sold = await db.get_sold_sessions()
        if not sold:
            await safe_edit(cq.message,
                f"{em.OFFLINE} **No sold numbers yet.**\n\n"
                f"Numbers appear here once a user receives an OTP.",
                reply_markup=back_kb("admin_panel"),
            )
            return

        all_buttons = []
        for s in sold:
            phone = s["phone_number"]
            cc = s.get("country_code", "XX")
            flag = get_country_flag(cc)
            sold_price = s.get("sold_price", 0)
            acc_year = s.get("account_year")
            acc_month = s.get("account_month")
            year_str = f" ~{format_account_year(acc_year, acc_month)}" if acc_year else ""
            all_buttons.append([InlineKeyboardButton(
                f"{em.OFFLINE} {flag} {phone}{year_str} — {sold_price} cr",
                callback_data=f"sold_detail:{phone}", style=S.DEFAULT,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_sl", "admin_panel")

        await safe_edit(cq.message,
            f"{em.OFFLINE} **Sold Numbers:** {len(sold)} total" + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^sold_detail:"))
    @verified
    async def cb_sold_detail(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        cc = session.get("country_code", "XX")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        acc_year = session.get("account_year")
        email_added = session.get("email_added", False)
        sold_to = session.get("sold_to")
        sold_at = session.get("sold_at")
        sold_price = session.get("sold_price", 0)
        order_id = session.get("order_id")

        buyer_line = ""
        if sold_to:
            buyer = await db.get_user(sold_to)
            if buyer:
                bname = buyer.get("first_name") or buyer.get("username") or str(sold_to)
                buyer_line = f"{em.USER} **Buyer:** {bname} (`{sold_to}`)\n"
            else:
                buyer_line = f"{em.USER} **Buyer ID:** `{sold_to}`\n"

        sold_time = ""
        if sold_at:
            sold_time = f"{em.CLOCK} **Sold At:** {sold_at.strftime('%Y-%m-%d %H:%M UTC')}\n"

        acc_month = session.get("account_month")
        age_line = f"{em.CALENDAR} **Year Old:** ~{format_account_year(acc_year, acc_month)}\n" if acc_year else ""
        email_line = f"{em.MAIL} **Email Added:** {'Yes' if email_added else 'No'}\n"

        order_line = f"{em.RECEIPT} **Order ID:** `{order_id}`\n" if order_id else ""

        info = (
            f"{em.OFFLINE} **Sold Number**\n\n"
            f"<blockquote>"
            f"{order_line}"
            f"{em.PHONE} **Number:** `{phone}`\n"
            f"{flag} **Country:** {name} ({cc})\n"
            f"{em.MONEY} **Price Paid:** {sold_price} credits\n"
            f"{buyer_line}"
            f"{sold_time}"
            f"{age_line}"
            f"{email_line}".rstrip("\n")
            + "</blockquote>"
        )

        await safe_edit(cq.message, info,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{em.SEARCH} Verify", callback_data=f"verify:{phone}", style=S.PRIMARY),
                    InlineKeyboardButton(f"{em.PENDING} Re-list for Sale", callback_data=f"relist:{phone}", style=S.SUCCESS),
                ],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data="sold_list", style=S.DEFAULT)],
            ]),
        )

    # ── Edit Number Category ──

    async def _edit_category_view(message, phone, session, prefix=""):
        """Show edit category panel. If category price is missing, prompt to set it."""
        cc = session.get("country_code", "XX")
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        year = session.get("account_year")
        month = session.get("account_month")
        email = session.get("email_added", False)
        year_label = format_account_year(year, month)
        email_str = "Yes" if email else "No"

        cat_price = await db.get_category_price(cc, year, email)
        if cat_price is None:
            auth_states[message.chat.id] = {
                "step": "edit_num_set_price",
                "phone": phone,
            }
            await safe_edit(message,
                f"{em.WARNING} **New Category Detected!**\n\n"
                f"{em.PHONE} Number: `{phone}`\n"
                f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
                f"{em.CALENDAR} Year Old: **{year_label}**\n"
                f"{em.MAIL} Email Added: **{email_str}**\n\n"
                f"No price set for this combination.\n"
                f"Send the price (in credits) for this category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data=f"editnum:{phone}", style=S.DANGER)]
                ]),
            )
            return

        price = await db.get_session_price(session)
        await safe_edit(message,
            f"{prefix}"
            f"{em.CONFIG} **Edit Category — `{phone}`**\n\n"
            f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n"
            f"{em.MONEY} Current Price: **{price}** credits\n\n"
            "Select what to change:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.GLOBE} Change Country ({cc})", callback_data=f"echg_cc:{phone}", style=S.PRIMARY)],
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data=f"echg_yr:{phone}:-1", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.CALENDAR} Year Old: {year_label}", callback_data="noop", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.ADD}", callback_data=f"echg_yr:{phone}:+1", style=S.DEFAULT),
                ],
                [InlineKeyboardButton(
                    f"{em.MAIL} Email: {email_str} — Tap to toggle",
                    callback_data=f"echg_em:{phone}", style=S.DEFAULT,
                )],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^editnum:"))
    @verified
    async def cb_editnum(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await _edit_category_view(cq.message, phone, session)

    @app.on_callback_query(filters.regex(r"^echg_yr:"))
    @verified
    async def cb_echg_yr(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        parts = cq.data.split(":")
        phone = parts[1]
        delta = int(parts[2])

        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        current = session.get("account_year") or 2013
        new_year = current + delta
        await db.set_session_category(phone, account_year=new_year)
        session["account_year"] = new_year

        await _edit_category_view(cq.message, phone, session)
        await cq.answer(f"Year set to {new_year}")

    @app.on_callback_query(filters.regex(r"^echg_em:"))
    @verified
    async def cb_echg_em(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        new_email = not session.get("email_added", False)
        await db.set_session_category(phone, email_added=new_email)
        session["email_added"] = new_email

        await _edit_category_view(cq.message, phone, session)
        await cq.answer(f"Email toggled to {'Yes' if new_email else 'No'}")

    @app.on_callback_query(filters.regex(r"^echg_cc:"))
    @verified
    async def cb_echg_cc(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        auth_states[cq.from_user.id] = {
            "step": "edit_num_country",
            "phone": phone,
        }
        await safe_edit(cq.message,
            f"{em.GLOBE} **Change Country for** `{phone}`\n\n"
            "Type the country name or send its flag emoji:\n"
            "Example: `India` or `🇮🇳`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data=f"editnum:{phone}", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^echg_ccpick:"))
    @verified
    async def cb_echg_ccpick(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "edit_num_country":
            await cq.answer("No pending action.", show_alert=True)
            return

        cc = cq.data.split(":", 1)[1]
        phone = state["phone"]
        auth_states.pop(cq.from_user.id, None)

        await db.set_session_category(phone, country_code=cc)

        flag = get_country_flag(cc)
        name = get_country_name(cc)
        await cq.answer(f"Country set to {flag} {name}")

        session = await db.get_session(phone)
        if session:
            await _edit_category_view(cq.message, phone, session, prefix=f"{em.SUCCESS} Country updated!\n\n")

    @app.on_callback_query(filters.regex(r"^verify:"))
    @verified
    async def cb_verify(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await safe_edit(cq.message, f"{em.LOADING} Verifying `{phone}`...")

        ok, error = await clients.verify_session(phone, session["session_string"])
        current_status = session.get("status")
        if current_status == "sold":
            if ok:
                await safe_edit(cq.message,
                    f"{em.SUCCESS} `{phone}` — session is **valid** (remains marked as **sold**).",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                    ]),
                )
            else:
                await safe_edit(cq.message,
                    f"{em.ERROR} `{phone}` — session is **invalid** (remains marked as **sold**).\n\n"
                    f"❗ Error: `{error[:200]}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                    ]),
                )
        else:
            if ok:
                await db.set_session_status(phone, "active")
                await safe_edit(cq.message,
                    f"{em.SUCCESS} `{phone}` — session is **valid** and ready for sale!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                    ]),
                )
            else:
                await db.set_session_status(phone, "error", error)
                await safe_edit(cq.message,
                    f"{em.ERROR} `{phone}` — verification failed\n\n"
                    f"❗ Error: `{error[:200]}`\n\n"
                    "Would you like to re-add this number?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.PENDING} Re-add Number", callback_data=f"readd:{phone}", style=S.PRIMARY)],
                        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                    ]),
                )

    @app.on_callback_query(filters.regex(r"^readd:"))
    @verified
    async def cb_readd(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        old_session = await db.get_session(phone)
        old_cc = old_session.get("country_code", "XX") if old_session else "XX"
        auth_states[cq.from_user.id] = {"step": "phone", "prefill_phone": phone, "old_country": old_cc}
        await safe_edit(cq.message,
            f"{em.PENDING} **Re-adding** `{phone}`\n\n"
            "A new code will be sent. Enter the verification code when received.",
        )
        await _handle_phone_direct(cq.from_user.id, phone, cq.message)

    @app.on_callback_query(filters.regex(r"^updpwd:"))
    @verified
    async def cb_updpwd(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await safe_edit(cq.message, f"{em.LOADING} Connecting to `{phone}`...")

        client = Client(
            name=f"pwdupd_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session["session_string"],
            in_memory=True,
        )
        try:
            await client.start()
            await client.get_me()
        except Exception as e:
            try:
                await client.stop()
            except Exception:
                pass
            await safe_edit(cq.message,
                f"{em.ERROR} Failed to connect: `{e}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
                ]),
            )
            return

        auth_states[cq.from_user.id] = {
            "step": "update_password_old",
            "phone": phone,
            "client": client,
            "db_password": session.get("password", ""),
        }

        if session.get("password"):
            await safe_edit(cq.message,
                f"{em.PASSWORD} **Update Password for** `{phone}`\n\n"
                f"Current stored password: `{session['password']}`\n\n"
                "Send the **current 2FA password** to verify:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)],
                ]),
            )
        else:
            await safe_edit(cq.message,
                f"{em.PASSWORD} **Update Password for** `{phone}`\n\n"
                "No password stored. Send the **current 2FA password**:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)],
                ]),
            )

    # ── Users ──

    @app.on_callback_query(filters.regex(r"^users_list$|^pg_ul:\d+$"))
    @verified
    async def cb_users_list(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ul:") else 0

        all_users = await db.get_all_users()
        sold = await db.get_sold_sessions()
        buyer_ids = {s["sold_to"] for s in sold if "sold_to" in s}
        users = [u for u in all_users if u.get("credits", 0) > 0 or u["telegram_id"] in buyer_ids]

        if not users:
            await safe_edit(cq.message,
                f"{em.USERS} **No users with credits or purchases yet.**",
                reply_markup=back_kb("admin_panel"),
            )
            return

        all_buttons = []
        for u in users:
            u_role = await db.get_user_role(u["telegram_id"])
            role_icon = f"{em.OWNER}" if u_role in ("admin", "moderator") else f"{em.USER}"
            name = u.get("first_name") or u.get("username") or str(u["telegram_id"])
            credits = u.get("credits", 0)
            all_buttons.append([
                InlineKeyboardButton(
                    f"{role_icon} {name} — {em.MONEY} {credits}",
                    callback_data=f"noop", style=S.DEFAULT,
                )
            ])

        page_btns = all_buttons[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

        total_pages = (len(all_buttons) + PAGE_SIZE - 1) // PAGE_SIZE
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(f"{em.BACK} Prev", callback_data=f"pg_ul:{page - 1}", style=S.DEFAULT))
        if (page + 1) * PAGE_SIZE < len(all_buttons):
            nav.append(InlineKeyboardButton(f"{em.NEXT} Next", callback_data=f"pg_ul:{page + 1}", style=S.PRIMARY))
        if nav:
            page_btns.append(nav)
        page_label = f"\n\n{em.LIST} Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        page_btns.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)])

        await safe_edit(cq.message,
            f"{em.USERS} **Users** ({len(users)})" + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns),
        )

    # ── Info ──

    @app.on_message(filters.command("info") & filters.private)
    @verified
    async def cmd_info(_, message: Message):
        parts = message.text.split()
        query = parts[1].strip() if len(parts) == 2 else ""

        # Order lookup — any verified user can check their own order by ID.
        if query.upper().startswith("ORD-"):
            oid = query.upper()
            is_admin = await db.is_admin(message.from_user.id)

            # Live order: still assigned, no sale yet — offer a Release button for a speedy refund.
            live = await db.get_active_assignment_by_order_id(oid)
            if live and (is_admin or live.get("user_id") == message.from_user.id):
                phone = live["phone_number"]
                cc = (await db.get_session(phone) or {}).get("country_code", "XX")
                flag = get_country_flag(cc)
                cname = get_country_name(cc)
                phone_disp = phone if is_admin else mask_phone(phone)
                otp_done = live.get("otp_received", False)
                status = "OTP received — sold on release" if otp_done else "Live — release for a refund"
                kb = None
                if not otp_done and is_admin:  # only admins can trigger the speedy release/refund
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.UNLOCK} Release & Refund", callback_data=f"release:{phone}", style=S.DANGER)],
                    ])
                await message.reply(
                    f"{em.RECEIPT} **Order Details**\n\n"
                    f"<blockquote>"
                    f"{em.RECEIPT} Order ID: `{oid}`\n"
                    f"{em.PHONE} Number: `{phone_disp}`\n"
                    f"{flag} Country: {cname} ({cc})\n"
                    f"{em.MONEY} Price: **{live.get('price', 0)}** credits\n"
                    f"{em.TIMER} Status: {status}"
                    f"</blockquote>",
                    reply_markup=kb,
                )
                return

            # WhatsApp orders live in their own collection — same ORD- format, so
            # look here too or a buyer pasting their WA order id gets "not found".
            wa = await db.get_wa_order(oid)
            if wa and (is_admin or wa.get("buyer_id") == message.from_user.id):
                wcc = wa.get("country_code", "XX")
                wstatus = wa.get("status")
                wphone = wa["phone_number"]
                # Number stays masked until an admin has actually connected.
                wphone_disp = wphone if (is_admin or wstatus in ("confirmed", "sold")) else mask_phone(wphone)
                next_step = {
                    "pending": "Waiting for an admin to connect. You're refunded in full if they can't fulfil it.",
                    "confirmed": "Admin is connected — request the OTP on this number and wait here for the code.",
                    "sold": f"Completed — OTP sent: `{wa.get('otp_code', '—')}`",
                }.get(wstatus, "—")
                await message.reply(
                    f"{em.SMS} **WhatsApp Order Details**\n\n"
                    f"<blockquote>"
                    f"{em.RECEIPT} Order ID: `{oid}`\n"
                    f"{em.PHONE} Number: `{wphone_disp}`\n"
                    f"{get_country_flag(wcc)} Country: {get_country_name(wcc)} ({wcc})\n"
                    f"{em.MONEY} Price: **{wa.get('price', 0)}** credits\n"
                    f"{em.TIMER} Status: **{wstatus}**\n\n"
                    f"{next_step}"
                    f"</blockquote>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{em.SMS} My Order", callback_data="wa_mine", style=S.PRIMARY)],
                    ]) if wstatus in ("pending", "confirmed") else None,
                )
                return

            session = await db.get_session_by_order_id(oid)
            if not session or (not is_admin and session.get("sold_to") != message.from_user.id):
                await message.reply(f"{em.ERROR} Order `{oid}` not found.")
                return

            phone = session["phone_number"]
            cc = session.get("country_code", "XX")
            flag = get_country_flag(cc)
            cname = get_country_name(cc)
            sold_at = session.get("sold_at")
            sold_str = sold_at.strftime("%Y-%m-%d %H:%M UTC") if sold_at else "—"
            price = session.get("sold_price", 0)
            phone_disp = phone if is_admin else mask_phone(phone)

            await message.reply(
                f"{em.RECEIPT} **Order Details**\n\n"
                f"<blockquote>"
                f"{em.RECEIPT} Order ID: `{session.get('order_id')}`\n"
                f"{em.PHONE} Number: `{phone_disp}`\n"
                f"{flag} Country: {cname} ({cc})\n"
                f"{em.MONEY} Price: **{price}** credits\n"
                f"{em.CALENDAR} Purchased: {sold_str}"
                f"</blockquote>",
            )
            return

        if not await db.is_admin(message.from_user.id):
            await message.reply(f"{em.BLOCKED} Admin only.")
            return

        if len(parts) != 2:
            await message.reply(
                "**Usage:** `/info <userid or @username>` — user lookup (admin)\n"
                "`/info <ORD-XXXXXXXX>` — order lookup\n"
                "**Example:** `/info 123456789` or `/info ORD-1A2B3C4D`"
            )
            return

        if query.startswith("@"):
            user = await db.db.users.find_one({"username": query.lstrip("@")})
        else:
            try:
                user = await db.get_user(int(query))
            except ValueError:
                user = await db.db.users.find_one({"username": query})

        if not user:
            await message.reply(f"{em.ERROR} User not found.")
            return

        uid = user["telegram_id"]
        uname = user.get("username") or "—"
        fname = user.get("first_name") or "—"
        role = await db.get_user_role(uid)
        credits = user.get("credits", 0)
        verified_status = user.get("verified", False)
        referred_by = user.get("referred_by")
        ref_earned = user.get("referral_earned", 0)
        ref_count = await db.get_referral_count(uid, verified_only=VERIFICATION_ENABLED)
        created = user.get("created_at")
        created_str = created.strftime("%Y-%m-%d %H:%M UTC") if created else "—"

        role_icon = f"{em.OWNER}" if role in ("admin", "moderator") else f"{em.USER}"
        verified_icon = f"{em.VERIFIED}" if verified_status else f"{em.UNVERIFIED}"

        ref_line = f"\n{em.LINK} Referred by: `{referred_by}`" if referred_by else ""

        # Seller & Buyer Info
        seller_info = await db.get_user_seller_details(uid)
        buyer_info = await db.get_user_buyer_details(uid)

        s_sold_cnt = seller_info["total_sold_count"]
        s_listed_cnt = seller_info["total_listed_count"]
        s_earned = seller_info["earned_total"]
        s_balance = seller_info["balance"]

        if s_sold_cnt > 0 or s_listed_cnt > 0 or s_earned > 0:
            seller_lines = [
                f"🏷 **Seller Info**",
                f"• Total Numbers Sold: **{s_sold_cnt}**",
                f"• Total Numbers Listed: **{s_listed_cnt}**",
                f"• Total Earned: **{s_earned}** credits | Balance: **{s_balance}** credits",
            ]
            if seller_info["sold_numbers"]:
                sold_items = []
                for item in seller_info["sold_numbers"][:10]:
                    ph = item["phone_number"]
                    payout = item.get("payout", 0)
                    sat = item.get("sold_at")
                    sat_str = sat.strftime("%Y-%m-%d %H:%M") if sat else ""
                    ts_str = f" ({sat_str})" if sat_str else ""
                    sold_items.append(f"  • `{ph}` (+{payout} cr){ts_str}")
                rem = len(seller_info["sold_numbers"]) - 10
                if rem > 0:
                    sold_items.append(f"  *...and {rem} more sold*")
                seller_lines.append("📤 **Numbers Sold:**\n" + "\n".join(sold_items))

            if seller_info["listed_numbers"]:
                listed_items = []
                for item in seller_info["listed_numbers"][:10]:
                    ph = item["phone_number"]
                    st = item.get("status", "active")
                    listed_items.append(f"  • `{ph}` [{st}]")
                rem = len(seller_info["listed_numbers"]) - 10
                if rem > 0:
                    listed_items.append(f"  *...and {rem} more listed*")
                seller_lines.append("📋 **Numbers Listed:**\n" + "\n".join(listed_items))
            seller_block = "\n\n" + "\n".join(seller_lines)
        else:
            seller_block = "\n\n🏷 **Seller Info:** None (0 sold / 0 listed)"

        b_count = buyer_info["total_bought_count"]
        if b_count > 0:
            buyer_lines = [
                f"🛍 **Buyer Info**",
                f"• Total Numbers Bought: **{b_count}**",
            ]
            bought_items = []
            for item in buyer_info["bought_numbers"][:10]:
                ph = item["phone_number"]
                pr = item.get("price", 0)
                sat = item.get("sold_at")
                sat_str = sat.strftime("%Y-%m-%d %H:%M") if sat else ""
                ts_str = f" ({sat_str})" if sat_str else ""
                bought_items.append(f"  • `{ph}` ({pr} cr){ts_str}")
            rem = b_count - 10
            if rem > 0:
                bought_items.append(f"  *...and {rem} more bought*")
            buyer_lines.append("📥 **Bought Numbers:**\n" + "\n".join(bought_items))
            buyer_block = "\n\n" + "\n".join(buyer_lines)
        else:
            buyer_block = "\n\n🛍 **Buyer Info:** None (0 bought)"

        # Recent payments/withdrawals — most recent first.
        transactions = await db.get_user_transactions(uid, limit=5)
        if transactions:
            pay_lines = []
            for t in transactions:
                t_at = t.get("created_at")
                t_str = t_at.strftime("%Y-%m-%d %H:%M UTC") if t_at else "—"
                if t.get("transaction_type") == "withdrawal":
                    w_amount = t.get("amount", 0)
                    w_method = t.get("method", "—")
                    w_details = t.get("details", "")
                    w_status = t.get("status", "pending")
                    pay_lines.append(
                        f"• 🔴 **-{w_amount} cr** (Withdrawal via {w_method} to `{w_details}`) [{w_status}] — {t_str}"
                    )
                else:
                    amount = t.get("amount", 0)
                    currency = t.get("currency", "")
                    method = t.get("method", "—")
                    p_credits = t.get("credits")
                    credits_str = f" → **{p_credits}** credits" if p_credits is not None else ""
                    pay_lines.append(
                        f"• 🟢 **{amount} {currency}** ({method}){credits_str} — {t_str}"
                    )
            payments_block = (
                f"\n\n{em.RECEIPT} **Recent Payments & Withdrawals** ({len(transactions)})\n"
                f"<blockquote>" + "\n".join(pay_lines) + "</blockquote>"
            )
        else:
            payments_block = f"\n\n{em.RECEIPT} **Recent Payments & Withdrawals:** none"

        await message.reply(
            f"{role_icon} **User Info**\n\n"
            f"<blockquote>"
            f"{em.ID_BADGE} ID: `{uid}`\n"
            f"📛 Name: **{fname}**\n"
            f"{em.USER} Username: @{uname}\n"
            f"{em.SHIELD} Role: **{role}**\n"
            f"{verified_icon} Verified: **{'Yes' if verified_status else 'No'}**\n"
            f"{em.MONEY} Credits: **{credits}**\n"
            f"{em.CALENDAR} Joined: {created_str}\n"
            f"{em.GIFT} Referrals: **{ref_count}** | Earned: **{ref_earned}**{ref_line}"
            f"</blockquote>"
            f"{seller_block}"
            f"{buyer_block}"
            f"{payments_block}",
        )

    # ── Broadcast ──

    @app.on_callback_query(filters.regex("^broadcast_help$"))
    @verified
    async def cb_broadcast_help(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await safe_edit(cq.message,
            f"{em.BROADCAST} **Broadcast Message**\n\n"
            "Reply to any message with:\n\n"
            "`/broadcast` — copies the message to all users (no sender shown)\n"
            "`/broadcast -name` — forwards the message (original sender visible)\n\n"
            f"{em.PIN} Must be used as a **reply** to the message you want to broadcast.",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.command("broadcast") & filters.private)
    @verified
    async def cmd_broadcast(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply(f"{em.BLOCKED} Admin only.")
            return

        target = message.reply_to_message
        if not target:
            await message.reply(
                f"{em.ERROR} **Reply to a message** to broadcast it.\n\n"
                "`/broadcast` — copy (no sender shown)\n"
                "`/broadcast -name` — forward (sender visible)"
            )
            return

        args = message.text.split(None, 1)
        flag = args[1].strip().lower() if len(args) > 1 else ""
        include_name = flag == "-name"

        if flag and not include_name:
            await message.reply(f"{em.ERROR} Unknown flag. Use `/broadcast` or `/broadcast -name`.")
            return

        users = await db.get_all_users()
        status_msg = await message.reply(f"{em.LOADING} Broadcasting to {len(users)} users...")

        sent = 0
        failed = 0
        for user in users:
            uid = user.get("telegram_id")
            if not uid:
                continue
            try:
                if include_name:
                    await target.forward(uid)
                else:
                    await target.copy(uid)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # ~20 msg/s, within Telegram limits

        await safe_edit(
            status_msg,
            f"{em.SUCCESS} **Broadcast complete!**\n\n"
            f"{em.MAIL} Sent: **{sent}**\n"
            f"{em.ERROR} Failed: **{failed}**",
        )

    # ── Stats ──

    @app.on_callback_query(filters.regex("^stats$"))
    @verified
    async def cb_stats(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)

        s = await db.get_stats()
        ps = await db.get_payment_stats()
        ext = await db.get_extended_stats()
        rev = await db.get_revenue_stats()
        wstats = await db.get_withdrawal_stats()
        cdist = await db.get_credit_distribution_stats()
        active = len(clients.active_clients)
        assigned = len(clients.active_requests)
        top_buyer = await db.top_buyer_24h()
        top_ref = await db.top_referrer_24h()

        def d(metric):
            # "24h | 7d | 30d | all" formatter for a windowed metric dict
            return f"{metric['24h']} | {metric['7d']} | {metric['30d']} | {metric['all']}"

        pay_lines = ""
        for method, info in ps.get("by_method", {}).items():
            total = info["total"]
            if method == "crypto_usdt":
                total_inr = total * USDT_TO_INR
                pay_lines += f"\n  {method}: {info['count']} payments, ₹{total_inr:.2f} ({total:.2f} USDT)"
            elif method == "telegram_stars":
                pay_lines += f"\n  {method}: {info['count']} payments, ⭐{int(total)} Stars"
            else:
                pay_lines += f"\n  {method}: {info['count']} payments, ₹{total:.2f}"

        # Inventory breakdown by status
        inv = ext["inventory"]
        inv_line = " | ".join(f"{k}: {v}" for k, v in sorted(inv.items())) or "—"

        overview_block = (
            f"<blockquote>"
            f"{em.USERS} Users: **{s['users']}**\n"
            f"{em.PHONE} Numbers (active): **{s['sessions']}**\n"
            f"{em.ONLINE} Connected: **{active}**\n"
            f"{em.LINK} Assigned now: **{assigned}**\n"
            f"{em.MAIL} OTPs forwarded: **{s['otps']}**\n"
            f"{em.WALLET} Outstanding credits: **{ext['outstanding_credits']}**\n"
            f"{em.PHONE} Inventory: {inv_line}"
            f"</blockquote>"
        )

        activity_block = (
            f"<blockquote>"
            f"{em.CALENDAR} **Activity — 24h | 7d | 30d | all:**\n"
            f"  {em.ADD} Numbers added: {d(ext['added'])}\n"
            f"  {em.MONEY} Numbers sold: {d(ext['sold'])}\n"
            f"  {em.DELETE} Numbers removed: {d(ext['removed'])}\n"
            f"  {em.CREDIT} Transactions: {d(ext['transactions'])}\n"
            f"  {em.NEW_USER} New users: {d(ext['new_users'])}\n"
            f"  {em.MAIL} OTPs forwarded: {d(ext['otps'])}\n"
            f"  {em.WARNING} Auth failures: {d(ext['auth_failures'])}"
            f"</blockquote>"
        )

        st = ext["sell_through"]
        tts = ext["avg_time_to_sell"]
        tts_str = f"{tts:.1f}h" if tts is not None else "—"
        fn = ext["funnel"]
        v_pct = (fn["verified"] / fn["users"] * 100) if fn["users"] else 0
        b_pct = (fn["buyers"] / fn["users"] * 100) if fn["users"] else 0

        performance_block = (
            f"<blockquote>"
            f"{em.TRENDING_UP} **Performance:**\n"
            f"  Sell-through (24h/7d/30d/all): "
            f"{st['24h']:.0f}% | {st['7d']:.0f}% | {st['30d']:.0f}% | {st['all']:.0f}%\n"
            f"  Avg time-to-sell: {tts_str}\n\n"
            f"{em.USERS} **Funnel (all-time):**\n"
            f"  Users: {fn['users']} → Verified: {fn['verified']} ({v_pct:.0f}%) "
            f"→ Buyers: {fn['buyers']} ({b_pct:.0f}%)"
            f"</blockquote>"
        )

        revenue_block = (
            f"<blockquote>"
            f"{em.BANK} **Revenue (INR-equiv):**\n"
            f"  Last 24h: ₹{rev['24h']['inr']:.2f} ({rev['24h']['count']} txns)\n"
            f"  This Month: ₹{rev['monthly']['inr']:.2f} ({rev['monthly']['count']} txns)\n"
            f"  All-time: ₹{rev['all']['inr']:.2f} ({rev['all']['count']} txns)\n\n"
            f"{em.DOLLAR} **Withdrawals:**\n"
            f"  This Month: **{wstats['monthly']['amount']}** credits ({wstats['monthly']['count']} reqs)\n"
            f"  Total: **{wstats['all']['amount']}** credits ({wstats['all']['count']} reqs)\n\n"
            f"{em.CREDIT} **Payments by method ({ps['total_payments']}):**{pay_lines}"
            f"</blockquote>"
        )

        hholder = cdist.get("highest_holder")
        if hholder:
            highest_holder_line = f"\n  {em.OWNER} Highest holder: @{hholder['name']} (`{hholder['user_id']}`) — **{hholder['total']}** credits ({hholder['credits']} cr, {hholder['balance']} withdrawable)"
        else:
            highest_holder_line = f"\n  {em.OWNER} Highest holder: —"

        credits_dist_block = (
            f"<blockquote>"
            f"{em.CREDIT} **Credits Distributed:**\n"
            f"  {em.MONEY} Purchased: **{cdist['purchased']}** credits\n"
            f"  {em.GIFT} Discount & Bonus: **{cdist['discount']}** credits\n"
            f"  {em.DOLLAR} Withdrawable Balance: **{cdist['withdrawable']}** credits\n"
            f"  {em.WALLET} Total Distributed: **{cdist['total_distributed']}** credits"
            f"{highest_holder_line}"
            f"</blockquote>"
        )

        top_buyer_str = f"@{top_buyer['name']} ({top_buyer['total']:.2f})" if top_buyer else "—"
        top_ref_str = f"@{top_ref['name']} ({top_ref['count']} refs)" if top_ref else "—"

        leaderboard_block = (
            f"<blockquote>"
            f"{em.FIRE} **Leaderboard (24h):**\n"
            f"  {em.MONEY} Top buyer: {top_buyer_str}\n"
            f"  {em.USERS} Top referrer: {top_ref_str}"
            f"</blockquote>"
        )

        await safe_edit(cq.message,
            f"{em.STATS} **Statistics Overview**\n\n"
            f"{overview_block}\n\n"
            f"{activity_block}\n\n"
            f"{performance_block}\n\n"
            f"{revenue_block}\n\n"
            f"{credits_dist_block}\n\n"
            f"{leaderboard_block}",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Get Number (User) — Country-based ──

    @app.on_callback_query(filters.regex(r"^get_number$|^pg_gn:\d+$"))
    @verified
    async def cb_get_number(_, cq: CallbackQuery):
        await _answer_cq(cq)
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_gn:") else 0

        offer = await db.get_active_offer(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)

        sessions = await db.get_active_sessions()
        # ponytail: batch pricing — one query for all sessions, not one each.
        prices = await db.get_session_prices(sessions)
        by_country = {}
        for s in sessions:
            p = prices.get(s["phone_number"])
            if p is None:
                continue
            eff = apply_discount(p, offer)
            # A zero-balance user can never get a number free — floor to 1.
            if eff == 0 and credits <= 0:
                eff = 1
            cc = s.get("country_code", "XX")
            by_country.setdefault(cc, []).append((s, eff))

        if not by_country:
            support = " | ".join(SUPPORT_HANDLES)
            await safe_edit(cq.message,
                f"{em.PHONE} **No numbers available right now.**\n\n"
                f"Check back later or contact support:\n{support}",
                reply_markup=back_kb("main_menu"),
            )
            return

        country_min = {}
        for cc, items in by_country.items():
            country_min[cc] = min(p for _, p in items)

        all_buttons = []
        all_lines = []
        for cc in sorted(by_country.keys(), key=lambda c: (country_min[c], c)):
            flag = get_country_flag(cc)
            name = get_country_name(cc)
            items = by_country[cc]

            session_prices = [p for _, p in items]
            min_p = min(session_prices) if session_prices else 1
            max_p = max(session_prices) if session_prices else 1
            range_str = f"({min_p}-{max_p})" if min_p != max_p else f"{min_p}"

            available = sum(1 for s, _ in items if not clients.get_request_user(s["phone_number"]))
            all_lines.append(f"{flag} {name} — **{range_str}** cr — {available} available")
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {name} — {range_str} cr ({available})",
                callback_data=f"country:{cc}", style=S.PRIMARY,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_gn", "main_menu")
        start = page * PAGE_SIZE
        page_lines = all_lines[start:start + PAGE_SIZE]
        banner = offer_banner(offer)
        header = f"{em.GLOBE} **Select a Country**\n"
        if banner:
            header += f"{banner} — prices shown already discounted\n"
        await safe_edit(cq.message,
            header + "\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^country:[A-Z]+$|^pg_cn:[A-Z]+:\d+$"))
    @verified
    async def cb_country(_, cq: CallbackQuery):
        await _answer_cq(cq)
        if cq.data.startswith("pg_cn:"):
            parts = cq.data.split(":")
            cc, page = parts[1], int(parts[2])
        else:
            cc = cq.data.split(":", 1)[1]
            page = 0

        offer = await db.get_active_offer(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)

        sessions = await db.get_active_sessions_by_country(cc)
        # ponytail: batch pricing — one query for all sessions, not one each.
        prices = await db.get_session_prices(sessions)
        valid_sessions = []
        session_prices = []   # effective (discounted) prices
        for s in sessions:
            p = prices.get(s["phone_number"])
            if p is not None:
                eff = apply_discount(p, offer)
                # A zero-balance user can never get a number free — floor to 1.
                if eff == 0 and credits <= 0:
                    eff = 1
                valid_sessions.append(s)
                session_prices.append(eff)

        if not valid_sessions:
            await cq.answer("No numbers available for this country.", show_alert=True)
            return

        flag = get_country_flag(cc)
        name = get_country_name(cc)

        min_p = min(session_prices) if session_prices else 1
        max_p = max(session_prices) if session_prices else 1
        range_str = f"({min_p}-{max_p})" if min_p != max_p else f"{min_p}"

        all_buttons = []
        for i, s in enumerate(valid_sessions):
            phone = s["phone_number"]
            masked = mask_phone(phone)
            year = s.get("account_year")
            month = s.get("account_month")
            year_str = f" ({format_account_year(year, month)})" if year else ""
            email_icon = f" {em.MAIL}" if s.get("email_added") else ""
            p = session_prices[i]
            price_tag = "FREE" if p == 0 else f"{p} cr"
            assigned = clients.get_request_user(phone)
            if assigned:
                all_buttons.append([
                    InlineKeyboardButton(f"{em.OFFLINE} {masked}{year_str}{email_icon} — {price_tag} (in use)", callback_data="noop", style=S.DEFAULT)
                ])
            else:
                all_buttons.append([
                    InlineKeyboardButton(
                        f"{em.ONLINE} {masked}{year_str}{email_icon} — {price_tag}", callback_data=f"sel:{phone}", style=S.SUCCESS
                    )
                ])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, f"pg_cn:{cc}", "get_number")
        banner = offer_banner(offer)
        offer_note = f"{banner} — prices below already discounted\n\n" if banner else ""
        await safe_edit(cq.message,
            f"{flag} **{name}** — **{range_str}** credits per account\n\n"
            f"{offer_note}"
            f"Select an account to buy:\n"
            f"{em.TIMER} Login window: {OTP_TIMEOUT // 60} minutes.{page_label}\n\n"
            f"{em.INFO} **Note:** Your credits are deducted when you pick an account\n"
            f"and refunded after 1 hour if you release it manually.",
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex("^noop$"))
    @verified
    async def cb_noop(_, cq: CallbackQuery):
        await cq.answer("This number is currently in use.", show_alert=True)

    # ── WhatsApp Portal (manual fulfilment) ──

    @app.on_callback_query(filters.regex(r"^wa_portal$|^pg_wa:\d+$"))
    @verified
    async def cb_wa_portal(_, cq: CallbackQuery):
        await _answer_cq(cq)
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_wa:") else 0
        user_id = cq.from_user.id

        # One live order at a time — show it instead of the catalogue, otherwise a
        # buyer can stack orders the admin has to reconcile by hand.
        live = await db.get_user_wa_order(user_id)
        if live:
            await safe_edit(cq.message, _wa_live_text(live), reply_markup=_wa_live_kb(live))
            return

        numbers = await db.get_wa_numbers("available")
        if not numbers:
            support = " | ".join(SUPPORT_HANDLES)
            await safe_edit(cq.message,
                f"{em.SMS} **No WhatsApp numbers available right now.**\n\n"
                f"Check back later or contact support:\n{support}",
                reply_markup=back_kb("main_menu"),
            )
            return

        credits, balance, total_funds = await db.get_total_funds(user_id)
        all_buttons = [
            [InlineKeyboardButton(
                f"{em.ONLINE} {get_country_flag(n['country_code'])} {mask_phone(n['phone_number'])} — {n['price']} cr",
                callback_data=f"wa_sel:{n['phone_number']}", style=S.SUCCESS,
            )]
            for n in numbers
        ]
        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_wa", "main_menu")
        await safe_edit(cq.message,
            f"{em.SMS} **WhatsApp Numbers**\n\n"
            f"{em.MONEY} Your funds: **{total_funds}** ({credits} credits, {balance} withdrawable)\n\n"
            f"These are fulfilled **manually by an admin**. Pick a number and an "
            f"admin will connect to the device to read your OTP.\n\n"
            f"{em.INFO} Credits are deducted when you pick a number and refunded "
            f"in full if the admin can't fulfil it.{page_label}",
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^wa_sel:"))
    @verified
    async def cb_wa_select(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]

        if await db.get_user_wa_order(cq.from_user.id):
            await cq.answer("You already have a live WhatsApp order.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        num = await db.get_wa_number(phone)
        if not num or num.get("status") != "available":
            await cq.answer(f"{em.ERROR} Number no longer available.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        price = num["price"]
        cc = num.get("country_code", "XX")
        credits, balance, total_funds = await db.get_total_funds(cq.from_user.id)

        await safe_edit(cq.message,
            f"{em.SMS} **Confirm WhatsApp Order**\n\n"
            f"{get_country_flag(cc)} {get_country_name(cc)}\n"
            f"{em.PHONE} `{mask_phone(phone)}`\n"
            f"{em.MONEY} Price: **{price}** credits\n"
            f"{em.MONEY} Your funds: **{total_funds}**\n\n"
            f"{em.WARNING} This is a **manual** order. After you confirm:\n"
            f"1. Your credits are deducted and an admin is notified.\n"
            f"2. You wait until the admin connects to the device.\n"
            f"3. Once confirmed you get the full number and request your OTP.\n"
            f"4. The admin reads the OTP off the device and sends it to you.\n\n"
            f"If the admin can't fulfil it, you're refunded in full immediately.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.SUCCESS} Confirm — {price} cr", callback_data=f"wa_buy:{phone}", style=S.SUCCESS)],
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="wa_portal", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^wa_buy:"))
    @verified
    async def cb_wa_buy(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        user_id = cq.from_user.id

        user = await db.get_user(user_id)
        if not user:
            await cq.answer("Please /start the bot first.", show_alert=True)
            return

        if await db.get_user_wa_order(user_id):
            await cq.answer("You already have a live WhatsApp order.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        num = await db.get_wa_number(phone)
        if not num or num.get("status") != "available":
            await cq.answer(f"{em.ERROR} Number no longer available.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        # Price is re-read from the doc here — never taken from callback data.
        price = num["price"]
        credits, balance, total_funds = await db.get_total_funds(user_id)
        if total_funds < price:
            await safe_edit(cq.message,
                f"{em.ERROR} **Not enough credits**\n\n"
                f"{em.PHONE} `{mask_phone(phone)}`\n"
                f"{em.MONEY} Price: **{price}** credits\n"
                f"{em.MONEY} Your funds: **{total_funds}**\n"
                f"{em.WARNING} Shortfall: **{price - total_funds}**",
                reply_markup=back_kb("buy_credits"),
            )
            return

        ok, credits_deducted, balance_deducted = await db.deduct_funds_for_purchase(user_id, price)
        if not ok:
            await safe_edit(cq.message,
                f"{em.ERROR} Could not deduct funds. Please try again or contact support.",
                reply_markup=back_kb("main_menu"))
            return

        order_id = await db.claim_wa_number(phone, user_id, price, credits_deducted, balance_deducted)
        if not order_id:
            # Lost the race — hand the money straight back.
            await db.restore_purchase_funds(user_id, credits_deducted, balance_deducted)
            await cq.answer(f"{em.OFFLINE} Just taken by someone else — you were not charged.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        log.info("WA order %s: user %d claimed %s for %d credits", order_id, user_id, phone, price)
        order = await db.get_wa_order(order_id)
        await safe_edit(cq.message, _wa_live_text(order), reply_markup=_wa_live_kb(order))

        uname = user.get("username") or user.get("first_name") or str(user_id)
        cc = num.get("country_code", "XX")
        await _wa_notify(app,
            f"{em.SMS} **New WhatsApp Order — Action Needed**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.USER} User: `{user_id}` (@{uname})\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{get_country_flag(cc)} Country: {get_country_name(cc)}\n"
            f"{em.MONEY} Price: **{price}** credits (deducted)\n\n"
            f"{em.WARNING} The buyer is waiting on this screen — please act soon.\n\n"
            f"{em.SUCCESS} **Confirm** — you're at the device and ready. The buyer "
            f"is told to request the OTP, then you relay it with "
            f"`/wotp {order_id} 123456`.\n"
            f"{em.ERROR} **Not Available** — cancels the order, refunds **{price}** "
            f"credits in full and puts the number back on sale.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{em.SUCCESS} Confirm", callback_data=f"wa_ok:{order_id}", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.ERROR} Not Available", callback_data=f"wa_no:{order_id}", style=S.DANGER),
            ]]),
        )

    @app.on_callback_query(filters.regex(r"^wa_mine$"))
    @verified
    async def cb_wa_mine(_, cq: CallbackQuery):
        await _answer_cq(cq)
        order = await db.get_user_wa_order(cq.from_user.id)
        if not order:
            await cq.answer("No live WhatsApp order.", show_alert=True)
            await cb_wa_portal(app, cq)
            return
        await safe_edit(cq.message, _wa_live_text(order), reply_markup=_wa_live_kb(order))

    @app.on_callback_query(filters.regex(r"^wa_drop:"))
    @verified
    async def cb_wa_drop(_, cq: CallbackQuery):
        """Buyer abandons their own pending order and takes the refund."""
        await _answer_cq(cq)
        order_id = cq.data.split(":", 1)[1]

        order = await db.get_wa_order(order_id)
        if not order or order.get("buyer_id") != cq.from_user.id:
            await cq.answer("Not your order.", show_alert=True)
            return
        # Only while still pending: once an admin has confirmed, they are holding
        # the device for this buyer and cancelling is a support matter.
        if order.get("status") != "pending":
            await cq.answer("Already confirmed by an admin — contact support to cancel.", show_alert=True)
            await cb_wa_mine(app, cq)
            return

        released = await db.cancel_wa_order(order_id, reason="cancelled_by_buyer")
        if not released:
            await cq.answer("Order already handled.", show_alert=True)
            await cb_wa_portal(app, cq)
            return

        refund = await _wa_refund(released)
        await safe_edit(cq.message,
            f"{em.CANCELLED} **Order cancelled**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.MONEY} **{refund}** credits refunded.",
            reply_markup=back_kb("wa_portal"),
        )
        await _wa_notify(app,
            f"{em.CANCELLED} **WhatsApp Order Cancelled by Buyer**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.PHONE} Number: `{released['phone_number']}`\n"
            f"{em.USER} User: `{cq.from_user.id}`\n"
            f"{em.REFUND} Refunded: **{refund}** credits",
        )

    @app.on_callback_query(filters.regex(r"^wa_ok:"))
    @verified
    async def cb_wa_confirm(_, cq: CallbackQuery):
        if not await _is_wa_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        order_id = cq.data.split(":", 1)[1]

        order = await db.confirm_wa_order(order_id)
        if not order:
            await cq.answer("Order is no longer pending.", show_alert=True)
            return

        phone = order["phone_number"]
        buyer_id = order["buyer_id"]
        log.info("WA order %s confirmed by admin %d", order_id, cq.from_user.id)

        support = " | ".join(SUPPORT_HANDLES)
        try:
            await app.send_message(
                buyer_id,
                f"{em.SUCCESS} **WhatsApp Order Confirmed!**\n\n"
                f"{em.RECEIPT} Order ID: `{order_id}`\n"
                f"{em.PHONE} Number: `{phone}`\n\n"
                f"An admin is connected to this device now.\n\n"
                f"{em.OTP} **Send the OTP to this number**, then wait here — "
                f"the admin will read the code off the device and forward it to you.\n\n"
                f"{em.WARNING} Issues? Contact support:\n{support}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.SMS} My Order", callback_data="wa_mine", style=S.PRIMARY)],
                ]),
            )
        except Exception as e:
            log.error("Failed to notify buyer %d of WA confirm: %s", buyer_id, e)

        await safe_edit(cq.message,
            f"{em.SUCCESS} **WhatsApp Order Confirmed**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{em.USER} User: `{buyer_id}`\n"
            f"{em.MONEY} Price: **{order.get('price', 0)}** credits\n\n"
            f"Buyer has been told to send the OTP to this number.\n"
            f"When the code lands on the device, send:\n\n"
            f"`/wotp {order_id} 123456`",
        )

    @app.on_callback_query(filters.regex(r"^wa_no:"))
    @verified
    async def cb_wa_reject(_, cq: CallbackQuery):
        if not await _is_wa_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        order_id = cq.data.split(":", 1)[1]

        released = await db.cancel_wa_order(order_id, reason="admin_not_available")
        if not released:
            await cq.answer("Order already handled.", show_alert=True)
            return

        buyer_id = released["buyer_id"]
        phone = released["phone_number"]
        refund = await _wa_refund(released)
        log.info("WA order %s rejected by admin %d, refunded %d", order_id, cq.from_user.id, refund)

        try:
            await app.send_message(
                buyer_id,
                f"{em.CANCELLED} **WhatsApp Order Cancelled**\n\n"
                f"{em.RECEIPT} Order ID: `{order_id}`\n"
                f"{em.PHONE} Number: `{mask_phone(phone)}`\n\n"
                f"This number isn't available right now.\n"
                f"{em.REFUND} **{refund}** credits have been refunded in full.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.SMS} Pick Another", callback_data="wa_portal", style=S.PRIMARY)],
                ]),
            )
        except Exception as e:
            log.error("Failed to notify buyer %d of WA reject: %s", buyer_id, e)

        await safe_edit(cq.message,
            f"{em.CANCELLED} **WhatsApp Order Cancelled — Not Available**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{em.USER} User: `{buyer_id}`\n"
            f"{em.REFUND} Refunded: **{refund}** credits\n\n"
            f"The number is back on sale.",
        )

    @app.on_message(filters.command("wotp") & filters.private)
    @verified
    async def cmd_wotp(_, message: Message):
        """Admin relays the OTP read off the device: /wotp ORD-XXXXXXXX 123456"""
        if not await _is_wa_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                f"{em.INFO} **Usage:** `/wotp <order_id> <code>`\n\n"
                f"Example: `/wotp ORD-1A2B3C4D 123456`"
            )
            return

        order_id, code = parts[1].strip().upper(), parts[2].strip()
        order = await db.get_wa_order(order_id)
        if not order:
            await message.reply(f"{em.ERROR} Order `{order_id}` not found.")
            return
        if order.get("status") != "confirmed":
            await message.reply(
                f"{em.ERROR} Order `{order_id}` is **{order.get('status')}**, not confirmed.\n"
                f"Only a confirmed order can be completed with an OTP."
            )
            return

        sold = await db.mark_wa_sold(order_id, code)
        if not sold:
            await message.reply(f"{em.ERROR} Order `{order_id}` was just completed by someone else.")
            return

        buyer_id = sold["buyer_id"]
        phone = sold["phone_number"]
        log.info("WA order %s completed by admin %d", order_id, message.from_user.id)

        support = " | ".join(SUPPORT_HANDLES)
        credits_left = await db.get_credits(buyer_id)
        try:
            await app.send_message(
                buyer_id,
                f"{em.OTP} **OTP Received!**\n\n"
                f"{em.RECEIPT} Order ID: `{order_id}`\n"
                f"{em.PHONE} Number: `{phone}`\n"
                f"{em.CODE} Code: `{code}`\n"
                f"{em.MONEY} Credits left: {credits_left}\n\n"
                f"{em.WARNING} Issues logging in? Contact support:\n{support}",
            )
            await app.send_message(
                buyer_id,
                "⭐ **Rate Your Experience**\n\n"
                "How would you rate your experience with our bot? Please tap a rating (1-5) below:",
                reply_markup=_feedback_kb(),
            )
        except Exception as e:
            log.error("Failed to deliver WA OTP to buyer %d: %s", buyer_id, e)
            await message.reply(f"{em.WARNING} Order completed but the buyer could not be messaged: `{e}`")
            return

        await message.reply(
            f"{em.SUCCESS} **OTP delivered — order complete.**\n\n"
            f"{em.RECEIPT} Order ID: `{order_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{em.USER} User: `{buyer_id}`\n"
            f"{em.CODE} Code: `{code}`\n"
            f"{em.MONEY} Price: **{sold.get('price', 0)}** credits (kept)"
        )

    # ── WhatsApp Admin Management ──

    _WA_STATUS_ICON = {"available": em.ONLINE, "pending": em.LOADING,
                       "confirmed": em.SMS, "sold": em.OFFLINE}

    @app.on_callback_query(filters.regex(r"^wa_admin$|^pg_waa:\d+$"))
    @verified
    async def cb_wa_admin(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_waa:") else 0

        numbers = await db.get_wa_numbers()
        counts = {}
        for n in numbers:
            counts[n["status"]] = counts.get(n["status"], 0) + 1

        all_buttons = [
            [InlineKeyboardButton(
                f"{_WA_STATUS_ICON.get(n['status'], em.PHONE)} {n['phone_number']} — {n['price']} cr",
                callback_data=f"wa_num:{n['phone_number']}", style=S.DEFAULT,
            )]
            for n in numbers
        ]
        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_waa", "admin_panel")
        add_row = [InlineKeyboardButton(f"{em.ADD} Add WhatsApp Number", callback_data="wa_add", style=S.SUCCESS)]

        summary = " | ".join(f"{k}: **{v}**" for k, v in sorted(counts.items())) or "none yet"
        await safe_edit(cq.message,
            f"{em.SMS} **WhatsApp Numbers ({len(numbers)})**\n\n"
            f"{summary}\n\n"
            f"{em.INFO} These are sold manually — you confirm each order and relay "
            f"the OTP with `/wotp <order_id> <code>`.{page_label}",
            reply_markup=InlineKeyboardMarkup([add_row] + page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^wa_num:"))
    @verified
    async def cb_wa_num(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]

        num = await db.get_wa_number(phone)
        if not num:
            await cq.answer("Number not found.", show_alert=True)
            await cb_wa_admin(app, cq)
            return

        status = num["status"]
        cc = num.get("country_code", "XX")
        order_line = ""
        rows = [[InlineKeyboardButton(f"{em.MONEY} Set Price", callback_data=f"wa_price:{phone}", style=S.PRIMARY)]]

        if status in ("pending", "confirmed"):
            order_line = (
                f"{em.RECEIPT} Order ID: `{num['order_id']}`\n"
                f"{em.USER} Buyer: `{num['buyer_id']}`\n"
            )
            if status == "pending":
                rows.append([
                    InlineKeyboardButton(f"{em.SUCCESS} Confirm", callback_data=f"wa_ok:{num['order_id']}", style=S.SUCCESS),
                    InlineKeyboardButton(f"{em.ERROR} Not Available", callback_data=f"wa_no:{num['order_id']}", style=S.DANGER),
                ])
            else:
                order_line += f"\nRelay the code with:\n`/wotp {num['order_id']} 123456`\n"
                rows.append([InlineKeyboardButton(f"{em.CANCELLED} Cancel & Refund", callback_data=f"wa_no:{num['order_id']}", style=S.DANGER)])
        elif status == "sold":
            order_line = (
                f"{em.RECEIPT} Order ID: `{num.get('order_id', 'N/A')}`\n"
                f"{em.USER} Buyer: `{num.get('buyer_id', 'N/A')}`\n"
                f"{em.CODE} OTP sent: `{num.get('otp_code', 'N/A')}`\n"
            )
            rows.append([InlineKeyboardButton(f"{em.RESTART} Relist for Sale", callback_data=f"wa_relist:{phone}", style=S.SUCCESS)])

        if status in ("available", "sold"):
            rows.append([InlineKeyboardButton(f"{em.REMOVE} Delete Number", callback_data=f"wa_del:{phone}", style=S.DANGER)])
        rows.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="wa_admin", style=S.DEFAULT)])

        await safe_edit(cq.message,
            f"{em.SMS} **WhatsApp Number**\n\n"
            f"{em.PHONE} `{phone}`\n"
            f"{get_country_flag(cc)} {get_country_name(cc)}\n"
            f"{em.MONEY} Price: **{num['price']}** credits\n"
            f"Status: **{status}**\n"
            f"{order_line}",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    @app.on_callback_query(filters.regex(r"^wa_add$"))
    @verified
    async def cb_wa_add(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        auth_states[cq.from_user.id] = {"step": "wa_add_phone"}
        await safe_edit(cq.message,
            f"{em.SMS} **Add WhatsApp Number**\n\n"
            "Send the phone number in international format:\n"
            "Example: `+1234567890`\n\n"
            "Country is detected automatically.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="wa_admin", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^wa_price:"))
    @verified
    async def cb_wa_price(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        auth_states[cq.from_user.id] = {"step": "wa_set_price", "phone": phone}
        await safe_edit(cq.message,
            f"{em.MONEY} **Set Price for** `{phone}`\n\n"
            "Send the new price in credits (e.g. `25`).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data=f"wa_num:{phone}", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^wa_del:"))
    @verified
    async def cb_wa_del(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        # remove_wa_number refuses while an order is live, so a buyer who is
        # mid-order can never have the number deleted out from under them.
        if await db.remove_wa_number(phone):
            await cq.answer("Number deleted.", show_alert=True)
        else:
            await cq.answer("Can't delete — an order is live on this number.", show_alert=True)
        await cb_wa_admin(app, cq)

    @app.on_callback_query(filters.regex(r"^wa_relist:"))
    @verified
    async def cb_wa_relist(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        if await db.relist_wa_number(phone):
            await cq.answer("Back on sale.", show_alert=True)
        else:
            await cq.answer("Only a sold number can be relisted.", show_alert=True)
        await cb_wa_num(app, cq)

    @app.on_callback_query(filters.regex(r"^sel:"))
    @verified
    async def cb_select_number(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]

        user = await db.get_user(cq.from_user.id)
        if not user:
            await cq.answer("Please /start the bot first.", show_alert=True)
            return

        session = await db.get_session(phone)
        if not session or session.get("status") != "active":
            await cq.answer(f"{em.ERROR} Number not available.", show_alert=True)
            return

        existing = clients.get_request_user(phone)
        if existing and existing != cq.from_user.id:
            await cq.answer(f"{em.OFFLINE} Already assigned to someone else.", show_alert=True)
            return

        cc = session.get("country_code", "XX")
        base_price = await db.get_session_price(session)
        if base_price is None:
            await cq.answer(f"{em.ERROR} This number is not configured for sale.", show_alert=True)
            return

        # Apply any active discount offer server-side (never trust the client).
        offer = await db.get_active_offer(cq.from_user.id)
        price = apply_discount(base_price, offer)

        credits, balance, total_funds = await db.get_total_funds(cq.from_user.id)
        # A fully-covered number is free only for users who hold real funds;
        # a user with zero funds pays a minimum of 1 credit.
        if price == 0 and total_funds <= 0:
            price = 1
        saved = base_price - price

        if total_funds < price:
            await _start_shortfall_topup(cq, phone, cc, base_price, price, total_funds, saved)
            return

        await _finalize_purchase(cq.from_user.id, phone, edit_msg=cq.message)

    @app.on_callback_query(filters.regex(r"^confirm_frozen:"))
    @verified
    async def cb_confirm_frozen(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        await _finalize_purchase(cq.from_user.id, phone, edit_msg=cq.message, confirmed_frozen=True)

    @app.on_callback_query(filters.regex(r"^cancel_frozen:"))
    @verified
    async def cb_cancel_frozen(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        await clients.stop_session(phone)
        await db.unreserve_session(phone)
        await safe_edit(cq.message,
            f"{em.CANCELLED} Purchase cancelled.",
            reply_markup=back_kb("get_number")
        )

    async def _start_shortfall_topup(cq, phone, cc, base_price, price, total_funds, saved):
        """Generate a Razorpay QR for (effective price − total_funds) and, once paid,
        assign the selected number automatically."""
        shortfall = price - total_funds

        if shortfall < 10:
            await cq.answer(
                f"{em.ERROR} You need {shortfall} more credit(s) for this account "
                f"({price} needed, you have {total_funds} total funds).\n\n"
                f"Top-ups start at 10 credits — tap Buy Credits.",
                show_alert=True,
            )
            offer_line = f"\n{em.GIFT} Offer applied: **{saved} credits off** (was {base_price})" if saved > 0 else ""
            await safe_edit(cq.message,
                f"{em.MONEY} **Not enough credits**\n\n"
                f"{em.PHONE} `{mask_phone(phone)}`\n"
                f"{em.CREDIT} Price: **{price}** credits{offer_line}\n"
                f"{em.MONEY} Available funds: **{total_funds}** credits\n"
                f"{em.WARNING} Shortfall: **{shortfall}** credit(s)\n\n"
                "Auto top-up needs at least 10 credits. Tap below to buy credits.",
                reply_markup=back_kb("buy_credits"),
            )
            return

        plan_key = f"custom_{shortfall}"
        plan = get_credit_plan(plan_key)

        await safe_edit(cq.message, f"{em.LOADING} Generating payment QR...")
        qr = await asyncio.to_thread(
            payments.create_razorpay_qr, plan["label"], plan["amount_inr"], cq.from_user.id,
        )
        if not qr:
            await safe_edit(cq.message,
                f"{em.ERROR} Payment gateway error. Try later.",
                reply_markup=back_kb("buy_credits"),
            )
            return

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.SUCCESS} I've Paid", callback_data=f"rz_check:{qr['id']}:{plan_key}", style=S.SUCCESS)],
            [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="get_number", style=S.DANGER)],
        ])

        try:
            await cq.message.delete()
        except Exception:
            pass

        flag = get_country_flag(cc)
        offer_line = f"{em.GIFT} Offer: **{saved} credits off** (was {base_price})\n" if saved > 0 else ""
        qr_msg = await safe_send_photo(
            cq.from_user.id,
            photo_url=qr["image_url"],
            caption=(
                f"{em.MONEY} **Top up to grab this account**\n\n"
                f"{flag} `{mask_phone(phone)}` — **{price}** credits\n"
                f"{offer_line}"
                f"{em.CREDIT} Your balance: **{total_funds}** — short by **{shortfall}**\n\n"
                f"{em.PHONE} **Scan to pay ₹{plan['amount_inr'] // 100}** ({shortfall} credits)\n"
                f"{em.SUCCESS} Once paid, `{mask_phone(phone)}` is assigned to you automatically.\n\n"
                f"{em.TIMER} Valid for 15 minutes."
            ),
            reply_markup=buttons,
        )

        await db.save_pending_payment(
            cq.from_user.id, qr["id"], plan_key, plan["amount_inr"],
            qr_msg.chat.id, qr_msg.id, assign_phone=phone,
        )

        asyncio.create_task(_razorpay_poller(
            cq.from_user.id, qr["id"], plan_key, qr_msg, assign_phone=phone,
        ))

    @app.on_callback_query(filters.regex(r"^release:"))
    @verified
    async def cb_release(_, cq: CallbackQuery):
        phone = cq.data.split(":", 1)[1]
        req = clients.active_requests.get(phone)
        if not req:
            await cq.answer("No active assignment.", show_alert=True)
            return
        if req["user_id"] != cq.from_user.id and not await db.is_admin(cq.from_user.id):
            await cq.answer("Not your assignment.", show_alert=True)
            return
        await _answer_cq(cq)

        otp_received = req.get("otp_received", False)
        price = req.get("price", 0)
        user_id = req["user_id"]
        no_sale = req.get("no_sale", False)
        live_order_id = req.get("order_id")

        # Seller self-login into their own listing: never a sale, never a refund.
        if no_sale:
            clients.release_number(phone)
            await clients.stop_session(phone)
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` logged out.\n\n"
                f"Your listing stays active and available for buyers.",
                reply_markup=back_kb("my_accounts"),
            )
            return

        if otp_received and not await db.is_admin(cq.from_user.id):
            await safe_edit(cq.message,
                f"{em.ERROR} Cannot release `{mask_phone(phone)}` — OTP was already forwarded.\n\n"
                "Number is marked as sold. No refund available.",
                reply_markup=back_kb("main_menu"),
            )
            return

        # release_number atomically pops the in-memory request. Only the caller
        # that actually wins the pop (non-None) may refund/mark-sold — this gates
        # against double-clicks and release-vs-timeout double refunds.
        released = clients.release_number(phone)
        await clients.stop_session(phone)
        if released is None:
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` was already released.",
                reply_markup=back_kb("main_menu"),
            )
            return

        # Re-read from the popped request — it's the source of truth for the winner.
        otp_received = released.get("otp_received", False)
        price = released.get("price", 0)
        user_id = released["user_id"]
        live_order_id = released.get("order_id")

        if otp_received:
            order_id = await db.mark_session_sold(phone, user_id, price, live_order_id)
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` released and marked as sold.\n\n"
                f"{em.RECEIPT} Order ID: `{order_id}`\n"
                f"{em.MONEY} **{price} credits** — no refund (OTP was received).",
                reply_markup=back_kb("main_menu"),
            )
        else:
            spent_offer_at = released.get("offer_granted_at")
            if price > 0:
                await db.save_pending_refund(user_id, phone, price, spent_offer_at)
            restored = await db.restore_offer(user_id, spent_offer_at, delay_hours=1 if price > 0 else 0)
            offer_line = f"\n{em.GIFT} **Discount offer restored!**" if restored else ""
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` released.\n\n"
                f"{em.MONEY} **{price} credits** will be refunded in **1 hour**.{offer_line}",
                reply_markup=back_kb("main_menu"),
            )

    # ── OTP History ──

    @app.on_callback_query(filters.regex(r"^my_history$|^pg_mh:\d+$"))
    @verified
    async def cb_history(_, cq: CallbackQuery):
        await _answer_cq(cq)
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_mh:") else 0

        recent_sold = await db.get_user_recent_sold_sessions(cq.from_user.id, hours=24)
        purchases = await db.get_user_sold_history(cq.from_user.id, limit=200)

        if not recent_sold and not purchases:
            await safe_edit(cq.message,
                f"{em.LOGS} **No purchase history yet.**\n\n"
                f"Your purchased numbers will appear here after you buy an account.",
                reply_markup=back_kb("main_menu"),
            )
            return

        all_buttons = []
        if recent_sold:
            for s in recent_sold:
                ph = s["phone_number"]
                all_buttons.append([
                    InlineKeyboardButton(
                        f"{em.PHONE} {ph} (Get OTP)",
                        callback_data=f"hnum:{ph}",
                        style=S.SUCCESS,
                    )
                ])

        all_lines = []
        for p in purchases:
            price = p.get("sold_price") or p.get("price") or 0
            ph = p.get("phone_number", "Unknown")
            service = p.get("app") or p.get("service") or "Telegram"
            sold_at = p.get("sold_at")
            ts = sold_at.strftime("%m/%d %H:%M") if sold_at else ""
            ts_str = f" — {ts}" if ts else ""
            all_lines.append(
                f"`{price} cr` — {ph} — {service}{ts_str}"
            )

        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_lines = all_lines[start:end]
        total_pages = (len(all_lines) + PAGE_SIZE - 1) // PAGE_SIZE if all_lines else 1

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(f"{em.BACK} Prev", callback_data=f"pg_mh:{page - 1}", style=S.DEFAULT))
        if end < len(all_lines):
            nav.append(InlineKeyboardButton(f"{em.NEXT} Next", callback_data=f"pg_mh:{page + 1}", style=S.PRIMARY))

        footer = []
        if nav:
            footer.append(nav)
        footer.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)])
        page_label = f"\n\n{em.LIST} Page {page + 1}/{total_pages}" if total_pages > 1 else ""

        body_parts = []
        if recent_sold:
            body_parts.append(f"{em.PHONE} **Purchased Numbers (< 24 Hours):**\nTap a number button below to start session & receive OTP.\n")
        if page_lines:
            body_parts.append(f"{em.LOGS} **Purchase History:**\n<blockquote expandable>\n" + "\n".join(page_lines) + "\n</blockquote>" + page_label)
        elif not recent_sold:
            body_parts.append(f"{em.LOGS} **No purchase history yet.**")

        await safe_edit(cq.message,
            "\n".join(body_parts),
            reply_markup=InlineKeyboardMarkup(all_buttons + footer),
        )

    @app.on_callback_query(filters.regex(r"^hnum:"))
    @verified
    async def cb_history_number(_, cq: CallbackQuery):
        await _answer_cq(cq)
        phone = cq.data.split(":", 1)[1]
        user_id = cq.from_user.id

        session = await db.get_session(phone)
        if not session:
            await cq.answer(f"{em.ERROR} Session not found.", show_alert=True)
            return

        if session.get("sold_to") != user_id or session.get("status") != "sold":
            await cq.answer(f"{em.ERROR} You do not own this number.", show_alert=True)
            return

        sold_at = session.get("sold_at")
        if sold_at:
            if sold_at.tzinfo is None:
                sold_at = sold_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sold_at > timedelta(hours=24):
                await cq.answer(f"{em.ERROR} 24-hour window to get OTP has expired for this number.", show_alert=True)
                return

        existing = clients.get_request_user(phone)
        if existing and existing != user_id:
            await cq.answer(f"{em.OFFLINE} Number is currently in an active session.", show_alert=True)
            return

        await safe_edit(cq.message, f"{em.LOADING} Connecting session for `{phone}`...")

        try:
            await clients.start_session(phone, session["session_string"])
        except Exception as e:
            log.error("Failed to start session %s from history: %s", phone, e)
            await safe_edit(cq.message,
                f"{em.ERROR} Failed to connect `{phone}`: `{str(e)[:200]}`",
                reply_markup=back_kb("my_history"),
            )
            return

        # Do not check 2FA password here as requested ("this will not check password").
        # Password will be included automatically when OTP arrives via clients._on_new_message.
        order_id = session.get("order_id") or db.new_order_id()
        clients.assign_number(
            phone,
            user_id,
            timeout=OTP_TIMEOUT,
            price=0,
            credits_deducted=0,
            balance_deducted=0,
            order_id=order_id,
        )

        pwd_hint = f"\n🔐 2FA Password will be shown after OTP comes." if session.get("password") else ""
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.CANCELLED} Release / Stop", callback_data=f"release:{phone}", style=S.DANGER)],
            [InlineKeyboardButton(f"{em.BACK} Back to History", callback_data="my_history", style=S.DEFAULT)],
        ])

        await safe_edit(cq.message,
            f"{em.SUCCESS} **Session Started!**\n\n"
            f"📱 Number: `{phone}`\n"
            f"⏳ Waiting for OTP... (Session active for {OTP_TIMEOUT // 60} min)\n"
            f"{pwd_hint}\n\n"
            f"Please request the OTP on Telegram now.",
            reply_markup=buttons,
        )

    # ── Buy Credits ──

    @app.on_callback_query(filters.regex("^buy_credits$"))
    @verified
    async def cb_buy_credits(_, cq: CallbackQuery):
        auth_states.pop(cq.from_user.id, None)
        credits, balance, total_funds = await db.get_total_funds(cq.from_user.id)

        payments = await db.get_user_payments(cq.from_user.id, limit=5)
        payments_block = ""
        if payments:
            pay_lines = []
            for p in payments:
                p_at = p.get("created_at")
                p_str = p_at.strftime("%m/%d %H:%M") if p_at else "—"
                amount = p.get("amount", 0)
                currency = p.get("currency", "")
                method = p.get("method", "—")
                p_credits = p.get("credits", 0)
                pay_lines.append(
                    f"• **{amount} {currency}** ({method}) → **{p_credits}** credits — {p_str}"
                )
            payments_block = (
                f"\n\n{em.RECEIPT} **Recent Deposits:**\n"
                f"<blockquote expandable>" + "\n".join(pay_lines) + "</blockquote>"
            )

        buttons = [
            [
                InlineKeyboardButton(f"{em.MONEY} Razorpay (UPI)", callback_data="rz_plans", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.COIN} Crypto (USDT)", callback_data="cr_plans", style=S.PRIMARY),
            ],
            [
                InlineKeyboardButton(f"{em.STAR} Telegram Stars", callback_data="stars_plans", style=S.PRIMARY),
            ],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)],
        ]
        await safe_edit(cq.message,
            f"{em.CREDIT} **Buy Credits**\n\n"
            f"{em.CREDIT} Credits: **{credits}** (purchase only)\n"
            f"{em.MONEY} Withdrawable Balance: **{balance}** credits (purchase & withdrawal)\n\n"
            f"Choose a payment method:{payments_block}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Razorpay Plans ──

    @app.on_callback_query(filters.regex("^rz_plans$"))
    @verified
    async def cb_rz_plans(_, cq: CallbackQuery):
        buttons = []
        for key, plan in CREDIT_PLANS.items():
            buttons.append([InlineKeyboardButton(
                plan["label"], callback_data=f"rz_pay:{key}", style=S.SUCCESS,
            )])
        buttons.append([InlineKeyboardButton(f"{em.EDIT} Custom Amount", callback_data="rz_custom", style=S.PRIMARY)])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="buy_credits", style=S.PRIMARY)])
        await safe_edit(cq.message,
            f"{em.MONEY} **Razorpay — Choose a plan:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^rz_pay:"))
    @verified
    async def cb_rz_pay(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        plan = get_credit_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)

        await safe_edit(cq.message, f"{em.LOADING} Generating QR code...")
        qr = await asyncio.to_thread(
            payments.create_razorpay_qr, plan["label"], plan["amount_inr"], cq.from_user.id,
        )
        if not qr:
            return await safe_edit(cq.message,
                f"{em.ERROR} Payment gateway error. Try later.",
                reply_markup=back_kb("buy_credits"),
            )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.SUCCESS} I've Paid", callback_data=f"rz_check:{qr['id']}:{plan_key}", style=S.SUCCESS)],
            [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="buy_credits", style=S.DANGER)],
        ])

        try:
            await cq.message.delete()
        except Exception:
            pass

        qr_msg = await safe_send_photo(
            cq.from_user.id,
            photo_url=qr["image_url"],
            caption=(
                f"{em.PHONE} **Scan to pay ₹{plan['amount_inr'] // 100}**\n"
                f"{em.GIFT} You'll receive **{plan['credits']} credits**\n\n"
                f"{em.TIMER} Valid for 15 minutes."
            ),
            reply_markup=buttons,
        )

        await db.save_pending_payment(
            cq.from_user.id, qr["id"], plan_key, plan["amount_inr"],
            qr_msg.chat.id, qr_msg.id,
        )

        asyncio.create_task(_razorpay_poller(
            cq.from_user.id, qr["id"], plan_key, qr_msg,
        ))

    @app.on_callback_query(filters.regex(r"^rz_check:"))
    @verified
    async def cb_rz_check(_, cq: CallbackQuery):
        parts = cq.data.split(":")
        qr_id, plan_key = parts[1], parts[2]
        plan = get_credit_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)

        status = await asyncio.to_thread(
            payments.check_razorpay_payment, qr_id, plan["amount_inr"],
        )
        if status == "paid":
            await cq.answer(f"{em.SUCCESS} Payment received!", show_alert=True)
            # Award immediately (idempotent) so credits/assignment don't wait for
            # the next poll tick. The QR message is this callback's own message.
            pending = await db.get_pending_payment(qr_id)
            assign_phone = pending.get("assign_phone") if pending else None
            await award_razorpay_payment(
                cq.from_user.id, qr_id, plan_key,
                assign_phone=assign_phone, qr_msg=cq.message,
            )
        elif status == "expired":
            await cq.answer(f"{em.ERROR} QR expired. Generate a new one.", show_alert=True)
        else:
            await cq.answer(f"{em.LOADING} Payment not detected yet. Wait a moment.", show_alert=True)

    # ── Crypto Plans ──

    @app.on_callback_query(filters.regex("^cr_plans$"))
    @verified
    async def cb_cr_plans(_, cq: CallbackQuery):
        buttons = []
        for key, plan in CRYPTO_PLANS.items():
            buttons.append([InlineKeyboardButton(
                f"{plan['credits']} Credits — {plan['amount_usdt']} USDT",
                callback_data=f"cr_net:{key}", style=S.SUCCESS,
            )])
        buttons.append([InlineKeyboardButton(f"{em.EDIT} Custom Amount", callback_data="cr_custom", style=S.PRIMARY)])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="buy_credits", style=S.PRIMARY)])
        await safe_edit(cq.message,
            f"{em.COIN} **Crypto — Choose a plan:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^cr_net:"))
    @verified
    async def cb_cr_net(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        buttons = [
            [InlineKeyboardButton("BSC (BEP20)", callback_data=f"cr_addr:BSC:{plan_key}", style=S.PRIMARY)],
            [InlineKeyboardButton("TRC20 (TRON)", callback_data=f"cr_addr:TRX:{plan_key}", style=S.SUCCESS)],
            [InlineKeyboardButton("ERC20 (Ethereum)", callback_data=f"cr_addr:ETH:{plan_key}", style=S.PRIMARY)],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="cr_plans", style=S.PRIMARY)],
        ]
        await safe_edit(cq.message,
            f"{em.GLOBE} **Select network for USDT deposit:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^cr_addr:"))
    @verified
    async def cb_cr_addr(_, cq: CallbackQuery):
        parts = cq.data.split(":")
        network, plan_key = parts[1], parts[2]
        plan = get_crypto_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)

        await safe_edit(cq.message, f"{em.LOADING} Fetching deposit address...")
        ok, info = await payments.get_binance_deposit_address("USDT", network)
        if not ok:
            return await safe_edit(cq.message,
                f"{em.ERROR} Could not fetch address: {info.get('error')}\nTry later.",
                reply_markup=back_kb("buy_credits"),
            )

        address = info["address"]
        tag = info.get("tag", "")
        net_label = {"BSC": "BSC (BEP20)", "TRX": "TRC20 (TRON)", "ETH": "ERC20 (Ethereum)"}

        pay_states[cq.from_user.id] = {
            "plan_key": plan_key,
            "network": network,
            "amount_usdt": float(plan["amount_usdt"]),
        }

        text = (
            f"{em.COIN} **USDT Deposit**\n\n"
            f"Send **{plan['amount_usdt']} USDT** on **{net_label.get(network, network)}** to:\n\n"
            f"`{address}`\n"
            + (f"Memo/Tag: `{tag}`\n" if tag else "") +
            f"\nAfter sending, **reply with your TX hash** here.\n"
            f"Type `cancel` to abort.\n\n"
            f"{em.GIFT} You'll receive **{plan['credits']} credits**"
        )
        await safe_edit(cq.message,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_pay", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex("^cancel_pay$"))
    @verified
    async def cb_cancel_pay(_, cq: CallbackQuery):
        pay_states.pop(cq.from_user.id, None)
        await safe_edit(cq.message,
            f"{em.ERROR} Payment cancelled. No charges were made.",
            reply_markup=back_kb("main_menu"),
        )

    @app.on_callback_query(filters.regex("^rz_custom$"))
    @verified
    async def cb_rz_custom(_, cq: CallbackQuery):
        auth_states[cq.from_user.id] = {"step": "rz_custom_amount"}
        await safe_edit(cq.message,
            f"{em.SMS} **Enter the number of credits you want to purchase (minimum 10):**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="buy_credits", style=S.DANGER)]
            ])
        )

    @app.on_callback_query(filters.regex("^cr_custom$"))
    @verified
    async def cb_cr_custom(_, cq: CallbackQuery):
        auth_states[cq.from_user.id] = {"step": "cr_custom_amount"}
        await safe_edit(cq.message,
            f"{em.SMS} **Enter the number of credits you want to purchase (minimum 10):**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="buy_credits", style=S.DANGER)]
            ])
        )

    # ── Telegram Stars Plans ──

    @app.on_callback_query(filters.regex("^stars_plans$"))
    @verified
    async def cb_stars_plans(_, cq: CallbackQuery):
        buttons = []
        for key, plan in STARS_PLANS.items():
            buttons.append([InlineKeyboardButton(
                f"{plan['credits']} Credits — ⭐{plan['stars']} Stars",
                callback_data=f"stars_pay:{key}",
                style=S.SUCCESS,
            )])
        buttons.append([InlineKeyboardButton(f"{em.EDIT} Custom Amount", callback_data="stars_custom", style=S.PRIMARY)])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="buy_credits", style=S.PRIMARY)])
        await safe_edit(cq.message,
            f"{em.STAR} **Telegram Stars — Choose a plan:**\n"
            f"Rate: **1 Star = 1 Credit**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^stars_pay:"))
    @verified
    async def cb_stars_pay(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        plan = get_stars_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)
        await cq.answer()
        await app.send_invoice(
            chat_id=cq.from_user.id,
            title=f"{plan['credits']} Credits",
            description=f"Top-up {plan['credits']} Credits in OTP Bot using Telegram Stars",
            payload=f"stars:{cq.from_user.id}:{plan_key}:{plan['credits']}:{int(time.time())}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{plan['credits']} Credits", amount=plan['stars'])],
        )

    @app.on_callback_query(filters.regex("^stars_custom$"))
    @verified
    async def cb_stars_custom(_, cq: CallbackQuery):
        auth_states[cq.from_user.id] = {"step": "stars_custom_amount"}
        await safe_edit(cq.message,
            f"{em.SMS} **Enter the number of credits you want to purchase via Telegram Stars (minimum 10):**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="buy_credits", style=S.DANGER)]
            ])
        )

    @app.on_pre_checkout_query()
    async def on_pre_checkout(_, query: PreCheckoutQuery):
        if query.invoice_payload and query.invoice_payload.startswith("stars:"):
            await app.answer_pre_checkout_query(query.id, ok=True)
        else:
            await app.answer_pre_checkout_query(query.id, ok=False, error_message="Invalid payment request")

    @app.on_message(filters.successful_payment)
    async def on_successful_payment(_, message: Message):
        db.begin_user_cache()
        sp = message.successful_payment
        if not sp or not sp.invoice_payload or not sp.invoice_payload.startswith("stars:"):
            return

        parts = sp.invoice_payload.split(":")
        if len(parts) < 4:
            return

        try:
            user_id = int(parts[1])
            plan_key = parts[2]
            credits = int(parts[3])
        except (ValueError, IndexError):
            return

        charge_id = sp.telegram_payment_charge_id or f"stars_{user_id}_{int(time.time())}"

        # Deduplicate payment using ref_id
        existing = await db.payments.find_one({"ref_id": charge_id})
        if existing:
            return

        await db.add_credits(user_id, credits)
        await db.save_payment(user_id, "telegram_stars", plan_key, sp.total_amount, "XTR", charge_id, credits=credits)
        await _check_referral_reward(user_id, credits)

        new_balance = await db.get_credits(user_id)
        buyer = await db.get_user(user_id)
        buyer_name = (buyer.get("first_name") or buyer.get("username") or str(user_id)) if buyer else str(user_id)

        await alert(bot,
            f"{em.STAR} **Credits Purchased (Telegram Stars)**\n\n"
            f"{em.USER} User: `{user_id}` ({buyer_name})\n"
            f"{em.GIFT} Credits: +{credits}\n"
            f"{em.STAR} Stars: ⭐{sp.total_amount}\n"
            f"{em.MONEY} New balance: {new_balance}"
        )

        await message.reply(
            f"{em.SUCCESS} **Payment Successful!**\n\n"
            f"You paid ⭐ **{sp.total_amount} Stars** and received **{credits} credits**.\n"
            f"{em.MONEY} Current Balance: **{new_balance} credits**"
        )

    # ── Sell Account (User Marketplace) ──

    @app.on_callback_query(filters.regex("^sell_account$"))
    @verified
    async def cb_sell_account(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        stats = await db.get_seller_stats(user_id)

        counts = stats["listings"]
        total_submitted = sum(counts.values())
        active_cnt = counts.get("active", 0)
        pending_cnt = counts.get("pending_price", 0)
        sold_cnt = counts.get("sold", 0)

        text = (
            f"{em.DOLLAR} **Sell Your Telegram Accounts**\n\n"
            f"List your Telegram accounts for sale and earn credits when buyers purchase them!\n\n"
            f"<blockquote>"
            f"• {em.MONEY} **Seller Cut:** {SELLER_PAYOUT_PERCENT}% of the sale price — paid directly to your wallet\n"
            f"• {em.GLOBE} **Pricing:** Auto-determined by category (country, year, email status)\n"
            f"• {em.LOCK} **Security:** Account credentials are stored safely\n"
            f"• {em.CREDIT} **Payout:** Earnings land in your withdrawable balance instantly on sale"
            f"</blockquote>\n\n"
            f"{em.STATS} **Your Seller Stats:**\n"
            f"• Submissions: **{total_submitted}** (🟢 {active_cnt} active | ⏳ {pending_cnt} pending | 🔴 {sold_cnt} sold)\n"
            f"• Total Earned: **{stats['earned_total']} credits** | Withdrawable Balance: **{stats.get('balance', 0)} credits**"
        )

        buttons = [
            [InlineKeyboardButton(f"{em.ADD} Submit Account", callback_data="submit_account", style=S.SUCCESS)],
            [
                InlineKeyboardButton(f"{em.LIST} My Listings ({total_submitted})", callback_data="my_listings", style=S.DEFAULT),
                InlineKeyboardButton(f"{em.MONEY} Withdraw Earnings", callback_data="withdraw_payout", style=S.PRIMARY),
            ],
            [
                InlineKeyboardButton(f"{em.PHONE} Login to My Accounts ({active_cnt})", callback_data="my_accounts", style=S.DEFAULT),
                InlineKeyboardButton(f"{em.STATS} Sold Stats ({sold_cnt})", callback_data="seller_sold", style=S.DEFAULT),
            ],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)],
        ]

        await safe_edit(cq.message, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(filters.regex("^submit_account$"))
    @verified
    async def cb_submit_account(_, cq: CallbackQuery):
        sell_states[cq.from_user.id] = {"step": "sell_phone"}
        await safe_edit(cq.message,
            f"{em.PHONE} **Submit Telegram Account for Sale**\n\n"
            "Send the phone number of the account in international format:\n"
            "Example: `+1234567890`\n\n"
            "A login code will be sent to the Telegram app of that account.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_sell", style=S.DANGER)],
            ]),
        )

    @app.on_callback_query(filters.regex("^cancel_sell$"))
    @verified
    async def cb_cancel_sell(_, cq: CallbackQuery):
        state = sell_states.pop(cq.from_user.id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await safe_edit(cq.message, f"{em.ERROR} Account submission cancelled.", reply_markup=back_kb("sell_account"))

    @app.on_callback_query(filters.regex("^sell_recheck$"))
    @verified
    async def cb_sell_recheck(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        pending = sell_recheck_states.get(user_id)
        if not pending:
            await cq.answer("This request expired. Please submit the account again.", show_alert=True)
            await safe_edit(cq.message, f"{em.ERROR} Re-check request expired.", reply_markup=back_kb("sell_account"))
            return

        await safe_edit(cq.message, f"{em.LOADING} Re-checking active sessions for `{pending['phone']}`...")

        # The login client was already disconnected; reconnect from the stored
        # session string just to re-count active sessions.
        client = Client(
            name=f"recheck_{pending['phone'].replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=pending["session_string"],
            in_memory=True,
        )
        try:
            await client.start()
            sess_cnt, sess_info = await get_active_sessions_info(client)
            await client.stop()
        except Exception as e:
            try:
                await client.stop()
            except Exception:
                pass
            await safe_edit(cq.message,
                f"{em.ERROR} Couldn't re-check the session: `{e}`\n\n"
                f"Please submit the account again.",
                reply_markup=back_kb("sell_account"),
            )
            sell_recheck_states.pop(user_id, None)
            return

        if sess_cnt > 1:
            await safe_edit(cq.message,
                f"{em.ERROR} **Still Multiple Active Sessions!**\n\n"
                f"⚠️ Please go to **Telegram Settings ➔ Devices**, remove **ALL** active sessions (including yourself), and leave **ONLY** the session named `OTP BOT`, then tap **Re-check** again.\n\n"
                f"{sess_info}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{em.SEARCH} Re-check Sessions", callback_data="sell_recheck", style=S.PRIMARY)],
                    [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="sell_account", style=S.DANGER)],
                ]),
            )
            return

        # Sessions are clean now — resume the submission with the fresh count.
        sell_recheck_states.pop(user_id, None)
        await _complete_sell_submission(
            user_id, cq.message, pending["phone"], pending["session_string"],
            pending["password"], pending["cc"], pending["acc_id"],
            pending["acc_year"], pending["has_email"], False, sess_cnt, sess_info,
        )

    @app.on_callback_query(filters.regex(r"^my_listings$|^pg_ml:\d+$"))
    @verified
    async def cb_my_listings(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ml:") else 0

        listings = await db.get_user_sell_listings(user_id)
        if not listings:
            await safe_edit(cq.message,
                f"{em.LIST} **No listings submitted yet.**\n\n"
                f"Tap **Submit Account** in the Sell Account menu to start selling.",
                reply_markup=back_kb("sell_account"),
            )
            return

        all_buttons = []
        status_icons = {
            "active": f"{em.ONLINE}",
            "pending_price": f"{em.LOADING}",
            "sold": f"{em.OFFLINE}",
            "removed": f"{em.BLOCKED}",
        }
        status_labels = {
            "active": "Listed (Active)",
            "pending_price": "Pending Price Setup",
            "sold": "Sold",
            "removed": "Removed",
        }

        for lst in listings:
            phone = lst["phone_number"]
            st = lst.get("status", "unknown")
            icon = status_icons.get(st, f"{em.INFO}")
            label = status_labels.get(st, st)
            payout = lst.get("payout_credits", 0)
            payout_str = f" (+{payout} cr)" if st == "sold" else ""

            all_buttons.append([InlineKeyboardButton(
                f"{icon} {mask_phone(phone)} — {label}{payout_str}",
                callback_data="noop", style=S.DEFAULT,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_ml", "sell_account")
        await safe_edit(cq.message,
            f"{em.LIST} **Your Account Listings ({len(listings)})**" + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^my_accounts$|^pg_ma:\d+$"))
    @verified
    async def cb_my_accounts(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ma:") else 0

        listings = await db.get_user_sell_listings(user_id)
        active = [l for l in listings if l.get("status") == "active"]
        if not active:
            await safe_edit(cq.message,
                f"{em.PHONE} **No accounts available to log into.**\n\n"
                f"Only **listed (active)** accounts that haven't been bought can be accessed here.",
                reply_markup=back_kb("sell_account"),
            )
            return

        all_buttons = []
        for lst in active:
            phone = lst["phone_number"]
            cc = lst.get("country_code", "XX")
            flag = get_country_flag(cc)
            yr = lst.get("account_year")
            mo = lst.get("account_month")
            yr_str = f" ~{format_account_year(yr, mo)}" if yr else ""
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {mask_phone(phone)}{yr_str} — Login",
                callback_data=f"slogin:{phone}", style=S.PRIMARY,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_ma", "sell_account")
        await safe_edit(cq.message,
            f"{em.PHONE} **Login to Your Accounts ({len(active)})**\n\n"
            f"These are your listed accounts not yet bought by anyone. Tap one to receive its "
            f"login OTP — **free**, and it stays listed for buyers." + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^slogin:"))
    @verified
    async def cb_slogin(_, cq: CallbackQuery):
        phone = cq.data.split(":", 1)[1]
        await cq.answer()
        await _seller_login(cq.from_user.id, phone, edit_msg=cq.message)

    @app.on_callback_query(filters.regex(r"^seller_sold$|^pg_ss:\d+$"))
    @verified
    async def cb_seller_sold(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ss:") else 0

        listings = await db.get_user_sell_listings(user_id)
        sold = [l for l in listings if l.get("status") == "sold"]
        stats = await db.get_seller_stats(user_id)

        if not sold:
            await safe_edit(cq.message,
                f"{em.STATS} **No accounts sold yet.**\n\n"
                f"You'll be paid **{SELLER_PAYOUT_PERCENT}%** of the price the moment a buyer purchases one of your listings.",
                reply_markup=back_kb("sell_account"),
            )
            return

        total_payout = sum(l.get("payout_credits", 0) for l in sold)
        all_buttons = []
        for lst in sold:
            phone = lst["phone_number"]
            cc = lst.get("country_code", "XX")
            flag = get_country_flag(cc)
            yr = lst.get("account_year")
            mo = lst.get("account_month")
            yr_str = f"{format_account_year(yr, mo)}" if yr else "?"
            payout = lst.get("payout_credits", 0)
            sold_at = lst.get("sold_at")
            when = sold_at.strftime("%d/%m/%Y") if sold_at else "—"
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {mask_phone(phone)} · {yr_str} · +{payout}cr · {when}",
                callback_data="noop", style=S.DEFAULT,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_ss", "sell_account")
        await safe_edit(cq.message,
            f"{em.STATS} **Your Sold Accounts ({len(sold)})**\n\n"
            f"{em.MONEY} Total earned (all-time): **{stats['earned_total']} credits**\n"
            f"{em.DOLLAR} Payout from these sales: **{total_payout} credits**" + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex("^withdraw_payout$"))
    @verified
    async def cb_withdraw_payout(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        balance = await db.get_balance(user_id)

        if balance <= 0:
            await cq.answer("No withdrawable balance available.", show_alert=True)
            return

        buttons = [
            [InlineKeyboardButton("UPI (India)", callback_data="withdraw_method:upi", style=S.DEFAULT)],
            [InlineKeyboardButton("USDT (BEP20 / TRC20)", callback_data="withdraw_method:crypto_usdt", style=S.DEFAULT)],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="sell_account", style=S.DEFAULT)],
        ]

        await safe_edit(cq.message,
            f"{em.MONEY} **External Withdrawal — Select Method**\n\n"
            f"Available balance: **{balance} credits**\n\n"
            f"Choose your payout method:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^withdraw_method:"))
    @verified
    async def cb_withdraw_method(_, cq: CallbackQuery):
        user_id = cq.from_user.id
        method = cq.data.split(":", 1)[1]

        balance = await db.get_balance(user_id)
        if balance <= 0:
            await cq.answer("No withdrawable balance available.", show_alert=True)
            return

        sell_states[user_id] = {
            "step": "sell_withdrawal_details",
            "method": method,
            "amount": balance,
        }

        prompt = "Send your UPI ID (e.g. `user@upi`):" if method == "upi" else "Send your USDT Wallet Address & Network (e.g. `0x123... (BEP20)`):"

        await safe_edit(cq.message,
            f"{em.NOTE} **Withdrawal Details ({method.upper()})**\n\n"
            f"Amount: **{balance} credits**\n\n"
            f"{prompt}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="sell_account", style=S.DANGER)],
            ]),
        )

    # ── Admin Seller Submissions & Withdrawals ──

    @app.on_callback_query(filters.regex("^seller_submissions$"))
    @verified
    async def cb_seller_submissions(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        pending = await db.get_pending_price_listings()
        if not pending:
            await safe_edit(cq.message,
                f"{em.INBOX} **No pending seller submissions.**\n\n"
                f"All seller accounts have category prices set and are active.",
                reply_markup=back_kb("admin_panel"),
            )
            return

        all_buttons = []
        for lst in pending:
            phone = lst["phone_number"]
            cc = lst.get("country_code", "XX")
            flag = get_country_flag(cc)
            yr = lst.get("account_year")
            mo = lst.get("account_month")
            yr_str = f" ({format_account_year(yr, mo)})" if yr else ""
            em_str = " +Email" if lst.get("email_added") else ""

            all_buttons.append([InlineKeyboardButton(
                f"{flag} {mask_phone(phone)}{yr_str}{em_str} — Details & Set Price",
                callback_data=f"view_pending_sub:{lst['_id']}", style=S.PRIMARY,
            )])

        await safe_edit(cq.message,
            f"{em.INBOX} **Pending Seller Submissions ({len(pending)})**\n\n"
            f"These accounts are waiting for their category price to be set.\n"
            f"Tap an account to view details and set its category price — once set, it will activate automatically.",
            reply_markup=InlineKeyboardMarkup(all_buttons + [[InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)]]),
        )

    @app.on_callback_query(filters.regex(r"^view_pending_sub:"))
    @verified
    async def cb_view_pending_sub(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        lid = cq.data.split(":", 1)[1]
        listing = await db.get_sell_listing_by_id(lid)
        if not listing or listing.get("status") != "pending_price":
            await cq.answer("This submission is no longer pending.", show_alert=True)
            await cb_seller_submissions(_, cq)
            return

        phone = listing["phone_number"]
        cc = listing.get("country_code", "XX")
        flag = get_country_flag(cc)
        cname = get_country_name(cc)
        yr = listing.get("account_year")
        mo = listing.get("account_month")
        year_label = format_account_year(yr, mo)
        has_email = listing.get("email_added", False)
        seller_id = listing.get("seller_id")

        text = (
            f"{em.INBOX} **Pending Seller Submission Details**\n\n"
            f"{em.USER} Seller ID: `{seller_id}`\n"
            f"{em.PHONE} Phone: `{mask_phone(phone)}`\n"
            f"{flag} Country: **{cname}** (`{cc}`)\n"
            f"{em.CALENDAR} Account Age: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{'Yes' if has_email else 'No'}**\n\n"
            f"⚠️ **Category Price is not set** for `{cname}` ({year_label}, Email: {'Yes' if has_email else 'No'}).\n"
            f"Tap **Set Category Price** below to configure the price and activate this account."
        )

        buttons = [
            [InlineKeyboardButton(f"{em.MONEY} Set Category Price", callback_data=f"set_pending_cat_price:{lid}", style=S.PRIMARY)],
            [InlineKeyboardButton(f"{em.BACK} Back to Submissions", callback_data="seller_submissions", style=S.DEFAULT)],
        ]
        await safe_edit(cq.message, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(filters.regex(r"^set_pending_cat_price:"))
    @verified
    async def cb_set_pending_cat_price(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        lid = cq.data.split(":", 1)[1]
        listing = await db.get_sell_listing_by_id(lid)
        if not listing:
            await cq.answer("Listing not found.", show_alert=True)
            return

        cc = listing.get("country_code", "XX")
        yr = listing.get("account_year")
        mo = listing.get("account_month")
        has_email = listing.get("email_added", False)
        phone = listing["phone_number"]

        auth_states[cq.from_user.id] = {
            "step": "update_category_price_input",
            "country_code": cc,
            "year": yr,
            "email_added": has_email,
            "listing_id": lid,
        }

        flag = get_country_flag(cc)
        cname = get_country_name(cc)
        email_str = "Yes" if has_email else "No"
        year_str = format_account_year(yr, mo)

        await safe_edit(cq.message,
            f"{em.MONEY} **Set Category Price**\n\n"
            f"{em.PHONE} Account: `{mask_phone(phone)}`\n"
            f"{flag} Country: **{cname}** (`{cc}`)\n"
            f"{em.CALENDAR} Account Age: **{year_str}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n\n"
            f"Send the new price in credits for this category (e.g. `220`):\n"
            f"*(Once set, this account and any other pending accounts in this category will activate automatically)*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="seller_submissions", style=S.DANGER)]
            ])
        )

    @app.on_callback_query(filters.regex("^seller_withdrawals$"))
    @verified
    async def cb_seller_withdrawals(_, cq: CallbackQuery):
        if not await db.is_moderator(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Moderator only.", show_alert=True)
            return

        withdrawals = await db.get_pending_withdrawals()
        if not withdrawals:
            await safe_edit(cq.message,
                f"{em.DOLLAR} **No pending withdrawal requests.**",
                reply_markup=back_kb("admin_panel"),
            )
            return

        lines = []
        buttons = []
        for w in withdrawals:
            wid = str(w["_id"])
            sid = w["seller_id"]
            amt = w["amount"]
            mth = w["method"]
            dtl = w["details"]
            rtype = w.get("request_type", "seller")
            rtag = "[ADMIN]" if rtype == "admin" else "[SELLER]"
            unit = "₹" if rtype == "admin" else "cr"
            lines.append(f"• {rtag} `{sid}`: **{amt} {unit}** via {mth.upper()} (`{dtl}`)")
            buttons.append([
                InlineKeyboardButton(f"{em.SUCCESS} Paid {sid} ({amt} {unit})", callback_data=f"approve_w:{wid}", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.ERROR} Reject", callback_data=f"reject_w:{wid}", style=S.DANGER),
            ])

        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)])

        await safe_edit(cq.message,
            f"{em.DOLLAR} **Pending Withdrawal Requests ({len(withdrawals)})**\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^approve_w:"))
    @verified
    async def cb_approve_w(_, cq: CallbackQuery):
        if not await db.is_moderator(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Moderator only.", show_alert=True)
            return

        wid = cq.data.split(":", 1)[1]
        ok = await db.mark_withdrawal_done(wid, admin_note=f"Approved by moderator {cq.from_user.id}")
        if ok:
            await cq.answer("Withdrawal marked as paid!", show_alert=True)
            await cb_seller_withdrawals(app, cq)
        else:
            await cq.answer("Withdrawal request not found or already processed.", show_alert=True)

    @app.on_callback_query(filters.regex(r"^reject_w:"))
    @verified
    async def cb_reject_w(_, cq: CallbackQuery):
        if not await db.is_moderator(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Moderator only.", show_alert=True)
            return

        wid = cq.data.split(":", 1)[1]
        doc = await db.mark_withdrawal_rejected(wid, reason="Rejected by moderator")
        if doc:
            await cq.answer("Withdrawal rejected.", show_alert=True)
            try:
                msg = (
                    f"{em.ERROR} **Admin Withdrawal Request Rejected**\n\nYour withdrawal request for **₹{doc['amount']}** was rejected by moderator."
                    if doc.get("request_type") == "admin"
                    else f"{em.ERROR} **Withdrawal Request Rejected**\n\nYour withdrawal request for **{doc['amount']} credits** was rejected by moderator.\nThe amount has been refunded to your withdrawable balance."
                )
                await app.send_message(doc["seller_id"], msg)
            except Exception:
                pass
            await cb_seller_withdrawals(app, cq)
        else:
            await cq.answer("Withdrawal request not found or already processed.", show_alert=True)

    # ── Admin Withdrawal Request Flow ──

    @app.on_callback_query(filters.regex("^admin_withdrawal_req$"))
    @verified
    async def cb_admin_withdrawal_req(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        avail_info = await db.get_admin_withdrawal_available()
        tot_rev = avail_info["total_revenue"]
        cut = avail_info["platform_cut"]
        net = avail_info["net_revenue"]
        comp = avail_info["total_completed"]
        pend = avail_info["pending_admin"]
        avail = avail_info["available"]

        msg = (
            f"{em.DOLLAR} **Admin Withdrawal Request**\n\n"
            f"• Total Revenue: **₹{tot_rev:,.2f}**\n"
            f"• Platform Cut (30%): **-₹{cut:,.2f}**\n"
            f"• Net Revenue (70%): **₹{net:,.2f}**\n"
            f"• Completed Withdrawals: **-₹{comp:,.2f}**\n"
            f"• Pending Admin Requests: **-₹{pend:,.2f}**\n\n"
            f"💰 **Available Pool: ₹{avail:,.2f}**\n\n"
        )
        if avail <= 0:
            msg += f"{em.ERROR} No funds currently available to withdraw."
            buttons = [[InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)]]
        else:
            msg += "Select payout method:"
            buttons = [
                [
                    InlineKeyboardButton(f"💳 UPI", callback_data="admin_wmth:upi", style=S.SUCCESS),
                    InlineKeyboardButton(f"🪙 Crypto USDT", callback_data="admin_wmth:usdt", style=S.SUCCESS),
                ],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)],
            ]
        await safe_edit(cq.message, msg, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(filters.regex(r"^admin_wmth:(upi|usdt)$"))
    @verified
    async def cb_admin_wmth(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        mth = cq.data.split(":")[1]
        avail_info = await db.get_admin_withdrawal_available()
        avail = avail_info["available"]
        if avail <= 0:
            await cq.answer("No funds available to withdraw.", show_alert=True)
            return

        admin_withdraw_states[cq.from_user.id] = {
            "step": "admin_withdrawal_amount",
            "method": mth,
            "available": avail,
        }
        await safe_edit(
            cq.message,
            f"{em.MONEY} **Enter Withdrawal Amount (INR)**\n\n"
            f"Available pool: **₹{avail:,.2f}**\n\n"
            f"Send the amount you wish to withdraw:",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Help / Cancel ──

    def _build_help_text(is_admin: bool) -> str:
        admin_section = (
            f"\n\n{em.GEAR} **Admin Commands:**\n"
            "<blockquote expandable>"
            "/info `<userid or @username>` — Look up user details\n"
            "/broadcast `<message>` — Broadcast to all users\n"
            "/broadcast `-name` `<message>` — Broadcast with your name\n"
            "/wotp `<ORD-XXXXXXXX>` `<code>` — Relay a WhatsApp OTP to the buyer"
            "</blockquote>\n\n"
            f"{em.SMS} **Handling a WhatsApp order:**\n"
            "<blockquote expandable>"
            "1. You get a notification with the number and buyer\n"
            "2. Tap **Confirm** once you're connected to the device — or "
            "**Not Available** to cancel and refund the buyer in full\n"
            "3. The buyer is told to request the OTP on that number\n"
            "4. Read the code off the device and send "
            "`/wotp <order_id> <code>`\n"
            "5. Manage stock in **Admin Panel → WhatsApp Numbers**"
            "</blockquote>"
        ) if is_admin else ""
        return (
            f"{em.HELP} **Help — OTP Bot**\n\n"
            f"{em.PIN} **How it works:**\n"
            "<blockquote>"
            "1. Buy credits via UPI or Crypto\n"
            "2. Tap **Buy Account** and select a country\n"
            "3. Pick an available account — credits are deducted\n"
            "4. The login OTP for that account is forwarded to you\n"
            "5. The account auto-releases if unused before the timeout"
            "</blockquote>\n\n"
            f"{em.SMS} **Buying a WhatsApp number:**\n"
            "<blockquote>"
            "These are fulfilled **by hand**, so they take a little longer:\n"
            "1. Tap **Buy WhatsApp** and pick a number — credits are deducted\n"
            "2. Wait for an admin to connect to the device\n"
            "3. Once confirmed you get the full number — request your OTP on it\n"
            "4. The admin reads the code off the device and sends it to you\n\n"
            f"{em.INFO} Only one WhatsApp order at a time. You can cancel for a "
            "full refund while still waiting, and you're refunded automatically "
            "if the admin can't fulfil it."
            "</blockquote>\n\n"
            f"{em.FAQ} **Features:**\n"
            "<blockquote>"
            f"• {em.PHONE} **Buy Account** — Purchase a Telegram account and get its login OTP\n"
            f"• {em.SMS} **Buy WhatsApp** — Manually fulfilled WhatsApp numbers\n"
            f"• {em.LOGS} **My History** — View your past purchases\n"
            f"• {em.CREDIT} **Buy Credits** — Top up via UPI or USDT\n"
            f"• {em.PHONE} **Support** — Contact our support agents"
            "</blockquote>\n\n"
            f"{em.SETTINGS} **Commands:**\n"
            "<blockquote>"
            "/start — Main menu\n"
            "/help — This help page\n"
            "/info `<ORD-XXXXXXXX>` — Check an order's details\n"
            "/feedback — Give feedback & rate our bot\n"
            "/cancel — Cancel current operation"
            "</blockquote>"
            f"{admin_section}"
        )

    @app.on_message(filters.command("help") & filters.private)
    @verified
    async def cmd_help(_, message: Message):
        is_adm = await db.is_admin(message.from_user.id)
        await message.reply(
            _build_help_text(is_adm),
            reply_markup=back_kb(),
        )

    @app.on_callback_query(filters.regex("^help$"))
    @verified
    async def cb_help(_, cq: CallbackQuery):
        is_adm = await db.is_admin(cq.from_user.id)
        await safe_edit(
            cq.message,
            _build_help_text(is_adm),
            reply_markup=back_kb(),
        )

    @app.on_callback_query(filters.regex("^how_to_use$"))
    @verified
    async def cb_how_to_use(_, cq: CallbackQuery):
        try:
            await cq.message.delete()
        except Exception:
            pass

        caption = (
            f"🎬 **How to Use OTP Bot — Tutorial Video**\n\n"
            f"Here is a quick guide on how to use the bot:\n"
            f"<blockquote>"
            f"1️⃣ **Top Up**: Buy credits via UPI or Crypto.\n"
            f"2️⃣ **Buy an Account**: Go to **Buy Account**, select a country, and purchase.\n"
            f"3️⃣ **Get Login OTP**: The bot will display the account's login OTP instantly."
            f"</blockquote>"
        )

        video_sent = False
        try:
            msg = await app.get_messages("Vault_store_News", 222)
            if msg and msg.video:
                await app.send_video(
                    chat_id=cq.from_user.id,
                    video=msg.video.file_id,
                    caption=caption,
                    reply_markup=back_kb("main_menu"),
                )
                video_sent = True
        except Exception as e:
            log.warning("Failed to fetch video from channel Vault_store_News/222: %s", e)

        if not video_sent:
            import os
            local_video = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_2026-07-07_17-55-47.mp4")
            if os.path.exists(local_video):
                try:
                    await app.send_video(
                        chat_id=cq.from_user.id,
                        video=local_video,
                        caption=caption,
                        reply_markup=back_kb("main_menu"),
                    )
                    video_sent = True
                except Exception as e:
                    log.error("Failed to send local fallback video: %s", e)
            else:
                log.error("Local fallback video file does not exist: %s", local_video)

        if not video_sent:
            await app.send_message(
                chat_id=cq.from_user.id,
                text=caption + "\n\n*(Error loading tutorial video, showing text only)*",
                reply_markup=back_kb("main_menu"),
            )


    @app.on_message(filters.command("cancel") & filters.private)
    @verified
    async def cmd_cancel(_, message: Message):
        feedback_states.pop(message.from_user.id, None)
        state = auth_states.pop(message.from_user.id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await message.reply(
            f"{em.ERROR} Current operation cancelled. Use the menu below to continue.",
            reply_markup=main_menu_kb(
                await db.is_admin(message.from_user.id)
            ),
        )

    # ── Feedback Flow ──

    @app.on_message(filters.command("feedback") & filters.private)
    @verified
    async def cmd_feedback(_, message: Message):
        await message.reply(
            "⭐ **Rate Your Experience**\n\n"
            "How would you rate your experience with our bot? Please tap a rating (1-5) below:",
            reply_markup=_feedback_kb(),
        )

    @app.on_callback_query(filters.regex("^feedback$"))
    @verified
    async def cb_feedback(_, cq: CallbackQuery):
        await _answer_cq(cq)
        await safe_edit(
            cq.message,
            "⭐ **Rate Your Experience**\n\n"
            "How would you rate your experience with our bot? Please tap a rating (1-5) below:",
            reply_markup=_feedback_kb(),
        )

    @app.on_callback_query(filters.regex("^rate_([1-5])$"))
    @verified
    async def cb_rate(_, cq: CallbackQuery):
        await _answer_cq(cq)
        rating = int(cq.matches[0].group(1))
        user_id = cq.from_user.id

        if rating >= 4:
            bot_me = await app.get_me()
            ref_link = f"https://t.me/{bot_me.username}?start=ref_{user_id}"
            share_text = "Buy Telegram accounts & OTPs easily with this bot!"
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_text)}"

            buttons = [
                [InlineKeyboardButton("📲 Share Bot", url=share_url)],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu")],
            ]
            await safe_edit(
                cq.message,
                f"❤️ **Thank You For Your Support!** (Rating: {rating}/5 ⭐)\n\n"
                "We're thrilled to hear that you love using our bot! If you find our service useful, please share it with your friends using the button below:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            # ponytail: transient 60s memory lock for feedback collection without persistent database queue
            feedback_states[user_id] = {
                "expiry": time.time() + 60,
                "rating": rating,
            }
            await safe_edit(
                cq.message,
                f"🙏 **We Value Your Feedback** (Rating: {rating}/5 ⭐)\n\n"
                "We're sorry to hear that! Please reply to this message within **1 minute** with your feedback or suggestions, and we'll send it directly to our team.",
                reply_markup=back_kb(),
            )


# ── Auth helpers ──

async def _account_info(client: Client, current_phone: str = "") -> tuple[int | None, int | None, int | None, bool, bool, int, str]:
    """Fetch account id + creation year/month (exact via MTProto or estimated) + has_email + is_peer_flood + session_count + session_info."""
    try:
        me = await client.get_me()
        account_id = me.id
    except Exception as e:
        log.error("Failed to get me from client: %s", e)
        return None, None, None, False, False, 1, ""

    has_email = False
    try:
        pwd_info = await client.invoke(
            __import__("pyrogram").raw.functions.account.GetPassword()
        )
        login_email = getattr(pwd_info, "login_email_pattern", None)
        has_email = login_email is not None
    except Exception as e:
        log.warning("Failed to check email status: %s", e)

    exact_year = None
    exact_month = None
    is_peer_flood = False

    # registration_month can only be read by ANOTHER account that A has just
    # messaged (it lives in PeerSettings, not on A's own GetFullUser). So we run a
    # cross-account probe: A messages a few active observer accounts, and each
    # observer reads A's registration_month back. The message attempt doubles as
    # the real PEER_FLOOD test — if A can't message, it's spam-limited/unsellable.
    try:
        reg_month, is_peer_flood = await clients.probe_registration_month(client, account_id, current_phone)
        if reg_month:
            yr, mo = parse_reg_month(reg_month)
            if yr:
                exact_year = yr
                exact_month = mo
                log.info("Exact registration date for %s: year=%d month=%s (via cross-account probe)", me.id, yr, mo)
    except Exception as e:
        log.warning("Error during cross-account registration probe: %s", e)

    year = exact_year if exact_year is not None else estimate_account_year(account_id)
    session_count, session_info = await get_active_sessions_info(client)
    return account_id, year, exact_month, has_email, is_peer_flood, session_count, session_info



_COUPON_RE = re.compile(rf"^[{COUPON_ALPHABET}]{{{COUPON_CODE_LENGTH}}}$")


async def _handle_coupon_text(message, text: str) -> bool:
    """Try to redeem a bare coupon code. Returns True if the text was a code.

    Only reached when the user is not in any other text flow, so a code can
    never shadow a phone number, price, or 2FA password.
    """
    code = text.strip().upper()
    if not _COUPON_RE.match(code):
        return False

    status, credits = await db.redeem_coupon(message.from_user.id, code)
    if status in ("unknown", "no_user"):
        return False  # not one of ours — stay silent, it was probably just chatter
    if status == "already":
        await message.reply(f"{em.BLOCKED} You've already used this coupon.")
    elif status == "expired":
        await message.reply(f"{em.CLOCK} This coupon has expired. Watch the channel for tonight's codes.")
    else:
        hours = int(COUPON_OFFER_HOURS)
        await message.reply(
            f"{em.GIFT} **Coupon redeemed!**\n\n"
            f"{em.CREDIT} **{credits} credits OFF** your next number.\n"
            f"{em.CLOCK} Valid for {hours} hours.",
        )
    return True


async def _handle_phone(message: Message, phone: str):
    user_id = message.from_user.id
    if not phone.startswith("+"):
        phone = "+" + phone

    existing = await db.get_session(phone)
    if existing:
        auth_states.pop(user_id, None)
        await message.reply(
            f"{em.ERROR} **Account Already Added!**\n\n"
            f"The phone number `{phone}` is already registered in the database.",
            reply_markup=back_kb("admin_panel"),
        )
        return

    cc, cname, cflag = detect_country(phone)
    status_msg = await message.reply(f"{em.LOADING} Sending code to `{phone}` ({cflag} {cname})...")

    try:
        client = Client(
            name=f"auth_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            device_model="OTP BOT",
            app_version="OTP BOT 1.0",
            in_memory=True,
        )
        await client.connect()
        sent_code = await client.send_code(phone)
        auth_states[user_id] = {
            "step": "code",
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
        }
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code sent to `{phone}` ({cflag} {cname})\n\n"
            "Enter the verification code received on Telegram:\n\n"
            f"{em.FAQ} Add spaces or dots between digits (e.g. `1 2 3 4 5`).",
        )
    except PhoneNumberInvalid:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"{em.ERROR} Invalid phone number format.",
            reply_markup=back_kb("admin_panel"),
        )
    except FloodWait as e:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"{em.WARNING} FloodWait — try again in {e.value} seconds.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"{em.ERROR} Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_code(message: Message, code: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    clean_code = code.replace(" ", "").replace(".", "").replace("-", "")
    status_msg = await message.reply(f"{em.LOADING} Verifying code...")

    try:
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=state["phone_code_hash"],
            phone_code=clean_code,
        )
        acc_id, acc_year, acc_month, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        cc, cname, cflag = detect_country(phone)
        auth_states[user_id] = {
            "step": "confirm_country",
            "phone": phone,
            "session_string": session_string,
            "password": "",
            "country_code": cc,
            "account_id": acc_id,
            "account_year": acc_year,
            "account_month": acc_month,
            "email_added": has_email,
        }
        sess_warn = f"\n\n⚠️ **Notice:** Account has **{sess_cnt} active sessions**." if sess_cnt > 1 else ""
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code verified for `{phone}`\n\n"
            f"{em.GLOBE} Detected country: {cflag} **{cname}** ({cc})\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(acc_year, acc_month)}**\n"
            f"{em.MAIL} Email added: **{'Yes' if has_email else 'No'}**{sess_warn}\n\n"
            "Is this correct?",
            reply_markup=_confirm_country_kb(cflag, cname, cc, acc_year, acc_month),
        )
    except SessionPasswordNeeded:
        auth_states[user_id]["step"] = "password"
        await safe_edit(status_msg,
            f"{em.PASSWORD} This account has 2FA enabled.\n"
            "Enter the 2FA password:",
        )
    except PhoneCodeInvalid:
        await safe_edit(status_msg,
            f"{em.ERROR} Invalid code. Try again.\n\n"
            f"{em.FAQ} Add spaces or dots between digits (e.g. `1 2 3 4 5`) "
            "to avoid the code being blocked.",
        )
    except PhoneCodeExpired:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"{em.ERROR} Code expired. Go back and tap **Add Number** to try again.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"{em.ERROR} Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_password(message: Message, password: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    status_msg = await message.reply(f"{em.LOADING} Checking password...")

    try:
        await client.check_password(password)
        acc_id, acc_year, acc_month, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        cc, cname, cflag = detect_country(phone)
        auth_states[user_id] = {
            "step": "confirm_country",
            "phone": phone,
            "session_string": session_string,
            "password": password,
            "country_code": cc,
            "account_id": acc_id,
            "account_year": acc_year,
            "account_month": acc_month,
            "email_added": has_email,
        }
        await safe_edit(status_msg,
            f"{em.SUCCESS} Password accepted for `{phone}`\n\n"
            f"{em.GLOBE} Detected country: {cflag} **{cname}** ({cc})\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(acc_year, acc_month)}**\n"
            f"{em.MAIL} Email added: **{'Yes' if has_email else 'No'}**\n\n"
            "Is this correct?",
            reply_markup=_confirm_country_kb(cflag, cname, cc, acc_year, acc_month),
        )
    except PasswordHashInvalid:
        await safe_edit(status_msg, f"{em.ERROR} Wrong password. Try again:")
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"{em.ERROR} Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


# ── Re-add / country-price / password-update helpers ──

async def _handle_phone_direct(user_id: int, phone: str, reply_target):
    cc, cname, cflag = detect_country(phone)
    try:
        client = Client(
            name=f"auth_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )
        await client.connect()
        sent_code = await client.send_code(phone)
        old_cc = auth_states.get(user_id, {}).get("old_country", cc)
        auth_states[user_id] = {
            "step": "code",
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "country_code": old_cc,
        }
        await safe_edit(reply_target,
            f"{em.PENDING} **Re-adding** `{phone}` ({cflag} {cname})\n\n"
            f"{em.SUCCESS} Code sent. Enter the verification code:\n\n"
            f"{em.FAQ} Add spaces or dots between digits.\n"
            "Example: `1 2 3 4 5` or `1.2.3.4.5`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)],
            ]),
        )
    except FloodWait as e:
        auth_states.pop(user_id, None)
        await safe_edit(reply_target,
            f"{em.WARNING} FloodWait — try again in {e.value} seconds.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        await safe_edit(reply_target,
            f"{em.ERROR} Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _notify_activated_sellers(bot: Client, activated: list, cc: str, price: int):
    if not activated:
        return
    flag = get_country_flag(cc)
    cname = get_country_name(cc)
    for act in activated:
        sid = act.get("seller_id")
        pnum = act.get("phone_number", "")
        payout = act.get("payout_credits", int(price * SELLER_PAYOUT_PERCENT / 100))
        if sid:
            try:
                await bot.send_message(
                    sid,
                    f"{em.SUCCESS} **Your Account is Now Active & Listed!**\n\n"
                    f"{flag} Phone: `{mask_phone(pnum)}` ({cname})\n"
                    f"{em.MONEY} Category Price: **{price} credits**\n"
                    f"{em.DOLLAR} You'll earn **{payout} credits** ({SELLER_PAYOUT_PERCENT}%) **when a buyer purchases it**\n\n"
                    f"Your account is now live in the store for buyers!"
                )
            except Exception as e:
                log.error("Failed to notify seller %s of activated listing: %s", sid, e)


async def _handle_update_category_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    try:
        price = int(text.strip())
        if price < 1:
            await message.reply(f"{em.ERROR} Price must be at least 1. Try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid price. Send a number (e.g. `220`):")
        return
        
    cc = state["country_code"]
    year = state["year"]
    email = state["email_added"]

    await db.set_category_price(cc, year, email, price)
    activated = await db.check_and_activate_pending_listings(cc, year, email)
    auth_states.pop(user_id, None)

    await _notify_activated_sellers(message._client, activated, cc, price)

    flag = get_country_flag(cc)
    name = get_country_name(cc)
    email_str = "Yes" if email else "No"
    act_str = f"\n⚡ **{len(activated)} pending seller account(s) activated!**" if activated else ""

    await message.reply(
        f"{em.SUCCESS} Category price successfully updated!\n\n"
        f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
        f"{em.CALENDAR} Year Old: **{format_account_year(year)}**\n"
        f"{em.MAIL} Email: **{email_str}**\n"
        f"{em.MONEY} New Price: **{price}** credits per OTP{act_str}",
        reply_markup=main_menu_kb(True),
    )





async def _handle_manual_country(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]

    matches = search_country(text)
    if not matches:
        await message.reply(
            f"{em.ERROR} No matching country found.\n"
            "Try the full country name (e.g. `India`) or send its flag emoji 🇮🇳:",
        )
        return

    if len(matches) == 1:
        cc, name, flag = matches[0]
        state["country_code"] = cc
        state["step"] = "confirm_country"
        year = state.get("account_year")
        month = state.get("account_month")
        email_added = state.get("email_added", False)
        await message.reply(
            f"{em.GLOBE} Found: {flag} **{name}** ({cc})\n"
            f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
            f"{em.MAIL} Email added: **{'Yes' if email_added else 'No'}**\n\n"
            f"Confirm this country for `{state['phone']}`?",
            reply_markup=_confirm_country_kb(flag, name, cc, year, month, pick=True),
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{flag} {name}", callback_data=f"cc_pick:{cc}", style=S.DEFAULT)]
        for cc, name, flag in matches
    ]
    buttons.append([InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="cancel_auth", style=S.DANGER)])
    await message.reply(
        f"{em.GLOBE} **Multiple matches found.** Pick one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_update_password_old(message: Message, text: str):
    user_id = message.from_user.id
    auth_states[user_id]["old_password"] = text.strip()
    auth_states[user_id]["step"] = "update_password_new"
    await message.reply(f"{em.SUCCESS} Got it. Now send the **new 2FA password**:")


async def _handle_update_password_new(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]
    old_password = state.get("old_password", "")

    new_password = text.strip()
    status_msg = await message.reply(f"{em.LOADING} Updating password on Telegram...")

    try:
        await client.change_cloud_password(current_password=old_password, new_password=new_password)
        await client.stop()
        await db.set_session_password(phone, new_password)
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"{em.SUCCESS} Password updated for `{phone}`\n\n"
            f"{em.PASSWORD} New password: `{new_password}`",
            reply_markup=back_kb("admin_panel"),
        )
    except PasswordHashInvalid:
        await safe_edit(status_msg,
            f"{em.ERROR} The old password was wrong. Send the correct **current 2FA password**:",
        )
        auth_states[user_id]["step"] = "update_password_old"
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.stop()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"{em.ERROR} Error updating password: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_edit_num_country(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    phone = state["phone"]

    matches = search_country(text)
    if not matches:
        await message.reply(
            f"{em.ERROR} No matching country found.\n"
            "Try the full country name (e.g. `India`) or send its flag emoji 🇮🇳:",
        )
        return

    if len(matches) == 1:
        cc, name, flag = matches[0]
        auth_states.pop(user_id, None)
        await db.set_session_category(phone, country_code=cc)

        session = await db.get_session(phone)
        year = session.get("account_year") if session else None
        month = session.get("account_month") if session else None
        year_label = format_account_year(year, month)
        email = session.get("email_added", False) if session else False
        email_str = "Yes" if email else "No"
        price = await db.get_session_price(session) if session else 1

        await message.reply(
            f"{em.SUCCESS} Country updated to {flag} **{name}** ({cc})\n\n"
            f"{em.CONFIG} **Edit Category — `{phone}`**\n\n"
            f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n"
            f"{em.MONEY} Current Price: **{price}** credits",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.GLOBE} Change Country ({cc})", callback_data=f"echg_cc:{phone}", style=S.PRIMARY)],
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data=f"echg_yr:{phone}:-1", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.CALENDAR} Year Old: {year_label}", callback_data="noop", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.ADD}", callback_data=f"echg_yr:{phone}:+1", style=S.DEFAULT),
                ],
                [InlineKeyboardButton(
                    f"{em.MAIL} Email: {email_str} — Tap to toggle",
                    callback_data=f"echg_em:{phone}", style=S.DEFAULT,
                )],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"num_actions:{phone}", style=S.DEFAULT)],
            ]),
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{flag} {name}", callback_data=f"echg_ccpick:{cc}", style=S.DEFAULT)]
        for cc, name, flag in matches
    ]
    buttons.append([InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data=f"editnum:{phone}", style=S.DANGER)])
    await message.reply(
        f"{em.GLOBE} **Multiple matches found.** Pick one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_edit_num_set_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    try:
        price = int(text.strip())
        if price < 1:
            await message.reply(f"{em.ERROR} Price must be at least 1. Try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid price. Send a number (e.g. `50`):")
        return

    phone = state["phone"]
    auth_states.pop(user_id, None)

    session = await db.get_session(phone)
    if not session:
        await message.reply(f"{em.ERROR} Number not found.", reply_markup=back_kb("admin_panel"))
        return

    cc = session.get("country_code", "XX")
    year = session.get("account_year")
    month = session.get("account_month")
    email = session.get("email_added", False)

    await db.set_category_price(cc, year, email, price)

    flag = get_country_flag(cc)
    name = get_country_name(cc)
    year_label = format_account_year(year, month)
    email_str = "Yes" if email else "No"

    await message.reply(
        f"{em.SUCCESS} **Category price set!**\n\n"
        f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
        f"{em.CALENDAR} Year Old: **{year_label}**\n"
        f"{em.MAIL} Email: **{email_str}**\n"
        f"{em.MONEY} Price: **{price}** credits per OTP",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"editnum:{phone}", style=S.DEFAULT)],
        ]),
    )


# ── Purchase finalization ──

async def _handle_wa_add_phone(message: Message, text: str):
    user_id = message.from_user.id
    phone = text.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    if not phone[1:].isdigit() or len(phone) < 8:
        await message.reply(f"{em.ERROR} Invalid number. Send it in international format, e.g. `+1234567890`.")
        return

    if await db.get_wa_number(phone):
        auth_states.pop(user_id, None)
        await message.reply(
            f"{em.ERROR} **Already Added**\n\n`{phone}` is already in the WhatsApp store.",
            reply_markup=back_kb("wa_admin"),
        )
        return

    cc, cname, cflag = detect_country(phone)
    auth_states[user_id] = {"step": "wa_add_price", "phone": phone, "country_code": cc}
    await message.reply(
        f"{em.SMS} **Add WhatsApp Number**\n\n"
        f"{em.PHONE} `{phone}`\n"
        f"{cflag} {cname}\n\n"
        f"Now send the price in credits (e.g. `25`).",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="wa_admin", style=S.DANGER)],
        ]),
    )


async def _handle_wa_add_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states.get(user_id)
    if not state:
        return
    if not text.strip().isdigit() or int(text.strip()) <= 0:
        await message.reply(f"{em.ERROR} Send a positive whole number of credits, e.g. `25`.")
        return

    price = int(text.strip())
    phone = state["phone"]
    auth_states.pop(user_id, None)

    if not await db.add_wa_number(phone, price, state.get("country_code", "XX")):
        await message.reply(
            f"{em.ERROR} `{phone}` is already in the WhatsApp store.",
            reply_markup=back_kb("wa_admin"),
        )
        return

    cc, cname, cflag = detect_country(phone)
    await message.reply(
        f"{em.SUCCESS} **WhatsApp Number Added**\n\n"
        f"{em.PHONE} `{phone}`\n"
        f"{cflag} {cname}\n"
        f"{em.MONEY} Price: **{price}** credits\n\n"
        f"It's now on sale in the WhatsApp portal.",
        reply_markup=back_kb("wa_admin"),
    )


async def _handle_wa_set_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states.get(user_id)
    if not state:
        return
    if not text.strip().isdigit() or int(text.strip()) <= 0:
        await message.reply(f"{em.ERROR} Send a positive whole number of credits, e.g. `25`.")
        return

    price = int(text.strip())
    phone = state["phone"]
    auth_states.pop(user_id, None)

    if not await db.set_wa_price(phone, price):
        await message.reply(
            f"{em.ERROR} Could not update `{phone}` (not found, or price unchanged).",
            reply_markup=back_kb("wa_admin"),
        )
        return

    await message.reply(
        f"{em.SUCCESS} Price for `{phone}` set to **{price}** credits.",
        reply_markup=back_kb("wa_admin"),
    )


async def _send_or_edit(user_id: int, edit_msg, text, reply_markup=None):
    """Edit an existing message when given one, otherwise send a fresh message."""
    if edit_msg is not None:
        return await safe_edit(edit_msg, text, reply_markup=reply_markup)
    return await bot.send_message(user_id, text, reply_markup=reply_markup)


async def _finalize_purchase(user_id: int, phone: str, edit_msg=None, confirmed_frozen: bool = False) -> bool:
    """Connect the session, deduct the effective price and assign the number.

    Price and offer are recomputed at call time so this is safe to invoke after a
    deferred top-up payment (the balance/offer may have changed since selection).
    Returns True only when the number was successfully assigned.
    """
    user = await db.get_user(user_id)
    if not user:
        return False

    session = await db.get_session(phone)
    if not session or session.get("status") != "active":
        await _send_or_edit(user_id, edit_msg,
            f"{em.ERROR} Number `{mask_phone(phone)}` is no longer available.",
            reply_markup=back_kb("get_number"))
        return False

    existing = clients.get_request_user(phone)
    if existing and existing != user_id:
        await _send_or_edit(user_id, edit_msg,
            f"{em.OFFLINE} `{mask_phone(phone)}` was just taken by someone else.",
            reply_markup=back_kb("get_number"))
        return False

    # Atomically claim the number before charging. Two buyers hitting the same
    # number in the same tick both pass the checks above; only one wins this flip,
    # so only one deducts funds. We restore 'active' on any abort or right after
    # assignment (the in-memory active_requests gate takes over from there).
    if not await db.reserve_session(phone):
        await _send_or_edit(user_id, edit_msg,
            f"{em.OFFLINE} `{mask_phone(phone)}` was just taken by someone else.",
            reply_markup=back_kb("get_number"))
        return False

    cc = session.get("country_code", "XX")
    if not cc or cc == "XX":
        detected_cc, _, _ = detect_country(phone)
        if detected_cc != "XX":
            cc = detected_cc
    base_price = await db.get_session_price(session)
    if base_price is None:
        await db.unreserve_session(phone)
        await _send_or_edit(user_id, edit_msg,
            f"{em.ERROR} This number is not configured for sale.",
            reply_markup=back_kb("get_number"))
        return False

    # Apply any active discount offer server-side (never trust the client).
    offer = await db.get_active_offer(user_id)
    price = apply_discount(base_price, offer)
    # Spend it here, where the price is computed, not after the ~27 awaits of
    # session work below. Marking it used only at the end leaves it live for a
    # coupon redeemed mid-flight to read as `cur` and roll forward into the
    # replacement, so the discount is banked and kept. Every abort between here
    # and assignment restores it.
    offer_granted_at = offer.get("granted_at") if offer else None
    if offer:
        await db.consume_offer(user_id, offer_granted_at)
    credits = await db.get_credits(user_id)
    # A fully-covered number is free only for users who hold real credits.
    if price == 0 and credits <= 0:
        price = 1
    saved = base_price - price

    if credits < price:
        # An offer may have expired between top-up and payment, raising the price.
        await db.unreserve_session(phone)
        if offer:
            await db.restore_offer(user_id, offer_granted_at)
        await _send_or_edit(user_id, edit_msg,
            f"{em.ERROR} You need {price} credits but have {credits}. "
            f"Your top-up was added to your balance — buy more credits or pick another number.",
            reply_markup=back_kb("buy_credits"))
        return False

    await _send_or_edit(user_id, edit_msg, f"{em.LOADING} Connecting session...")

    try:
        await clients.start_session(phone, session["session_string"])
    except Exception as e:
        log.error("Failed to start session %s: %s", phone, e)
        await db.set_session_status(phone, "unlisted", str(e))
        await db.log_auth_failure(phone, str(e), kind="connect", requested_by=user_id)
        await alert(bot,
            f"{em.ALERT} **Session Connection Failed — Unlisted**\n\n"
            f"{em.USER} Requested by: `{user_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{em.ERROR} Error: `{str(e)[:200]}`"
        )
        # Notify the seller their account failed and was unlisted.
        _listing = await db.get_sell_listing_by_phone(phone)
        if _listing and _listing.get("seller_id"):
            try:
                await bot.send_message(
                    _listing["seller_id"],
                    f"{em.WARNING} **Account Unlisted — Connection Failed**\n\n"
                    f"📱 `{mask_phone(phone)}` could not be connected during a purchase attempt and has been **unlisted**."
                    f"\n\nPlease verify the account is still accessible and contact support if needed.",
                )
            except Exception:
                pass
        await _send_or_edit(user_id, edit_msg,
            f"{em.ERROR} Failed to connect `{mask_phone(phone)}`.\n\n"
            "This has been reported to the admins.",
            reply_markup=back_kb("main_menu"))
        if offer:
            await db.restore_offer(user_id, offer_granted_at)
        return False

    pwd = session.get("password", "")
    if pwd:
        await _send_or_edit(user_id, edit_msg, f"{em.LOADING} Verifying password...")
        ok, err = await clients.check_password(phone, pwd)
        if not ok:
            await clients.stop_session(phone)
            await db.set_session_status(phone, "unlisted", err)
            await db.log_auth_failure(phone, err, kind="password", requested_by=user_id)
            await alert(bot,
                f"{em.ALERT} **Password Check Failed — Unlisted**\n\n"
                f"{em.USER} Requested by: `{user_id}`\n"
                f"{em.PHONE} Number: `{phone}`\n"
                f"{em.ERROR} Error: `{err[:200]}`\n"
                f"{em.PASSWORD} Stored password may be wrong or changed."
            )
            # Notify the seller their account failed and was unlisted.
            _listing = await db.get_sell_listing_by_phone(phone)
            if _listing and _listing.get("seller_id"):
                try:
                    await bot.send_message(
                        _listing["seller_id"],
                        f"{em.WARNING} **Account Unlisted — Password Failed**\n\n"
                        f"📱 `{mask_phone(phone)}` failed 2FA verification during a purchase attempt and has been **unlisted**."
                        f"\n\nThe stored password may have been changed. Please contact support.",
                    )
                except Exception:
                    pass
            await _send_or_edit(user_id, edit_msg,
                f"{em.ERROR} Password verification failed for `{mask_phone(phone)}`.\n\n"
                "This has been reported to the admins.",
                reply_markup=back_kb("main_menu"))
            if offer:
                await db.restore_offer(user_id, offer_granted_at)
            return False

    is_frozen = clients.is_account_frozen(phone)
    if is_frozen and (price > 10 or base_price > 10) and not confirmed_frozen:
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{em.SUCCESS} Yes, Continue", callback_data=f"confirm_frozen:{phone}", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.CANCELLED} Cancel", callback_data=f"cancel_frozen:{phone}", style=S.DANGER),
            ]
        ])
        await _send_or_edit(user_id, edit_msg,
            f"{em.WARNING} **This account may be frozen.**\n\n"
            f"📱 Number: `{phone}`\n"
            f"{em.MONEY} Price: **{price}** credits\n"
            f"Status: **frozen**\n\n"
            f"Are you sure to continue?",
            reply_markup=confirm_kb
        )
        # Must restore: tapping "Yes, Continue" re-enters this function, which
        # re-reads the offer. Left used, the retry would be priced without it.
        if offer:
            await db.restore_offer(user_id, offer_granted_at)
        return False

    credits_deducted, balance_deducted = 0, 0
    if price > 0:
        ok, credits_deducted, balance_deducted = await db.deduct_funds_for_purchase(user_id, price)
        if not ok:
            await clients.stop_session(phone)
            await db.unreserve_session(phone)
            if offer:
                await db.restore_offer(user_id, offer_granted_at)
            await _send_or_edit(user_id, edit_msg,
                f"{em.ERROR} Could not deduct funds. Please try again or contact support.",
                reply_markup=back_kb("main_menu"))
            return False
        log.info("Deducted %d credits and %d balance from user %d on selection", credits_deducted, balance_deducted, user_id)

    order_id = db.new_order_id()
    clients.assign_number(phone, user_id, OTP_TIMEOUT, price, credits_deducted=credits_deducted, balance_deducted=balance_deducted, order_id=order_id, offer_granted_at=offer_granted_at)
    # Assignment done — the in-memory active_requests gate now blocks other buyers,
    # so return the DB status to 'active' (release/timeout/sold paths expect it).
    await db.unreserve_session(phone)

    uname = user.get("username") or user.get("first_name") or str(user_id)
    flag = get_country_flag(cc)
    name = get_country_name(cc)
    credits, balance, total_funds = await db.get_total_funds(user_id)
    admin_price_line = f"{em.MONEY} Price: **{price}** credits (paid)\n"
    if saved > 0:
        paid_display = "**FREE** (0 paid)" if price == 0 else f"**{price}** credits paid"
        admin_price_line = (
            f"{em.MONEY} Original price: **{base_price}** credits\n"
            f"{em.GIFT} Offer discount: **{saved}** credits\n"
            f"{em.CREDIT} Actual credits used: {paid_display}\n"
        )
    status_str = "frozen" if is_frozen else "normal"
    # Defer this admin alert until an OTP is actually forwarded — stash the
    # text on the active request so clients.py can send it at that point.
    purchase_alert = (
        f"{em.PHONE} **Number Purchased**\n\n"
        f"{em.RECEIPT} Order ID: `{order_id}`\n"
        f"{em.USER} User: `{user_id}` (@{uname})\n"
        f"{em.PHONE} Number: `{phone}`\n"
        f"{flag} Country: {name}\n"
        f"Status: **{status_str}**\n"
        f"{admin_price_line}"
        f"{em.MONEY} Remaining funds: **{total_funds}** ({credits} credits, {balance} withdrawable)"
    )
    req_info = clients.active_requests.get(phone)
    if req_info is not None:
        req_info["purchase_alert"] = purchase_alert

    # Send immediate alert to CHAT_ID upon assignment
    assign_alert = (
        f"{em.PHONE} **Number Assigned**\n\n"
        f"{em.RECEIPT} Order ID: `{order_id}`\n"
        f"{em.USER} User: `{user_id}` (@{uname})\n"
        f"{em.PHONE} Number: `{phone}`\n"
        f"{flag} Country: {name}\n"
        f"Status: **{status_str}**\n"
        f"{admin_price_line}"
        f"{em.MONEY} Remaining funds: **{total_funds}** ({credits} credits, {balance} withdrawable)"
    )
    await alert(bot, assign_alert)

    credit_line = f"\n{em.CREDIT} Credits: {credits}\n{em.MONEY} Withdrawable Balance: {balance}"
    acc_year = session.get("account_year")
    acc_month = session.get("account_month")
    age_line = f"\n{em.CALENDAR} Year Old: ~{format_account_year(acc_year, acc_month)}" if acc_year else ""
    email_added = session.get("email_added", False)
    email_line = f"\n{em.MAIL} Email Added: {'Yes' if email_added else 'No'}"
    status_line = f"\nStatus: {status_str}"
    support = " | ".join(SUPPORT_HANDLES)
    if saved > 0:
        price_display = "**FREE** 🎉" if price == 0 else f"**{price}** credits (deducted)"
        price_line = (
            f"{em.MONEY} Price: {price_display}\n"
            f"{em.GIFT} Offer applied: **{saved} credits off** (was {base_price}) — you saved **{saved}** credits\n"
        )
    else:
        price_line = f"{em.MONEY} Price: **{price}** credits (deducted)\n"
    await _send_or_edit(user_id, edit_msg,
        f"{em.SUCCESS} **Account purchased!**\n\n"
        f"{em.RECEIPT} Order ID: `{order_id}`\n"
        f"{flag} {name}\n"
        f"{em.PHONE} `{phone}`\n"
        f"{price_line}"
        f"{em.TIMER} Login window: {OTP_TIMEOUT // 60} min{age_line}{email_line}{status_line}{credit_line}\n\n"
        "The login OTP for this account will be forwarded to you here.\n\n"
        "ℹ️ Your account will be with us for 24 hours and after that you can log us out.\n"
        "In this time, you can get OTP again anytime under the History section.\n\n"
        f"{em.WARNING} On manual release, your credits will be locked for 1 hour.\n\n"
        f"{em.WARNING} Issues logging in? Contact support:\n{support}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.UNLOCK} Release Account", callback_data=f"release:{phone}", style=S.DANGER)],
        ]),
    )
    try:
        await bot.send_message(
            user_id,
            "⭐ **Rate Your Experience**\n\n"
            "How would you rate your experience with our bot? Please tap a rating (1-5) below:",
            reply_markup=_feedback_kb(),
        )
    except Exception as e:
        log.warning("Failed to send feedback prompt after purchase to user %d: %s", user_id, e)

    return True


async def _seller_login(seller_id: int, phone: str, edit_msg=None) -> bool:
    """Log a seller into their OWN listed account for free (no charge, no sale).

    Connects the session, verifies the stored 2FA password, and assigns the number
    to the seller with no_sale=True so the OTP delivery never marks it sold or pays
    them out. The listing stays active and available for real buyers afterwards.
    """
    listing = await db.get_sell_listing_by_phone(phone)
    if not listing or listing.get("seller_id") != seller_id:
        await _send_or_edit(seller_id, edit_msg,
            f"{em.ERROR} That account isn't one of your listings.",
            reply_markup=back_kb("my_accounts"))
        return False

    if listing.get("status") != "active":
        await _send_or_edit(seller_id, edit_msg,
            f"{em.ERROR} `{mask_phone(phone)}` isn't available to log into "
            f"(status: {listing.get('status')}).",
            reply_markup=back_kb("my_accounts"))
        return False

    session = await db.get_session(phone)
    if not session or session.get("status") != "active":
        await _send_or_edit(seller_id, edit_msg,
            f"{em.ERROR} `{mask_phone(phone)}` is not in active inventory.",
            reply_markup=back_kb("my_accounts"))
        return False

    existing = clients.get_request_user(phone)
    if existing and existing != seller_id:
        await _send_or_edit(seller_id, edit_msg,
            f"{em.OFFLINE} `{mask_phone(phone)}` is currently being purchased by a buyer. Try again later.",
            reply_markup=back_kb("my_accounts"))
        return False

    await _send_or_edit(seller_id, edit_msg, f"{em.LOADING} Connecting session...")

    try:
        await clients.start_session(phone, session["session_string"])
    except Exception as e:
        log.error("Seller login: failed to start session %s: %s", phone, e)
        await _send_or_edit(seller_id, edit_msg,
            f"{em.ERROR} Failed to connect `{mask_phone(phone)}`. Please try again.",
            reply_markup=back_kb("my_accounts"))
        return False

    pwd = session.get("password", "")
    if pwd:
        ok, _err = await clients.check_password(phone, pwd)
        if not ok:
            await clients.stop_session(phone)
            await _send_or_edit(seller_id, edit_msg,
                f"{em.ERROR} Could not verify the stored password for `{mask_phone(phone)}`.",
                reply_markup=back_kb("my_accounts"))
            return False

    clients.assign_number(phone, seller_id, OTP_TIMEOUT, 0, no_sale=True)

    cc = session.get("country_code", "XX")
    flag = get_country_flag(cc)
    name = get_country_name(cc)
    pwd_line = f"\n{em.PASSWORD} 2FA Password: `{pwd}`" if pwd else ""
    await _send_or_edit(seller_id, edit_msg,
        f"{em.SUCCESS} **Logging into your account**\n\n"
        f"{flag} {name}\n"
        f"{em.PHONE} `{phone}`{pwd_line}\n"
        f"{em.TIMER} Login window: {OTP_TIMEOUT // 60} min\n\n"
        f"Request the login code on Telegram now — the OTP will be forwarded to you here.\n"
        f"This is **free** and does **not** sell your account; the listing stays active.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.UNLOCK} Log Out", callback_data=f"release:{phone}", style=S.DANGER)],
        ]),
    )
    return True


# ── Payment helpers ──

async def award_razorpay_payment(user_id: int, qr_id: str, plan_key: str,
                                 assign_phone: str = None, qr_msg=None) -> bool:
    """Credit a confirmed Razorpay payment exactly once and notify the user.

    When ``assign_phone`` is set (a deferred top-up for a specific number), the
    number is assigned automatically after crediting. Returns True if this call
    was the one that flipped the pending payment to done.
    """
    # Derive the plan from the STORED pending payment, never the caller-supplied
    # plan_key — the callback data that carries plan_key is client-controlled, so
    # the credited amount must come from what we persisted at QR-creation time.
    pending = await db.get_pending_payment(qr_id)
    authoritative_key = pending.get("plan_key") if pending else plan_key
    plan = get_credit_plan(authoritative_key)
    if not plan:
        return False
    plan_key = authoritative_key
    # Atomic flip guarantees credits are granted once even if the live poller and
    # the restart-recovery processor both observe the payment.
    if not await db.mark_pending_payment_done(qr_id):
        return False

    await db.add_credits(user_id, plan["credits"])
    await db.save_payment(user_id, "razorpay", plan_key, plan["amount_inr"] / 100, "INR", qr_id, credits=plan["credits"])
    await _check_referral_reward(user_id, plan["credits"])
    new_balance = await db.get_credits(user_id)
    buyer = await db.get_user(user_id)
    buyer_name = (buyer.get("first_name") or buyer.get("username") or str(user_id)) if buyer else str(user_id)
    await alert(bot,
        f"{em.CREDIT} **Credits Purchased (Razorpay)**\n\n"
        f"{em.USER} User: `{user_id}` ({buyer_name})\n"
        f"{em.GIFT} Credits: +{plan['credits']}\n"
        f"{em.DOLLAR} Amount: ₹{plan['amount_inr'] // 100}\n"
        f"{em.MONEY} New balance: {new_balance}"
        + (f"\n{em.PHONE} Auto-assigning: `{assign_phone}`" if assign_phone else "")
    )

    if qr_msg is not None:
        try:
            await qr_msg.delete()
        except Exception:
            pass

    if assign_phone:
        # Confirm the top-up, then run the full purchase/assignment flow.
        await bot.send_message(
            user_id,
            f"{em.SUCCESS} **Payment received!**\n\n"
            f"{em.GIFT} +{plan['credits']} credits added\n"
            f"{em.MONEY} New balance: **{new_balance}**\n\n"
            f"{em.LOADING} Assigning `{mask_phone(assign_phone)}`...",
        )
        await _finalize_purchase(user_id, assign_phone, edit_msg=None)
    else:
        await bot.send_message(
            user_id,
            f"{em.SUCCESS} **Payment received!**\n\n"
            f"{em.GIFT} +{plan['credits']} credits added\n"
            f"{em.MONEY} New balance: **{new_balance}**",
            reply_markup=back_kb("main_menu"),
        )
    return True


async def _razorpay_poller(user_id: int, qr_id: str, plan_key: str, qr_msg, assign_phone: str = None):
    import time as _time
    plan = get_credit_plan(plan_key)
    if not plan:
        return
    start = _time.time()
    while _time.time() - start < 900:
        await asyncio.sleep(15)
        status = await asyncio.to_thread(
            payments.check_razorpay_payment, qr_id, plan["amount_inr"],
        )
        if status == "paid":
            await award_razorpay_payment(
                user_id, qr_id, plan_key, assign_phone=assign_phone, qr_msg=qr_msg,
            )
            return
        if status == "expired":
            break

    await db.mark_pending_payment_expired(qr_id)
    try:
        await qr_msg.delete()
    except Exception:
        pass
    await bot.send_message(
        user_id,
        f"{em.WARNING} **Payment QR expired** (15-minute limit).\n\n"
        f"No charges were made. Tap below to generate a new one.",
        reply_markup=back_kb("buy_credits"),
    )


async def _handle_tx_hash(message: Message, text: str, pstate: dict):
    user_id = message.from_user.id

    if text.lower() == "cancel":
        pay_states.pop(user_id, None)
        await message.reply(
            f"{em.ERROR} Crypto payment cancelled. No charges were made.",
            reply_markup=back_kb("main_menu"),
        )
        return

    tx_hash = text.strip()
    if not ((tx_hash.startswith("0x") and len(tx_hash) == 66) or len(tx_hash) == 64):
        await message.reply(
            f"{em.ERROR} Invalid TX hash format.\n\n"
            "Send the 64-character transaction hash from your wallet or exchange history.",
        )
        return

    if await db.is_tx_used(tx_hash):
        pay_states.pop(user_id, None)
        support = " | ".join(SUPPORT_HANDLES)
        await message.reply(
            f"{em.ERROR} This TX hash has already been used.\n\n"
            f"If you believe this is a mistake, contact support:\n{support}",
        )
        return

    status_msg = await message.reply(f"{em.LOADING} Verifying deposit on Binance...")

    plan_key = pstate["plan_key"]
    plan = get_crypto_plan(plan_key)
    if not plan:
        pay_states.pop(user_id, None)
        await safe_edit(status_msg, f"{em.ERROR} Invalid plan.", reply_markup=back_kb("main_menu"))
        return

    ok, reason = await payments.verify_binance_deposit(tx_hash, "USDT", pstate["amount_usdt"])

    if not ok:
        await safe_edit(status_msg,
            f"{em.ERROR} **Verification failed:** {reason}\n\n"
            "If the transaction is recent, wait for network confirmations and try again.\n"
            "You can resend the same TX hash.",
        )
        return

    # Atomically claim the tx BEFORE crediting — a concurrent/duplicate send of
    # the same hash loses the claim and must not be credited again.
    if not await db.claim_tx(tx_hash, user_id, plan_key):
        pay_states.pop(user_id, None)
        support = " | ".join(SUPPORT_HANDLES)
        await safe_edit(status_msg,
            f"{em.ERROR} This TX hash has already been used.\n\n"
            f"If you believe this is a mistake, contact support:\n{support}",
        )
        return

    pay_states.pop(user_id, None)
    await db.add_credits(user_id, plan["credits"])
    await db.save_payment(user_id, "crypto_usdt", plan_key, pstate["amount_usdt"], "USDT", tx_hash, credits=plan["credits"])
    await _check_referral_reward(user_id, plan["credits"])
    new_balance = await db.get_credits(user_id)
    buyer = await db.get_user(user_id)
    buyer_name = (buyer.get("first_name") or buyer.get("username") or str(user_id)) if buyer else str(user_id)

    await alert(bot,
        f"{em.COIN} **Credits Purchased (Crypto)**\n\n"
        f"{em.USER} User: `{user_id}` ({buyer_name})\n"
        f"{em.GIFT} Credits: +{plan['credits']}\n"
        f"{em.DOLLAR} Amount: {pstate['amount_usdt']} USDT\n"
        f"{em.GLOBE} Network: {pstate['network']}\n"
        f"{em.LINK} TX: `{tx_hash[:16]}...`\n"
        f"{em.MONEY} New balance: {new_balance}"
    )

    await safe_edit(status_msg,
        f"{em.SUCCESS} **Deposit confirmed!**\n\n"
        f"{em.GIFT} +{plan['credits']} credits added\n"
        f"{em.MONEY} New balance: **{new_balance}**",
        reply_markup=back_kb("main_menu"),
    )


async def _handle_rz_custom_amount(message: Message, text: str):
    user_id = message.from_user.id
    try:
        credits = int(text.strip())
        if credits < 10:
            await message.reply(f"{em.ERROR} Minimum amount is 10 credits. Please try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid number. Please enter a valid integer (minimum 10):")
        return

    auth_states.pop(user_id, None)

    plan_key = f"custom_{credits}"
    plan = get_credit_plan(plan_key)

    status_msg = await message.reply(f"{em.LOADING} Generating QR code...")
    qr = await asyncio.to_thread(
        payments.create_razorpay_qr, plan["label"], plan["amount_inr"], user_id,
    )
    if not qr:
        await safe_edit(status_msg,
            f"{em.ERROR} Payment gateway error. Try later.",
            reply_markup=back_kb("buy_credits"),
        )
        return

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{em.SUCCESS} I've Paid", callback_data=f"rz_check:{qr['id']}:{plan_key}", style=S.SUCCESS)],
        [InlineKeyboardButton(f"{em.ERROR} Cancel", callback_data="buy_credits", style=S.DANGER)],
    ])

    try:
        await status_msg.delete()
    except Exception:
        pass

    qr_msg = await safe_send_photo(
        user_id,
        photo_url=qr["image_url"],
        caption=(
            f"{em.PHONE} **Scan to pay ₹{plan['amount_inr'] // 100}**\n"
            f"{em.GIFT} You'll receive **{plan['credits']} credits**\n\n"
            f"{em.TIMER} Valid for 15 minutes."
        ),
        reply_markup=buttons,
    )

    await db.save_pending_payment(
        user_id, qr["id"], plan_key, plan["amount_inr"],
        qr_msg.chat.id, qr_msg.id,
    )

    asyncio.create_task(_razorpay_poller(
        user_id, qr["id"], plan_key, qr_msg,
    ))


async def _handle_cr_custom_amount(message: Message, text: str):
    user_id = message.from_user.id
    try:
        credits = int(text.strip())
        if credits < 10:
            await message.reply(f"{em.ERROR} Minimum amount is 10 credits. Please try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid number. Please enter a valid integer (minimum 10):")
        return

    auth_states.pop(user_id, None)

    plan_key = f"custom_{credits}"
    plan = get_crypto_plan(plan_key)

    buttons = [
        [InlineKeyboardButton("BSC (BEP20)", callback_data=f"cr_addr:BSC:{plan_key}", style=S.DEFAULT)],
        [InlineKeyboardButton("TRC20 (TRON)", callback_data=f"cr_addr:TRX:{plan_key}", style=S.DEFAULT)],
        [InlineKeyboardButton("ERC20 (Ethereum)", callback_data=f"cr_addr:ETH:{plan_key}", style=S.DEFAULT)],
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data="cr_plans", style=S.DEFAULT)],
    ]
    await message.reply(
        f"{em.GLOBE} **Select network for USDT deposit ({plan['amount_usdt']} USDT for {credits} credits):**",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_stars_custom_amount(message: Message, text: str):
    user_id = message.from_user.id
    try:
        credits = int(text.strip())
        if credits < 10:
            await message.reply(f"{em.ERROR} Minimum amount is 10 credits. Please try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid number. Please enter a valid integer (minimum 10):")
        return

    auth_states.pop(user_id, None)

    plan_key = f"custom_{credits}"
    plan = get_stars_plan(plan_key)
    if not plan:
        await message.reply(f"{em.ERROR} Error generating plan. Try again.")
        return

    await message._client.send_invoice(
        chat_id=user_id,
        title=f"{plan['credits']} Credits",
        description=f"Top-up {plan['credits']} Credits in OTP Bot using Telegram Stars",
        payload=f"stars:{user_id}:{plan_key}:{plan['credits']}:{int(time.time())}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{plan['credits']} Credits", amount=plan['stars'])],
    )



async def _handle_set_new_category_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    try:
        price = int(text.strip())
        if price <= 0:
            await message.reply(f"{em.ERROR} Price must be a positive integer. Please try again:")
            return
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid price. Please enter a positive integer:")
        return

    cc = state["pending_cc"]
    year = state.get("account_year")
    month = state.get("account_month")
    email_added = state.get("email_added", False)

    await db.set_category_price(cc, year, email_added, price)
    activated = await db.check_and_activate_pending_listings(cc, year, email_added)

    phone = state["phone"]
    flag = get_country_flag(cc)
    name = get_country_name(cc)

    await db.save_session(phone, state["session_string"], user_id,
                          password=state.get("password", ""), country_code=cc,
                          account_id=state.get("account_id"), account_year=year,
                          account_month=month, email_added=email_added)
    await db.set_session_account_info(phone, state.get("account_id"), year, email_added, account_month=month)
    auth_states.pop(user_id, None)

    await alert(bot,
        f"{em.ADD} **Number Added**\n\n"
        f"{em.SHIELD} Admin: `{user_id}`\n"
        f"{em.PHONE} Number: `{phone}`\n"
        f"{flag} Country: {name}\n"
        f"{em.CALENDAR} Year Old: **{format_account_year(year, month)}**\n"
        f"{em.MAIL} Email Added: **{'Yes' if email_added else 'No'}**\n"
        f"{em.MONEY} Price: {price} credits"
    )

    act_str = f"\n⚡ **{len(activated)} pending seller account(s) activated!**" if activated else ""

    await message.reply(
        f"{em.SUCCESS} **Category price set and number added successfully!**\n\n"
        f"{em.PHONE} `{phone}` — {flag} {name}\n"
        f"{em.MONEY} Price: **{price}** credits per OTP{act_str}",
        reply_markup=main_menu_kb(True),
    )


# ── Seller Account Auth Handlers ──

async def _handle_sell_phone(message: Message, phone: str):
    user_id = message.from_user.id
    if not phone.startswith("+"):
        phone = "+" + phone

    existing = await db.get_session(phone)
    existing_listing = await db.get_active_listing_by_phone(phone)
    if existing or existing_listing:
        sell_states.pop(user_id, None)
        await message.reply(
            f"{em.ERROR} **Account Already Added!**\n\n"
            f"The phone number `{phone}` is already registered in the store database.",
            reply_markup=back_kb("sell_account"),
        )
        return

    if await db.is_seller_phone_blacklisted(phone):
        sell_states.pop(user_id, None)
        await message.reply(
            f"{em.BLOCKED} **Account Blacklisted!**\n\n"
            f"The number `{phone}` was previously reclaimed by its seller after an OTP was retrieved.\n"
            f"This number **cannot be re-listed** for sale.",
            reply_markup=back_kb("sell_account"),
        )
        return

    cc, cname, cflag = detect_country(phone)
    status_msg = await message.reply(f"{em.LOADING} Sending code to `{phone}` ({cflag} {cname})...")

    try:
        client = Client(
            name=f"sell_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            device_model="OTP BOT",
            app_version="OTP BOT 1.0",
            in_memory=True,
        )
        await client.connect()
        sent_code = await client.send_code(phone)
        sell_states[user_id] = {
            "step": "sell_code",
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "country_code": cc,
        }
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code sent to `{phone}` ({cflag} {cname})\n\n"
            "Enter the verification code received on Telegram:\n\n"
            f"{em.FAQ} Add spaces or dots between digits (e.g. `1 2 3 4 5`).",
        )
    except PhoneNumberInvalid:
        sell_states.pop(user_id, None)
        await safe_edit(status_msg, f"{em.ERROR} Invalid phone number format.", reply_markup=back_kb("sell_account"))
    except FloodWait as e:
        sell_states.pop(user_id, None)
        await safe_edit(status_msg, f"{em.WARNING} FloodWait — try again in {e.value} seconds.", reply_markup=back_kb("sell_account"))
    except Exception as e:
        sell_states.pop(user_id, None)
        await safe_edit(status_msg, f"{em.ERROR} Error: `{e}`", reply_markup=back_kb("sell_account"))


async def _handle_sell_code(message: Message, code: str):
    user_id = message.from_user.id
    state = sell_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    clean_code = code.replace(" ", "").replace(".", "").replace("-", "")
    status_msg = await message.reply(f"{em.LOADING} Verifying code...")

    try:
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=state["phone_code_hash"],
            phone_code=clean_code,
        )
        acc_id, acc_year, acc_month, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        await _complete_sell_submission(user_id, status_msg, phone, session_string, "", state["country_code"], acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info, acc_month)
    except SessionPasswordNeeded:
        sell_states[user_id]["step"] = "sell_password"
        await safe_edit(status_msg, f"{em.PASSWORD} 2FA is enabled on this account.\nEnter the 2FA password:")
    except PhoneCodeInvalid:
        await safe_edit(status_msg, f"{em.ERROR} Invalid code. Try again (add spaces, e.g. `1 2 3 4 5`):")
    except PhoneCodeExpired:
        sell_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg, f"{em.ERROR} Code expired. Please start over.", reply_markup=back_kb("sell_account"))
    except Exception as e:
        sell_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg, f"{em.ERROR} Error: `{e}`", reply_markup=back_kb("sell_account"))


async def _handle_sell_password(message: Message, password: str):
    user_id = message.from_user.id
    state = sell_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    status_msg = await message.reply(f"{em.LOADING} Checking password...")

    try:
        await client.check_password(password)
        acc_id, acc_year, acc_month, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        await _complete_sell_submission(user_id, status_msg, phone, session_string, password, state["country_code"], acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info, acc_month)
    except PasswordHashInvalid:
        await safe_edit(status_msg, f"{em.ERROR} Wrong password. Try again:")
    except Exception as e:
        sell_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg, f"{em.ERROR} Error: `{e}`", reply_markup=back_kb("sell_account"))


async def _complete_sell_submission(seller_id: int, status_msg, phone: str, session_string: str, password: str, cc: str, acc_id: int | None, acc_year: int | None, has_email: bool, is_peer_flood: bool, sess_cnt: int = 1, sess_info: str = "", acc_month: int | None = None):
    sell_states.pop(seller_id, None)

    if acc_id is not None and acc_id == seller_id:
        await safe_edit(status_msg,
            f"{em.ERROR} **Selling Request Cancelled!**\n\n"
            f"You're trying to sell the **same Telegram account** you're using this bot with.\n\n"
            f"Selling it would log you out and you'd lose access here. "
            f"Please submit a **different** account.",
            reply_markup=back_kb("sell_account"),
        )
        return

    if is_peer_flood:
        await db.blacklist_seller_phone(phone, seller_id, reason="peer_flood")
        await safe_edit(status_msg,
            f"{em.ERROR} **Selling Request Cancelled!**\n\n"
            f"⚠️ **Your account is limited/restricted by Telegram.**\n\n"
            f"`[400 PEER_FLOOD] - The current account is limited, you cannot execute this action, check @spambot for more info.`\n\n"
            f"This number has been **blacklisted** and cannot be re-submitted for sale.\n"
            f"Please check `@spambot` on Telegram to resolve restrictions.",
            reply_markup=back_kb("sell_account"),
        )
        return

    if sess_cnt > 1:
        # Stash the already-gathered submission data so the seller can just remove
        # their other devices and re-check, instead of redoing the whole login flow.
        sell_recheck_states[seller_id] = {
            "phone": phone,
            "session_string": session_string,
            "password": password,
            "cc": cc,
            "acc_id": acc_id,
            "acc_year": acc_year,
            "acc_month": acc_month,
            "has_email": has_email,
        }
        await safe_edit(status_msg,
            f"{em.ERROR} **Selling Request Cancelled: Multiple Active Sessions!**\n\n"
            f"⚠️ Please go to **Telegram Settings ➔ Devices** on your Telegram app, remove **ALL** active sessions (including yourself), and leave **ONLY** the session named `OTP BOT`.\n\n"
            f"{sess_info}\n"
            f"Once you've removed the other sessions, tap **Re-check Sessions** below.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.SEARCH} Re-check Sessions", callback_data="sell_recheck", style=S.PRIMARY)],
                [InlineKeyboardButton(f"{em.BACK} Back", callback_data="sell_account", style=S.DEFAULT)],
            ]),
        )
        return

    flag = get_country_flag(cc)
    cname = get_country_name(cc)
    year_label = format_account_year(acc_year, acc_month)

    # Guard against duplicate submissions BEFORE securing — re-rotating the
    # password on an already-listed account would invalidate the stored one.
    if await db.get_active_listing_by_phone(phone):
        await safe_edit(status_msg,
            f"{em.ERROR} **Already Submitted!**\n\n"
            f"`{phone}` is already listed for sale and awaiting processing.\n"
            f"You can't submit the same account twice.",
            reply_markup=back_kb("sell_account"),
        )
        return

    # ── Secure the purchased account: rotate 2FA password and (if a login email
    # already exists) switch it to ours, so the seller can no longer recover it.
    # The rotated password is what we store — never the seller's original.
    await safe_edit(status_msg, f"{em.LOADING} Securing `{phone}` (rotating credentials)...")
    secured = await clients.secure_purchased_account(phone, session_string, password)
    password_changed = bool(secured.get("ok") and secured.get("new_password"))
    stored_password = secured["new_password"] if password_changed else password

    # Mask the password in the admin channel: show first 2 + last 2 chars only.
    masked_pwd = mask_secret(stored_password) if stored_password else "—"

    await alert(bot,
        f"{em.SHIELD} **Account Securing — `{phone}`**\n\n"
        f"{em.PASSWORD} Password changed: **{'Yes' if password_changed else 'No'}**\n"
        f"{em.PASSWORD} New password: `{masked_pwd}`\n"
        f"{em.MAIL} Login email switched: **{'Yes' if secured.get('email_changed') else 'No'}**"
        + (f"\n{em.WARNING} Securing error: `{secured['error']}`" if secured.get("error") else "")
    )

    cat_price = await db.get_category_price(cc, acc_year, has_email)

    listing = await db.create_sell_listing(
        phone, seller_id, session_string, stored_password, cc, acc_id, acc_year, has_email, account_month=acc_month,
    )

    if listing is None:
        # Lost a concurrent race against another submission of the same phone.
        await safe_edit(status_msg,
            f"{em.ERROR} **Already Submitted!**\n\n"
            f"`{phone}` is already listed for sale and awaiting processing.",
            reply_markup=back_kb("sell_account"),
        )
        return

    if cat_price is not None:
        updated_listing = await db.activate_sell_listing(listing["_id"], cat_price)
        await db.save_session(phone, session_string, seller_id, password=stored_password, country_code=cc, account_id=acc_id, account_year=acc_year, account_month=acc_month, email_added=has_email)

        seller_payout = updated_listing["payout_credits"] if updated_listing else int(cat_price * SELLER_PAYOUT_PERCENT / 100)

        await alert(bot,
            f"{em.ADD} **Seller Account Listed**\n\n"
            f"{em.USER} Seller: `{seller_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {cname}\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n"
            f"{em.MONEY} Category Price: {cat_price} credits\n"
            f"{em.DOLLAR} Payout on sale: {seller_payout} credits"
        )

        await safe_edit(status_msg,
            f"{em.SUCCESS} **Account Listed for Sale!**\n\n"
            f"{flag} `{phone}` ({cname})\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n"
            f"{em.MONEY} Category Price: **{cat_price} credits**\n"
            f"{em.DOLLAR} You'll earn **{seller_payout} credits** ({SELLER_PAYOUT_PERCENT}%) **when a buyer purchases it**\n\n"
            f"Your account is now live in the store. You can still log into it any time from "
            f"**Sell Account ➔ Login to My Accounts** until it sells.",
            reply_markup=back_kb("sell_account"),
        )
    else:
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.MONEY} Set Category Price", callback_data=f"set_pending_cat_price:{listing['_id']}", style=S.PRIMARY)]
        ])
        await alert(bot,
            f"{em.ALERT} **New Seller Submission — Pending Category Price**\n\n"
            f"{em.USER} Seller: `{seller_id}`\n"
            f"{em.PHONE} Number: `{mask_phone(phone)}`\n"
            f"{flag} Country: {cname} ({cc})\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{'Yes' if has_email else 'No'}**\n\n"
            f"Tap **Set Category Price** below to activate this account in the store.",
            reply_markup=btn,
        )

        await safe_edit(status_msg,
            f"{em.SUCCESS} **Account Submitted!**\n\n"
            f"{flag} `{phone}` ({cname})\n"
            f"{em.CALENDAR} Year Old: **{year_label}**\n\n"
            f"⏳ Category price for {flag} {cname} ({year_label}) is being configured by admins.\n"
            f"Your account will automatically be listed once price setup is complete!",
            reply_markup=back_kb("sell_account"),
        )


async def _handle_sell_withdrawal_details(message: Message, text: str):
    user_id = message.from_user.id
    state = sell_states.pop(user_id, None)
    if not state:
        return

    method = state["method"]
    amount = state["amount"]
    details = text.strip()

    req = await db.create_withdrawal_request(user_id, amount, method, details)
    if not req:
        await message.reply(f"{em.ERROR} Insufficient withdrawable balance.", reply_markup=back_kb("sell_account"))
        return

    try:
        await bot.send_message(
            MODERATOR_ID,
            f"{em.ALERT} **New Seller Withdrawal Request**\n\n"
            f"{em.USER} Seller: `{user_id}`\n"
            f"{em.MONEY} Amount: **{amount} credits**\n"
            f"{em.NOTE} Method: **{method.upper()}**\n"
            f"{em.LINK} Details: `{details}`"
        )
    except Exception as e:
        log.error("Failed to notify moderator %d: %s", MODERATOR_ID, e)

    await message.reply(
        f"{em.SUCCESS} **Withdrawal Request Submitted!**\n\n"
        f"{em.MONEY} Amount: **{amount} credits**\n"
        f"{em.NOTE} Method: **{method.upper()}**\n"
        f"{em.LINK} Details: `{details}`\n\n"
        f"Admins will process your payment shortly.",
        reply_markup=main_menu_kb(False),
    )


async def _handle_admin_withdrawal_amount(message: Message, text: str):
    user_id = message.from_user.id
    state = admin_withdraw_states.get(user_id)
    if not state:
        return

    try:
        amount = float(text.strip())
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.reply(f"{em.ERROR} Invalid amount. Please send a valid positive number.")
        return

    avail_info = await db.get_admin_withdrawal_available()
    avail = avail_info["available"]
    if amount > avail:
        await message.reply(
            f"{em.ERROR} Amount exceeds available pool (₹{avail:,.2f}). Please enter a smaller amount:"
        )
        return

    state["amount"] = amount
    state["step"] = "admin_withdrawal_details"

    mth = state["method"]
    prompt = "UPI ID (e.g. `user@upi`)" if mth == "upi" else "Crypto USDT Wallet Address (TRC20/BEP20)"
    await message.reply(
        f"{em.NOTE} **Enter Payout Details**\n\n"
        f"Selected Amount: **₹{amount:,.2f}** ({mth.upper()})\n\n"
        f"Send your {prompt}:",
        reply_markup=back_kb("admin_panel"),
    )


async def _handle_admin_withdrawal_details(message: Message, text: str):
    user_id = message.from_user.id
    state = admin_withdraw_states.pop(user_id, None)
    if not state:
        return

    method = state["method"]
    amount = state["amount"]
    details = text.strip()

    req = await db.create_admin_withdrawal_request(user_id, amount, method, details)
    if not req:
        await message.reply(f"{em.ERROR} Withdrawal request failed or exceeds available pool.", reply_markup=back_kb("admin_panel"))
        return

    try:
        await bot.send_message(
            MODERATOR_ID,
            f"{em.ALERT} **New Admin Withdrawal Request**\n\n"
            f"{em.USER} Admin: `{user_id}`\n"
            f"{em.MONEY} Amount: **₹{amount:,.2f}**\n"
            f"{em.NOTE} Method: **{method.upper()}**\n"
            f"{em.LINK} Details: `{details}`"
        )
    except Exception as e:
        log.error("Failed to notify moderator %d: %s", MODERATOR_ID, e)

    await message.reply(
        f"{em.SUCCESS} **Admin Withdrawal Request Submitted!**\n\n"
        f"{em.MONEY} Amount: **₹{amount:,.2f}**\n"
        f"{em.NOTE} Method: **{method.upper()}**\n"
        f"{em.LINK} Details: `{details}`\n\n"
        f"Moderator will process your payout shortly.",
        reply_markup=admin_kb(await db.is_moderator(user_id)),
    )

