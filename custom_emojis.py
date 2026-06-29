"""Custom emoji registry backed by data/custom_emoji_ids.json.

Constants are plain Unicode strings — safe to use in button labels AND message text.

For Telegram custom emoji sticker rendering in message TEXT only, call render() explicitly:
    await bot.send_message(user_id, render('✅') + " Done!", parse_mode="html")

DO NOT use render() output in InlineKeyboardButton labels — Telegram does not support
custom emoji tags in buttons.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

_DATA_PATH = Path(__file__).resolve().parent / 'data' / 'custom_emoji_ids.json'


@lru_cache(maxsize=1)
def _load_registry() -> Dict[str, List[int]]:
    registry: Dict[str, List[int]] = {}
    if not _DATA_PATH.exists():
        return registry
    try:
        payload = json.loads(_DATA_PATH.read_text(encoding='utf-8'))
    except Exception:
        return registry
    for pack in payload:
        for item in pack.get('items', []):
            emoji = item.get('emoji')
            document_id = item.get('document_id')
            if not emoji or document_id is None:
                continue
            registry.setdefault(emoji, []).append(int(document_id))
    return registry


def available_emojis() -> List[str]:
    return sorted(_load_registry().keys())


def document_ids_for(emoji: str) -> List[int]:
    return list(_load_registry().get(emoji, []))


def pick_document_id(emoji: str, index: int = 0) -> Optional[int]:
    ids = document_ids_for(emoji)
    if not ids:
        return None
    return ids[index % len(ids)]


def render(emoji: str, fallback: Optional[str] = None, index: int = 0) -> str:
    """Render a Telegram custom emoji tag for use in HTML message TEXT only.

    WARNING: Do NOT use the output in InlineKeyboardButton labels.
    Telegram does not support custom emoji tags in buttons.

    Args:
        emoji: The visible emoji and lookup key.
        fallback: Plain-text shown if no document ID found.
        index: Which document ID to use when multiple exist.
    """
    document_id = pick_document_id(emoji, index=index)
    visible = fallback if fallback is not None else emoji
    if document_id is None:
        return visible
    return f'<emoji id="{document_id}">{visible}</emoji>'


# ─────────────────────────────────────────────────────────────────────────────
# All constants below are PLAIN UNICODE — safe for buttons AND text
# ─────────────────────────────────────────────────────────────────────────────

# STATUS & RESULTS
SUCCESS      = '✅'
ERROR        = '❌'
WARNING      = '⚠️'
INFO         = 'ℹ️'
LOADING      = '⏳'
DONE         = '☑️'
CANCELLED    = '🚫'
PENDING      = '🔄'
FAILED       = '💢'
BLOCKED      = '⛔'
VERIFIED     = '✔️'
UNVERIFIED   = '✖️'
ONLINE       = '🟢'
OFFLINE      = '🔴'
IDLE         = '🟡'
BANNED       = '🚫'
MUTED        = '🔇'
UNMUTED      = '🔊'
BUSY         = '⛔'
PREMIUM      = '💎'
BADGE        = '🏅'
MEDAL        = '🥇'
TROPHY       = '🏆'
NEW_TAG      = '🆕'
HOT          = '♨️'
TRENDING     = '📈'
DROPPED      = '📉'

# OTP / AUTH
OTP          = '🔑'
CODE         = '🔢'
LOCK         = '🔒'
UNLOCK       = '🔓'
KEY          = '🗝️'
PHONE        = '📱'
SMS          = '💬'
SHIELD       = '🛡️'
SESSION      = '🖥️'
LOGIN        = '🚪'
LOGOUT       = '🚶'
PASSWORD     = '🔐'
TOKEN        = '🎫'
TIMER        = '⏱️'
TIMEOUT      = '⌛'
EXPIRED      = '🕰️'

# PAYMENTS & CREDITS
CREDIT       = '💳'
MONEY        = '💰'
WALLET       = '👛'
COIN         = '🪙'
DIAMOND      = '💎'
GIFT         = '🎁'
RECEIPT      = '🧾'
INVOICE      = '📃'
PAID         = '✅'
REFUND       = '↩️'
BANK         = '🏦'
RUPEE        = '₹'
DOLLAR       = '💵'
USDT         = '🪙'
CRYPTO       = '₿'
PRICE_TAG    = '🏷️'
TRENDING_UP  = '📈'
TRENDING_DN  = '📉'
PLAN         = '📋'
BALANCE      = '⚖️'
TOPUP        = '➕'
DEDUCT       = '➖'

# ADMIN & MANAGEMENT
ADMIN        = '👮'
OWNER        = '👑'
BOT          = '🤖'
GEAR         = '⚙️'
SETTINGS     = '🛠️'
DATABASE     = '🗄️'
BROADCAST    = '📢'
BAN          = '🔨'
UNBAN        = '🔓'
MUTE         = '🔇'
UNMUTE       = '🔊'
WARN         = '⚠️'
STATS        = '📊'
LOGS         = '📜'
BACKUP       = '💾'
RESTART      = '🔁'
DEPLOY       = '🚀'
DEBUG        = '🐛'
TERMINAL     = '⌨️'
CONFIG       = '📝'

# USERS & SOCIAL
USER         = '👤'
USERS        = '👥'
NEW_USER     = '🆕'
VIP          = '🌟'
GUEST        = '🧑'
ANONYMOUS    = '👻'
SPY          = '🕵️'
SUPPORT      = '🎧'
HANDSHAKE    = '🤝'
WAVE         = '👋'
CLAP         = '👏'
THUMBS_UP    = '👍'
THUMBS_DOWN  = '👎'
HEART        = '❤️'
BROKEN_HEART = '💔'
PRAY         = '🙏'
COOL         = '😎'
ANGRY        = '😡'
SLEEP        = '💤'

# NOTIFICATIONS & MESSAGES
BELL         = '🔔'
NO_BELL      = '🔕'
PIN          = '📌'
INBOX        = '📥'
OUTBOX       = '📤'
MAIL         = '📧'
NOTE         = '📝'
ALERT        = '🚨'
ANNOUNCE     = '📣'
FORWARD      = '↪️'
REPLY        = '↩️'
MENTION      = '🔖'
LINK         = '🔗'
CHANNEL      = '📡'
GROUP        = '👥'
ID_BADGE     = '🆔'

# ACTIONS & UI
SEARCH       = '🔍'
ADD          = '➕'
REMOVE       = '➖'
EDIT         = '✏️'
DELETE       = '🗑️'
SAVE         = '💾'
COPY         = '📋'
SEND         = '📤'
DOWNLOAD     = '⬇️'
UPLOAD       = '⬆️'
REFRESH      = '🔄'
BACK         = '◀️'
NEXT         = '▶️'
CLOSE        = '✖️'
CONFIRM      = '✅'
MENU         = '📂'
LIST         = '📃'
FILTER       = '🔽'
SORT         = '🔼'
HOME         = '🏠'
HELP         = '❓'
FAQ          = '💡'

# COUNTRIES & GEOGRAPHY
GLOBE        = '🌐'
MAP          = '🗺️'
FLAG         = '🏳️'
LOCATION     = '📍'
INDIA        = '🇮🇳'
USA          = '🇺🇸'
UK           = '🇬🇧'
RUSSIA       = '🇷🇺'
PAKISTAN     = '🇵🇰'
BANGLADESH   = '🇧🇩'
NEPAL        = '🇳🇵'

# TIME & PROGRESS
CLOCK        = '🕐'
ALARM        = '⏰'
HOURGLASS    = '⌛'
CALENDAR     = '📅'
TODAY        = '📆'
FAST         = '💨'
SLOW         = '🐢'
DEADLINE     = '⏰'
INFINITE     = '♾️'

# MISC / FUN
FIRE         = '🔥'
SPARK        = '✨'
ROCKET       = '🚀'
ZAP          = '⚡'
STAR         = '⭐'
CROWN        = '👑'
ROBOT        = '🤖'
IDEA         = '💡'
MAGIC        = '🪄'
CRYSTAL      = '🔮'
BOMB         = '💣'
SKULL        = '💀'
NINJA        = '🥷'
ALIEN        = '👽'
RECYCLE      = '♻️'


# ─────────────────────────────────────────────────────────────────────────────
# HTML Rendering & Pyrogram Monkey-Patching for Custom Emojis
# ─────────────────────────────────────────────────────────────────────────────

import re
import html

def render_custom_emojis(text: str) -> str:
    """Replace registered emojis with custom emoji tags.
    
    This function tokenizes the text to avoid replacing emojis that are:
    1. Inside HTML tags (e.g. <a href="...">)
    2. Inside existing custom emoji tags (e.g. <emoji id="...">...</emoji>)
    3. Inside inline code (`...`) or code blocks (```...```)
    """
    if not isinstance(text, str) or not text:
        return text

    registry = _load_registry()
    if not registry:
        return text

    # Tokenize the text using a regex that matches tags and code blocks
    pattern = re.compile(r'(<emoji\b[^>]*>[\s\S]*?</emoji>|```[\s\S]*?```|`[^`\n]+`|<[^>]+>)')
    parts = pattern.split(text)

    sorted_emojis = sorted(registry.keys(), key=len, reverse=True)
    escaped_emojis = [re.escape(e) for e in sorted_emojis]
    emoji_pattern = '|'.join(escaped_emojis)

    if emoji_pattern:
        emoji_re = re.compile(f'({emoji_pattern})')
        for i in range(0, len(parts), 2):  # Only process non-matched parts (even indices)
            def replace_match(match):
                em_char = match.group(1)
                doc_id = pick_document_id(em_char)
                if doc_id is not None:
                    return f'<emoji id="{doc_id}">{em_char}</emoji>'
                return em_char
            parts[i] = emoji_re.sub(replace_match, parts[i])

    return "".join(parts)


def patch_pyrogram_for_custom_emojis():
    """Monkey-patch Pyrogram Client methods to automatically render custom emojis.
    """
    from pyrogram import Client
    from pyrogram.enums import ParseMode

    orig_send_message = Client.send_message
    async def new_send_message(self, chat_id, text, parse_mode=None, *args, **kwargs):
        # Determine effective parse mode (if None, it defaults to client.parse_mode or ParseMode.DEFAULT)
        effective_mode = parse_mode if parse_mode is not None else (self.parse_mode or ParseMode.DEFAULT)
        
        # If parse mode is disabled, do not render custom emojis
        if effective_mode != ParseMode.DISABLED:
            text = render_custom_emojis(text)
            # If the user explicitly passed ParseMode.MARKDOWN, we convert it to ParseMode.DEFAULT (None)
            # because the strict Markdown parser would escape the HTML <emoji> tags.
            if parse_mode == ParseMode.MARKDOWN:
                parse_mode = ParseMode.DEFAULT

        return await orig_send_message(self, chat_id, text, parse_mode=parse_mode, *args, **kwargs)
    Client.send_message = new_send_message

    orig_edit_message_text = Client.edit_message_text
    async def new_edit_message_text(self, chat_id, message_id, text, parse_mode=None, *args, **kwargs):
        effective_mode = parse_mode if parse_mode is not None else (self.parse_mode or ParseMode.DEFAULT)
        if effective_mode != ParseMode.DISABLED:
            text = render_custom_emojis(text)
            if parse_mode == ParseMode.MARKDOWN:
                parse_mode = ParseMode.DEFAULT
        return await orig_edit_message_text(self, chat_id, message_id, text, parse_mode=parse_mode, *args, **kwargs)
    Client.edit_message_text = new_edit_message_text


