"""Send only FLAG custom emojis to @just_a_dev for preview."""
import asyncio
import json
from pathlib import Path
from pyrogram import Client
from pyrogram.enums import ParseMode
from config import API_ID, API_HASH, BOT_TOKEN

TARGET = "just_a_dev"
DATA_PATH = Path(__file__).resolve().parent / "data" / "custom_emoji_ids.json"


def is_flag(emoji: str) -> bool:
    """Return True if the emoji is a regional indicator / flag sequence."""
    return any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in emoji)


async def main():
    bot = Client(
        "emoji_test_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
    await bot.start()

    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    # Collect all flag items across all packs
    flags = []
    for pack in payload:
        for item in pack.get("items", []):
            emoji = item.get("emoji", "")
            doc_id = item.get("document_id")
            if doc_id and is_flag(emoji):
                rendered = f'<emoji id="{doc_id}">{emoji}</emoji>'
                flags.append(rendered)

    print(f"Found {len(flags)} flag emojis")

    # Send in chunks of 40
    chunk_size = 40
    chunks = [flags[i : i + chunk_size] for i in range(0, len(flags), chunk_size)]

    for idx, chunk in enumerate(chunks, 1):
        header = f"<b>🏳️ Flag Emojis — Part {idx}/{len(chunks)}</b>\n\n"
        msg = header + "  ".join(chunk)
        try:
            await bot.send_message(TARGET, msg, parse_mode=ParseMode.HTML)
            print(f"✅ Sent part {idx}/{len(chunks)}")
        except Exception as e:
            print(f"⚠️  Failed part {idx}: {e}")
        await asyncio.sleep(1)

    print(f"\n🎉 Done! Sent {len(flags)} flag emojis to @{TARGET}")
    await bot.stop()


asyncio.run(main())
