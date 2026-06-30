import secrets
import logging
from aiohttp import web
import aiohttp
import database as db
from config import TURNSTILE_SECRET_KEY, TURNSTILE_SITE_KEY, VERIFY_PORT, VERIFY_URL, REFERRAL_VERIFY_BONUS

log = logging.getLogger(__name__)


async def create_verification_link(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    await db.create_verify_token(uid, token)
    return f"{VERIFY_URL.rstrip('/')}/verify?t={token}"


VERIFY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Human Verification</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0e1621;color:#e4e6eb;display:flex;align-items:center;justify-content:center;
min-height:100vh;padding:20px}
.card{background:#1b2533;border-radius:16px;padding:40px 32px;max-width:420px;
width:100%;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.4)}
h1{font-size:1.5rem;margin-bottom:8px}
p{color:#8b9bb4;font-size:.95rem;margin-bottom:24px}
.cf-turnstile{display:flex;justify-content:center;margin-bottom:24px}
.status{padding:12px 20px;border-radius:10px;font-size:.95rem;display:none}
.status.ok{background:#1a3a2a;color:#4ade80;display:block}
.status.err{background:#3a1a1a;color:#f87171;display:block}
.status.wait{background:#2a2a1a;color:#facc15;display:block}
.expired{color:#f87171;font-size:1.1rem;margin-top:16px}
</style>
</head>
<body>
<div class="card" id="card">
<h1>Human Verification</h1>
<p>Complete the challenge below to access the bot.</p>
<div class="cf-turnstile" data-sitekey="{{SITE_KEY}}" data-callback="onToken" data-theme="dark"></div>
<div id="status"></div>
</div>
<script>
const vtoken = "{{VTOKEN}}";
async function onToken(cfToken) {
  const el = document.getElementById("status");
  el.className = "status wait";
  el.textContent = "Verifying...";
  el.style.display = "block";
  try {
    const r = await fetch("/api/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({vtoken, token: cfToken})
    });
    const d = await r.json();
    if (d.ok) {
      el.className = "status ok";
      el.textContent = "Verified! You can close this page and return to the bot.";
    } else {
      el.className = "status err";
      el.textContent = d.error || "Verification failed. Try again.";
      turnstile.reset();
    }
  } catch {
    el.className = "status err";
    el.textContent = "Network error. Try again.";
    turnstile.reset();
  }
}
</script>
</body>
</html>
"""

EXPIRED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link Expired</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0e1621;color:#e4e6eb;display:flex;align-items:center;justify-content:center;
min-height:100vh;padding:20px}
.card{background:#1b2533;border-radius:16px;padding:40px 32px;max-width:420px;
width:100%;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.4)}
h1{font-size:1.5rem;margin-bottom:12px;color:#f87171}
p{color:#8b9bb4;font-size:.95rem}
</style>
</head>
<body>
<div class="card">
<h1>Link Expired</h1>
<p>This verification link has expired or was already used.<br>Go back to the bot and tap <b>Verify</b> to get a new link.</p>
</div>
</body>
</html>
"""


async def handle_page(request):
    vtoken = request.query.get("t")
    if not vtoken:
        return web.Response(text="Invalid link.", status=400)
    doc = await db.db.verify_tokens.find_one({"token": vtoken})
    if not doc or doc.get("used"):
        return web.Response(text=EXPIRED_HTML, content_type="text/html", status=410)
    from datetime import datetime, timezone
    if doc["expires_at"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return web.Response(text=EXPIRED_HTML, content_type="text/html", status=410)
    html = (VERIFY_HTML
            .replace("{{SITE_KEY}}", TURNSTILE_SITE_KEY)
            .replace("{{VTOKEN}}", vtoken))
    return web.Response(text=html, content_type="text/html")


async def handle_verify(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)

    vtoken = data.get("vtoken")
    cf_token = data.get("token")
    if not vtoken or not cf_token:
        return web.json_response({"ok": False, "error": "Missing fields"}, status=400)

    uid = await db.consume_verify_token(vtoken)
    if uid is None:
        return web.json_response({"ok": False, "error": "Link expired or already used. Get a new one from the bot."})

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET_KEY, "response": cf_token},
        ) as resp:
            result = await resp.json()

    if not result.get("success"):
        log.warning("Turnstile rejected token for uid %s: %s", uid, result)
        return web.json_response({"ok": False, "error": "Challenge failed. Get a new link from the bot."})

    await db.mark_verified(uid)
    log.info("User %d passed Turnstile verification", uid)

    user = await db.get_user(uid)
    if user and not await db.is_referral_rewarded(uid):
        referrer_id = user.get("referred_by")
        if referrer_id and await db.get_user(referrer_id):
            await db.mark_referral_rewarded(uid)
            if REFERRAL_VERIFY_BONUS > 0:
                await db.add_referral_earning(referrer_id, REFERRAL_VERIFY_BONUS)
                log.info("Referral reward: %d credits to user %d for referring %d", REFERRAL_VERIFY_BONUS, referrer_id, uid)
                try:
                    from bot import bot
                    import custom_emojis as em
                    uname = user.get("first_name") or user.get("username") or str(uid)
                    new_balance = await db.get_credits(referrer_id)
                    await bot.send_message(
                        referrer_id,
                        f"{em.GIFT} **Referral Reward!**\n\n"
                        f"Your referral **{uname}** joined and verified.\n"
                        f"{em.MONEY} +{REFERRAL_VERIFY_BONUS} credits added!\n"
                        f"{em.MONEY} Balance: **{new_balance}**",
                    )
                except Exception as e:
                    log.warning("Failed to notify referrer %d: %s", referrer_id, e)

    return web.json_response({"ok": True})


async def start_server():
    app = web.Application()
    app.router.add_get("/verify", handle_page)
    app.router.add_post("/api/verify", handle_verify)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", VERIFY_PORT)
    await site.start()
    log.info("Verification server started on port %d", VERIFY_PORT)
    return runner
