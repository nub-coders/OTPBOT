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
    # Cleanup keys off this timestamp, not the returned list: if
    # create_coupon_batch raises after inserting some of its codes, the list is
    # never bound and those live codes would stay in the production collection
    # forever (there is no TTL index on coupons.expires_at).
    started_at = datetime.now(timezone.utc)
    codes = []
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

        # A coupon redeemed mid-purchase replaces the offer that purchase priced
        # against. When that purchase then fails, restoring must not un-spend the
        # newer coupon offer — that hands out its discount twice. consume/restore
        # are keyed on offer.granted_at to tell the instances apart.
        # A code uid_a has not spent yet: every earlier one would return
        # "already" and grant nothing, so the replacement would never happen.
        codes += await db.create_coupon_batch(count=1)
        await db.set_offer(uid_a, 10, 6)
        old = (await db.get_user(uid_a))["offer"]["granted_at"]
        await db.consume_offer(uid_a, old)                  # purchase X prices against it
        status, _ = await db.redeem_coupon(uid_a, codes[-1])  # coupon lands mid-flight
        assert status == "ok", status
        new = (await db.get_user(uid_a))["offer"]["granted_at"]
        assert new != old, "coupon did not replace the offer slot"
        await db.consume_offer(uid_a, new)                  # purchase Y spends the coupon
        assert await db.restore_offer(uid_a, old) is False, "stale restore was allowed"
        assert await db.get_active_offer(uid_a) is None, (
            "restoring purchase X revived the coupon offer purchase Y already spent")
        # The offer its own purchase spent is still restorable.
        assert await db.restore_offer(uid_a, new) is True
        assert await db.get_active_offer(uid_a) is not None
        # None means no offer was spent (price=0 re-request, seller self-login),
        # so it must never revive whatever happens to occupy the slot.
        await db.consume_offer(uid_a, new)
        assert await db.restore_offer(uid_a, None) is False, "unkeyed restore was allowed"
        assert await db.get_active_offer(uid_a) is None

        # A granted_at that has NOT round-tripped through Mongo still restores.
        # set_offer returns its in-memory dict, whose datetime carries
        # microseconds; BSON keeps milliseconds. A Python == between the two is
        # False, so comparing in Python would refuse a restore the user is owed.
        live = await db.set_offer(uid_a, 9, 6)
        raw = live["granted_at"]
        assert raw.microsecond % 1000, f"need sub-ms precision to test: {raw}"
        await db.consume_offer(uid_a, raw)
        assert await db.restore_offer(uid_a, raw) is True, (
            "microsecond granted_at was rejected: restore compared in Python")
        assert await db.get_active_offer(uid_a) is not None

        # An offer carrying no granted_at at all must not be revived either.
        # {"offer.granted_at": None} matches a missing field as well as an
        # explicit null, so a legacy refund draining after deploy would hit this.
        await db.db.users.update_one(
            {"telegram_id": uid_b},
            {"$set": {"offer": {"credits": big, "used": True,
                                "expires_at": now + timedelta(hours=5)}}},
        )
        db._invalidate_user_cache(uid_b)
        assert await db.restore_offer(uid_b, None) is False
        assert await db.get_active_offer(uid_b) is None, (
            "revived an offer that has no granted_at to key on")

        print("mongo redeem checks passed")
    finally:
        # Users first, and each guarded: the sentinels carry inflated
        # offer.credits, and get_stats sums offer.credits across every user
        # document with no id filter, so a leaked sentinel shows up in the
        # admin "Total Discount Credits" figure permanently.
        for cleanup in (
            lambda: db.db.users.delete_many(
                {"telegram_id": {"$in": [uid_a, uid_b, -9003, -9004]}}),
            lambda: db.db.coupons.delete_many(
                {"$or": [{"code": {"$in": codes}},
                         {"batch_at": {"$gte": started_at}}]}),
        ):
            try:
                await cleanup()
            except Exception as e:
                print(f"cleanup failed, check the DB by hand: {e}")


if __name__ == "__main__":
    import asyncio
    test_code_format()
    test_keyspace_is_brute_force_resistant()
    test_matches_redeem_regex()
    asyncio.run(test_redeem_against_mongo())
    print("coupon checks passed")
