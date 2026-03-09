# bot.py — V4 ADS BOT — Final Clean Version
# Config from ENV or hardcoded defaults

import asyncio
import json
import os
import pathlib
import secrets
import sqlite3
import string
from datetime import datetime, timedelta

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
import telethon

# ─────────────────────────── CONFIG ──────────────────────────
API_ID    = int(os.environ.get("API_ID",    "24244418"))
API_HASH  =     os.environ.get("API_HASH",  "b2673deba5561827f53e82b6161fe6f4")
BOT_TOKEN =     os.environ.get("BOT_TOKEN", "8770953822:AAFUbFpo9kDHFeyB5bQVNbpxbTwOriR3NS0")
ADMIN_ID  = int(os.environ.get("ADMIN_ID",  "7831057346"))
DB_FILE   = os.environ.get("DB_FILE", os.path.join(os.path.expanduser("~"), "bot_data.db"))
WELCOME_PHOTO = str(pathlib.Path(__file__).parent / "welcome.jpg")
MAX_ACCOUNTS = 3
MAX_FAILS    = 5

print(f"Telethon {telethon.__version__} | DB: {DB_FILE}")

# ─────────────────────────── DATABASE ────────────────────────
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c    = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id       INTEGER PRIMARY KEY,
    username      TEXT    DEFAULT '',
    trial_granted INTEGER DEFAULT 0,
    trial_expires TEXT,
    is_banned     INTEGER DEFAULT 0,
    joined_at     TEXT    DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS user_accounts(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    phone       TEXT UNIQUE,
    session_str TEXT,
    added_at    TEXT DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS access_codes(
    code       TEXT PRIMARY KEY,
    days_valid INTEGER,
    created_at TEXT,
    claimed_by INTEGER,
    claimed_at TEXT,
    expires_at TEXT,
    is_active  INTEGER DEFAULT 1)""")
c.execute("""CREATE TABLE IF NOT EXISTS scheduled_tasks(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    phone            TEXT,
    messages_json    TEXT    DEFAULT '[]',
    interval_seconds INTEGER,
    next_run         TEXT,
    current_msg_idx  INTEGER DEFAULT 0,
    fail_count       INTEGER DEFAULT 0,
    is_active        INTEGER DEFAULT 1,
    created_at       TEXT    DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS admins(
    user_id  INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    added_by INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS code_requests(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by INTEGER,
    days         INTEGER,
    status       TEXT DEFAULT 'pending',
    code         TEXT DEFAULT '',
    requested_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
# Add is_protected column if not exists
try:
    c.execute("ALTER TABLE users ADD COLUMN is_protected INTEGER DEFAULT 0")
except Exception: pass
conn.commit()

# ─────────────────────────── GLOBALS ─────────────────────────
bot              = TelegramClient("bot_session", API_ID, API_HASH)
pending: dict    = {}
scheduler_tasks: dict = {}
db_lock          = None

# ─────────────────────────── UTILS ───────────────────────────
def is_super_admin(uid):
    return uid == ADMIN_ID

def is_admin(uid):
    if uid == ADMIN_ID: return True
    return c.execute("SELECT user_id FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

async def db_write(sql, params=()):
    async with db_lock:
        c.execute(sql, params)
        conn.commit()
        return c.lastrowid

def now_utc():    return datetime.utcnow()
def now_iso():    return now_utc().isoformat()
def parse_iso(s): return datetime.fromisoformat(s)

def fmt_mins(secs):
    m = secs // 60
    if m < 60:    return f"{m} min"
    if m == 60:   return "1 hour"
    if m == 1440: return "Daily"
    h, r = divmod(m, 60)
    return f"{h}h {r}m" if r else f"{h}h"

def msgs_list(j):
    try:
        v = json.loads(j or "[]")
        return v if isinstance(v, list) else [str(v)]
    except Exception:
        return [j] if j else []

def gen_code(n=10):
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))

def upsert_user(uid, uname=""):
    row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if row: c.execute("UPDATE users SET username=? WHERE user_id=?", (uname or "", uid))
    else:   c.execute("INSERT INTO users(user_id,username) VALUES(?,?)", (uid, uname or ""))
    conn.commit()

async def check_access(uid):
    if is_admin(uid): return True, "ADMIN"
    row = c.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0]: return False, "BANNED"
    code = c.execute(
        "SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (uid,)
    ).fetchone()
    if code and now_utc() <= parse_iso(code[1]): return True, code[0]
    trial = c.execute(
        "SELECT trial_expires FROM users WHERE user_id=? AND trial_granted=1", (uid,)
    ).fetchone()
    if trial and now_utc() <= parse_iso(trial[0]): return True, "TRIAL"
    return False, None

async def open_client(phone, sess_str):
    try:
        cl = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
        await cl.connect()
        if await cl.is_user_authorized(): return cl
        await cl.disconnect()
    except Exception: pass
    return None

async def close(cl):
    try: await cl.disconnect()
    except Exception: pass

# ─────────────────────────── KEYBOARDS ───────────────────────
def main_kb():
    return [
        [Button.text("➕ Add Account"),   Button.text("📊 My Groups")],
        [Button.text("⏰ Schedule Msg"),  Button.text("🚀 Send Now")],
        [Button.text("📋 My Schedules"), Button.text("🛑 Stop All")],
        [Button.text("📊 My Groups"),    Button.text("⚙️ Settings")],
        [Button.text("🔑 Redeem Code")],
    ]

def admin_kb():
    return [
        [Button.text("👥 Users"),     Button.text("📱 All Numbers")],
        [Button.text("🔑 Codes"),     Button.text("⏰ All Tasks")],
        [Button.text("➕ Gen Code"),  Button.text("📊 Stats")],
        [Button.text("📢 Broadcast"), Button.text("👑 Admins")],
        [Button.text("🔙 User Menu")],
    ]

def action_btns():
    return [
        [Button.inline("🚀 Send Now",     b"do_send_now")],
        [Button.inline("⏰ Schedule",     b"do_schedule")],
        [Button.inline("📋 My Schedules", b"view_tasks")],
        [Button.inline("❌ Cancel",       b"cx")],
    ]

# ─────────────────────────── WELCOME ─────────────────────────
async def send_welcome(event, caption, buttons):
    try:
        if pathlib.Path(WELCOME_PHOTO).exists():
            await bot.send_file(
                event.chat_id, WELCOME_PHOTO,
                caption=caption, buttons=buttons, parse_mode="markdown"
            )
        else:
            await event.reply(caption, buttons=buttons)
    except Exception:
        await event.reply(caption, buttons=buttons)

# ─────────────────────────── SCHEDULER ───────────────────────
async def run_task(task_id, uid, phone, sess, interval):
    while True:
        row = c.execute(
            "SELECT is_active,messages_json,current_msg_idx,fail_count "
            "FROM scheduled_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row or not row[0]: break

        active, mj, idx, fails = row
        msgs     = msgs_list(mj)
        if not msgs: await asyncio.sleep(interval); continue

        msg      = msgs[idx % len(msgs)]
        next_idx = (idx + 1) % len(msgs)
        cl       = await open_client(phone, sess)
        next_run = (now_utc() + timedelta(seconds=interval)).isoformat()

        if cl:
            try:
                dialogs = await cl.get_dialogs(limit=None)
                groups  = [d for d in dialogs if d.is_group or d.is_channel]
                sent    = 0
                for g in groups:
                    try:
                        await cl.send_message(g.entity, msg)
                        sent += 1
                        await asyncio.sleep(1)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds + 5)
                    except Exception: pass
                await close(cl)
                await db_write(
                    "UPDATE scheduled_tasks SET fail_count=0,current_msg_idx=?,next_run=? WHERE id=?",
                    (next_idx, next_run, task_id)
                )
                label = f"msg {idx+1}/{len(msgs)}" if len(msgs) > 1 else "msg"
                try:
                    await bot.send_message(uid,
                        f"✅ Task #{task_id} ({label}) → **{sent}** groups\n"
                        f"📝 `{msg[:60]}{'...' if len(msg)>60 else ''}`")
                except Exception: pass
            except Exception as e:
                await close(cl)
                fails += 1
                await db_write(
                    "UPDATE scheduled_tasks SET fail_count=?,next_run=? WHERE id=?",
                    (fails, next_run, task_id))
                if fails >= MAX_FAILS:
                    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (task_id,))
                    try:
                        await bot.send_message(uid,
                            f"🚫 Task #{task_id} auto-disabled ({MAX_FAILS} errors)\n`{e}`")
                    except Exception: pass
                    break
        else:
            fails += 1
            await db_write("UPDATE scheduled_tasks SET fail_count=?,next_run=? WHERE id=?",
                (fails, next_run, task_id))
            if fails >= MAX_FAILS:
                await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (task_id,))
                try:
                    await bot.send_message(uid,
                        f"🚫 Task #{task_id} auto-disabled — `{phone}` connect nahi hua {MAX_FAILS}x")
                except Exception: pass
                break

        await asyncio.sleep(interval)

def start_task(tid, uid, phone, sess, interval):
    t = asyncio.create_task(run_task(tid, uid, phone, sess, interval))
    scheduler_tasks[tid] = t

# ─────────────────────────── /start ──────────────────────────
@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    uid   = event.sender_id
    uname = getattr(event.sender, "username", "") or ""
    upsert_user(uid, uname)

    if is_admin(uid):
        await event.reply(
            "👑 **Welcome Admin!**\n/help — saari commands",
            buttons=[[Button.text("🔧 Admin Panel")], [Button.text("👤 User Menu")]]
        ); return

    ok, tag = await check_access(uid)

    if tag == "BANNED":
        await event.reply(
            "🚫 **Access Denied**\n\nTumhara account ban ho gaya hai.\n"
            "Admin se contact karo: @V4_XTRD"
        ); return

    welcome_text = (
        "🍂 **ALEXADS** 🍂\n"
        "**TG Ads Bot**\n\n"
        "🤖 @V4_XTRD_bot\n"
        "👑 Owner: @V4_XTRD\n\n"
        "📣 **Our Channel:** [Alex Store](https://t.me/alexstore037)\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚡ _Powerful Telegram Ads Bot_\n"
        "_Send ads to all groups automatically!_\n"
        "━━━━━━━━━━━━━━━━"
    )

    if ok:
        caption = welcome_text + "\n\n✅ **Welcome back! Access active hai.**\nMenu se kaam shuru karo 👇"
        await send_welcome(event, caption, main_kb()); return

    trial = c.execute("SELECT trial_granted FROM users WHERE user_id=?", (uid,)).fetchone()
    if not trial or not trial[0]:
        exp = (now_utc() + timedelta(days=28)).isoformat()
        c.execute("UPDATE users SET trial_granted=1,trial_expires=? WHERE user_id=?", (exp, uid))
        conn.commit()
        caption = (
            welcome_text +
            f"\n\n🎁 **Welcome! Tumhe 10 din ka FREE Trial mila!**\n"
            f"⏳ Valid till: **{exp.split('T')[0]}** (28 din)\n\n👇 Start karo!"
        )
        await send_welcome(event, caption, main_kb())
    else:
        caption = (
            welcome_text +
            "\n\n⏳ **Trial expire ho gaya.**\n\n"
            "🔑 Access ke liye:\n"
            "  /redeem CODE — Code lagao\n"
            "  📩 Contact: @V4\\_XTRD"
        )
        await send_welcome(event, caption, main_kb())

# ─────────────────────────── /help ───────────────────────────
@bot.on(events.NewMessage(pattern=r"^/help$"))
async def cmd_help(event):
    uid = event.sender_id
    u = (
        "📖 **USER COMMANDS**\n\n"
        "/start  /help  /cancel  /myid  /status\n"
        "/redeem CODE\n"
        "/addaccount — Phone se login\n"
        "/removeaccount +phone\n"
        "/mygroups\n"
        "/sendnow message\n"
        "/schedule\n"
        "/myschedules\n"
        "/starttask ID — Task start karo\n"
        "/stoptask ID  /deltask ID  /stopall\n"
        "/settings\n"
        "/protect — Apna account protect karo\n"
    )
    a = (
        "\n👑 **ADMIN COMMANDS**\n\n"
        "/admin  /stats\n"
        "/addadmin ID  /removeadmin ID  /admins\n"
        "/users  /userinfo ID\n"
        "/ban ID  /unban ID  /removeuser ID\n"
        "/gencode DAYS — Owner approve karega\n"
        "/extend ID DAYS  /revoke CODE  /codes\n"
        "/endtrial ID — User ka trial khatam karo\n"
        "/numbers  /removenum +phone\n"
        "/tasks  /adminstoptask ID  /admindeltask ID\n"
        "/adminstarttask ID — Stopped task start karo\n"
        "/usergroups ID\n"
        "/sendmsg ID text  /broadcast text\n"
        "\n🔒 **PROTECTION**\n"
        "/protect — Sab users protect/unprotect\n"
        "/pruser @username — Specific user protect\n"
        "/protectedlist — Protected users list\n"
    )
    await event.reply(u + (a if is_admin(uid) else ""))

# ─────────────────────────── /cancel ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
        await event.reply("✅ Cancel ho gaya.", buttons=main_kb())
    else:
        await event.reply("Kuch cancel nahi tha.", buttons=main_kb())

# ─────────────────────────── /myid ───────────────────────────
@bot.on(events.NewMessage(pattern=r"^/myid$"))
async def cmd_myid(event):
    uid   = event.sender_id
    uname = getattr(event.sender, "username", None) or "none"
    await event.reply(f"🆔 **Tumhara Telegram ID:** `{uid}`\n👤 Username: @{uname}")

# ─────────────────────────── /status ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/status$"))
async def cmd_status(event):
    uid     = event.sender_id
    ok, tag = await check_access(uid)
    if tag == "ADMIN":  await event.reply("👑 **Status: Admin**"); return
    if tag == "BANNED": await event.reply("🚫 **Status: Banned**"); return
    if ok and tag == "TRIAL":
        row = c.execute("SELECT trial_expires FROM users WHERE user_id=?", (uid,)).fetchone()
        await event.reply(f"🎁 **Status: Trial**\nExpiry: {(row[0] or '?').split('T')[0]}")
    elif ok:
        row = c.execute("SELECT expires_at FROM access_codes WHERE code=?", (tag,)).fetchone()
        await event.reply(f"✅ **Status: Active**\nCode: `{tag}`\nExpiry: {(row[0] or '?').split('T')[0]}")
    else:
        await event.reply("❌ **Status: No Access**\n/redeem CODE karo.")

# ─────────────────────────── /redeem ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/redeem\s+(\S+)$"))
async def cmd_redeem(event):
    await _do_redeem(event, event.sender_id, event.pattern_match.group(1).strip().upper())

# ─────────────────────────── /addaccount ─────────────────────
@bot.on(events.NewMessage(pattern=r"^/addaccount$"))
async def cmd_addaccount(event):
    uid     = event.sender_id
    ok, tag = await check_access(uid)
    if not ok:
        await event.reply("❌ Access nahi hai. /redeem CODE karo."); return
    cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
    if cnt >= MAX_ACCOUNTS:
        await event.reply(f"❌ Max {MAX_ACCOUNTS} accounts.\n/removeaccount +phone se pehle hatao."); return
    pending[uid] = {"action": "add_phone"}
    await event.reply(
        f"📱 Phone bhejo (e.g. `+919876543210`)\n({cnt}/{MAX_ACCOUNTS})\n\n"
        "⚠️ Agar Telegram block kare toh /addsession use karo\n/cancel se wapas."
    )


# ─────────────────────────── /removeaccount ──────────────────
@bot.on(events.NewMessage(pattern=r"^/removeaccount\s+(\+\d+)$"))
async def cmd_removeaccount(event):
    uid   = event.sender_id
    phone = event.pattern_match.group(1).strip()
    row   = c.execute("SELECT user_id FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not row:
        await event.reply("❌ Yeh phone linked nahi."); return
    if row[0] != uid and not is_admin(uid):
        await event.reply("❌ Yeh account tumhara nahi."); return
    await db_write("DELETE FROM user_accounts WHERE phone=?", (phone,))
    await event.reply(f"🗑 `{phone}` removed.", buttons=main_kb())

# ─────────────────────────── /mygroups ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/mygroups$"))
async def cmd_mygroups(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi hai."); return
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.reply("Koi account nahi. /addaccount ya /addsession karo."); return
    msg = await event.reply("🔍 Fetching groups...")
    lines = ["📊 **Tumhare Groups:**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"\n📵 `{phone}`: connect fail"); continue
        try:
            dlgs   = await cl.get_dialogs(limit=None)
            groups = [d for d in dlgs if d.is_group or d.is_channel]
            lines.append(f"\n📱 `{phone}` — **{len(groups)} groups:**")
            for g in groups:
                icon  = "📣" if g.is_channel else "👥"
                uname = f"@{g.entity.username}" if getattr(g.entity, 'username', None) else "🔒 private"
                lines.append(f"  {icon} {g.name}  |  {uname}")
        except Exception as e: lines.append(f"\n⚠️ `{phone}`: {e}")
        finally: await close(cl)
    full = "\n".join(lines)
    await msg.edit(full[:4000])
    if len(full) > 4000: await event.reply(full[4000:8000])

# ─────────────────────────── /sendnow ────────────────────────
@bot.on(events.NewMessage(pattern=r"^/sendnow\s+(.+)$"))
async def cmd_sendnow(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    text     = event.pattern_match.group(1).strip()
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.reply("❌ Koi account nahi."); return
    msg = await event.reply("📤 Sending...")
    await _send_now_core(msg, uid, text, accounts)

# ─────────────────────────── /schedule ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/schedule$"))
async def cmd_schedule(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    if not c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]:
        await event.reply("❌ Koi account nahi. /addsession karo."); return
    pending[uid] = {"action": "await_msg", "mode": "schedule", "messages": []}
    await event.reply("📝 **Message #1 type karo** (ya forward karo):\n\nMultiple messages add kar sakte ho.\n/cancel se wapas.")

# ─────────────────────────── /myschedules ────────────────────
@bot.on(events.NewMessage(pattern=r"^/myschedules$"))
async def cmd_myschedules(event):
    await _show_schedules(event, event.sender_id, edit=False)

# ─────────────────────────── /stoptask ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/starttask\s+(\d+)$"))
async def cmd_starttask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,phone,interval_seconds,is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply("❌ Task nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    if row[3]: await event.reply("⚠️ Task already chal raha hai."); return
    phone, iv = row[1], row[2]
    sess_row  = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row: await event.reply("❌ Account nahi mila. /addaccount karo."); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid, phone, sess_row[0], iv)
    await event.reply("▶️ **Task #" + str(tid) + " Start Ho Gaya!**", buttons=main_kb())

@bot.on(events.NewMessage(pattern=r"^/stoptask\s+(\d+)$"))
async def cmd_stoptask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply(f"❌ Task #{tid} nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.reply(f"⏹ Task #{tid} stop ho gaya.")

# ─────────────────────────── /deltask ────────────────────────
@bot.on(events.NewMessage(pattern=r"^/deltask\s+(\d+)$"))
async def cmd_deltask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply(f"❌ Task #{tid} nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.reply(f"🗑 Task #{tid} deleted.")

# ─────────────────────────── /stopall ────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/stopall", "🛑 Stop All"]))
async def cmd_stopall(event):
    uid = event.sender_id
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE user_id=?", (uid,))
    stopped = 0
    for tid in list(scheduler_tasks):
        row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row and row[0] == uid:
            scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]; stopped += 1
    await event.reply(f"🛑 {stopped} task(s) stop.", buttons=main_kb())

# ─────────────────────────── /settings ───────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/settings", "⚙️ Settings"]))
async def cmd_settings(event):
    uid      = event.sender_id
    ok, tag  = await check_access(uid)
    accounts = c.execute("SELECT phone,added_at FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    tasks    = c.execute("SELECT id,interval_seconds,is_active,messages_json FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall()
    lines = [
        "⚙️ **Settings**\n",
        f"🔐 Access: {'✅ ' + str(tag) if ok else '❌ No access'}",
        f"\n📱 Accounts ({len(accounts)}/{MAX_ACCOUNTS}):",
    ]
    for ph, added in accounts:
        lines.append(f"  • `{ph}` — {(added or '').split('T')[0]}\n    /removeaccount {ph}")
    if not accounts: lines.append("  Koi nahi — /addsession")
    lines.append(f"\n⏰ Tasks ({len(tasks)}):")
    for tid, iv, act2, mj in tasks:
        nm = len(msgs_list(mj))
        lines.append(
            f"  {'▶️' if act2 else '⏹'} #{tid} | {fmt_mins(iv)} | {nm} msg(s)\n"
            f"    /stoptask {tid}  /deltask {tid}"
        )
    if not tasks: lines.append("  Koi nahi — /schedule")
    await event.reply("\n".join(lines), buttons=main_kb())

# ─────────────────────────── ADMIN COMMANDS ──────────────────
@bot.on(events.NewMessage(pattern=r"^/addadmin\s+(\d+)$"))
async def cmd_addadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    if uid == ADMIN_ID:
        await event.reply("⚠️ Yeh already Super Admin hai."); return
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = urow[0] if urow and urow[0] else ""
    await db_write("INSERT OR REPLACE INTO admins(user_id,username,added_by) VALUES(?,?,?)",
        (uid, uname, event.sender_id))
    name = f"@{uname}" if uname else f"`{uid}`"
    name = f"@{uname}" if uname else f"`{uid}`"
    msg = "✅ " + name + " Admin ban gaya!\n\n🔰 Woh admin panel use kar sakta hai.\n⚠️ Woh naye admin nahi bana sakta.\n\n/removeadmin " + str(uid)
    await event.reply(msg, buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/removeadmin\s+(\d+)$"))
async def cmd_removeadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM admins WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await event.reply(f"❌ `{uid}` admin nahi hai."); return
    await db_write("DELETE FROM admins WHERE user_id=?", (uid,))
    await event.reply(f"🗑 `{uid}` admin se remove ho gaya.", buttons=admin_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/admins", "👑 Admins"]))
async def cmd_admins_list(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT user_id,username,added_at FROM admins").fetchall()
    out = "👑 **Super Admin (Sirf Tum):**\n"
    out += "  • `" + str(ADMIN_ID) + "` — Full Powers\n\n"
    out += "🔰 **Sub Admins** (" + str(len(rows)) + "):\n"
    if rows:
        for uid2, uname, added in rows:
            name = "@" + uname if uname else "`" + str(uid2) + "`"
            out += "  • " + name + " | `" + str(uid2) + "`\n"
            if is_super_admin(event.sender_id):
                out += "    /removeadmin " + str(uid2) + "\n"
    else:
        out += "  Koi sub admin nahi\n"
    if is_super_admin(event.sender_id):
        out += "\n➕ Add karo: /addadmin USER_ID"
    await event.reply(out, buttons=admin_kb())




@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/admin", "🔧 Admin Panel"]))
async def cmd_admin(event):
    if not is_admin(event.sender_id): return
    await event.reply("👑 **Admin Panel**", buttons=admin_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["👤 User Menu", "🔙 User Menu"]))
async def cmd_usermenu(event):
    await event.reply("👤 **User Menu**", buttons=main_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/stats", "📊 Stats"]))
async def cmd_stats(event):
    if not is_admin(event.sender_id): return
    total  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    trials = c.execute("SELECT COUNT(*) FROM users WHERE trial_granted=1 AND trial_expires>?", (now_iso(),)).fetchone()[0]
    phones = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
    codes  = c.execute("SELECT COUNT(*) FROM access_codes").fetchone()[0]
    actc   = c.execute("SELECT COUNT(*) FROM access_codes WHERE is_active=1 AND expires_at>?", (now_iso(),)).fetchone()[0]
    tasks  = c.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE is_active=1").fetchone()[0]
    await event.reply(
        f"📊 **Stats**\n\nUsers: `{total}` | Banned: `{banned}`\nTrials: `{trials}`\n"
        f"Numbers: `{phones}`\nCodes: `{codes}` (active: `{actc}`)\nRunning Tasks: `{tasks}`",
        buttons=admin_kb()
    )

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/users", "👥 Users"]))
async def cmd_users(event):
    if not is_admin(event.sender_id): return
    rows = c.execute(
        "SELECT user_id,username,trial_granted,trial_expires,is_banned FROM users ORDER BY rowid DESC LIMIT 20"
    ).fetchall()
    if not rows: await event.reply("Koi user nahi.", buttons=admin_kb()); return
    lines = ["👥 **Users** (last 20)\n"]
    buttons = []
    for uid2, uname, trial, texp, banned in rows:
        prot = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid2,)).fetchone()
        is_prot = prot[0] if prot else 0
        if is_prot and not is_super_admin(event.sender_id):
            lines.append("🔒 Protected User | /userinfo " + str(uid2))
            buttons.append([Button.inline("🔒 Protected", b"noop")])
            continue
        ph  = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid2,)).fetchone()[0]
        cod = c.execute("SELECT code FROM access_codes WHERE claimed_by=? AND is_active=1", (uid2,)).fetchone()
        st  = "🚫" if banned else ("✅" if cod else "🎁" if (trial and texp and now_utc() <= parse_iso(texp)) else "❌")
        name = f"@{uname}" if uname else f"ID:{uid2}"
        lines.append(f"• {name} `{uid2}` 📱{ph} {st}\n  /userinfo {uid2}")
        buttons.append([
            Button.inline(f"ℹ️ {name[:12]}", f"uinfo_{uid2}".encode()),
            Button.inline("🚫 Ban",           f"uban_{uid2}".encode()),
            Button.inline("🗑 Del",           f"udelc_{uid2}".encode()),
        ])
    await event.reply("\n".join(lines), buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/userinfo\s+(\d+)$"))
async def cmd_userinfo(event):
    if not is_admin(event.sender_id): return
    await _show_userinfo(event, int(event.pattern_match.group(1)), event.sender_id)

async def _show_userinfo(ctx, uid, requester_id=None):
    row = c.execute("SELECT user_id,username,trial_granted,trial_expires,is_banned,joined_at,is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await ctx.reply("❌ User `" + str(uid) + "` nahi mila."); return
    # Protection check — sub admin cannot see protected user
    is_prot = row[6] if len(row) > 6 else 0
    if is_prot and requester_id and not is_super_admin(requester_id):
        await ctx.reply(
            "🔒 **Protected User**\n\n"
            "Is user ne apna data protect kiya hua hai.\n"
            "Sirf Owner details dekh sakta hai."
        ); return
    _, uname, trial, texp, banned, joined, is_prot2 = row
    phones = c.execute("SELECT phone,added_at FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    code   = c.execute("SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (uid,)).fetchone()
    tasks  = c.execute("SELECT id,interval_seconds,is_active FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall()
    name      = f"@{uname}" if uname else f"ID:{uid}"
    joined_dt = (joined or "").split("T")[0] or "?"
    prot_icon = "🔒" if is_prot2 else "🔓"

    if banned:
        status = "🚫 BANNED"
    elif code:
        status = "✅ Active | " + code[0] + " | exp " + code[1].split("T")[0]
    elif trial and texp and now_utc() <= parse_iso(texp):
        status = "🎁 Trial | exp " + texp.split("T")[0]
    else:
        status = "❌ No Access"

    out = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **" + name + "** | `" + str(uid) + "`\n"
        "📅 Joined: " + joined_dt + "\n"
        "🔐 " + status + "\n"
        "" + prot_icon + " Protection: " + ("ON" if is_prot2 else "OFF") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📱 Accounts (" + str(len(phones)) + "/3):\n"
    )
    for ph, ad in phones:
        out += "  • `" + ph + "`\n"
    if not phones:
        out += "  Koi nahi\n"

    out += "\n⏰ Tasks (" + str(len(tasks)) + "):\n"
    for tid, iv, act2 in tasks:
        icon = "▶️" if act2 else "⏹"
        out += "  " + icon + " #" + str(tid) + " " + fmt_mins(iv) + "  /adminstoptask " + str(tid) + "\n"
    if not tasks:
        out += "  Koi task nahi\n"

    out += "━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "/ban " + str(uid) + "  /unban " + str(uid) + "  /removeuser " + str(uid)

    btns = [
        [Button.inline("📊 Groups",  ("ugrp_" + str(uid)).encode()),
         Button.inline("➕ Extend",  ("uext_" + str(uid)).encode())],
        [Button.inline("🚫 Ban",     ("uban_" + str(uid)).encode()),
         Button.inline("✅ Unban",   ("uunb_" + str(uid)).encode())],
        [Button.inline("🔒 Protect" if not is_prot2 else "🔓 Unprotect",
                       ("upr_" + str(uid)).encode()),
         Button.inline("⏹ End Trial", ("uet_" + str(uid)).encode())],
        [Button.inline("🗑 Delete",  ("udelc_" + str(uid)).encode())],
    ]
    try:
        await ctx.edit(out, buttons=btns)
    except Exception:
        await ctx.reply(out, buttons=btns)

@bot.on(events.NewMessage(pattern=r"^/ban\s+(\d+)$"))
async def cmd_ban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await event.reply(f"🚫 `{uid}` banned.", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/unban\s+(\d+)$"))
async def cmd_unban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await event.reply(f"✅ `{uid}` unbanned.", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/removeuser\s+(\d+)$"))
async def cmd_removeuser(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    _del_user(uid)
    await event.reply(f"🗑 User `{uid}` deleted.", buttons=admin_kb())

def _del_user(uid):
    for tid, in c.execute("SELECT id FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall():
        if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    c.execute("DELETE FROM scheduled_tasks WHERE user_id=?", (uid,))
    c.execute("DELETE FROM user_accounts WHERE user_id=?", (uid,))
    c.execute("UPDATE access_codes SET claimed_by=NULL,claimed_at=NULL WHERE claimed_by=?", (uid,))
    c.execute("DELETE FROM users WHERE user_id=?", (uid,))
    conn.commit()

@bot.on(events.NewMessage(func=lambda e: e.text and (e.text.strip() == "➕ Gen Code" or e.text.strip().startswith("/gencode"))))
async def cmd_gencode(event):
    if not is_admin(event.sender_id): return
    import re
    text = event.text.strip()
    if text == "➕ Gen Code":
        pending[event.sender_id] = {"action": "admin_gencode"}
        await event.reply("🔑 Kitne din ka code? (e.g. `30`)", buttons=[[Button.text("❌ Cancel")]]); return
    m = re.match(r"^/gencode\s+(\d+)$", text)
    if not m: await event.reply("Usage: /gencode 30"); return
    await _do_gencode(event, int(m.group(1)), event.sender_id)

async def _do_gencode(ctx, days, requester_id=None):
    # Super admin — direct generate
    if requester_id is None or is_super_admin(requester_id):
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at) VALUES(?,?,?,?)",
            (code, days, now_iso(), expires))
        await ctx.reply(
            "✅ **Code Generate Hua!**\n\n🔑 `" + code + "`\n📅 " + str(days) + " din\n⏳ " + expires.split("T")[0] + "\n\n/redeem " + code,
            buttons=admin_kb()
        )
    else:
        # Sub admin — send approval request to owner
        req_id = await db_write(
            "INSERT INTO code_requests(requested_by,days) VALUES(?,?)",
            (requester_id, days)
        )
        urow = c.execute("SELECT username FROM users WHERE user_id=?", (requester_id,)).fetchone()
        uname = urow[0] if urow and urow[0] else ""
        name  = "@" + uname if uname else "`" + str(requester_id) + "`"
        await ctx.reply(
            "⏳ **Request bheji gayi!**\n\nOwner verify karega tab code milega.",
            buttons=admin_kb()
        )
        # Notify super admin
        await bot.send_message(
            ADMIN_ID,
            "🔔 **Code Request Aayi!**\n\n"
            "👤 Admin: " + name + "\n"
            "📅 Days: **" + str(days) + "**\n\n"
            "Approve karo toh code generate hoga.",
            buttons=[
                [Button.inline("✅ Approve", ("creq_ok_" + str(req_id)).encode())],
                [Button.inline("❌ Reject",  ("creq_no_" + str(req_id)).encode())],
            ]
        )

# Approve callback
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"creq_ok_")))
async def cb_creq_ok(event):
    if not is_super_admin(event.sender_id): return
    req_id = int(event.data.decode().replace("creq_ok_", ""))
    row = c.execute("SELECT requested_by,days,status FROM code_requests WHERE id=?", (req_id,)).fetchone()
    if not row: await event.edit("❌ Request nahi mili."); return
    requester, days, status = row
    if status != "pending": await event.edit("⚠️ Already processed."); return
    code    = gen_code()
    expires = (now_utc() + timedelta(days=days)).isoformat()
    await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at) VALUES(?,?,?,?)",
        (code, days, now_iso(), expires))
    await db_write("UPDATE code_requests SET status=?,code=? WHERE id=?", ("approved", code, req_id))
    await event.edit(
        "✅ **Approved!**\n\n🔑 `" + code + "`\n📅 " + str(days) + " din\n⏳ " + expires.split("T")[0]
    )
    try:
        await bot.send_message(
            requester,
            "✅ **Code Approved By Owner!**\n\n🔑 `" + code + "`\n📅 " + str(days) + " din\n⏳ " + expires.split("T")[0] + "\n\n/redeem " + code
        )
    except Exception: pass

# Reject callback
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"creq_no_")))
async def cb_creq_no(event):
    if not is_super_admin(event.sender_id): return
    req_id = int(event.data.decode().replace("creq_no_", ""))
    row = c.execute("SELECT requested_by,days,status FROM code_requests WHERE id=?", (req_id,)).fetchone()
    if not row: await event.edit("❌ Request nahi mili."); return
    requester, days, status = row
    if status != "pending": await event.edit("⚠️ Already processed."); return
    await db_write("UPDATE code_requests SET status=? WHERE id=?", ("rejected", req_id))
    await event.edit("❌ **Rejected.**")
    try:
        await bot.send_message(requester, "❌ **Code Request Reject Ho Gayi.**\nOwner ne approve nahi kiya.")
    except Exception: pass

@bot.on(events.NewMessage(pattern=r"^/extend\s+(\d+)\s+(\d+)$"))
async def cmd_extend(event):
    if not is_admin(event.sender_id): return
    await _do_extend(event, int(event.pattern_match.group(1)), int(event.pattern_match.group(2)))

async def _do_extend(ctx, target_uid, days):
    row = c.execute("SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (target_uid,)).fetchone()
    if row:
        new_exp = (parse_iso(row[1]) + timedelta(days=days)).isoformat()
        await db_write("UPDATE access_codes SET expires_at=?,days_valid=days_valid+? WHERE code=?",
            (new_exp, days, row[0]))
        await ctx.reply(f"✅ `{target_uid}` +{days} din. New expiry: {new_exp.split('T')[0]}", buttons=admin_kb())
    else:
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,claimed_by,claimed_at,expires_at) VALUES(?,?,?,?,?,?)",
            (code, days, now_iso(), target_uid, now_iso(), expires))
        await ctx.reply(f"✅ Code `{code}` → user `{target_uid}`. Expiry: {expires.split('T')[0]}", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/revoke\s+(\S+)$"))
async def cmd_revoke(event):
    if not is_admin(event.sender_id): return
    code = event.pattern_match.group(1).upper()
    await db_write("UPDATE access_codes SET is_active=0 WHERE code=?", (code,))
    await event.reply(f"🚫 `{code}` revoked.", buttons=admin_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/codes", "🔑 Codes"]))
async def cmd_codes(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes ORDER BY rowid DESC LIMIT 20").fetchall()
    if not rows: await event.reply("Koi code nahi.", buttons=admin_kb()); return
    lines = ["🔑 **Codes** (last 20)\n"]
    buttons = []
    for code, days, cb, exp, active in rows:
        st = "🚫" if not active else ("❌" if now_utc() > parse_iso(exp) else "✅")
        lines.append(f"{st} `{code}` | {days}d | {f'`{cb}`' if cb else '—'} | {exp.split('T')[0]}")
        if active and now_utc() <= parse_iso(exp):
            buttons.append([Button.inline(f"🚫 Revoke {code}", f"rev_{code}".encode())])
    await event.reply("\n".join(lines), buttons=buttons or None)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/numbers", "📱 All Numbers"]))
async def cmd_numbers(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT ua.phone,ua.user_id,u.username,ua.added_at FROM user_accounts ua LEFT JOIN users u ON ua.user_id=u.user_id ORDER BY ua.added_at DESC").fetchall()
    if not rows: await event.reply("Koi number nahi.", buttons=admin_kb()); return
    lines = [f"📱 **All Numbers** ({len(rows)})\n"]
    buttons = []
    for phone, uid2, uname, added in rows:
        name = f"@{uname}" if uname else f"ID:{uid2}"
        lines.append(f"• `{phone}` — {name}  /removenum {phone}")
        buttons.append([Button.inline(f"🗑 {phone}", f"rmnum_{phone}".encode())])
    await event.reply("\n".join(lines), buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/removenum\s+(\+\d+)$"))
async def cmd_removenum(event):
    if not is_admin(event.sender_id): return
    phone = event.pattern_match.group(1).strip()
    await db_write("DELETE FROM user_accounts WHERE phone=?", (phone,))
    await event.reply(f"🗑 `{phone}` removed.", buttons=admin_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/tasks", "⏰ All Tasks"]))
async def cmd_tasks(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT st.id,st.user_id,u.username,st.phone,st.interval_seconds,st.is_active,st.fail_count,st.messages_json FROM scheduled_tasks st LEFT JOIN users u ON st.user_id=u.user_id ORDER BY st.id DESC").fetchall()
    if not rows: await event.reply("Koi task nahi.", buttons=admin_kb()); return
    lines = [f"⏰ **All Tasks** ({len(rows)})\n"]
    buttons = []
    for tid, uid2, uname, phone, iv, act2, fails, mj in rows:
        name = f"@{uname}" if uname else f"ID:{uid2}"
        nm   = len(msgs_list(mj))
        lines.append(f"{'▶️' if act2 else '⏹'} **#{tid}** {name} | `{phone}` | {fmt_mins(iv)} | {nm}msg")
        if act2: buttons.append([Button.inline(f"🛑 Stop #{tid}", f"ast_{tid}".encode())])
    await event.reply("\n".join(lines), buttons=buttons or None)

@bot.on(events.NewMessage(pattern=r"^/adminstarttask\s+(\d+)$"))
async def cmd_adminstarttask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,phone,interval_seconds,is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply("❌ Task nahi mila."); return
    uid2, phone, iv, active = row
    if active: await event.reply("⚠️ Task already chal raha hai."); return
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid2, phone)).fetchone()
    if not sess_row: await event.reply("❌ Session nahi mila."); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid2, phone, sess_row[0], iv)
    await event.reply("▶️ **Task #" + str(tid) + " Started!**", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/adminstoptask\s+(\d+)$"))
async def cmd_adminstoptask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.reply("🛑 Task #" + str(tid) + " stopped.", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/endtrial\s+(\d+)$"))
async def cmd_endtrial(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,username,trial_granted,trial_expires FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.reply("❌ User nahi mila."); return
    if not row[2]: await event.reply("⚠️ Is user ka trial tha hi nahi."); return
    await db_write("UPDATE users SET trial_expires=?,trial_granted=0 WHERE user_id=?",
        (now_iso(), uid))
    name = "@" + row[1] if row[1] else "`" + str(uid) + "`"
    await event.reply("✅ **" + name + " ka Trial Khatam Kar Diya!**\n\nAb woh /redeem se code lega.", buttons=admin_kb())
    try:
        await bot.send_message(uid,
            "⚠️ **Tumhara Trial Khatam Ho Gaya**\n\n"
            "Admin ne tumhara trial end kar diya.\n"
            "Access ke liye /redeem CODE karo."
        )
    except Exception: pass

@bot.on(events.NewMessage(pattern=r"^/admindeltask\s+(\d+)$"))
async def cmd_admindeltask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.reply(f"🗑 Task #{tid} deleted.", buttons=admin_kb())

@bot.on(events.NewMessage(pattern=r"^/usergroups\s+(\d+)$"))
async def cmd_usergroups(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    msg = await event.reply("🔍 Fetching...")
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    urow     = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name     = f"@{urow[0]}" if urow and urow[0] else f"ID:{uid}"
    if not accounts: await msg.edit(f"📊 {name} ke koi accounts nahi."); return
    lines = [f"📊 **{name} ke Groups**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"\n📵 `{phone}`: fail"); continue
        try:
            dlgs   = await cl.get_dialogs(limit=None)
            groups = [d for d in dlgs if d.is_group or d.is_channel]
            lines.append(f"\n📱 `{phone}` — {len(groups)} groups:")
            for g in groups:
                icon  = "📣" if g.is_channel else "👥"
                uname = f"@{g.entity.username}" if getattr(g.entity, 'username', None) else "🔒 private"
                lines.append(f"  {icon} {g.name}  |  {uname}")
        except Exception as e: lines.append(f"\n⚠️ `{phone}`: {e}")
        finally: await close(cl)
    await msg.edit("\n".join(lines)[:4000])

@bot.on(events.NewMessage(pattern=r"^/sendmsg\s+(\d+)\s+(.+)$"))
async def cmd_sendmsg(event):
    if not is_admin(event.sender_id): return
    target = int(event.pattern_match.group(1))
    text   = event.pattern_match.group(2).strip()
    try:
        await bot.send_message(target, f"📨 **Admin ka message:**\n\n{text}")
        await event.reply(f"✅ Sent to `{target}`.", buttons=admin_kb())
    except Exception as e:
        await event.reply(f"❌ Failed: {e}", buttons=admin_kb())

@bot.on(events.NewMessage(func=lambda e: e.text and (e.text.strip() == "📢 Broadcast" or e.text.strip().startswith("/broadcast"))))
async def cmd_broadcast(event):
    if not is_admin(event.sender_id): return
    import re
    text = event.text.strip()
    if text == "📢 Broadcast":
        pending[event.sender_id] = {"action": "admin_broadcast"}
        await event.reply("📢 Message type karo:", buttons=[[Button.text("❌ Cancel")]]); return
    m = re.match(r"^/broadcast\s+(.+)$", text, re.DOTALL)
    if not m: await event.reply("Usage: /broadcast text here"); return
    await _do_broadcast(event, m.group(1).strip())

async def _do_broadcast(ctx, text):
    users = c.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    prog  = await ctx.reply(f"📢 Sending to {len(users)} users...")
    sent  = 0; failed = 0
    for (uid2,) in users:
        try:
            await bot.send_message(uid2, f"📢 **Admin ka message:**\n\n{text}")
            sent += 1; await asyncio.sleep(0.3)
        except Exception: failed += 1
    await prog.edit(f"📢 **Done!** ✅ Sent: {sent} | ❌ Failed: {failed}", buttons=admin_kb())

# ─────────────────────────── BUTTON HANDLERS ─────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "➕ Add Account"))
async def btn_add(event): await cmd_addaccount(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📊 My Groups"))
async def btn_groups(event): await cmd_mygroups(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "⏰ Schedule Msg"))
async def btn_sched(event): await cmd_schedule(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🚀 Send Now"))
async def btn_sendnow(event):
    uid   = event.sender_id
    ok, _ = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    if not c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]:
        await event.reply("❌ Account nahi. /addsession karo."); return
    pending[uid] = {"action": "await_msg", "mode": "send_now"}
    await event.reply("✏️ Message type karo (ya forward karo):\n/cancel se wapas.")

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📋 My Schedules"))
async def btn_scheds(event): await _show_schedules(event, event.sender_id, edit=False)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🔑 Redeem Code"))
async def btn_redeem(event):
    uid = event.sender_id
    pending[uid] = {"action": "await_redeem_code"}
    await event.reply("🔑 Code type karo:\n/cancel se wapas.", buttons=[[Button.text("❌ Cancel")]])

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "❌ Cancel"))
async def btn_cancel(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
    await event.reply("✅ Cancel ho gaya.", buttons=main_kb())

# ─────────────────────────── FORWARD ─────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.message and e.message.fwd_from is not None))
async def on_forward(event):
    uid   = event.sender_id
    ok, _ = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    text  = event.message.message or "[Forwarded media]"
    st    = pending.get(uid, {})
    if st.get("action") == "await_msg" and st.get("mode") == "schedule":
        msgs = st.setdefault("messages", [])
        msgs.append(text)
        await event.reply(
            f"📩 **Message #{len(msgs)} added!**\n`{text[:100]}`",
            buttons=[
                [Button.inline(f"➕ Add #{len(msgs)+1}", b"add_msg")],
                [Button.inline("▶️ Continue",           b"msgs_done")],
                [Button.inline("❌ Cancel",             b"cx")],
            ]
        )
    else:
        pending[uid] = {"action": "msg_ready", "text": text}
        await event.reply(
            f"📩 **Forward detect hua!**\n`{text[:200]}`\n\nKya karna hai?",
            buttons=action_btns()
        )

# ─────────────────────────── CALLBACKS ───────────────────────
@bot.on(events.CallbackQuery(data=b"cx"))
async def cb_cx(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
    await event.edit("❌ Cancel ho gaya.")

@bot.on(events.CallbackQuery(data=b"noop"))
async def cb_noop(event): await event.answer()

@bot.on(events.CallbackQuery(data=b"do_send_now"))
async def cb_do_send_now(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "msg_ready":
        await event.answer("Koi message nahi.", alert=True); return
    text     = pending.pop(uid)["text"]
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.edit("❌ Koi account nahi."); return
    await event.edit("📤 Sending...")
    await _send_now_core(event, uid, text, accounts)

@bot.on(events.CallbackQuery(data=b"do_schedule"))
async def cb_do_schedule(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "msg_ready":
        await event.answer("Koi message nahi.", alert=True); return
    text = pending[uid].pop("text")
    pending[uid].update({"action": "schedule_pick_account", "messages": [text]})
    await _show_acct_picker(event, uid)

@bot.on(events.CallbackQuery(data=b"view_tasks"))
async def cb_view_tasks(event):
    pending.pop(event.sender_id, None)
    await _show_schedules(event, event.sender_id, edit=True)

@bot.on(events.CallbackQuery(data=b"add_msg"))
async def cb_add_msg(event):
    uid  = event.sender_id
    msgs = pending.get(uid, {}).get("messages", [])
    pending[uid]["action"] = "await_msg"
    pending[uid]["mode"]   = "schedule"
    await event.edit(f"✅ {len(msgs)} message(s) ready!\n📝 **Message #{len(msgs)+1} type karo:**\n/cancel se wapas.")

@bot.on(events.CallbackQuery(data=b"msgs_done"))
async def cb_msgs_done(event):
    uid  = event.sender_id
    msgs = pending.get(uid, {}).get("messages", [])
    if not msgs: await event.answer("Koi message nahi!", alert=True); return
    pending[uid]["action"] = "schedule_pick_account"
    await _show_acct_picker(event, uid)

async def _show_acct_picker(event, uid):
    accounts = c.execute("SELECT phone FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.edit("❌ Koi account nahi. /addsession karo."); pending.pop(uid, None); return
    msgs    = pending[uid].get("messages", [])
    preview = "\n".join(f"  {i+1}. `{m[:50]}`" for i, m in enumerate(msgs))
    btns    = [[Button.inline(f"📱 {ph[0]}", f"acct_{ph[0]}".encode())] for ph in accounts]
    btns.append([Button.inline("❌ Cancel", b"cx")])
    await event.edit(f"📝 **{len(msgs)} msg(s):**\n{preview}\n\n📱 **Kaunsa account?**", buttons=btns)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"acct_")))
async def cb_acct(event):
    uid   = event.sender_id
    phone = event.data.decode().replace("acct_", "")
    if uid not in pending or pending[uid].get("action") != "schedule_pick_account":
        await event.answer("Session expire.", alert=True); return
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row: await event.edit("❌ Account nahi mila."); pending.pop(uid, None); return
    pending[uid]["selected_phone"] = phone
    pending[uid]["selected_sess"]  = sess_row[0]
    pending[uid]["action"]         = "schedule_interval"
    await event.edit(
        f"✅ Account: `{phone}`\n\n⏰ **Interval choose karo:**",
        buttons=[
            [Button.inline("⏱ 5 min",   b"iv5"),   Button.inline("⏱ 10 min",  b"iv10")],
            [Button.inline("⏱ 15 min",  b"iv15"),  Button.inline("⏱ 30 min",  b"iv30")],
            [Button.inline("⏱ 45 min",  b"iv45"),  Button.inline("⏱ 1 hour",  b"iv60")],
            [Button.inline("⏱ 2 hours", b"iv120"), Button.inline("⏱ 6 hours", b"iv360")],
            [Button.inline("📅 12h",    b"iv720"), Button.inline("📅 Daily",  b"iv1440")],
            [Button.inline("✏️ Custom minutes", b"iv_custom")],
            [Button.inline("❌ Cancel", b"cx")],
        ]
    )

IV_MAP = {b"iv5":300, b"iv10":600, b"iv15":900, b"iv30":1800, b"iv45":2700,
          b"iv60":3600, b"iv120":7200, b"iv360":21600, b"iv720":43200, b"iv1440":86400}

@bot.on(events.CallbackQuery(data=lambda d: d in IV_MAP))
async def cb_interval(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "schedule_interval":
        await event.answer("Session expire.", alert=True); return
    await _create_task_cb(event, uid, IV_MAP[event.data])

@bot.on(events.CallbackQuery(data=b"iv_custom"))
async def cb_iv_custom(event):
    uid = event.sender_id
    if uid not in pending: await event.answer("Session expire.", alert=True); return
    pending[uid]["action"] = "schedule_custom_iv"
    await event.edit("✏️ **Kitne minutes?** Type karo:\nExamples: `5` `42` `200` (minimum 1)")

async def _create_task_cb(event, uid, iv_sec):
    data  = pending.pop(uid)
    msgs  = data.get("messages", [])
    phone = data.get("selected_phone")
    sess  = data.get("selected_sess")
    if not phone:
        row = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchone()
        if not row: await event.edit("❌ Koi account nahi."); return
        phone, sess = row
    tid = await db_write(
        "INSERT INTO scheduled_tasks(user_id,phone,messages_json,interval_seconds,next_run) VALUES(?,?,?,?,?)",
        (uid, phone, json.dumps(msgs), iv_sec,
         (now_utc() + timedelta(seconds=iv_sec)).isoformat())
    )
    start_task(tid, uid, phone, sess, iv_sec)
    preview = "\n".join(f"  {i+1}. `{m[:60]}`" for i, m in enumerate(msgs))
    await event.edit(
        f"✅ **Task #{tid} Schedule Ho Gaya!**\n\n📱 `{phone}`\n⏱ Har **{iv_sec//60} min**\n"
        f"💬 **{len(msgs)} msg(s):**\n{preview}\n\n/myschedules  /stoptask {tid}  /deltask {tid}"
    )

# Admin inline callbacks
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uinfo_")))
async def cb_uinfo(event):
    if not is_admin(event.sender_id): return
    await event.answer()  # instant response — stops loading spinner
    uid = int(event.data.decode().replace("uinfo_", ""))
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"ugrp_")))
async def cb_ugrp(event):
    if not is_admin(event.sender_id): return
    uid  = int(event.data.decode().replace("ugrp_", ""))
    await event.edit("🔍 Fetching groups...")
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = f"@{urow[0]}" if urow and urow[0] else f"ID:{uid}"
    if not accounts: await event.edit(f"📊 {name} ke koi accounts nahi."); return
    lines = [f"📊 **{name} ke Groups**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"\n📵 `{phone}`: fail"); continue
        try:
            dlgs = await cl.get_dialogs(limit=None)
            grps = [d for d in dlgs if d.is_group or d.is_channel]
            lines.append(f"\n📱 `{phone}` — {len(grps)}:")
            for g in grps:
                icon  = "📣" if g.is_channel else "👥"
                uname = f"@{g.entity.username}" if getattr(g.entity, 'username', None) else "🔒 private"
                lines.append(f"  {icon} {g.name}  |  {uname}")
        except Exception as e: lines.append(f"\n⚠️ `{phone}`: {e}")
        finally: await close(cl)
    await event.edit("\n".join(lines)[:4000])

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uext_")))
async def cb_uext(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uext_", ""))
    pending[event.sender_id] = {"action": "admin_extend", "target_uid": uid}
    await event.edit(f"➕ User `{uid}` — kitne extra days?", buttons=[[Button.inline("❌ Cancel", b"cx")]])

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uban_")))
async def cb_uban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uban_", ""))
    await db_write("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await event.answer("🚫 Banned!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uunb_")))
async def cb_uunb(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uunb_", ""))
    await db_write("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await event.answer("✅ Unbanned!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"upr_")))
async def cb_upr(event):
    if not is_super_admin(event.sender_id):
        await event.answer("❌ Sirf Owner!", alert=True); return
    uid = int(event.data.decode().replace("upr_", ""))
    row = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.answer("User nahi mila.", alert=True); return
    new_val = 0 if row[0] else 1
    await db_write("UPDATE users SET is_protected=? WHERE user_id=?", (new_val, uid))
    await event.answer("🔒 Protected!" if new_val else "🔓 Unprotected!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uet_")))
async def cb_uet(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    uid = int(event.data.decode().replace("uet_", ""))
    row = c.execute("SELECT trial_granted,username FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.answer("User nahi mila.", alert=True); return
    if not row[0]:
        await event.answer("⚠️ Is user ka trial tha hi nahi.", alert=True); return
    await db_write("UPDATE users SET trial_expires=?,trial_granted=0 WHERE user_id=?", (now_iso(), uid))
    name = "@" + row[1] if row[1] else str(uid)
    await event.answer("✅ Trial ended!")
    try:
        await bot.send_message(uid,
            "⚠️ **Tumhara Trial Khatam Ho Gaya**\n\n"
            "Admin ne tumhara trial end kar diya.\n"
            "Access ke liye /redeem CODE karo."
        )
    except Exception: pass
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"udelc_")))
async def cb_udelc(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("udelc_", ""))
    row = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = f"@{row[0]}" if row and row[0] else f"ID:{uid}"
    await event.edit(
        f"⚠️ **Confirm delete `{name}`?**\nSaara data delete hoga.",
        buttons=[[Button.inline("✅ Delete", f"udely_{uid}".encode()),
                  Button.inline("❌ Cancel", b"cx")]]
    )

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"udely_")))
async def cb_udely(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("udely_", ""))
    _del_user(uid)
    await event.edit(f"🗑 User `{uid}` deleted.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"rmnum_")))
async def cb_rmnum(event):
    if not is_admin(event.sender_id): return
    phone = event.data.decode().replace("rmnum_", "")
    await db_write("DELETE FROM user_accounts WHERE phone=?", (phone,))
    await event.edit(f"🗑 `{phone}` removed.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"rev_")))
async def cb_rev(event):
    if not is_admin(event.sender_id): return
    code = event.data.decode().replace("rev_", "")
    await db_write("UPDATE access_codes SET is_active=0 WHERE code=?", (code,))
    await event.edit(f"🚫 `{code}` revoked.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"ast_")))
async def cb_ast(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("ast_", ""))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.edit(f"🛑 Task #{tid} stopped.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tsp_") and d != b"tsp_all"))
async def cb_tsp(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tsp_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.answer("⏹ Stopped!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tst_") and d != b"tst_all"))
async def cb_tst(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tst_", ""))
    row = c.execute("SELECT user_id,phone,interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    _, phone, iv = row
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row:
        await event.answer("❌ Account nahi mila. /addaccount karo.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid, phone, sess_row[0], iv)
    await event.answer("▶️ Started!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=b"tst_all"))
async def cb_tst_all(event):
    uid  = event.sender_id
    rows = c.execute(
        "SELECT id,phone,interval_seconds FROM scheduled_tasks WHERE user_id=? AND is_active=0", (uid,)
    ).fetchall()
    started = 0
    for tid, phone, iv in rows:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
        if not sess_row: continue
        await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
        if tid not in scheduler_tasks:
            start_task(tid, uid, phone, sess_row[0], iv)
        started += 1
    await event.answer("▶️ " + str(started) + " tasks started!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tdl_") and d != b"tdl_all"))
async def cb_tdl(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tdl_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.edit(f"🗑 Task #{tid} deleted.")

@bot.on(events.CallbackQuery(data=b"tsp_all"))
async def cb_tsp_all(event):
    uid = event.sender_id
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE user_id=?", (uid,))
    for tid in list(scheduler_tasks):
        row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row and row[0] == uid: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.answer("⏹ Sab stopped!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=b"tdl_all"))
async def cb_tdl_all(event):
    uid = event.sender_id
    for tid, in c.execute("SELECT id FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall():
        if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE user_id=?", (uid,))
    await event.edit("🗑 Saare tasks delete ho gaye!")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tms_")))
async def cb_tms(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tms_", ""))
    row = c.execute("SELECT user_id,messages_json FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    msgs  = msgs_list(row[1])
    lines = [f"📝 **Task #{tid} — {len(msgs)} Messages:**\n"]
    for i, m in enumerate(msgs, 1): lines.append(f"**{i}.** `{m[:200]}`\n")
    await event.edit("\n".join(lines)[:4000])

# ─────────────────────────── HELPERS ─────────────────────────
async def _do_redeem(ctx, uid, code):
    row = c.execute("SELECT * FROM access_codes WHERE code=?", (code,)).fetchone()
    if not row: await ctx.reply("❌ Code exist nahi karta."); return
    _, days, _, claimed_by, _, expires, active = row
    if not active: await ctx.reply("❌ Code revoke ho chuka hai."); return
    if claimed_by and claimed_by != uid: await ctx.reply("❌ Code kisi aur ne le liya."); return
    if now_utc() > parse_iso(expires): await ctx.reply("⚠️ Code expire ho gaya."); return
    if not claimed_by:
        await db_write("UPDATE access_codes SET claimed_by=?,claimed_at=? WHERE code=?", (uid, now_iso(), code))
    await ctx.reply(
        f"🎉 **Access Activate Ho Gaya!**\n\n🔑 Code: `{code}`\n📅 {days} din\n⏳ {expires.split('T')[0]}",
        buttons=main_kb()
    )

async def _send_now_core(status_msg, uid, text, accounts):
    total = 0; lines = []
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"📵 `{phone}`: fail"); continue
        try:
            dlgs   = await cl.get_dialogs(limit=None)
            groups = [d for d in dlgs if d.is_group or d.is_channel]
            sent   = 0
            for g in groups:
                try:
                    await cl.send_message(g.entity, text)
                    sent += 1; total += 1; await asyncio.sleep(1)
                except FloodWaitError as fw: await asyncio.sleep(fw.seconds + 5)
                except Exception: pass
            lines.append(f"✅ `{phone}`: {sent} groups")
        except Exception as e: lines.append(f"⚠️ `{phone}`: {e}")
        finally: await close(cl)
    await status_msg.edit(f"🚀 **Done! {total} groups mein bheja.**\n\n" + "\n".join(lines))

async def _show_schedules(ctx, uid, edit=False):
    rows = c.execute(
        "SELECT id,phone,messages_json,interval_seconds,is_active,next_run FROM scheduled_tasks WHERE user_id=? ORDER BY id DESC", (uid,)
    ).fetchall()
    if not rows:
        txt = "📋 Koi task nahi.\n/schedule se naya banao."
        if edit: await ctx.edit(txt)
        else:    await ctx.reply(txt, buttons=main_kb())
        return
    active_count  = sum(1 for r in rows if r[4])
    stopped_count = len(rows) - active_count
    lines   = [f"📋 **Tumhare Tasks** ({len(rows)}) | ▶️{active_count} ⏹{stopped_count}\n"]
    buttons = []
    for tid, phone, mj, iv, act2, nr in rows:
        msgs    = msgs_list(mj)
        st      = "▶️ RUNNING" if act2 else "⏹ STOPPED"
        nr_s    = (nr or "").split("T")[0] or "?"
        preview = (msgs[0][:40] + "...") if msgs and len(msgs[0]) > 40 else (msgs[0] if msgs else "—")
        lines.append(
            f"{st} **Task #{tid}**\n"
            f"   📱 `{phone}` · {fmt_mins(iv)} · {len(msgs)} msg\n"
            f"   📝 `{preview}`  🕐 {nr_s}"
        )
        row_btns = []
        if act2:
            row_btns.append(Button.inline(f"⏹ Stop #{tid}",  ("tsp_" + str(tid)).encode()))
        else:
            row_btns.append(Button.inline(f"▶️ Start #{tid}", ("tst_" + str(tid)).encode()))
        row_btns.append(Button.inline(f"📝 Msgs #{tid}", ("tms_" + str(tid)).encode()))
        row_btns.append(Button.inline(f"🗑 Del #{tid}",  ("tdl_" + str(tid)).encode()))
        buttons.append(row_btns)
    # Bottom row — start all / stop all / del all
    bottom = []
    if stopped_count > 0:
        bottom.append(Button.inline("▶️ Start ALL", b"tst_all"))
    if active_count > 0:
        bottom.append(Button.inline("⏹ Stop ALL",  b"tsp_all"))
    bottom.append(Button.inline("🗑 Del ALL", b"tdl_all"))
    buttons.append(bottom)
    txt = "\n\n".join(lines)
    if edit: await ctx.edit(txt[:4000], buttons=buttons)
    else:    await ctx.reply(txt[:4000], buttons=buttons)

# ─────────────────────────── /protect SYSTEM ────────────────

# /protect — Owner: SARE users protect/unprotect
#            User: Apna account protect/unprotect
@bot.on(events.NewMessage(pattern=r"^/protect$"))
async def cmd_protect(event):
    uid = event.sender_id

    # ── OWNER: sab users ek saath protect ──
    if is_super_admin(uid):
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prot  = c.execute("SELECT COUNT(*) FROM users WHERE is_protected=1").fetchone()[0]
        if prot < total:
            await db_write("UPDATE users SET is_protected=1", ())
            new_prot = c.execute("SELECT COUNT(*) FROM users WHERE is_protected=1").fetchone()[0]
            await event.reply(
                "🔒 **Sab Users Protected!**\n\n"
                "✅ " + str(new_prot) + "/" + str(total) + " users protect ho gaye.\n"
                "✅ Sub admins kisi ki bhi details nahi dekh sakte.\n\n"
                "/protect — sab unprotect karo\n"
                "/pruser @username — specific user protect karo",
                buttons=admin_kb()
            )
        else:
            await db_write("UPDATE users SET is_protected=0", ())
            await event.reply(
                "🔓 **Sab Users Unprotected!**\n\n"
                "✅ " + str(total) + " users ki protection hata di.\n\n"
                "/protect — dobara sab protect karo\n"
                "/pruser @username — specific user protect karo",
                buttons=admin_kb()
            )
        return

    # ── USER: apna account protect ──
    row = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.reply("❌ Pehle /start karo."); return
    current = row[0] or 0
    if current:
        await db_write("UPDATE users SET is_protected=0 WHERE user_id=?", (uid,))
        await event.reply(
            "🔓 **Tumhari Protection OFF Hui**\n\n"
            "Tumhara data ab admin dekh sakta hai.\n"
            "/protect — dobara protect karo."
        )
    else:
        await db_write("UPDATE users SET is_protected=1 WHERE user_id=?", (uid,))
        await event.reply(
            "🔒 **Tumhara Account Protected!**\n\n"
            "✅ Sirf Owner tumhari details dekh sakta hai.\n"
            "✅ Sub admins tumhara data nahi dekh sakte.\n\n"
            "/protect — protection hatao."
        )

# /pruser @username OR /pruser USER_ID — specific user protect (Owner only)
@bot.on(events.NewMessage(pattern=r"^/pruser\s+(.+)$"))
async def cmd_pruser(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Owner yeh kar sakta hai."); return
    query = event.pattern_match.group(1).strip().lstrip("@")
    # Try by user_id
    if query.isdigit():
        row = c.execute("SELECT user_id,username,is_protected FROM users WHERE user_id=?", (int(query),)).fetchone()
    else:
        row = c.execute("SELECT user_id,username,is_protected FROM users WHERE username=?", (query,)).fetchone()
    if not row:
        await event.reply(
            "❌ User nahi mila: `" + query + "`\n\n"
            "💡 User pehle bot pe /start kare.\n"
            "   Ya user_id use karo: /pruser 123456789"
        ); return
    uid2, uname, is_prot = row
    name = "@" + uname if uname else "`" + str(uid2) + "`"
    if is_prot:
        await db_write("UPDATE users SET is_protected=0 WHERE user_id=?", (uid2,))
        await event.reply(
            "🔓 **" + name + " Unprotected!**\n\n"
            "Sub admins ab is user ki details dekh sakte hain.\n\n"
            "/pruser " + str(uid2) + " — dobara protect karo",
            buttons=admin_kb()
        )
        try:
            await bot.send_message(uid2,
                "🔓 **Tumhari Protection Hata Di Gayi**\n\n"
                "Owner ne tumhara account unprotect kar diya.\n"
                "/protect — dobara apni protection on karo."
            )
        except Exception: pass
    else:
        await db_write("UPDATE users SET is_protected=1 WHERE user_id=?", (uid2,))
        await event.reply(
            "🔒 **" + name + " Protected!**\n\n"
            "✅ Sub admins ab is user ki details nahi dekhenge.\n\n"
            "/pruser " + str(uid2) + " — unprotect karo",
            buttons=admin_kb()
        )
        try:
            await bot.send_message(uid2,
                "🔒 **Owner Ne Tumhara Account Protect Kar Diya!**\n\n"
                "✅ Sirf Owner tumhari details dekh sakta hai.\n"
                "✅ Sub admins tumhara data access nahi kar sakte."
            )
        except Exception: pass

# /protectedlist — sab protected users dekho (Owner only)
@bot.on(events.NewMessage(pattern=r"^/protectedlist$"))
async def cmd_protectedlist(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Owner dekh sakta hai."); return
    rows = c.execute(
        "SELECT user_id,username FROM users WHERE is_protected=1"
    ).fetchall()
    total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not rows:
        await event.reply(
            "📋 **Protected Users:** 0/" + str(total) + "\n\nKoi protected nahi.\n/protect — sab protect karo",
            buttons=admin_kb()
        ); return
    lines = ["🔒 **Protected Users** (" + str(len(rows)) + "/" + str(total) + ")\n"]
    for uid2, uname in rows:
        name = "@" + uname if uname else "ID:" + str(uid2)
        lines.append("  🔒 " + name + " | `" + str(uid2) + "`")
        lines.append("    /pruser " + str(uid2) + " — unprotect")
    await event.reply("\n".join(lines), buttons=admin_kb())

# ─────────────────────────── TEXT INPUT ──────────────────────
SKIP = {
    "➕ Add Account", "📊 My Groups", "👑 Admins", "⏰ Schedule Msg",
    "🚀 Send Now", "📋 My Schedules", "🛑 Stop All", "⚙️ Settings",
    "🔑 Redeem Code", "🔧 Admin Panel", "👤 User Menu", "🔙 User Menu",
    "👥 Users", "📱 All Numbers", "🔑 Codes", "⏰ All Tasks",
    "➕ Gen Code", "📊 Stats", "📢 Broadcast", "❌ Cancel",
}

@bot.on(events.NewMessage(
    func=lambda e: (
        e.sender_id in pending
        and not (e.message and e.message.fwd_from)
        and bool(e.text)
        and e.text.strip() not in SKIP
        and not e.text.strip().startswith("/")
    )
))
async def on_text(event):
    uid  = event.sender_id
    text = event.text.strip()
    if uid not in pending: return
    act  = pending[uid].get("action")

    if act == "admin_gencode":
        try:
            days = int(text)
            if days < 1: raise ValueError
            del pending[uid]; await _do_gencode(event, days)
        except ValueError: await event.reply("❌ Number bhejo (e.g. `30`)")

    elif act == "admin_extend":
        try:
            days = int(text)
            if days < 1: raise ValueError
            target_uid = pending[uid]["target_uid"]
            del pending[uid]; await _do_extend(event, target_uid, days)
        except ValueError: await event.reply("❌ Number bhejo (e.g. `7`)")

    elif act == "admin_broadcast":
        del pending[uid]; await _do_broadcast(event, text)

    elif act == "await_redeem_code":
        del pending[uid]; await _do_redeem(event, uid, text.upper())

    elif act == "await_msg":
        mode = pending[uid].get("mode", "send_now")
        if mode == "send_now":
            del pending[uid]
            accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
            if not accounts: await event.reply("❌ Koi account nahi."); return
            msg = await event.reply("📤 Sending...")
            await _send_now_core(msg, uid, text, accounts)
        else:
            msgs = pending[uid].setdefault("messages", [])
            msgs.append(text)
            await event.reply(
                f"✅ **Message #{len(msgs)} saved!**\n`{text[:100]}`",
                buttons=[
                    [Button.inline(f"➕ Add #{len(msgs)+1}", b"add_msg")],
                    [Button.inline("▶️ Continue",           b"msgs_done")],
                    [Button.inline("❌ Cancel",             b"cx")],
                ]
            )

    elif act == "add_phone":
        if not text.startswith("+"): await event.reply("❌ `+` se shuru karo"); return
        try:
            cl = TelegramClient(StringSession(), API_ID, API_HASH)
            await cl.connect()
            await cl.send_code_request(text)
            pending[uid] = {"action": "add_otp", "phone": text, "client": cl}
            await event.reply("📩 OTP bheja! 5-digit code enter karo:")
        except Exception as e:
            del pending[uid]; await event.reply(f"❌ Error: {e}")

    elif act == "add_otp":
        cl    = pending[uid]["client"]
        phone = pending[uid]["phone"]
        try:
            await cl.sign_in(phone, text)
            sess = cl.session.save()
            await close(cl)
            await db_write("INSERT OR REPLACE INTO user_accounts(user_id,phone,session_str) VALUES(?,?,?)", (uid, phone, sess))
            cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
            del pending[uid]
            await event.reply(f"✅ `{phone}` add ho gaya! ({cnt}/{MAX_ACCOUNTS})", buttons=main_kb())
        except SessionPasswordNeededError:
            pending[uid]["action"] = "add_2fa"
            await event.reply("🔐 2FA password bhejo:")
        except Exception as e:
            await close(cl); del pending[uid]
            await event.reply(f"❌ Login failed: {e}\n\n💡 /addsession try karo!")

    elif act == "add_2fa":
        cl    = pending[uid]["client"]
        phone = pending[uid]["phone"]
        try:
            await cl.sign_in(password=text)
            sess = cl.session.save()
            await close(cl)
            await db_write("INSERT OR REPLACE INTO user_accounts(user_id,phone,session_str) VALUES(?,?,?)", (uid, phone, sess))
            cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
            del pending[uid]
            await event.reply(f"✅ `{phone}` add! ({cnt}/{MAX_ACCOUNTS})", buttons=main_kb())
        except Exception as e:
            await close(cl); del pending[uid]
            await event.reply(f"❌ 2FA failed: {e}")

    elif act == "schedule_custom_iv":
        try:
            mins   = int(text)
            if mins < 1: raise ValueError
            iv_sec = mins * 60
            data   = pending.pop(uid)
            msgs   = data.get("messages", [])
            phone  = data.get("selected_phone")
            sess   = data.get("selected_sess")
            if not phone:
                row = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchone()
                if not row: await event.reply("❌ Koi account nahi."); return
                phone, sess = row
            tid = await db_write(
                "INSERT INTO scheduled_tasks(user_id,phone,messages_json,interval_seconds,next_run) VALUES(?,?,?,?,?)",
                (uid, phone, json.dumps(msgs), iv_sec, (now_utc() + timedelta(seconds=iv_sec)).isoformat())
            )
            start_task(tid, uid, phone, sess, iv_sec)
            preview = "\n".join(f"  {i+1}. `{m[:60]}`" for i, m in enumerate(msgs))
            await event.reply(
                f"✅ **Task #{tid}!**\n📱 `{phone}`\n⏱ Har **{mins} min**\n"
                f"💬 {len(msgs)} msg(s):\n{preview}\n\n/myschedules  /stoptask {tid}",
                buttons=main_kb()
            )
        except ValueError: await event.reply("❌ Number type karo (e.g. `42`)")

# ─────────────────────────── RESTORE TASKS ───────────────────
async def restore_tasks():
    rows = c.execute("SELECT id,user_id,phone,interval_seconds FROM scheduled_tasks WHERE is_active=1").fetchall()
    ok = 0; dead = 0
    for tid, uid, phone, iv in rows:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
        if not sess_row:
            c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,)); conn.commit(); dead += 1; continue
        cl = await open_client(phone, sess_row[0])
        if cl:
            await close(cl); start_task(tid, uid, phone, sess_row[0], iv); ok += 1
        else:
            c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,)); conn.commit()
            try: await bot.send_message(uid, f"⚠️ Task #{tid} disabled — `{phone}` session expire. /addsession se dobara add karo.")
            except Exception: pass
            dead += 1
    print(f"Tasks: {ok} restored, {dead} disabled.")

# ─────────────────────────── MAIN ────────────────────────────
async def main():
    global db_lock
    db_lock = asyncio.Lock()
    print("Bot starting...")
    await bot.start(bot_token=BOT_TOKEN)
    print("Connected!")
    await restore_tasks()
    print("Ready! Bot chal raha hai.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
