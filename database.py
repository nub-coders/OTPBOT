import motor.motor_asyncio
from datetime import datetime, timezone, timedelta
from config import MONGODB_URI, ADMIN_IDS

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client.otpbot


# ── Users ──

async def get_user(telegram_id: int):
    return await db.users.find_one({"telegram_id": telegram_id})


async def create_user(telegram_id: int, username: str, first_name: str, role: str = "user", referred_by: int = None):
    doc = {
        "telegram_id": telegram_id,
        "username": username or "",
        "first_name": first_name or "",
        "role": role,
        "credits": 0,
        "is_active": True,
        "referred_by": referred_by,
        "referral_earned": 0,
        "created_at": datetime.now(timezone.utc),
    }
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return doc


async def set_user_role(telegram_id: int, role: str):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"role": role}},
    )


async def is_admin(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return True
    user = await get_user(telegram_id)
    return user is not None and user.get("role") == "admin"


async def admin_count() -> int:
    return await db.users.count_documents({"role": "admin"})


async def get_all_users():
    return await db.users.find().to_list(None)


# ── Verification ──

async def is_verified(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    return user is not None and user.get("verified", False)


async def mark_verified(telegram_id: int):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"verified": True}},
    )


async def create_verify_token(telegram_id: int, token: str, ttl_seconds: int = 600):
    await db.verify_tokens.delete_many({"telegram_id": telegram_id})
    await db.verify_tokens.insert_one({
        "telegram_id": telegram_id,
        "token": token,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        "used": False,
    })


async def consume_verify_token(token: str) -> int | None:
    doc = await db.verify_tokens.find_one_and_update(
        {"token": token, "used": False, "expires_at": {"$gt": datetime.now(timezone.utc)}},
        {"$set": {"used": True}},
    )
    if doc:
        return doc["telegram_id"]
    return None


# ── Credits ──

async def get_credits(telegram_id: int) -> float:
    user = await get_user(telegram_id)
    if not user:
        return 0
    return user.get("credits", 0)


async def add_credits(telegram_id: int, amount: float):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"credits": amount}},
    )


async def deduct_credit(telegram_id: int) -> bool:
    result = await db.users.update_one(
        {"telegram_id": telegram_id, "credits": {"$gte": 1}},
        {"$inc": {"credits": -1}},
    )
    return result.modified_count > 0


async def deduct_credits(telegram_id: int, amount: float) -> bool:
    result = await db.users.update_one(
        {"telegram_id": telegram_id, "credits": {"$gte": amount}},
        {"$inc": {"credits": -amount}},
    )
    return result.modified_count > 0


# ── Country Pricing (Removed) ──



# ── Sessions ──

async def save_session(phone_number: str, session_string: str, added_by: int,
                       password: str = "", country_code: str = "XX",
                       account_id: int = None, account_year: int = None,
                       email_added: bool = None):
    doc = {
        "phone_number": phone_number,
        "session_string": session_string,
        "password": password,
        "country_code": country_code,
        "is_active": True,
        "status": "active",
        "added_by": added_by,
        "created_at": datetime.now(timezone.utc),
    }
    if account_id is not None:
        doc["account_id"] = account_id
    if account_year is not None:
        doc["account_year"] = account_year
    if email_added is not None:
        doc["email_added"] = email_added
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": doc},
        upsert=True,
    )
    return doc


async def get_session(phone_number: str):
    return await db.sessions.find_one({"phone_number": phone_number})


async def get_all_sessions():
    return await db.sessions.find().to_list(None)


async def get_active_sessions():
    return await db.sessions.find({"status": "active"}).to_list(None)


async def get_active_sessions_by_country(country_code: str):
    if country_code == "XX":
        return await db.sessions.find({
            "status": "active",
            "$or": [{"country_code": "XX"}, {"country_code": {"$exists": False}}],
        }).to_list(None)
    return await db.sessions.find({"status": "active", "country_code": country_code}).to_list(None)


async def remove_session(phone_number: str):
    await db.sessions.delete_one({"phone_number": phone_number})


async def mark_session_sold(phone_number: str, sold_to: int, price: int = 0):
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": {
            "status": "sold",
            "sold_to": sold_to,
            "sold_at": datetime.now(timezone.utc),
            "sold_price": price,
        }},
    )


async def get_sold_sessions():
    return await db.sessions.find({"status": "sold"}).sort("sold_at", -1).to_list(None)


async def set_session_password(phone_number: str, password: str):
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": {"password": password}},
    )


async def set_session_account_info(phone_number: str, account_id: int, account_year: int | None, email_added: bool | None = None):
    update_doc = {"account_id": account_id, "account_year": account_year}
    if email_added is not None:
        update_doc["email_added"] = email_added
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": update_doc},
    )


async def set_session_category(phone_number: str, country_code: str = None,
                               account_year: int = None, email_added: bool = None):
    update_doc = {}
    if country_code is not None:
        update_doc["country_code"] = country_code
    if account_year is not None:
        update_doc["account_year"] = account_year
    if email_added is not None:
        update_doc["email_added"] = email_added
    if update_doc:
        await db.sessions.update_one(
            {"phone_number": phone_number},
            {"$set": update_doc},
        )


async def set_session_status(phone_number: str, status: str, error: str = ""):
    if error:
        await db.sessions.update_one(
            {"phone_number": phone_number},
            {"$set": {"status": status, "last_error": error}},
        )
    else:
        await db.sessions.update_one(
            {"phone_number": phone_number},
            {"$set": {"status": status}, "$unset": {"last_error": ""}},
        )


# ── OTP History ──

async def save_otp(phone_number: str, code: str, message: str, sender: str, requested_by: int):
    doc = {
        "phone_number": phone_number,
        "code": code,
        "message": message,
        "sender": sender,
        "requested_by": requested_by,
        "created_at": datetime.now(timezone.utc),
    }
    await db.otps.insert_one(doc)
    return doc


async def get_user_otps(telegram_id: int, limit: int = 10):
    return await db.otps.find(
        {"requested_by": telegram_id}
    ).sort("created_at", -1).limit(limit).to_list(None)


# ── Active Assignments ──

async def save_active_assignment(phone_number: str, user_id: int, price: int, timeout: int):
    doc = {
        "phone_number": phone_number,
        "user_id": user_id,
        "price": price,
        "otp_received": False,
        "assigned_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=timeout),
    }
    await db.active_assignments.update_one(
        {"phone_number": phone_number},
        {"$set": doc},
        upsert=True,
    )


async def mark_assignment_otp_received(phone_number: str):
    await db.active_assignments.update_one(
        {"phone_number": phone_number},
        {"$set": {"otp_received": True}},
    )


async def remove_active_assignment(phone_number: str):
    await db.active_assignments.delete_one({"phone_number": phone_number})


async def get_all_active_assignments():
    return await db.active_assignments.find().to_list(None)


# ── Pending Payments ──

async def save_pending_payment(user_id: int, qr_id: str, plan_key: str, amount_inr: int, msg_chat_id: int, msg_id: int):
    doc = {
        "user_id": user_id,
        "qr_id": qr_id,
        "plan_key": plan_key,
        "amount_inr": amount_inr,
        "msg_chat_id": msg_chat_id,
        "msg_id": msg_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    }
    await db.pending_payments.update_one(
        {"qr_id": qr_id},
        {"$set": doc},
        upsert=True,
    )


async def get_pending_payments():
    return await db.pending_payments.find({"status": "pending"}).to_list(None)


async def mark_pending_payment_done(qr_id: str) -> bool:
    """Atomically mark a pending payment as done. Returns True only if this call was the one that flipped it."""
    result = await db.pending_payments.find_one_and_update(
        {"qr_id": qr_id, "status": "pending"},
        {"$set": {"status": "done", "paid_at": datetime.now(timezone.utc)}},
    )
    return result is not None


async def mark_pending_payment_expired(qr_id: str):
    await db.pending_payments.update_one(
        {"qr_id": qr_id},
        {"$set": {"status": "expired"}},
    )


# ── Pending Refunds ──

REFUND_DELAY_HOURS = 2


async def save_pending_refund(user_id: int, phone_number: str, amount: int):
    refund_at = datetime.now(timezone.utc) + timedelta(hours=REFUND_DELAY_HOURS)
    doc = {
        "user_id": user_id,
        "phone_number": phone_number,
        "amount": amount,
        "created_at": datetime.now(timezone.utc),
        "refund_at": refund_at,
        "status": "pending",
    }
    await db.pending_refunds.insert_one(doc)
    return doc


async def get_due_refunds():
    now = datetime.now(timezone.utc)
    return await db.pending_refunds.find({
        "status": "pending",
        "refund_at": {"$lte": now},
    }).to_list(None)


async def mark_refund_done(refund_id):
    await db.pending_refunds.update_one(
        {"_id": refund_id},
        {"$set": {"status": "done", "processed_at": datetime.now(timezone.utc)}},
    )


async def cancel_pending_refund(phone_number: str, user_id: int):
    await db.pending_refunds.update_many(
        {"phone_number": phone_number, "user_id": user_id, "status": "pending"},
        {"$set": {"status": "cancelled"}},
    )


# ── Stats ──

async def get_stats():
    users = await db.users.count_documents({})
    sessions = await db.sessions.count_documents({"status": "active"})
    otps = await db.otps.count_documents({})
    return {"users": users, "sessions": sessions, "otps": otps}


# ── Payments ──

async def save_payment(user_id: int, method: str, plan: str, amount: float, currency: str, ref_id: str = ""):
    doc = {
        "user_id": user_id,
        "method": method,
        "plan": plan,
        "amount": amount,
        "currency": currency,
        "ref_id": ref_id,
        "created_at": datetime.now(timezone.utc),
    }
    await db.payments.insert_one(doc)


async def get_payment_stats():
    total = await db.payments.count_documents({})
    pipeline = [{"$group": {"_id": "$method", "count": {"$sum": 1}, "total": {"$sum": "$amount"}}}]
    by_method = {}
    async for doc in db.payments.aggregate(pipeline):
        by_method[doc["_id"]] = {"count": doc["count"], "total": doc["total"]}
    return {"total_payments": total, "by_method": by_method}


async def is_tx_used(tx_hash: str) -> bool:
    return await db.used_tx.find_one({"tx_hash": tx_hash}) is not None


async def mark_tx_used(tx_hash: str, user_id: int, plan: str):
    await db.used_tx.update_one(
        {"tx_hash": tx_hash},
        {"$set": {"user_id": user_id, "plan": plan, "ts": datetime.now(timezone.utc)}},
        upsert=True,
    )


# ── Category Pricing ──

async def get_category_price(country_code: str, year: int | None, email_added: bool | None) -> int | None:
    y = year if year is not None else 2025
    e = bool(email_added)
    doc = await db.category_pricing.find_one({
        "country_code": country_code,
        "year": y,
        "email_added": e
    })
    if doc:
        return doc["price"]
    return None


async def get_category_prices(country_code: str) -> list:
    return await db.category_pricing.find({"country_code": country_code}).to_list(None)


async def set_category_price(country_code: str, year: int | None, email_added: bool | None, price: int):
    y = year if year is not None else 2025
    e = bool(email_added)
    await db.category_pricing.update_one(
        {"country_code": country_code, "year": y, "email_added": e},
        {"$set": {"price": price}},
        upsert=True,
    )


async def get_session_price(session: dict) -> int | None:
    cc = session.get("country_code", "XX")
    year = session.get("account_year")
    email_added = session.get("email_added", False)
    
    return await get_category_price(cc, year, email_added)


# ── Referrals ──

async def get_referral_count(telegram_id: int) -> int:
    return await db.users.count_documents({"referred_by": telegram_id})


async def get_referral_earned(telegram_id: int) -> int:
    user = await get_user(telegram_id)
    if not user:
        return 0
    return user.get("referral_earned", 0)


async def add_referral_earning(telegram_id: int, amount: float):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"referral_earned": amount, "credits": amount}},
    )


async def has_made_purchase(telegram_id: int) -> bool:
    return await db.payments.count_documents({"user_id": telegram_id}) > 0


async def mark_referral_rewarded(telegram_id: int):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"referral_rewarded": True}},
    )


async def is_referral_rewarded(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    return user is not None and user.get("referral_rewarded", False)
