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
)
from decimal import Decimal
from config import API_ID, API_HASH, BOT_TOKEN, OTP_TIMEOUT, CREDIT_PLANS, CRYPTO_PLANS, SUPPORT_HANDLES, CHAT_ID, ADMIN_IDS, UPDATES_CHANNEL, USDT_TO_INR, TURNSTILE_SITE_KEY, VERIFY_URL, REFERRAL_BONUS, REFERRAL_VERIFY_BONUS, ENABLE_VERIFICATION
import database as db
import clients
import payments
import verification
from utils import detect_country, get_country_flag, get_country_name, search_country, estimate_account_year, mask_phone
import custom_emojis as em
em.patch_pyrogram_for_custom_emojis()

log = logging.getLogger(__name__)

bot: Client = None
auth_states: dict[int, dict] = {}
pay_states: dict[int, dict] = {}


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


async def _check_referral_reward(user_id: int):
    if await db.is_referral_purchase_rewarded(user_id):
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
    await db.mark_referral_purchase_rewarded(user_id)
    if REFERRAL_BONUS > 0:
        await db.add_referral_earning(referrer_id, REFERRAL_BONUS)
        try:
            uname = user.get("first_name") or user.get("username") or str(user_id)
            new_balance = await db.get_credits(referrer_id)
            await bot.send_message(
                referrer_id,
                f"{em.GIFT} **Referral Reward!**\n\n"
                f"Your referral **{uname}** made their first purchase.\n"
                f"{em.MONEY} +{REFERRAL_BONUS} credits added!\n"
                f"{em.MONEY} Balance: **{new_balance}**",
            )
        except Exception:
            pass


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
            InlineKeyboardButton(f"{em.PHONE} Get Number", callback_data="get_number", style=S.PRIMARY),
            InlineKeyboardButton(f"{em.LOGS} My History", callback_data="my_history", style=S.DEFAULT),
        ],
        [InlineKeyboardButton(f"{em.CREDIT} Buy Credits", callback_data="buy_credits", style=S.SUCCESS)],
        [
            InlineKeyboardButton(f"{em.GIFT} Refer & Earn", callback_data="referral", style=S.DEFAULT),
            InlineKeyboardButton(f"{em.TUTORIAL} How to Use", callback_data="how_to_use", style=S.DEFAULT),
        ],
        [
            InlineKeyboardButton(f"{em.PHONE} Support", callback_data="support", style=S.DEFAULT),
            InlineKeyboardButton(f"{em.HELP} Help", callback_data="help", style=S.DEFAULT),
        ],
    ]
    if UPDATES_CHANNEL:
        buttons[-1].append(InlineKeyboardButton(f"{em.BROADCAST} Updates", url=UPDATES_CHANNEL, style=S.DEFAULT))
    if is_admin:
        buttons.append(
            [InlineKeyboardButton(f"{em.GEAR} Admin Panel", callback_data="admin_panel", style=S.PRIMARY)]
        )
    return InlineKeyboardMarkup(buttons)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{em.ADD} Add Number", callback_data="add_number", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.PLAN} List Numbers", callback_data="list_numbers", style=S.DEFAULT),
        ],
        [
            InlineKeyboardButton(f"{em.MONEY} Country Pricing", callback_data="country_pricing", style=S.DEFAULT),
            InlineKeyboardButton(f"{em.USERS} Users", callback_data="users_list", style=S.DEFAULT),
        ],
        [InlineKeyboardButton(f"{em.OFFLINE} Sold", callback_data="sold_list", style=S.DEFAULT)],
        [
            InlineKeyboardButton(f"{em.MONEY} Add Credits", callback_data="add_credits", style=S.SUCCESS),
            InlineKeyboardButton(f"{em.STATS} Stats", callback_data="stats", style=S.DEFAULT),
        ],
        [InlineKeyboardButton(f"{em.BROADCAST} Broadcast", callback_data="broadcast_help", style=S.PRIMARY)],
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)],
    ])


def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{em.BACK} Back", callback_data=target, style=S.DEFAULT)],
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
            InlineKeyboardButton(f"{em.CALENDAR} Account Year: " + year_label, callback_data="ay_edit", style=S.DEFAULT),
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
        nav.append(InlineKeyboardButton(f"{em.BACK} Prev", callback_data=f"{cb_prefix}:{page - 1}", style=S.DEFAULT))
    if end < total:
        nav.append(InlineKeyboardButton(f"{em.NEXT} Next", callback_data=f"{cb_prefix}:{page + 1}", style=S.PRIMARY))

    footer = []
    if nav:
        footer.append(nav)
    footer.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data=back_target, style=S.DEFAULT)])

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
        credits = await db.get_credits(user_id)
        credit_line = f"\n{em.MONEY} Credits: **{credits}**"
        await message.reply(
            f"{em.WAVE} **Welcome to OTP Bot!**\n\n"
            "Get OTP codes from monitored Telegram numbers.\n"
            f"Choose an option below:{credit_line}",
            reply_markup=main_menu_kb(is_adm),
        )

    @app.on_callback_query(filters.regex("^main_menu$"))
    @verified
    async def cb_main_menu(_, cq: CallbackQuery):
        is_adm = await db.is_admin(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)
        credit_line = f"\n{em.MONEY} Credits: **{credits}**"
        if cq.message.video or cq.message.photo:
            try:
                await cq.message.delete()
            except Exception:
                pass
            await app.send_message(
                chat_id=cq.from_user.id,
                text=(
                    f"{em.WAVE} **OTP Bot — Main Menu**\n\n"
                    f"Buy credits, rent a number, and receive OTPs instantly.{credit_line}"
                ),
                reply_markup=main_menu_kb(is_adm),
            )
        else:
            await safe_edit(cq.message,
                f"{em.WAVE} **OTP Bot — Main Menu**\n\n"
                f"Buy credits, rent a number, and receive OTPs instantly.{credit_line}",
                reply_markup=main_menu_kb(is_adm),
            )


    @app.on_callback_query(filters.regex("^support$"))
    @verified
    async def cb_support(_, cq: CallbackQuery):
        lines = "\n".join(f"  • [{h.lstrip('@')}](https://t.me/{h.lstrip('@')})" for h in SUPPORT_HANDLES)
        await safe_edit(cq.message,
            f"{em.PHONE} **Support**\n\n"
            f"Having issues? Contact any of our support agents:\n\n"
            f"{lines}\n\n"
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
            f"{em.SHIELD} **{REFERRAL_VERIFY_BONUS} credits** when your friend {'verifies' if VERIFICATION_ENABLED else 'joins'}\n"
            f"{em.CREDIT} **{REFERRAL_BONUS} credits** on their first purchase\n\n"
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
            f"{em.PHONE} **Number:** `{phone}`\n"
            f"{flag} **Country:** {name} ({cc})\n"
            f"{em.STATS} Status: **{status}**\n"
            f"{em.MONEY} Price: **{price_str}**\n"
            f"{age_line}"
            f"{email_line}"
            f"{em.PASSWORD} Password: {'`' + pwd + '`' if pwd else 'Not set'}\n"
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
            f"{em.PHONE} **Number:** `{phone}`\n"
            f"{flag} **Country:** {name} ({cc})\n"
            f"{em.MONEY} **Price Paid:** {sold_price} credits\n"
            f"{buyer_line}"
            f"{sold_time}"
            f"{age_line}"
            f"{email_line}"
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
            f"{em.ID_BADGE} ID: `{uid}`\n"
            f"📛 Name: **{fname}**\n"
            f"{em.USER} Username: @{uname}\n"
            f"{em.SHIELD} Role: **{role}**\n"
            f"{verified_icon} Verified: **{'Yes' if verified_status else 'No'}**\n"
            f"{em.MONEY} Credits: **{credits}**\n"
            f"{em.CALENDAR} Joined: {created_str}\n"
            f"{em.GIFT} Referrals: **{ref_count}** | Earned: **{ref_earned}**{ref_line}",
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
        active = len(clients.active_clients)
        assigned = len(clients.active_requests)
        top_buyer = await db.top_buyer_24h()
        top_ref = await db.top_referrer_24h()

        pay_lines = ""
        for method, info in ps.get("by_method", {}).items():
            pay_lines += f"\n  {method}: {info['count']} payments, {info['total']:.2f}"

        daily_lines = "\n\n📊 **Last 24h:**"
        if top_buyer:
            daily_lines += f"\n  💰 Top buyer: @{top_buyer['name']} ({top_buyer['total']:.2f})"
        else:
            daily_lines += "\n  💰 Top buyer: —"
        if top_ref:
            daily_lines += f"\n  👥 Top referrer: @{top_ref['name']} ({top_ref['count']} refs)"
        else:
            daily_lines += "\n  👥 Top referrer: —"

        await safe_edit(cq.message,
            f"{em.STATS} **Statistics**\n\n"
            f"{em.USERS} Users: {s['users']}\n"
            f"{em.PHONE} Numbers (DB): {s['sessions']}\n"
            f"{em.ONLINE} Connected: {active}\n"
            f"{em.LINK} Assigned now: {assigned}\n"
            f"{em.MAIL} OTPs forwarded: {s['otps']}\n\n"
            f"{em.CREDIT} **Payments:** {ps['total_payments']}{pay_lines}"
            f"{daily_lines}",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Get Number (User) — Country-based ──

    @app.on_callback_query(filters.regex(r"^get_number$|^pg_gn:\d+$"))
    @verified
    async def cb_get_number(_, cq: CallbackQuery):
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_gn:") else 0

        sessions = await db.get_active_sessions()
        by_country = {}
        for s in sessions:
            p = await db.get_session_price(s)
            if p is None:
                continue
            cc = s.get("country_code", "XX")
            by_country.setdefault(cc, []).append((s, p))

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
                callback_data=f"country:{cc}", style=S.DEFAULT,
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_gn", "main_menu")
        start = page * PAGE_SIZE
        page_lines = all_lines[start:start + PAGE_SIZE]
        await safe_edit(cq.message,
            f"{em.GLOBE} **Select a Country**\n\n" + "\n".join(page_lines) + page_label,
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

        sessions = await db.get_active_sessions_by_country(cc)
        valid_sessions = []
        session_prices = []
        for s in sessions:
            p = await db.get_session_price(s)
            if p is not None:
                valid_sessions.append(s)
                session_prices.append(p)

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
            assigned = clients.get_request_user(phone)
            if assigned:
                all_buttons.append([
                    InlineKeyboardButton(f"{em.OFFLINE} {masked}{year_str}{email_icon} — {p} cr (in use)", callback_data="noop", style=S.DEFAULT)
                ])
            else:
                all_buttons.append([
                    InlineKeyboardButton(
                        f"{em.ONLINE} {masked}{year_str}{email_icon} — {p} cr", callback_data=f"sel:{phone}", style=S.SUCCESS
                    )
                ])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, f"pg_cn:{cc}", "get_number")
        await safe_edit(cq.message,
            f"{flag} **{name}** — **{range_str}** credits per OTP\n\n"
            f"Select a number:\n"
            f"{em.TIMER} Timeout: {OTP_TIMEOUT // 60} minutes.{page_label}\n\n"
            f"{em.INFO} **Note:** Your credit will be deducted on choosing the number\n"
            f"and will be refunded after 2 hours when manual release.",
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
        price = await db.get_session_price(session)
        if price is None:
            await cq.answer(f"{em.ERROR} This number is not configured for sale.", show_alert=True)
            return

        credits = await db.get_credits(cq.from_user.id)
        if credits < price:
            await cq.answer(
                f"{em.ERROR} You need {price} credits but have {credits}. Buy more credits!",
                show_alert=True,
            )
            return

        await safe_edit(cq.message, f"{em.LOADING} Connecting session...")

        try:
            await clients.start_session(phone, session["session_string"])
        except Exception as e:
            log.error("Failed to start session %s: %s", phone, e)
            await db.set_session_status(phone, "unlisted", str(e))
            await alert(app,
                f"{em.ALERT} **Session Connection Failed — Unlisted**\n\n"
                f"{em.USER} Requested by: `{cq.from_user.id}`\n"
                f"{em.PHONE} Number: `{phone}`\n"
                f"{em.ERROR} Error: `{str(e)[:200]}`"
            )
            await safe_edit(cq.message,
                f"{em.ERROR} Failed to connect `{mask_phone(phone)}`.\n\n"
                "This has been reported to the admins.",
                reply_markup=back_kb("main_menu"),
            )
            return

        pwd = session.get("password", "")
        if pwd:
            await safe_edit(cq.message, f"{em.LOADING} Verifying password...")
            ok, err = await clients.check_password(phone, pwd)
            if not ok:
                await clients.stop_session(phone)
                await db.set_session_status(phone, "unlisted", err)
                masked = mask_phone(phone)
                await alert(app,
                    f"{em.ALERT} **Password Check Failed — Unlisted**\n\n"
                    f"{em.USER} Requested by: `{cq.from_user.id}`\n"
                    f"{em.PHONE} Number: `{phone}`\n"
                    f"{em.ERROR} Error: `{err[:200]}`\n"
                    f"{em.PASSWORD} Stored password may be wrong or changed."
                )
                await safe_edit(cq.message,
                    f"{em.ERROR} Password verification failed for `{masked}`.\n\n"
                    "This has been reported to the admins.",
                    reply_markup=back_kb("main_menu"),
                )
                return

        if not await db.deduct_credits(cq.from_user.id, price):
            await clients.stop_session(phone)
            await safe_edit(cq.message,
                f"{em.ERROR} Could not deduct credits. Please try again or contact support.",
                reply_markup=back_kb("main_menu"),
            )
            return
        log.info("Deducted %d credits from user %d on selection", price, cq.from_user.id)

        clients.assign_number(phone, cq.from_user.id, OTP_TIMEOUT, price)

        uname = cq.from_user.username or cq.from_user.first_name or str(cq.from_user.id)
        flag = get_country_flag(cc)
        name = get_country_name(cc)
        credits = await db.get_credits(cq.from_user.id)
        await alert(app,
            f"{em.PHONE} **Number Purchased**\n\n"
            f"{em.USER} User: `{cq.from_user.id}` (@{uname})\n"
            f"{em.PHONE} Number: `{phone}`\n"
            f"{flag} Country: {name}\n"
            f"{em.MONEY} Price: **{price}** credits\n"
            f"{em.MONEY} Remaining balance: **{credits}**"
        )
        credit_line = f"\n{em.MONEY} Credits: {credits}"
        pwd = session.get("password", "")
        pwd_line = f"\n{em.PASSWORD} 2FA Password: `{pwd}`" if pwd else ""
        acc_year = session.get("account_year")
        age_line = f"\n{em.CALENDAR} Account created: ~{acc_year}" if acc_year else ""
        email_added = session.get("email_added", False)
        email_line = f"\n{em.MAIL} Email Added: {'Yes' if email_added else 'No'}"
        support = " | ".join(SUPPORT_HANDLES)
        await safe_edit(cq.message,
            f"{em.SUCCESS} **Number assigned!**\n\n"
            f"{flag} {name}\n"
            f"{em.PHONE} `{phone}`\n"
            f"{em.MONEY} Price: **{price}** credits (deducted)\n"
            f"{em.TIMER} Timeout: {OTP_TIMEOUT // 60} min{age_line}{email_line}{credit_line}{pwd_line}\n\n"
            "Any OTP received on this number will be forwarded to you.\n\n"
            f"{em.WARNING} On manual release, your credits will be locked for 2 hours.\n\n"
            f"{em.WARNING} Issues logging in? Contact support:\n{support}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{em.UNLOCK} Release Number", callback_data=f"release:{phone}", style=S.DANGER)],
            ]),
        )

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
            await safe_edit(cq.message,
                f"{em.UNLOCK} `{mask_phone(phone)}` released.\n\n"
                f"{em.MONEY} **{price} credits** will be refunded in **2 hours**.",
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
                f"Your received OTPs will appear here after you rent a number.",
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
            f"{em.LOGS} **Recent OTPs:**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(footer),
        )

    # ── Buy Credits ──

    @app.on_callback_query(filters.regex("^buy_credits$"))
    @verified
    async def cb_buy_credits(_, cq: CallbackQuery):
        auth_states.pop(cq.from_user.id, None)
        credits = await db.get_credits(cq.from_user.id)
        buttons = [
            [
                InlineKeyboardButton(f"{em.MONEY} Razorpay (UPI)", callback_data="rz_plans", style=S.SUCCESS),
                InlineKeyboardButton(f"{em.COIN} Crypto (USDT)", callback_data="cr_plans", style=S.PRIMARY),
            ],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="main_menu", style=S.DEFAULT)],
        ]
        await safe_edit(cq.message,
            f"{em.CREDIT} **Buy Credits**\n\n"
            f"{em.MONEY} Your balance: **{credits}**\n\n"
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
                plan["label"], callback_data=f"rz_pay:{key}", style=S.DEFAULT,
            )])
        buttons.append([InlineKeyboardButton(f"{em.EDIT} Custom Amount", callback_data="rz_custom", style=S.DEFAULT)])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="buy_credits", style=S.DEFAULT)])
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
                callback_data=f"cr_net:{key}", style=S.DEFAULT,
            )])
        buttons.append([InlineKeyboardButton(f"{em.EDIT} Custom Amount", callback_data="cr_custom", style=S.DEFAULT)])
        buttons.append([InlineKeyboardButton(f"{em.BACK} Back", callback_data="buy_credits", style=S.DEFAULT)])
        await safe_edit(cq.message,
            f"{em.COIN} **Crypto — Choose a plan:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^cr_net:"))
    @verified
    async def cb_cr_net(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        buttons = [
            [InlineKeyboardButton("BSC (BEP20)", callback_data=f"cr_addr:BSC:{plan_key}", style=S.DEFAULT)],
            [InlineKeyboardButton("TRC20 (TRON)", callback_data=f"cr_addr:TRX:{plan_key}", style=S.DEFAULT)],
            [InlineKeyboardButton("ERC20 (Ethereum)", callback_data=f"cr_addr:ETH:{plan_key}", style=S.DEFAULT)],
            [InlineKeyboardButton(f"{em.BACK} Back", callback_data="cr_plans", style=S.DEFAULT)],
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
        ok, info = await asyncio.to_thread(
            payments.get_binance_deposit_address, "USDT", network,
        )
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

    # ── Help / Cancel ──

    def _build_help_text(is_admin: bool) -> str:
        admin_section = (
            f"\n\n{em.GEAR} **Admin Commands:**\n"
            "  /addcred `<userid>` `<credits>` — Add credits to a user\n"
            "  /removecred `<userid>` `<credits>` — Remove credits from a user\n"
            "  /info `<userid or @username>` — Look up user details\n"
            "  /broadcast `<message>` — Broadcast to all users\n"
            "  /broadcast `-name` `<message>` — Broadcast with your name"
        ) if is_admin else ""
        return (
            f"{em.HELP} **Help — OTP Bot**\n\n"
            f"{em.PIN} **How it works:**\n"
            "  1. Buy credits via UPI or Crypto\n"
            "  2. Tap **Get Number** and select a country\n"
            "  3. Pick an available number — credits are deducted\n"
            "  4. OTP messages arriving on that number are forwarded to you\n"
            "  5. The number auto-releases after the timeout\n\n"
            f"{em.FAQ} **Features:**\n"
            f"  • {em.PHONE} **Get Number** — Rent a number to receive OTPs\n"
            f"  • {em.LOGS} **My History** — View your past sessions\n"
            f"  • {em.CREDIT} **Buy Credits** — Top up via UPI or USDT\n"
            f"  • {em.PHONE} **Support** — Contact our support agents\n\n"
            f"{em.SETTINGS} **Commands:**\n"
            "  /start — Main menu\n"
            "  /help — This help page\n"
            "  /cancel — Cancel current operation"
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
            f"1️⃣ **Top Up**: Buy credits via UPI or Crypto.\n"
            f"2️⃣ **Rent a Number**: Go to **Get Number**, select country, and rent.\n"
            f"3️⃣ **Receive OTP**: The bot will display incoming OTP messages instantly."
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

async def _account_info(client: Client) -> tuple[int | None, int | None, bool]:
    """Fetch account id + estimated creation year + has_email from a connected client."""
    try:
        me = await client.get_me()
        year = estimate_account_year(me.id)
        has_email = False
        try:
            pwd_info = await client.invoke(
                __import__("pyrogram").raw.functions.account.GetPassword()
            )
            login_email = getattr(pwd_info, "login_email_pattern", None)
            has_email = login_email is not None
        except Exception as e:
            log.warning("Failed to check email status: %s", e)
        return me.id, year, has_email
    except Exception:
        return None, None, False


async def _handle_phone(message: Message, phone: str):
    user_id = message.from_user.id
    if not phone.startswith("+"):
        phone = "+" + phone

    cc, cname, cflag = detect_country(phone)
    status_msg = await message.reply(f"{em.LOADING} Sending code to `{phone}` ({cflag} {cname})...")

    try:
        client = Client(
            name=f"auth_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )
        await client.connect()
        sent_code = await client.send_code(phone)
        auth_states[user_id] = {
            "step": "code",
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "country_code": cc,
        }
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code sent to `{phone}` ({cflag} {cname})\n\n"
            "Enter the verification code you received:\n\n"
            f"{em.FAQ} If Telegram sent it as a message, "
            "add spaces or dots between digits to avoid the code being blocked.\n"
            "Example: `1 2 3 4 5` or `1.2.3.4.5`",
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
        acc_id, acc_year, has_email = await _account_info(client)
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
        await safe_edit(status_msg,
            f"{em.SUCCESS} Code verified for `{phone}`\n\n"
            f"{em.GLOBE} Detected country: {cflag} **{cname}** ({cc})\n"
            f"{em.CALENDAR} Account year: **{acc_year or 'Unknown'}**\n"
            f"{em.MAIL} Email added: **{'Yes' if has_email else 'No'}**\n\n"
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
        acc_id, acc_year, has_email = await _account_info(client)
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
    auth_states.pop(user_id, None)
    
    flag = get_country_flag(cc)
    name = get_country_name(cc)
    email_str = "Yes" if email else "No"
    
    await message.reply(
        f"{em.SUCCESS} Category price successfully updated!\n\n"
        f"{em.GLOBE} Country: {flag} **{name}** ({cc})\n"
        f"{em.CALENDAR} Year: **{year}**\n"
        f"{em.MAIL} Email: **{email_str}**\n"
        f"{em.MONEY} New Price: **{price}** credits per OTP",
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
        await client.update_password(new_password=new_password, old_password=old_password)
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


# ── Payment helpers ──

async def _razorpay_poller(user_id: int, qr_id: str, plan_key: str, qr_msg):
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
            if not await db.mark_pending_payment_done(qr_id):
                return
            await db.add_credits(user_id, plan["credits"])
            await db.save_payment(user_id, "razorpay", plan_key, plan["amount_inr"] / 100, "INR", qr_id)
            await _check_referral_reward(user_id)
            new_balance = await db.get_credits(user_id)
            buyer = await db.get_user(user_id)
            buyer_name = (buyer.get("first_name") or buyer.get("username") or str(user_id)) if buyer else str(user_id)
            await alert(bot,
                f"{em.CREDIT} **Credits Purchased (Razorpay)**\n\n"
                f"{em.USER} User: `{user_id}` ({buyer_name})\n"
                f"{em.GIFT} Credits: +{plan['credits']}\n"
                f"{em.DOLLAR} Amount: ₹{plan['amount_inr'] // 100}\n"
                f"{em.MONEY} New balance: {new_balance}"
            )
            try:
                await qr_msg.delete()
            except Exception:
                pass
            await bot.send_message(
                user_id,
                f"{em.SUCCESS} **Payment received!**\n\n"
                f"{em.GIFT} +{plan['credits']} credits added\n"
                f"{em.MONEY} New balance: **{new_balance}**",
                reply_markup=back_kb("main_menu"),
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

    ok, reason = await asyncio.to_thread(
        payments.verify_binance_deposit, tx_hash, "USDT", pstate["amount_usdt"],
    )

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
    await _check_referral_reward(user_id)
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

    await message.reply(
        f"{em.SUCCESS} **Category price set and number added successfully!**\n\n"
        f"{em.PHONE} `{phone}` — {flag} {name}\n"
        f"{em.MONEY} Price: **{price}** credits per OTP",
        reply_markup=main_menu_kb(True),
    )
