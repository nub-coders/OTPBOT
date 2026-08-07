"""Self-checks for the coupon system.

Run: python3 test_coupons.py    (no test framework needed)

The redeem checks need a real MongoDB and skip when none is reachable; the
format checks run anywhere. database.py builds its client at import, so with
no MONGODB_URI configured at all this file cannot load — configure one or
accept the ConfigurationError.
"""
from config import COUPON_ALPHABET, COUPON_CODE_LENGTH
from database import generate_coupon_code


def test_code_format():
    # Scale the draw to the configured keyspace: a hardcoded bound would blame
    # the generator for a shrunken alphabet or length.
    keyspace = len(COUPON_ALPHABET) ** COUPON_CODE_LENGTH
    draws = 500
    codes = {generate_coupon_code() for _ in range(draws)}
    assert len(codes) > min(draws * 0.8, keyspace * 0.8), (
        f"only {len(codes)} distinct in {draws} draws over a {keyspace} keyspace"
    )
    for c in codes:
        assert len(c) == COUPON_CODE_LENGTH, c
        assert set(c) <= set(COUPON_ALPHABET), c
        assert not (set(c) & set("O0I1")), f"ambiguous glyph in {c}"


def test_keyspace_is_brute_force_resistant():
    # A config typo shortening the code must fail loudly, not silently weaken
    # the codes: these are guessable over a 24h TTL through the chat handler.
    keyspace = len(COUPON_ALPHABET) ** COUPON_CODE_LENGTH
    assert keyspace >= 10**9, f"keyspace {keyspace} is brute-forceable"


def test_matches_redeem_regex():
    # The handler in Task 4 only accepts codes matching this; a generator that
    # emits anything else would mint codes nobody can redeem.
    import re
    pattern = re.compile(rf"^[{COUPON_ALPHABET}]{{{COUPON_CODE_LENGTH}}}$")
    for _ in range(100):
        assert pattern.match(generate_coupon_code())


async def _mongo_available() -> bool:
    """Whether a real MongoDB is reachable, decided in bounded time.

    Not db.client: it carries the default 30s serverSelectionTimeoutMS, so
    probing it with no database stalls the run for 30s before the skip prints.
    The retries are for mongodb+srv:// DNS, which resolves slowly often enough
    that a single short ping would skip a working cluster.
    """
    import motor.motor_asyncio
    from config import MONGODB_URI

    if not MONGODB_URI:
        return False
    probe = None
    try:
        # Construction itself raises on a malformed URI, so it lives in the try.
        probe = motor.motor_asyncio.AsyncIOMotorClient(
            MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        for _ in range(3):
            try:
                await probe.admin.command("ping")
                return True
            except Exception:
                continue  # transient DNS / server selection: retry the ping
        return False
    except Exception:
        return False
    finally:
        if probe is not None:
            probe.close()


async def test_redeem_against_mongo():
    """Exercise the real redeem path: once-per-user, best-of, expiry."""
    from datetime import datetime, timedelta, timezone
    from config import COUPON_MIN_CREDITS, COUPON_MAX_CREDITS
    import database as db

    if not await _mongo_available():
        print("skipped: no MongoDB reachable")
        return

    # Negative ids can never collide with a real Telegram user.
    uid_a, uid_b = -9001, -9002
    # Bound to the configured range, not a literal: a retuned COUPON_MAX_CREDITS
    # must not turn a passing check into a spurious failure.
    big = COUPON_MAX_CREDITS + 100
    codes = []  # bound before the try so the finally can always clean up
    try:
        # redeem_coupon's $set on users is not an upsert — same as set_offer and
        # every other users write here. Production users exist by then (/start
        # runs create_user), so the check has to create them too.
        await db.create_user(uid_a, "coupon_test_a", "Coupon Test A")
        await db.create_user(uid_b, "coupon_test_b", "Coupon Test B")

        codes = await db.create_coupon_batch(count=3)
        assert len(codes) == 3, codes
        code = codes[0]

        # First redemption succeeds and grants an offer.
        status, credits = await db.redeem_coupon(uid_a, code)
        assert status == "ok", status
        assert COUPON_MIN_CREDITS <= credits <= COUPON_MAX_CREDITS, credits
        offer = (await db.get_user(uid_a))["offer"]
        assert offer["credits"] == credits

        # Same user, same code: refused, offer untouched.
        again, zero = await db.redeem_coupon(uid_a, code)
        assert again == "already", again
        assert zero == 0
        assert (await db.get_user(uid_a))["offer"]["credits"] == credits

        # A different user redeems the same code fine.
        status_b, _ = await db.redeem_coupon(uid_b, code)
        assert status_b == "ok", status_b

        # Best-of: a big live offer is never downgraded by a smaller roll.
        now = datetime.now(timezone.utc)
        await db.db.users.update_one(
            {"telegram_id": uid_a},
            {"$set": {"offer": {"credits": big, "granted_at": now,
                                "expires_at": now + timedelta(hours=5)}}},
        )
        db._invalidate_user_cache(uid_a)
        _, kept = await db.redeem_coupon(uid_a, codes[1])
        assert kept == big, f"downgraded a live offer to {kept}"

        # An expired offer must not block a fresh roll.
        await db.db.users.update_one(
            {"telegram_id": uid_a},
            {"$set": {"offer": {"credits": big, "granted_at": now,
                                "expires_at": now - timedelta(hours=1)}}},
        )
        db._invalidate_user_cache(uid_a)
        _, fresh = await db.redeem_coupon(uid_a, codes[2])
        assert COUPON_MIN_CREDITS <= fresh <= COUPON_MAX_CREDITS, (
            f"expired offer leaked through: {fresh}")

        # A used offer must not block a fresh roll either — consume_offer sets
        # offer.used, and get_active_offer treats that as dead.
        await db.db.users.update_one(
            {"telegram_id": uid_b},
            {"$set": {"offer": {"credits": big, "granted_at": now, "used": True,
                                "expires_at": now + timedelta(hours=5)}}},
        )
        db._invalidate_user_cache(uid_b)
        _, unused = await db.redeem_coupon(uid_b, codes[1])
        assert COUPON_MIN_CREDITS <= unused <= COUPON_MAX_CREDITS, (
            f"used offer leaked through: {unused}")
        # And the granted offer must be spendable, not just correctly valued.
        # A pipeline $set MERGES into the existing subdocument, so without an
        # explicit $unset the old `used: true` survives and get_active_offer
        # returns None — the user is promised a discount they cannot use.
        assert await db.get_active_offer(uid_b) is not None, (
            "redeemed offer is not active: offer.used survived the grant")

        # Unknown and expired codes are distinguished. The unknown code uses a
        # glyph outside COUPON_ALPHABET so it can never collide with a real one.
        assert (await db.redeem_coupon(uid_a, "ZZZZ0O"))[0] == "unknown"
        await db.db.coupons.update_one(
            {"code": code}, {"$set": {"expires_at": now - timedelta(hours=1)}})
        await db.create_user(-9003, "coupon_test_c", "Coupon Test C")
        assert (await db.redeem_coupon(-9003, code))[0] == "expired"

        # A user with no document is refused and the code is NOT spent: the
        # offer write is not an upsert, so claiming first would burn the
        # redemption and grant nothing.
        no_user, zero = await db.redeem_coupon(-9004, codes[1])
        assert no_user == "no_user", no_user
        assert zero == 0
        spent = await db.db.coupons.find_one({"code": codes[1]})
        assert -9004 not in spent["redeemed_by"], "burned a code on a missing user"
        print("mongo redeem checks passed")
    finally:
        await db.db.coupons.delete_many({"code": {"$in": codes}})
        await db.db.users.delete_many(
            {"telegram_id": {"$in": [uid_a, uid_b, -9003, -9004]}})


if __name__ == "__main__":
    import asyncio
    test_code_format()
    test_keyspace_is_brute_force_resistant()
    test_matches_redeem_regex()
    asyncio.run(test_redeem_against_mongo())
    print("coupon checks passed")
