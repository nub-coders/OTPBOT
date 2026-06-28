import asyncio
import logging
from pyrogram import Client, filters
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
from config import API_ID, API_HASH, BOT_TOKEN, OTP_TIMEOUT, CREDIT_PLANS, CRYPTO_PLANS, SUPPORT_HANDLES, CHAT_ID, ADMIN_IDS, UPDATES_CHANNEL, USDT_TO_INR
import database as db
import clients
import payments
from utils import detect_country, get_country_flag, get_country_name, search_country, estimate_account_year, mask_phone

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
            InlineKeyboardButton("📱 Get Number", callback_data="get_number"),
            InlineKeyboardButton("📜 My History", callback_data="my_history"),
        ],
        [InlineKeyboardButton("💳 Buy Credits", callback_data="buy_credits")],
        [
            InlineKeyboardButton("📞 Support", callback_data="support"),
        ],
    ]
    if UPDATES_CHANNEL:
        buttons[-1].append(InlineKeyboardButton("📢 Updates", url=UPDATES_CHANNEL))
    if is_admin:
        buttons.append(
            [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")]
        )
    return InlineKeyboardMarkup(buttons)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Number", callback_data="add_number"),
            InlineKeyboardButton("📋 List Numbers", callback_data="list_numbers"),
        ],
        [
            InlineKeyboardButton("💰 Country Pricing", callback_data="country_pricing"),
            InlineKeyboardButton("👥 Users", callback_data="users_list"),
        ],
        [
            InlineKeyboardButton("💰 Add Credits", callback_data="add_credits"),
            InlineKeyboardButton("📊 Stats", callback_data="stats"),
        ],
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_help")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ])


def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=target)],
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
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{cb_prefix}:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"{cb_prefix}:{page + 1}"))

    footer = []
    if nav:
        footer.append(nav)
    footer.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])

    page_label = f"\n\n📄 Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    return page_items, footer, page_label


# ── Handlers ──

def _register_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(_, message: Message):
        user_id = message.from_user.id
        user = await db.get_user(user_id)
        if not user:
            role = "admin" if await db.admin_count() == 0 else "user"
            await db.create_user(
                user_id,
                message.from_user.username,
                message.from_user.first_name,
                role,
            )
            if role == "admin":
                await message.reply(
                    "👑 **Welcome, Admin!**\n\n"
                    "You are the first user — you've been set as admin.\n"
                    "Use the panel below to manage numbers and users.",
                    reply_markup=main_menu_kb(True),
                )
                return

        is_adm = await db.is_admin(user_id)
        credits = await db.get_credits(user_id)
        credit_line = f"\n💰 Credits: **{credits}**"
        await message.reply(
            "👋 **Welcome to OTP Bot!**\n\n"
            "Get OTP codes from monitored Telegram numbers.\n"
            f"Choose an option below:{credit_line}",
            reply_markup=main_menu_kb(is_adm),
        )

    @app.on_callback_query(filters.regex("^main_menu$"))
    async def cb_main_menu(_, cq: CallbackQuery):
        is_adm = await db.is_admin(cq.from_user.id)
        credits = await db.get_credits(cq.from_user.id)
        credit_line = f"\n💰 Credits: **{credits}**"
        await safe_edit(cq.message,
            f"👋 **OTP Bot — Main Menu**\n\nChoose an option:{credit_line}",
            reply_markup=main_menu_kb(is_adm),
        )

    @app.on_callback_query(filters.regex("^support$"))
    async def cb_support(_, cq: CallbackQuery):
        lines = "\n".join(f"  • [{h.lstrip('@')}](https://t.me/{h.lstrip('@')})" for h in SUPPORT_HANDLES)
        await safe_edit(cq.message,
            f"📞 **Support**\n\n"
            f"Having issues? Contact any of our support agents:\n\n"
            f"{lines}\n\n"
            "We're here to help with purchases, login issues, or any questions.",
            reply_markup=back_kb(),
        )

    @app.on_callback_query(filters.regex("^admin_panel$"))
    async def cb_admin_panel(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return
        await safe_edit(cq.message, "⚙️ **Admin Panel**", reply_markup=admin_kb())

    # ── Add Number Flow ──

    @app.on_callback_query(filters.regex("^add_number$"))
    async def cb_add_number(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return
        auth_states[cq.from_user.id] = {"step": "phone"}
        await safe_edit(cq.message,
            "📱 **Add Number**\n\n"
            "Send the phone number in international format:\n"
            "Example: `+1234567890`\n\n"
            "Country and pricing will be detected automatically.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")],
            ]),
        )

    @app.on_callback_query(filters.regex("^cancel_auth$"))
    async def cb_cancel_auth(_, cq: CallbackQuery):
        state = auth_states.pop(cq.from_user.id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await safe_edit(cq.message, "❌ Cancelled.", reply_markup=back_kb("admin_panel"))

    # ── Country confirmation after adding number ──

    @app.on_callback_query(filters.regex("^cc_yes$"))
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
                f"💰 **New Category Detected!**\n\n"
                f"🌍 Country: {flag} **{name}** ({cc})\n"
                f"📅 Year: **{year}**\n"
                f"📧 Email Added: **{'Yes' if email_added else 'No'}**\n\n"
                f"This combination has no set price. Please send the price (in credits) for this category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")]
                ])
            )
            return

        await db.save_session(phone, state["session_string"], cq.from_user.id,
                              password=state.get("password", ""), country_code=cc,
                              account_id=state.get("account_id"), account_year=year,
                              email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added)
        auth_states.pop(cq.from_user.id, None)

        await safe_edit(cq.message,
            f"✅ **Number added successfully!**\n\n"
            f"📱 `{phone}` — {flag} {name}\n"
            f"💰 Price: **{price}** credits per OTP",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_callback_query(filters.regex("^cc_no$"))
    async def cb_cc_no(_, cq: CallbackQuery):
        state = auth_states.get(cq.from_user.id)
        if not state or state.get("step") != "confirm_country":
            await cq.answer("No pending action.", show_alert=True)
            return

        auth_states[cq.from_user.id]["step"] = "manual_country"
        await safe_edit(cq.message,
            f"🌍 **Select Country for** `{state['phone']}`\n\n"
            "Type the country name or send its flag emoji:\n"
            "Example: `India` or `🇮🇳`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^cc_pick:"))
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
                f"💰 **New Category Detected!**\n\n"
                f"🌍 Country: {flag} **{name}** ({cc})\n"
                f"📅 Year: **{year}**\n"
                f"📧 Email Added: **{'Yes' if email_added else 'No'}**\n\n"
                f"This combination has no set price. Please send the price (in credits) for this category:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")]
                ])
            )
            return

        await db.save_session(phone, state["session_string"], cq.from_user.id,
                              password=state.get("password", ""), country_code=cc,
                              account_id=state.get("account_id"), account_year=year,
                              email_added=email_added)
        await db.set_session_account_info(phone, state.get("account_id"), year, email_added)
        auth_states.pop(cq.from_user.id, None)

        await safe_edit(cq.message,
            f"✅ **Number added successfully!**\n\n"
            f"📱 `{phone}` — {flag} {name}\n"
            f"💰 Price: **{price}** credits per OTP",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.text & filters.private & ~filters.command([
        "start", "help", "cancel", "addcred", "broadcast",
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

    # ── Country Pricing ──

    @app.on_callback_query(filters.regex(r"^country_pricing$|^pg_cp:\d+$"))
    async def cb_country_pricing(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_cp:") else 0

        sessions = await db.get_all_sessions()
        prices = await db.get_all_country_prices()

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
                "💰 **Country Pricing**\n\nNo numbers added yet.",
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
            else:
                range_str = str(prices.get(cc, 1))

            all_lines.append(f"{flag} **{name}** ({cc}) — **({range_str} credits per OTP)** — {info['active']}/{info['total']} numbers")
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {name} — {range_str} cr",
                callback_data=f"setcprice:{cc}",
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_cp", "admin_panel")
        start = page * PAGE_SIZE
        page_lines = all_lines[start:start + PAGE_SIZE]
        await safe_edit(cq.message,
            "💰 **Country Pricing**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^setcprice:"))
    async def cb_setcprice(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
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
                
                lines.append(f"📅 Year: **{year}** | 📧 Email: **{email_str}** — **{price}** cr")
                buttons.append([InlineKeyboardButton(
                    f"✏️ {year} | Email: {email_str} — {price} cr",
                    callback_data=f"editcat:{cc}:{year}:{email}",
                )])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="country_pricing")])

        await safe_edit(cq.message,
            f"💰 **Category Pricing — {flag} {name} ({cc})**\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^editcat:"))
    async def cb_editcat(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
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
            f"💰 **Update Category Price**\n\n"
            f"🌍 Country: {get_country_flag(cc)} {get_country_name(cc)}\n"
            f"📅 Year: **{year}**\n"
            f"📧 Email Added: **{email_str}**\n\n"
            f"Send the new price (in credits) for this category:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data=f"setcprice:{cc}")]
            ])
        )

    # ── List Numbers (Admin) ──

    @app.on_callback_query(filters.regex(r"^list_numbers$|^pg_ln:\d+$"))
    async def cb_list_numbers(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ln:") else 0

        sessions = await db.get_all_sessions()
        if not sessions:
            await safe_edit(cq.message,
                "📋 **No numbers added yet.**",
                reply_markup=back_kb("admin_panel"),
            )
            return

        prices = await db.get_all_country_prices()
        by_country = {}
        for s in sessions:
            cc = s.get("country_code", "XX")
            by_country.setdefault(cc, []).append(s)

        all_entries = []
        for cc in sorted(by_country.keys()):
            flag = get_country_flag(cc)
            name = get_country_name(cc)
            price = prices.get(cc, 1)
            header = f"\n{flag} **{name}** — {price} cr/OTP"
            for s in by_country[cc]:
                phone = s["phone_number"]
                status_icon = {"active": "🟢", "sold": "🔴", "error": "⚠️"}.get(s.get("status"), "⚪")
                assigned = clients.get_request_user(phone)
                assigned_text = f" → user `{assigned}`" if assigned else ""
                error_text = f"\n  └ ❗ {s['last_error'][:80]}" if s.get("last_error") else ""
                acc_year = s.get("account_year")
                age_text = f" — 📅 ~{acc_year}" if acc_year else ""
                line = f"  {status_icon} `{phone}`{age_text}{assigned_text}{error_text}"
                btn = [InlineKeyboardButton(f"🔍 {phone}", callback_data=f"num_actions:{phone}")]
                all_entries.append({"header": header, "line": line, "btn": btn})
                header = None

        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_entries = all_entries[start:end]

        lines = ["📋 **Registered Numbers:**\n"]
        page_btns = []
        seen_headers = set()
        for e in page_entries:
            if e["header"] and e["header"] not in seen_headers:
                lines.append(e["header"])
                seen_headers.add(e["header"])
            lines.append(e["line"])
            page_btns.append(e["btn"])

        total_pages = (len(all_entries) + PAGE_SIZE - 1) // PAGE_SIZE
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg_ln:{page - 1}"))
        if end < len(all_entries):
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"pg_ln:{page + 1}"))
        if nav:
            page_btns.append(nav)
        page_label = f"\n\n📄 Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        page_btns.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

        await safe_edit(cq.message,
            "\n".join(lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns),
        )

    @app.on_callback_query(filters.regex(r"^rm:"))
    async def cb_remove_number(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        await safe_edit(cq.message,
            f"⚠️ Remove `{phone}` and disconnect its session?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes", callback_data=f"confirm_rm:{phone}"),
                    InlineKeyboardButton("❌ No", callback_data="list_numbers"),
                ],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^confirm_rm:"))
    async def cb_confirm_remove(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        await clients.remove_client(phone)
        await safe_edit(cq.message,
            f"✅ `{phone}` removed.", reply_markup=back_kb("admin_panel")
        )

    # ── Per-number actions ──

    @app.on_callback_query(filters.regex(r"^num_actions:"))
    async def cb_num_actions(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
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
        status = session.get("status", "unknown")
        pwd = session.get("password", "")
        error = session.get("last_error", "")
        acc_year = session.get("account_year")
        age_line = f"📅 **Account:** created ~{acc_year}\n" if acc_year else ""
        email_added = session.get("email_added", False)
        email_line = f"📧 **Email Added:** {'Yes' if email_added else 'No'}\n"

        info = (
            f"📱 **Number:** `{phone}`\n"
            f"{flag} **Country:** {name} ({cc})\n"
            f"📊 Status: **{status}**\n"
            f"💰 Price: **{price}** credits\n"
            f"{age_line}"
            f"{email_line}"
            f"🔐 Password: {'`' + pwd + '`' if pwd else 'Not set'}\n"
        )
        if error:
            info += f"❗ Last error: `{error[:120]}`\n"

        buttons = [
            [
                InlineKeyboardButton("🔍 Verify", callback_data=f"verify:{phone}"),
                InlineKeyboardButton("🔐 Update Password", callback_data=f"updpwd:{phone}"),
            ],
            [
                InlineKeyboardButton("❌ Remove", callback_data=f"rm:{phone}"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="list_numbers")],
        ]
        await safe_edit(cq.message, info, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(filters.regex(r"^verify:"))
    async def cb_verify(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await safe_edit(cq.message, f"⏳ Verifying `{phone}`...")

        ok, error = await clients.verify_session(phone, session["session_string"])
        if ok:
            await db.set_session_status(phone, "active")
            await safe_edit(cq.message,
                f"✅ `{phone}` — session is **valid**!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data=f"num_actions:{phone}")],
                ]),
            )
        else:
            await db.set_session_status(phone, "error", error)
            await safe_edit(cq.message,
                f"❌ `{phone}` — verification failed\n\n"
                f"❗ Error: `{error[:200]}`\n\n"
                "Would you like to re-add this number?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Re-add Number", callback_data=f"readd:{phone}")],
                    [InlineKeyboardButton("🔙 Back", callback_data=f"num_actions:{phone}")],
                ]),
            )

    @app.on_callback_query(filters.regex(r"^readd:"))
    async def cb_readd(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        old_session = await db.get_session(phone)
        old_cc = old_session.get("country_code", "XX") if old_session else "XX"
        auth_states[cq.from_user.id] = {"step": "phone", "prefill_phone": phone, "old_country": old_cc}
        await safe_edit(cq.message,
            f"🔄 **Re-adding** `{phone}`\n\n"
            "A new code will be sent. Enter the verification code when received.",
        )
        await _handle_phone_direct(cq.from_user.id, phone, cq.message)

    @app.on_callback_query(filters.regex(r"^updpwd:"))
    async def cb_updpwd(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        phone = cq.data.split(":", 1)[1]
        session = await db.get_session(phone)
        if not session:
            await cq.answer("Number not found.", show_alert=True)
            return

        await safe_edit(cq.message, f"⏳ Connecting to `{phone}`...")

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
                f"❌ Failed to connect: `{e}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data=f"num_actions:{phone}")],
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
                f"🔐 **Update Password for** `{phone}`\n\n"
                f"Current stored password: `{session['password']}`\n\n"
                "Send the **current 2FA password** to verify:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")],
                ]),
            )
        else:
            await safe_edit(cq.message,
                f"🔐 **Update Password for** `{phone}`\n\n"
                "No password stored. Send the **current 2FA password**:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")],
                ]),
            )

    # ── Users ──

    @app.on_callback_query(filters.regex(r"^users_list$|^pg_ul:\d+$"))
    async def cb_users_list(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_ul:") else 0

        users = await db.get_all_users()
        if not users:
            await safe_edit(cq.message,
                "👥 **No users yet.**", reply_markup=back_kb("admin_panel")
            )
            return

        all_lines = []
        all_buttons = []
        for u in users:
            role_icon = "👑" if u["role"] == "admin" else "👤"
            name = u.get("first_name") or u.get("username") or str(u["telegram_id"])
            credits = u.get("credits", 0)
            all_lines.append(f"{role_icon} {name} — `{u['telegram_id']}` — 💰 {credits}")
            if u["role"] != "admin":
                all_buttons.append([
                    InlineKeyboardButton(
                        f"💰 Add credits: {name}",
                        callback_data=f"cr:{u['telegram_id']}",
                    )
                ])
            else:
                all_buttons.append(None)

        page_lines = all_lines[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        page_raw = all_buttons[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        page_btns = [b for b in page_raw if b is not None]

        total_pages = (len(all_lines) + PAGE_SIZE - 1) // PAGE_SIZE
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg_ul:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(all_lines):
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"pg_ul:{page + 1}"))
        if nav:
            page_btns.append(nav)
        page_label = f"\n\n📄 Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        page_btns.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

        await safe_edit(cq.message,
            "👥 **Users:**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns),
        )

    # ── Credits ──

    @app.on_callback_query(filters.regex("^add_credits$"))
    async def cb_add_credits(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        await safe_edit(cq.message,
            "💰 **Add Credits**\n\n"
            "Use the command:\n"
            "`/addcred <userid> <credits>`\n\n"
            "**Example:**\n"
            "`/addcred 123456789 50`\n\n"
            "You can find user IDs in the **Users** section.",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.command("addcred") & filters.private)
    async def cmd_addcred(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply("⛔ Admin only.")
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
            await message.reply("❌ Invalid user ID.")
            return

        try:
            amount = int(parts[2])
            if amount <= 0:
                await message.reply("❌ Credits must be a positive number.")
                return
        except ValueError:
            await message.reply("❌ Invalid credits amount.")
            return

        target = await db.get_user(target_id)
        if not target:
            await message.reply(f"❌ User `{target_id}` not found.")
            return

        await db.add_credits(target_id, amount)
        new_balance = await db.get_credits(target_id)
        name = target.get("first_name") or target.get("username") or str(target_id)

        await message.reply(
            f"✅ **Credits added!**\n\n"
            f"👤 User: **{name}**\n"
            f"➕ Added: **{amount}**\n"
            f"💰 New balance: **{new_balance}**",
        )

        try:
            await bot.send_message(
                target_id,
                f"💰 **Credits added!**\n\n"
                f"➕ {amount} credits added to your account.\n"
                f"💰 New balance: **{new_balance}**",
            )
        except Exception:
            pass

    # ── Broadcast ──

    @app.on_callback_query(filters.regex("^broadcast_help$"))
    async def cb_broadcast_help(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return
        await safe_edit(cq.message,
            "📢 **Broadcast Message**\n\n"
            "Reply to any message with:\n\n"
            "`/broadcast` — copies the message to all users (no sender shown)\n"
            "`/broadcast -name` — forwards the message (original sender visible)\n\n"
            "📌 Must be used as a **reply** to the message you want to broadcast.",
            reply_markup=back_kb("admin_panel"),
        )

    @app.on_message(filters.command("broadcast") & filters.private)
    async def cmd_broadcast(_, message: Message):
        if not await db.is_admin(message.from_user.id):
            await message.reply("⛔ Admin only.")
            return

        target = message.reply_to_message
        if not target:
            await message.reply(
                "❌ **Reply to a message** to broadcast it.\n\n"
                "`/broadcast` — copy (no sender shown)\n"
                "`/broadcast -name` — forward (sender visible)"
            )
            return

        args = message.text.split(None, 1)
        flag = args[1].strip().lower() if len(args) > 1 else ""
        include_name = flag == "-name"

        if flag and not include_name:
            await message.reply("❌ Unknown flag. Use `/broadcast` or `/broadcast -name`.")
            return

        users = await db.get_all_users()
        status_msg = await message.reply(f"⏳ Broadcasting to {len(users)} users...")

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
            f"✅ **Broadcast complete!**\n\n"
            f"📨 Sent: **{sent}**\n"
            f"❌ Failed: **{failed}**",
        )

    # ── Stats ──

    @app.on_callback_query(filters.regex("^stats$"))
    async def cb_stats(_, cq: CallbackQuery):
        if not await db.is_admin(cq.from_user.id):
            await cq.answer("⛔ Admin only.", show_alert=True)
            return

        s = await db.get_stats()
        ps = await db.get_payment_stats()
        active = len(clients.active_clients)
        assigned = len(clients.active_requests)

        pay_lines = ""
        for method, info in ps.get("by_method", {}).items():
            pay_lines += f"\n  {method}: {info['count']} payments, {info['total']:.2f}"

        await safe_edit(cq.message,
            f"📊 **Statistics**\n\n"
            f"👥 Users: {s['users']}\n"
            f"📱 Numbers (DB): {s['sessions']}\n"
            f"🟢 Connected: {active}\n"
            f"🔗 Assigned now: {assigned}\n"
            f"📨 OTPs forwarded: {s['otps']}\n\n"
            f"💳 **Payments:** {ps['total_payments']}{pay_lines}",
            reply_markup=back_kb("admin_panel"),
        )

    # ── Get Number (User) — Country-based ──

    @app.on_callback_query(filters.regex(r"^get_number$|^pg_gn:\d+$"))
    async def cb_get_number(_, cq: CallbackQuery):
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_gn:") else 0

        sessions = await db.get_active_sessions()
        if not sessions:
            await safe_edit(cq.message,
                "📱 **No numbers available right now.**\n"
                "Contact admin to add numbers.",
                reply_markup=back_kb("main_menu"),
            )
            return

        by_country = {}
        for s in sessions:
            cc = s.get("country_code", "XX")
            by_country.setdefault(cc, []).append(s)

        all_buttons = []
        all_lines = []
        for cc in sorted(by_country.keys()):
            flag = get_country_flag(cc)
            name = get_country_name(cc)
            nums = by_country[cc]
            
            session_prices = []
            for s in nums:
                p = await db.get_session_price(s)
                session_prices.append(p)
            min_p = min(session_prices) if session_prices else 1
            max_p = max(session_prices) if session_prices else 1
            range_str = f"({min_p}-{max_p})" if min_p != max_p else f"{min_p}"
            
            available = sum(1 for s in nums if not clients.get_request_user(s["phone_number"]))
            all_lines.append(f"{flag} {name} — **{range_str}** cr — {available} available")
            all_buttons.append([InlineKeyboardButton(
                f"{flag} {name} — {range_str} cr ({available})",
                callback_data=f"country:{cc}",
            )])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, "pg_gn", "main_menu")
        start = page * PAGE_SIZE
        page_lines = all_lines[start:start + PAGE_SIZE]
        await safe_edit(cq.message,
            "🌍 **Select a Country**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex(r"^country:[A-Z]+$|^pg_cn:[A-Z]+:\d+$"))
    async def cb_country(_, cq: CallbackQuery):
        if cq.data.startswith("pg_cn:"):
            parts = cq.data.split(":")
            cc, page = parts[1], int(parts[2])
        else:
            cc = cq.data.split(":", 1)[1]
            page = 0

        sessions = await db.get_active_sessions_by_country(cc)
        if not sessions:
            await cq.answer("No numbers available for this country.", show_alert=True)
            return

        flag = get_country_flag(cc)
        name = get_country_name(cc)

        session_prices = []
        for s in sessions:
            p = await db.get_session_price(s)
            session_prices.append(p)
        min_p = min(session_prices) if session_prices else 1
        max_p = max(session_prices) if session_prices else 1
        range_str = f"({min_p}-{max_p})" if min_p != max_p else f"{min_p}"

        all_buttons = []
        for s in sessions:
            phone = s["phone_number"]
            masked = mask_phone(phone)
            year = s.get("account_year")
            year_str = f" ({year})" if year else ""
            assigned = clients.get_request_user(phone)
            if assigned:
                all_buttons.append([
                    InlineKeyboardButton(f"🔴 {masked}{year_str} (in use)", callback_data="noop")
                ])
            else:
                all_buttons.append([
                    InlineKeyboardButton(
                        f"🟢 {masked}{year_str}", callback_data=f"sel:{phone}"
                    )
                ])

        page_btns, footer, page_label = paginate_buttons(all_buttons, page, f"pg_cn:{cc}", "get_number")
        await safe_edit(cq.message,
            f"{flag} **{name}** — **{range_str}** credits per OTP\n\n"
            f"Select a number:\n"
            f"⏱ Timeout: {OTP_TIMEOUT // 60} minutes.{page_label}\n\n"
            f"ℹ️ **Note:** Your credit will be deducted on choosing the number\n"
            f"and will be refunded after 2 hours when manual release.",
            reply_markup=InlineKeyboardMarkup(page_btns + footer),
        )

    @app.on_callback_query(filters.regex("^noop$"))
    async def cb_noop(_, cq: CallbackQuery):
        await cq.answer("This number is currently in use.", show_alert=True)

    @app.on_callback_query(filters.regex(r"^sel:"))
    async def cb_select_number(_, cq: CallbackQuery):
        phone = cq.data.split(":", 1)[1]

        user = await db.get_user(cq.from_user.id)
        if not user:
            await cq.answer("Please /start the bot first.", show_alert=True)
            return

        session = await db.get_session(phone)
        if not session or session.get("status") != "active":
            await cq.answer("❌ Number not available.", show_alert=True)
            return

        existing = clients.get_request_user(phone)
        if existing and existing != cq.from_user.id:
            await cq.answer("🔴 Already assigned to someone else.", show_alert=True)
            return

        cc = session.get("country_code", "XX")
        price = await db.get_session_price(session)
        credits = await db.get_credits(cq.from_user.id)
        if credits < price:
            await cq.answer(
                f"❌ You need {price} credits but have {credits}. Buy more credits!",
                show_alert=True,
            )
            return

        await safe_edit(cq.message, "⏳ Connecting session...")

        try:
            await clients.start_session(phone, session["session_string"])
        except Exception as e:
            log.error("Failed to start session %s: %s", phone, e)
            await safe_edit(cq.message,
                f"❌ Failed to connect `{mask_phone(phone)}`: `{e}`",
                reply_markup=back_kb("main_menu"),
            )
            return

        pwd = session.get("password", "")
        if pwd:
            await safe_edit(cq.message, "⏳ Verifying password...")
            ok, err = await clients.check_password(phone, pwd)
            if not ok:
                await clients.stop_session(phone)
                await db.set_session_status(phone, "password_failed", err)
                masked = mask_phone(phone)
                await alert(app,
                    f"🚨 **Password Check Failed**\n\n"
                    f"📱 Number: `{phone}`\n"
                    f"❌ Error: `{err[:200]}`\n"
                    f"🔐 Stored password may be wrong or changed."
                )
                await safe_edit(cq.message,
                    f"❌ Password verification failed for `{masked}`.\n\n"
                    "This has been reported to the admins.",
                    reply_markup=back_kb("main_menu"),
                )
                return

        if not await db.deduct_credits(cq.from_user.id, price):
            await clients.stop_session(phone)
            await safe_edit(cq.message,
                "❌ Failed to deduct credits. Try again.",
                reply_markup=back_kb("main_menu"),
            )
            return
        log.info("Deducted %d credits from user %d on selection", price, cq.from_user.id)

        clients.assign_number(phone, cq.from_user.id, OTP_TIMEOUT, price)

        flag = get_country_flag(cc)
        name = get_country_name(cc)
        credits = await db.get_credits(cq.from_user.id)
        credit_line = f"\n💰 Credits: {credits}"
        pwd = session.get("password", "")
        pwd_line = f"\n🔐 2FA Password: `{pwd}`" if pwd else ""
        acc_year = session.get("account_year")
        age_line = f"\n📅 Account created: ~{acc_year}" if acc_year else ""
        support = " | ".join(SUPPORT_HANDLES)
        await safe_edit(cq.message,
            f"✅ **Number assigned!**\n\n"
            f"{flag} {name}\n"
            f"📱 `{phone}`\n"
            f"💰 Price: **{price}** credits (deducted)\n"
            f"⏱ Timeout: {OTP_TIMEOUT // 60} min{age_line}{credit_line}{pwd_line}\n\n"
            "Any OTP received on this number will be forwarded to you.\n\n"
            "⚠️ On manual release, your credits will be locked for 2 hours.\n\n"
            f"⚠️ Issues logging in? Contact support:\n{support}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Release Number", callback_data=f"release:{phone}")],
                [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
            ]),
        )

    @app.on_callback_query(filters.regex(r"^release:"))
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

        clients.release_number(phone)
        await clients.stop_session(phone)

        if otp_received:
            await db.mark_session_sold(phone, user_id)
            await safe_edit(cq.message,
                f"🔓 `{mask_phone(phone)}` released.",
                reply_markup=back_kb("main_menu"),
            )
        else:
            if price > 0:
                await db.add_credits(user_id, price)
            await safe_edit(cq.message,
                f"🔓 `{mask_phone(phone)}` released.\n\n"
                f"💰 **{price} credits** refunded.",
                reply_markup=back_kb("main_menu"),
            )

    # ── OTP History ──

    @app.on_callback_query(filters.regex(r"^my_history$|^pg_mh:\d+$"))
    async def cb_history(_, cq: CallbackQuery):
        page = int(cq.data.split(":")[1]) if cq.data.startswith("pg_mh:") else 0

        otps = await db.get_user_otps(cq.from_user.id, limit=200)
        if not otps:
            await safe_edit(cq.message,
                "📜 **No OTP history yet.**",
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
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg_mh:{page - 1}"))
        if end < len(all_lines):
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"pg_mh:{page + 1}"))

        footer = []
        if nav:
            footer.append(nav)
        footer.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
        page_label = f"\n\n📄 Page {page + 1}/{total_pages}" if total_pages > 1 else ""

        await safe_edit(cq.message,
            "📜 **Recent OTPs:**\n\n" + "\n".join(page_lines) + page_label,
            reply_markup=InlineKeyboardMarkup(footer),
        )

    # ── Buy Credits ──

    @app.on_callback_query(filters.regex("^buy_credits$"))
    async def cb_buy_credits(_, cq: CallbackQuery):
        auth_states.pop(cq.from_user.id, None)
        credits = await db.get_credits(cq.from_user.id)
        buttons = [
            [
                InlineKeyboardButton("💸 Razorpay (UPI)", callback_data="rz_plans"),
                InlineKeyboardButton("🪙 Crypto (USDT)", callback_data="cr_plans"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
        ]
        await safe_edit(cq.message,
            f"💳 **Buy Credits**\n\n"
            f"💰 Your balance: **{credits}**\n\n"
            "Choose a payment method:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Razorpay Plans ──

    @app.on_callback_query(filters.regex("^rz_plans$"))
    async def cb_rz_plans(_, cq: CallbackQuery):
        buttons = []
        for key, plan in CREDIT_PLANS.items():
            buttons.append([InlineKeyboardButton(
                plan["label"], callback_data=f"rz_pay:{key}",
            )])
        buttons.append([InlineKeyboardButton("✏️ Custom Amount", callback_data="rz_custom")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="buy_credits")])
        await safe_edit(cq.message,
            "💸 **Razorpay — Choose a plan:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^rz_pay:"))
    async def cb_rz_pay(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        plan = get_credit_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)

        await safe_edit(cq.message, "⏳ Generating QR code...")
        qr = await asyncio.to_thread(
            payments.create_razorpay_qr, plan["label"], plan["amount_inr"], cq.from_user.id,
        )
        if not qr:
            return await safe_edit(cq.message,
                "❌ Payment gateway error. Try later.",
                reply_markup=back_kb("buy_credits"),
            )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"rz_check:{qr['id']}:{plan_key}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="buy_credits")],
        ])

        try:
            await cq.message.delete()
        except Exception:
            pass

        qr_msg = await bot.send_photo(
            cq.from_user.id,
            photo=qr["image_url"],
            caption=(
                f"📱 **Scan to pay ₹{plan['amount_inr'] // 100}**\n"
                f"🎁 You'll receive **{plan['credits']} credits**\n\n"
                "⏱ Valid for 15 minutes."
            ),
            reply_markup=buttons,
        )

        asyncio.create_task(_razorpay_poller(
            cq.from_user.id, qr["id"], plan_key, qr_msg,
        ))

    @app.on_callback_query(filters.regex(r"^rz_check:"))
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
            await cq.answer("✅ Payment received!", show_alert=True)
        elif status == "expired":
            await cq.answer("❌ QR expired. Generate a new one.", show_alert=True)
        else:
            await cq.answer("⏳ Payment not detected yet. Wait a moment.", show_alert=True)

    # ── Crypto Plans ──

    @app.on_callback_query(filters.regex("^cr_plans$"))
    async def cb_cr_plans(_, cq: CallbackQuery):
        buttons = []
        for key, plan in CRYPTO_PLANS.items():
            buttons.append([InlineKeyboardButton(
                f"{plan['credits']} Credits — {plan['amount_usdt']} USDT",
                callback_data=f"cr_net:{key}",
            )])
        buttons.append([InlineKeyboardButton("✏️ Custom Amount", callback_data="cr_custom")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="buy_credits")])
        await safe_edit(cq.message,
            "🪙 **Crypto — Choose a plan:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^cr_net:"))
    async def cb_cr_net(_, cq: CallbackQuery):
        plan_key = cq.data.split(":", 1)[1]
        buttons = [
            [InlineKeyboardButton("BSC (BEP20)", callback_data=f"cr_addr:BSC:{plan_key}")],
            [InlineKeyboardButton("TRC20 (TRON)", callback_data=f"cr_addr:TRX:{plan_key}")],
            [InlineKeyboardButton("ERC20 (Ethereum)", callback_data=f"cr_addr:ETH:{plan_key}")],
            [InlineKeyboardButton("🔙 Back", callback_data="cr_plans")],
        ]
        await safe_edit(cq.message,
            "🌐 **Select network for USDT deposit:**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^cr_addr:"))
    async def cb_cr_addr(_, cq: CallbackQuery):
        parts = cq.data.split(":")
        network, plan_key = parts[1], parts[2]
        plan = get_crypto_plan(plan_key)
        if not plan:
            return await cq.answer("Invalid plan.", show_alert=True)

        await safe_edit(cq.message, "⏳ Fetching deposit address...")
        ok, info = await asyncio.to_thread(
            payments.get_binance_deposit_address, "USDT", network,
        )
        if not ok:
            return await safe_edit(cq.message,
                f"❌ Could not fetch address: {info.get('error')}\nTry later.",
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
            f"🪙 **USDT Deposit**\n\n"
            f"Send **{plan['amount_usdt']} USDT** on **{net_label.get(network, network)}** to:\n\n"
            f"`{address}`\n"
            + (f"Memo/Tag: `{tag}`\n" if tag else "") +
            f"\nAfter sending, **reply with your TX hash** here.\n"
            f"Type `cancel` to abort.\n\n"
            f"🎁 You'll receive **{plan['credits']} credits**"
        )
        await safe_edit(cq.message,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")],
            ]),
        )

    @app.on_callback_query(filters.regex("^cancel_pay$"))
    async def cb_cancel_pay(_, cq: CallbackQuery):
        pay_states.pop(cq.from_user.id, None)
        await safe_edit(cq.message, "❌ Payment cancelled.", reply_markup=back_kb("main_menu"))

    @app.on_callback_query(filters.regex("^rz_custom$"))
    async def cb_rz_custom(_, cq: CallbackQuery):
        auth_states[cq.from_user.id] = {"step": "rz_custom_amount"}
        await safe_edit(cq.message,
            "💬 **Enter the number of credits you want to purchase (minimum 10):**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="buy_credits")]
            ])
        )

    @app.on_callback_query(filters.regex("^cr_custom$"))
    async def cb_cr_custom(_, cq: CallbackQuery):
        auth_states[cq.from_user.id] = {"step": "cr_custom_amount"}
        await safe_edit(cq.message,
            "💬 **Enter the number of credits you want to purchase (minimum 10):**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="buy_credits")]
            ])
        )

    # ── Help / Cancel ──

    @app.on_message(filters.command("help") & filters.private)
    async def cmd_help(_, message: Message):
        is_adm = await db.is_admin(message.from_user.id)
        admin_section = (
            "\n**Admin Commands:**\n"
            "/addcred <userid> <credits> — Add credits to a user\n"
            "/broadcast <message> — Broadcast to all users (no name)\n"
            "/broadcast -name <message> — Broadcast with your name\n"
        ) if is_adm else ""
        await message.reply(
            "**OTP Bot Help**\n\n"
            "/start — Main menu\n"
            "/help — This message\n"
            "/cancel — Cancel current operation\n\n"
            "**How it works:**\n"
            "1. Admin adds Telegram numbers via the bot\n"
            "2. Select a country, then pick a number\n"
            "3. OTP messages arriving on that number are forwarded to you\n"
            "4. The number auto-releases after timeout"
            f"{admin_section}",
        )

    @app.on_message(filters.command("cancel") & filters.private)
    async def cmd_cancel(_, message: Message):
        state = auth_states.pop(message.from_user.id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await message.reply("Cancelled.", reply_markup=main_menu_kb(
            await db.is_admin(message.from_user.id)
        ))


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
    status_msg = await message.reply(f"⏳ Sending code to `{phone}` ({cflag} {cname})...")

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
            f"✅ Code sent to `{phone}` ({cflag} {cname})\n\n"
            "Enter the verification code you received:\n\n"
            "💡 If Telegram sent it as a message, "
            "add spaces or dots between digits to avoid the code being blocked.\n"
            "Example: `1 2 3 4 5` or `1.2.3.4.5`",
        )
    except PhoneNumberInvalid:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            "❌ Invalid phone number format.",
            reply_markup=back_kb("admin_panel"),
        )
    except FloodWait as e:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"⚠️ FloodWait — try again in {e.value} seconds.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"❌ Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_code(message: Message, code: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    clean_code = code.replace(" ", "").replace(".", "").replace("-", "")
    status_msg = await message.reply("⏳ Verifying code...")

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
            f"✅ Code verified for `{phone}`\n\n"
            f"🌍 Detected country: {cflag} **{cname}** ({cc})\n\n"
            "Is this correct?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Yes, {cflag} {cname}", callback_data="cc_yes"),
                    InlineKeyboardButton("❌ No", callback_data="cc_no"),
                ],
            ]),
        )
    except SessionPasswordNeeded:
        auth_states[user_id]["step"] = "password"
        await safe_edit(status_msg,
            "🔐 This account has 2FA enabled.\n"
            "Enter the 2FA password:",
        )
    except PhoneCodeInvalid:
        await safe_edit(status_msg, "❌ Invalid code. Try again:")
    except PhoneCodeExpired:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            "❌ Code expired. Start over.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"❌ Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_password(message: Message, password: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]

    status_msg = await message.reply("⏳ Checking password...")

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
            f"✅ Password accepted for `{phone}`\n\n"
            f"🌍 Detected country: {cflag} **{cname}** ({cc})\n\n"
            "Is this correct?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Yes, {cflag} {cname}", callback_data="cc_yes"),
                    InlineKeyboardButton("❌ No", callback_data="cc_no"),
                ],
            ]),
        )
    except PasswordHashInvalid:
        await safe_edit(status_msg, "❌ Wrong password. Try again:")
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"❌ Error: `{e}`",
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
            f"🔄 **Re-adding** `{phone}` ({cflag} {cname})\n\n"
            "✅ Code sent. Enter the verification code:\n\n"
            "💡 Add spaces or dots between digits.\n"
            "Example: `1 2 3 4 5` or `1.2.3.4.5`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")],
            ]),
        )
    except FloodWait as e:
        auth_states.pop(user_id, None)
        await safe_edit(reply_target,
            f"⚠️ FloodWait — try again in {e.value} seconds.",
            reply_markup=back_kb("admin_panel"),
        )
    except Exception as e:
        auth_states.pop(user_id, None)
        await safe_edit(reply_target,
            f"❌ Error: `{e}`",
            reply_markup=back_kb("admin_panel"),
        )


async def _handle_update_category_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    try:
        price = int(text.strip())
        if price < 1:
            await message.reply("❌ Price must be at least 1. Try again:")
            return
    except ValueError:
        await message.reply("❌ Invalid price. Send a number (e.g. `220`):")
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
        f"✅ Category price successfully updated!\n\n"
        f"🌍 Country: {flag} **{name}** ({cc})\n"
        f"📅 Year: **{year}**\n"
        f"📧 Email: **{email_str}**\n"
        f"💰 New Price: **{price}** credits per OTP",
        reply_markup=main_menu_kb(True),
    )





async def _handle_manual_country(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]

    matches = search_country(text)
    if not matches:
        await message.reply(
            "❌ No matching country found.\n"
            "Try the full country name (e.g. `India`) or send its flag emoji 🇮🇳:",
        )
        return

    if len(matches) == 1:
        cc, name, flag = matches[0]
        state["country_code"] = cc
        state["step"] = "confirm_country"
        await message.reply(
            f"🌍 Found: {flag} **{name}** ({cc})\n\n"
            f"Confirm this country for `{state['phone']}`?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Yes, {flag} {name}", callback_data=f"cc_pick:{cc}"),
                    InlineKeyboardButton("❌ No", callback_data="cc_no"),
                ],
            ]),
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{flag} {name}", callback_data=f"cc_pick:{cc}")]
        for cc, name, flag in matches
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_auth")])
    await message.reply(
        "🌍 **Multiple matches found.** Pick one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_update_password_old(message: Message, text: str):
    user_id = message.from_user.id
    auth_states[user_id]["old_password"] = text.strip()
    auth_states[user_id]["step"] = "update_password_new"
    await message.reply("✅ Got it. Now send the **new 2FA password**:")


async def _handle_update_password_new(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    client: Client = state["client"]
    phone = state["phone"]
    old_password = state.get("old_password", "")

    new_password = text.strip()
    status_msg = await message.reply("⏳ Updating password on Telegram...")

    try:
        await client.update_password(new_password=new_password, old_password=old_password)
        await client.stop()
        await db.set_session_password(phone, new_password)
        auth_states.pop(user_id, None)
        await safe_edit(status_msg,
            f"✅ Password updated for `{phone}`\n\n"
            f"🔐 New password: `{new_password}`",
            reply_markup=back_kb("admin_panel"),
        )
    except PasswordHashInvalid:
        await safe_edit(status_msg,
            "❌ The old password was wrong. Send the correct **current 2FA password**:",
        )
        auth_states[user_id]["step"] = "update_password_old"
    except Exception as e:
        auth_states.pop(user_id, None)
        try:
            await client.stop()
        except Exception:
            pass
        await safe_edit(status_msg,
            f"❌ Error updating password: `{e}`",
            reply_markup=back_kb("admin_panel"),
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
            await db.add_credits(user_id, plan["credits"])
            await db.save_payment(user_id, "razorpay", plan_key, plan["amount_inr"] / 100, "INR", qr_id)
            new_balance = await db.get_credits(user_id)
            try:
                await qr_msg.delete()
            except Exception:
                pass
            await bot.send_message(
                user_id,
                f"✅ **Payment received!**\n\n"
                f"🎁 +{plan['credits']} credits added\n"
                f"💰 New balance: **{new_balance}**",
                reply_markup=back_kb("main_menu"),
            )
            return
        if status == "expired":
            break

    try:
        await qr_msg.delete()
    except Exception:
        pass
    await bot.send_message(
        user_id,
        "⏳ Payment expired. Generate a new QR if needed.",
        reply_markup=back_kb("buy_credits"),
    )


async def _handle_tx_hash(message: Message, text: str, pstate: dict):
    user_id = message.from_user.id

    if text.lower() == "cancel":
        pay_states.pop(user_id, None)
        await message.reply("❌ Cancelled.", reply_markup=back_kb("main_menu"))
        return

    tx_hash = text.strip()
    if not ((tx_hash.startswith("0x") and len(tx_hash) == 66) or len(tx_hash) == 64):
        await message.reply("❌ Invalid TX hash format. Send the 64-hex transaction ID.")
        return

    if await db.is_tx_used(tx_hash):
        pay_states.pop(user_id, None)
        await message.reply("❌ This TX hash has already been used.")
        return

    status_msg = await message.reply("⏳ Verifying deposit on Binance...")

    plan_key = pstate["plan_key"]
    plan = get_crypto_plan(plan_key)
    if not plan:
        pay_states.pop(user_id, None)
        await safe_edit(status_msg, "❌ Invalid plan.", reply_markup=back_kb("main_menu"))
        return

    ok, reason = await asyncio.to_thread(
        payments.verify_binance_deposit, tx_hash, "USDT", pstate["amount_usdt"],
    )

    if not ok:
        await safe_edit(status_msg, f"❌ Verification failed: {reason}")
        return

    pay_states.pop(user_id, None)
    await db.mark_tx_used(tx_hash, user_id, plan_key)
    await db.add_credits(user_id, plan["credits"])
    await db.save_payment(user_id, "crypto_usdt", plan_key, pstate["amount_usdt"], "USDT", tx_hash)
    new_balance = await db.get_credits(user_id)

    await safe_edit(status_msg,
        f"✅ **Deposit confirmed!**\n\n"
        f"🎁 +{plan['credits']} credits added\n"
        f"💰 New balance: **{new_balance}**",
        reply_markup=back_kb("main_menu"),
    )


async def _handle_rz_custom_amount(message: Message, text: str):
    user_id = message.from_user.id
    try:
        credits = int(text.strip())
        if credits < 10:
            await message.reply("❌ Minimum amount is 10 credits. Please try again:")
            return
    except ValueError:
        await message.reply("❌ Invalid number. Please enter a valid integer (minimum 10):")
        return

    auth_states.pop(user_id, None)

    plan_key = f"custom_{credits}"
    plan = get_credit_plan(plan_key)

    status_msg = await message.reply("⏳ Generating QR code...")
    qr = await asyncio.to_thread(
        payments.create_razorpay_qr, plan["label"], plan["amount_inr"], user_id,
    )
    if not qr:
        await safe_edit(status_msg,
            "❌ Payment gateway error. Try later.",
            reply_markup=back_kb("buy_credits"),
        )
        return

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"rz_check:{qr['id']}:{plan_key}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="buy_credits")],
    ])

    try:
        await status_msg.delete()
    except Exception:
        pass

    qr_msg = await bot.send_photo(
        user_id,
        photo=qr["image_url"],
        caption=(
            f"📱 **Scan to pay ₹{plan['amount_inr'] // 100}**\n"
            f"🎁 You'll receive **{plan['credits']} credits**\n\n"
            "⏱ Valid for 15 minutes."
        ),
        reply_markup=buttons,
    )

    asyncio.create_task(_razorpay_poller(
        user_id, qr["id"], plan_key, qr_msg,
    ))


async def _handle_cr_custom_amount(message: Message, text: str):
    user_id = message.from_user.id
    try:
        credits = int(text.strip())
        if credits < 10:
            await message.reply("❌ Minimum amount is 10 credits. Please try again:")
            return
    except ValueError:
        await message.reply("❌ Invalid number. Please enter a valid integer (minimum 10):")
        return

    auth_states.pop(user_id, None)

    plan_key = f"custom_{credits}"
    plan = get_crypto_plan(plan_key)

    buttons = [
        [InlineKeyboardButton("BSC (BEP20)", callback_data=f"cr_addr:BSC:{plan_key}")],
        [InlineKeyboardButton("TRC20 (TRON)", callback_data=f"cr_addr:TRX:{plan_key}")],
        [InlineKeyboardButton("ERC20 (Ethereum)", callback_data=f"cr_addr:ETH:{plan_key}")],
        [InlineKeyboardButton("🔙 Back", callback_data="cr_plans")],
    ]
    await message.reply(
        f"🌐 **Select network for USDT deposit ({plan['amount_usdt']} USDT for {credits} credits):**",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_set_new_category_price(message: Message, text: str):
    user_id = message.from_user.id
    state = auth_states[user_id]
    try:
        price = int(text.strip())
        if price <= 0:
            await message.reply("❌ Price must be a positive integer. Please try again:")
            return
    except ValueError:
        await message.reply("❌ Invalid price. Please enter a positive integer:")
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

    await message.reply(
        f"✅ **Category price set and number added successfully!**\n\n"
        f"📱 `{phone}` — {flag} {name}\n"
        f"💰 Price: **{price}** credits per OTP",
        reply_markup=main_menu_kb(True),
    )
