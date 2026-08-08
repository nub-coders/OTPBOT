# Coupon System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At 00:00 UTC the bot posts 10 coupon codes to the support channel; a user types a code in chat and gets a discount offer worth a random 1–10 credits off, valid 6 hours.

**Architecture:** A new `coupons` MongoDB collection stores one document per code with a `redeemed_by` array. Redemption is a single atomic `update_one` that both checks once-per-user and claims. A background task in main.py posts the nightly batch, guarded by a `system` document so restarts don't double-post. The granted offer reuses the existing offer schema untouched, so `apply_discount` / `offer_banner` / `consume_offer` / `restore_offer` keep working as-is.

**Tech Stack:** Python 3, kurigram (Pyrogram fork, imported as `pyrogram`), motor (async MongoDB), python-dotenv. No test framework is installed — checks are `assert`-based `__main__` self-checks run with `python3`.

## Global Constraints

- All datetimes are UTC and timezone-aware: `datetime.now(timezone.utc)`. Mongo returns naive datetimes — normalize with `.replace(tzinfo=timezone.utc)` before comparing, matching `get_active_offer` at `database.py:255`.
- Code alphabet is exactly `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no `O`, `0`, `I`, `1`). Code length is 6.
- Coupon lifetime 24h from batch time. Offer lifetime 6h from redemption. Reward `random.randint(1, 10)`.
- Collision rule: keep the higher discount, always reset `expires_at` to 6h out.
- Do not call `db.can_grant_offer()` on the coupon path — its 24h cooldown belonged to the removed daily drop.
- The channel post must never contain reward amounts.
- Reuse the existing offer document shape from `set_offer` (`database.py:334`): keys `credits`, `granted_at`, `expires_at`, and optional `used`.
- After every `db.users` write, call `_invalidate_user_cache(telegram_id)` — see `consume_offer` at `database.py:268`.
- No new dependencies.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config.py` | `UPDATES_CHANNEL_ID` postable target + `COUPON_*` knobs | Modify |
| `database.py` | `coupons` collection: generate, insert, atomic redeem, best-of offer upsert, batch bookkeeping, index | Modify |
| `bot.py` | bare-code detection in `on_text`, redemption reply, `post_coupon_batch` channel message | Modify |
| `main.py` | `coupon_processor` background task + wiring | Modify |
| `test_coupons.py` | assert-based self-check for code format and best-of logic | Create |

This codebase keeps large single-purpose modules (`bot.py`, `database.py`); the plan follows that rather than introducing a new package.

---

### Task 1: Config

**Files:**
- Modify: `config.py:26-34` (extend the `_updates_raw` block), `config.py` (append `COUPON_*` near the `OFFER_*` block)

**Interfaces:**
- Produces: `UPDATES_CHANNEL_ID: str | int` (postable `@username` or `-100…` int, `""` when absent), `COUPON_COUNT: int`, `COUPON_CODE_LENGTH: int`, `COUPON_TTL_HOURS: float`, `COUPON_OFFER_HOURS: float`, `COUPON_MIN_CREDITS: int`, `COUPON_MAX_CREDITS: int`, `COUPON_ALPHABET: str`

- [ ] **Step 1: Add the postable channel id**

`UPDATES_CHANNEL` stays exactly as it is (a `t.me` URL for the keyboard button). Add a second derived value below the existing `else: UPDATES_CHANNEL = ""` at `config.py:34`:

```python
# Postable form of the updates channel. UPDATES_CHANNEL above is a t.me URL
# used for an inline button and cannot be passed to send_message; this holds
# the raw @username / -100 chat id. Empty derives from UPDATES_CHANNEL; set
# UPDATES_CHANNEL_ID to "-" to disable the coupon broadcast while keeping
# the button.
_updates_id = os.getenv("UPDATES_CHANNEL_ID", "").strip() or _updates_raw
if _updates_id.lower().startswith(("https://t.me/", "http://t.me/")):
    # Keep the whole post-host path as one string: a multi-segment path like
    # /joinchat/AAAA then fails the username check below instead of passing
    # its last segment off as a valid @username. Also cut any ?query/#fragment
    # so a tracking-tagged URL still resolves to its channel.
    _updates_id = _updates_id.split("/", 3)[-1].split("?", 1)[0].split("#", 1)[0].rstrip("/")
_updates_id = _updates_id.lstrip("@")
if _updates_id.isascii() and re.fullmatch(r"-?\d+", _updates_id):
    # int, not str: kurigram routes a digits-only string to a phone-number
    # lookup (ResolvePhone) and fails; a -100… id must be an int peer.
    UPDATES_CHANNEL_ID = int(_updates_id)
elif (4 <= len(_updates_id) <= 32 and _updates_id.isascii()
        and _updates_id.replace("_", "").isalnum()):
    UPDATES_CHANNEL_ID = f"@{_updates_id}"
else:
    # A private invite link (t.me/+hash, /joinchat/…) has no @username form.
    # Leave this empty so the broadcast disables itself instead of failing
    # every night against an unroutable peer; set UPDATES_CHANNEL_ID to the
    # numeric -100… id to post into a private channel.
    UPDATES_CHANNEL_ID = ""
```

- [ ] **Step 2: Add the coupon knobs**

Append after the `OFFER_*` block (ends `config.py:88`):

```python
# ── Nightly coupon codes ──
# COUPON_COUNT codes are posted to UPDATES_CHANNEL_ID at 00:00 UTC. Any user
# may redeem any code once; a redemption grants a discount offer worth a
# random COUPON_MIN_CREDITS..COUPON_MAX_CREDITS credits off for
# COUPON_OFFER_HOURS. Codes stop working COUPON_TTL_HOURS after posting.
# The alphabet excludes O/0/I/1 so a mistyped code cannot hit another coupon.
COUPON_COUNT = int(os.getenv("COUPON_COUNT", "10"))
# Floored at 6: the codes are brute-forceable through the chat handler over
# their 24h life, and a shorter code silently removes that protection.
COUPON_CODE_LENGTH = max(6, int(os.getenv("COUPON_CODE_LENGTH", "6")))
COUPON_TTL_HOURS = float(os.getenv("COUPON_TTL_HOURS", "24"))
COUPON_OFFER_HOURS = float(os.getenv("COUPON_OFFER_HOURS", "6"))
COUPON_MIN_CREDITS = int(os.getenv("COUPON_MIN_CREDITS", "1"))
# Clamped to min: an inverted config (min > max) would otherwise burn a
# redemption — the claim runs before the roll, and random.randint(min, max)
# raises ValueError on an empty range, leaving the code spent with no offer.
# Matches the bot.py:110 precedent for the offer discount.
COUPON_MAX_CREDITS = max(int(os.getenv("COUPON_MAX_CREDITS", "10")), COUPON_MIN_CREDITS)
COUPON_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
```

- [ ] **Step 3: Add the `re` import**

The digit test uses `re.fullmatch`; `config.py` imports only `os`, `dotenv`, and
`Decimal`. Add `import re` beside `import os` at the top.

- [ ] **Step 4: Verify it parses and resolves**

Run: `cd /root/OTPBOT && python3 -c "import config; print(repr(config.UPDATES_CHANNEL), repr(config.UPDATES_CHANNEL_ID), config.COUPON_COUNT, config.COUPON_ALPHABET)"`
Expected: the existing URL unchanged, an `@name`/`-100…` int/`""` id, `10`, and the alphabet with no `O01I`.

Also confirm, for each input, that `UPDATES_CHANNEL` is byte-identical to its
pre-change value and that a numeric id comes back as an `int`:

| `UPDATES_CHANNEL_ID` env | `UPDATES_CHANNEL_ID` |
|---|---|
| `-1001234567890` | `-1001234567890` (int) |
| `--123` | `""` |
| `١٢٣٤` | `""` |
| `-` | `""` (the off switch) |
| `https://T.me/mychannel` | `"@mychannel"` |
| `https://t.me/mychannel?start=x` | `"@mychannel"` |
| `https://t.me/s/mychannel` | `""` |
| `https://t.me/+ntOL-unWf3swYmE1` | `""` (the live `.env` value) |

- [ ] **Step 5: Document the env var**

Add to `.env.example`, replacing the bare `UPDATES_CHANNEL_ID=` line if Task 1
already added one:

```
# Postable form of the updates channel, used for the nightly coupon post.
# Leave empty to derive it from UPDATES_CHANNEL. A private invite link
# (t.me/+hash) cannot be derived — put the numeric -100… id here instead.
# Set to "-" to keep the Updates button but turn the coupon post off.
UPDATES_CHANNEL_ID=
```

- [ ] **Step 6: Commit**

```bash
git add config.py .env.example
git commit -m "feat(config): add postable channel id and coupon knobs"
```

---

### Task 2: Code generation + self-check

**Files:**
- Modify: `database.py` (append a `# ── Coupons ──` section after the Discount Offers section, which ends at `database.py:344`)
- Test: `test_coupons.py` (create)

**Interfaces:**
- Consumes: `COUPON_ALPHABET`, `COUPON_CODE_LENGTH` from Task 1
- Produces: `generate_coupon_code() -> str`

- [ ] **Step 1: Write the failing check**

Create `test_coupons.py`:

```python
"""Self-checks for the coupon system.

Run: python3 test_coupons.py    (no test framework needed)

The redeem path needs a real MongoDB and is added in Task 3; it skips when
MONGODB_URI is unset or unreachable.
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


if __name__ == "__main__":
    test_code_format()
    test_keyspace_is_brute_force_resistant()
    test_matches_redeem_regex()
    print("coupon checks passed")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /root/OTPBOT && python3 test_coupons.py`
Expected: `ImportError: cannot import name 'generate_coupon_code' from 'database'`

- [ ] **Step 3: Implement the generator**

Append to `database.py`. Note `secrets` is not currently imported — use `secrets.choice` (already-safe randomness, no new dependency):

```python
# ── Coupons ──
#
# Nightly codes posted to the updates channel. Any user may redeem any code
# once; the reward is rolled at redeem time so the codes themselves carry no
# value and are worthless to leak.


def generate_coupon_code() -> str:
    """A random coupon code from the unambiguous alphabet.

    secrets, not random: the codes are posted publicly, and Mersenne Twister
    state is recoverable from observed output — which would let an observer
    derive codes that have not been posted yet.
    """
    import secrets
    from config import COUPON_ALPHABET, COUPON_CODE_LENGTH

    return "".join(secrets.choice(COUPON_ALPHABET) for _ in range(COUPON_CODE_LENGTH))
```

- [ ] **Step 4: Run the check to verify it passes**

Run: `cd /root/OTPBOT && python3 test_coupons.py`
Expected: `coupon checks passed`

- [ ] **Step 5: Commit**

```bash
git add database.py test_coupons.py
git commit -m "feat(db): add coupon code generator"
```

---

### Task 3: Database layer

**Files:**
- Modify: `database.py` (the `# ── Coupons ──` section from Task 2), `database.py:939-969` (`ensure_indexes`)

**Interfaces:**
- Consumes: `generate_coupon_code` from Task 2; `COUPON_*` from Task 1
- Produces:
  - `async create_coupon_batch(count: int | None = None, ttl_hours: float | None = None) -> list[str]`
  - `async redeem_coupon(telegram_id: int, code: str) -> tuple[str, int]` returning `(status, credits)` where status is one of `"ok"`, `"no_user"`, `"unknown"`, `"expired"`, `"already"` and `credits` is the effective discount on `"ok"` else `0`. The `"no_user"` check runs *before* the claim: the offer write is not an upsert, so claiming first would spend the code and grant nothing.
  - `async get_last_coupon_batch_date() -> str` / `async set_last_coupon_batch_date(day: str) -> None` (`day` is `YYYY-MM-DD`)

- [ ] **Step 1: Add the index**

Insert into `ensure_indexes()` after the `used_tx` index at `database.py:967`:

```python
    # Coupons: code is the redeem lookup key and the generation collision gate.
    await db.coupons.create_index("code", unique=True, name="uniq_coupon_code")
    await db.coupons.create_index("expires_at")
```

- [ ] **Step 2: Implement batch creation**

Append to the Coupons section. `DuplicateKeyError` is already imported at the top of `database.py` (line 5) — do not re-import it. The unique index turns a generation collision into a `DuplicateKeyError` we retry, rather than a silent overwrite:

```python
async def create_coupon_batch(count: int | None = None, ttl_hours: float | None = None) -> list[str]:
    """Generate and insert a fresh batch of coupon codes. Returns the codes."""
    from config import COUPON_COUNT, COUPON_TTL_HOURS

    n = COUPON_COUNT if count is None else count
    ttl = COUPON_TTL_HOURS if ttl_hours is None else ttl_hours
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl)

    codes: list[str] = []
    for _ in range(n):
        for _attempt in range(10):
            code = generate_coupon_code()
            try:
                await db.coupons.insert_one({
                    "code": code,
                    "batch_at": now,
                    "expires_at": expires_at,
                    "redeemed_by": [],
                })
            except DuplicateKeyError:
                continue
            codes.append(code)
            break
    return codes
```

- [ ] **Step 3: Implement the atomic redeem**

```python
async def redeem_coupon(telegram_id: int, code: str) -> tuple[str, int]:
    """Redeem a coupon and grant a discount offer.

    Returns (status, credits): status is "ok" | "no_user" | "unknown" | "expired" | "already";
    credits is the effective discount on success, else 0.

    The claim is one update_one so the once-per-user check and the claim cannot
    interleave — a double-tap can never grant two offers.
    """
    import random
    from config import COUPON_MIN_CREDITS, COUPON_MAX_CREDITS, COUPON_OFFER_HOURS

    code = code.strip().upper()
    now = datetime.now(timezone.utc)

    # Before the claim, not after: the offer update below is not an upsert, so a
    # user with no document would have the code marked redeemed and get nothing.
    # This is one extra find_one, on a path only a well-formed code reaches.
    if not await get_user(telegram_id):
        return "no_user", 0

    claim = await db.coupons.update_one(
        {"code": code, "expires_at": {"$gt": now}, "redeemed_by": {"$ne": telegram_id}},
        {"$addToSet": {"redeemed_by": telegram_id}},
    )
    if claim.matched_count == 0:
        # Nothing was claimed — read once more only to word the reply.
        doc = await db.coupons.find_one({"code": code})
        if not doc:
            return "unknown", 0
        if telegram_id in doc.get("redeemed_by", []):
            return "already", 0
        return "expired", 0

    rolled = random.randint(COUPON_MIN_CREDITS, COUPON_MAX_CREDITS)
    expires_at = now + timedelta(hours=COUPON_OFFER_HOURS)

    # Single aggregation-pipeline update so the best-of comparison happens
    # server-side. A read-then-write here would let two codes redeemed at the
    # same instant clobber each other's discount.
    await db.users.update_one(
        {"telegram_id": telegram_id},
        [
            {"$set": {"offer": {
                "credits": {"$let": {
                    "vars": {"cur": {"$cond": [
                        {"$and": [
                            {"$ne": [{"$ifNull": ["$offer.used", False]}, True]},
                            {"$gt": [{"$ifNull": ["$offer.expires_at", now]}, now]},
                        ]},
                        {"$ifNull": ["$offer.credits", 0]},
                        0,
                    ]}},
                    "in": {"$max": [rolled, "$$cur"]},
                }},
                "granted_at": now,
                "expires_at": expires_at,
            }}},
            # Pipeline $set MERGES into the existing offer subdocument rather
            # than replacing it, so a `used: true` left by consume_offer would
            # survive and make get_active_offer treat this fresh offer as
            # spent — the user would be told they got a discount they cannot
            # use. Verified against the live server.
            {"$unset": "offer.used"},
        ],
    )
    _invalidate_user_cache(telegram_id)

    # Read back the value the server actually stored, so the reply cannot
    # disagree with the database after a concurrent redemption.
    user = await get_user(telegram_id)
    credits = int(((user or {}).get("offer") or {}).get("credits", rolled))
    return "ok", credits
```

- [ ] **Step 4: Implement batch bookkeeping**

Mirrors the `system`-document pattern the removed daily drop used:

```python
async def get_last_coupon_batch_date() -> str:
    doc = await db.system.find_one({"_id": "coupon_batch"})
    return (doc or {}).get("day", "")


async def set_last_coupon_batch_date(day: str) -> None:
    await db.system.update_one(
        {"_id": "coupon_batch"},
        {"$set": {"day": day, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
```

- [ ] **Step 5: Add the Mongo-backed redeem check**

Append to `test_coupons.py`. This is the real verification of the atomic claim and the best-of rule; it skips when no database is reachable so the file always runs:

```python
async def _mongo_available() -> bool:
    from config import MONGODB_URI
    if not MONGODB_URI:
        return False
    import database as db
    try:
        await db.client.admin.command("ping")
        return True
    except Exception:
        return False


async def test_redeem_against_mongo():
    """Exercise the real redeem path: once-per-user, best-of, expiry."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import database as db

    if not await _mongo_available():
        print("skipped: no MongoDB reachable (MONGODB_URI unset or down)")
        return

    uid_a, uid_b = -9001, -9002
    try:
        codes = await db.create_coupon_batch(count=3)
        assert len(codes) == 3, codes
        code = codes[0]

        # First redemption succeeds and grants an offer.
        status, credits = await db.redeem_coupon(uid_a, code)
        assert status == "ok", status
        assert 1 <= credits <= 10, credits
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
            {"$set": {"offer": {"credits": 99, "granted_at": now,
                                "expires_at": now + timedelta(hours=5)}}},
        )
        db._invalidate_user_cache(uid_a)
        _, kept = await db.redeem_coupon(uid_a, codes[1])
        assert kept == 99, f"downgraded a live offer to {kept}"

        # An expired offer must not block a fresh roll.
        await db.db.users.update_one(
            {"telegram_id": uid_a},
            {"$set": {"offer": {"credits": 99, "granted_at": now,
                                "expires_at": now - timedelta(hours=1)}}},
        )
        db._invalidate_user_cache(uid_a)
        _, fresh = await db.redeem_coupon(uid_a, codes[2])
        assert 1 <= fresh <= 10, f"expired offer leaked through: {fresh}"

        # A used offer must not block a fresh roll either — consume_offer sets
        # offer.used, and get_active_offer treats that as dead.
        await db.db.users.update_one(
            {"telegram_id": uid_b},
            {"$set": {"offer": {"credits": 99, "granted_at": now, "used": True,
                                "expires_at": now + timedelta(hours=5)}}},
        )
        db._invalidate_user_cache(uid_b)
        _, unused = await db.redeem_coupon(uid_b, codes[1])
        assert 1 <= unused <= 10, f"used offer leaked through: {unused}"
        # And the granted offer must be spendable, not just correctly valued.
        # A pipeline $set MERGES into the existing subdocument, so without an
        # explicit $unset the old `used: true` survives and get_active_offer
        # returns None — the user is promised a discount they cannot use.
        assert await db.get_active_offer(uid_b) is not None, (
            "redeemed offer is not active: offer.used survived the grant")

        # Unknown and expired codes are distinguished.
        assert (await db.redeem_coupon(uid_a, "ZZZZZZ"))[0] == "unknown"
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
```

Extend the `__main__` block to run it:

```python
if __name__ == "__main__":
    import asyncio
    test_code_format()
    test_keyspace_is_brute_force_resistant()
    test_matches_redeem_regex()
    asyncio.run(test_redeem_against_mongo())
    print("coupon checks passed")
```

- [ ] **Step 6: Verify it imports and the checks pass**

Run: `cd /root/OTPBOT && python3 -c "import database" && python3 test_coupons.py`
Expected: no output from the import, then either `mongo redeem checks passed` or the `skipped:` line, followed by `coupon checks passed`. A traceback is a failure — fix it.

`db.client` and `db.db` are the correct attribute names (`database.py:9-10`), verified when this plan was written.

- [ ] **Step 7: Commit**

```bash
git add database.py test_coupons.py
git commit -m "feat(db): add coupon batch creation, atomic redeem, and index"
```

---

### Task 4: Redemption in the bot

**Files:**
- Modify: `bot.py:32` (imports), `bot.py:1003-1005` (the `if not state: return` in `on_text`)

**Interfaces:**
- Consumes: `db.redeem_coupon` from Task 3; `COUPON_CODE_LENGTH`, `COUPON_ALPHABET`, `COUPON_OFFER_HOURS` from Task 1
- Produces: `_handle_coupon_text(message, text) -> bool` (True when the text was handled as a coupon)

- [ ] **Step 1: Extend the config import**

At `bot.py:32`, append to the existing `from config import ...` line:

```python
, COUPON_CODE_LENGTH, COUPON_ALPHABET, COUPON_OFFER_HOURS
```

- [ ] **Step 2: Add the handler**

`bot.py` does **not** currently import `re` (it only uses `filters.regex`, which
takes a pattern string). Add `import re` alongside the stdlib imports at the top
of `bot.py` near `from datetime import ...` (`bot.py:7`).

Then add the handler near the other `_handle_*` helpers. The regex is built from the configured alphabet so the two can't drift:

```python
_COUPON_RE = re.compile(rf"^[{COUPON_ALPHABET}]{{{COUPON_CODE_LENGTH}}}$")


async def _handle_coupon_text(message, text: str) -> bool:
    """Try to redeem a bare coupon code. Returns True if the text was a code.

    Only reached when the user is not in any other text flow, so a code can
    never shadow a phone number, price, or 2FA password.
    """
    code = text.strip().upper()
    if not _COUPON_RE.match(code):
        return False

    status, credits = await db.redeem_coupon(message.from_user.id, code)
    if status in ("unknown", "no_user"):
        return False  # not one of ours — stay silent, it was probably just chatter
    if status == "already":
        await message.reply(f"{em.BLOCKED} You've already used this coupon.")
    elif status == "expired":
        await message.reply(f"{em.CLOCK} This coupon has expired. Watch the channel for tonight's codes.")
    else:
        hours = int(COUPON_OFFER_HOURS)
        await message.reply(
            f"{em.GIFT} **Coupon redeemed!**\n\n"
            f"{em.CREDIT} **{credits} credits OFF** your next number.\n"
            f"{em.CLOCK} Valid for {hours} hours.",
        )
    return True
```

- [ ] **Step 3: Wire it into `on_text`**

Replace `bot.py:1003-1005`:

```python
        state = auth_states.get(user_id)
        if not state:
            return
```

with:

```python
        state = auth_states.get(user_id)
        if not state:
            # Checked last: a bare coupon code must never shadow an active flow.
            await _handle_coupon_text(message, text)
            return
```

- [ ] **Step 4: Confirm the emoji names exist**

`GIFT`, `CREDIT`, `CLOCK`, and `BLOCKED` are all defined in `custom_emojis.py` — verified when this plan was written. Confirm nothing has changed:

Run: `cd /root/OTPBOT && python3 -c "import custom_emojis as em; [getattr(em, n) for n in ('GIFT','CREDIT','CLOCK','BLOCKED')]; print('emoji ok')"`
Expected: `emoji ok`

- [ ] **Step 5: Verify the module compiles and the regex behaves**

Run:
```bash
cd /root/OTPBOT && python3 -m py_compile bot.py && python3 -c "
import re
from config import COUPON_ALPHABET, COUPON_CODE_LENGTH
r = re.compile(rf'^[{COUPON_ALPHABET}]{{{COUPON_CODE_LENGTH}}}$')
assert r.match('KX7F2A'); assert not r.match('kx7f2a')
assert not r.match('KX7F2'); assert not r.match('KX7F2AB')
assert not r.match('KX0F2A'), '0 must not be accepted'
assert not r.match('hello there'); print('regex ok')"
```
Expected: `regex ok`

- [ ] **Step 6: Commit**

```bash
git add bot.py
git commit -m "feat(bot): redeem coupon codes typed in chat"
```

---

### Task 5: Nightly broadcast

**Files:**
- Modify: `bot.py` (add `post_coupon_batch`), `main.py:146` (add `coupon_processor` before `main`), `main.py:209` (wiring)

**Interfaces:**
- Consumes: `db.create_coupon_batch`, `db.get_last_coupon_batch_date`, `db.set_last_coupon_batch_date` from Task 3; `UPDATES_CHANNEL_ID`, `COUPON_TTL_HOURS` from Task 1
- Produces: `async post_coupon_batch(bot, codes: list[str]) -> bool` in bot.py; `async coupon_processor(bot)` in main.py

- [ ] **Step 1: Add the channel post to bot.py**

Place next to `alert`. Import `UPDATES_CHANNEL_ID` and `COUPON_TTL_HOURS` by appending them to the `from config import ...` line at `bot.py:32`:

```python
async def post_coupon_batch(bot, codes: list[str]) -> bool:
    """Post tonight's coupon codes to the updates channel. Returns True on success.

    Codes only — never the reward amounts, which are rolled at redeem time.
    """
    if not UPDATES_CHANNEL_ID or not codes:
        return False
    listed = "\n".join(f"`{c}`" for c in codes)
    hours = int(COUPON_TTL_HOURS)
    try:
        await bot.send_message(
            UPDATES_CHANNEL_ID,
            f"{em.GIFT} **Tonight's Coupon Codes**\n\n"
            f"{listed}\n\n"
            f"Send any code to the bot to claim a discount on your next number.\n"
            f"Each code works once per user. Valid {hours}h.",
        )
        return True
    except Exception as e:
        log.error("Coupon batch post failed: %s", e)
        return False
```

- [ ] **Step 2: Add the processor to main.py**

Insert before `async def main():` at `main.py:182`, following the `refund_processor` shape:

```python
async def coupon_processor(bot):
    """Post a fresh coupon batch once per UTC day at 00:00."""
    import database as db
    from bot import post_coupon_batch
    from config import UPDATES_CHANNEL_ID

    if not UPDATES_CHANNEL_ID:
        log.info("Coupon broadcast disabled: no postable UPDATES_CHANNEL_ID (set a @username / -100… id, or a public t.me link)")
        return

    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            # Hour gate: the day's batch is simply "missing" until it is posted,
            # so without this a bot started at 15:00 announces "Tonight's Coupon
            # Codes" at 15:00. The 1h window still recovers a batch missed by a
            # restart straddling midnight; a longer outage waits for 00:00.
            if now.hour == 0 and await db.get_last_coupon_batch_date() != today:
                codes = await db.create_coupon_batch()
                # Recorded before the post succeeds: a missed post can be re-sent
                # by hand, a double post cannot be taken back.
                await db.set_last_coupon_batch_date(today)
                if await post_coupon_batch(bot, codes):
                    log.info("Coupon batch %s: %d codes posted", today, len(codes))
                else:
                    # The codes are already live in Mongo and expire unused if
                    # nobody ever saw them — that needs an operator's eye.
                    log.error("Coupon batch %s: %d codes created but NOT posted",
                              today, len(codes))
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            delay = max(60, (tomorrow - now).total_seconds())
        except Exception as e:
            log.error("Coupon processor error: %s", e)
            delay = 300
        await asyncio.sleep(delay)
```

- [ ] **Step 3: Add the datetime import to main.py**

`main.py` imports only `asyncio`, `logging`, and `from pyrogram import idle`
(`main.py:1-3`) — it has no `datetime` import, so `coupon_processor` will raise
`NameError` without this. Add after `import logging` (`main.py:2`):

```python
from datetime import datetime, timedelta, timezone
```

Verify: `cd /root/OTPBOT && grep -n "^from datetime" main.py`
Expected: one line showing all three names.

- [ ] **Step 4: Wire the task**

After `main.py:209` (`asyncio.create_task(payment_recovery_processor(bot))`), add:

```python
    asyncio.create_task(coupon_processor(bot))
```

- [ ] **Step 5: Verify everything compiles**

Run: `cd /root/OTPBOT && python3 -m py_compile bot.py main.py database.py config.py && python3 test_coupons.py`
Expected: no compile output, then `coupon checks passed`

- [ ] **Step 6: Verify the once-per-day guard logically**

Run:
```bash
cd /root/OTPBOT && python3 -c "
from datetime import datetime, timedelta, timezone
now = datetime(2026, 8, 7, 3, 14, tzinfo=timezone.utc)
tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
assert tomorrow == datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc), tomorrow
assert 0 < (tomorrow - now).total_seconds() <= 86400
print('schedule ok')"
```
Expected: `schedule ok`

- [ ] **Step 7: Commit**

```bash
git add bot.py main.py
git commit -m "feat: post nightly coupon batch to the updates channel"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| `coupons` collection + `redeemed_by` | 3 |
| Unique `code` index, `expires_at` index | 3 (Step 1) |
| Code alphabet without `O/0/I/1`, 6 chars | 1, 2 |
| Atomic claim, no read-then-write window | 3 (Step 3) |
| Nightly 00:00 UTC batch of 10 | 5 |
| Restart safety via `system` doc | 3 (Step 4), 5 (Step 2) |
| `UPDATES_CHANNEL_ID`, disabled when it does not resolve to a postable peer | 1, 5 |
| Post carries no reward amounts | 5 (Step 1) |
| Bare-code redeem checked after all flows | 4 (Step 3) |
| Roll 1–10 server-side at redeem | 3 (Step 3) |
| Best-of collision, 6h reset | 2, 3 |
| `can_grant_offer` bypassed | Global Constraints; 3 never calls it |
| Error handling: unknown/expired/already, post failure, insert collision | 3, 4, 5 |
| Tests | 2, plus per-task verification steps |

**Deviation from the spec, deliberate:** the spec's test list includes "two different users on one code both succeed" and "second redemption by the same user refused". Those need a live MongoDB, and this repo has no test database or framework. Task 2 covers the pure logic (format, best-of, expiry, used-offer) with runnable asserts; the multi-user race is enforced structurally by the unique-index + single-`update_one` claim in Task 3 and verified by the `matched_count` branch. Add a Mongo-backed test if you stand up a test database.

**Placeholder scan:** no TBD/TODO; every code step has literal code; Task 4 Step 4 gives a concrete fallback if an emoji name is absent.

**Type consistency:** `redeem_coupon` returns `tuple[str, int]` in Task 3 and is unpacked as `status, credits` in Task 4. `generate_coupon_code() -> str` matches its Task 2 definition and its Task 3 call. `create_coupon_batch() -> list[str]` matches `post_coupon_batch(bot, codes: list[str])`. Batch date is a `YYYY-MM-DD` string in both the getter/setter and the processor comparison.

**Amendments during execution** (both ruled by the human partner before Task 1):

1. The best-of offer upsert in Task 3 is a single aggregation-pipeline `update_one` computing `$max` server-side, not a read-then-write. The pure `_better_offer_credits` helper the earlier draft used is gone — the comparison lives in Mongo now, so a helper nothing calls would be dead code.
2. No MongoDB is reachable in this environment (`MONGODB_URI` unset). Task 2's check covers code format only; Task 3 adds a Mongo-backed redeem check that skips cleanly when no database is available, so the suite passes either way and does real verification wherever a database exists.
