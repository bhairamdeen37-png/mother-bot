import os
import httpx

_URL = os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://").rstrip("/")
_TOKEN = os.environ["TURSO_AUTH_TOKEN"]
_client = None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=25, headers={"Authorization": f"Bearer {_TOKEN}"})
    return _client


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _val(c):
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return float(c["value"])
    return c.get("value")


async def batch(stmts):
    """stmts = [(sql, (args...)), ...] -> list of row-lists (dicts)"""
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}} for s, args in stmts]
    reqs.append({"type": "close"})
    r = await _http().post(f"{_URL}/v2/pipeline", json={"requests": reqs})
    r.raise_for_status()
    out = []
    for res in r.json()["results"][:-1]:
        if res["type"] == "error":
            raise RuntimeError(res["error"]["message"])
        rs = res["response"]["result"]
        cols = [c["name"] for c in rs["cols"]]
        out.append([dict(zip(cols, [_val(x) for x in row])) for row in rs["rows"]])
    return out


async def q(sql, *args):
    return (await batch([(sql, args)]))[0]


async def one(sql, *args):
    r = await q(sql, *args)
    return r[0] if r else None


async def ex(sql, *args):
    await q(sql, *args)


async def kv_get(key, default=None):
    r = await one("SELECT value FROM kv WHERE key=?", key)
    return r["value"] if r else default


async def kv_set(key, value):
    await ex("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", key, str(value))


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, name TEXT, username TEXT, banned INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0, expiry INTEGER DEFAULT 0, remind3 INTEGER DEFAULT 0, remind1 INTEGER DEFAULT 0, joined INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS clones(id INTEGER PRIMARY KEY, token TEXT, username TEXT, name TEXT, owner_id INTEGER, status TEXT DEFAULT 'active', created INTEGER, last_report INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS i_clones_owner ON clones(owner_id)",
    "CREATE TABLE IF NOT EXISTS admins(clone_id INTEGER, user_id INTEGER, PRIMARY KEY(clone_id,user_id))",
    "CREATE TABLE IF NOT EXISTS users(clone_id INTEGER, user_id INTEGER, name TEXT, username TEXT, first_seen INTEGER, last_seen INTEGER, blocked INTEGER DEFAULT 0, shadow INTEGER DEFAULT 0, vip INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, status TEXT DEFAULT 'open', tags TEXT DEFAULT '', note TEXT DEFAULT '', ref_by INTEGER, msgs INTEGER DEFAULT 0, rating INTEGER, PRIMARY KEY(clone_id,user_id))",
    "CREATE TABLE IF NOT EXISTS msgmap(clone_id INTEGER, admin_chat INTEGER, admin_msg INTEGER, user_id INTEGER, ts INTEGER, PRIMARY KEY(clone_id,admin_chat,admin_msg))",
    "CREATE TABLE IF NOT EXISTS msglog(id INTEGER PRIMARY KEY AUTOINCREMENT, clone_id INTEGER, user_id INTEGER, dir TEXT, text TEXT, ts INTEGER)",
    "CREATE INDEX IF NOT EXISTS i_msglog ON msglog(clone_id,user_id)",
    "CREATE TABLE IF NOT EXISTS settings(clone_id INTEGER PRIMARY KEY, welcome TEXT, offline TEXT, hours TEXT, maintenance INTEGER DEFAULT 0, captcha INTEGER DEFAULT 0, forcejoin TEXT, autoclean INTEGER DEFAULT 30)",
    "CREATE TABLE IF NOT EXISTS autoreplies(clone_id INTEGER, keyword TEXT, response TEXT, PRIMARY KEY(clone_id,keyword))",
    "CREATE TABLE IF NOT EXISTS saved(clone_id INTEGER, name TEXT, text TEXT, PRIMARY KEY(clone_id,name))",
    "CREATE TABLE IF NOT EXISTS scheduled(id INTEGER PRIMARY KEY AUTOINCREMENT, clone_id INTEGER, user_id INTEGER, text TEXT, run_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, stars INTEGER, days INTEGER, charge_id TEXT UNIQUE, ts INTEGER, source TEXT)",
    "CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER, used INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS coupon_uses(code TEXT, owner_id INTEGER, PRIMARY KEY(code,owner_id))",
    "CREATE TABLE IF NOT EXISTS state(owner_id INTEGER PRIMARY KEY, state TEXT)",
    "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)",
]


async def init():
    await batch([(s, ()) for s in SCHEMA])
