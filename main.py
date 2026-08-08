import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pyrogram import idle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def recover_orphaned_assignments(bot):
    """On startup, refund users who had active assignments when bot died."""
    import database as db

    assignments = await db.get_all_active_assignments()
    if not assignments:
        return

    log.info("Found %d orphaned assignment(s), processing...", len(assignments))
    for a in assignments:
        phone = a["phone_number"]
        # Isolate each orphan: a DB error on one must not abort recovery of the
        # rest, or the remaining users stay un-refunded and their assignments
        # orphaned across the next restart too.
        try:
            user_id = a["user_id"]
            price = a.get("price", 0)
            otp_received = a.get("otp_received", False)

            # Atomically claim the assignment before refunding — guards against
            # double-refund if recovery runs twice (restart loop or concurrent startup).
            if not await db.claim_orphan_assignment(phone):
                log.info("[%s] Orphan already claimed by another recovery pass, skipping", phone)
                continue

            if otp_received:
                await db.mark_session_sold(phone, user_id, a.get("price", 0), a.get("order_id"))
                log.info("[%s] Orphan — OTP was received, marked sold", phone)
            else:
                if price > 0:
                    cd = a.get("credits_deducted", price)
                    bd = a.get("balance_deducted", 0)
                    await db.restore_purchase_funds(user_id, cd, bd)
                restored = await db.restore_offer(user_id, a.get("offer_granted_at"))
                offer_line = f"\n🎁 **Discount offer restored!**" if restored else ""
                log.info("[%s] Orphan — no OTP, refunded %d credits to user %d", phone, price, user_id)
                try:
                    await bot.send_message(
                        user_id,
                        f"⚠️ **Bot restarted** — your session for `{phone}` was interrupted.\n\n"
                        f"💰 **{price} credits** have been refunded.{offer_line}",
                    )
                except Exception:
                    pass
        except Exception as e:
            log.error("[%s] Failed to recover orphaned assignment: %s", phone, e)

    log.info("Orphaned assignments recovered.")


async def recover_pending_payments(bot):
    """On startup, resume checking any pending Razorpay payments."""
    import database as db
    import payments
    from bot import get_credit_plan, award_razorpay_payment

    pending = await db.get_pending_payments()
    if not pending:
        return

    log.info("Found %d pending payment(s), resuming...", len(pending))
    for p in pending:
        qr_id = p["qr_id"]
        plan_key = p["plan_key"]
        amount_inr = p["amount_inr"]
        assign_phone = p.get("assign_phone")

        plan = get_credit_plan(plan_key)
        if not plan:
            await db.mark_pending_payment_expired(qr_id)
            continue

        status = await asyncio.to_thread(
            payments.check_razorpay_payment, qr_id, amount_inr,
        )

        if status == "paid":
            await award_razorpay_payment(
                p["user_id"], qr_id, plan_key, assign_phone=assign_phone,
            )
            log.info("Recovered payment %s for user %d", qr_id, p["user_id"])
        elif status == "expired":
            await db.mark_pending_payment_expired(qr_id)
            log.info("Payment %s expired", qr_id)
        else:
            log.info("Payment %s still pending, will keep checking", qr_id)

    log.info("Pending payments recovery done.")


async def refund_processor(bot):
    """Background task that processes pending refunds every 60 seconds."""
    import database as db
    from bot import alert
    while True:
        try:
            due = await db.get_due_refunds()
            for refund in due:
                user_id = refund["user_id"]
                amount = refund["amount"]
                # Atomic claim: only the caller that flips status to "done" credits.
                # Guards against double-credit on restart or concurrent processors.
                claimed = await db.claim_and_process_refund(refund["_id"])
                if not claimed:
                    continue
                await db.add_credits(user_id, amount)
                await db.mark_refund_done(refund["_id"])
                new_balance = await db.get_credits(user_id)
                log.info("Refund processed: %d credits to user %d", amount, user_id)
                phone = refund.get("phone_number", "N/A")
                await alert(bot,
                    f"💰 **Refund Issued**\n\n"
                    f"👤 User: `{user_id}`\n"
                    f"📱 Number: `{phone}`\n"
                    f"➕ Credits: +{amount}\n"
                    f"💰 New balance: {new_balance}"
                )
                # No offer line here: the release path already restored the offer
                # and said so. Restoring again an hour later would un-spend an
                # offer the user may have spent on another purchase since.
                try:
                    await bot.send_message(
                        user_id,
                        f"💰 **Credits refunded!**\n\n"
                        f"📱 Number: `{phone}`\n"
                        f"➕ **{amount}** credits returned to your account.\n"
                        f"💰 New balance: **{new_balance}**",
                    )
                except Exception:
                    pass
        except Exception as e:
            log.error("Refund processor error: %s", e)
        await asyncio.sleep(60)


async def payment_recovery_processor(bot):
    """Background task that checks still-pending payments every 30 seconds."""
    import database as db
    import payments
    from bot import get_credit_plan, award_razorpay_payment

    while True:
        try:
            pending = await db.get_pending_payments()
            for p in pending:
                qr_id = p["qr_id"]
                plan_key = p["plan_key"]
                amount_inr = p["amount_inr"]
                assign_phone = p.get("assign_phone")

                plan = get_credit_plan(plan_key)
                if not plan:
                    await db.mark_pending_payment_expired(qr_id)
                    continue

                status = await asyncio.to_thread(
                    payments.check_razorpay_payment, qr_id, amount_inr,
                )

                if status == "paid":
                    if await award_razorpay_payment(
                        p["user_id"], qr_id, plan_key, assign_phone=assign_phone,
                    ):
                        log.info("Payment %s confirmed for user %d", qr_id, p["user_id"])
                elif status == "expired":
                    await db.mark_pending_payment_expired(qr_id)
        except Exception as e:
            log.error("Payment recovery processor error: %s", e)
        await asyncio.sleep(30)


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


async def main():
    from bot import create_bot
    import clients
    import database as db
    from config import TURNSTILE_SITE_KEY, ENABLE_VERIFICATION
    import verification

    bot = create_bot()
    clients.set_bot(bot)

    await bot.start()
    log.info("Bot started.")

    await db.ensure_indexes()
    log.info("Database indexes ensured.")

    if ENABLE_VERIFICATION and TURNSTILE_SITE_KEY:
        await verification.start_server()

    await clients.validate_sessions()
    cleared = await db.clear_stale_reservations()
    if cleared:
        log.info("Cleared %d stale session reservation(s) from a prior crash.", cleared)
    await recover_orphaned_assignments(bot)
    await recover_pending_payments(bot)

    asyncio.create_task(refund_processor(bot))
    asyncio.create_task(payment_recovery_processor(bot))
    asyncio.create_task(coupon_processor(bot))
    log.info("Background processors started.")

    log.info("OTP Bot is running. Press Ctrl+C to stop.")
    await idle()

    await clients.disconnect_all()
    await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
