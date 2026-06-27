import re

OTP_KEYWORDS = [
    "code", "код", "otp", "verify", "verification",
    "confirm", "login", "password", "пароль", "pin",
    "подтверждение", "вход", "авторизац", "token",
    "one-time", "одноразов",
]


def extract_otp(text: str) -> str | None:
    if not text:
        return None

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
