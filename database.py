import motor.motor_asyncio
from datetime import datetime, timezone, timedelta
from config import MONGODB_URI, ADMIN_IDS, USDT_TO_INR

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


async def deduct_credits(telegram_id: int, amount: int) -> bool:
    result = await db.users.update_one(
        {"telegram_id": telegram_id, "credits": {"$gte": amount}},
        {"$inc": {"credits": -amount}},
    )
    return result.modified_count > 0


# ── Discount Offers ──

async def get_active_offer(telegram_id: int) -> dict | None:
    """Return the user's discount offer if one is currently active, else None."""
    user = await get_user(telegram_id)
    if not user:
        return None
    offer = user.get("offer")
    if not offer:
        return None
    expires_at = offer.get("expires_at")
    if not expires_at:
        return None
    # Mongo returns naive UTC datetimes; compare in UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return offer


async def can_grant_offer(telegram_id: int) -> bool:
    """A new offer can be granted only if none is active and the cooldown
    since the last grant has elapsed."""
    from config import OFFER_COOLDOWN_HOURS

    user = await get_user(telegram_id)
    if not user:
        return False
    offer = user.get("offer")
    if not offer:
        return True
    now = datetime.now(timezone.utc)
    expires_at = offer.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now:
            return False  # an offer is still active
    granted_at = offer.get("granted_at")
    if granted_at is not None:
        if granted_at.tzinfo is None:
            granted_at = granted_at.replace(tzinfo=timezone.utc)
        if now - granted_at < timedelta(hours=OFFER_COOLDOWN_HOURS):
            return False  # still within cooldown
    return True


async def set_offer(telegram_id: int, credits: int, duration_hours: float):
    now = datetime.now(timezone.utc)
    offer = {
        "credits": credits,
        "granted_at": now,
        "expires_at": now + timedelta(hours=duration_hours),
    }
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"offer": offer}},
    )
    return offer


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
    # Archive before deleting so removals can be counted in stats.
    session = await db.sessions.find_one({"phone_number": phone_number})
    if session:
        session["removed_at"] = datetime.now(timezone.utc)
        session.pop("_id", None)
        session.pop("session_string", None)  # don't retain the credential
        await db.removed_sessions.insert_one(session)
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


async def get_extended_stats():
    """Rich stats with 24h / 7d / 30d / all-time breakdowns for the admin panel."""
    now = datetime.now(timezone.utc)
    windows = {
        "24h": now - timedelta(hours=24),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }

    async def counts(coll, field, extra=None):
        base = dict(extra or {})
        out = {"all": await coll.count_documents(base)}
        for label, since in windows.items():
            out[label] = await coll.count_documents({**base, field: {"$gte": since}})
        return out

    def merge(a, b):
        return {k: a[k] + b[k] for k in a}

    # Numbers added (sessions created), sold, removed
    added = await counts(db.sessions, "created_at")
    # Sold: count from sessions still marked sold + any that were later removed
    sold_live = await counts(db.sessions, "sold_at", {"status": "sold"})
    sold_removed = await counts(db.removed_sessions, "sold_at", {"status": "sold"})
    sold = merge(sold_live, sold_removed)
    removed = await counts(db.removed_sessions, "removed_at")

    # Transactions (payments) and new users
    transactions = await counts(db.payments, "created_at")
    new_users = await counts(db.users, "created_at")
    otps = await counts(db.otps, "created_at")

    # Auth failures / auto-unlists
    auth_failures = await counts(db.auth_failures, "created_at")

    # Sell-through: sold / added, per window
    sell_through = {}
    for k in ["24h", "7d", "30d", "all"]:
        a = added[k]
        sell_through[k] = (sold[k] / a * 100) if a else 0.0

    # Average hours from added to sold (over sold sessions, live + removed)
    avg_time_to_sell = None
    tts_pipeline = [
        {"$match": {"status": "sold", "sold_at": {"$exists": True}, "created_at": {"$exists": True}}},
        {"$project": {"secs": {"$subtract": ["$sold_at", "$created_at"]}}},
        {"$group": {"_id": None, "avg": {"$avg": "$secs"}}},
    ]
    total_avg = []
    for coll in (db.sessions, db.removed_sessions):
        async for doc in coll.aggregate(tts_pipeline):
            if doc.get("avg") is not None:
                total_avg.append(doc["avg"])
    if total_avg:
        avg_time_to_sell = (sum(total_avg) / len(total_avg)) / 1000 / 3600  # ms -> hours

    # Funnel: total users -> verified -> made a purchase (all-time)
    total_users = new_users["all"]
    verified_users = await db.users.count_documents({"verified": True})
    buyers = len(await db.payments.distinct("user_id"))

    # Inventory breakdown by status
    inventory = {}
    async for doc in db.sessions.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        inventory[doc["_id"] or "unknown"] = doc["n"]

    # Total credits currently held by users (outstanding liability)
    outstanding = 0
    async for doc in db.users.aggregate([{"$group": {"_id": None, "c": {"$sum": "$credits"}}}]):
        outstanding = doc["c"]

    return {
        "added": added,
        "sold": sold,
        "removed": removed,
        "transactions": transactions,
        "new_users": new_users,
        "otps": otps,
        "auth_failures": auth_failures,
        "sell_through": sell_through,
        "avg_time_to_sell": avg_time_to_sell,
        "funnel": {"users": total_users, "verified": verified_users, "buyers": buyers},
        "inventory": inventory,
        "outstanding_credits": outstanding,
    }


async def ensure_indexes():
    """Create indexes used by the stats queries. Idempotent — safe to call on every startup."""
    await db.auth_failures.create_index("created_at")
    await db.removed_sessions.create_index("removed_at")
    await db.removed_sessions.create_index("sold_at")


async def log_auth_failure(phone_number: str, reason: str, kind: str = "auth", requested_by: int = None):
    """Record an auth failure / auto-unlist event so it can be counted in stats."""
    await db.auth_failures.insert_one({
        "phone_number": phone_number,
        "kind": kind,  # e.g. "connect" or "password"
        "reason": (reason or "")[:300],
        "requested_by": requested_by,
        "created_at": datetime.now(timezone.utc),
    })


async def get_revenue_stats():
    """Revenue totals split by all-time and last 24h, in INR-equivalent."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    async def revenue(match):
        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$method", "count": {"$sum": 1}, "total": {"$sum": "$amount"}}},
        ]
        count = 0
        total_inr = 0.0
        async for doc in db.payments.aggregate(pipeline):
            count += doc["count"]
            if doc["_id"] == "crypto_usdt":
                total_inr += doc["total"] * USDT_TO_INR
            else:
                total_inr += doc["total"]
        return {"count": count, "inr": total_inr}

    return {
        "all": await revenue({}),
        "24h": await revenue({"created_at": {"$gte": since}}),
    }


# ── Payments ──

async def save_payment(user_id: int, method: str, plan: str, amount: float, currency: str, ref_id: str = ""):
    doc = {
        "user_id": user_id,
        "method": method,
        "plan": plan,
        "amount": float(amount),  # ensure BSON double, not Decimal128
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

async def get_referral_count(telegram_id: int, verified_only: bool = False) -> int:
    query = {"referred_by": telegram_id}
    if verified_only:
        query["verified"] = True
    return await db.users.count_documents(query)


async def get_referral_earned(telegram_id: int) -> int:
    user = await get_user(telegram_id)
    if not user:
        return 0
    return user.get("referral_earned", 0)


async def add_referral_earning(telegram_id: int, amount: int):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"referral_earned": amount, "credits": amount}},
    )


async def has_made_purchase(telegram_id: int) -> bool:
    return await db.payments.count_documents({"user_id": telegram_id}) > 0


async def mark_referral_rewarded(telegram_id: int):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"referral_verify_rewarded": True}},
    )


async def is_referral_rewarded(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    return user is not None and user.get("referral_verify_rewarded", False)


async def mark_referral_purchase_rewarded(telegram_id: int):
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"referral_purchase_rewarded": True}},
    )


async def is_referral_purchase_rewarded(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    return user is not None and user.get("referral_purchase_rewarded", False)


async def top_buyer_24h():
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}},
        {"$limit": 1},
    ]
    async for doc in db.payments.aggregate(pipeline):
        user = await get_user(doc["_id"])
        name = (user.get("username") or user.get("first_name") or str(doc["_id"])) if user else str(doc["_id"])
        return {"name": name, "user_id": doc["_id"], "total": doc["total"]}
    return None


async def top_referrer_24h():
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    pipeline = [
        {"$match": {"referred_by": {"$ne": None}, "created_at": {"$gte": since}}},
        {"$group": {"_id": "$referred_by", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1},
    ]
    async for doc in db.users.aggregate(pipeline):
        user = await get_user(doc["_id"])
        name = (user.get("username") or user.get("first_name") or str(doc["_id"])) if user else str(doc["_id"])
        return {"name": name, "user_id": doc["_id"], "count": doc["count"]}
    return None
