import motor.motor_asyncio
from datetime import datetime, timezone
from config import MONGODB_URI, ADMIN_IDS

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client.otpbot


# ── Users ──

async def get_user(telegram_id: int):
    return await db.users.find_one({"telegram_id": telegram_id})


async def create_user(telegram_id: int, username: str, first_name: str, role: str = "user"):
    doc = {
        "telegram_id": telegram_id,
        "username": username or "",
        "first_name": first_name or "",
        "role": role,
        "credits": 0,
        "is_active": True,
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


# ── Credits ──

async def get_credits(telegram_id: int) -> int:
    user = await get_user(telegram_id)
    if not user:
        return 0
    return user.get("credits", 0)


async def add_credits(telegram_id: int, amount: int):
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


# ── Country Pricing ──

async def set_country_price(country_code: str, price: int):
    await db.country_pricing.update_one(
        {"country_code": country_code},
        {"$set": {"country_code": country_code, "price": price}},
        upsert=True,
    )


async def get_country_price(country_code: str) -> int:
    doc = await db.country_pricing.find_one({"country_code": country_code})
    if doc:
        return doc.get("price", 1)
    return 1


async def get_all_country_prices() -> dict:
    result = {}
    async for doc in db.country_pricing.find():
        result[doc["country_code"]] = doc["price"]
    return result


# ── Sessions ──

async def save_session(phone_number: str, session_string: str, added_by: int,
                       password: str = "", country_code: str = "XX"):
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


async def mark_session_sold(phone_number: str, sold_to: int):
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": {"status": "sold", "sold_to": sold_to}},
    )


async def set_session_password(phone_number: str, password: str):
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": {"password": password}},
    )


async def set_session_account_info(phone_number: str, account_id: int, account_year: int | None):
    await db.sessions.update_one(
        {"phone_number": phone_number},
        {"$set": {"account_id": account_id, "account_year": account_year}},
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
