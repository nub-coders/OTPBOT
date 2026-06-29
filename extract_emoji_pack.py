"""
Extract a Telegram custom emoji sticker pack and append it to
data/custom_emoji_ids.json.

Usage:
    python extract_emoji_pack.py tgsemoji112
    python extract_emoji_pack.py https://t.me/addemoji/tgsemoji112
"""

import asyncio
import json
import sys
from pathlib import Path
from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN

DATA_PATH = Path(__file__).resolve().parent / "data" / "custom_emoji_ids.json"


async def extract(short_name: str):
    from pyrogram.raw import functions, types as raw_types

    bot = Client(
        "emoji_extractor_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    await bot.start()
    print(f"🔍 Fetching sticker set via raw API: {short_name}")

    try:
        raw_set = await bot.invoke(
            functions.messages.GetStickerSet(
                stickerset=raw_types.InputStickerSetShortName(short_name=short_name),
                hash=0,
            )
        )
    except Exception as e:
        print(f"❌ Failed to get sticker set: {e}")
        await bot.stop()
        return

    items = []
    for doc in raw_set.documents:
        emoji = ""
        for attr in doc.attributes:
            if hasattr(attr, "alt"):
                emoji = attr.alt
                break
        items.append({
            "document_id": doc.id,
            "emoji": emoji,
            "mime_type": getattr(doc, "mime_type", "application/x-tgsticker"),
        })

    pack = {
        "short_name": short_name,
        "count": len(items),
        "items": items,
    }

    # Load existing data
    if DATA_PATH.exists():
        existing = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    else:
        existing = []

    # Remove existing entry with same short_name (to allow re-extraction)
    existing = [p for p in existing if p.get("short_name") != short_name]
    existing.append(pack)

    DATA_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    await bot.stop()

    print(f"✅ Saved {len(items)} emojis from '{short_name}' to {DATA_PATH}")
    print(f"📦 Total packs in JSON: {len(existing)}")

    # Show sample
    print("\n🎨 Sample emojis:")
    for item in items[:10]:
        print(f"  {item['emoji']}  →  id={item['document_id']}")


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else "tgsemoji112"
    # Strip URL if full link was provided
    short_name = raw.rstrip("/").split("/")[-1]
    asyncio.run(extract(short_name))
