import asyncio
import logging
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
        user_id = a["user_id"]
        price = a.get("price", 0)
        otp_received = a.get("otp_received", False)

        if otp_received:
            await db.mark_session_sold(phone, user_id)
            log.info("[%s] Orphan — OTP was received, marked sold", phone)
        else:
            if price > 0:
                await db.add_credits(user_id, price)
            log.info("[%s] Orphan — no OTP, refunded %d credits to user %d", phone, price, user_id)
            try:
                await bot.send_message(
                    user_id,
                    f"⚠️ **Bot restarted** — your session for `{phone}` was interrupted.\n\n"
                    f"💰 **{price} credits** have been refunded.",
                )
            except Exception:
                pass

        await db.remove_active_assignment(phone)

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
    await recover_orphaned_assignments(bot)
    await recover_pending_payments(bot)

    asyncio.create_task(refund_processor(bot))
    asyncio.create_task(payment_recovery_processor(bot))
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
