import hmac
import hashlib
import time
import logging
import aiohttp
import base64
import json
import urllib.request
from decimal import Decimal
from config import (
    RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
    BINANCE_API_KEY, BINANCE_API_SECRET,
)

log = logging.getLogger(__name__)


def _razorpay_request(method: str, path: str, data: dict = None) -> dict:
    url = f"https://api.razorpay.com/v1/{path.lstrip('/')}"
    auth_str = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_razorpay_qr(plan_label: str, amount_paisa: int, user_id: int = 0) -> dict | None:
    try:
        return _razorpay_request("POST", "payments/qr_codes", {
            "type": "upi_qr",
            "name": plan_label,
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": amount_paisa,
            "close_by": int(time.time()) + 900,
            "description": f"otpbot|{user_id}|{plan_label}",
            "notes": {
                "project": "otpbot",
                "user_id": str(user_id),
                "plan": plan_label,
            },
        })
    except Exception as e:
        log.error("Razorpay QR creation failed: %s", e)
        return None


def check_razorpay_payment(qr_id: str, expected_amount: int) -> str:
    try:
        status = _razorpay_request("GET", f"payments/qr_codes/{qr_id}")
        if status.get("payments_amount_received", 0) >= expected_amount:
            return "paid"
        if status.get("status") == "closed":
            return "expired"
        return "pending"
    except Exception as e:
        log.error("Razorpay check failed: %s", e)
        return "error"


async def get_binance_deposit_address(coin: str = "USDT", network: str = "BSC") -> tuple[bool, dict]:
    ts = int(time.time() * 1000)
    params = f"coin={coin}&network={network}&timestamp={ts}&recvWindow=60000"
    sig = hmac.new(BINANCE_API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"https://api.binance.com/sapi/v1/capital/deposit/address?{params}&signature={sig}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                j = await resp.json()
    except Exception as e:
        return False, {"error": str(e)}
    if isinstance(j, dict) and j.get("address"):
        return True, {"address": j["address"], "tag": j.get("tag", "")}
    return False, {"error": j.get("msg", str(j)) if isinstance(j, dict) else str(j)}


async def verify_binance_deposit(tx_hash: str, asset: str = "USDT", min_amount: float = 0.0) -> tuple[bool, str]:
    ts = int(time.time() * 1000)
    params = {"timestamp": ts, "recvWindow": 60000}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"https://api.binance.com/sapi/v1/capital/deposit/hisrec?{query}&signature={sig}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                j = await resp.json()
    except Exception as e:
        return False, f"Network error: {e}"
    if not isinstance(j, list):
        return False, f"API error: {j}"

    expected = Decimal(str(min_amount))
    for dep in j:
        if dep.get("txId") != tx_hash or dep.get("coin") != asset:
            continue
        if dep.get("status") != 1:
            return False, "Deposit not credited yet. Wait for confirmation."
        received = Decimal(str(dep.get("amount", 0)))
        if received < expected:
            return False, f"Amount mismatch: expected {expected}, received {received}."
        return True, f"Confirmed: {received} {asset}"

    return False, "No matching deposit found. Check your TX hash."

