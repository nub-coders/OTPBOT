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

def render_html(text: str) -> str:
    """Convert Pyrogram markdown to HTML and replace registered emojis with custom emoji tags.
    
    Safe to call multiple times or on already formatted HTML.
    """
    if not isinstance(text, str) or not text:
        return text

    # 1. Escape HTML special characters
    escaped = html.escape(text)

    # 2. Convert Pyrogram Markdown to HTML
    # Placeholder token uses only letters/digits — NO markdown-special chars
    # (no _ * ~ | `) so the bold/underline/italic passes below can't mangle it.
    placeholders = []
    def save_code_block(match):
        content = match.group(1)
        placeholders.append(f"<pre><code>{content}</code></pre>")
        return f"XXCODEBLOCKPLACEHOLDER{len(placeholders) - 1}XX"

    # Match ``` or ```` blocks
    escaped = re.sub(r'```(?:[a-zA-Z0-9_-]+\n)?(.*?)\n?```', save_code_block, escaped, flags=re.DOTALL)

    # Inline code: `text`
    def save_inline_code(match):
        content = match.group(1)
        placeholders.append(f"<code>{content}</code>")
        return f"XXCODEBLOCKPLACEHOLDER{len(placeholders) - 1}XX"
    escaped = re.sub(r'`(.*?)`', save_inline_code, escaped)

    # Bold: **text**
    escaped = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', escaped)
    # Underline: __text__
    escaped = re.sub(r'__(.*?)__', r'<u>\1</u>', escaped)
    # Italic: *text*
    escaped = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', escaped)
    # Strikethrough: ~~text~~
    escaped = re.sub(r'~~(.*?)~~', r'<s>\1</s>', escaped)
    # Spoiler: ||text||
    escaped = re.sub(r'\|\|(.*?)\|\|', r'<tg-spoiler>\1</tg-spoiler>', escaped)
    # Links: [text](url)
    escaped = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', escaped)

    # 3. Render Emojis in non-HTML parts
    parts = re.split(r'(<[^>]+>|___CODE_BLOCK_PLACEHOLDER_\d+___)', escaped)
    
    registry = _load_registry()
    if registry:
        sorted_emojis = sorted(registry.keys(), key=len, reverse=True)
        escaped_emojis = [re.escape(e) for e in sorted_emojis]
        pattern = '|'.join(escaped_emojis)
        
        if pattern:
            emoji_re = re.compile(f'({pattern})')
            
            for i in range(len(parts)):
                if i % 2 == 0:
                    def replace_match(match):
                        em_char = match.group(1)
                        doc_id = pick_document_id(em_char)
                        if doc_id is not None:
                            return f'<emoji id="{doc_id}">{em_char}</emoji>'
                        return em_char
                    parts[i] = emoji_re.sub(replace_match, parts[i])
                    
    html_text = "".join(parts)
    
    # 4. Restore code blocks
    for idx, ph in enumerate(placeholders):
        html_text = html_text.replace(f"___CODE_BLOCK_PLACEHOLDER_{idx}___", ph)
        
    return html_text


def patch_pyrogram_for_custom_emojis():
    """Monkey-patch Pyrogram Client methods to automatically render custom emojis.

    Only Client-level methods are patched because Message.reply / .edit_text
    call Client.send_message / .edit_message_text internally — patching both
    levels caused double-rendering (html.escape ran twice, destroying tags).
    """
    from pyrogram import Client
    from pyrogram.enums import ParseMode

    orig_send_message = Client.send_message
    async def new_send_message(self, chat_id, text, parse_mode=None, *args, **kwargs):
        if parse_mode is None or parse_mode == ParseMode.MARKDOWN:
            text = render_html(text)
            parse_mode = ParseMode.HTML
        return await orig_send_message(self, chat_id, text, parse_mode=parse_mode, *args, **kwargs)
    Client.send_message = new_send_message

    orig_edit_message_text = Client.edit_message_text
    async def new_edit_message_text(self, chat_id, message_id, text, parse_mode=None, *args, **kwargs):
        if parse_mode is None or parse_mode == ParseMode.MARKDOWN:
            text = render_html(text)
            parse_mode = ParseMode.HTML
        return await orig_edit_message_text(self, chat_id, message_id, text, parse_mode=parse_mode, *args, **kwargs)
    Client.edit_message_text = new_edit_message_text

