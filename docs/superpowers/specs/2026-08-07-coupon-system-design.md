# Coupon System Design

Date: 2026-08-07

## Summary

At 00:00 the bot posts 10 coupon codes to the support channel. Any user may
redeem any code, once each. A redemption grants a **discount offer** worth a
random 1–10 credits off, valid 6 hours — it does not add credits to a balance.

This reuses the existing discount-offer machinery (`set_offer` schema,
`get_active_offer`, `apply_discount`, `offer_banner`, `consume_offer`,
`restore_offer`), which lost its only grant source when the daily random credit
drop was removed. The coupon becomes the new grant source.

## Decisions

| Question | Decision |
|---|---|
| Redemptions per code | Anyone, once per user |
| Code lifetime | 24h after posting |
| Offer lifetime | 6h from redemption |
| Reward | Random 1–10 credits off, rolled at redeem time |
| Collision with a live offer | Keep the better discount, reset clock to 6h |
| Post target | Channel derived from `UPDATES_CHANNEL` |
| Redeem UX | Bare code typed in chat |
| Batch time | 00:00 UTC (all timestamps in this design are UTC) |

## 1. Data model

New `coupons` collection, one document per code:

```
{ code: "KX7F2A",             // unique index, uppercase
  batch_at: <00:00 UTC>,      // when posted
  expires_at: <batch_at+24h>,
  redeemed_by: [12345, 678]   // telegram_ids
}
```

Indexes:
- `code` unique, name `uniq_coupon_code` — makes generation collision-safe and
  backs the redeem lookup. Added to `ensure_indexes()`.
- `expires_at` — for the cleanup/lookup path.

Code format: 6 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no
`O/0/I/1`). Ambiguous glyphs are excluded so a mistyped code fails cleanly
instead of matching a different valid coupon.

### Atomic claim

The once-per-user check and the claim are a single `update_one`:

```
filter: {code: CODE, expires_at: {$gt: now}, redeemed_by: {$ne: uid}}
update: {$addToSet: {redeemed_by: uid}}
```

`matched_count == 0` means refused — expired, unknown, or already redeemed by
this user. There is no read-then-write window, so double-tapping a code cannot
grant two offers. The distinct refusal reasons are resolved with a follow-up
read only to word the reply.

## 2. Nightly broadcast

`coupon_processor(bot)` runs as a background task in main.py next to
`refund_processor` and `payment_recovery_processor`, started with
`asyncio.create_task`.

Each cycle: sleep until the next 00:00 UTC, generate 10 codes, insert them,
post one channel message listing the codes and how to redeem.

Restart safety: a `system` document records the last batch date. A restart at
03:00 does not re-post that night's batch; a bot that was down across midnight
posts the missed batch on next boot. This mirrors the persistence shape the
removed daily-drop used (`get/set_last_daily_discount_time`), which was the
part of that feature worth keeping.

Post target: `UPDATES_CHANNEL` currently holds a `t.me` URL used for a keyboard
button and is not postable. config.py gains `UPDATES_CHANNEL_ID` holding the
raw `@username` / `-100…` form derived from the same env var. The existing URL
form is untouched so the Updates button keeps working. If the raw form is
absent, the coupon system stays off rather than failing every night.

The channel post contains codes only — never reward amounts.

## 3. Redemption

Handled inside the existing `on_text` handler in bot.py, checked **after** all
current state flows (feedback, pay, sell, admin-withdraw, auth) and before the
final `return`. A bare code therefore only counts when the user is not mid-flow.

Guard: `^[A-Z2-9]{6}$` against the uppercased, stripped text, restricted to the
code alphabet. Ordinary chat never reaches the database.

On a successful claim:
1. Roll `random.randint(1, 10)`.
2. Read any unexpired offer.
3. Upsert the offer with `max(rolled, existing_credits)` and `expires_at = now + 6h`.
4. Reply with the effective discount and expiry.

A roll lower than a live offer's discount keeps the higher value and says so,
rather than silently appearing to do nothing. The reward is rolled server-side
at redeem time, so codes carry no value information and are worthless to leak.

`can_grant_offer` is deliberately bypassed: its 24h cooldown and
active-offer rejection belonged to the removed daily drop, and both are
superseded by the once-per-user and best-of rules here.

## 4. Error handling

- Unknown / expired / already-redeemed code: a specific reply, no state change.
- Channel post failure: logged, batch still recorded as posted, codes remain
  redeemable. Retrying the post risks duplicate channel messages; a missing post
  is recoverable by re-sending manually, a double post is not.
- Insert collision on a generated code: regenerate that one code (unique index
  makes this detectable rather than silent).
- User not in the database: refused, same as any other flow.

## 5. Testing

Extend the existing assert-based check with:
- expired code refused
- unknown code refused
- second redemption by the same user refused
- two different users on one code both succeed
- roll stays within 1–10
- lower roll does not downgrade a live offer; higher roll replaces it
- both cases reset `expires_at` to 6h out

## Cost

Unbounded by user count: 10 codes × every active user per night. Because a user
holds at most one offer (best-of), the real ceiling is one discount of ≤10
credits per 6h window per user, not 100 credits per night.
