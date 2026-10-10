import asyncio
import hashlib
import hmac
import os
import time

from cryptography.fernet import Fernet
from telegram import Bot

import db

MAIN_TOKEN = os.environ["MAIN_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL_ID") or 0)
FORCE_JOIN = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()  # jaise @mychannel
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_MINUTES") or 330)  # India = +5:30
_fernet = Fernet(os.environ["ENCRYPT_KEY"].encode())

MAIN_USERNAME = ""  # startup par set hota hai

# ===== Limits & Prices (yahin se badlo) =====
FREE_USERS = 70
FREE_SAVED = 3
FREE_CLONES = 1
PREMIUM_CLONES = 3
TRIAL_DAYS = 3
PLANS = {30: 50, 90: 120, 365: 350}  # din -> Stars
BRAND = "\n\n⚡ Powered by TEAMVB\nTry it @{main}"
# ============================================

_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


def enc(t):
    return _fernet.encrypt(t.encode()).decode()


def dec(t):
    return _fernet.decrypt(t.encode()).decode()


def secret_for(bid):
    return hmac.new(WEBHOOK_SECRET.encode(), str(bid).encode(), hashlib.sha256).hexdigest()[:32]


def fdate(ts):
    return time.strftime("%d %b %Y", time.gmtime(ts + TZ_OFFSET * 60))


# ---------- bot cache ----------
_bots = {}


async def get_bot(token):
    b = _bots.get(token)
    if b is None:
        b = Bot(token)
        await b.initialize()
        _bots[token] = b
    return b


async def drop_bot(token):
    b = _bots.pop(token, None)
    if b:
        try:
            await b.shutdown()
        except Exception:
            pass


# ---------- clone cache ----------
_cc = {}


async def get_clone(cid):
    c = _cc.get(cid)
    if c and time.time() - c[0] < 20:
        return c[1]
    r = await db.one("SELECT * FROM clones WHERE id=?", cid)
    _cc[cid] = (time.time(), r)
    return r


def inval(cid):
    _cc.pop(cid, None)


# ---------- premium ----------
_pc = {}


async def premium_until(owner_id):
    c = _pc.get(owner_id)
    if c and time.time() - c[0] < 30:
        return c[1]
    r = await db.one("SELECT expiry FROM owners WHERE id=?", owner_id)
    e = r["expiry"] if r else 0
    _pc[owner_id] = (time.time(), e)
    return e


async def is_premium(owner_id):
    if owner_id == OWNER_ID:
        return True
    return await premium_until(owner_id) > time.time()


async def add_premium(owner_id, days):
    now = int(time.time())
    r = await db.one("SELECT expiry FROM owners WHERE id=?", owner_id)
    base = max(now, r["expiry"] if r else 0)
    new = base + days * 86400
    await db.ex("UPDATE owners SET expiry=?, remind3=0, remind1=0 WHERE id=?", new, owner_id)
    _pc.pop(owner_id, None)
    return new


async def ensure_owner(u):
    await db.ex(
        "INSERT INTO owners(id,name,username,joined) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        u.id, u.full_name, u.username, int(time.time()),
    )


# ---------- messaging helpers ----------
async def notify(uid, text, **kw):
    try:
        await (await get_bot(MAIN_TOKEN)).send_message(uid, text, **kw)
        return True
    except Exception:
        return False


async def log(text):
    if LOG_CHANNEL:
        await notify(LOG_CHANNEL, text)
