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
MAX_ACCOUNTS    = 999  # No limit
MAX_FAILS       = 5
# Auto backup har 6 ghante mein (seconds)
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", str(6 * 3600)))

print(f"Telethon {telethon.__version__} | DB: {DB_FILE}")

# ─────────────────────────── DATABASE ────────────────────────
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c    = conn.cursor()
# Performance optimizations
c.execute("PRAGMA journal_mode=WAL")    # Faster concurrent writes
c.execute("PRAGMA synchronous=NORMAL")  # Balance speed vs safety
c.execute("PRAGMA cache_size=10000")    # 10MB cache
c.execute("PRAGMA temp_store=MEMORY")   # Temp tables in RAM
conn.commit()
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
c.execute("""CREATE TABLE IF NOT EXISTS logs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    admin_id   INTEGER,
    admin_name TEXT,
    code       TEXT    DEFAULT '',
    details    TEXT    DEFAULT '',
    created_at TEXT    DEFAULT CURRENT_TIMESTAMP)""")
# Add columns if not exists
try:
    c.execute("ALTER TABLE users ADD COLUMN is_protected INTEGER DEFAULT 0")
except Exception: pass
try:
    c.execute("ALTER TABLE access_codes ADD COLUMN created_by INTEGER DEFAULT NULL")
except Exception: pass
try:
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN msg_ids_json TEXT DEFAULT '[]'")
except Exception: pass
try:
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN source_chat_id INTEGER DEFAULT NULL")
except Exception: pass
try:
    # custom_targets: JSON list of @usernames or invite links to add
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN custom_targets TEXT DEFAULT '[]'")
except Exception: pass
try:
    # send_to: "all" | "groups" | "channels"
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN send_to TEXT DEFAULT 'all'")
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

async def log_event(event_type, admin_id, admin_name, code="", details=""):
    """Log important events: code_created, code_approved, code_claimed"""
    await db_write(
        "INSERT INTO logs(event_type,admin_id,admin_name,code,details) VALUES(?,?,?,?,?)",
        (event_type, admin_id, admin_name, code, details)
    )

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
        [Button.text("⚙️ Settings"),     Button.text("🔑 Redeem Code")],
        [Button.text("💬 Buy Access")],
    ]

def admin_kb(uid=None):
    # Sub Admin keyboard — Pending aur Logs nahi dikhte
    sub_kb = [
        [Button.text("👥 Users"),        Button.text("📱 All Numbers")],
        [Button.text("🔑 Codes"),        Button.text("⏰ All Tasks")],
        [Button.text("➕ Gen Code"),     Button.text("📊 Stats")],
        [Button.text("📢 Broadcast"),    Button.text("👑 Admins")],
        [Button.text("📋 My Requests"),  Button.text("🔙 User Menu")],
    ]
    # Owner keyboard — Pending + Logs with count
    pending_cnt = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    pending_btn = "📋 Pending (" + str(pending_cnt) + ")" if pending_cnt > 0 else "📋 Pending"
    owner_kb = [
        [Button.text("👥 Users"),        Button.text("📱 All Numbers")],
        [Button.text("🔑 Codes"),        Button.text("⏰ All Tasks")],
        [Button.text("➕ Gen Code"),     Button.text("📊 Stats")],
        [Button.text("📢 Broadcast"),    Button.text("👑 Admins")],
        [Button.text(pending_btn),       Button.text("📜 Logs")],
        [Button.text("🔙 User Menu")],
    ]
    # uid pass hua hai to check karo, warna default owner kb return karo
    if uid is not None:
        return owner_kb if is_super_admin(uid) else sub_kb
    return owner_kb

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
async def run_task(task_id, uid, phone, sess, interval, initial_delay=0):
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    cached_groups = None        # Cache dialogs — refresh every 30 min
    last_dialog_fetch = 0
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
                import time as _time
                now_ts = _time.time()
                if cached_groups is None or (now_ts - last_dialog_fetch) > 1800:
                    dialogs       = await cl.get_dialogs(limit=500)
                    cached_groups = [d for d in dialogs if d.is_group or d.is_channel]
                    last_dialog_fetch = now_ts
                groups = cached_groups
                sent   = 0

                # Get forward pairs + entities
                task_row2 = c.execute(
                    "SELECT msg_ids_json FROM scheduled_tasks WHERE id=?", (task_id,)
                ).fetchone()
                fwd_data  = {}
                try:
                    fwd_data = json.loads(task_row2[0] or "{}") if task_row2 else {}
                except: pass
                fwd_pairs2 = fwd_data.get("pairs", [])
                ents_all   = fwd_data.get("ents", [])
                ents_json2 = ents_all[idx % len(ents_all)] if ents_all else None
                fwd_pair   = fwd_pairs2[idx % len(fwd_pairs2)] if fwd_pairs2 else None
                orig_mid   = fwd_pair[0] if fwd_pair and len(fwd_pair) > 0 else None
                orig_peer2 = fwd_pair[1] if fwd_pair and len(fwd_pair) > 1 else None

                # Rebuild Telethon entities from JSON
                from telethon.tl.types import (
                    MessageEntityBold, MessageEntityItalic, MessageEntityCode,
                    MessageEntityPre, MessageEntityTextUrl, MessageEntityUnderline,
                    MessageEntityStrike, MessageEntityBlockquote, MessageEntityCustomEmoji,
                    MessageEntitySpoiler
                )
                ENTITY_MAP = {
                    "MessageEntityBold":        MessageEntityBold,
                    "MessageEntityItalic":      MessageEntityItalic,
                    "MessageEntityCode":        MessageEntityCode,
                    "MessageEntityPre":         MessageEntityPre,
                    "MessageEntityUnderline":   MessageEntityUnderline,
                    "MessageEntityStrike":      MessageEntityStrike,
                    "MessageEntityBlockquote":  MessageEntityBlockquote,
                    "MessageEntitySpoiler":     MessageEntitySpoiler,
                    "MessageEntityTextUrl":     MessageEntityTextUrl,
                    "MessageEntityCustomEmoji": MessageEntityCustomEmoji,
                }

                def rebuild_entities(ej):
                    if not ej: return None
                    try:
                        elist = json.loads(ej) if isinstance(ej, str) else ej
                        result = []
                        for ed in elist:
                            cls = ENTITY_MAP.get(ed.get("type"))
                            if not cls: continue
                            d = ed.get("data")
                            if ed["type"] == "MessageEntityTextUrl" and d:
                                result.append(cls(offset=ed["offset"], length=ed["length"], url=d))
                            elif ed["type"] == "MessageEntityPre" and d:
                                result.append(cls(offset=ed["offset"], length=ed["length"], language=d))
                            else:
                                try: result.append(cls(offset=ed["offset"], length=ed["length"]))
                                except: pass
                        return result if result else None
                    except: return None

                entities_to_use = rebuild_entities(ents_json2)

                all_targets = [g.entity for g in groups]

                for target in all_targets:
                    try:
                        if orig_mid and orig_peer2:
                            # ✅ Forward from original source — exact format!
                            await cl.forward_messages(target, orig_mid, orig_peer2)
                        elif entities_to_use:
                            # ✅ Send with entities — bold/quote preserved
                            await cl.send_message(target, msg, formatting_entities=entities_to_use)
                        else:
                            await cl.send_message(target, msg)
                        sent += 1
                        await asyncio.sleep(0.3)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds + 5)
                    except Exception:
                        try:
                            await cl.send_message(target, msg)
                            sent += 1
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

def start_task(tid, uid, phone, sess, interval, initial_delay=0):
    t = asyncio.create_task(run_task(tid, uid, phone, sess, interval, initial_delay))
    scheduler_tasks[tid] = t

# ─────────────────────────── /start ──────────────────────────
@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    uid   = event.sender_id
    uname = getattr(event.sender, "username", "") or ""
    upsert_user(uid, uname)

    if is_super_admin(uid):
        await event.reply(
            "👑 **Welcome Owner!**\n\n"
            "🔑 Tumhare paas full control hai.\n"
            "/help — saari commands",
            buttons=admin_kb(uid)
        ); return

    if is_admin(uid):
        await event.reply(
            "🔰 **Welcome Admin!**\n\n"
            "✅ Tum Sub Admin ho.\n"
            "/help — saari commands",
            buttons=admin_kb(uid)
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
            "  📩 Contact: @V4_XTRD"
        )
        await send_welcome(event, caption, main_kb())

# ─────────────────────────── /help ───────────────────────────
@bot.on(events.NewMessage(pattern=r"^/help$"))
async def cmd_help(event):
    uid = event.sender_id

    # ── OWNER HELP ──
    if is_super_admin(uid):
        msg = (
            "👑 **OWNER COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**👥 User Management:**\n"
            "/users — All users list\n"
            "/userinfo ID — User details\n"
            "/ban ID — Ban user\n"
            "/unban ID — Unban user\n"
            "/removeuser ID — Delete user\n"
            "/endtrial ID — Trial khatam karo\n\n"
            "**👑 Admin Management:**\n"
            "/addadmin ID — New admin add karo\n"
            "/removeadmin ID — Admin hatao\n"
            "/admins — Admin list\n"
            "/adminstats — Admin wise coupon stats\n\n"
            "**🔑 Coupon / Code:**\n"
            "/gencode DAYS — Direct code generate\n"
            "/extend ID DAYS — Access extend karo\n"
            "/revoke CODE — Code revoke karo\n"
            "/codes — All codes (by admin)\n"
            "/pending — Pending approval requests\n"
            "/logs — Activity logs\n\n"
            "**📊 Stats & Tasks:**\n"
            "/stats — Bot statistics\n"
            "/tasks — All tasks\n"
            "/adminstoptask ID — Task stop karo\n"
            "/adminstarttask ID — Task start karo\n"
            "/admindeltask ID — Task delete karo\n"
            "/usergroups ID — User ke groups dekho\n\n"
            "**📱 Numbers:**\n"
            "/numbers — All numbers\n"
            "/removenum +phone — Number remove karo\n\n"
            "**🔒 Protection:**\n"
            "/protect — Sab users protect/unprotect\n"
            "/pruser @username — Specific user protect\n"
            "/protectedlist — Protected users list\n\n"
            "**📢 Messaging:**\n"
            "/sendmsg ID text — User ko message bhejo\n"
            "/broadcast text — Sab ko broadcast karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🔧 General:**\n"
            "/admin — Admin panel\n"
            "/start /help /cancel /myid /status\n"
            "/buy — Admin list show karo\n"
        "\n🗄 **BACKUP & RESTORE:**\n"
        "/backup — Cloud backup bhejo (Saved Msgs)\n"
        "/restoredb — Backups list ya restore karo\n"
        "/backupstatus — Backup system info\n"
        "/gitsync — GitHub pe DB push karo\n"
        "/syncstatus — GitHub sync status\n"
    )
        await event.reply(msg, buttons=admin_kb(event.sender_id)); return

    # ── SUB ADMIN HELP ──
    if is_admin(uid):
        msg = (
            "🔰 **SUB ADMIN COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🔑 Coupon:**\n"
            "/gencode DAYS — Code request karo (Owner approve karega)\n"
            "/approval — Apni requests aur status dekho\n"
            "/codes — Apne approved codes dekho\n\n"
            "**👥 User Management:**\n"
            "/users — Users list dekho\n"
            "/userinfo ID — User details\n"
            "/ban ID — Ban user\n"
            "/unban ID — Unban user\n"
            "/endtrial ID — Trial khatam karo\n"
            "/numbers — Phone numbers\n\n"
            "**📊 Tasks & Stats:**\n"
            "/stats — Bot stats\n"
            "/tasks — All tasks\n"
            "/adminstoptask ID — Task stop karo\n"
            "/adminstarttask ID — Task start karo\n"
            "/usergroups ID — User ke groups\n\n"
            "**📢 Messaging:**\n"
            "/sendmsg ID text — User ko message\n"
            "/broadcast text — Broadcast karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🔧 General:**\n"
            "/admin — Admin panel\n"
            "/start /help /cancel /myid /status\n"
            "/buy — Admin list show karo\n"
        "\n🗄 **BACKUP & RESTORE:**\n"
        "/backup — Cloud backup bhejo (Saved Msgs)\n"
        "/restoredb — Backups list ya restore karo\n"
        "/backupstatus — Backup system info\n"
        "/gitsync — GitHub pe DB push karo\n"
        "/syncstatus — GitHub sync status\n"
    )
        await event.reply(msg, buttons=admin_kb(event.sender_id)); return

    # ── NORMAL USER HELP ──
    msg = (
        "📖 **USER COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🔐 Access:**\n"
        "/start — Bot start karo\n"
        "/status — Apna access status dekho\n"
        "/redeem CODE — Access code lagao\n"
        "/buy — Coupon/Code ke liye admin se contact karo\n\n"
        "**📱 Account:**\n"
        "/addaccount — Phone se account add karo\n"
        "/removeaccount +phone — Account hatao\n"
        "/mygroups — Apne groups dekho\n\n"
        "**📤 Messaging:**\n"
        "/sendnow message — Abhi sab groups mein bhejo\n"
        "/schedule — Auto schedule banao\n"
        "/myschedules — Apne schedules dekho\n\n"
        "**⏰ Tasks:**\n"
        "/starttask ID — Task start karo\n"
        "/stoptask ID — Task stop karo\n"
        "/deltask ID — Task delete karo\n"
        "/stopall — Sab tasks stop karo\n\n"
        "**⚙️ Other:**\n"
        "/settings — Settings dekho\n"
        "/protect — Apna account protect karo\n"
        "/myid — Apna Telegram ID dekho\n"
        "/cancel — Cancel karo\n"
        "/help — Yeh menu\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await event.reply(msg, buttons=main_kb())

# ─────────────────────────── /cancel ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
        await event.reply("✅ Cancel ho gaya.", buttons=main_kb(), parse_mode='md')
    else:
        await event.reply("Kuch cancel nahi tha.", buttons=main_kb(), parse_mode='md')

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
        await event.reply("❌ **Status: No Access**\n/redeem CODE karo.", parse_mode='md')

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
    # No account limit
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
    await event.reply("📝 **Message #1 type karo** (ya forward karo):\n\nMultiple messages add kar sakte ho.\n/cancel se wapas.", parse_mode='md')

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
    await event.reply("▶️ **Task #" + str(tid) + " Start Ho Gaya!**", buttons=main_kb(), parse_mode='md')

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
    await event.reply("\n".join(lines), buttons=main_kb(), parse_mode='md')

# ─────────────────────────── ADMIN COMMANDS ──────────────────
@bot.on(events.NewMessage(pattern=r"^/addadmin\s+(\d+)$"))
async def cmd_addadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    if uid == ADMIN_ID:
        await event.reply("⚠️ Yeh already Super Admin hai."); return
    # Try to get username from DB first, then from Telegram
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = urow[0] if urow and urow[0] else ""
    if not uname:
        try:
            tg_user = await bot.get_entity(uid)
            uname = tg_user.username or ""
        except Exception: pass
    # Save/update user in users table too
    if not c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone():
        c.execute("INSERT OR IGNORE INTO users(user_id,username) VALUES(?,?)", (uid, uname))
        conn.commit()
    else:
        if uname:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
            conn.commit()
    await db_write("INSERT OR REPLACE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
        (uid, uname, event.sender_id, now_iso()))
    name = "@" + uname if uname else "`" + str(uid) + "`"
    msg = (
        "✅ **" + name + " Admin ban gaya!**\n\n"
        "🆔 ID: `" + str(uid) + "`\n"
        "👤 Username: " + ("@" + uname if uname else "—") + "\n"
        "⚙️ Permissions:\n"
        "  ✅ Admin panel use kar sakta hai\n"
        "  ✅ Coupon request kar sakta hai (Owner approve karega)\n"
        "  ✅ Apne codes dekh sakta hai\n"
        "  ❌ Naye admin nahi bana sakta\n\n"
        "/removeadmin " + str(uid) + " — hatane ke liye"
    )
    await event.reply(msg, buttons=admin_kb(event.sender_id))
    # Notify the new admin
    try:
        await bot.send_message(uid,
            "🎉 **Tumhe Admin Banaya Gaya!**\n\n"
            "👑 Bot: @V4_XTRD_bot\n"
            "🔰 Role: Sub Admin\n\n"
            "Ab tum /admin se admin panel access kar sakte ho.\n"
            "/help se saari commands dekho."
        )
    except Exception: pass

@bot.on(events.NewMessage(pattern=r"^/buy$"))
async def cmd_buy(event):
    # Build dynamic admin list from DB
    rows = c.execute("SELECT user_id, username FROM admins").fetchall()
    # Add owner info
    owner_row = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
    owner_name = owner_row[0] if owner_row and owner_row[0] else None

    lines = [
        "💬 **DM Any Admin For Coupon / Best Price**\n",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "👑 **Owner:**",
        "  • " + ("@" + owner_name if owner_name else "`" + str(ADMIN_ID) + "`"),
    ]
    if rows:
        lines.append("\n🔰 **Admins:**")
        for uid2, uname in rows:
            if uname:
                lines.append("  • @" + uname)
            else:
                lines.append("  • `" + str(uid2) + "`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 _Admin ko DM karo aur best deal pao!_")
    await event.reply("\n".join(lines), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/removeadmin\s+(\d+)$"))
async def cmd_removeadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM admins WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await event.reply(f"❌ `{uid}` admin nahi hai."); return
    await db_write("DELETE FROM admins WHERE user_id=?", (uid,))
    await event.reply(f"🗑 `{uid}` admin se remove ho gaya.", buttons=admin_kb(event.sender_id))

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
    await event.reply(out, buttons=admin_kb(event.sender_id))




@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/admin", "🔧 Admin Panel"]))
async def cmd_admin(event):
    if not is_admin(event.sender_id): return
    await event.reply("👑 **Admin Panel**", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["👤 User Menu", "🔙 User Menu"]))
async def cmd_usermenu(event):
    await event.reply("👤 **User Menu**", buttons=main_kb(), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/stats", "📊 Stats"]))
async def cmd_stats(event):
    if not is_admin(event.sender_id): return
    total  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    trials = c.execute("SELECT COUNT(*) FROM users WHERE trial_granted=1 AND trial_expires>?", (now_iso(),)).fetchone()[0]
    phones = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
    codes  = c.execute("SELECT COUNT(*) FROM access_codes").fetchone()[0]
    actc   = c.execute("SELECT COUNT(*) FROM access_codes WHERE is_active=1 AND expires_at>?", (now_iso(),)).fetchone()[0]
    claimed= c.execute("SELECT COUNT(*) FROM access_codes WHERE claimed_by IS NOT NULL").fetchone()[0]
    tasks  = c.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE is_active=1").fetchone()[0]
    pending_req = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    admins_cnt  = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]

    out = (
        "📊 **Bot Statistics**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👥 Total Users: `" + str(total) + "` | 🚫 Banned: `" + str(banned) + "`\n"
        "🎁 Active Trials: `" + str(trials) + "`\n"
        "📱 Numbers: `" + str(phones) + "`\n"
        "⏰ Running Tasks: `" + str(tasks) + "`\n"
        "👑 Sub Admins: `" + str(admins_cnt) + "`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 **Coupon Stats:**\n"
        "  Total: `" + str(codes) + "` | Active: `" + str(actc) + "`\n"
        "  ✅ Claimed: `" + str(claimed) + "` | 🟢 Unclaimed: `" + str(actc - claimed) + "`\n"
        "  ⏳ Pending Approval: `" + str(pending_req) + "`\n"
    )

    # Admin coupon breakdown (owner only)
    if is_super_admin(event.sender_id):
        out += "━━━━━━━━━━━━━━━━━━━━━━\n"
        out += "📋 **Coupons By Admin:**\n"
        # Owner codes
        owner_cnt = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (ADMIN_ID,)).fetchone()[0]
        owner_row = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
        owner_uname = owner_row[0] if owner_row and owner_row[0] else ""
        out += "  👑 " + ("@" + owner_uname if owner_uname else "Owner") + " | ID: `" + str(ADMIN_ID) + "`\n"
        out += "     Total Coupons Created: **" + str(owner_cnt) + "**\n\n"
        # Sub admin codes
        admin_rows = c.execute("SELECT user_id,username FROM admins").fetchall()
        for aid, auname in admin_rows:
            cnt  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (aid,)).fetchone()[0]
            clm  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (aid,)).fetchone()[0]
            pend = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='pending'", (aid,)).fetchone()[0]
            name = "@" + auname if auname else "ID:" + str(aid)
            out += "  🔰 " + name + " | ID: `" + str(aid) + "`\n"
            out += "     Total Created: **" + str(cnt) + "** | Claimed: **" + str(clm) + "** | Pending: **" + str(pend) + "**\n\n"

    await event.reply(out[:4000], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/logs", "📜 Logs"]))
async def cmd_logs(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute(
        "SELECT event_type,admin_name,admin_id,code,details,created_at FROM logs ORDER BY id DESC LIMIT 30"
    ).fetchall()
    if not rows:
        await event.reply("📜 **Logs**\n\nKoi log nahi abhi tak.", buttons=admin_kb(event.sender_id)); return
    icons = {"code_created": "🆕", "code_approved": "✅", "code_claimed": "🔑", "code_rejected": "❌"}
    lines = ["📜 **Recent Logs** (last 30)\n"]
    for etype, aname, aid, code, details, created in rows:
        icon = icons.get(etype, "📌")
        date = (created or "").replace("T", " ").split(".")[0]
        lines.append(
            icon + " **" + etype.replace("_", " ").title() + "**\n"
            "   👤 " + (aname or "—") + " | `" + str(aid) + "`\n"
            "   🔑 `" + (code or "—") + "`\n"
            "   📝 " + (details or "—") + "\n"
            "   🕐 " + date
        )
    await event.reply("\n\n".join(lines)[:4000], buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/adminstats$"))
async def cmd_adminstats(event):
    if not is_super_admin(event.sender_id): return
    out = "📋 **Admin Coupon Statistics**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    # Owner stats
    o_total   = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (ADMIN_ID,)).fetchone()[0]
    o_claimed = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (ADMIN_ID,)).fetchone()[0]
    o_active  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND is_active=1 AND expires_at>?", (ADMIN_ID, now_iso())).fetchone()[0]
    orow = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
    oname = "@" + orow[0] if orow and orow[0] else "Owner"
    out += (
        "👑 **Admin:** " + oname + "\n"
        "   🆔 Admin ID: `" + str(ADMIN_ID) + "`\n"
        "   🔑 Total Coupons Created: **" + str(o_total) + "**\n"
        "   ✅ Claimed: **" + str(o_claimed) + "** | 🟢 Unclaimed: **" + str(o_active - o_claimed) + "**\n\n"
    )
    # Sub admin stats
    admin_rows = c.execute("SELECT user_id,username FROM admins").fetchall()
    if admin_rows:
        for aid, auname in admin_rows:
            total_req  = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=?", (aid,)).fetchone()[0]
            approved   = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='approved'", (aid,)).fetchone()[0]
            pending    = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='pending'", (aid,)).fetchone()[0]
            rejected   = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='rejected'", (aid,)).fetchone()[0]
            ac_claimed = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (aid,)).fetchone()[0]
            ac_active  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND is_active=1 AND expires_at>?", (aid, now_iso())).fetchone()[0]
            name = "@" + auname if auname else "ID:" + str(aid)
            out += (
                "🔰 **Admin:** " + name + "\n"
                "   🆔 Admin ID: `" + str(aid) + "`\n"
                "   📨 Total Requests: **" + str(total_req) + "**\n"
                "   ✅ Approved: **" + str(approved) + "** | ❌ Rejected: **" + str(rejected) + "** | ⏳ Pending: **" + str(pending) + "**\n"
                "   🔑 Active Codes: **" + str(ac_active) + "** | Claimed: **" + str(ac_claimed) + "**\n\n"
            )
    else:
        out += "🔰 Koi sub admin nahi.\n"
    out += "━━━━━━━━━━━━━━━━━━━━━━"
    await event.reply(out[:4000], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/users", "👥 Users"]))
async def cmd_users(event):
    if not is_admin(event.sender_id): return
    rows = c.execute(
        "SELECT user_id,username,trial_granted,trial_expires,is_banned FROM users ORDER BY rowid DESC LIMIT 20"
    ).fetchall()
    if not rows: await event.reply("Koi user nahi.", buttons=admin_kb(event.sender_id)); return
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
    await event.reply("\n".join(lines), buttons=buttons, parse_mode='md')

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
        "📱 Accounts (" + str(len(phones)) + "):\n"
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
        await ctx.edit(out, buttons=btns, parse_mode='md')
    except Exception:
        try:
            await ctx.respond(out, buttons=btns, parse_mode='md')
        except Exception:
            try:
                await bot.send_message(
                    ctx.sender_id if hasattr(ctx, 'sender_id') else ctx.chat_id,
                    out, buttons=btns, parse_mode='md'
                )
            except Exception as e:
                print(f"userinfo error: {e}")

@bot.on(events.NewMessage(pattern=r"^/ban\s+(\d+)$"))
async def cmd_ban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await event.reply(f"🚫 `{uid}` banned.", buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/unban\s+(\d+)$"))
async def cmd_unban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await event.reply(f"✅ `{uid}` unbanned.", buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/removeuser\s+(\d+)$"))
async def cmd_removeuser(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    _del_user(uid)
    await event.reply(f"🗑 User `{uid}` deleted.", buttons=admin_kb(event.sender_id))

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
    # requester_id se uid decide karo
    uid = requester_id or ADMIN_ID
    # Super admin — direct generate
    if is_super_admin(uid):
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
            (code, days, now_iso(), expires, uid))
        _urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        _uname = _urow[0] if _urow and _urow[0] else ""
        await log_event("code_created", uid, "@" + _uname if _uname else str(uid), code, str(days) + " days")
        await ctx.reply(
            "✅ **Code Generate Hua!**\n\n🔑 `" + code + "`\n📅 " + str(days) + " din\n⏳ " + expires.split("T")[0] + "\n\n/redeem " + code,
            buttons=admin_kb(uid)
        )
    else:
        # Sub admin — send approval request to owner
        req_id = await db_write(
            "INSERT INTO code_requests(requested_by,days) VALUES(?,?)",
            (uid, days)
        )
        urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        uname = urow[0] if urow and urow[0] else ""
        name  = "@" + uname if uname else "`" + str(uid) + "`"
        await log_event("code_created", uid, name, "", str(days) + " days (pending approval)")
        await ctx.reply(
            "⏳ **Request bheji gayi!**\n\nOwner verify karega tab code milega.",
            buttons=admin_kb(uid)
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

# ── /pending — show all pending requests ──────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and (
    e.text.strip().startswith("📋 Pending") or e.text.strip() == "/pending"
)))
async def cmd_pending(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute(
        "SELECT cr.id, cr.requested_by, u.username, cr.days, cr.requested_at "
        "FROM code_requests cr LEFT JOIN users u ON cr.requested_by=u.user_id "
        "WHERE cr.status='pending' ORDER BY cr.id ASC"
    ).fetchall()
    if not rows:
        await event.reply("📋 **Pending Requests**\n\nKoi pending request nahi hai! ✅", buttons=admin_kb(event.sender_id)); return
    lines2  = ["📋 **Pending Code Requests** (" + str(len(rows)) + ")\n"]
    buttons = []
    for req_id, req_by, uname, days, req_at in rows:
        name = "@" + uname if uname else "ID:" + str(req_by)
        date = (req_at or "").split("T")[0]
        lines2.append(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 **Request #" + str(req_id) + "**\n"
            "👤 Admin: " + name + " | `" + str(req_by) + "`\n"
            "📅 Days: **" + str(days) + "**\n"
            "🕐 Date: " + date
        )
        buttons.append([
            Button.inline("✅ Approve #" + str(req_id), ("creq_ok_" + str(req_id)).encode()),
            Button.inline("❌ Reject #" + str(req_id),  ("creq_no_" + str(req_id)).encode()),
        ])
    buttons.append([
        Button.inline("✅ Approve ALL", b"creq_all_ok"),
        Button.inline("❌ Reject ALL",  b"creq_all_no"),
    ])
    await event.reply("\n\n".join(lines2), buttons=buttons, parse_mode='md')

# Approve ALL
@bot.on(events.CallbackQuery(data=b"creq_all_ok"))
async def cb_creq_all_ok(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute("SELECT id,requested_by,days FROM code_requests WHERE status='pending'").fetchall()
    if not rows: await event.answer("Koi pending nahi.", alert=True); return
    done = 0
    for req_id, requester, days in rows:
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
            (code, days, now_iso(), expires, requester))
        await db_write("UPDATE code_requests SET status=?,code=? WHERE id=?", ("approved", code, req_id))
        try:
            await bot.send_message(requester,
                "✅ **Code Approved By Owner!**\n\n"
                "🔑 `" + code + "`\n"
                "📅 " + str(days) + " din\n"
                "⏳ " + expires.split("T")[0] + "\n\n"
                "/redeem " + code
            )
        except Exception: pass
        done += 1
    await event.edit("✅ **" + str(done) + " requests approve ho gayi!**", buttons=admin_kb(event.sender_id), parse_mode='md')

# Reject ALL
@bot.on(events.CallbackQuery(data=b"creq_all_no"))
async def cb_creq_all_no(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute("SELECT id,requested_by FROM code_requests WHERE status='pending'").fetchall()
    if not rows: await event.answer("Koi pending nahi.", alert=True); return
    for req_id, requester in rows:
        await db_write("UPDATE code_requests SET status='rejected' WHERE id=?", (req_id,))
        try:
            await bot.send_message(requester, "❌ **Code Request Reject Ho Gayi.**\nOwner ne approve nahi kiya.")
        except Exception: pass
    await event.edit("❌ **" + str(len(rows)) + " requests reject ho gayi.**", buttons=admin_kb(event.sender_id), parse_mode='md')

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
    await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
        (code, days, now_iso(), expires, requester))
    await db_write("UPDATE code_requests SET status=?,code=? WHERE id=?", ("approved", code, req_id))
    # Log approval
    _urow2 = c.execute("SELECT username FROM users WHERE user_id=?", (requester,)).fetchone()
    _un2   = _urow2[0] if _urow2 and _urow2[0] else ""
    await log_event("code_approved", ADMIN_ID, "Owner", code, "Approved for " + ("@" + _un2 if _un2 else str(requester)) + " | " + str(days) + " days")
    remaining = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    await event.edit(
        "✅ **Approved!**\n\n"
        "🔑 `" + code + "`\n"
        "📅 " + str(days) + " din\n"
        "⏳ " + expires.split("T")[0] + "\n\n"
        "📋 Remaining pending: " + str(remaining)
    )
    try:
        await bot.send_message(requester,
            "✅ **Code Approved By Owner!**\n\n"
            "🔑 `" + code + "`\n"
            "📅 " + str(days) + " din\n"
            "⏳ " + expires.split("T")[0] + "\n\n"
            "/redeem " + code
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
    _urow3 = c.execute("SELECT username FROM users WHERE user_id=?", (requester,)).fetchone()
    _un3   = _urow3[0] if _urow3 and _urow3[0] else ""
    await log_event("code_rejected", ADMIN_ID, "Owner", "", "Rejected request from " + ("@" + _un3 if _un3 else str(requester)) + " | " + str(days) + " days")
    remaining = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    await event.edit("❌ **Rejected.**\n\n📋 Remaining pending: " + str(remaining), parse_mode='md')
    try:
        await bot.send_message(requester, "❌ **Code Request Reject Ho Gayi.**\nOwner ne approve nahi kiya.")
    except Exception: pass

# ─────────────────────────── ADMIN COMMANDS PART 2 ──────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/codes", "🔑 Codes"]))
async def cmd_codes(event):
    if not is_admin(event.sender_id): return
    uid = event.sender_id
    if not is_super_admin(uid):
        # Sub admin: only own approved codes
        rows = c.execute(
            "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC", (uid,)
        ).fetchall()
        if not rows:
            await event.reply("🔑 **Tumhare Codes**\n\nAbhi tak koi code approve nahi hua.", buttons=admin_kb(event.sender_id)); return
        lines2 = ["🔑 **Tumhare Approved Codes** (" + str(len(rows)) + ")\n"]
        for code, days, cb, exp, active in rows:
            expired = now_utc() > parse_iso(exp)
            if not active:    st = "🚫 Revoked"
            elif expired:     st = "⌛ Expired"
            elif cb:          st = "✅ Claimed"
            else:             st = "🟢 Unclaimed"
            urow = c.execute("SELECT username FROM users WHERE user_id=?", (cb,)).fetchone() if cb else None
            claimant = ("@" + urow[0] if urow and urow[0] else "`" + str(cb) + "`") if cb else "—"
            lines2.append("━━━━━━━━━━━━\n" + st + " `" + code + "`\n📅 " + str(days) + "d | ⏳ " + exp.split("T")[0] + "\n👤 " + claimant)
        await event.reply("\n\n".join(lines2), buttons=admin_kb(event.sender_id)); return
    # Owner: codes grouped by admin
    all_sections = [(ADMIN_ID, "👑 Owner (You)")]
    for aid, auname in c.execute("SELECT user_id,username FROM admins").fetchall():
        all_sections.append((aid, "🔰 @" + auname if auname else "🔰 ID:" + str(aid)))
    full_text = "🔑 **All Codes By Admin**\n\n"
    buttons   = []
    for admin_id, admin_name in all_sections:
        rows = c.execute(
            "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC LIMIT 15", (admin_id,)
        ).fetchall()
        if not rows: continue
        full_text += "━━━━━━━━━━━━━━━━━━━━━━\n" + admin_name + "  (" + str(len(rows)) + " codes)\n\n"
        for code, days, cb, exp, active in rows:
            expired = now_utc() > parse_iso(exp)
            if not active:  st = "🚫"
            elif expired:   st = "⌛"
            elif cb:        st = "✅ Claimed"
            else:           st = "🟢 Unclaimed"
            full_text += st + " `" + code + "` | " + str(days) + "d | " + exp.split("T")[0] + "\n"
            if active and not expired:
                buttons.append([Button.inline("🚫 Revoke " + code, ("rev_" + code).encode())])
        full_text += "\n"
    if full_text == "🔑 **All Codes By Admin**\n\n":
        await event.reply("Koi code nahi.", buttons=admin_kb(event.sender_id)); return
    await event.reply(full_text[:4000], buttons=buttons or None)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📋 My Requests"))
async def btn_my_requests(event):
    await cmd_approval(event)

@bot.on(events.NewMessage(pattern=r"^/approval$"))
async def cmd_approval(event):
    uid = event.sender_id
    if not is_admin(uid): return
    if is_super_admin(uid):
        await cmd_pending(event); return

    # ── SUB ADMIN: Full dashboard ──
    urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = urow[0] if urow and urow[0] else str(uid)

    # Code requests
    reqs = c.execute(
        "SELECT id,days,status,code,requested_at FROM code_requests WHERE requested_by=? ORDER BY id DESC", (uid,)
    ).fetchall()

    # Own codes from access_codes
    codes = c.execute(
        "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC", (uid,)
    ).fetchall()

    # Own logs
    logs = c.execute(
        "SELECT event_type,code,details,created_at FROM logs WHERE admin_id=? ORDER BY id DESC LIMIT 10", (uid,)
    ).fetchall()

    p  = sum(1 for r in reqs if r[2]=="pending")
    a  = sum(1 for r in reqs if r[2]=="approved")
    rj = sum(1 for r in reqs if r[2]=="rejected")

    out = (
        "📋 **My Panel — @" + uname + "**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    # ── SECTION 1: Pending/Approval Requests ──
    out += "⏳ **Code Requests** — Pending: **" + str(p) + "** | ✅ " + str(a) + " | ❌ " + str(rj) + "\n\n"
    if reqs:
        for req_id, days, status, code, req_at in reqs:
            date = (req_at or "").split("T")[0]
            if status == "pending":
                st = "⏳ PENDING"
            elif status == "approved":
                st = "✅ APPROVED"
            else:
                st = "❌ REJECTED"
            out += "━━━━━━━━━━━━\n"
            out += st + "  #" + str(req_id) + "  |  📅 " + str(days) + " din  |  🕐 " + date + "\n"
            if status == "pending":
                out += "   ⏳ Owner approval ka wait kar raha hai...\n"
            elif status == "approved" and code:
                claimed_row = c.execute("SELECT claimed_by FROM access_codes WHERE code=?", (code,)).fetchone()
                clm = "✅ Claimed" if (claimed_row and claimed_row[0]) else "🟢 Unclaimed"
                out += "   🔑 Code: `" + code + "`\n"
                out += "   " + clm + "\n"
            elif status == "rejected":
                out += "   ❌ Owner ne reject kar diya.\n"
    else:
        out += "   Koi request nahi abhi tak. /gencode se bhejo.\n"

    # ── SECTION 2: My Codes ──
    out += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "🔑 **My Codes** (" + str(len(codes)) + ")\n\n"
    if codes:
        for code, days, cb, exp, active in codes:
            expired = now_utc() > parse_iso(exp)
            if not active:   st = "🚫 Revoked"
            elif expired:    st = "⌛ Expired"
            elif cb:         st = "✅ Claimed"
            else:            st = "🟢 Unclaimed"
            urow2 = c.execute("SELECT username FROM users WHERE user_id=?", (cb,)).fetchone() if cb else None
            claimant = ("@" + urow2[0] if urow2 and urow2[0] else str(cb)) if cb else "—"
            out += st + "  `" + code + "`  |  " + str(days) + "d  |  ⏳" + exp.split("T")[0] + "\n"
            if cb:
                out += "   👤 Claimed by: " + claimant + "\n"
    else:
        out += "   Koi code nahi. Pehle gencode request karo.\n"

    # ── SECTION 3: Recent Activity Logs ──
    out += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "📜 **My Recent Activity** (last 10)\n\n"
    icons = {"code_created": "🆕", "code_approved": "✅", "code_claimed": "🔑", "code_rejected": "❌"}
    if logs:
        for etype, lcode, details, created in logs:
            icon = icons.get(etype, "📌")
            date2 = (created or "").replace("T", " ").split(".")[0]
            out += icon + " " + etype.replace("_", " ").title() + "\n"
            if lcode: out += "   🔑 `" + lcode + "`\n"
            if details: out += "   📝 " + details + "\n"
            out += "   🕐 " + date2 + "\n\n"
    else:
        out += "   Koi activity nahi abhi tak.\n"

    out += "━━━━━━━━━━━━━━━━━━━━━━"
    await event.reply(out[:4096], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/extend\s+(\d+)\s+(\d+)$"))
async def cmd_extend(event):
    if not is_admin(event.sender_id): return
    await _do_extend(event, int(event.pattern_match.group(1)), int(event.pattern_match.group(2)), event.sender_id)

async def _do_extend(ctx, target_uid, days, admin_uid=None):
    admin_uid = admin_uid or ADMIN_ID
    row = c.execute("SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (target_uid,)).fetchone()
    if row:
        new_exp = (parse_iso(row[1]) + timedelta(days=days)).isoformat()
        await db_write("UPDATE access_codes SET expires_at=?,days_valid=days_valid+? WHERE code=?", (new_exp, days, row[0]))
        await ctx.reply("✅ `" + str(target_uid) + "` +" + str(days) + " din. Expiry: " + new_exp.split("T")[0], buttons=admin_kb(admin_uid), parse_mode='md')
    else:
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,claimed_by,claimed_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?)",
            (code, days, now_iso(), target_uid, now_iso(), expires, admin_uid))
        await ctx.reply("✅ Code `" + code + "` given to `" + str(target_uid) + "`. Expiry: " + expires.split("T")[0], buttons=admin_kb(admin_uid), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/revoke\s+(\S+)$"))
async def cmd_revoke(event):
    if not is_admin(event.sender_id): return
    code = event.pattern_match.group(1).upper()
    await db_write("UPDATE access_codes SET is_active=0 WHERE code=?", (code,))
    await event.reply("🚫 `" + code + "` revoked.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/numbers", "📱 All Numbers"]))
async def cmd_numbers(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT ua.phone,ua.user_id,u.username,ua.added_at FROM user_accounts ua LEFT JOIN users u ON ua.user_id=u.user_id ORDER BY ua.added_at DESC").fetchall()
    if not rows: await event.reply("Koi number nahi.", buttons=admin_kb(event.sender_id)); return
    lines2 = ["📱 **All Numbers** (" + str(len(rows)) + ")\n"]
    buttons = []
    for phone, uid2, uname, added in rows:
        name = "@" + uname if uname else "ID:" + str(uid2)
        lines2.append("• `" + phone + "` — " + name + "  /removenum " + phone)
        buttons.append([Button.inline("🗑 " + phone, ("rmnum_" + phone).encode())])
    await event.reply("\n".join(lines2), buttons=buttons, parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/removenum\s+(\+\d+)$"))
async def cmd_removenum(event):
    if not is_admin(event.sender_id): return
    phone = event.pattern_match.group(1).strip()
    await db_write("DELETE FROM user_accounts WHERE phone=?", (phone,))
    await event.reply("🗑 `" + phone + "` removed.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/endtrial\s+(\d+)$"))
async def cmd_endtrial(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,username,trial_granted FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.reply("❌ User nahi mila."); return
    if not row[2]: await event.reply("⚠️ Is user ka trial tha hi nahi."); return
    await db_write("UPDATE users SET trial_expires=?,trial_granted=0 WHERE user_id=?", (now_iso(), uid))
    name = "@" + row[1] if row[1] else "`" + str(uid) + "`"
    await event.reply("✅ **" + name + " ka Trial Khatam!**", buttons=admin_kb(event.sender_id), parse_mode='md')
    try:
        await bot.send_message(uid, "⚠️ **Tumhara Trial Khatam Ho Gaya**\n\nAdmin ne trial end kar diya.\nAccess ke liye /redeem CODE karo.")
    except Exception: pass

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/tasks", "⏰ All Tasks"]))
async def cmd_tasks(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT st.id,st.user_id,u.username,st.phone,st.interval_seconds,st.is_active,st.fail_count,st.messages_json FROM scheduled_tasks st LEFT JOIN users u ON st.user_id=u.user_id ORDER BY st.id DESC").fetchall()
    if not rows: await event.reply("Koi task nahi.", buttons=admin_kb(event.sender_id)); return
    lines2  = ["⏰ **All Tasks** (" + str(len(rows)) + ")\n"]
    buttons = []
    for tid, uid2, uname, phone, iv, act2, fails, mj in rows:
        name  = "@" + uname if uname else "ID:" + str(uid2)
        msgs  = msgs_list(mj)
        nm    = len(msgs)
        # Show first message preview
        preview = (msgs[0][:60] + "...") if msgs and len(msgs[0]) > 60 else (msgs[0] if msgs else "—")
        lines2.append(
            ("▶️" if act2 else "⏹") + " **#" + str(tid) + "** " + name + "\n"
            "   📱 `" + phone + "` | ⏱ " + fmt_mins(iv) + " | " + str(nm) + " msg\n"
            "   📝 `" + preview + "`"
        )
        row_btns = []
        if act2: row_btns.append(Button.inline("🛑 Stop #" + str(tid),  ("ast_"    + str(tid)).encode()))
        else:    row_btns.append(Button.inline("▶️ Start #" + str(tid), ("astart_" + str(tid)).encode()))
        row_btns.append(Button.inline("👁 Msgs #" + str(tid), ("atms_" + str(tid)).encode()))
        buttons.append(row_btns)
    await event.reply("\n\n".join(lines2), buttons=buttons or None, parse_mode='md')

# Admin view full messages of any task
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"atms_")))
async def cb_atms(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("atms_", ""))
    row = c.execute(
        "SELECT st.messages_json, u.username, st.user_id, st.phone, st.interval_seconds "
        "FROM scheduled_tasks st LEFT JOIN users u ON st.user_id=u.user_id WHERE st.id=?", (tid,)
    ).fetchone()
    if not row: await event.answer("Task nahi mila.", alert=True); return
    mj, uname, uid2, phone, iv = row
    msgs  = msgs_list(mj)
    name  = "@" + uname if uname else "ID:" + str(uid2)
    lines = [
        "📝 **Task #" + str(tid) + " — Messages**\n"
        "👤 " + name + " | 📱 `" + phone + "` | ⏱ " + fmt_mins(iv) + "\n"
        "Total: **" + str(len(msgs)) + "** messages\n"
    ]
    for i, m in enumerate(msgs, 1):
        lines.append("**" + str(i) + ".** `" + m[:300] + "`")
    await event.edit("\n\n".join(lines)[:4000], parse_mode='md')

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
    await event.reply("▶️ **Task #" + str(tid) + " Started!**", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/adminstoptask\s+(\d+)$"))
async def cmd_adminstoptask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.reply("🛑 Task #" + str(tid) + " stopped.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/admindeltask\s+(\d+)$"))
async def cmd_admindeltask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.reply("🗑 Task #" + str(tid) + " deleted.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"astart_")))
async def cb_astart(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("astart_", ""))
    row = c.execute("SELECT user_id,phone,interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.answer("Task nahi mila.", alert=True); return
    uid2, phone, iv = row
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid2, phone)).fetchone()
    if not sess_row: await event.answer("Session nahi mila.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid2, phone, sess_row[0], iv)
    await event.answer("▶️ Started!")
    await event.edit("▶️ **Task #" + str(tid) + " started!**", parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/usergroups\s+(\d+)$"))
async def cmd_usergroups(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    msg = await event.reply("🔍 Fetching...")
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = "@" + urow[0] if urow and urow[0] else "ID:" + str(uid)
    if not accounts: await msg.edit("📊 " + name + " ke koi accounts nahi."); return
    lines2 = ["📊 **" + name + " ke Groups & Channels**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines2.append("\n📵 `" + phone + "`: fail"); continue
        try:
            dlgs     = await cl.get_dialogs(limit=None)
            channels = [d for d in dlgs if d.is_channel]
            groups   = [d for d in dlgs if d.is_group and not d.is_channel]
            lines2.append(
                "\n📱 `" + phone + "`\n"
                "📣 Channels: **" + str(len(channels)) + "** | "
                "👥 Groups: **" + str(len(groups)) + "**\n"
            )
            if channels:
                lines2.append("━━ 📣 **CHANNELS** ━━")
                for g in channels:
                    un = "@" + g.entity.username if getattr(g.entity, "username", None) else "🔒 private"
                    lines2.append("  📣 " + g.name + "  " + un)
            if groups:
                lines2.append("\n━━ 👥 **GROUPS** ━━")
                for g in groups:
                    un = "@" + g.entity.username if getattr(g.entity, "username", None) else "🔒 private"
                    lines2.append("  👥 " + g.name + "  " + un)
        except Exception as e: lines2.append("\n⚠️ `" + phone + "`: " + str(e))
        finally: await close(cl)
    await msg.edit("\n".join(lines2)[:4000], parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/sendmsg\s+(\d+)\s+(.+)$"))
async def cmd_sendmsg(event):
    if not is_admin(event.sender_id): return
    target = int(event.pattern_match.group(1))
    text   = event.pattern_match.group(2).strip()
    try:
        await bot.send_message(target, "📨 **Admin message:**\n\n" + text)
        await event.reply("✅ Sent to `" + str(target) + "`.", buttons=admin_kb(event.sender_id), parse_mode='md')
    except Exception as e:
        await event.reply("❌ Failed: " + str(e), buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and (e.text.strip() == "📢 Broadcast" or e.text.strip().startswith("/broadcast"))))
async def cmd_broadcast(event):
    if not is_admin(event.sender_id): return
    import re
    text = event.text.strip()
    if text == "📢 Broadcast":
        pending[event.sender_id] = {"action": "admin_broadcast"}
        await event.reply("📢 Message type karo:", buttons=[[Button.text("❌ Cancel")]]); return
    m = re.match(r"^/broadcast\s+(.+)$", text, re.DOTALL)
    if not m: await event.reply("Usage: /broadcast text"); return
    await _do_broadcast(event, m.group(1).strip())

async def _do_broadcast(ctx, text):
    users = c.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    prog  = await ctx.reply("📢 Sending to " + str(len(users)) + " users...")
    sent = 0; failed = 0
    for (uid2,) in users:
        try:
            await bot.send_message(uid2, "📢 **Admin message:**\n\n" + text)
            sent += 1; await asyncio.sleep(0.3)
        except Exception: failed += 1
    bcast_uid = getattr(ctx, 'sender_id', ADMIN_ID) or ADMIN_ID
    await prog.edit("📢 **Done!** ✅ " + str(sent) + " | ❌ " + str(failed), buttons=admin_kb(bcast_uid), parse_mode='md')

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
    await event.reply("✏️ Message type karo (ya forward karo):\n/cancel se wapas.", parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📋 My Schedules"))
async def btn_scheds(event): await _show_schedules(event, event.sender_id, edit=False)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🔑 Redeem Code"))
async def btn_redeem(event):
    uid = event.sender_id
    pending[uid] = {"action": "await_redeem_code"}
    await event.reply("🔑 Code type karo:\n/cancel se wapas.", buttons=[[Button.text("❌ Cancel")]], parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "💬 Buy Access"))
async def btn_buy(event):
    await cmd_buy(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🗄 Backup"))
async def btn_backup(event):
    if not is_super_admin(event.sender_id): return
    await cmd_backupstatus(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "❌ Cancel"))
async def btn_cancel(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
    await event.reply("✅ Cancel ho gaya.", buttons=main_kb(), parse_mode='md')

# ─────────────────────────── FORWARD ─────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.message and e.message.fwd_from is not None))
async def on_forward(event):
    uid   = event.sender_id
    ok, _ = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi.", parse_mode='md'); return

    text = event.message.message or ""

    # ── Get ORIGINAL source from fwd_from ──
    fwd      = event.message.fwd_from
    orig_id  = None
    orig_peer= None

    if fwd:
        # Channel post forward
        if getattr(fwd, "channel_post", None) and getattr(fwd, "from_id", None):
            orig_id   = fwd.channel_post
            from_id   = fwd.from_id
            if hasattr(from_id, "channel_id"):
                orig_peer = from_id.channel_id
            elif hasattr(from_id, "chat_id"):
                orig_peer = from_id.chat_id
            elif hasattr(from_id, "user_id"):
                orig_peer = from_id.user_id
        # User message forward
        elif getattr(fwd, "saved_from_msg_id", None):
            orig_id   = fwd.saved_from_msg_id
            orig_peer = getattr(getattr(fwd, "saved_from_peer", None), "channel_id", None)

    # Fallback entities from current message
    entities  = event.message.entities or []
    def entity_to_dict(e):
        return {"type": type(e).__name__, "offset": e.offset, "length": e.length,
                "data": getattr(e, "url", None) or getattr(e, "language", None)}
    ents_json = json.dumps([entity_to_dict(e) for e in entities])

    st = pending.get(uid, {})
    if st.get("action") == "await_msg" and st.get("mode") == "schedule":
        msgs      = st.setdefault("messages", [])
        msg_ids   = st.setdefault("msg_ids", [])   # original msg IDs
        peers     = st.setdefault("peers", [])      # original chat/channel IDs
        ents_list = st.setdefault("entities_list", [])
        msgs.append(text)
        msg_ids.append(orig_id)
        peers.append(orig_peer)
        ents_list.append(ents_json)
        has_src = "✅ Original source mila!" if orig_id and orig_peer else "⚠️ Source nahi mila, entities use hongi"
        await event.reply(
            f"📩 **Message #{len(msgs)} added!**\n{has_src}\n`{text[:80]}`",
            buttons=[
                [Button.inline(f"➕ Add #{len(msgs)+1}", b"add_msg")],
                [Button.inline("▶️ Continue",           b"msgs_done")],
                [Button.inline("❌ Cancel",             b"cx")],
            ], parse_mode='md'
        )
    elif st.get("action") == "tedit_msg":
        tid = st["tid"]
        del pending[uid]
        await db_write(
            "UPDATE scheduled_tasks SET messages_json=?, msg_ids_json=?, source_chat_id=? WHERE id=?",
            (json.dumps([text]), json.dumps([orig_id]), orig_peer, tid)
        )
        if tid in scheduler_tasks:
            scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
        row_e = c.execute("SELECT phone, interval_seconds, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row_e and row_e[2]:
            sess_e = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row_e[0])).fetchone()
            if sess_e: start_task(tid, uid, row_e[0], sess_e[0], row_e[1])
        await event.reply(
            f"✅ **Task #{tid} update ho gaya!**\n`{text[:80]}`",
            buttons=main_kb(), parse_mode='md'
        )
    else:
        pending[uid] = {
            "action":  "msg_ready", "text": text,
            "orig_id": orig_id,     "orig_peer": orig_peer,
            "ents_json": ents_json
        }
        await event.reply(
            f"📩 **Forward detect hua!**\n`{text[:100]}`\n\nKya karna hai?",
            buttons=action_btns(), parse_mode='md'
        )

# ─────────────────────────── CALLBACKS ───────────────────────
@bot.on(events.CallbackQuery(data=b"cx"))
async def cb_cx(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        del pending[uid]
    await event.edit("❌ Cancel ho gaya.", parse_mode='md')

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
    await event.edit("📤 Sending...", parse_mode='md')
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

IV_MAP = {
    b"iv5":300, b"iv10":600, b"iv15":900, b"iv30":1800,
    b"iv45":2700, b"iv60":3600, b"iv120":7200, b"iv180":10800,
    b"iv360":21600, b"iv720":43200, b"iv1440":86400,
}

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
    await event.edit("✏️ **Kitne minutes?** Type karo:\nExamples: `5` `42` `200` (minimum 1)", parse_mode='md')

async def _create_task_cb(event, uid, iv_sec):
    await _finalize_task(event, uid, "all")

async def _finalize_task(event, uid, send_to):
    data  = pending.pop(uid, {})
    msgs  = data.get("messages", [])
    phone = data.get("selected_phone")
    sess  = data.get("selected_sess")
    iv_sec= data.get("iv_sec", 1800)
    ents_list  = data.get("entities_list", [])
    msg_ids    = data.get("msg_ids", [])
    peers      = data.get("peers", [])
    source_cid = data.get("source_chat_id") or (peers[0] if peers else None)
    custom_tgts= data.get("custom_targets", [])
    # Store peers as msg_ids_json combined: [[msg_id, peer], ...]
    fwd_pairs  = [[mid, peer] for mid, peer in zip(msg_ids, peers)]
    if not phone:
        row = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchone()
        if not row: await event.edit("❌ Koi account nahi."); return
        phone, sess = row
    tid = await db_write(
        "INSERT INTO scheduled_tasks(user_id,phone,messages_json,interval_seconds,next_run,msg_ids_json,source_chat_id,send_to,custom_targets) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, phone, json.dumps(msgs), iv_sec,
         (now_utc() + timedelta(seconds=iv_sec)).isoformat(),
         json.dumps({"pairs": fwd_pairs, "ents": ents_list}),
         source_cid, send_to, json.dumps(custom_tgts))
    )
    start_task(tid, uid, phone, sess, iv_sec)
    send_label = "👥 Groups" if send_to=="groups" else ("📣 Channels" if send_to=="channels" else "🌐 Sab")
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
    await event.edit("🔍 Fetching groups...", parse_mode='md')
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
    await event.edit("\n".join(lines)[:4000], parse_mode='md')

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
    await event.edit("🗑 Saare tasks delete ho gaye!", parse_mode='md')

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tms_")))
async def cb_tms(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tms_", ""))
    row = c.execute("SELECT user_id,messages_json FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    msgs  = msgs_list(row[1])
    lines = [f"📝 **Task #{tid} — {len(msgs)} Messages:**\n"]
    for i, m in enumerate(msgs, 1): lines.append(f"**{i}.** `{m[:200]}`\n")
    await event.edit("\n".join(lines)[:4000], parse_mode='md')

# ── Edit message text ──────────────────────────────────────
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tedit_msg_")))
async def cb_tedit_msg(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tedit_msg_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    pending[uid] = {"action": "tedit_msg", "tid": tid}
    await event.edit(
        f"✏️ **Task #{tid} — Naya message bhejo ya forward karo:**\n\n"
        f"⚠️ Purana message replace ho jayega.\n/cancel se wapas.",
        buttons=[[Button.inline("❌ Cancel", b"cx")]], parse_mode='md'
    )

# ── Edit interval time ───────────────────────────────────
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tedit_iv_")))
async def cb_tedit_iv(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tedit_iv_", ""))
    row = c.execute("SELECT user_id, interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    curr_mins = row[1] // 60
    pending[uid] = {"action": "tedit_iv", "tid": tid}
    await event.edit(
        f"⏱ **Task #{tid} — Naya interval set karo:**\n\n"
        f"Current: **{curr_mins} minutes**\n\n"
        f"Naya interval minutes mein type karo (e.g. `30`):\n/cancel se wapas.",
        buttons=[[Button.inline("❌ Cancel", b"cx")]], parse_mode='md'
    )

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
        # Log claim
        _urow4 = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        _un4   = _urow4[0] if _urow4 and _urow4[0] else ""
        _creator = c.execute("SELECT created_by FROM access_codes WHERE code=?", (code,)).fetchone()
        _crid  = _creator[0] if _creator and _creator[0] else ADMIN_ID
        _crow  = c.execute("SELECT username FROM users WHERE user_id=?", (_crid,)).fetchone()
        _cname = _crow[0] if _crow and _crow[0] else str(_crid)
        await log_event("code_claimed", _crid, "@" + _cname if _cname else str(_crid), code,
            "Claimed by " + ("@" + _un4 if _un4 else str(uid)) + " | " + str(days) + " days")
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
        row_btns.append(Button.inline(f"🗑 Del #{tid}",  ("tdl_" + str(tid)).encode()))
        buttons.append(row_btns)
        # Edit row
        buttons.append([
            Button.inline(f"✏️ Edit Msg #{tid}",  ("tedit_msg_" + str(tid)).encode()),
            Button.inline(f"⏱ Edit Time #{tid}", ("tedit_iv_"  + str(tid)).encode()),
        ])
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
                buttons=admin_kb(event.sender_id)
            )
        else:
            await db_write("UPDATE users SET is_protected=0", ())
            await event.reply(
                "🔓 **Sab Users Unprotected!**\n\n"
                "✅ " + str(total) + " users ki protection hata di.\n\n"
                "/protect — dobara sab protect karo\n"
                "/pruser @username — specific user protect karo",
                buttons=admin_kb(event.sender_id)
            )
        return

    # ── USER: sirf owner protect kar sakta hai ──
    await event.reply(
        "🔒 **Protection**\n\n"
        "Apna account protect karne ke liye:\n"
        "Admin se contact karo: @V4_XTRD\n\n"
        "Protection sirf Owner set kar sakta hai."
    )
    return

    # ── (dead code — owner only now) ──
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
            buttons=admin_kb(event.sender_id)
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
            buttons=admin_kb(event.sender_id)
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
            buttons=admin_kb(event.sender_id)
        ); return
    lines = ["🔒 **Protected Users** (" + str(len(rows)) + "/" + str(total) + ")\n"]
    for uid2, uname in rows:
        name = "@" + uname if uname else "ID:" + str(uid2)
        lines.append("  🔒 " + name + " | `" + str(uid2) + "`")
        lines.append("    /pruser " + str(uid2) + " — unprotect")
    await event.reply("\n".join(lines), buttons=admin_kb(event.sender_id), parse_mode='md')

# ─────────────────────────── TEXT INPUT ──────────────────────
SKIP = {
    "➕ Add Account", "📊 My Groups", "👑 Admins", "⏰ Schedule Msg",
    "🚀 Send Now", "📋 My Schedules", "🛑 Stop All", "⚙️ Settings",
    "🔑 Redeem Code", "🔧 Admin Panel", "👤 User Menu", "🔙 User Menu",
    "👥 Users", "📱 All Numbers", "🔑 Codes", "⏰ All Tasks",
    "➕ Gen Code", "📊 Stats", "📢 Broadcast", "❌ Cancel",
    "📋 My Requests", "📜 Logs", "📋 Pending", "💬 Buy Access", "🗄 Backup", "🔄 Git Sync", "✏️ Edit", "⏱ Time",
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
            del pending[uid]; await _do_gencode(event, days, uid)  # ← uid pass karo!
        except ValueError: await event.reply("❌ Number bhejo (e.g. `30`)")

    elif act == "admin_extend":
        try:
            days = int(text)
            if days < 1: raise ValueError
            target_uid = pending[uid]["target_uid"]
            del pending[uid]; await _do_extend(event, target_uid, days, uid)
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
            await event.reply("📩 OTP bheja! 5-digit code enter karo:", parse_mode='md')
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
            await event.reply("🔐 2FA password bhejo:", parse_mode='md')
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

    elif act == "tedit_msg":
        tid = pending[uid]["tid"]
        del pending[uid]
        # Save new message text
        await db_write(
            "UPDATE scheduled_tasks SET messages_json=?, msg_ids_json='[]', source_chat_id=NULL WHERE id=?",
            (json.dumps([text]), tid)
        )
        # Restart task if running
        if tid in scheduler_tasks:
            scheduler_tasks[tid].cancel()
            del scheduler_tasks[tid]
        row2 = c.execute("SELECT phone, interval_seconds, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row2 and row2[2]:
            sess_r = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row2[0])).fetchone()
            if sess_r:
                start_task(tid, uid, row2[0], sess_r[0], row2[1])
        await event.reply(
            f"✅ **Task #{tid} ka message update ho gaya!**\n\n"
            f"📝 Naya message: `{text[:100]}`",
            buttons=main_kb(), parse_mode='md'
        )

    elif act == "tedit_iv":
        tid = pending[uid]["tid"]
        try:
            mins = int(text)
            if mins < 1: raise ValueError
            iv_sec = mins * 60
            del pending[uid]
            await db_write(
                "UPDATE scheduled_tasks SET interval_seconds=? WHERE id=?",
                (iv_sec, tid)
            )
            # Restart task with new interval
            if tid in scheduler_tasks:
                scheduler_tasks[tid].cancel()
                del scheduler_tasks[tid]
            row3 = c.execute("SELECT phone, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
            if row3 and row3[1]:
                sess_r2 = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row3[0])).fetchone()
                if sess_r2:
                    start_task(tid, uid, row3[0], sess_r2[0], iv_sec)
            await event.reply(
                f"✅ **Task #{tid} ka interval update ho gaya!**\n\n"
                f"⏱ Naya interval: **{mins} minutes**",
                buttons=main_kb(), parse_mode='md'
            )
        except ValueError:
            await event.reply("❌ Sirf number bhejo (e.g. `30`)", parse_mode='md')

    elif act == "schedule_custom_iv":
        try:
            mins   = int(text)
            if mins < 1: raise ValueError
            iv_sec = mins * 60
            st2    = pending.get(uid, {})
            st2["iv_sec"] = iv_sec
            pending[uid]  = st2
            await _finalize_task(event, uid, "all")
        except ValueError: await event.reply("❌ Number type karo (e.g. `42`)")

# ─────────────────────────── RESTORE TASKS ───────────────────
async def restore_tasks():
    rows = c.execute(
        "SELECT id,user_id,phone,interval_seconds,next_run FROM scheduled_tasks WHERE is_active=1"
    ).fetchall()
    ok = 0; dead = 0
    for tid, uid, phone, iv, next_run in rows:
        sess_row = c.execute(
            "SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)
        ).fetchone()
        if not sess_row:
            c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
            conn.commit(); dead += 1; continue
        cl = await open_client(phone, sess_row[0])
        if not cl:
            c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
            conn.commit()
            try: await bot.send_message(uid, f"⚠️ Task #{tid} disabled — `{phone}` session expire.")
            except Exception: pass
            dead += 1; continue
        await close(cl)

        # ── Calculate delay before first run ──
        delay = 0
        if next_run:
            try:
                nr_dt = parse_iso(next_run)
                diff  = (nr_dt - now_utc()).total_seconds()
                delay = max(0, diff)   # 0 if overdue, else wait remaining time
            except Exception:
                delay = iv  # fallback: wait full interval

        # Spread out tasks — add small offset per task to avoid simultaneous sends
        delay = delay + (ok * 3)  # 3 sec gap between each task restart

        start_task(tid, uid, phone, sess_row[0], iv, initial_delay=delay)
        ok += 1

    print(f"Tasks: {ok} restored (spread out), {dead} disabled.")

# ─────────────────────────── CLOUD BACKUP SYSTEM ────────────

# ═══════════════════════════════════════════════════════════
#  FULL DATA BACKUP SYSTEM
#  Kya backup hota hai:
#  1. bot_data.db  — Users, Admins, Codes, Tasks, Logs (sab)
#  2. data.json    — Sab tables ka readable JSON export
#  Dono files Saved Messages mein jaati hain
#  session_str bhi DB mein stored hai — alag nahi chahiye
# ═══════════════════════════════════════════════════════════

def _make_json_export():
    """Sab tables ka JSON export banao — readable format"""
    tables = [
        "users", "user_accounts", "access_codes",
        "code_requests", "scheduled_tasks", "admins", "logs"
    ]
    export = {"backup_time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).isoformat(), "tables": {}}
    for table in tables:
        try:
            rows = c.execute(f"SELECT * FROM {table}").fetchall()
            cols = [d[0] for d in c.description]
            export["tables"][table] = [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            export["tables"][table] = []
    return export

async def _do_full_backup(notify=False):
    """Full backup — DB + JSON — dono Saved Messages mein bhejo"""
    import io
    try:
        if not os.path.exists(DB_FILE):
            return False, "DB file nahi mili!"

        stamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d_%H-%M")

        # ── Stats gather karo ──
        try:
            u_cnt  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            a_cnt  = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
            co_cnt = c.execute("SELECT COUNT(*) FROM access_codes").fetchone()[0]
            t_cnt  = c.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
            ad_cnt = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
            lg_cnt = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        except:
            u_cnt = a_cnt = co_cnt = t_cnt = ad_cnt = lg_cnt = 0

        db_sz = round(os.path.getsize(DB_FILE) / 1024, 1)

        caption = (
            "#ALEXADS_BACKUP\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🗄 **ALEXADS BOT — Full Data Backup**\n"
            "📅 " + stamp + " UTC\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 **Data:**\n"
            "  👥 Users: " + str(u_cnt) + "\n"
            "  📱 Phone Accounts: " + str(a_cnt) + "\n"
            "  🔑 Codes: " + str(co_cnt) + "\n"
            "  ⏰ Tasks: " + str(t_cnt) + "\n"
            "  👑 Admins: " + str(ad_cnt) + "\n"
            "  📜 Logs: " + str(lg_cnt) + "\n"
            "  💾 DB Size: " + str(db_sz) + " KB\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Session strings bhi included hain (DB mein stored)\n"
            "_Restore: /restoredb MESSAGE_ID"
        )

        # ── Step 1: DB file bhejo ──
        db_msg = await bot.send_file(
            ADMIN_ID,
            DB_FILE,
            caption=caption,
            file_name="alexads_backup_" + stamp + ".db"
        )

        # ── Step 2: JSON export bhejo ──
        export_data = _make_json_export()
        json_bytes  = json.dumps(export_data, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        json_file   = io.BytesIO(json_bytes)
        json_file.name = "alexads_data_" + stamp + ".json"

        await bot.send_file(
            ADMIN_ID,
            json_file,
            caption=(
                "📋 **JSON Export** — " + stamp + "\n"
                "Sabhi tables ka readable data.\n"
                "DB Message ID: `" + str(db_msg.id) + "`"
            ),
            file_name="alexads_data_" + stamp + ".json"
        )

        if notify:
            await bot.send_message(
                ADMIN_ID,
                "✅ **Auto Backup Complete!**\n\n"
                "📅 " + stamp + "\n"
                "🗄 DB Message ID: `" + str(db_msg.id) + "`\n\n"
                "👥 " + str(u_cnt) + " users | 📱 " + str(a_cnt) + " accounts | 🔑 " + str(co_cnt) + " codes\n\n"
                "Restore ke liye:\n"
                "/restoredb " + str(db_msg.id)
            )

        # ── Also push to GitHub ──
        asyncio.create_task(_git_push_db())
        print(f"✅ Full backup done! DB msg={db_msg.id} | {u_cnt} users | {a_cnt} accounts")
        return db_msg.id, None

    except Exception as e:
        print(f"⚠️ Backup failed: {e}")
        return False, str(e)

async def _git_push_db():
    """DB ko GitHub pe push karo (background)"""
    try:
        sync_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_sync.sh")
        if not os.path.exists(sync_script):
            return
        proc = await asyncio.create_subprocess_exec(
            "bash", sync_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            print("✅ Git push done!")
        else:
            print("⚠️ Git push failed:", stderr.decode()[:200])
    except asyncio.TimeoutError:
        print("⚠️ Git push timeout")
    except Exception as e:
        print(f"⚠️ Git push error: {e}")

# /gitsync — Manual GitHub sync (owner only)
@bot.on(events.NewMessage(pattern=r"^/gitsync$"))
async def cmd_gitsync(event):
    if not is_super_admin(event.sender_id): return
    msg = await event.reply("🔄 **GitHub pe DB push ho raha hai...**")
    try:
        sync_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_sync.sh")
        if not os.path.exists(sync_script):
            await msg.edit("❌ git_sync.sh nahi mila!", buttons=admin_kb(event.sender_id)); return
        proc = await asyncio.create_subprocess_exec(
            "bash", sync_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        if proc.returncode == 0:
            last = out.split("\n")[-1] if out else "Done!"
            await msg.edit(
                "✅ **GitHub Sync Complete!**\n\n"
                "📤 DB GitHub pe push ho gaya!\n"
                "📝 " + last + "\n\n"
                "_Naye system pe: bash install.sh_",
                buttons=admin_kb(event.sender_id)
            )
        else:
            await msg.edit(
                "❌ **Git Push Failed!**\n\n"
                "Error: " + (err or out)[:200] + "\n\n"
                "Fix: bash git_setup.sh chalaao",
                buttons=admin_kb(event.sender_id)
            )
    except asyncio.TimeoutError:
        await msg.edit("⏰ Timeout! 60s se zyada laga.", buttons=admin_kb(event.sender_id), parse_mode='md')
    except Exception as e:
        await msg.edit("❌ Error: " + str(e), buttons=admin_kb(event.sender_id), parse_mode='md')

# /syncstatus — GitHub sync info
@bot.on(events.NewMessage(pattern=r"^/syncstatus$"))
async def cmd_syncstatus(event):
    if not is_super_admin(event.sender_id): return
    bot_dir   = os.path.dirname(os.path.abspath(__file__))
    has_sync  = os.path.exists(os.path.join(bot_dir, "git_sync.sh"))
    has_token = os.path.exists(os.path.join(bot_dir, ".git_token"))
    has_data  = os.path.exists(os.path.join(bot_dir, "data", "bot_data.db"))
    has_log   = os.path.exists(os.path.join(bot_dir, "sync.log"))
    last_sync = "Koi sync nahi hua abhi tak"
    if has_log:
        try:
            with open(os.path.join(bot_dir, "sync.log")) as f:
                slines = f.readlines()
            for line in reversed(slines):
                if "Pushed" in line or "No changes" in line:
                    last_sync = line.strip()[-80:]; break
        except: pass
    db_sz = round(os.path.getsize(DB_FILE) / 1024, 1) if os.path.exists(DB_FILE) else 0
    out = (
        "📊 **GitHub Sync Status**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 git_sync.sh: " + ("✅" if has_sync else "❌ Missing!") + "\n"
        "🔑 Git Token: " + ("✅ Set" if has_token else "❌ Run git_setup.sh!") + "\n"
        "💾 data/bot_data.db: " + ("✅ " + str(db_sz) + " KB" if has_data else "❌ Not synced yet") + "\n\n"
        "🕐 Last sync:\n`" + last_sync + "`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Commands:**\n"
        "/gitsync — Abhi GitHub pe push karo\n"
        "/backup — Telegram + GitHub backup\n\n"
        "**New system setup:**\n"
        "`bash install.sh`"
    )
    await event.reply(out, buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/backup$"))
async def cmd_backup(event):
    if not is_super_admin(event.sender_id): return
    msg = await event.reply(
        "🔄 **Full Backup ho raha hai...**\n\n"
        "📦 DB file + JSON export dono bhej raha hoon Saved Messages mein..."
    )
    mid, err = await _do_full_backup(notify=False)
    if mid:
        await msg.edit(
            "✅ **Full Backup Complete!**\n\n"
            "📤 2 files Saved Messages mein:\n"
            "  🗄 DB file (restore ke liye)\n"
            "  📋 JSON export (readable data)\n\n"
            "🆔 DB Message ID: `" + str(mid) + "`\n\n"
            "New system pe restore:\n"
            "`/restoredb " + str(mid) + "`",
            buttons=admin_kb(event.sender_id)
        )
    else:
        await msg.edit("❌ Backup fail!\n\nError: " + str(err), buttons=admin_kb(event.sender_id), parse_mode='md')

# /restoredb MESSAGE_ID — Saved Messages se DB download + restore
@bot.on(events.NewMessage(pattern=r"^/restoredb(?:\s+(\d+))?$"))
async def cmd_restoredb(event):
    if not is_super_admin(event.sender_id): return
    msg_id = event.pattern_match.group(1)
    
    if not msg_id:
        # List recent backups from saved messages
        msg = await event.reply("🔍 Recent backups dhundh raha hoon...")
        try:
            backups = []
            async for m in bot.iter_messages(ADMIN_ID, limit=50, search="ALEXADS BOT — Auto Backup"):
                if m.file and m.file.name and m.file.name.endswith(".db"):
                    stamp   = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
                    sz      = round(m.file.size / 1024, 1) if m.file.size else 0
                    backups.append((m.id, stamp, sz))
                if len(backups) >= 10:
                    break
            
            if not backups:
                await msg.edit(
                    "📦 **Koi backup nahi mila!**\n\n"
                    "Pehle /backup se backup lo.\n"
                    "Phir naye system pe /restoredb MESSAGE_ID se restore karo.",
                    buttons=admin_kb(event.sender_id)
                ); return
            
            lines = ["📦 **Recent Backups (Saved Messages se):**\n"]
            for mid2, stamp2, sz2 in backups:
                lines.append(f"🗄 `{stamp2}` | {sz2} KB\n   👉 `/restoredb {mid2}`")
            await msg.edit("\n\n".join(lines), buttons=admin_kb(event.sender_id), parse_mode='md')
        except Exception as e:
            await msg.edit(f"❌ Error: {e}", buttons=admin_kb(event.sender_id))
        return
    
    # Restore from specific message ID
    msg = await event.reply(f"⬇️ Message #{msg_id} se DB download ho raha hai...")
    try:
        # Download from saved messages
        tg_msg = await bot.get_messages(ADMIN_ID, ids=int(msg_id))
        if not tg_msg or not tg_msg.file:
            await msg.edit("❌ Message nahi mila ya file nahi hai!", buttons=admin_kb(event.sender_id)); return
        if not tg_msg.file.name.endswith(".db"):
            await msg.edit("❌ Yeh DB file nahi hai!", buttons=admin_kb(event.sender_id)); return
        
        # Current DB ka local backup
        if os.path.exists(DB_FILE):
            stamp_bk = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
            bk_path  = DB_FILE + ".bak_" + stamp_bk
            import shutil
            shutil.copy2(DB_FILE, bk_path)
        
        await msg.edit("⬇️ Downloading...", parse_mode='md')
        # Download to temp then replace
        tmp_path = DB_FILE + ".tmp"
        await bot.download_media(tg_msg, file=tmp_path)
        
        # Verify downloaded file is valid SQLite
        try:
            test_conn = sqlite3.connect(tmp_path)
            tables    = test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            test_conn.close()
            if not tables:
                raise Exception("Empty DB!")
        except Exception as ve:
            os.remove(tmp_path)
            await msg.edit(f"❌ Invalid DB file: {ve}", buttons=admin_kb(event.sender_id)); return
        
        # Replace current DB
        import shutil
        shutil.move(tmp_path, DB_FILE)
        
        # Reload DB connection
        global conn, c
        conn.close()
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c    = conn.cursor()
        
        # Stats of restored DB
        try:
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            codes = c.execute("SELECT COUNT(*) FROM access_codes").fetchone()[0]
            tasks = c.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
            admins_cnt = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
            stats = (
                f"👥 Users: {users}\n"
                f"👑 Admins: {admins_cnt}\n"
                f"🔑 Codes: {codes}\n"
                f"⏰ Tasks: {tasks}"
            )
        except:
            stats = "Stats load nahi hui."
        
        stamp3 = tg_msg.date.strftime("%Y-%m-%d %H:%M") if tg_msg.date else "?"
        await msg.edit(
            "✅ **Restore Complete!**\n\n"
            "📅 Backup date: " + stamp3 + "\n\n"
            "📊 **Restored Data:**\n" + stats + "\n\n"
            "🔄 Bot restart karo tasks dobara chalane ke liye:\n"
            "`pkill -f bot.py && python bot.py`",
            buttons=admin_kb(event.sender_id)
        )
    except Exception as e:
        await msg.edit(f"❌ Restore fail: {e}", buttons=admin_kb(event.sender_id))

# /backupstatus — Backup info
@bot.on(events.NewMessage(pattern=r"^/backupstatus$"))
async def cmd_backupstatus(event):
    if not is_super_admin(event.sender_id): return
    db_sz = round(os.path.getsize(DB_FILE) / 1024, 1) if os.path.exists(DB_FILE) else 0
    interval_h = BACKUP_INTERVAL // 3600
    await event.reply(
        "🗄 **Backup Status**\n\n"
        f"💾 DB Size: `{db_sz} KB`\n"
        f"⏱ Auto Backup: har **{interval_h} ghante**\n"
        f"📤 Location: Tumhare Saved Messages\n\n"
        "**Commands:**\n"
        "/backup — Abhi backup lo\n"
        "/restoredb — Recent backups list\n"
        "/restoredb MSG_ID — Specific backup restore karo",
        buttons=admin_kb(event.sender_id)
    )

async def _auto_backup_loop():
    """Har BACKUP_INTERVAL seconds pe automatically backup lo"""
    await asyncio.sleep(90)
    while True:
        try:
            mid, err = await _do_full_backup(notify=False)
            if mid:
                asyncio.create_task(_git_push_db())
                print(f"Auto backup OK: msg {mid}")
            else:
                print(f"Auto backup failed: {err}")
        except Exception as e:
            print(f"Auto backup error: {e}")
        await asyncio.sleep(BACKUP_INTERVAL)

# ─────────────────────────── MAIN ────────────────────────────
async def main():
    global db_lock
    db_lock = asyncio.Lock()
    print("Bot starting...")
    await bot.start(bot_token=BOT_TOKEN)
    print("Connected!")
    await restore_tasks()
    asyncio.create_task(_auto_backup_loop())
    print(f"✅ Auto backup every {BACKUP_INTERVAL//3600}h | Ready!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
