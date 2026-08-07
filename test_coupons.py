"""Self-checks for the coupon system.

Run: python3 test_coupons.py    (no test framework needed)

The redeem path needs a real MongoDB and is added in Task 3; it skips when
MONGODB_URI is unset or unreachable.
"""
from config import COUPON_ALPHABET, COUPON_CODE_LENGTH
from database import generate_coupon_code


def test_code_format():
    codes = {generate_coupon_code() for _ in range(500)}
    assert len(codes) > 400, "generator is not random enough"
    for c in codes:
        assert len(c) == COUPON_CODE_LENGTH, c
        assert set(c) <= set(COUPON_ALPHABET), c
        assert not (set(c) & set("O0I1")), f"ambiguous glyph in {c}"


def test_code_length_override():
    assert len(generate_coupon_code(10)) == 10


if __name__ == "__main__":
    test_code_format()
    test_code_length_override()
    print("coupon checks passed")
