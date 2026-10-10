import asyncio
import csv
import io
import re
import time
from collections import defaultdict, deque

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import Update
from telegram.error import Forbidden, RetryAfter, TelegramError

import core
import db

_cache = {}
_flood = defaultdict(deque)
_hdr = {}
_off = {}
_limit_told = set()

# ================= STYLE =================
LINE = "━━━━━━━━━━━━━━━━━━"


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
    return "".join(_BOLD.get(c, c) for c in t)


def card(icon, title, *lines):
    return f"✦{LINE}✦\n   {icon} {bd(title)} {icon}\n✦{LINE}✦\n\n" + "\n".join(lines)


def B(t, d):
    return Btn(t, callback_data=d)


class Ctx:
    def __init__(self, bot, clone, st, admins, premium):
        self.bot, self.clone, self.st = bot, clone, st
        self.cid, self.owner = clone["id"], clone["owner_id"]
        self.admins, self.premium = admins, premium


async def load(clone, bot):
    cid = clone["id"]
    c = _cache.get(cid)
    if not c or time.time() - c[0] > 15:
        st = await db.one("SELECT * FROM settings WHERE clone_id=?", cid)
        if not st:
            await db.ex("INSERT OR IGNORE INTO settings(clone_id) VALUES(?)", cid)
            st = await db.one("SELECT * FROM settings WHERE clone_id=?", cid)
        rows = await db.q("SELECT user_id FROM admins WHERE clone_id=?", cid)
        admins = {clone["owner_id"]} | {r["user_id"] for r in rows}
        c = (time.time(), st, admins)
        _cache[cid] = c
    prem = await core.is_premium(clone["owner_id"])
    return Ctx(bot, clone, c[1], c[2], prem)


def refresh(cid):
    _cache.pop(cid, None)


async def branding_off(ctx):
    """Branding tabhi band hoti hai jab Premium ho AND owner ne /branding off kiya ho."""
    if not ctx.premium:
        return False
    return await db.kv_get(f"nobrand:{ctx.cid}", "0") == "1"


async def brand(ctx):
    if await branding_off(ctx):
        return ""
    return "\n\n" + "─" * 16 + "\n" + core.BRAND.format(main=core.MAIN_USERNAME).strip()


def preview(m):
    t = m.text or m.caption
    if t:
        return t[:500]
    for a in ("photo", "video", "voice", "audio", "document", "sticker", "animation", "video_note", "location", "contact", "poll"):
        if getattr(m, a, None):
            return f"[{a}]"
    return "[message]"


def flooded(cid, uid):
    d = _flood[(cid, uid)]
    now = time.time()
    while d and now - d[0] > 60:
        d.popleft()
    d.append(now)
    return len(d) > 5


def in_hours(hrs):
    try:
        a, b = hrs.split("-")
        t = time.gmtime(time.time() + core.TZ_OFFSET * 60)
        cur = t.tm_hour * 60 + t.tm_min
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
        s, e = h1 * 60 + m1, h2 * 60 + m2
        return (s <= cur < e) if s < e else (cur >= s or cur < e)
    except Exception:
        return True


# ================= ENTRY =================
async def handle(clone, data):
    bot = await core.get_bot(core.dec(clone["token"]))
    upd = Update.de_json(data, bot)
    ctx = await load(clone, bot)
    if upd.callback_query:
        await on_cb(ctx, upd.callback_query)
    elif upd.edited_message:
        m = upd.edited_message
        if m.chat.type == "private" and m.from_user and m.from_user.id not in ctx.admins:
            await on_edit(ctx, m)
    elif upd.message:
        m = upd.message
        if m.chat.type != "private" or not m.from_user:
            return
        if m.from_user.id in ctx.admins:
            await on_admin(ctx, m)
        else:
            await on_user(ctx, m)


# ================= USER SIDE =================
async def check_join(ctx, m, u):
    fj = ctx.st["forcejoin"]
    if not fj or not ctx.premium:
        return False
    try:
        mem = await ctx.bot.get_chat_member(fj, u.id)
        if mem.status in ("member", "administrator", "creator", "owner"):
            return False
    except TelegramError:
        return False
    await m.reply_text(
        f"📢 Pehle channel join karo: {fj}",
        reply_markup=Kb([[Btn("📢 Join karo", url=f"https://t.me/{fj.lstrip('@')}")], [B("✅ Join kar liya", "fj")]]),
    )
    return True


async def limit_notice(ctx):
    if ctx.cid in _limit_told:
        return
    _limit_told.add(ctx.cid)
    try:
        await ctx.bot.send_message(
            ctx.owner,
            f"⚠️ Free limit ({core.FREE_USERS} users) pura ho gaya, naye users ruk gaye.\nPremium lo: @{core.MAIN_USERNAME} → /upgrade",
        )
    except TelegramError:
        pass


async def auto_replies(ctx, m, text, uid):
    if text:
        rows = await db.q("SELECT keyword,response FROM autoreplies WHERE clone_id=?", ctx.cid)
        low = text.lower()
        for r in rows:
            if r["keyword"] in low:
                await m.reply_text(r["response"])
                break
    hrs = ctx.st["hours"]
    if hrs and not in_hours(hrs):
        k = (ctx.cid, uid)
        if time.time() - _off.get(k, 0) > 6 * 3600:
            _off[k] = time.time()
            await m.reply_text(ctx.st["offline"] or "🌙 Admin abhi offline hai. Jaldi reply milega.")


def welcome_text(ctx, name):
    w = ctx.st["welcome"]
    if w:
        return w
    return card(
        "⚡", "WELCOME",
        f"👋 {bd('Hello')}, {name}!", "",
        "◈ Apna message yahan likho",
        "◈ Admin jaldi reply karega 💬",
        "◈ Tumhari privacy safe hai 🔒",
    )


async def on_user(ctx, m):
    u, cid, bot = m.from_user, ctx.cid, ctx.bot
    now = int(time.time())
    text = m.text or ""

    if ctx.st["maintenance"]:
        await m.reply_text("🛠 Bot abhi maintenance me hai. Thodi der baad aao.")
        return

    row = await db.one("SELECT * FROM users WHERE clone_id=? AND user_id=?", cid, u.id)
    new = row is None
    if new:
        if not ctx.premium:
            n = (await db.one("SELECT COUNT(*) c FROM users WHERE clone_id=?", cid))["c"]
            if n >= core.FREE_USERS:
                await m.reply_text("⚠️ Ye bot abhi full hai. Baad me try karo.")
                await limit_notice(ctx)
                return
        ref = None
        mt = re.match(r"/start ref_(\d+)", text)
        if mt and int(mt.group(1)) != u.id:
            ref = int(mt.group(1))
        await db.ex(
            "INSERT OR IGNORE INTO users(clone_id,user_id,name,username,first_seen,last_seen,ref_by) VALUES(?,?,?,?,?,?,?)",
            cid, u.id, u.full_name, u.username, now, now, ref,
        )
        row = await db.one("SELECT * FROM users WHERE clone_id=? AND user_id=?", cid, u.id)
    else:
        await db.ex("UPDATE users SET name=?, username=?, last_seen=? WHERE clone_id=? AND user_id=?",
                    u.full_name, u.username, now, cid, u.id)

    if row["blocked"]:
        return
    if await check_join(ctx, m, u):
        return
    if ctx.st["captcha"] and not row["verified"]:
        await m.reply_text("🤖 Pehle verify karo:", reply_markup=Kb([[B("✅ Main insaan hu", "cap")]]))
        return
    if text.startswith("/start"):
        await m.reply_text(welcome_text(ctx, u.first_name) + await brand(ctx))
        return
    if text.startswith("/link"):
        await m.reply_text(f"🔗 Tumhara link:\nhttps://t.me/{ctx.clone['username']}?start=ref_{u.id}")
        return
    if row["shadow"]:
        return
    if flooded(cid, u.id):
        if len(_flood[(cid, u.id)]) == 6:
            await m.reply_text("⏳ Dheere bhejo, spam mat karo.")
        return
    if ctx.premium:
        await auto_replies(ctx, m, text, u.id)

    reopened = row["status"] == "closed"
    tm = time.strftime("%I:%M %p", time.gmtime(now + core.TZ_OFFSET * 60))
    title = "NEW USER" if new else ("CHAT REOPENED" if reopened else "NEW MESSAGE")
    lines = [
        f"👤 {bd('Name')}   ➜  {u.full_name}",
        f"🔗 {bd('User')}   ➜  {'@' + u.username if u.username else '—'}",
        f"🆔 {bd('ID')}       ➜  {u.id}",
        f"💬 {bd('Msgs')}   ➜  {row['msgs'] + 1}    •    🕒 {tm}",
    ]
    if row["vip"]:
        lines.append("👑 VIP user")
    if row["tags"]:
        lines.append(f"🏷 {row['tags']}")
    lines += ["", "↩️ Jawab dene ke liye is card ya", "     neeche ke message par Reply karo"]
    head = card("📩", title, *lines)
    kb = Kb([[B("🚫 Block", f"b:{u.id}"), B("👑 VIP", f"v:{u.id}")],
             [B("🎫 Close", f"x:{u.id}"), B("ℹ️ Info", f"i:{u.id}")],
             [B("📜 History", f"h:{u.id}")]])

    stmts = []
    for a in ctx.admins:
        try:
            if new or reopened or time.time() - _hdr.get((cid, a, u.id), 0) > 120:
                h = await bot.send_message(a, head, reply_markup=kb)
                _hdr[(cid, a, u.id)] = time.time()
                stmts.append(("INSERT OR REPLACE INTO msgmap(clone_id,admin_chat,admin_msg,user_id,ts) VALUES(?,?,?,?,?)",
                              (cid, a, h.message_id, u.id, now)))
            c = await bot.copy_message(a, m.chat_id, m.message_id)
            stmts.append(("INSERT OR REPLACE INTO msgmap(clone_id,admin_chat,admin_msg,user_id,ts) VALUES(?,?,?,?,?)",
                          (cid, a, c.message_id, u.id, now)))
        except TelegramError:
            pass
    stmts.append(("UPDATE users SET msgs=msgs+1, status='open' WHERE clone_id=? AND user_id=?", (cid, u.id)))
    stmts.append(("INSERT INTO msglog(clone_id,user_id,dir,text,ts) VALUES(?,?,?,?,?)", (cid, u.id, "in", preview(m), now)))
    await db.batch(stmts)


async def on_edit(ctx, m):
    row = await db.one("SELECT blocked,shadow FROM users WHERE clone_id=? AND user_id=?", ctx.cid, m.from_user.id)
    t = m.text or m.caption
    if not row or row["blocked"] or row["shadow"] or not t:
        return
    for a in ctx.admins:
        try:
            await ctx.bot.send_message(a, f"✏️ {m.from_user.full_name} ({m.from_user.id}) ne edit kiya:\n{t[:3000]}")
        except TelegramError:
            pass


# ================= CALLBACKS =================
async def close_ticket(ctx, uid):
    await db.ex("UPDATE users SET status='closed' WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    b = await brand(ctx)
    try:
        await ctx.bot.send_message(
            uid, "✅ Aapki chat close kar di gayi. Rating dein:" + b,
            reply_markup=Kb([[B(f"⭐{i}", f"rt:{i}") for i in range(1, 6)]]),
        )
    except TelegramError:
        pass


async def userinfo(ctx, uid):
    r = await db.one("SELECT * FROM users WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    if not r:
        return "User nahi mila."
    yn = lambda v: "Haan" if v else "Nahi"
    s = (f"👤 {r['name']} ({'@' + r['username'] if r['username'] else '-'})\n🆔 {uid}\n"
         f"📅 Pehli baar: {core.fdate(r['first_seen'])}\n💬 Messages: {r['msgs']}\n"
         f"🚫 Blocked: {yn(r['blocked'])} | 👻 Shadow: {yn(r['shadow'])} | 👑 VIP: {yn(r['vip'])}\n"
         f"🎫 Ticket: {r['status']}" + (f" ⭐{r['rating']}" if r["rating"] else ""))
    if r["tags"]:
        s += f"\n🏷 {r['tags']}"
    if r["note"]:
        s += f"\n📝 {r['note']}"
    return s


async def on_cb(ctx, q):
    d, u = q.data or "", q.from_user
    if d == "cap":
        await db.ex("UPDATE users SET verified=1 WHERE clone_id=? AND user_id=?", ctx.cid, u.id)
        await q.answer("✅ Verified! Ab message bhejo.", show_alert=True)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return
    if d == "fj":
        fj = ctx.st["forcejoin"]
        ok = False
        try:
            mem = await ctx.bot.get_chat_member(fj, u.id)
            ok = mem.status in ("member", "administrator", "creator", "owner")
        except TelegramError:
            ok = True
        if ok:
            await q.answer("✅ Ab message bhejo.", show_alert=True)
            try:
                await q.message.delete()
            except TelegramError:
                pass
        else:
            await q.answer("❌ Abhi join nahi kiya.", show_alert=True)
        return
    if d.startswith("rt:"):
        try:
            await db.ex("UPDATE users SET rating=? WHERE clone_id=? AND user_id=?", int(d[3:]), ctx.cid, u.id)
            await q.answer("Shukriya! ⭐")
            await q.edit_message_text("🙏 Rating ke liye shukriya!")
        except (ValueError, TelegramError):
            pass
        return

    if u.id not in ctx.admins:
        await q.answer("Not allowed")
        return
    try:
        act, _, rid = d.partition(":")
        uid = int(rid)
    except ValueError:
        await q.answer()
        return
    row = await db.one("SELECT * FROM users WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    if not row:
        await q.answer("User nahi mila", show_alert=True)
        return
    if act == "b":
        v = 0 if row["blocked"] else 1
        await db.ex("UPDATE users SET blocked=? WHERE clone_id=? AND user_id=?", v, ctx.cid, uid)
        await q.answer("🚫 Blocked" if v else "✅ Unblocked")
    elif act == "v":
        if not ctx.premium:
            await q.answer("💎 Premium feature", show_alert=True)
            return
        v = 0 if row["vip"] else 1
        await db.ex("UPDATE users SET vip=? WHERE clone_id=? AND user_id=?", v, ctx.cid, uid)
        await q.answer("👑 VIP on" if v else "VIP off")
    elif act == "x":
        await close_ticket(ctx, uid)
        await q.answer("🎫 Closed")
    elif act == "i":
        await q.answer()
        await ctx.bot.send_message(q.message.chat.id, await userinfo(ctx, uid))
    elif act == "h":
        await q.answer()
        rows = await db.q("SELECT dir,text FROM msglog WHERE clone_id=? AND user_id=? ORDER BY id DESC LIMIT 15", ctx.cid, uid)
        s = "\n".join(f"{'👤' if r['dir'] == 'in' else '🧑‍💼'} {r['text']}" for r in reversed(rows)) or "Koi history nahi."
        await ctx.bot.send_message(q.message.chat.id, card("📜", "HISTORY", s)[:4000])
    else:
        await q.answer()


# ================= ADMIN SIDE =================
async def on_admin(ctx, m):
    text = m.text or ""
    if text.startswith("/"):
        await command(ctx, m, text)
        return
    rm = m.reply_to_message
    if rm:
        r = await db.one("SELECT user_id FROM msgmap WHERE clone_id=? AND admin_chat=? AND admin_msg=?",
                         ctx.cid, m.chat_id, rm.message_id)
        if r:
            await deliver(ctx, m, r["user_id"])
            return
    await m.reply_text("↩️ Kisi user ke message par Reply karke likho. /help dekho.")


async def deliver(ctx, m, uid):
    try:
        await ctx.bot.copy_message(uid, m.chat_id, m.message_id)
    except Forbidden:
        await m.reply_text("❌ User ne bot block kar diya.")
        return
    except TelegramError as e:
        await m.reply_text(f"❌ Nahi bheja ja saka: {e}")
        return
    await db.batch([
        ("INSERT INTO msglog(clone_id,user_id,dir,text,ts) VALUES(?,?,?,?,?)", (ctx.cid, uid, "out", preview(m), int(time.time()))),
        ("UPDATE users SET status='open' WHERE clone_id=? AND user_id=? AND status='closed'", (ctx.cid, uid)),
    ])


async def send_text(ctx, m, uid, text):
    try:
        await ctx.bot.send_message(uid, text)
    except Forbidden:
        await m.reply_text("❌ User ne bot block kar diya.")
        return
    except TelegramError as e:
        await m.reply_text(f"❌ {e}")
        return
    await db.ex("INSERT INTO msglog(clone_id,user_id,dir,text,ts) VALUES(?,?,?,?,?)", ctx.cid, uid, "out", text[:500], int(time.time()))
    await m.reply_text("✅ Bhej diya.")


async def target(ctx, m, args):
    if m.reply_to_message:
        r = await db.one("SELECT user_id FROM msgmap WHERE clone_id=? AND admin_chat=? AND admin_msg=?",
                         ctx.cid, m.chat_id, m.reply_to_message.message_id)
        if r:
            return r["user_id"], args
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), args[1:]
    return None, args


NEED = "↩️ User ke message par Reply karo ya ID likho."

HELP_BODY = f"""💬 {bd('Chat')}
↩️ Message par Reply = user ko jawab
/send ID text – user ko pehle message
/close, /open – ticket (reply/ID)
/info, /history, /export (reply/ID)
/find naam – user dhundo

🚫 {bd('Control')} (reply ya ID)
/block /unblock /shadow /unshadow
/blocked – blocked list

⚙️ {bd('Settings')}
/setwelcome text (ya reset)
/maintenance on|off
/captcha on|off
/autoclean din (0 = band)
/branding on|off (off = Premium)
/addadmin ID, /deladmin ID (sirf owner)

⚡ {bd('Saved replies')}
/save naam text, /r naam (reply), /saved, /delsaved naam

📊 /stats /plan /backup /refstats

💎 {bd('Premium')}
/vip /unvip, /tag text, /untag, /note text
/setoffline text, /hours 10:00-22:00 | off
/autoreply keyword | jawab, /delauto keyword, /autoreplies
/broadcast (kisi message par Reply)
/later 30m ID text   (m/h/d)
/forcejoin @channel | off

🔗 Users apna referral link /link se le sakte hain."""

PREMIUM_CMDS = {"vip", "unvip", "tag", "untag", "note", "setoffline", "hours", "autoreply", "delauto",
                "autoreplies", "broadcast", "later", "forcejoin"}


async def command(ctx, m, text):
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].split("@")[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if cmd in PREMIUM_CMDS and not ctx.premium:
        await m.reply_text(f"💎 Ye feature Premium me hai.\nUpgrade: @{core.MAIN_USERNAME} → /upgrade")
        return
    fn = CMDS.get(cmd)
    if not fn:
        await m.reply_text("❓ Unknown command. /help dekho.")
        return
    await fn(ctx, m, rest, rest.split())


async def setting(ctx, col, val):
    await db.ex(f"UPDATE settings SET {col}=? WHERE clone_id=?", val, ctx.cid)
    refresh(ctx.cid)


async def c_help(ctx, m, rest, args):
    off = await branding_off(ctx)
    plan = "💎 Premium" if ctx.premium else f"🆓 Free ({core.FREE_USERS} users)"
    foot = (f"\n\n{'─' * 16}\n🏷 Branding: {'OFF' if off else 'ON'}   •   {plan}\n"
            f"⚡ Powered by TEAMVB\nTry it @{core.MAIN_USERNAME}")
    await m.reply_text(card("📖", "ADMIN PANEL", HELP_BODY) + foot)


async def c_branding(ctx, m, rest, args):
    off = await branding_off(ctx)
    if not args or args[0].lower() not in ("on", "off"):
        await m.reply_text(f"🏷 Branding abhi: {'OFF' if off else 'ON'}\nUse: /branding on  ya  /branding off (off = 💎 Premium)")
        return
    if args[0].lower() == "off":
        if not ctx.premium:
            await m.reply_text(f"💎 Branding hatana Premium me hai.\nUpgrade: @{core.MAIN_USERNAME} → /upgrade")
            return
        await db.kv_set(f"nobrand:{ctx.cid}", "1")
        await m.reply_text("✅ Branding OFF. Ab 'Powered by TEAMVB' nahi dikhega.")
    else:
        await db.kv_set(f"nobrand:{ctx.cid}", "0")
        await m.reply_text("✅ Branding ON.")


async def c_stats(ctx, m, rest, args):
    cid, day = ctx.cid, int(time.time()) - 86400
    r = await db.batch([
        ("SELECT COUNT(*) c FROM users WHERE clone_id=?", (cid,)),
        ("SELECT COUNT(*) c FROM users WHERE clone_id=? AND first_seen>?", (cid, day)),
        ("SELECT COUNT(*) c FROM users WHERE clone_id=? AND blocked=1", (cid,)),
        ("SELECT COUNT(*) c FROM users WHERE clone_id=? AND status='closed'", (cid,)),
        ("SELECT COUNT(*) c FROM msglog WHERE clone_id=? AND dir='in' AND ts>?", (cid, day)),
        ("SELECT name,msgs FROM users WHERE clone_id=? ORDER BY msgs DESC LIMIT 1", (cid,)),
    ])
    top = r[5][0] if r[5] else None
    await m.reply_text(
        f"📊 Stats\n👥 Kul users: {r[0][0]['c']}\n🆕 Aaj ke naye: {r[1][0]['c']}\n💬 Aaj ke messages: {r[4][0]['c']}\n"
        f"🚫 Blocked: {r[2][0]['c']}\n🎫 Closed tickets: {r[3][0]['c']}"
        + (f"\n🏆 Sabse active: {top['name']} ({top['msgs']})" if top else "")
    )


async def c_plan(ctx, m, rest, args):
    until = await core.premium_until(ctx.owner)
    if ctx.premium:
        await m.reply_text(f"💎 Premium active" + (f" ({core.fdate(until)} tak)" if until else ""))
    else:
        await m.reply_text(f"🆓 Free plan: {core.FREE_USERS} users limit.\nUpgrade: @{core.MAIN_USERNAME} → /upgrade")


def flag_cmd(col, val, label):
    async def f(ctx, m, rest, args):
        uid, _ = await target(ctx, m, args)
        if not uid:
            await m.reply_text(NEED)
            return
        await db.ex(f"UPDATE users SET {col}=? WHERE clone_id=? AND user_id=?", val, ctx.cid, uid)
        await m.reply_text(f"{label}: {uid}")
    return f


async def c_blocked(ctx, m, rest, args):
    rows = await db.q("SELECT user_id,name FROM users WHERE clone_id=? AND blocked=1 LIMIT 40", ctx.cid)
    await m.reply_text("🚫 Blocked:\n" + "\n".join(f"{r['user_id']} – {r['name']}" for r in rows) if rows else "Koi blocked nahi.")


async def c_tag(ctx, m, rest, args):
    uid, a = await target(ctx, m, args)
    if not uid or not a:
        await m.reply_text("Use: (reply) /tag Client   ya  /tag ID Client")
        return
    r = await db.one("SELECT tags FROM users WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    if not r:
        await m.reply_text("User nahi mila.")
        return
    tags = ((r["tags"] or "") + ", " + " ".join(a)).strip(", ")
    await db.ex("UPDATE users SET tags=? WHERE clone_id=? AND user_id=?", tags, ctx.cid, uid)
    await m.reply_text(f"🏷 {tags}")


async def c_untag(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    if not uid:
        await m.reply_text(NEED)
        return
    await db.ex("UPDATE users SET tags='' WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    await m.reply_text("✅ Tags hata diye.")


async def c_note(ctx, m, rest, args):
    uid, a = await target(ctx, m, args)
    if not uid or not a:
        await m.reply_text("Use: (reply) /note text   ya  /note ID text")
        return
    await db.ex("UPDATE users SET note=? WHERE clone_id=? AND user_id=?", " ".join(a), ctx.cid, uid)
    await m.reply_text("📝 Note save.")


async def c_info(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    await m.reply_text(await userinfo(ctx, uid) if uid else NEED)


async def c_find(ctx, m, rest, args):
    if not rest:
        await m.reply_text("Use: /find naam ya @username ya ID")
        return
    k = rest.lstrip("@")
    rows = await db.q(
        "SELECT user_id,name,username FROM users WHERE clone_id=? AND (name LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT)=?) LIMIT 15",
        ctx.cid, f"%{k}%", f"%{k}%", k)
    await m.reply_text("\n".join(f"{r['user_id']} – {r['name']} (@{r['username'] or '-'})" for r in rows) if rows else "Kuch nahi mila.")


async def c_history(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    if not uid:
        await m.reply_text(NEED)
        return
    rows = await db.q("SELECT dir,text,ts FROM msglog WHERE clone_id=? AND user_id=? ORDER BY id DESC LIMIT 15", ctx.cid, uid)
    if not rows:
        await m.reply_text("Koi history nahi.")
        return
    s = "\n".join(f"{'👤' if r['dir'] == 'in' else '🧑‍💼'} {r['text']}" for r in reversed(rows))
    await m.reply_text(s[:4000])


async def c_export(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    if not uid:
        await m.reply_text(NEED)
        return
    rows = await db.q("SELECT dir,text,ts FROM msglog WHERE clone_id=? AND user_id=? ORDER BY id LIMIT 3000", ctx.cid, uid)
    s = "\n".join(f"[{core.fdate(r['ts'])}] {'USER' if r['dir'] == 'in' else 'ADMIN'}: {r['text']}" for r in rows) or "empty"
    await ctx.bot.send_document(m.chat_id, io.BytesIO(s.encode()), filename=f"chat_{uid}.txt")


async def c_send(ctx, m, rest, args):
    p = rest.split(maxsplit=1)
    if len(p) < 2 or not p[0].isdigit():
        await m.reply_text("Use: /send ID text")
        return
    if not await db.one("SELECT 1 x FROM users WHERE clone_id=? AND user_id=?", ctx.cid, int(p[0])):
        await m.reply_text("Ye user is bot ka nahi hai.")
        return
    await send_text(ctx, m, int(p[0]), p[1])


async def c_close(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    if not uid:
        await m.reply_text(NEED)
        return
    await close_ticket(ctx, uid)
    await m.reply_text("🎫 Ticket closed.")


async def c_open(ctx, m, rest, args):
    uid, _ = await target(ctx, m, args)
    if not uid:
        await m.reply_text(NEED)
        return
    await db.ex("UPDATE users SET status='open' WHERE clone_id=? AND user_id=?", ctx.cid, uid)
    await m.reply_text("🎫 Ticket open.")


async def c_setwelcome(ctx, m, rest, args):
    if not rest:
        await m.reply_text("Use: /setwelcome text   (default: /setwelcome reset)")
        return
    await setting(ctx, "welcome", None if rest.lower() == "reset" else rest)
    await m.reply_text("✅ Welcome message set.")


async def c_setoffline(ctx, m, rest, args):
    if not rest:
        await m.reply_text("Use: /setoffline text")
        return
    await setting(ctx, "offline", rest)
    await m.reply_text("✅ Offline message set.")


async def c_hours(ctx, m, rest, args):
    if args and args[0].lower() == "off":
        await setting(ctx, "hours", None)
        await m.reply_text("✅ Working hours band.")
    elif args and re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", args[0]):
        await setting(ctx, "hours", args[0])
        await m.reply_text(f"✅ Online time {args[0]}. Iske bahar auto offline-reply jayega.")
    else:
        await m.reply_text(f"Use: /hours 10:00-22:00   ya /hours off\nAbhi: {ctx.st['hours'] or 'off'}")


def toggle_cmd(col, label):
    async def f(ctx, m, rest, args):
        if not args or args[0].lower() not in ("on", "off"):
            await m.reply_text(f"Use: /{label} on  ya  /{label} off")
            return
        on = args[0].lower() == "on"
        await setting(ctx, col, 1 if on else 0)
        await m.reply_text(f"✅ {label}: {'on' if on else 'off'}")
    return f


async def c_autoclean(ctx, m, rest, args):
    if not args or not args[0].isdigit():
        await m.reply_text(f"Use: /autoclean din (0 = band). Abhi: {ctx.st['autoclean']}")
        return
    await setting(ctx, "autoclean", int(args[0]))
    await m.reply_text("✅ Set.")


async def c_forcejoin(ctx, m, rest, args):
    if not args:
        await m.reply_text("Use: /forcejoin @channel  ya  /forcejoin off")
        return
    if args[0].lower() == "off":
        await setting(ctx, "forcejoin", None)
        await m.reply_text("✅ Force join band.")
        return
    ch = args[0] if args[0].startswith("@") else "@" + args[0]
    try:
        mem = await ctx.bot.get_chat_member(ch, ctx.bot.id)
        if mem.status not in ("administrator", "creator"):
            raise TelegramError("not admin")
    except TelegramError:
        await m.reply_text("❌ Pehle bot ko us channel me admin banao, phir dobara try karo.")
        return
    await setting(ctx, "forcejoin", ch)
    await m.reply_text(f"✅ Ab users ko {ch} join karna padega.")


async def c_autoreply(ctx, m, rest, args):
    kw, _, resp = rest.partition("|")
    kw, resp = kw.strip().lower(), resp.strip()
    if not kw or not resp:
        await m.reply_text("Use: /autoreply keyword | jawab")
        return
    await db.ex("INSERT OR REPLACE INTO autoreplies(clone_id,keyword,response) VALUES(?,?,?)", ctx.cid, kw, resp)
    await m.reply_text(f"✅ '{kw}' ka auto-reply set.")


async def c_delauto(ctx, m, rest, args):
    await db.ex("DELETE FROM autoreplies WHERE clone_id=? AND keyword=?", ctx.cid, rest.strip().lower())
    await m.reply_text("🗑 Done.")


async def c_autoreplies(ctx, m, rest, args):
    rows = await db.q("SELECT keyword,response FROM autoreplies WHERE clone_id=?", ctx.cid)
    await m.reply_text("\n".join(f"• {r['keyword']} → {r['response'][:60]}" for r in rows) if rows else "Koi auto-reply nahi.")


async def c_save(ctx, m, rest, args):
    p = rest.split(maxsplit=1)
    if len(p) < 2:
        await m.reply_text("Use: /save naam text")
        return
    name = p[0].lower()
    if not ctx.premium:
        n = (await db.one("SELECT COUNT(*) c FROM saved WHERE clone_id=?", ctx.cid))["c"]
        exists = await db.one("SELECT 1 x FROM saved WHERE clone_id=? AND name=?", ctx.cid, name)
        if n >= core.FREE_SAVED and not exists:
            await m.reply_text(f"💎 Free me sirf {core.FREE_SAVED} saved replies. Premium me unlimited.")
            return
    await db.ex("INSERT OR REPLACE INTO saved(clone_id,name,text) VALUES(?,?,?)", ctx.cid, name, p[1])
    await m.reply_text(f"✅ Saved: {name}")


async def c_r(ctx, m, rest, args):
    if not args:
        await m.reply_text("Use: (user ke message par reply karke) /r naam")
        return
    s = await db.one("SELECT text FROM saved WHERE clone_id=? AND name=?", ctx.cid, args[0].lower())
    if not s:
        await m.reply_text("Ye saved reply nahi mila. /saved dekho.")
        return
    uid, _ = await target(ctx, m, args[1:])
    if not uid:
        await m.reply_text(NEED)
        return
    await send_text(ctx, m, uid, s["text"])


async def c_saved(ctx, m, rest, args):
    rows = await db.q("SELECT name,text FROM saved WHERE clone_id=?", ctx.cid)
    await m.reply_text("\n".join(f"• {r['name']} → {r['text'][:60]}" for r in rows) if rows else "Koi saved reply nahi.")


async def c_delsaved(ctx, m, rest, args):
    await db.ex("DELETE FROM saved WHERE clone_id=? AND name=?", ctx.cid, rest.strip().lower())
    await m.reply_text("🗑 Done.")


async def do_broadcast(ctx, chat, mid):
    rows = await db.q("SELECT user_id FROM users WHERE clone_id=? AND blocked=0 AND shadow=0", ctx.cid)
    ok = fail = 0
    for r in rows:
        for _ in range(2):
            try:
                await ctx.bot.copy_message(r["user_id"], chat, mid)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                fail += 1
                break
        await asyncio.sleep(0.06)
    try:
        await ctx.bot.send_message(chat, f"📢 Broadcast poora: ✅ {ok}  ❌ {fail}")
    except TelegramError:
        pass


async def c_broadcast(ctx, m, rest, args):
    if not m.reply_to_message:
        await m.reply_text("Jo message bhejna hai, usko likho/bhejo, phir us par Reply karke /broadcast likho.")
        return
    core.spawn(do_broadcast(ctx, m.chat_id, m.reply_to_message.message_id))
    await m.reply_text("📢 Broadcast shuru ho gaya...")


async def c_later(ctx, m, rest, args):
    p = rest.split(maxsplit=2)
    mt = re.fullmatch(r"(\d+)([mhd])", p[0]) if p else None
    if len(p) < 3 or not mt or not p[1].isdigit():
        await m.reply_text("Use: /later 30m ID text   (m = minute, h = ghanta, d = din)")
        return
    secs = int(mt.group(1)) * {"m": 60, "h": 3600, "d": 86400}[mt.group(2)]
    await db.ex("INSERT INTO scheduled(clone_id,user_id,text,run_at) VALUES(?,?,?,?)", ctx.cid, int(p[1]), p[2], int(time.time()) + secs)
    await m.reply_text(f"⏰ {p[0]} baad bhej diya jayega.")


async def c_addadmin(ctx, m, rest, args):
    if m.from_user.id != ctx.owner:
        await m.reply_text("Sirf bot owner admin add kar sakta hai.")
        return
    if not args or not args[0].isdigit():
        await m.reply_text("Use: /addadmin ID\n(Us banda ko pehle is bot me /start karna padega)")
        return
    await db.ex("INSERT OR IGNORE INTO admins(clone_id,user_id) VALUES(?,?)", ctx.cid, int(args[0]))
    refresh(ctx.cid)
    await m.reply_text("✅ Admin add ho gaya.")


async def c_deladmin(ctx, m, rest, args):
    if m.from_user.id != ctx.owner:
        await m.reply_text("Sirf bot owner admin hata sakta hai.")
        return
    if not args or not args[0].isdigit():
        await m.reply_text("Use: /deladmin ID")
        return
    await db.ex("DELETE FROM admins WHERE clone_id=? AND user_id=?", ctx.cid, int(args[0]))
    refresh(ctx.cid)
    await m.reply_text("✅ Admin hata diya.")


async def c_refstats(ctx, m, rest, args):
    rows = await db.q(
        "SELECT ref_by, COUNT(*) c FROM users WHERE clone_id=? AND ref_by IS NOT NULL GROUP BY ref_by ORDER BY c DESC LIMIT 10", ctx.cid)
    await m.reply_text("🏆 Top referrers:\n" + "\n".join(f"{r['ref_by']} → {r['c']} users" for r in rows) if rows else "Abhi koi referral nahi.")


async def c_backup(ctx, m, rest, args):
    rows = await db.q("SELECT user_id,name,username,first_seen,msgs,blocked,vip,tags FROM users WHERE clone_id=?", ctx.cid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "first_seen", "msgs", "blocked", "vip", "tags"])
    for r in rows:
        w.writerow([r["user_id"], r["name"], r["username"], core.fdate(r["first_seen"]), r["msgs"], r["blocked"], r["vip"], r["tags"]])
    await ctx.bot.send_document(m.chat_id, io.BytesIO(buf.getvalue().encode()), filename="users_backup.csv")


CMDS = {
    "start": c_help, "help": c_help, "stats": c_stats, "plan": c_plan, "branding": c_branding,
    "block": flag_cmd("blocked", 1, "🚫 Blocked"), "unblock": flag_cmd("blocked", 0, "✅ Unblocked"),
    "shadow": flag_cmd("shadow", 1, "👻 Shadow ban"), "unshadow": flag_cmd("shadow", 0, "✅ Shadow hata"),
    "vip": flag_cmd("vip", 1, "👑 VIP"), "unvip": flag_cmd("vip", 0, "VIP hata"),
    "blocked": c_blocked, "tag": c_tag, "untag": c_untag, "note": c_note, "info": c_info,
    "find": c_find, "history": c_history, "export": c_export, "send": c_send,
    "close": c_close, "open": c_open, "setwelcome": c_setwelcome, "setoffline": c_setoffline,
    "hours": c_hours, "maintenance": toggle_cmd("maintenance", "maintenance"),
    "captcha": toggle_cmd("captcha", "captcha"), "autoclean": c_autoclean, "forcejoin": c_forcejoin,
    "autoreply": c_autoreply, "delauto": c_delauto, "autoreplies": c_autoreplies,
    "save": c_save, "r": c_r, "saved": c_saved, "delsaved": c_delsaved,
    "broadcast": c_broadcast, "later": c_later, "addadmin": c_addadmin, "deladmin": c_deladmin,
    "refstats": c_refstats, "backup": c_backup,
}


# ================= BACKGROUND =================
async def run_scheduled():
    rows = await db.q(
        "SELECT s.id,s.user_id,s.text,c.token FROM scheduled s JOIN clones c ON c.id=s.clone_id "
        "WHERE s.run_at<=? AND c.status='active'", int(time.time()))
    for r in rows:
        try:
            bot = await core.get_bot(core.dec(r["token"]))
            await bot.send_message(r["user_id"], r["text"])
        except Exception:
            pass
        await db.ex("DELETE FROM scheduled WHERE id=?", r["id"])
