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


def B(t, d):
    return Btn(t, callback_data=d)


def menu(uid):
    rows = [[B("➕ Create My Bot", "create"), B("🤖 My Bot", "my")],
            [B("💎 Upgrade", "up"), B("🎟 Coupon", "coupon")],
            [B("ℹ️ Help", "help")]]
    if uid == core.OWNER_ID:
        rows.append([B("👑 Owner Panel", "o:menu")])
    return Kb(rows)


HELP = f"""🤖 Spark Live Bot

Apna personal inbox bot banao: log tumhare bot ko message karenge, tum reply karoge, aur tumhara personal account kisi ko nahi dikhega.

➕ Create My Bot – BotFather ka token do, bot ready
🤖 My Bot – pause / delete / status
💎 Upgrade – Premium Stars se
🎟 Coupon – code se free Premium

🆓 Free: {core.FREE_CLONES} bot, {core.FREE_USERS} users
💎 Premium: {core.PREMIUM_CLONES} bots, unlimited users, broadcast, auto-reply, working hours, VIP, tags, force join, branding hatao"""

OHELP = """👑 Owner commands
/owner – panel
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


async def on_msg(bot, m):
    u = m.from_user
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
        if st and st["state"] == "token":
            await take_token(bot, m, text.strip())
        elif st and st["state"] == "coupon":
            await redeem(bot, m, text.strip())
        else:
            await bot.send_message(m.chat_id, "Menu ke liye /start dabao.", reply_markup=menu(u.id))
        return

    await db.ex("DELETE FROM state WHERE owner_id=?", u.id)
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].split("@")[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    chat = m.chat_id
    if cmd in ("start", "menu"):
        await bot.send_message(chat, f"👋 Hello {u.first_name}!\n\n" + HELP, reply_markup=menu(u.id))
    elif cmd == "help":
        await bot.send_message(chat, HELP, reply_markup=menu(u.id))
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
            await db.ex("INSERT OR REPLACE INTO state(owner_id,state) VALUES(?,'coupon')", u.id)
            await bot.send_message(chat, "🎟 Coupon code bhejo (ya /cancel):")
    elif cmd == "cancel":
        await bot.send_message(chat, "❌ Cancel.", reply_markup=menu(u.id))
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


async def start_create(bot, chat, uid):
    if uid != core.OWNER_ID and await db.kv_get("maintenance", "0") == "1":
        await bot.send_message(chat, "🛠 Abhi naye bots banana band hai. Thodi der baad aao.")
        return
    if not await joined(bot, uid):
        await bot.send_message(chat, f"📢 Pehle {core.FORCE_JOIN} join karo, phir dobara try karo.",
                               reply_markup=Kb([[Btn("📢 Join", url=f"https://t.me/{core.FORCE_JOIN.lstrip('@')}")]]))
        return
    n = (await db.one("SELECT COUNT(*) c FROM clones WHERE owner_id=?", uid))["c"]
    prem = await core.is_premium(uid)
    lim = 99 if uid == core.OWNER_ID else (core.PREMIUM_CLONES if prem else core.FREE_CLONES)
    if n >= lim:
        extra = "" if prem else f"\n💎 Premium me {core.PREMIUM_CLONES} bots bana sakte ho: /upgrade"
        await bot.send_message(chat, f"⚠️ Tumhara bot limit ({lim}) poora ho gaya.{extra}")
        return
    await db.ex("INSERT OR REPLACE INTO state(owner_id,state) VALUES(?,'token')", uid)
    await bot.send_message(
        chat,
        "🛠 Bot banane ke steps:\n\n1️⃣ @BotFather kholo\n2️⃣ /newbot bhejo, naam aur username do\n"
        "3️⃣ Jo token mile (123456:ABC...) wo yahan paste karo\n\n(Token safe store hota hai, encrypted. Cancel: /cancel)")


async def take_token(bot, m, token):
    u = m.from_user
    try:
        await m.delete()
    except TelegramError:
        pass
    if not TOKEN_RE.match(token) or token == core.MAIN_TOKEN:
        await bot.send_message(m.chat_id, "❌ Token galat lag raha hai. Dobara bhejo ya /cancel.")
        return
    tb = Bot(token)
    try:
        async with tb:
            me = await tb.get_me()
            if await db.one("SELECT id FROM clones WHERE id=?", me.id):
                await bot.send_message(m.chat_id, "⚠️ Ye bot pehle se registered hai.")
                return
            await tb.set_webhook(url=f"{core.BASE_URL}/wh/{me.id}", secret_token=core.secret_for(me.id),
                                 allowed_updates=ALLOWED, drop_pending_updates=True)
    except InvalidToken:
        await bot.send_message(m.chat_id, "❌ Token invalid hai. Dobara check karke bhejo.")
        return
    except TelegramError as e:
        await bot.send_message(m.chat_id, f"❌ Error: {e}")
        return

    now = int(time.time())
    await db.batch([
        ("INSERT INTO clones(id,token,username,name,owner_id,status,created,last_report) VALUES(?,?,?,?,?,'active',?,?)",
         (me.id, core.enc(token), me.username, me.full_name, u.id, now, now)),
        ("INSERT OR IGNORE INTO settings(clone_id) VALUES(?)", (me.id,)),
        ("DELETE FROM state WHERE owner_id=?", (u.id,)),
    ])
    trial = ""
    o = await db.one("SELECT trial_used FROM owners WHERE id=?", u.id)
    if not o["trial_used"] and not await core.is_premium(u.id):
        await core.add_premium(u.id, core.TRIAL_DAYS)
        await db.ex("UPDATE owners SET trial_used=1 WHERE id=?", u.id)
        trial = f"\n\n🎁 {core.TRIAL_DAYS} din ka Premium trial free mila!"
    await bot.send_message(
        m.chat_id,
        f"✅ Bot ready: @{me.username}\n\n1️⃣ Pehle @{me.username} kholo aur /start dabao (tum admin ho)\n"
        f"2️⃣ /help se saare commands dekho\n3️⃣ Test kisi DUSRE account se karo: wo message bhejega, tumhe us bot ki chat me aayega.{trial}",
        reply_markup=menu(u.id))
    await core.log(f"🆕 Clone: @{me.username} ({me.id}) by {u.full_name} ({u.id})")


async def show_my(bot, chat, uid):
    rows = await db.q("SELECT c.*,(SELECT COUNT(*) FROM users x WHERE x.clone_id=c.id) n FROM clones c WHERE owner_id=?", uid)
    if not rows:
        await bot.send_message(chat, "Abhi tumhara koi bot nahi hai.", reply_markup=menu(uid))
        return
    for r in rows:
        txt = f"🤖 @{r['username']}\nStatus: {r['status']}\n👥 Users: {r['n']}\n📅 Bana: {core.fdate(r['created'])}"
        btns = []
        if r["status"] in ("active", "paused"):
            btns.append(B("⏸ Pause" if r["status"] == "active" else "▶️ Resume", f"pz:{r['id']}"))
        btns.append(B("🗑 Delete", f"rm:{r['id']}"))
        await bot.send_message(chat, txt, reply_markup=Kb([btns]))
    until = await core.premium_until(uid)
    if await core.is_premium(uid):
        await bot.send_message(chat, f"💎 Premium" + (f" {core.fdate(until)} tak" if until else ""))
    else:
        await bot.send_message(chat, f"🆓 Free plan ({core.FREE_USERS} users/bot). Upgrade: /upgrade")


async def own_clone(uid, cid):
    c = await db.one("SELECT * FROM clones WHERE id=?", cid)
    if c and (c["owner_id"] == uid or uid == core.OWNER_ID):
        return c
    return None


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
async def show_plans(bot, chat, uid):
    until = await core.premium_until(uid)
    cur = f"\n\n✅ Abhi Premium: {core.fdate(until)} tak (renew karoge to din jud jayenge)" if until > time.time() else ""
    txt = (f"💎 TEAMVB Premium\n\n✅ {core.PREMIUM_CLONES} bots, unlimited users\n✅ Broadcast, auto-reply, working hours\n"
           f"✅ VIP, tags, notes, force join, scheduled msgs\n✅ Daily report, branding hatao{cur}")
    kb = Kb([[B(f"⭐{stars} Stars – {days} din", f"buy:{days}")] for days, stars in core.PLANS.items()])
    await bot.send_message(chat, txt, reply_markup=kb)


async def send_invoice(bot, chat, days):
    stars = core.PLANS[days]
    await bot.send_invoice(chat_id=chat, title=f"TEAMVB Premium – {days} din",
                           description=f"{days} din ka Premium", payload=f"plan:{days}",
                           provider_token="", currency="XTR", prices=[LabeledPrice(f"{days} din Premium", stars)])


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
    await bot.send_message(m.chat_id, f"🎉 Payment mil gaya! Premium {core.fdate(new)} tak active.", reply_markup=menu(u.id))
    await core.notify(core.OWNER_ID, f"💰 {u.full_name} ({u.id}) ne ⭐{sp.total_amount} diye ({days} din)")
    await core.log(f"💰 Payment ⭐{sp.total_amount} from {u.id} ({days}d)")


async def redeem(bot, m, code):
    uid = m.from_user.id
    await db.ex("DELETE FROM state WHERE owner_id=?", uid)
    code = code.upper()
    c = await db.one("SELECT * FROM coupons WHERE code=?", code)
    if not c or c["used"] >= c["max_uses"]:
        await bot.send_message(m.chat_id, "❌ Coupon invalid ya khatam.")
        return
    if await db.one("SELECT 1 x FROM coupon_uses WHERE code=? AND owner_id=?", code, uid):
        await bot.send_message(m.chat_id, "⚠️ Ye coupon tum pehle use kar chuke ho.")
        return
    await db.batch([("INSERT INTO coupon_uses(code,owner_id) VALUES(?,?)", (code, uid)),
                    ("UPDATE coupons SET used=used+1 WHERE code=?", (code,))])
    new = await core.add_premium(uid, c["days"])
    await bot.send_message(m.chat_id, f"🎉 Coupon laga! +{c['days']} din. Premium {core.fdate(new)} tak.", reply_markup=menu(uid))


# ================= CALLBACKS =================
async def on_cb(bot, q):
    u, d, chat = q.from_user, q.data or "", q.message.chat.id
    await core.ensure_owner(u)
    o = await db.one("SELECT banned FROM owners WHERE id=?", u.id)
    if o["banned"] and u.id != core.OWNER_ID:
        await q.answer("Banned", show_alert=True)
        return
    await q.answer()
    if d == "create":
        await start_create(bot, chat, u.id)
    elif d == "my":
        await show_my(bot, chat, u.id)
    elif d == "up":
        await show_plans(bot, chat, u.id)
    elif d == "coupon":
        await db.ex("INSERT OR REPLACE INTO state(owner_id,state) VALUES(?,'coupon')", u.id)
        await bot.send_message(chat, "🎟 Coupon code bhejo (ya /cancel):")
    elif d == "help":
        await bot.send_message(chat, HELP, reply_markup=menu(u.id))
    elif d.startswith("buy:"):
        days = int(d[4:]) if d[4:].isdigit() else 0
        if days in core.PLANS:
            await send_invoice(bot, chat, days)
    elif d.startswith(("pz:", "rm:", "rmy:")):
        act, _, cid = d.partition(":")
        c = await own_clone(u.id, int(cid)) if cid.isdigit() else None
        if not c:
            return
        if act == "pz" and c["status"] in ("active", "paused"):
            ns = "paused" if c["status"] == "active" else "active"
            await db.ex("UPDATE clones SET status=? WHERE id=?", ns, c["id"])
            core.inval(c["id"])
            await bot.send_message(chat, f"@{c['username']} → {ns}")
        elif act == "rm":
            await bot.send_message(chat, f"⚠️ @{c['username']} delete karna hai? Saara data (users, chats) hamesha ke liye jayega.",
                                   reply_markup=Kb([[B("✅ Haan delete", f"rmy:{c['id']}"), B("❌ Nahi", "my")]]))
        elif act == "rmy":
            await remove_clone(c["id"])
            await bot.send_message(chat, f"🗑 @{c['username']} delete ho gaya.", reply_markup=menu(u.id))
            await core.log(f"🗑 Clone removed: @{c['username']} by {u.id}")
    elif d.startswith("o:") and u.id == core.OWNER_ID:
        await owner_cb(bot, chat, d[2:])


# ================= OWNER PANEL =================
def panel_kb():
    return Kb([[B("📊 Dashboard", "o:dash"), B("📋 Clones", "o:clones")],
               [B("💰 Payments", "o:pay"), B("🛠 Maintenance", "o:maint")],
               [B("📥 Backup", "o:backup"), B("📖 Commands", "o:help")]])


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
    return (f"📊 Dashboard\n🤖 Clones: {sum(st.values())} (active {st.get('active', 0)}, paused {st.get('paused', 0)}, "
            f"dead {st.get('dead', 0)}, banned {st.get('banned', 0)})\n👤 Main-bot users: {r[1][0]['c']}\n"
            f"👥 Clone users total: {r[2][0]['c']}\n💬 24h messages: {r[3][0]['c']}\n💎 Premium owners: {r[6][0]['c']}\n"
            f"⭐ Kamai: {r[4][0]['s']} total | {r[5][0]['s']} (30 din)\n🛠 Maintenance: {'ON' if maint == '1' else 'off'}")


async def clones_text(page=0):
    rows = await db.q(
        "SELECT c.id,c.username,c.owner_id,c.status,(SELECT COUNT(*) FROM users u WHERE u.clone_id=c.id) n "
        "FROM clones c ORDER BY c.created DESC LIMIT 15 OFFSET ?", page * 15)
    if not rows:
        return "Koi clone nahi."
    return f"📋 Clones (page {page})\nID | bot | owner | status | users\n" + "\n".join(
        f"{r['id']} | @{r['username']} | {r['owner_id']} | {r['status']} | {r['n']}" for r in rows)


async def payments_text():
    rows = await db.q("SELECT owner_id,stars,days,ts FROM payments ORDER BY id DESC LIMIT 15")
    return "💰 Recent payments\n" + "\n".join(
        f"{core.fdate(r['ts'])} | {r['owner_id']} | ⭐{r['stars']} | {r['days']}d" for r in rows) if rows else "Abhi koi payment nahi."


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
    await bot.send_message(chat, "📥 Backup bhej diya (tokens include nahi hain).")


async def toggle_maint(on):
    await db.kv_set("maintenance", "1" if on else "0")


async def owner_cb(bot, chat, a):
    if a == "menu":
        await bot.send_message(chat, "👑 Owner Panel", reply_markup=panel_kb())
    elif a == "dash":
        await bot.send_message(chat, await dashboard())
    elif a == "clones":
        await bot.send_message(chat, await clones_text(0))
    elif a == "pay":
        await bot.send_message(chat, await payments_text())
    elif a == "maint":
        on = await db.kv_get("maintenance", "0") != "1"
        await toggle_maint(on)
        await bot.send_message(chat, f"🛠 Naye clones: {'BAND' if on else 'chalu'}")
    elif a == "backup":
        await backup(bot, chat)
    elif a == "help":
        await bot.send_message(chat, OHELP)


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
        await bot.send_message(chat, "👑 Owner Panel", reply_markup=panel_kb())
    elif cmd == "ownerhelp":
        await say(OHELP)
    elif cmd == "stats":
        await say(await dashboard())
    elif cmd == "clones":
        await say(await clones_text(int(a[0]) if a and a[0].isdigit() else 0))
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
        if await remove_clone(int(a[0])):
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
