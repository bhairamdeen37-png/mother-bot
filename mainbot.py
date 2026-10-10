import asyncio
import csv
import io
import re
import time

from telegram import Bot, LabeledPrice, Update
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram.error import InvalidToken, TelegramError

import core
import db

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
ALLOWED = ["message", "edited_message", "callback_query"]

# ================= STYLE =================
LINE = "━━━━━━━━━━━━━━━━━━"
_last = {}  # chat_id -> last panel message id (sirf ek panel screen par rahe)


def _mk():
    d = {}
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        d[c] = chr(0x1D5D4 + i)
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        d[c] = chr(0x1D5EE + i)
    for i, c in enumerate("0123456789"):
        d[c] = chr(0x1D7EC + i)
    return d


_BOLD = _mk()


def bd(t):
    """Stylish bold letters: Spark -> 𝗦𝗽𝗮𝗿𝗸"""
    return "".join(_BOLD.get(c, c) for c in t)


def card(icon, title, *lines):
    return f"✦{LINE}✦\n   {icon} {bd(title)} {icon}\n✦{LINE}✦\n\n" + "\n".join(lines)


def B(t, d):
    return Btn(t, callback_data=d)


def back_kb(label="🏠 Menu", data="home"):
    return Kb([[B(label, data)]])


def menu(uid):
    rows = [[B("➕ Create Bot", "create"), B("🤖 My Bots", "my")],
            [B("💎 Premium", "up"), B("🎟 Coupon", "coupon")],
            [B("📖 Help", "help")]]
    if uid == core.OWNER_ID:
        rows.append([B("👑 Owner Panel", "o:menu")])
    return Kb(rows)


async def safe_delete(m):
    try:
        await m.delete()
    except TelegramError:
        pass


async def _purge(bot, chat, old):
    ids = set()
    if old is not None:
        ids.add(old.message_id)
    last = _last.get(chat)
    if last:
        ids.add(last)
    for i in ids:
        try:
            await bot.delete_message(chat, i)
        except TelegramError:
            pass


async def show(bot, chat, text, kb=None, old=None):
    """Purana panel delete karo, naya bhejo."""
    await _purge(bot, chat, old)
    s = await bot.send_message(chat, text, reply_markup=kb)
    _last[chat] = s.message_id
    return s


# ================= TEXTS =================
def home_text(name):
    return card(
        "⚡", "SPARK LIVE BOT",
        f"👋 {bd('Welcome')}, {name}!", "",
        "◈ Apna personal inbox bot 1 minute me banao",
        "◈ Log bot ko message karein → tumhe milega",
        "◈ Tum reply karo → seedha unhe jayega",
        "◈ Tumhara account kisi ko nahi dikhega 🔒", "",
        f"🆓 {bd('Free')}  ➜  {core.FREE_CLONES} bot • {core.FREE_USERS} users",
        f"💎 {bd('Premium')}  ➜  {core.PREMIUM_CLONES} bots • Unlimited users", "",
        "👇 Neeche se option chuno",
    )


def help_text():
    return card(
        "📖", "HELP",
        f"➕ {bd('Create Bot')}  –  BotFather ka token do, bot ready",
        f"🤖 {bd('My Bots')}  –  status, pause, delete",
        f"💎 {bd('Premium')}  –  Stars se upgrade",
        f"🎟 {bd('Coupon')}  –  code se free Premium", "",
        f"🆓 {bd('Free')}: {core.FREE_CLONES} bot, {core.FREE_USERS} users",
        f"💎 {bd('Premium')}: {core.PREMIUM_CLONES} bots, unlimited users,",
        "     broadcast, auto-reply, working hours,",
        "     VIP, tags, force join, branding hatao", "",
        "⌨️ Commands: /start  /create  /mybot  /upgrade  /coupon",
    )


OHELP = """/owner – panel
/clones [page], /search text
/pause ID, /resume ID, /remove ID   (clone ID)
/ban OWNERID, /unban OWNERID
/give OWNERID DIN – free premium
/broadcast (reply) – sab main-bot users ko
/dm OWNERID text
/newcoupon CODE DIN USES, /delcoupon CODE, /coupons
/payments, /stats
/maintenance on|off – naye clones band/chalu
/backup – CSV files"""

STATUS = {"active": "🟢 Active", "paused": "⏸ Paused", "dead": "💀 Dead", "banned": "🚫 Banned"}


# ================= ENTRY =================
async def handle(data):
    bot = await core.get_bot(core.MAIN_TOKEN)
    upd = Update.de_json(data, bot)
    if upd.pre_checkout_query:
        await pre_checkout(upd.pre_checkout_query)
    elif upd.callback_query:
        await on_cb(bot, upd.callback_query)
    elif upd.message and upd.message.chat.type == "private" and upd.message.from_user:
        await on_msg(bot, upd.message)


async def pre_checkout(q):
    p = q.invoice_payload or ""
    ok = p.startswith("plan:") and p[5:].isdigit() and int(p[5:]) in core.PLANS and q.total_amount == core.PLANS[int(p[5:])]
    await q.answer(ok=ok, error_message=None if ok else "Invalid plan")


async def home(bot, chat, u, old=None):
    await show(bot, chat, home_text(u.first_name), menu(u.id), old)


async def on_msg(bot, m):
    u, chat = m.from_user, m.chat_id
    await core.ensure_owner(u)
    o = await db.one("SELECT banned FROM owners WHERE id=?", u.id)
    if o["banned"] and u.id != core.OWNER_ID:
        return
    if m.successful_payment:
        await paid(bot, m)
        return
    text = m.text or ""

    if not text.startswith("/"):
        st = await db.one("SELECT state FROM state WHERE owner_id=?", u.id)
        if st:
            if not text:
                await safe_delete(m)
                return
            kind, _, mid = st["state"].partition(":")
            if mid.isdigit():
                try:
                    await bot.delete_message(chat, int(mid))
                except TelegramError:
                    pass
            await safe_delete(m)
            await db.ex("DELETE FROM state WHERE owner_id=?", u.id)
            if kind == "token":
                await take_token(bot, m, text.strip())
            elif kind == "coupon":
                await redeem(bot, m, text.strip())
            return
        await home(bot, chat, u)
        return

    # user ke /commands delete nahi hote (chat khali nahi dikhegi)
    await db.ex("DELETE FROM state WHERE owner_id=?", u.id)
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].split("@")[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if cmd in ("start", "menu"):
        await home(bot, chat, u)
    elif cmd == "help":
        await show(bot, chat, help_text(), back_kb())
    elif cmd == "create":
        await start_create(bot, chat, u.id)
    elif cmd == "mybot":
        await show_my(bot, chat, u.id)
    elif cmd == "upgrade":
        await show_plans(bot, chat, u.id)
    elif cmd == "coupon":
        if rest:
            await redeem(bot, m, rest.strip())
        else:
            await ask_coupon(bot, chat, u.id)
    elif cmd == "cancel":
        await home(bot, chat, u)
    elif u.id == core.OWNER_ID:
        await owner_cmd(bot, m, cmd, rest)


# ================= CREATE / MANAGE CLONE =================
async def joined(bot, uid):
    if not core.FORCE_JOIN:
        return True
    try:
        mem = await bot.get_chat_member(core.FORCE_JOIN, uid)
        return mem.status in ("member", "administrator", "creator", "owner")
    except TelegramError:
        return True


async def ask_token(bot, chat, uid, old=None, err=None):
    lines = [
        f"①  @BotFather kholo",
        f"②  /newbot bhejo – naam aur username do",
        f"③  Jo {bd('TOKEN')} mile (123456:ABC...) wo yahan paste karo", "",
        "🔐 Token encrypted store hota hai",
    ]
    if err:
        lines += ["", err]
    kb = Kb([[Btn("🤖 Open BotFather", url="https://t.me/BotFather")], [B("❌ Cancel", "home")]])
    s = await show(bot, chat, card("🛠", "CREATE BOT", *lines), kb, old)
    await db.ex("INSERT OR REPLACE INTO state(owner_id,state) VALUES(?,?)", uid, f"token:{s.message_id}")


async def ask_coupon(bot, chat, uid, old=None, err=None):
    lines = ["🎟 Apna coupon code bhejo", "", "Example:  FRIEND30"]
    if err:
        lines += ["", err]
    s = await show(bot, chat, card("🎟", "COUPON", *lines), back_kb("❌ Cancel"), old)
    await db.ex("INSERT OR REPLACE INTO state(owner_id,state) VALUES(?,?)", uid, f"coupon:{s.message_id}")


async def start_create(bot, chat, uid, old=None):
    if uid != core.OWNER_ID and await db.kv_get("maintenance", "0") == "1":
        await show(bot, chat, card("🛠", "MAINTENANCE", "Abhi naye bots banana band hai.", "Thodi der baad aao."), back_kb(), old)
        return
    if not await joined(bot, uid):
        kb = Kb([[Btn("📢 Join Channel", url=f"https://t.me/{core.FORCE_JOIN.lstrip('@')}")],
                 [B("✅ Join kar liya", "create")], [B("🏠 Menu", "home")]])
        await show(bot, chat, card("📢", "JOIN REQUIRED", f"Pehle {core.FORCE_JOIN} join karo,", "phir dobara try karo."), kb, old)
        return
    n = (await db.one("SELECT COUNT(*) c FROM clones WHERE owner_id=?", uid))["c"]
    prem = await core.is_premium(uid)
    lim = 99 if uid == core.OWNER_ID else (core.PREMIUM_CLONES if prem else core.FREE_CLONES)
    if n >= lim:
        lines = [f"Tumhara bot limit ({lim}) poora ho gaya."]
        rows = [[B("🏠 Menu", "home")]]
        if not prem:
            lines += ["", f"💎 Premium me {core.PREMIUM_CLONES} bots bana sakte ho."]
            rows.insert(0, [B("💎 Premium lo", "up")])
        await show(bot, chat, card("⚠️", "LIMIT FULL", *lines), Kb(rows), old)
        return
    await ask_token(bot, chat, uid, old)


async def take_token(bot, m, token):
    u, chat = m.from_user, m.chat_id
    if not TOKEN_RE.match(token) or token == core.MAIN_TOKEN:
        await ask_token(bot, chat, u.id, err="❌ Token galat lag raha hai. Dobara bhejo.")
        return
    err = None
    me = None
    tb = Bot(token)
    try:
        async with tb:
            me = await tb.get_me()
            if await db.one("SELECT id FROM clones WHERE id=?", me.id):
                err = "⚠️ Ye bot pehle se registered hai."
            else:
                await tb.set_webhook(url=f"{core.BASE_URL}/wh/{me.id}", secret_token=core.secret_for(me.id),
                                     allowed_updates=ALLOWED, drop_pending_updates=True)
    except InvalidToken:
        err = "❌ Token invalid hai. Check karke dobara bhejo."
    except TelegramError as e:
        err = f"❌ Error: {e}"
    if err:
        await ask_token(bot, chat, u.id, err=err)
        return

    now = int(time.time())
    await db.batch([
        ("INSERT INTO clones(id,token,username,name,owner_id,status,created,last_report) VALUES(?,?,?,?,?,'active',?,?)",
         (me.id, core.enc(token), me.username, me.full_name, u.id, now, now)),
        ("INSERT OR IGNORE INTO settings(clone_id) VALUES(?)", (me.id,)),
        ("DELETE FROM state WHERE owner_id=?", (u.id,)),
    ])
    lines = [f"🤖 @{me.username}", "",
             "①  Pehle bot kholo aur /start dabao (tum admin ho)",
             "②  /help se saare commands dekho",
             f"③  Test kisi {bd('DUSRE')} account se karo"]
    o = await db.one("SELECT trial_used FROM owners WHERE id=?", u.id)
    if not o["trial_used"] and not await core.is_premium(u.id):
        await core.add_premium(u.id, core.TRIAL_DAYS)
        await db.ex("UPDATE owners SET trial_used=1 WHERE id=?", u.id)
        lines += ["", f"🎁 {core.TRIAL_DAYS} din ka Premium trial free mila!"]
    kb = Kb([[Btn("🤖 Open My Bot", url=f"https://t.me/{me.username}")], [B("🏠 Menu", "home")]])
    await show(bot, chat, card("✅", "BOT READY", *lines), kb)
    await core.log(f"🆕 Clone: @{me.username} ({me.id}) by {u.full_name} ({u.id})")


async def show_my(bot, chat, uid, old=None):
    rows = await db.q("SELECT c.id,c.username,c.status,(SELECT COUNT(*) FROM users x WHERE x.clone_id=c.id) n FROM clones c WHERE owner_id=?", uid)
    until = await core.premium_until(uid)
    if await core.is_premium(uid):
        plan = "💎 Premium" + (f" • {core.fdate(until)} tak" if until else "")
    else:
        plan = f"🆓 Free • {core.FREE_USERS} users/bot"
    if not rows:
        text = card("🤖", "MY BOTS", "Abhi tumhara koi bot nahi hai.", "", plan)
        kb = Kb([[B("➕ Create Bot", "create")], [B("🏠 Menu", "home")]])
    else:
        lines = [f"{STATUS.get(r['status'], '⚪')}  @{r['username']}  •  👥 {r['n']}" for r in rows]
        text = card("🤖", "MY BOTS", *lines, "", plan, "", "👇 Manage karne ke liye bot chuno")
        kb = Kb([[B(f"⚙️ @{r['username']}", f"cl:{r['id']}")] for r in rows] + [[B("🏠 Menu", "home")]])
    await show(bot, chat, text, kb, old)


async def own_clone(uid, cid):
    c = await db.one("SELECT * FROM clones WHERE id=?", cid)
    if c and (c["owner_id"] == uid or uid == core.OWNER_ID):
        return c
    return None


async def show_clone(bot, chat, uid, cid, old=None):
    c = await own_clone(uid, cid)
    if not c:
        await show_my(bot, chat, uid, old)
        return
    n = (await db.one("SELECT COUNT(*) c FROM users WHERE clone_id=?", cid))["c"]
    text = card("🤖", "BOT DETAILS",
                f"◈ Bot: @{c['username']}",
                f"◈ Status: {STATUS.get(c['status'], c['status'])}",
                f"◈ Users: {n}",
                f"◈ Bana: {core.fdate(c['created'])}")
    rows = []
    if c["status"] in ("active", "paused"):
        rows.append([B("⏸ Pause" if c["status"] == "active" else "▶️ Resume", f"pz:{cid}")])
    rows.append([B("🗑 Delete", f"rm:{cid}")])
    rows.append([B("🔙 Back", "my")])
    await show(bot, chat, text, Kb(rows), old)


async def remove_clone(cid):
    c = await db.one("SELECT * FROM clones WHERE id=?", cid)
    if not c:
        return False
    token = core.dec(c["token"])
    try:
        await Bot(token).delete_webhook()
    except Exception:
        pass
    await core.drop_bot(token)
    stmts = [(f"DELETE FROM {t} WHERE clone_id=?", (cid,)) for t in
             ("admins", "users", "msgmap", "msglog", "settings", "autoreplies", "saved", "scheduled")]
    stmts.append(("DELETE FROM clones WHERE id=?", (cid,)))
    await db.batch(stmts)
    core.inval(cid)
    return True


# ================= PAYMENTS (Stars) =================
async def show_plans(bot, chat, uid, old=None):
    until = await core.premium_until(uid)
    status = (f"✅ Premium active: {core.fdate(until)} tak\n    (renew karoge to din jud jayenge)"
              if until > time.time() else "🆓 Abhi Free plan par ho")
    text = card("💎", "PREMIUM",
                f"◈ {core.PREMIUM_CLONES} bots • Unlimited users",
                "◈ Broadcast • Auto-reply • Working hours",
                "◈ VIP • Tags • Notes • Force join",
                "◈ Scheduled msgs • Daily report",
                "◈ Branding hatao", "", status, "", "👇 Plan chuno")
    rows = [[B(f"⭐ {stars} Stars  •  {days} din", f"buy:{days}")] for days, stars in core.PLANS.items()]
    rows.append([B("🏠 Menu", "home")])
    await show(bot, chat, text, Kb(rows), old)


async def send_invoice(bot, chat, days, old=None):
    await _purge(bot, chat, old)
    stars = core.PLANS[days]
    kb = Kb([[Btn("⭐ Pay Now", pay=True)], [B("🔙 Back", "up")]])
    s = await bot.send_invoice(chat_id=chat, title=f"Spark Live Premium – {days} din",
                               description=f"{days} din ka Premium", payload=f"plan:{days}",
                               provider_token="", currency="XTR",
                               prices=[LabeledPrice(f"{days} din Premium", stars)], reply_markup=kb)
    _last[chat] = s.message_id


async def paid(bot, m):
    sp, u = m.successful_payment, m.from_user
    p = sp.invoice_payload or ""
    days = int(p[5:]) if p.startswith("plan:") and p[5:].isdigit() else 0
    if days not in core.PLANS:
        await core.notify(core.OWNER_ID, f"⚠️ Unknown payment payload from {u.id}: {p} (⭐{sp.total_amount}) – manual check karo.")
        return
    if await db.one("SELECT id FROM payments WHERE charge_id=?", sp.telegram_payment_charge_id):
        return
    await db.ex("INSERT INTO payments(owner_id,stars,days,charge_id,ts,source) VALUES(?,?,?,?,?,'stars')",
                u.id, sp.total_amount, days, sp.telegram_payment_charge_id, int(time.time()))
    new = await core.add_premium(u.id, days)
    await show(bot, m.chat_id,
               card("🎉", "PAYMENT SUCCESS", f"💎 Premium {core.fdate(new)} tak active",
                    f"⭐ {sp.total_amount} Stars  •  {days} din", "", "Shukriya! 🙏"),
               menu(u.id))
    await core.notify(core.OWNER_ID, f"💰 {u.full_name} ({u.id}) ne ⭐{sp.total_amount} diye ({days} din)")
    await core.log(f"💰 Payment ⭐{sp.total_amount} from {u.id} ({days}d)")


async def redeem(bot, m, code):
    uid, chat = m.from_user.id, m.chat_id
    await db.ex("DELETE FROM state WHERE owner_id=?", uid)
    code = code.upper()
    c = await db.one("SELECT * FROM coupons WHERE code=?", code)
    if not c or c["used"] >= c["max_uses"]:
        await ask_coupon(bot, chat, uid, err="❌ Coupon invalid ya khatam.")
        return
    if await db.one("SELECT 1 x FROM coupon_uses WHERE code=? AND owner_id=?", code, uid):
        await ask_coupon(bot, chat, uid, err="⚠️ Ye coupon tum pehle use kar chuke ho.")
        return
    await db.batch([("INSERT INTO coupon_uses(code,owner_id) VALUES(?,?)", (code, uid)),
                    ("UPDATE coupons SET used=used+1 WHERE code=?", (code,))])
    new = await core.add_premium(uid, c["days"])
    await show(bot, chat, card("🎉", "COUPON APPLIED", f"◈ +{c['days']} din Premium", f"◈ Premium {core.fdate(new)} tak"), menu(uid))


# ================= CALLBACKS =================
async def on_cb(bot, q):
    u, d = q.from_user, q.data or ""
    msg = q.message
    chat = msg.chat.id
    await core.ensure_owner(u)
    o = await db.one("SELECT banned FROM owners WHERE id=?", u.id)
    if o["banned"] and u.id != core.OWNER_ID:
        await q.answer("Banned", show_alert=True)
        return
    await q.answer()
    await db.ex("DELETE FROM state WHERE owner_id=?", u.id)

    if d == "home":
        await home(bot, chat, u, msg)
    elif d == "create":
        await start_create(bot, chat, u.id, msg)
    elif d == "my":
        await show_my(bot, chat, u.id, msg)
    elif d == "up":
        await show_plans(bot, chat, u.id, msg)
    elif d == "coupon":
        await ask_coupon(bot, chat, u.id, msg)
    elif d == "help":
        await show(bot, chat, help_text(), back_kb(), msg)
    elif d.startswith("buy:"):
        days = int(d[4:]) if d[4:].isdigit() else 0
        if days in core.PLANS:
            await send_invoice(bot, chat, days, msg)
    elif d.startswith("cl:"):
        if d[3:].isdigit():
            await show_clone(bot, chat, u.id, int(d[3:]), msg)
    elif d.startswith(("pz:", "rm:", "rmy:")):
        act, _, cid = d.partition(":")
        c = await own_clone(u.id, int(cid)) if cid.isdigit() else None
        if not c:
            return
        if act == "pz" and c["status"] in ("active", "paused"):
            ns = "paused" if c["status"] == "active" else "active"
            await db.ex("UPDATE clones SET status=? WHERE id=?", ns, c["id"])
            core.inval(c["id"])
            await show_clone(bot, chat, u.id, c["id"], msg)
        elif act == "rm":
            kb = Kb([[B("✅ Haan, delete", f"rmy:{c['id']}"), B("❌ Nahi", f"cl:{c['id']}")]])
            await show(bot, chat, card("⚠️", "DELETE BOT", f"@{c['username']} delete karna hai?", "",
                                       "Saara data (users, chats) hamesha ke liye jayega."), kb, msg)
        elif act == "rmy":
            await remove_clone(c["id"])
            await core.log(f"🗑 Clone removed: @{c['username']} by {u.id}")
            await show(bot, chat, card("🗑", "DELETED", f"@{c['username']} delete ho gaya."), menu(u.id), msg)
    elif d.startswith("o:") and u.id == core.OWNER_ID:
        await owner_cb(bot, chat, d[2:], msg)


# ================= OWNER PANEL =================
def panel_kb():
    return Kb([[B("📊 Dashboard", "o:dash"), B("📋 Clones", "o:cl:0")],
               [B("💰 Payments", "o:pay"), B("🛠 Maintenance", "o:maint")],
               [B("📥 Backup", "o:backup"), B("📖 Commands", "o:help")],
               [B("🏠 Menu", "home")]])


def oback():
    return Kb([[B("🔙 Panel", "o:menu")]])


async def panel_text():
    m = await db.kv_get("maintenance", "0")
    return card("👑", "OWNER PANEL", "◈ Control center",
                f"◈ Naye clones: {'🔴 BAND' if m == '1' else '🟢 chalu'}")


async def dashboard():
    now = int(time.time())
    r = await db.batch([
        ("SELECT status,COUNT(*) c FROM clones GROUP BY status", ()),
        ("SELECT COUNT(*) c FROM owners", ()),
        ("SELECT COUNT(*) c FROM users", ()),
        ("SELECT COUNT(*) c FROM msglog WHERE dir='in' AND ts>?", (now - 86400,)),
        ("SELECT COALESCE(SUM(stars),0) s FROM payments", ()),
        ("SELECT COALESCE(SUM(stars),0) s FROM payments WHERE ts>?", (now - 30 * 86400,)),
        ("SELECT COUNT(*) c FROM owners WHERE expiry>?", (now,)),
    ])
    st = {x["status"]: x["c"] for x in r[0]}
    maint = await db.kv_get("maintenance", "0")
    return card("📊", "DASHBOARD",
                f"🤖 Clones: {sum(st.values())}",
                f"     🟢 {st.get('active', 0)}  ⏸ {st.get('paused', 0)}  💀 {st.get('dead', 0)}  🚫 {st.get('banned', 0)}",
                f"👤 Main-bot users: {r[1][0]['c']}",
                f"👥 Clone users total: {r[2][0]['c']}",
                f"💬 24h messages: {r[3][0]['c']}",
                f"💎 Premium owners: {r[6][0]['c']}",
                f"⭐ Kamai: {r[4][0]['s']} total  |  {r[5][0]['s']} (30 din)",
                f"🛠 Maintenance: {'ON' if maint == '1' else 'off'}")


async def clones_page(page=0):
    rows = await db.q(
        "SELECT c.id,c.username,c.owner_id,c.status,(SELECT COUNT(*) FROM users u WHERE u.clone_id=c.id) n "
        "FROM clones c ORDER BY c.created DESC LIMIT 16 OFFSET ?", page * 15)
    more = len(rows) > 15
    rows = rows[:15]
    if not rows:
        return card("📋", "CLONES", "Koi clone nahi."), False
    lines = [f"{r['id']} • @{r['username']} • 👤{r['owner_id']} • {r['status']} • 👥{r['n']}" for r in rows]
    return card("📋", f"CLONES P{page + 1}", *lines), more


async def payments_text():
    rows = await db.q("SELECT owner_id,stars,days,ts FROM payments ORDER BY id DESC LIMIT 15")
    if not rows:
        return card("💰", "PAYMENTS", "Abhi koi payment nahi.")
    return card("💰", "PAYMENTS", *[f"{core.fdate(r['ts'])} • 👤{r['owner_id']} • ⭐{r['stars']} • {r['days']}d" for r in rows])


async def send_csv(bot, chat, name, rows):
    if not rows:
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(r.values())
    await bot.send_document(chat, io.BytesIO(buf.getvalue().encode()), filename=name)


async def backup(bot, chat):
    await send_csv(bot, chat, "clones.csv", await db.q("SELECT id,username,name,owner_id,status,created FROM clones"))
    await send_csv(bot, chat, "owners.csv", await db.q("SELECT * FROM owners"))
    await send_csv(bot, chat, "payments.csv", await db.q("SELECT * FROM payments"))


async def toggle_maint(on):
    await db.kv_set("maintenance", "1" if on else "0")


async def owner_cb(bot, chat, a, old):
    if a == "menu":
        await show(bot, chat, await panel_text(), panel_kb(), old)
    elif a == "dash":
        await show(bot, chat, await dashboard(), oback(), old)
    elif a.startswith("cl:"):
        page = int(a[3:]) if a[3:].isdigit() else 0
        text, more = await clones_page(page)
        nav = []
        if page > 0:
            nav.append(B("◀ Prev", f"o:cl:{page - 1}"))
        if more:
            nav.append(B("Next ▶", f"o:cl:{page + 1}"))
        rows = ([nav] if nav else []) + [[B("🔙 Panel", "o:menu")]]
        await show(bot, chat, text, Kb(rows), old)
    elif a == "pay":
        await show(bot, chat, await payments_text(), oback(), old)
    elif a == "maint":
        on = await db.kv_get("maintenance", "0") != "1"
        await toggle_maint(on)
        await show(bot, chat, await panel_text(), panel_kb(), old)
    elif a == "backup":
        await backup(bot, chat)
        await show(bot, chat, card("📥", "BACKUP", "Files bhej di gayi hain.", "(Tokens include nahi hain)"), oback(), old)
    elif a == "help":
        await show(bot, chat, card("👑", "OWNER COMMANDS", OHELP), oback(), old)


async def broadcast_all(bot, chat, from_chat, mid):
    rows = await db.q("SELECT id FROM owners WHERE banned=0")
    ok = fail = 0
    for r in rows:
        try:
            await bot.copy_message(r["id"], from_chat, mid)
            ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.06)
    await bot.send_message(chat, f"📢 Broadcast poora: ✅ {ok} ❌ {fail}")


async def owner_cmd(bot, m, cmd, rest):
    a, chat = rest.split(), m.chat_id
    say = lambda t: bot.send_message(chat, t)
    if cmd in ("owner", "admin", "panel"):
        await show(bot, chat, await panel_text(), panel_kb())
    elif cmd == "ownerhelp":
        await say(OHELP)
    elif cmd == "stats":
        await say(await dashboard())
    elif cmd == "clones":
        text, _ = await clones_page(int(a[0]) if a and a[0].isdigit() else 0)
        await say(text)
    elif cmd == "search":
        k = rest.lstrip("@")
        rows = await db.q("SELECT id,username,owner_id,status FROM clones WHERE username LIKE ? OR CAST(owner_id AS TEXT)=? OR CAST(id AS TEXT)=? LIMIT 15",
                          f"%{k}%", k, k)
        await say("\n".join(f"{r['id']} | @{r['username']} | {r['owner_id']} | {r['status']}" for r in rows) if rows else "Kuch nahi mila.")
    elif cmd in ("pause", "resume"):
        if not a or not a[0].isdigit():
            await say(f"Use: /{cmd} CLONE_ID")
            return
        await db.ex("UPDATE clones SET status=? WHERE id=?", "paused" if cmd == "pause" else "active", int(a[0]))
        core.inval(int(a[0]))
        await say("✅ Done.")
    elif cmd == "remove":
        if not a or not a[0].isdigit():
            await say("Use: /remove CLONE_ID")
            return
        c = await db.one("SELECT username,owner_id FROM clones WHERE id=?", int(a[0]))
        if c and await remove_clone(int(a[0])):
            await say("🗑 Clone remove ho gaya.")
            await core.notify(c["owner_id"], f"ℹ️ Tumhara bot @{c['username']} admin ne remove kar diya.")
            await core.log(f"🗑 Owner removed clone {a[0]}")
        else:
            await say("Clone nahi mila.")
    elif cmd in ("ban", "unban"):
        if not a or not a[0].isdigit():
            await say(f"Use: /{cmd} OWNERID")
            return
        uid = int(a[0])
        ids = [r["id"] for r in await db.q("SELECT id FROM clones WHERE owner_id=?", uid)]
        if cmd == "ban":
            await db.batch([("UPDATE owners SET banned=1 WHERE id=?", (uid,)),
                            ("UPDATE clones SET status='banned' WHERE owner_id=? AND status IN ('active','paused')", (uid,))])
        else:
            await db.batch([("UPDATE owners SET banned=0 WHERE id=?", (uid,)),
                            ("UPDATE clones SET status='active' WHERE owner_id=? AND status='banned'", (uid,))])
        for i in ids:
            core.inval(i)
        await say(f"✅ {cmd} done: {uid}")
    elif cmd == "give":
        if len(a) < 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
            await say("Use: /give OWNERID DIN")
            return
        if not await db.one("SELECT 1 x FROM owners WHERE id=?", int(a[0])):
            await say("Is user ne main bot start nahi kiya.")
            return
        new = await core.add_premium(int(a[0]), int(a[1]))
        await core.notify(int(a[0]), f"🎁 Tumhe {a[1]} din ka Premium mila! {core.fdate(new)} tak.")
        await say(f"✅ Premium {core.fdate(new)} tak.")
    elif cmd == "broadcast":
        if not m.reply_to_message:
            await say("Message par Reply karke /broadcast likho.")
            return
        core.spawn(broadcast_all(bot, chat, chat, m.reply_to_message.message_id))
        await say("📢 Broadcast shuru...")
    elif cmd == "dm":
        p = rest.split(maxsplit=1)
        if len(p) < 2 or not p[0].isdigit():
            await say("Use: /dm OWNERID text")
            return
        await say("✅ Bhej diya." if await core.notify(int(p[0]), p[1]) else "❌ Nahi gaya.")
    elif cmd == "newcoupon":
        if len(a) < 3 or not a[1].isdigit() or not a[2].isdigit():
            await say("Use: /newcoupon CODE DIN USES")
            return
        await db.ex("INSERT OR REPLACE INTO coupons(code,days,max_uses,used) VALUES(?,?,?,0)", a[0].upper(), int(a[1]), int(a[2]))
        await say(f"✅ Coupon {a[0].upper()}: {a[1]} din, {a[2]} uses")
    elif cmd == "delcoupon":
        await db.ex("DELETE FROM coupons WHERE code=?", rest.strip().upper())
        await say("🗑 Done.")
    elif cmd == "coupons":
        rows = await db.q("SELECT * FROM coupons")
        await say("\n".join(f"{r['code']} | {r['days']}d | {r['used']}/{r['max_uses']}" for r in rows) if rows else "Koi coupon nahi.")
    elif cmd == "payments":
        await say(await payments_text())
    elif cmd == "maintenance":
        if a and a[0].lower() in ("on", "off"):
            await toggle_maint(a[0].lower() == "on")
            await say(f"🛠 Naye clones: {'BAND' if a[0].lower() == 'on' else 'chalu'}")
        else:
            await say("Use: /maintenance on|off")
    elif cmd == "backup":
        await backup(bot, chat)
        await say("📥 Backup bhej diya (tokens include nahi hain).")
