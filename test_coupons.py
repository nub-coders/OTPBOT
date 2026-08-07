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
