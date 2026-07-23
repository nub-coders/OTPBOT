import asyncio
import logging
from pyrogram import Client, filters, enums

# Shorthand for button style
S = enums.ButtonStyle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
from config import API_ID, API_HASH, BOT_TOKEN, OTP_TIMEOUT, CREDIT_PLANS, CRYPTO_PLANS, SUPPORT_HANDLES, CHAT_ID, ADMIN_IDS, UPDATES_CHANNEL, USDT_TO_INR, TURNSTILE_SITE_KEY, VERIFY_URL, REFERRAL_BONUS, REFERRAL_VERIFY_BONUS, ENABLE_VERIFICATION, OFFER_MIN_CREDITS, OFFER_MAX_CREDITS, OFFER_MIN_HOURS, OFFER_MAX_HOURS, SELLER_PAYOUT_PERCENT
import database as db
import clients
import payments
import verification
from utils import detect_country, get_country_flag, get_country_name, search_country, estimate_account_year, mask_phone, mask_secret, extract_year_from_reg_month, get_active_sessions_info, format_timestamp
import custom_emojis as em
em.patch_pyrogram_for_custom_emojis()

log = logging.getLogger(__name__)

bot: Client = None
auth_states: dict[int, dict] = {}
pay_states: dict[int, dict] = {}
sell_states: dict[int, dict] = {}   # tracks user-side sell-account auth flow
sell_recheck_states: dict[int, dict] = {}  # holds submission data for a pending session re-check


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


def _random_discount_credits() -> int:
    """Pick a random flat credit discount biased toward the minimum.

    Squaring a [0,1) random draw skews the result low, so most users land
    near OFFER_MIN_CREDITS and only a few reach OFFER_MAX_CREDITS.
    """
    import random

    lo, hi = OFFER_MIN_CREDITS, OFFER_MAX_CREDITS
    if hi <= lo:
        return lo
    span = hi - lo
    biased = random.random() ** 2  # skew toward 0
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


async def maybe_grant_offer(telegram_id: int) -> dict | None:
    """Grant a new random discount offer if eligible; return the active offer
    (newly granted or already running), or None."""
    active = await db.get_active_offer(telegram_id)
    if active:
        return active
    if not await db.can_grant_offer(telegram_id):
        return None
    credits = _random_discount_credits()
    hours = _random_offer_hours()
    return await db.set_offer(telegram_id, credits, hours)


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


async def alert(bot: Client, text: str):
    """Send an alert to CHAT_ID channel, or to all admins if not configured."""
    if CHAT_ID:
        try:
            await bot.send_message(CHAT_ID, text)
        except Exception as e:
            log.error("Failed to send alert to CHAT_ID %s: %s", CHAT_ID, e)
    else:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text)
            except Exception as e:
                log.error("Failed to send alert to admin %d: %s", admin_id, e)


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
        if VERIFICATION_ENABLED:
            tg_user = update.from_user
            user_id = tg_user.id
            if not await db.is_admin(user_id):
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
        buttons[-1].append(InlineKeyboardButton(f"{em.BROADCAST} Updates", url=UPDATES_CHANNEL, style=S.SUCCESS))
    if is_admin:
        buttons.append(
            [InlineKeyboardButton(f"{em.GEAR} Admin Panel", callback_data="admin_panel", style=S.DANGER)]
        )
    return InlineKeyboardMarkup(buttons)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{em.ADD} Add Number", callback_data="add_number", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.PLAN} List Numbers", callback_data="list_numbers", style=S.PRIMARY),
        ],
        [
            InlineKeyboardButton(f"{em.MONEY} Country Pricing", callback_data="country_pricing", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.USERS} Users", callback_data="users_list", style=S.PRIMARY),
        ],
        [InlineKeyboardButton(f"{em.OFFLINE} Sold", callback_data="sold_list", style=S.PRIMARY)],
        [
            InlineKeyboardButton(f"{em.INBOX} Seller Submissions", callback_data="seller_submissions", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.DOLLAR} Withdrawals", callback_data="seller_withdrawals", style=S.DANGER),
        ],
        [
            InlineKeyboardButton(f"{em.MONEY} Add Credits", callback_data="add_credits", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.STATS} Stats", callback_data="stats", style=S.PRIMARY),
        ],
        [InlineKeyboardButton(f"{em.BROADCAST} Broadcast", callback_data="broadcast_help", style=S.PRIMARY)],
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DANGER)],
    ])


def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=target, style=S.PRIMARY)],
    ])


def _confirm_country_kb(cflag: str, cname: str, cc: str, year: int | None, *, pick: bool = False) -> InlineKeyboardMarkup:
    """Country-confirm keyboard with an inline account-year adjuster row."""
    yes_cb = f"cc_pick:{cc}" if pick else "cc_yes"
    year_label = str(year) if year else "Unknown"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{em.SUCCESS} Yes, {cflag} {cname}", callback_data=yes_cb, style=S.SUCCESS),
            InlineKeyboardButton(f"{em.ERROR} No", callback_data="cc_no", style=S.DANGER),
        ],
        [
            InlineKeyboardButton(f"{em.CALENDAR} Account Year: " + year_label, callback_data="ay_edit", style=S.PRIMARY),
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
            offer = await maybe_grant_offer(user_id)
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
                    f"Buy credits, grab a Telegram account, and get its login OTP instantly.{credit_line}"
                ),
                reply_markup=main_menu_kb(is_adm),
            )
        else:
            await safe_edit(cq.message,
                f"{em.WAVE} **OTP Bot — Main Menu**\n\n"
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
        await safe_edit(cq.message, f"{em.GEAR} **Admin Panel**\n\n"
            f"Manage numbers, users, pricing, and broadcasts.",
            reply_markup=admin_kb())

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
        year_label = str(year) if year else "Unknown"
        await safe_edit(cq.message,
            f"{em.CALENDAR} **Adjust Account Year**\n\n"
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
        year_label = str(new_year)
        await safe_edit(cq.message,
            f"{em.CALENDAR} **Adjust Account Year**\n\n"
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
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        phone = state["phone"]
        await safe_edit(cq.message,
            f"{em.SUCCESS} **Account year set to {year}**\n\n"
            f"{em.PHONE} `{phone}`\n"
            f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n\n"
            "Confirm and save?",
            reply_markup=_confirm_country_kb(flag, name, cc, year),
        )
        await cq.answer(f"Year set to {year}")

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
        email_added = state.get("email_added", False)

        price = await db.get_category_price(cc, year, email_added)
        if price is None:
            state["step"] = "set_new_category_price"
            state["pending_cc"] = cc
            await safe_edit(cq.message,
                f"{em.MONEY} **New Category Detected!**\n\n"
                f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
                f"{em.CALENDAR} Year: **{year}**\n"
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
                              email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added)
        auth_states.pop(cq.from_user.id, None)

        await alert(app,
            f"{em.ADD} **Number Added**\n\n"
            f"{em.SHIELD} Admin: `{cq.from_user.id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {name}\n"
            f"{em.CALENDAR} Year: **{year if year else 'Unknown'}**\n"
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
        email_added = state.get("email_added", False)

        price = await db.get_category_price(cc, year, email_added)
        if price is None:
            state["step"] = "set_new_category_price"
            state["pending_cc"] = cc
            await safe_edit(cq.message,
                f"{em.MONEY} **New Category Detected!**\n\n"
                f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
                f"{em.CALENDAR} Year: **{year}**\n"
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
                              email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added)
        auth_states.pop(cq.from_user.id, None)

        await alert(app,
            f"{em.ADD} **Number Added**\n\n"
            f"{em.SHIELD} Admin: `{cq.from_user.id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {name}\n"
            f"{em.CALENDAR} Year: **{year if year else 'Unknown'}**\n"
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
        "start", "help", "cancel", "addcred", "removecred", "broadcast", "info",
    ]))
    async def on_text(_, message: Message):
        user_id = message.from_user.id
        text = message.text.strip()

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

        state = auth_states.get(user_id)
        if not state:
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
        elif step == "set_new_category_price":
            await _handle_set_new_category_price(message, text)
        elif step == "edit_num_country":
            await _handle_edit_num_country(message, text)
        elif step == "edit_num_set_price":
            await _handle_edit_num_set_price(message, text)


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
                email = cat.get("email_added", False)
                price = cat.get("price", 1)
                email_str = "Yes" if email else "No"
                
                lines.append(f"{em.CALENDAR} Year: **{year}** | {em.MAIL} Email: **{email_str}** — **{price}** cr")
                buttons.append([InlineKeyboardButton(
                    f"{em.EDIT} {year} | Email: {email_str} — {price} cr",
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
            f"{em.CALENDAR} Year: **{year}**\n"
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
        for cc in sorted(by_country.keys()):
            flag = get_country_flag(cc)
            for s in by_country[cc]:
                phone = s["phone_number"]
                status_icon = {"active": f"{em.ONLINE}", "sold": f"{em.OFFLINE}", "error": f"{em.WARNING}", "unlisted": f"{em.BLOCKED}"}.get(s.get("status"), f"{em.IDLE}")
                acc_year = s.get("account_year")
                year_str = f" ~{acc_year}" if acc_year else ""
                p = await db.get_session_price(s)
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
        age_line = f"{em.CALENDAR} **Account:** created ~{acc_year}\n" if acc_year else ""
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

        await db.set_session_status(phone, "active")
        await cq.answer(f"{em.SUCCESS} Number re-listed for sale!")

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
            year_str = f" ~{acc_year}" if acc_year else ""
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

        age_line = f"{em.CALENDAR} **Account Year:** ~{acc_year}\n" if acc_year else ""
        email_line = f"{em.MAIL} **Email Added:** {'Yes' if email_added else 'No'}\n"

        info = (
            f"{em.OFFLINE} **Sold Number**\n\n"
            f"<blockquote>"
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
        email = session.get("email_added", False)
        year_label = str(year) if year else "Unknown"
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
                f"{em.CALENDAR} Year: **{year_label}**\n"
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
            f"{em.CALENDAR} Account Year: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n"
            f"{em.MONEY} Current Price: **{price}** credits\n\n"
            "Select what to change:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.GLOBE} Change Country ({cc})", callback_data=f"echg_cc:{phone}", style=S.PRIMARY)],
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data=f"echg_yr:{phone}:-1", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.CALENDAR} Year: {year_label}", callback_data="noop", style=S.DEFAULT),
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
            role_icon = f"{em.OWNER}" if u["role"] == "admin" else f"{em.USER}"
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

    # ── Credits ──

    @app.on_callback_query(filters.regex("^add_credits$"))
    @verified
    async def cb_add_credits(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        await safe_edit(cq.message,
            f"{em.MONEY} **Add Credits**\n\n"
            "Use the command:\n"
            "`/addcred <userid> <credits>`\n\n"
            "**Example:**\n"
            "`/addcred 123456789 50`\n\n"
            "You can find user IDs in the **Users** section.",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.command("addcred") & filters.private)
    @verified
    async def cmd_addcred(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply(f"{em.BLOCKED} Admin only.")
            return

        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "**Usage:** `/addcred <userid> <credits>`\n"
                "**Example:** `/addcred 123456789 50`"
            )
            return

        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply(f"{em.ERROR} Invalid user ID.")
            return

        try:
            amount = int(parts[2])
            if amount <= 0:
                await message.reply(f"{em.ERROR} Credits must be a positive number.")
                return
        except ValueError:
            await message.reply(f"{em.ERROR} Invalid credits amount.")
            return

        target = await db.get_user(target_id)
        if not target:
            await message.reply(f"{em.ERROR} User `{target_id}` not found.")
            return

        await db.add_credits(target_id, amount)
        new_balance = await db.get_credits(target_id)
        name = target.get("first_name") or target.get("username") or str(target_id)

        await alert(app,
            f"{em.OWNER} **Admin Added Credits**\n\n"
            f"{em.SHIELD} Admin: `{message.from_user.id}`\n"
            f"{em.USER} Target: `{target_id}` ({name})\n"
            f"{em.ADD} Credits: +{amount}\n"
            f"{em.MONEY} New balance: {new_balance}"
        )

        await message.reply(
            f"{em.SUCCESS} **Credits added!**\n\n"
            f"{em.USER} User: **{name}**\n"
            f"{em.ADD} Added: **{amount}**\n"
            f"{em.MONEY} New balance: **{new_balance}**",
        )

        try:
            await bot.send_message(
                target_id,
                f"{em.MONEY} **Credits added!**\n\n"
                f"{em.ADD} {amount} credits added to your account.\n"
                f"{em.MONEY} New balance: **{new_balance}**",
            )
        except Exception:
            pass

    @app.on_message(filters.command("removecred") & filters.private)
    @verified
    async def cmd_removecred(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply(f"{em.BLOCKED} Admin only.")
            return

        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "**Usage:** `/removecred <userid> <credits>`\n"
                "**Example:** `/removecred 123456789 50`"
            )
            return

        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply(f"{em.ERROR} Invalid user ID.")
            return

        try:
            amount = int(parts[2])
            if amount <= 0:
                await message.reply(f"{em.ERROR} Credits must be a positive number.")
                return
        except ValueError:
            await message.reply(f"{em.ERROR} Invalid credits amount.")
            return

        target = await db.get_user(target_id)
        if not target:
            await message.reply(f"{em.ERROR} User `{target_id}` not found.")
            return

        current = await db.get_credits(target_id)
        if current < amount:
            await message.reply(
                f"{em.ERROR} User only has **{current}** credits. "
                f"Cannot remove **{amount}**."
            )
            return

        await db.deduct_credits(target_id, amount)
        new_balance = await db.get_credits(target_id)
        name = target.get("first_name") or target.get("username") or str(target_id)

        await alert(app,
            f"{em.OWNER} **Admin Removed Credits**\n\n"
            f"{em.SHIELD} Admin: `{message.from_user.id}`\n"
            f"{em.USER} Target: `{target_id}` ({name})\n"
            f"{em.ERROR} Credits: -{amount}\n"
            f"{em.MONEY} New balance: {new_balance}"
        )

        await message.reply(
            f"{em.SUCCESS} **Credits removed!**\n\n"
            f"{em.USER} User: **{name}**\n"
            f"{em.ERROR} Removed: **{amount}**\n"
            f"{em.MONEY} New balance: **{new_balance}**",
        )

        try:
            await bot.send_message(
                target_id,
                f"{em.MONEY} **Credits removed**\n\n"
                f"{em.ERROR} {amount} credits removed from your account.\n"
                f"{em.MONEY} New balance: **{new_balance}**",
            )
        except Exception:
            pass

    # ── Info ──

    @app.on_message(filters.command("info") & filters.private)
    @verified
    async def cmd_info(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply(f"{em.BLOCKED} Admin only.")
            return

        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "**Usage:** `/info <userid or @username>`\n"
                "**Example:** `/info 123456789` or `/info @john`"
            )
            return

        query = parts[1].strip()
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
        role = user.get("role", "user")
        credits = user.get("credits", 0)
        verified_status = user.get("verified", False)
        referred_by = user.get("referred_by")
        ref_earned = user.get("referral_earned", 0)
        ref_count = await db.get_referral_count(uid, verified_only=VERIFICATION_ENABLED)
        created = user.get("created_at")
        created_str = created.strftime("%Y-%m-%d %H:%M UTC") if created else "—"

        role_icon = f"{em.OWNER}" if role == "admin" else f"{em.USER}"
        verified_icon = f"{em.VERIFIED}" if verified_status else f"{em.UNVERIFIED}"

        ref_line = f"\n{em.LINK} Referred by: `{referred_by}`" if referred_by else ""

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
            f"</blockquote>",
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

        s = await db.get_stats()
        ps = await db.get_payment_stats()
        ext = await db.get_extended_stats()
        rev = await db.get_revenue_stats()
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
            else:
                pay_lines += f"\n  {method}: {info['count']} payments, ₹{total:.2f}"

        # Inventory breakdown by status
        inv = ext["inventory"]
        inv_line = " | ".join(f"{k}: {v}" for k, v in sorted(inv.items())) or "—"

        activity = (
            f"\n\n{em.CALENDAR} **Activity — 24h | 7d | 30d | all:**\n"
            f"  {em.ADD} Numbers added: {d(ext['added'])}\n"
            f"  {em.MONEY} Numbers sold: {d(ext['sold'])}\n"
            f"  {em.DELETE} Numbers removed: {d(ext['removed'])}\n"
            f"  {em.CREDIT} Transactions: {d(ext['transactions'])}\n"
            f"  {em.NEW_USER} New users: {d(ext['new_users'])}\n"
            f"  {em.MAIL} OTPs forwarded: {d(ext['otps'])}\n"
            f"  {em.WARNING} Auth failures: {d(ext['auth_failures'])}"
        )

        st = ext["sell_through"]
        tts = ext["avg_time_to_sell"]
        tts_str = f"{tts:.1f}h" if tts is not None else "—"
        performance = (
            f"\n\n{em.TRENDING_UP} **Performance:**\n"
            f"  Sell-through (24h/7d/30d/all): "
            f"{st['24h']:.0f}% | {st['7d']:.0f}% | {st['30d']:.0f}% | {st['all']:.0f}%\n"
            f"  Avg time-to-sell: {tts_str}"
        )

        fn = ext["funnel"]
        v_pct = (fn["verified"] / fn["users"] * 100) if fn["users"] else 0
        b_pct = (fn["buyers"] / fn["users"] * 100) if fn["users"] else 0
        funnel = (
            f"\n\n{em.USERS} **Funnel (all-time):**\n"
            f"  Users: {fn['users']} → Verified: {fn['verified']} ({v_pct:.0f}%) "
            f"→ Buyers: {fn['buyers']} ({b_pct:.0f}%)"
        )

        revenue = (
            f"\n\n{em.BANK} **Revenue (INR-equiv):**\n"
            f"  Last 24h: ₹{rev['24h']['inr']:.2f} ({rev['24h']['count']} txns)\n"
            f"  All-time: ₹{rev['all']['inr']:.2f} ({rev['all']['count']} txns)"
        )

        top_lines = f"\n\n{em.FIRE} **Leaderboard (24h):**"
        if top_buyer:
            top_lines += f"\n  {em.MONEY} Top buyer: @{top_buyer['name']} ({top_buyer['total']:.2f})"
        else:
            top_lines += f"\n  {em.MONEY} Top buyer: —"
        if top_ref:
            top_lines += f"\n  {em.USERS} Top referrer: @{top_ref['name']} ({top_ref['count']} refs)"
        else:
            top_lines += f"\n  {em.USERS} Top referrer: —"

        await safe_edit(cq.message,
            f"{em.STATS} **Statistics**\n\n"
            f"<blockquote expandable>"
            f"{em.USERS} Users: {s['users']}\n"
            f"{em.PHONE} Numbers (active): {s['sessions']}\n"
            f"{em.ONLINE} Connected: {active}\n"
            f"{em.LINK} Assigned now: {assigned}\n"
            f"{em.MAIL} OTPs forwarded: {s['otps']}\n"
            f"{em.WALLET} Outstanding credits: {ext['outstanding_credits']}\n"
            f"{em.PHONE} Inventory: {inv_line}"
            f"{activity}"
            f"{performance}"
            f"{funnel}"
            f"{revenue}\n\n"
            f"{em.CREDIT} **Payments by method:** {ps['total_payments']}{pay_lines}"
            f"{top_lines}"
            f"</blockquote>",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Get Number (User) — Country-based ──

    @app.on_callback_query(filters.regex(r"^get_number$|^pg_gn:\d+$"))
    @verified
    async def cb_get_number(_, cq: CallbackQuery):
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_gn:") else 0

        offer = await db.get_active_offer(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)

        sessions = await db.get_active_sessions()
        by_country = {}
        for s in sessions:
            p = await db.get_session_price(s)
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
        if cq.data.startswith("pg_cn:"):
            parts = cq.data.split(":")
            cc, page = parts[1], int(parts[2])
        else:
            cc = cq.data.split(":", 1)[1]
            page = 0

        offer = await db.get_active_offer(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)

        sessions = await db.get_active_sessions_by_country(cc)
        valid_sessions = []
        session_prices = []   # effective (discounted) prices
        for s in sessions:
            p = await db.get_session_price(s)
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
            year_str = f" ({year})" if year else ""
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

    @app.on_callback_query(filters.regex(r"^sel:"))
    @verified
    async def cb_select_number(_, cq: CallbackQuery):
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
        qr_msg = await bot.send_photo(
            cq.from_user.id,
            photo=qr["image_url"],
            caption=(
                f"{em.MONEY} **Top up to grab this account**\n\n"
                f"{flag} `{mask_phone(phone)}` — **{price}** credits\n"
                f"{offer_line}"
                f"{em.CREDIT} Your balance: **{credits}** — short by **{shortfall}**\n\n"
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

        otp_received = req.get("otp_received", False)
        price = req.get("price", 0)
        user_id = req["user_id"]
        no_sale = req.get("no_sale", False)

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

        clients.release_number(phone)
        await clients.stop_session(phone)

        if otp_received:
            await db.mark_session_sold(phone, user_id, price)
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` released and marked as sold.\n\n"
                f"{em.MONEY} **{price} credits** — no refund (OTP was received).",
                reply_markup=back_kb("main_menu"),
            )
        else:
            if price > 0:
                await db.save_pending_refund(user_id, phone, price)
            restored = await db.restore_offer(user_id, delay_hours=1 if price > 0 else 0)
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
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_mh:") else 0

        otps = await db.get_user_otps(cq.from_user.id, limit=200)
        if not otps:
            await safe_edit(cq.message,
                f"{em.LOGS} **No OTP history yet.**\n\n"
                f"Your login OTPs will appear here after you buy an account.",
                reply_markup=back_kb("main_menu"),
            )
            return

        all_lines = []
        for o in otps:
            ts = o["created_at"].strftime("%m/%d %H:%M")
            all_lines.append(
                f"`{o['code']}` — {o['phone_number']} — {o['sender']} — {ts}"
            )

        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_lines = all_lines[start:end]
        total_pages = (len(all_lines) + PAGE_SIZE - 1) // PAGE_SIZE

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

        await safe_edit(cq.message,
            f"{em.LOGS} **Recent OTPs:**\n\n<blockquote expandable>"
            + "\n".join(page_lines)
            + "</blockquote>" + page_label,
            reply_markup=InlineKeyboardMarkup(footer),
        )

    # ── Buy Credits ──

    @app.on_callback_query(filters.regex("^buy_credits$"))
    @verified
    async def cb_buy_credits(_, cq: CallbackQuery):
        auth_states.pop(cq.from_user.id, None)
        credits, balance, total_funds = await db.get_total_funds(cq.from_user.id)
        buttons = [
            [
                InlineKeyboardButton(f"{em.MONEY} Razorpay (UPI)", callback_data="rz_plans", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.COIN} Crypto (USDT)", callback_data="cr_plans", style=S.PRIMARY),
            ],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)],
        ]
        await safe_edit(cq.message,
            f"{em.CREDIT} **Buy Credits**\n\n"
            f"{em.CREDIT} Credits: **{credits}** (purchase only)\n"
            f"{em.MONEY} Withdrawable Balance: **{balance}** credits (purchase & withdrawal)\n\n"
            "Choose a payment method:",
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

        qr_msg = await bot.send_photo(
            cq.from_user.id,
            photo=qr["image_url"],
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
            yr_str = f" ~{yr}" if yr else ""
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
            yr_str = f"~{yr}" if yr else "?"
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
            yr_str = f" ~{yr}" if yr else ""
            em_str = " +Email" if lst.get("email_added") else ""

            all_buttons.append([InlineKeyboardButton(
                f"{flag} {phone}{yr_str}{em_str} — Set Price",
                callback_data=f"setcprice:{cc}", style=S.PRIMARY,
            )])

        await safe_edit(cq.message,
            f"{em.INBOX} **Pending Seller Submissions ({len(pending)})**\n\n"
            f"These accounts are waiting for their category price to be set.\n"
            f"Tap an account to configure its category price in Country Pricing — once set, it will activate automatically.",
            reply_markup=InlineKeyboardMarkup(all_buttons + [[InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)]]),
        )

    @app.on_callback_query(filters.regex("^seller_withdrawals$"))
    @verified
    async def cb_seller_withdrawals(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
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
            lines.append(f"• `{sid}`: **{amt} cr** via {mth.upper()} (`{dtl}`)")
            buttons.append([
                InlineKeyboardButton(f"{em.SUCCESS} Paid {sid} ({amt} cr)", callback_data=f"approve_w:{wid}", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.ERROR} Reject", callback_data=f"reject_w:{wid}", style=S.DANGER),
            ])

        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="admin_panel", style=S.DEFAULT)])

        await safe_edit(cq.message,
            f"{em.DOLLAR} **Pending Seller Withdrawals ({len(withdrawals)})**\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^approve_w:"))
    @verified
    async def cb_approve_w(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        wid = cq.data.split(":", 1)[1]
        ok = await db.mark_withdrawal_done(wid, admin_note=f"Approved by {cq.from_user.id}")
        if ok:
            await cq.answer("Withdrawal marked as paid!", show_alert=True)
            await cb_seller_withdrawals(app, cq)
        else:
            await cq.answer("Withdrawal request not found or already processed.", show_alert=True)

    @app.on_callback_query(filters.regex(r"^reject_w:"))
    @verified
    async def cb_reject_w(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer(f"{em.BLOCKED} Admin only.", show_alert=True)
            return

        wid = cq.data.split(":", 1)[1]
        doc = await db.mark_withdrawal_rejected(wid, reason="Rejected by admin")
        if doc:
            await cq.answer("Withdrawal rejected and balance refunded.", show_alert=True)
            try:
                await app.send_message(
                    doc["seller_id"],
                    f"{em.ERROR} **Withdrawal Request Rejected**\n\n"
                    f"Your withdrawal request for **{doc['amount']} credits** was rejected by admin.\n"
                    f"The amount has been refunded to your withdrawable balance.",
                )
            except Exception:
                pass
            await cb_seller_withdrawals(app, cq)
        else:
            await cq.answer("Withdrawal request not found or already processed.", show_alert=True)

    # ── Help / Cancel ──

    def _build_help_text(is_admin: bool) -> str:
        admin_section = (
            f"\n\n{em.GEAR} **Admin Commands:**\n"
            "<blockquote expandable>"
            "/addcred `<userid>` `<credits>` — Add credits to a user\n"
            "/removecred `<userid>` `<credits>` — Remove credits from a user\n"
            "/info `<userid or @username>` — Look up user details\n"
            "/broadcast `<message>` — Broadcast to all users\n"
            "/broadcast `-name` `<message>` — Broadcast with your name"
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
            f"{em.FAQ} **Features:**\n"
            "<blockquote>"
            f"• {em.PHONE} **Buy Account** — Purchase a Telegram account and get its login OTP\n"
            f"• {em.LOGS} **My History** — View your past purchases\n"
            f"• {em.CREDIT} **Buy Credits** — Top up via UPI or USDT\n"
            f"• {em.PHONE} **Support** — Contact our support agents"
            "</blockquote>\n\n"
            f"{em.SETTINGS} **Commands:**\n"
            "<blockquote>"
            "/start — Main menu\n"
            "/help — This help page\n"
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


# ── Auth helpers ──

async def _account_info(client: Client, current_phone: str = "") -> tuple[int | None, int | None, bool, bool, int, str]:
    """Fetch account id + creation year (exact via MTProto or estimated) + has_email + is_peer_flood + session_count + session_info."""
    try:
        me = await client.get_me()
        account_id = me.id
    except Exception as e:
        log.error("Failed to get me from client: %s", e)
        return None, None, False, False, 1, ""

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
    is_peer_flood = False

    # registration_month can only be read by ANOTHER account that A has just
    # messaged (it lives in PeerSettings, not on A's own GetFullUser). So we run a
    # cross-account probe: A messages a few active observer accounts, and each
    # observer reads A's registration_month back. The message attempt doubles as
    # the real PEER_FLOOD test — if A can't message, it's spam-limited/unsellable.
    try:
        reg_month, is_peer_flood = await clients.probe_registration_month(client, account_id, current_phone)
        if reg_month:
            yr = extract_year_from_reg_month(reg_month)
            if yr:
                exact_year = yr
                log.info("Exact registration year for %s: %d (via cross-account probe)", me.id, yr)
    except Exception as e:
        log.warning("Error during cross-account registration probe: %s", e)

    year = exact_year if exact_year is not None else estimate_account_year(account_id)
    session_count, session_info = await get_active_sessions_info(client)
    return account_id, year, has_email, is_peer_flood, session_count, session_info



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
        acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
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
            "email_added": has_email,
        }
        sess_warn = f"\n\n⚠️ **Notice:** Account has **{sess_cnt} active sessions**." if sess_cnt > 1 else ""
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code verified for `{phone}`\n\n"
            f"{em.GLOBE} Detected country: {cflag} **{cname}** ({cc})\n"
            f"{em.CALENDAR} Account year: **{acc_year or 'Unknown'}**\n"
            f"{em.MAIL} Email added: **{'Yes' if has_email else 'No'}**{sess_warn}\n\n"
            "Is this correct?",
            reply_markup=_confirm_country_kb(cflag, cname, cc, acc_year),
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
        acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
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
            "email_added": has_email,
        }
        await safe_edit(status_msg,
            f"{em.SUCCESS} Password accepted for `{phone}`\n\n"
            f"{em.GLOBE} Detected country: {cflag} **{cname}** ({cc})\n"
            f"{em.CALENDAR} Account year: **{acc_year or 'Unknown'}**\n"
            f"{em.MAIL} Email added: **{'Yes' if has_email else 'No'}**\n\n"
            "Is this correct?",
            reply_markup=_confirm_country_kb(cflag, cname, cc, acc_year),
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

    flag = get_country_flag(cc)
    name = get_country_name(cc)
    email_str = "Yes" if email else "No"
    act_str = f"\n⚡ **{len(activated)} pending seller account(s) activated!**" if activated else ""

    await message.reply(
        f"{em.SUCCESS} Category price successfully updated!\n\n"
        f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
        f"{em.CALENDAR} Year: **{year}**\n"
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
        email_added = state.get("email_added", False)
        await message.reply(
            f"{em.GLOBE} Found: {flag} **{name}** ({cc})\n"
            f"{em.CALENDAR} Account year: **{year or 'Unknown'}**\n"
            f"{em.MAIL} Email added: **{'Yes' if email_added else 'No'}**\n\n"
            f"Confirm this country for `{state['phone']}`?",
            reply_markup=_confirm_country_kb(flag, name, cc, year, pick=True),
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
        year_label = str(year) if year else "Unknown"
        email = session.get("email_added", False) if session else False
        email_str = "Yes" if email else "No"
        price = await db.get_session_price(session) if session else 1

        await message.reply(
            f"{em.SUCCESS} Country updated to {flag} **{name}** ({cc})\n\n"
            f"{em.CONFIG} **Edit Category — `{phone}`**\n\n"
            f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
            f"{em.CALENDAR} Account Year: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{email_str}**\n"
            f"{em.MONEY} Current Price: **{price}** credits",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.GLOBE} Change Country ({cc})", callback_data=f"echg_cc:{phone}", style=S.PRIMARY)],
                [
                    InlineKeyboardButton(f"{em.REMOVE}", callback_data=f"echg_yr:{phone}:-1", style=S.DEFAULT),
                    InlineKeyboardButton(f"{em.CALENDAR} Year: {year_label}", callback_data="noop", style=S.DEFAULT),
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
    email = session.get("email_added", False)

    await db.set_category_price(cc, year, email, price)

    flag = get_country_flag(cc)
    name = get_country_name(cc)
    year_label = str(year) if year else "Unknown"
    email_str = "Yes" if email else "No"

    await message.reply(
        f"{em.SUCCESS} **Category price set!**\n\n"
        f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
        f"{em.CALENDAR} Year: **{year_label}**\n"
        f"{em.MAIL} Email: **{email_str}**\n"
        f"{em.MONEY} Price: **{price}** credits per OTP",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data=f"editnum:{phone}", style=S.DEFAULT)],
        ]),
    )


# ── Purchase finalization ──

async def _send_or_edit(user_id: int, edit_msg, text, reply_markup=None):
    """Edit an existing message when given one, otherwise send a fresh message."""
    if edit_msg is not None:
        return await safe_edit(edit_msg, text, reply_markup=reply_markup)
    return await bot.send_message(user_id, text, reply_markup=reply_markup)


async def _finalize_purchase(user_id: int, phone: str, edit_msg=None) -> bool:
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

    cc = session.get("country_code", "XX")
    if not cc or cc == "XX":
        detected_cc, _, _ = detect_country(phone)
        if detected_cc != "XX":
            cc = detected_cc
    base_price = await db.get_session_price(session)
    if base_price is None:
        await _send_or_edit(user_id, edit_msg,
            f"{em.ERROR} This number is not configured for sale.",
            reply_markup=back_kb("get_number"))
        return False

    # Apply any active discount offer server-side (never trust the client).
    offer = await db.get_active_offer(user_id)
    price = apply_discount(base_price, offer)
    credits = await db.get_credits(user_id)
    # A fully-covered number is free only for users who hold real credits.
    if price == 0 and credits <= 0:
        price = 1
    saved = base_price - price

    if credits < price:
        # An offer may have expired between top-up and payment, raising the price.
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
            return False

    credits_deducted, balance_deducted = 0, 0
    if price > 0:
        ok, credits_deducted, balance_deducted = await db.deduct_funds_for_purchase(user_id, price)
        if not ok:
            await clients.stop_session(phone)
            await _send_or_edit(user_id, edit_msg,
                f"{em.ERROR} Could not deduct funds. Please try again or contact support.",
                reply_markup=back_kb("main_menu"))
            return False
        log.info("Deducted %d credits and %d balance from user %d on selection", credits_deducted, balance_deducted, user_id)

    if offer:
        await db.consume_offer(user_id)

    clients.assign_number(phone, user_id, OTP_TIMEOUT, price, credits_deducted=credits_deducted, balance_deducted=balance_deducted)

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
    # Defer this admin alert until an OTP is actually forwarded — stash the
    # text on the active request so clients.py can send it at that point.
    purchase_alert = (
        f"{em.PHONE} **Number Purchased**\n\n"
        f"{em.USER} User: `{user_id}` (@{uname})\n"
        f"{em.PHONE} Number: `{phone}`\n"
        f"{flag} Country: {name}\n"
        f"{admin_price_line}"
        f"{em.MONEY} Remaining funds: **{total_funds}** ({credits} credits, {balance} withdrawable)"
    )
    req_info = clients.active_requests.get(phone)
    if req_info is not None:
        req_info["purchase_alert"] = purchase_alert
    credit_line = f"\n{em.CREDIT} Credits: {credits}\n{em.MONEY} Withdrawable Balance: {balance}"
    acc_year = session.get("account_year")
    age_line = f"\n{em.CALENDAR} Account created: ~{acc_year}" if acc_year else ""
    email_added = session.get("email_added", False)
    email_line = f"\n{em.MAIL} Email Added: {'Yes' if email_added else 'No'}"
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
        f"{flag} {name}\n"
        f"{em.PHONE} `{phone}`\n"
        f"{price_line}"
        f"{em.TIMER} Login window: {OTP_TIMEOUT // 60} min{age_line}{email_line}{credit_line}\n\n"
        "The login OTP for this account will be forwarded to you here.\n\n"
        f"{em.WARNING} On manual release, your credits will be locked for 1 hour.\n\n"
        f"{em.WARNING} Issues logging in? Contact support:\n{support}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{em.UNLOCK} Release Account", callback_data=f"release:{phone}", style=S.DANGER)],
        ]),
    )
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
    plan = get_credit_plan(plan_key)
    if not plan:
        return False
    # Atomic flip guarantees credits are granted once even if the live poller and
    # the restart-recovery processor both observe the payment.
    if not await db.mark_pending_payment_done(qr_id):
        return False

    await db.add_credits(user_id, plan["credits"])
    await db.save_payment(user_id, "razorpay", plan_key, plan["amount_inr"] / 100, "INR", qr_id)
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

    pay_states.pop(user_id, None)
    await db.mark_tx_used(tx_hash, user_id, plan_key)
    await db.add_credits(user_id, plan["credits"])
    await db.save_payment(user_id, "crypto_usdt", plan_key, pstate["amount_usdt"], "USDT", tx_hash)
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

    qr_msg = await bot.send_photo(
        user_id,
        photo=qr["image_url"],
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
    email_added = state.get("email_added", False)

    await db.set_category_price(cc, year, email_added, price)
    activated = await db.check_and_activate_pending_listings(cc, year, email_added)

    phone = state["phone"]
    flag = get_country_flag(cc)
    name = get_country_name(cc)

    await db.save_session(phone, state["session_string"], user_id,
                          password=state.get("password", ""), country_code=cc,
                          account_id=state.get("account_id"), account_year=year,
                          email_added=email_added)
    await db.set_session_account_info(phone, state.get("account_id"), year, email_added)
    auth_states.pop(user_id, None)

    await alert(bot,
        f"{em.ADD} **Number Added**\n\n"
        f"{em.SHIELD} Admin: `{user_id}`\n"
        f"{em.PHONE} Number: `{phone}`\n"
        f"{flag} Country: {name}\n"
        f"{em.CALENDAR} Year: **{year if year else 'Unknown'}**\n"
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
        acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        await _complete_sell_submission(user_id, status_msg, phone, session_string, "", state["country_code"], acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info)
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
        acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info = await _account_info(client, phone)
        session_string = await client.export_session_string()
        await client.disconnect()

        await _complete_sell_submission(user_id, status_msg, phone, session_string, password, state["country_code"], acc_id, acc_year, has_email, is_peer_flood, sess_cnt, sess_info)
    except PasswordHashInvalid:
        await safe_edit(status_msg, f"{em.ERROR} Wrong password. Try again:")
    except Exception as e:
        sell_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg, f"{em.ERROR} Error: `{e}`", reply_markup=back_kb("sell_account"))


async def _complete_sell_submission(seller_id: int, status_msg, phone: str, session_string: str, password: str, cc: str, acc_id: int | None, acc_year: int | None, has_email: bool, is_peer_flood: bool, sess_cnt: int = 1, sess_info: str = ""):
    sell_states.pop(seller_id, None)

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
    year_label = str(acc_year) if acc_year else "Unknown"

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
        phone, seller_id, session_string, stored_password, cc, acc_id, acc_year, has_email,
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
        await db.save_session(phone, session_string, seller_id, password=stored_password, country_code=cc, account_id=acc_id, account_year=acc_year, email_added=has_email)

        seller_payout = updated_listing["payout_credits"] if updated_listing else int(cat_price * SELLER_PAYOUT_PERCENT / 100)

        await alert(bot,
            f"{em.ADD} **Seller Account Listed**\n\n"
            f"{em.USER} Seller: `{seller_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {cname}\n"
            f"{em.CALENDAR} Year: **{year_label}**\n"
            f"{em.MONEY} Category Price: {cat_price} credits\n"
            f"{em.DOLLAR} Payout on sale: {seller_payout} credits"
        )

        await safe_edit(status_msg,
            f"{em.SUCCESS} **Account Listed for Sale!**\n\n"
            f"{flag} `{phone}` ({cname})\n"
            f"{em.CALENDAR} Account Year: **{year_label}**\n"
            f"{em.MONEY} Category Price: **{cat_price} credits**\n"
            f"{em.DOLLAR} You'll earn **{seller_payout} credits** ({SELLER_PAYOUT_PERCENT}%) **when a buyer purchases it**\n\n"
            f"Your account is now live in the store. You can still log into it any time from "
            f"**Sell Account ➔ Login to My Accounts** until it sells.",
            reply_markup=back_kb("sell_account"),
        )
    else:
        await alert(bot,
            f"{em.ALERT} **New Seller Submission — Pending Category Price**\n\n"
            f"{em.USER} Seller: `{seller_id}`\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {cname} ({cc})\n"
            f"{em.CALENDAR} Year: **{year_label}**\n"
            f"{em.MAIL} Email Added: **{'Yes' if has_email else 'No'}**\n\n"
            f"Set category price in **Admin Panel ➔ Country Pricing** to activate this account."
        )

        await safe_edit(status_msg,
            f"{em.SUCCESS} **Account Submitted!**\n\n"
            f"{flag} `{phone}` ({cname})\n"
            f"{em.CALENDAR} Account Year: **{year_label}**\n\n"
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

    await alert(bot,
        f"{em.ALERT} **New External Withdrawal Request**\n\n"
        f"{em.USER} Seller: `{user_id}`\n"
        f"{em.MONEY} Amount: **{amount} credits**\n"
        f"{em.NOTE} Method: **{method.upper()}**\n"
        f"{em.LINK} Details: `{details}`"
    )

    await message.reply(
        f"{em.SUCCESS} **Withdrawal Request Submitted!**\n\n"
        f"{em.MONEY} Amount: **{amount} credits**\n"
        f"{em.NOTE} Method: **{method.upper()}**\n"
        f"{em.LINK} Details: `{details}`\n\n"
        f"Admins will process your payment shortly.",
        reply_markup=main_menu_kb(False),
    )

