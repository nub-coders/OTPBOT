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
<title>Verify</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;color:#ccc;font-family:monospace;font-size:14px;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.wrap{max-width:360px;width:100%}
h1{font-size:16px;font-weight:normal;color:#fff;margin-bottom:6px}
p{color:#777;margin-bottom:20px}
.cf-turnstile{margin-bottom:20px}
#status{padding:8px 0;display:none}
#status.ok{color:#6f6;display:block}
#status.err{color:#f66;display:block}
#status.wait{color:#cc0;display:block}
a.back{color:#6af;text-decoration:none;border-bottom:1px solid #6af}
a.back:hover{color:#fff}
</style>
</head>
<body>
<div class="wrap">
<h1>verify you're human</h1>
<p>complete the check below.</p>
<div class="cf-turnstile" data-sitekey="{{SITE_KEY}}" data-callback="onToken" data-theme="dark"></div>
<div id="status"></div>
</div>
<script>
const vtoken="{{VTOKEN}}";
async function onToken(cfToken){
  const el=document.getElementById("status");
  el.className="status wait";el.textContent="checking...";el.style.display="block";
  try{
    const r=await fetch("/api/verify",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({vtoken,token:cfToken})});
    const d=await r.json();
    if(d.ok){el.className="status ok";el.innerHTML='done. <a class="back" href="https://t.me/{{BOT_USERNAME}}">back to bot</a>';}
    else{el.className="status err";el.textContent=d.error||"failed. try again.";turnstile.reset();}
  }catch{el.className="status err";el.textContent="network error.";turnstile.reset();}
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
<title>Expired</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;color:#ccc;font-family:monospace;font-size:14px;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.wrap{max-width:360px;width:100%}
h1{font-size:16px;font-weight:normal;color:#f66;margin-bottom:6px}
p{color:#777}
</style>
</head>
<body>
<div class="wrap">
<h1>link expired</h1>
<p>this link was already used or expired. go back to the bot and tap verify for a new one.</p>
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
    bot_username = ""
    try:
        from bot import bot as bot_app
        if bot_app and bot_app.me:
            bot_username = bot_app.me.username or ""
    except Exception:
        pass
    html = (VERIFY_HTML
            .replace("{{SITE_KEY}}", TURNSTILE_SITE_KEY)
            .replace("{{VTOKEN}}", vtoken)
            .replace("{{BOT_USERNAME}}", bot_username))
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
                log.info("Referral reward: %s credits to user %d for referring %d", REFERRAL_VERIFY_BONUS, referrer_id, uid)
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
