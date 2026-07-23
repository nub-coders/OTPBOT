import re

OTP_KEYWORDS = [
    "code", "код", "otp", "verify", "verification",
    "confirm", "login", "password", "пароль", "pin",
    "подтверждение", "вход", "авторизац", "token",
    "one-time", "одноразов",
]


def extract_otp(text: str, from_service: bool = False) -> str | None:
    if not text:
        return None

    if from_service:
        codes = re.findall(r"\b(\d{5})\b", text)
        return codes[0] if codes else None

    text_lower = text.lower()
    has_keyword = any(kw in text_lower for kw in OTP_KEYWORDS)
    is_telegram = "telegram" in text_lower or "login code" in text_lower

    if has_keyword or is_telegram:
        codes = re.findall(r"\b(\d{4,8})\b", text)
        if codes:
            return codes[0]

    stripped = text.strip()
    if re.match(r"^\d{4,8}$", stripped):
        return stripped

    return None


def mask_phone(phone: str) -> str:
    if len(phone) > 6:
        return phone[:3] + "*" * (len(phone) - 5) + phone[-2:]
    return phone


def mask_secret(secret: str) -> str:
    """Mask a secret, keeping the first 2 and last 2 chars (e.g. 'aa****zz').

    Short secrets (<= 4 chars) are fully masked so nothing meaningful leaks.
    """
    if not secret:
        return ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


COUNTRY_CODES = [
    ("93", "AF", "Afghanistan", "\U0001f1e6\U0001f1eb"),
    ("355", "AL", "Albania", "\U0001f1e6\U0001f1f1"),
    ("213", "DZ", "Algeria", "\U0001f1e9\U0001f1ff"),
    ("376", "AD", "Andorra", "\U0001f1e6\U0001f1e9"),
    ("244", "AO", "Angola", "\U0001f1e6\U0001f1f4"),
    ("54", "AR", "Argentina", "\U0001f1e6\U0001f1f7"),
    ("374", "AM", "Armenia", "\U0001f1e6\U0001f1f2"),
    ("61", "AU", "Australia", "\U0001f1e6\U0001f1fa"),
    ("43", "AT", "Austria", "\U0001f1e6\U0001f1f9"),
    ("994", "AZ", "Azerbaijan", "\U0001f1e6\U0001f1ff"),
    ("973", "BH", "Bahrain", "\U0001f1e7\U0001f1ed"),
    ("880", "BD", "Bangladesh", "\U0001f1e7\U0001f1e9"),
    ("375", "BY", "Belarus", "\U0001f1e7\U0001f1fe"),
    ("32", "BE", "Belgium", "\U0001f1e7\U0001f1ea"),
    ("55", "BR", "Brazil", "\U0001f1e7\U0001f1f7"),
    ("359", "BG", "Bulgaria", "\U0001f1e7\U0001f1ec"),
    ("855", "KH", "Cambodia", "\U0001f1f0\U0001f1ed"),
    ("237", "CM", "Cameroon", "\U0001f1e8\U0001f1f2"),
    ("1", "US", "United States", "\U0001f1fa\U0001f1f8"),
    ("1403", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1416", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1431", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1437", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1438", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1450", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1506", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1514", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1548", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1579", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1581", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1587", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1604", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1613", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1639", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1647", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1672", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1705", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1709", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1742", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1778", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1780", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1782", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1807", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1819", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1825", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1867", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1873", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1902", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1905", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("1942", "CA", "Canada", "\U0001f1e8\U0001f1e6"),
    ("56", "CL", "Chile", "\U0001f1e8\U0001f1f1"),
    ("86", "CN", "China", "\U0001f1e8\U0001f1f3"),
    ("57", "CO", "Colombia", "\U0001f1e8\U0001f1f4"),
    ("506", "CR", "Costa Rica", "\U0001f1e8\U0001f1f7"),
    ("385", "HR", "Croatia", "\U0001f1ed\U0001f1f7"),
    ("53", "CU", "Cuba", "\U0001f1e8\U0001f1fa"),
    ("357", "CY", "Cyprus", "\U0001f1e8\U0001f1fe"),
    ("420", "CZ", "Czech Republic", "\U0001f1e8\U0001f1ff"),
    ("45", "DK", "Denmark", "\U0001f1e9\U0001f1f0"),
    ("20", "EG", "Egypt", "\U0001f1ea\U0001f1ec"),
    ("372", "EE", "Estonia", "\U0001f1ea\U0001f1ea"),
    ("251", "ET", "Ethiopia", "\U0001f1ea\U0001f1f9"),
    ("358", "FI", "Finland", "\U0001f1eb\U0001f1ee"),
    ("33", "FR", "France", "\U0001f1eb\U0001f1f7"),
    ("995", "GE", "Georgia", "\U0001f1ec\U0001f1ea"),
    ("49", "DE", "Germany", "\U0001f1e9\U0001f1ea"),
    ("233", "GH", "Ghana", "\U0001f1ec\U0001f1ed"),
    ("245", "GW", "Guinea-Bissau", "\U0001f1ec\U0001f1fc"),
    ("30", "GR", "Greece", "\U0001f1ec\U0001f1f7"),
    ("299", "GL", "Greenland", "\U0001f1ec\U0001f1f1"),
    ("852", "HK", "Hong Kong", "\U0001f1ed\U0001f1f0"),
    ("36", "HU", "Hungary", "\U0001f1ed\U0001f1fa"),
    ("354", "IS", "Iceland", "\U0001f1ee\U0001f1f8"),
    ("91", "IN", "India", "\U0001f1ee\U0001f1f3"),
    ("62", "ID", "Indonesia", "\U0001f1ee\U0001f1e9"),
    ("98", "IR", "Iran", "\U0001f1ee\U0001f1f7"),
    ("964", "IQ", "Iraq", "\U0001f1ee\U0001f1f6"),
    ("353", "IE", "Ireland", "\U0001f1ee\U0001f1ea"),
    ("972", "IL", "Israel", "\U0001f1ee\U0001f1f1"),
    ("39", "IT", "Italy", "\U0001f1ee\U0001f1f9"),
    ("81", "JP", "Japan", "\U0001f1ef\U0001f1f5"),
    ("962", "JO", "Jordan", "\U0001f1ef\U0001f1f4"),
    ("77", "KZ", "Kazakhstan", "\U0001f1f0\U0001f1ff"),
    ("254", "KE", "Kenya", "\U0001f1f0\U0001f1ea"),
    ("82", "KR", "South Korea", "\U0001f1f0\U0001f1f7"),
    ("965", "KW", "Kuwait", "\U0001f1f0\U0001f1fc"),
    ("996", "KG", "Kyrgyzstan", "\U0001f1f0\U0001f1ec"),
    ("371", "LV", "Latvia", "\U0001f1f1\U0001f1fb"),
    ("961", "LB", "Lebanon", "\U0001f1f1\U0001f1e7"),
    ("370", "LT", "Lithuania", "\U0001f1f1\U0001f1f9"),
    ("352", "LU", "Luxembourg", "\U0001f1f1\U0001f1fa"),
    ("60", "MY", "Malaysia", "\U0001f1f2\U0001f1fe"),
    ("52", "MX", "Mexico", "\U0001f1f2\U0001f1fd"),
    ("373", "MD", "Moldova", "\U0001f1f2\U0001f1e9"),
    ("976", "MN", "Mongolia", "\U0001f1f2\U0001f1f3"),
    ("212", "MA", "Morocco", "\U0001f1f2\U0001f1e6"),
    ("95", "MM", "Myanmar", "\U0001f1f2\U0001f1f2"),
    ("977", "NP", "Nepal", "\U0001f1f3\U0001f1f5"),
    ("31", "NL", "Netherlands", "\U0001f1f3\U0001f1f1"),
    ("64", "NZ", "New Zealand", "\U0001f1f3\U0001f1ff"),
    ("234", "NG", "Nigeria", "\U0001f1f3\U0001f1ec"),
    ("47", "NO", "Norway", "\U0001f1f3\U0001f1f4"),
    ("968", "OM", "Oman", "\U0001f1f4\U0001f1f2"),
    ("92", "PK", "Pakistan", "\U0001f1f5\U0001f1f0"),
    ("507", "PA", "Panama", "\U0001f1f5\U0001f1e6"),
    ("51", "PE", "Peru", "\U0001f1f5\U0001f1ea"),
    ("63", "PH", "Philippines", "\U0001f1f5\U0001f1ed"),
    ("48", "PL", "Poland", "\U0001f1f5\U0001f1f1"),
    ("351", "PT", "Portugal", "\U0001f1f5\U0001f1f9"),
    ("974", "QA", "Qatar", "\U0001f1f6\U0001f1e6"),
    ("40", "RO", "Romania", "\U0001f1f7\U0001f1f4"),
    ("7", "RU", "Russia", "\U0001f1f7\U0001f1fa"),
    ("966", "SA", "Saudi Arabia", "\U0001f1f8\U0001f1e6"),
    ("381", "RS", "Serbia", "\U0001f1f7\U0001f1f8"),
    ("65", "SG", "Singapore", "\U0001f1f8\U0001f1ec"),
    ("421", "SK", "Slovakia", "\U0001f1f8\U0001f1f0"),
    ("386", "SI", "Slovenia", "\U0001f1f8\U0001f1ee"),
    ("27", "ZA", "South Africa", "\U0001f1ff\U0001f1e6"),
    ("34", "ES", "Spain", "\U0001f1ea\U0001f1f8"),
    ("94", "LK", "Sri Lanka", "\U0001f1f1\U0001f1f0"),
    ("46", "SE", "Sweden", "\U0001f1f8\U0001f1ea"),
    ("41", "CH", "Switzerland", "\U0001f1e8\U0001f1ed"),
    ("886", "TW", "Taiwan", "\U0001f1f9\U0001f1fc"),
    ("992", "TJ", "Tajikistan", "\U0001f1f9\U0001f1ef"),
    ("255", "TZ", "Tanzania", "\U0001f1f9\U0001f1ff"),
    ("66", "TH", "Thailand", "\U0001f1f9\U0001f1ed"),
    ("90", "TR", "Turkey", "\U0001f1f9\U0001f1f7"),
    ("993", "TM", "Turkmenistan", "\U0001f1f9\U0001f1f2"),
    ("256", "UG", "Uganda", "\U0001f1fa\U0001f1ec"),
    ("380", "UA", "Ukraine", "\U0001f1fa\U0001f1e6"),
    ("971", "AE", "UAE", "\U0001f1e6\U0001f1ea"),
    ("44", "GB", "United Kingdom", "\U0001f1ec\U0001f1e7"),
    ("998", "UZ", "Uzbekistan", "\U0001f1fa\U0001f1ff"),
    ("58", "VE", "Venezuela", "\U0001f1fb\U0001f1ea"),
    ("84", "VN", "Vietnam", "\U0001f1fb\U0001f1f3"),
    ("967", "YE", "Yemen", "\U0001f1fe\U0001f1ea"),
    ("260", "ZM", "Zambia", "\U0001f1ff\U0001f1f2"),
    ("263", "ZW", "Zimbabwe", "\U0001f1ff\U0001f1fc"),
    ("268", "SZ", "Eswatini", "\U0001f1f8\U0001f1ff"),
]

_PREFIX_MAP = sorted(COUNTRY_CODES, key=lambda x: len(x[0]), reverse=True)
_CODE_MAP = {code: (name, flag) for _, code, name, flag in COUNTRY_CODES}


def detect_country(phone: str) -> tuple[str, str, str]:
    """Returns (code, name, flag) from a phone number. Falls back to XX/Unknown."""
    if not phone:
        return "XX", "Unknown", "\U0001f3f3️"
    digits = re.sub(r"\D", "", str(phone))
    for prefix, code, name, flag in _PREFIX_MAP:
        if digits.startswith(prefix):
            return code, name, flag
    return "XX", "Unknown", "\U0001f3f3️"


def get_country_flag(country_code: str) -> str:
    entry = _CODE_MAP.get(country_code)
    return entry[1] if entry else "\U0001f3f3️"


def get_country_name(country_code: str) -> str:
    entry = _CODE_MAP.get(country_code)
    return entry[0] if entry else "Unknown"


# ── Account age estimation ──
# Telegram has no API for creation date, but user IDs are assigned roughly
# sequentially over time. We interpolate against known ID->date checkpoints.
# Accuracy is approximate (within a few months).

_ID_CHECKPOINTS = [
    (1_000_000, 2013.6),
    (5_000_000, 2014.2),
    (10_000_000, 2014.6),
    (50_000_000, 2015.5),
    (100_000_000, 2016.0),
    (200_000_000, 2017.0),
    (300_000_000, 2018.0),
    (500_000_000, 2019.0),
    (1_000_000_000, 2019.6),
    (2_000_000_000, 2021.0),
    (4_000_000_000, 2022.3),
    (5_000_000_000, 2022.8),
    (6_000_000_000, 2023.5),
    (7_000_000_000, 2024.0),
    (7_500_000_000, 2024.6),
    (8_000_000_000, 2025.2),
]


def estimate_account_year(user_id: int) -> int | None:
    """Estimate the year a Telegram account was created from its user ID.
    Returns the approximate year, or None if it can't be estimated."""
    if not user_id or user_id <= 0:
        return None

    pts = _ID_CHECKPOINTS
    if user_id <= pts[0][0]:
        return int(pts[0][1])
    if user_id >= pts[-1][0]:
        return round(pts[-1][1])

    for i in range(len(pts) - 1):
        id_lo, yr_lo = pts[i]
        id_hi, yr_hi = pts[i + 1]
        if id_lo <= user_id <= id_hi:
            frac = (user_id - id_lo) / (id_hi - id_lo)
            return round(yr_lo + frac * (yr_hi - yr_lo))
    return None


def extract_year_from_reg_month(reg_month) -> int | None:
    """Extract 4-digit registration year from Telegram registration_month string or value (e.g. '05.2024' or '2024.05')."""
    if not reg_month:
        return None
    try:
        s = str(reg_month).strip()
        import re
        m = re.search(r'\b(20[1-3][0-9])\b', s)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def format_timestamp(ts: int) -> str:
    """Format Unix timestamp into readable date-time string."""
    if not ts:
        return "Unknown"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")


async def get_active_sessions_info(client) -> tuple[int, str]:
    """Invoke GetAuthorizations and return (session_count, formatted_session_info)."""
    from pyrogram.raw.functions.account import GetAuthorizations

    res = await client.invoke(GetAuthorizations())
    authorizations = getattr(res, "authorizations", [])

    session_info = "**ACTIVE SESSIONS**\n\n"
    for session in authorizations:
        app_name = getattr(session, "app_name", "Unknown")
        current = getattr(session, "current", False)

        session_info += (
            f"<blockquote>App Name: {app_name}</blockquote>\n"
            f"<blockquote>Current Session: {current}</blockquote>\n\n"
        )
    return len(authorizations), session_info




def search_country(query: str) -> list[tuple[str, str, str]]:
    """Fuzzy search countries by name or flag. Returns list of (code, name, flag)."""
    query = query.strip()
    if not query:
        return []

    for prefix, code, name, flag in COUNTRY_CODES:
        if query == flag:
            return [(code, name, flag)]

    q = query.lower()

    exact = []
    for prefix, code, name, flag in COUNTRY_CODES:
        if q == name.lower() or q == code.lower():
            exact.append((code, name, flag))
    if exact:
        return exact

    partial = []
    for prefix, code, name, flag in COUNTRY_CODES:
        if q in name.lower():
            partial.append((code, name, flag))
    return partial[:5]
