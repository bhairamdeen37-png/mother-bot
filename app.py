import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram.error import InvalidToken, TelegramError

import clone
import core
import db
import mainbot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
_locks = defaultdict(asyncio.Lock)


def sender(d):
    for k in ("message", "edited_message", "callback_query", "pre_checkout_query"):
        if k in d:
            return d[k].get("from", {}).get("id")
    return None


async def process(bid, data):
    try:
        async with _locks[(bid, sender(data))]:
            if bid == "main":
                await mainbot.handle(data)
            else:
                c = await core.get_clone(int(bid))
                if c and c["status"] == "active":
                    await clone.handle(c, data)
    except Exception:
        log.exception("update failed")
    if len(_locks) > 20000:
        for k in [k for k, v in _locks.items() if not v.locked()]:
            _locks.pop(k, None)


# ---------- background jobs ----------
async def reminders():
    now = int(time.time())
    for o in await db.q("SELECT id,expiry,remind3,remind1 FROM owners WHERE expiry>0 AND id!=?", core.OWNER_ID):
        left = o["expiry"] - now
        if left <= 0:
            if o["remind1"] < 2:
                await core.notify(o["id"], "⏰ Tumhara Premium khatam ho gaya. Bots free limits par aa gaye "
                                           f"({core.FREE_USERS} users, premium features band). Renew: /upgrade")
                await db.ex("UPDATE owners SET remind1=2 WHERE id=?", o["id"])
        elif left <= 86400:
            if o["remind1"] < 1:
                await core.notify(o["id"], "⏳ Premium 1 din me khatam hone wala hai. Renew: /upgrade")
                await db.ex("UPDATE owners SET remind1=1, remind3=1 WHERE id=?", o["id"])
        elif left <= 3 * 86400 and not o["remind3"]:
            await core.notify(o["id"], "⏳ Premium 3 din me khatam hone wala hai. Renew: /upgrade")
            await db.ex("UPDATE owners SET remind3=1 WHERE id=?", o["id"])


async def health():
    for c in await db.q("SELECT id,token,owner_id,username FROM clones WHERE status='active'"):
        try:
            b = await core.get_bot(core.dec(c["token"]))
            await b.get_me()
        except InvalidToken:
            await db.ex("UPDATE clones SET status='dead' WHERE id=?", c["id"])
            core.inval(c["id"])
            await core.notify(c["owner_id"], f"⚠️ @{c['username']} ka token invalid ho gaya (revoke hua?). Bot band. Naya bot banao: /create")
            await core.log(f"💀 Clone dead: @{c['username']} ({c['id']})")
        except TelegramError:
            pass
        await asyncio.sleep(0.3)


async def reports():
    now = int(time.time())
    day = now - 86400
    for c in await db.q("SELECT id,token,owner_id FROM clones WHERE status='active' AND last_report<?", day):
        if not await core.is_premium(c["owner_id"]):
            continue
        r = await db.batch([
            ("SELECT COUNT(*) c FROM users WHERE clone_id=? AND first_seen>?", (c["id"], day)),
            ("SELECT COUNT(*) c FROM msglog WHERE clone_id=? AND dir='in' AND ts>?", (c["id"], day)),
            ("SELECT COUNT(*) c FROM users WHERE clone_id=?", (c["id"],)),
        ])
        try:
            b = await core.get_bot(core.dec(c["token"]))
            await b.send_message(c["owner_id"], f"📊 Daily report (24 ghante)\n🆕 Naye users: {r[0][0]['c']}\n"
                                                f"💬 Messages: {r[1][0]['c']}\n👥 Kul users: {r[2][0]['c']}")
        except TelegramError:
            pass
        await db.ex("UPDATE clones SET last_report=? WHERE id=?", now, c["id"])


async def cleanup():
    now = int(time.time())
    for s in await db.q("SELECT clone_id,autoclean FROM settings WHERE autoclean>0"):
        await db.ex("DELETE FROM msglog WHERE clone_id=? AND ts<?", s["clone_id"], now - s["autoclean"] * 86400)
    await db.ex("DELETE FROM msgmap WHERE ts<?", now - 90 * 86400)


async def minutely():
    while True:
        await asyncio.sleep(60)
        try:
            await clone.run_scheduled()
        except Exception:
            log.exception("scheduled")


async def hourly():
    await asyncio.sleep(90)
    while True:
        for fn in (reminders, health, reports, cleanup):
            try:
                await fn()
            except Exception:
                log.exception(fn.__name__)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    await db.init()
    main = await core.get_bot(core.MAIN_TOKEN)
    me = await main.get_me()
    core.MAIN_USERNAME = me.username
    await main.set_webhook(
        url=f"{core.BASE_URL}/wh/main", secret_token=core.secret_for("main"),
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )
    log.info("Main bot @%s ready, webhook %s/wh/main", me.username, core.BASE_URL)
    tasks = [asyncio.create_task(minutely()), asyncio.create_task(hourly())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health_route():
    return {"ok": True}


@app.post("/wh/{bid}")
async def webhook(bid: str, request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != core.secret_for(bid):
        return Response(status_code=403)
    data = await request.json()
    core.spawn(process(bid, data))
    return {"ok": True}
