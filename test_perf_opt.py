"""Checks for the query-reduction changes: ctx-var user cache, batch pricing.
Run against a throwaway Mongo:  MONGODB_URI=mongodb://localhost:27099 python3 test_perf_opt.py
"""
import asyncio
import os
import sys

import motor.motor_asyncio
from pymongo import monitoring

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db

URI = os.getenv("TEST_MONGODB_URI", "mongodb://localhost:27099")

# Count real wire queries against `users` — motor rebuilds collection wrappers on
# every attribute access, so monkeypatching find_one never reaches database.py.
calls = {"n": 0}


class UserFindCounter(monitoring.CommandListener):
    def started(self, event):
        if event.command_name == "find" and event.command.get("find") == "users":
            calls["n"] += 1

    def succeeded(self, event): pass
    def failed(self, event): pass


async def main():
    monitoring.register(UserFindCounter())
    client = motor.motor_asyncio.AsyncIOMotorClient(URI)
    db.db = client.otpbot_test_perf
    await client.drop_database("otpbot_test_perf")

    await db.create_user(1, "alice", "Alice")
    await db.add_credits(1, 100)

    # ── cache collapses repeated reads within one "action" ──
    db.begin_user_cache()
    calls["n"] = 0
    for _ in range(7):
        await db.get_user(1)
    await db.get_credits(1)
    await db.get_balance(1)
    await db.is_verified(1)
    assert calls["n"] == 1, f"expected 1 query, got {calls['n']}"

    # ── no cache started => every read hits Mongo (background tasks) ──
    db._user_cache.set(None)
    calls["n"] = 0
    await db.get_user(1)
    await db.get_user(1)
    assert calls["n"] == 2, f"expected 2 uncached queries, got {calls['n']}"

    # ── a write must invalidate, or the next read serves stale credits ──
    db.begin_user_cache()
    assert await db.get_credits(1) == 100
    await db.add_credits(1, 50)
    assert await db.get_credits(1) == 150, "stale credits after add_credits"

    # deduct reads fresh even if a stale copy was cached first
    db.begin_user_cache()
    await db.get_user(1)                      # warm cache at 150
    await db.db.users.update_one({"telegram_id": 1}, {"$inc": {"credits": 1000}})
    ok, c, b = await db.deduct_funds_for_purchase(1, 1100)
    assert ok and c == 1100, f"deduct used stale balance: ok={ok} c={c} b={b}"
    assert await db.get_credits(1) == 50

    # ── cache is per-task: two concurrent users must not see each other ──
    await db.create_user(2, "bob", "Bob")
    await db.add_credits(2, 7)

    async def action(uid):
        db.begin_user_cache()
        await db.get_user(uid)
        await asyncio.sleep(0.01)             # let the other task interleave
        return await db.get_credits(uid)

    got = await asyncio.gather(action(1), action(2))
    assert got == [50, 7], f"cross-task cache leak: {got}"

    # ── batch pricing matches per-session pricing exactly ──
    await db.set_category_price("IN", 2023, False, 10)
    await db.set_category_price("IN", 2023, True, 15)
    await db.set_category_price("US", 2025, False, 40)

    sessions = [
        {"phone_number": "+911", "country_code": "IN", "account_year": 2023, "email_added": False},
        {"phone_number": "+912", "country_code": "IN", "account_year": 2023, "email_added": True},
        {"phone_number": "+13",  "country_code": "US", "account_year": 2025, "email_added": False},
        {"phone_number": "+14",  "country_code": "US", "account_year": None, "email_added": False},  # year defaults to 2025
        {"phone_number": "+15",  "country_code": "DE", "account_year": 2020, "email_added": False},  # unpriced
        {"phone_number": "+16",  "country_code": "IN", "account_year": 2023},                        # email_added absent
    ]
    batch = await db.get_session_prices(sessions)
    for s in sessions:
        one = await db.get_session_price(s)
        assert batch[s["phone_number"]] == one, \
            f"{s['phone_number']}: batch={batch[s['phone_number']]} single={one}"
    assert batch["+14"] == 40, "None year should normalise to 2025"
    assert batch["+15"] is None, "unpriced session should be None"
    assert batch["+16"] == 10, "absent email_added should coerce to False"
    assert await db.get_session_prices([]) == {}

    await client.drop_database("otpbot_test_perf")
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
