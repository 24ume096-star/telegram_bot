# telegram_ledger_plus_only.py
# Ledger bot (final): per-user totals on top, recent entries in middle, totals block at bottom.
# Replace TELEGRAM_TOKEN with your token (keep quotes).

import re
import sqlite3
import logging
import time
import sys
import asyncio
from datetime import datetime, timezone

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# Fix event loop on some Windows setups
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ========== CONFIG ==========
import os
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8552973630:AAFL3g6d_hMjKmqTQiP7ekmXt5iqDx5T1Uo")
DB_PATH = 'ledger_plus_only.db'
DEFAULT_RATE = 93.5
# ============================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Regex accepts +123 or -123 (commas & decimals allowed)
SINGLE_SIGN_NUMBER_RE = re.compile(r'^\s*([+-][0-9]{1,3}(?:,[0-9]{3})*(?:\.\d+)?|[+-][0-9]+(?:\.\d+)?)\s*$')

# ---------- Database ----------
def init_db(path=DB_PATH):
    conn = sqlite3.connect(path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount_inr REAL,
            amount_usdt REAL,
            ts TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('rate', ?)", (str(DEFAULT_RATE),))
    conn.commit()
    return conn

_db = init_db()

def get_rate():
    cur = _db.cursor()
    cur.execute("SELECT value FROM meta WHERE key='rate'")
    r = cur.fetchone()
    if r:
        try:
            return float(r[0])
        except:
            return DEFAULT_RATE
    set_rate(DEFAULT_RATE)
    return DEFAULT_RATE

def set_rate(rate: float):
    cur = _db.cursor()
    cur.execute("REPLACE INTO meta (key, value) VALUES (?,?)", ('rate', str(rate)))
    _db.commit()

def add_entry(user_id: int, username: str, amount_inr: float, amount_usdt: float):
    cur = _db.cursor()
    ts = datetime.now().astimezone().isoformat()
    cur.execute('INSERT INTO entries (user_id, username, amount_inr, amount_usdt, ts) VALUES (?,?,?,?,?)',
                (user_id, username, amount_inr, amount_inr and amount_usdt or 0.0, ts))
    # note: second expression stores actual amount_usdt, fallback kept 0.0 (but normally amount_inr gives amount_usdt)
    # to be safe we just insert the provided amount_usdt argument:
    cur.execute('UPDATE entries SET amount_usdt = ? WHERE id = (SELECT MAX(id) FROM entries)', (amount_usdt,))
    _db.commit()
    return cur.lastrowid

def delete_last_entry():
    cur = _db.cursor()
    cur.execute('SELECT id FROM entries ORDER BY id DESC LIMIT 1')
    row = cur.fetchone()
    if not row:
        return None
    last_id = row[0]
    cur.execute('DELETE FROM entries WHERE id = ?', (last_id,))
    _db.commit()
    return last_id

def reset_ledger():
    cur = _db.cursor()
    cur.execute('DELETE FROM entries')
    _db.commit()

def get_last_entries(limit=10):
    cur = _db.cursor()
    cur.execute('SELECT id, username, amount_inr, amount_usdt, ts FROM entries ORDER BY id DESC LIMIT ?', (limit,))
    return cur.fetchall()

# ---------- helpers ----------
def parse_signed_token(token: str) -> float:
    t = token.replace(',', '')
    return float(t)

def format_num(n: float) -> str:
    try:
        if float(n).is_integer():
            return str(int(n))
        return f"{n:.2f}"
    except:
        return str(n)

def format_commas(n: float) -> str:
    try:
        if float(n).is_integer():
            return f"{int(n):,}"
        else:
            return f"{n:,.2f}"
    except:
        return str(n)

async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except:
        return False

# Safe reply: prefer reply; if fail (deleted message), fallback to send_message
async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    try:
        if update and getattr(update, "message", None):
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    except Exception as e:
        try:
            err = str(e)
            if isinstance(e, telegram.error.BadRequest) and 'Message to be replied not found' in err:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send message after fallback.")

# ---------- reporting function (FINAL desired layout) ----------
def build_report(latest_n=12):
    cur = _db.cursor()

    # 1) Per-user totals (top section)
    # Sum per username: total INR and total USDT
    per_user = cur.execute(
        'SELECT COALESCE(username, "Unknown"), '
        'SUM(amount_inr) as s_inr, SUM(amount_usdt) as s_usdt '
        'FROM entries GROUP BY username ORDER BY s_inr DESC'
    ).fetchall()

    # 2) Recent entries (middle section)
    recent = get_last_entries(latest_n)

    # 3) Totals block (bottom)
    cur.execute(
        'SELECT '
        'SUM(CASE WHEN amount_inr>0 THEN amount_inr ELSE 0 END), '
        'SUM(CASE WHEN amount_inr<0 THEN amount_inr ELSE 0 END), '
        'SUM(CASE WHEN amount_usdt>0 THEN amount_usdt ELSE 0 END), '
        'SUM(CASE WHEN amount_usdt<0 THEN amount_usdt ELSE 0 END) '
        'FROM entries'
    )
    row = cur.fetchone() or (0, 0, 0, 0)
    gross_inr = row[0] or 0.0
    refunded_inr = -(row[1] or 0.0)
    gross_usdt = row[2] or 0.0
    refunded_usdt = -(row[3] or 0.0)

    net_inr = gross_inr - refunded_inr
    net_usdt = gross_usdt - refunded_usdt

    lines = []
    lines.append("📋 Ledger")
    lines.append("")

    # --- Per-user totals (top) ---
        # --- Per-user totals (top) ---
    # We want the user from the most recent entry to appear first.
    if per_user:
        # determine last-updated username from recent (recent list is created below; use it here)
        last_user = None
        if recent and len(recent) > 0:
            # recent[0] is (id, username, inr, usdt, ts)
            last_user = recent[0][1]

        # move last_user to the front of the per_user list if present
        if last_user:
            # make a stable reorder: last_user first, then others in original order
            reordered = []
            found = None
            for uname, s_inr, s_usdt in per_user:
                if uname == last_user:
                    found = (uname, s_inr, s_usdt)
                else:
                    reordered.append((uname, s_inr, s_usdt))
            if found:
                per_user = [found] + reordered
            # else leave per_user as-is (no match)
        # write per-user lines
        for uname, s_inr, s_usdt in per_user:
            lines.append(f"{uname} ▶ {format_commas(s_inr)} = {format_num(s_usdt)} USDT")
    else:
        lines.append("No user totals yet.")
    lines.append("")  # gap

     # --- Recent entries (middle) ---
    lines.append("➡️ 已入账 (Recent):")
    if recent:
        for _id, uname, inr, usdt, ts in recent:
            # timestamp on own line (local time), then username + amount line
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone()
                short_ts = local.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                short_ts = ts.replace('T', ' ')[:19]

            sign = '+' if inr >= 0 else '-'
            lines.append(f"{short_ts} |")
            lines.append(f"{uname} | {sign}{format_commas(abs(inr))} = {format_num(abs(usdt))} USDT")
            lines.append("")  # blank between entries (matches style)
    else:
        lines.append("No recent entries.")
        lines.append("")

    # --- Bottom totals block (INR then USDT) ---
    lines.append(f"💰 Total INR (Gross) | 总入款: {format_commas(gross_inr)}")
    lines.append(f"↩️ Refunded INR | 已退款: {format_commas(refunded_inr)}")
    lines.append(f"🔢 Net INR | 实际入款: {format_commas(net_inr)}")
    lines.append("")  # small gap
    lines.append(f"💲 Total USDT (Gross) | 总 USDT: {format_num(gross_usdt)}")
    lines.append(f"↩️ Refunded USDT | 已退款 USDT: {format_num(refunded_usdt)}")
    lines.append(f"🔢 Net USDT | 实际 USDT: {format_num(net_usdt)}")
    lines.append("")  # gap before rate
    lines.append(f"Rate | 汇率: {format_num(get_rate())}")

    return "\n".join(lines)

# ---------- message handler ----------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not update.message.reply_to_message:
        return

    m = SINGLE_SIGN_NUMBER_RE.match(text)
    if not m:
        return

    # admin check
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can record payments.")
            return
    except Exception:
        await safe_reply(update, context, "Permission check failed. Only admins can record payments.")
        return

    try:
        val = parse_signed_token(m.group(1))
    except Exception:
        return

    if abs(val) < 0.00001:
        return

    target = update.message.reply_to_message.from_user
    target_name = target.username or f"{target.first_name or ''} {target.last_name or ''}".strip()
    target_id = target.id

    rate = get_rate()
    usdt = val / rate if rate != 0 else 0.0

    add_entry(target_id, target_name, val, usdt)

    conv = (f"💸 {update.message.from_user.first_name or update.message.from_user.username}\n"
            f"{'+' if val>=0 else '-'}{format_commas(abs(val))}\n\n"
            f"{target_name}: {format_commas(val)} = {format_num(usdt)} USDT")
    await safe_reply(update, context, conv)

    report = build_report(latest_n=10)
    await safe_reply(update, context, report)
    return

# ---------- commands ----------
async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin-only
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can change rate.")
            return
    except:
        await safe_reply(update, context, "Permission check failed.")
        return

    args = context.args
    if not args:
        await safe_reply(update, context, f"Current rate: {get_rate()}")
        return
    try:
        r = float(args[0])
        if r <= 0:
            raise ValueError
    except:
        await safe_reply(update, context, "Provide a valid positive rate. Example: /rate 93.5")
        return
    set_rate(r)
    await safe_reply(update, context, f"Rate updated to {r}")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context, build_report(latest_n=12))

async def ledger_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    limit = 10
    if args:
        try:
            limit = min(100, max(1, int(args[0])))
        except:
            pass
    rows = get_last_entries(limit)
    if not rows:
        await safe_reply(update, context, "No entries yet.")
        return
    lines = []
    for r in rows:
        _id, uname, inr, usdt, ts = r
        short = ts.replace('T',' ')[:19]
        sign = '+' if inr >= 0 else '-'
        lines.append(f"{_id}. {uname}: {sign}{format_commas(abs(inr))} INR = {format_num(abs(usdt))} USDT  ({short})")
    await safe_reply(update, context, "\n".join(lines))

async def myentries_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 10
    args = context.args
    if args:
        try:
            limit = min(100, max(1, int(args[0])))
        except:
            pass
    user = update.message.from_user
    rows = _db.cursor().execute('SELECT id, amount_inr, amount_usdt, ts FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT ?', (user.id, limit)).fetchall()
    if not rows:
        await safe_reply(update, context, "You have no entries yet.")
        return
    lines = []
    for r in rows:
        _id, inr, usdt, ts = r
        short = ts.replace('T',' ')[:19]
        sign = '+' if inr >= 0 else '-'
        lines.append(f"{_id}. {sign}{format_commas(abs(inr))} INR = {format_num(abs(usdt))} USDT  ({short})")
    await safe_reply(update, context, "\n".join(lines))

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin-only manual add (reply with /add amount OR /add username amount)
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can use /add.")
            return
    except:
        await safe_reply(update, context, "Permission check failed.")
        return

    args = context.args
    if update.message.reply_to_message and len(args) == 1:
        try:
            val = float(args[0].replace(',', ''))
        except:
            await safe_reply(update, context, "Invalid amount. Example as reply: /add 3986")
            return
        target = update.message.reply_to_message.from_user
        name = target.username or f"{target.first_name or ''} {target.last_name or ''}".strip()
        rate = get_rate()
        usdt = val / rate if rate != 0 else 0.0
        add_entry(target.id, name, val, usdt)
        await safe_reply(update, context, f"Added for {name}: {format_commas(val)} = {format_num(usdt)} USDT")
        return

    if len(args) == 2:
        name = args[0]
        try:
            val = float(args[1].replace(',', ''))
        except:
            await safe_reply(update, context, "Invalid amount. Usage: /add username amount")
            return
        rate = get_rate()
        usdt = val / rate if rate != 0 else 0.0
        add_entry(0, name, val, usdt)
        await safe_reply(update, context, f"Added for {name}: {format_commas(val)} = {format_num(usdt)} USDT")
        return

    await safe_reply(update, context, "Usage: reply to user with /add 3986  OR  /add username 3986")

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin only
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can undo.")
            return
    except:
        await safe_reply(update, context, "Permission check failed.")
        return

    last = delete_last_entry()
    if not last:
        await safe_reply(update, context, "No entries to delete.")
    else:
        await safe_reply(update, context, f"Last entry (id={last}) deleted.")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can export.")
            return
    except:
        await safe_reply(update, context, "Permission check failed.")
        return

    rows = _db.cursor().execute('SELECT id, username, amount_inr, amount_usdt, ts FROM entries ORDER BY id').fetchall()
    if not rows:
        await safe_reply(update, context, "No entries to export.")
        return
    fname = f'ledger_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    try:
        with open(fname, 'w', newline='', encoding='utf-8') as f:
            import csv
            writer = csv.writer(f)
            writer.writerow(['id','username','amount_inr','amount_usdt','timestamp'])
            writer.writerows(rows)
        try:
            await update.message.reply_document(document=InputFile(fname))
        except Exception:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(fname))
    finally:
        try:
            import os
            os.remove(fname)
        except:
            pass

# ---------- reset with confirmation ----------
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin-only
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update, context, "Only admins can reset.")
            return
    except:
        await safe_reply(update, context, "Permission check failed.")
        return

    keyboard = [
        [InlineKeyboardButton("✅ Yes — reset", callback_data="reset_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")]
    ]
    try:
        await update.message.reply_text("⚠️ You are about to delete ALL ledger entries. This cannot be undone.\n\nIf you want a copy, use /export first. Proceed?",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ You are about to delete ALL ledger entries. This cannot be undone.\n\nIf you want a copy, use /export first. Proceed?")

async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "reset_cancel":
        try:
            await query.edit_message_text("✅ Reset cancelled.")
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Reset cancelled.")
        return

    # admin confirm again
    user_id = query.from_user.id
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        if member.status not in ("administrator", "creator"):
            try:
                await query.edit_message_text("Only admins can confirm reset.")
            except:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="Only admins can confirm reset.")
            return
    except:
        try:
            await query.edit_message_text("Permission check failed. Reset aborted.")
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Permission check failed. Reset aborted.")
        return

    try:
        reset_ledger()
    except Exception as e:
        try:
            await query.edit_message_text(f"❌ Failed to reset ledger: {e}")
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to reset ledger: {e}")
        return

    try:
        await query.edit_message_text("🔄 Ledger has been reset. All entries cleared.")
    except:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔄 Ledger has been reset. All entries cleared.")

    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=build_report(latest_n=10))
    except:
        pass

# ---------- app setup ----------
def start_app():
    if TELEGRAM_TOKEN is None or TELEGRAM_TOKEN.strip() == "" or TELEGRAM_TOKEN == "REPLACE_WITH_YOUR_TOKEN":
        logger.error("Please edit TELEGRAM_TOKEN in the script.")
        raise SystemExit("No token set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    app.add_handler(CommandHandler('rate', rate_cmd))
    app.add_handler(CommandHandler('summary', summary_cmd))
    app.add_handler(CommandHandler('ledger', ledger_cmd))
    app.add_handler(CommandHandler('myentries', myentries_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('undo', undo_cmd))
    app.add_handler(CommandHandler('export', export_cmd))
    app.add_handler(CommandHandler('reset', reset_cmd))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern='^reset_'))

    logger.info("Starting bot (make sure privacy mode is disabled in BotFather).")
    while True:
        try:
            app.run_polling()
            break
        except Exception as e:
            logger.exception("Bot crashed, retrying in 10s: %s", e)
            time.sleep(10)

if __name__ == "__main__":
    start_app()
