import asyncio
import logging
from pyrogram import idle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def main():
    from bot import create_bot
    import clients

    bot = create_bot()
    clients.set_bot(bot)

    await bot.start()
    log.info("Bot started.")

    await clients.validate_sessions()

    log.info("OTP Bot is running. Press Ctrl+C to stop.")
    await idle()

    await clients.disconnect_all()
    await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
