import os
import json
import time
import random
import string
import asyncio
import sqlite3

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message

load_dotenv()

# ---------------- CONFIG (from .env) ----------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))          # your Telegram user ID
STORAGE_CHANNEL_ID = int(os.environ.get("STORAGE_CHANNEL_ID", "0"))  # private storage channel ID (starts with -100)

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


# ---------------- COMMAND HANDLERS ----------------
@app.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    args = message.text.split(" ", 1)
    if len(args) > 1 and args[1].startswith("collection_"):
        code = args[1][len("collection_"):]
        await deliver_collection(client, message, code)
        return

    await message.reply_text(
        "স্বাগতম! 👋\n\n"
        "এই বট দিয়ে ভিডিও আপলোড করে শেয়ারেবল লিংক বানানো যায়।\n"
        "কমান্ড লিস্ট দেখতে /help দিন।"
    )


@app.on_message(filters.command("help"))
async def help_handler(client, message: Message):
    text = (
        "**কমান্ড লিস্ট:**\n"
        "/make_files - আপলোড মোড চালু (শুধু owner)\n"
        "/make_link - নতুন শেয়ারেবল লিংক বানানো\n"
        "/set_mode public|private - forward/download protect চালু-বন্ধ\n"
        "/set_timer <minutes> - auto-delete সময় সেট করা (0 দিলে বন্ধ)\n"
        "/list - সব লিংক দেখা\n"
        "/clear - সব লিংক ডাটা ক্লিয়ার করা\n"
        "/status - বর্তমান সেটিংস দেখা"
    )
    await message.reply_text(text)


@app.on_message(filters.command("make_files"))
async def make_files_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        await message.reply_text("❌ আপনি এই বট ব্যবহার করার অনুমতি পাননি।")
        return

    user_id = message.from_user.id
    upload_mode[user_id] = True
    pending_uploads[user_id] = []
    await message.reply_text(
        "📤 আপলোড মোড চালু হয়েছে!\n"
        "এখন ভিডিও/ফাইল পাঠান বা ফরওয়ার্ড করুন (ক্যাপশন বাদ দিয়ে সেভ হবে)।\n"
        "শেষ হলে /make_link দিন।"
    )


@app.on_message(filters.command("make_link"))
async def make_link_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        await message.reply_text("❌ আপনি এই বট ব্যবহার করার অনুমতি পাননি।")
        return

    user_id = message.from_user.id
    msgs = pending_uploads.get(user_id, [])
    if not msgs:
        await message.reply_text("⚠️ কোনো ফাইল আপলোড করা হয়নি। আগে /make_files দিয়ে ফাইল পাঠান।")
        return

    code = generate_code()
    save_collection(code, msgs)
    upload_mode[user_id] = False
    pending_uploads[user_id] = []

    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=collection_{code}"
    await message.reply_text(f"🔗 আপনার লিংক:\n{link}")


@app.on_message(filters.command("set_mode"))
async def set_mode_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        return

    args = message.text.split()
    if len(args) != 2 or args[1] not in ("public", "private"):
        await message.reply_text("ব্যবহার: /set_mode public অথবা /set_mode private")
        return

    set_setting("mode", args[1])
    note = "(download/forward বন্ধ থাকবে)" if args[1] == "private" else "(download/forward চালু থাকবে)"
    await message.reply_text(f"✅ মোড পরিবর্তন হয়েছে: {args[1]} {note}")


@app.on_message(filters.command("set_timer"))
async def set_timer_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        return

    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.reply_text("ব্যবহার: /set_timer <minutes>, যেমন /set_timer 30 (0 দিলে auto-delete বন্ধ)")
        return

    set_setting("timer_minutes", args[1])
    await message.reply_text(f"✅ Auto-delete টাইমার সেট হয়েছে: {args[1]} মিনিট")


@app.on_message(filters.command("status"))
async def status_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        return

    mode = get_setting("mode")
    timer = get_setting("timer_minutes")
    await message.reply_text(f"⚙️ বর্তমান সেটিংস:\nমোড: {mode}\nAuto-delete: {timer} মিনিট")


@app.on_message(filters.command("list"))
async def list_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        return

    rows = list_collections()
    if not rows:
        await message.reply_text("কোনো লিংক পাওয়া যায়নি।")
        return

    me = await client.get_me()
    lines = [f"https://t.me/{me.username}?start=collection_{code}" for code, _, _ in rows[:20]]
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command("clear"))
async def clear_handler(client, message: Message):
    if not is_owner(message.from_user.id):
        return

    clear_all_collections()
    await message.reply_text("🗑️ সব লিংক ডাটা ক্লিয়ার করা হয়েছে। (স্টোরেজ চ্যানেলের ফাইল আলাদাভাবে ডিলিট করতে হবে)")


# ---------------- FILE UPLOAD HANDLER ----------------
@app.on_message((filters.video | filters.document) & filters.private)
async def receive_file_handler(client, message: Message):
    user_id = message.from_user.id

    if not is_owner(user_id):
        return  # ignore files from anyone who is not the owner

    if not upload_mode.get(user_id):
        return  # not currently in upload mode, ignore

    # copy into storage channel, stripping any caption/title
    sent = await message.copy(chat_id=STORAGE_CHANNEL_ID, caption="")
    pending_uploads.setdefault(user_id, []).append(sent.id)

    await message.reply_text(f"✅ ফাইল সেভ হয়েছে। (মোট: {len(pending_uploads[user_id])})")


# ---------------- DELIVERY + AUTO-DELETE ----------------
async def deliver_collection(client, message: Message, code):
    msg_ids = get_collection(code)
    if not msg_ids:
        await message.reply_text("❌ লিংকটি সঠিক নয় বা মেয়াদ শেষ হয়ে গেছে।")
        return

    mode = get_setting("mode")
    protect = mode == "private"
    timer_minutes = int(get_setting("timer_minutes") or 0)

    sent_messages = []
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
            print(f"Error sending file: {e}")

    if timer_minutes > 0 and sent_messages:
        asyncio.create_task(
            auto_delete(client, message.chat.id, [m.id for m in sent_messages], timer_minutes)
        )


async def auto_delete(client, chat_id, message_ids, minutes):
    await asyncio.sleep(minutes * 60)
    try:
        await client.delete_messages(chat_id, message_ids)
    except Exception as e:
        print(f"Auto-delete error: {e}")


# ---------------- RUN ----------------
if __name__ == "__main__":
    init_db()
    print("Bot starting...")
    app.run()
