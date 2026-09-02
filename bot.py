import os
import json
import time
import random
import string
import asyncio
import logging
import traceback
import sqlite3

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# Make sure errors and info actually show up in the hosting provider's logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)  # keep pyrogram's own logs less noisy

load_dotenv()

# ---------------- CONFIG (from .env / Railway Variables) ----------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
STORAGE_CHANNEL_ID = int(os.environ.get("STORAGE_CHANNEL_ID", "0"))

DB_FILE = "bot_data.db"

app = Client(
    "my_drive_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS collections ("
        "code TEXT PRIMARY KEY, message_ids TEXT, created_at INTEGER)"
    )
    conn.commit()
    # Hardcoded sensible defaults - auto-delete defaults to 30 minutes
    defaults = {"mode": "private", "timer_minutes": "30"}
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def save_collection(code, message_ids):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO collections (code, message_ids, created_at) VALUES (?, ?, ?)",
        (code, json.dumps(message_ids), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_collection(code):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT message_ids FROM collections WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def clear_all_collections():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM collections")
    conn.commit()
    conn.close()


def list_collections():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT code, message_ids, created_at FROM collections ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# ---------------- IN-MEMORY STATE ----------------
upload_mode = {}       # user_id -> bool (currently in upload mode)
pending_uploads = {}   # user_id -> list of message_ids saved in storage channel (not yet linked)


def is_owner(user_id):
    return user_id == OWNER_ID


def generate_code(length=10):
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def make_link_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Make Link", callback_data="make_link")]]
    )


# ---------------- DEBUG: log every incoming message/callback ----------------
# This runs in a later group so it never blocks the real handlers below.
# If these lines never show up in your Railway logs when you message the bot,
# it means Telegram updates are not reaching this process at all (not a code bug).
@app.on_message(filters.private, group=10)
async def debug_log_message(client, message: Message):
    print(f"[debug] message from user_id={message.from_user.id if message.from_user else '?'} text={message.text or message.caption or '<media>'}", flush=True)


@app.on_callback_query(group=10)
async def debug_log_callback(client, callback_query: CallbackQuery):
    print(f"[debug] callback from user_id={callback_query.from_user.id} data={callback_query.data}", flush=True)


# ---------------- COMMAND HANDLERS ----------------
@app.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    try:
        if len(message.command) > 1 and message.command[1].startswith("collection_"):
            code = message.command[1][len("collection_"):]
            await deliver_collection(client, message, code)
            return

        await message.reply_text(
            "👋 Welcome!\n\n"
            "Use this bot to upload videos/files and generate a shareable link.\n"
            "Tap the Menu button below, or send /help to see all commands."
        )
    except Exception:
        print("[error] start_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("help"))
async def help_handler(client, message: Message):
    text = (
        "**Available commands:**\n"
        "/make_files - Turn on upload mode (owner only)\n"
        "/make_link - Generate a shareable link\n"
        "/set_mode public|private - Toggle forward/download protection\n"
        "/set_timer <minutes> - Set auto-delete time (0 disables it)\n"
        "/list - View your saved links\n"
        "/clear - Clear all saved links\n"
        "/status - View current settings"
    )
    await message.reply_text(text)


@app.on_message(filters.command("make_files"))
async def make_files_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            await message.reply_text("❌ You are not authorized to use this bot.")
            return

        user_id = message.from_user.id
        upload_mode[user_id] = True
        pending_uploads[user_id] = []
        await message.reply_text(
            "📤 Upload Mode Active!\n\n"
            "Send or forward videos/files to upload them (caption will be stripped).\n"
            "When done, tap the Make Link button on any confirmation, or send /make_link."
        )
    except Exception:
        print("[error] make_files_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("make_link"))
async def make_link_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            await message.reply_text("❌ You are not authorized to use this bot.")
            return
        await finalize_and_send_link(client, message.from_user.id, message.chat.id, reply_to=message)
    except Exception:
        print("[error] make_link_handler failed:\n" + traceback.format_exc(), flush=True)


async def finalize_and_send_link(client, user_id, chat_id, reply_to=None):
    msgs = pending_uploads.get(user_id, [])
    if not msgs:
        text = "❌ Your collection is empty. Send some files first using /make_files."
        if reply_to:
            await reply_to.reply_text(text)
        else:
            await client.send_message(chat_id, text)
        return

    code = generate_code()
    save_collection(code, msgs)
    upload_mode[user_id] = False
    pending_uploads[user_id] = []

    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=collection_{code}"
    text = f"🔗 Your Link:\n{link}"
    if reply_to:
        await reply_to.reply_text(text)
    else:
        await client.send_message(chat_id, text)


@app.on_callback_query(filters.regex("^make_link$"))
async def make_link_callback(client, callback_query: CallbackQuery):
    try:
        user_id = callback_query.from_user.id
        if not is_owner(user_id):
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        if not pending_uploads.get(user_id):
            await callback_query.answer("No files uploaded yet.", show_alert=True)
            return

        await callback_query.answer()
        await finalize_and_send_link(client, user_id, callback_query.message.chat.id, reply_to=callback_query.message)
    except Exception:
        print("[error] make_link_callback failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("set_mode"))
async def set_mode_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            return

        args = message.command
        if len(args) != 2 or args[1] not in ("public", "private"):
            await message.reply_text("Usage: /set_mode public OR /set_mode private")
            return

        set_setting("mode", args[1])
        note = "(download/forward disabled)" if args[1] == "private" else "(download/forward enabled)"
        await message.reply_text(f"✅ Mode changed to: {args[1]} {note}")
    except Exception:
        print("[error] set_mode_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("set_timer"))
async def set_timer_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            return

        args = message.command
        if len(args) != 2 or not args[1].isdigit():
            await message.reply_text("Usage: /set_timer <minutes>, e.g. /set_timer 30 (0 disables auto-delete)")
            return

        set_setting("timer_minutes", args[1])
        await message.reply_text(f"✅ Auto-delete timer set to: {args[1]} minutes")
    except Exception:
        print("[error] set_timer_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("status"))
async def status_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            return

        mode = get_setting("mode")
        timer = get_setting("timer_minutes")
        await message.reply_text(f"⚙️ Current settings:\nMode: {mode}\nAuto-delete: {timer} minutes")
    except Exception:
        print("[error] status_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("list"))
async def list_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            return

        rows = list_collections()
        if not rows:
            await message.reply_text("No links found.")
            return

        me = await client.get_me()
        lines = [f"https://t.me/{me.username}?start=collection_{code}" for code, _, _ in rows[:20]]
        await message.reply_text("\n".join(lines))
    except Exception:
        print("[error] list_handler failed:\n" + traceback.format_exc(), flush=True)


@app.on_message(filters.command("clear"))
async def clear_handler(client, message: Message):
    try:
        if not is_owner(message.from_user.id):
            return

        clear_all_collections()
        await message.reply_text("🗑️ All saved link data has been cleared. (Files in the storage channel are not removed automatically.)")
    except Exception:
        print("[error] clear_handler failed:\n" + traceback.format_exc(), flush=True)


# ---------------- FILE UPLOAD HANDLER ----------------
@app.on_message((filters.video | filters.document) & filters.private)
async def receive_file_handler(client, message: Message):
    try:
        user_id = message.from_user.id

        if not is_owner(user_id):
            return  # ignore files from anyone who is not the owner

        if not upload_mode.get(user_id):
            return  # not currently in upload mode, ignore

        # copy into storage channel, stripping any caption/title
        sent = await message.copy(chat_id=STORAGE_CHANNEL_ID, caption="")
        pending_uploads.setdefault(user_id, []).append(sent.id)

        await message.reply_text(
            f"✅ File saved. (Total: {len(pending_uploads[user_id])})",
            reply_markup=make_link_keyboard(),
        )
    except Exception:
        print("[error] receive_file_handler failed:\n" + traceback.format_exc(), flush=True)
        try:
            await message.reply_text("❌ Something went wrong saving this file. Check the logs.")
        except Exception:
            pass


# ---------------- DELIVERY + AUTO-DELETE ----------------
async def deliver_collection(client, message: Message, code):
    try:
        await _deliver_collection_inner(client, message, code)
    except Exception:
        print("[error] deliver_collection failed:\n" + traceback.format_exc(), flush=True)


async def _deliver_collection_inner(client, message: Message, code):
    msg_ids = get_collection(code)
    if not msg_ids:
        await message.reply_text("❌ This link is invalid or has expired.")
        return

    mode = get_setting("mode")
    protect = mode == "private"
    timer_minutes = int(get_setting("timer_minutes") or 30)

    sent_messages = []
    failed = 0
    for mid in msg_ids:
        try:
            sent = await client.copy_message(
                chat_id=message.chat.id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=mid,
                caption="",
                protect_content=protect,
            )
            sent_messages.append(sent)
        except Exception as e:
            failed += 1
            print(f"Error sending file: {e}", flush=True)

    total = len(msg_ids)
    summary = f"✅ Complete!\n\nFiles: {total} | Sent: {len(sent_messages)} | Failed: {failed}"
    if timer_minutes > 0:
        summary += f"\n\n⚠️ Files will auto-delete in {timer_minutes * 60} seconds"
    await message.reply_text(summary)

    if timer_minutes > 0 and sent_messages:
        asyncio.create_task(
            auto_delete(client, message.chat.id, [m.id for m in sent_messages], timer_minutes)
        )


async def auto_delete(client, chat_id, message_ids, minutes):
    print(f"[auto-delete] scheduled: chat={chat_id} in {minutes} min for {len(message_ids)} messages", flush=True)
    await asyncio.sleep(minutes * 60)
    try:
        await client.delete_messages(chat_id, message_ids)
        print(f"[auto-delete] deleted {len(message_ids)} messages in chat {chat_id}", flush=True)
    except Exception as e:
        print(f"[auto-delete] error: {e}", flush=True)


# ---------------- BOT COMMAND MENU ----------------
async def setup_bot_commands():
    await app.set_bot_commands([
        BotCommand("start", "Bot info"),
        BotCommand("make_files", "Upload files"),
        BotCommand("make_link", "Generate link"),
        BotCommand("list", "View files"),
        BotCommand("clear", "Clear storage"),
        BotCommand("help", "Know all commands"),
    ])


# ---------------- RUN ----------------
async def main():
    init_db()
    await app.start()
    await setup_bot_commands()
    print("Bot starting...", flush=True)
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
