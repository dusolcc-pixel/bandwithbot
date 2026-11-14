#!/usr/bin/env python3

import asyncio
import html
import os
import queue
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================================
# CONFIGURATION
# ==========================================

BOT_TOKEN = "9252491638:AAHFrIGAeAGEtdn2w4JZMkutTGTBEYgn7Wk"

DEFAULT_CONNECTIONS = 8
MAX_CONNECTIONS = 16

# 0 = Unlimited
SPEED_LIMIT_MB = 0

MAX_RUNTIME = 600
MAX_DATA = 1024 * 1024 * 1024

CHUNK_SIZE = 256 * 1024

# ==========================================
# GLOBAL VARIABLES
# ==========================================

lock = threading.RLock()
stop_event = threading.Event()
error_queue = queue.Queue()

connections = DEFAULT_CONNECTIONS
speed_limit = SPEED_LIMIT_MB

stats = {
    "bytes": 0,
    "completed": 0,
    "deleted": 0,
    "errors": 0,
    "speed": 0,
    "running": False,
    "start": None,
    "elapsed": 0,
    "url": "",
}

waiting_for_url = set()
dashboard_messages = {}
alert_chat_id = None


# ==========================================
# UTILITIES
# ==========================================

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024

    return f"{n:.2f} PB"


def duration(seconds):
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)

    return f"{h:02d}:{m:02d}:{s:02d}"


def get_elapsed():
    with lock:
        if stats["running"]:
            return time.monotonic() - stats["start"]

        return stats["elapsed"]


# ==========================================
# TELEGRAM DASHBOARD
# ==========================================

def dashboard():

    with lock:
        running = stats["running"]
        total = stats["bytes"]
        completed = stats["completed"]
        deleted = stats["deleted"]
        errors = stats["errors"]
        speed = stats["speed"]
        url = stats["url"]

        conn = connections
        limit = speed_limit

    status = "🟢 RUNNING" if running else "⚪ STOPPED"

    limit_text = (
        f"{limit} MB/s"
        if limit > 0
        else "Unlimited"
    )

    url_text = html.escape(url[:65]) if url else "Not selected"

    return (
        "📊 <b>BANDWIDTH DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Status: {status}\n"
        f"⏱ Time: {duration(get_elapsed())}\n"
        f"📥 Bandwidth: {human(total)}\n"
        f"🚀 Speed: {human(speed)}/s\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"✅ Downloads: {completed}\n"
        f"🗑 Deleted files: {deleted}\n"
        f"⚠️ Errors: {errors}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"🔗 URL: <code>{url_text}</code>\n"
        f"🔌 Connections: {conn}\n"
        f"⚡ Speed limit: {limit_text}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"Maximum time: {duration(MAX_RUNTIME)}\n"
        f"Maximum data: {human(MAX_DATA)}"
    )


def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶ New Download",
                callback_data="new"
            ),
            InlineKeyboardButton(
                "⏹ Stop",
                callback_data="stop"
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙ Settings",
                callback_data="settings"
            ),
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh"
            ),
        ],
    ])


def settings_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "4 Connections",
                callback_data="conn:4"
            ),
            InlineKeyboardButton(
                "8 Connections",
                callback_data="conn:8"
            ),
            InlineKeyboardButton(
                "16 Connections",
                callback_data="conn:16"
            ),
        ],
        [
            InlineKeyboardButton(
                "1 MB/s",
                callback_data="speed:1"
            ),
            InlineKeyboardButton(
                "5 MB/s",
                callback_data="speed:5"
            ),
            InlineKeyboardButton(
                "10 MB/s",
                callback_data="speed:10"
            ),
        ],
        [
            InlineKeyboardButton(
                "Unlimited",
                callback_data="speed:0"
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅ Dashboard",
                callback_data="dashboard"
            ),
        ],
    ])


# ==========================================
# DOWNLOAD ENGINE
# ==========================================

def worker(number, directory, url):

    path = os.path.join(
        directory,
        f"download_{number}.tmp"
    )

    headers = {
        "User-Agent": "BandwidthBot/1.0"
    }

    while not stop_event.is_set():

        try:

            with lock:
                remaining = MAX_DATA - stats["bytes"]

            if remaining <= 0:
                stop_event.set()
                break

            req = Request(
                url,
                headers=headers
            )

            with urlopen(req, timeout=30) as response:

                with open(path, "wb") as file:

                    while not stop_event.is_set():

                        with lock:
                            remaining = MAX_DATA - stats["bytes"]

                        if remaining <= 0:
                            stop_event.set()
                            break

                        data = response.read(
                            min(CHUNK_SIZE, remaining)
                        )

                        if not data:
                            with lock:
                                stats["completed"] += 1
                            break

                        file.write(data)

                        with lock:
                            stats["bytes"] += len(data)

                        # Aggregate speed limiting
                        with lock:
                            limit = speed_limit
                            started = stats["start"]

                        if limit > 0:
                            allowed = (
                                limit * 1024 * 1024
                                * (time.monotonic() - started)
                            )

                            while (
                                stats["bytes"] > allowed + CHUNK_SIZE
                                and not stop_event.is_set()
                            ):
                                stop_event.wait(0.1)

        except Exception as error:

            if not stop_event.is_set():

                with lock:
                    stats["errors"] += 1

                error_queue.put(
                    f"Worker {number}: {type(error).__name__}: {error}"
                )

                stop_event.wait(3)

        finally:

            try:

                if os.path.exists(path):

                    os.remove(path)

                    with lock:
                        stats["deleted"] += 1

            except OSError as error:

                error_queue.put(
                    f"File deletion failed: {error}"
                )


# ==========================================
# START / STOP
# ==========================================

def start_download(url):

    global temp_dir

    with lock:

        if stats["running"]:
            return False

        stats.update({
            "bytes": 0,
            "completed": 0,
            "deleted": 0,
            "errors": 0,
            "speed": 0,
            "running": True,
            "start": time.monotonic(),
            "elapsed": 0,
            "url": url,
        })

        count = connections

    while True:
        try:
            error_queue.get_nowait()
        except queue.Empty:
            break

    stop_event.clear()

    temp_dir = tempfile.mkdtemp(
        prefix="bandwidthbot_"
    )

    for i in range(1, count + 1):

        thread = threading.Thread(
            target=worker,
            args=(i, temp_dir, url),
            daemon=True
        )

        thread.start()

    threading.Thread(
        target=limit_watcher,
        daemon=True
    ).start()

    return True


def stop_download():

    with lock:

        if not stats["running"]:
            return False

        stats["elapsed"] = (
            time.monotonic() - stats["start"]
        )

        stats["running"] = False

    stop_event.set()

    return True


def limit_watcher():

    while True:

        time.sleep(0.5)

        with lock:

            running = stats["running"]

            elapsed = (
                time.monotonic() - stats["start"]
                if running else 0
            )

            total = stats["bytes"]

        if not running:
            return

        if elapsed >= MAX_RUNTIME or total >= MAX_DATA:

            stop_download()
            return


# ==========================================
# TELEGRAM COMMANDS
# ==========================================

async def start_command(update, context):

    chat_id = update.effective_chat.id

    with lock:
        running = stats["running"]

    if running:

        await update.message.reply_text(
            dashboard(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    waiting_for_url.add(chat_id)

    await update.message.reply_text(
        "🔗 <b>Send your download URL.</b>\n\n"
        "The download will start automatically "
        "after receiving the URL.",
        parse_mode="HTML"
    )


async def receive_url(update, context):

    global alert_chat_id

    chat_id = update.effective_chat.id

    if chat_id not in waiting_for_url:
        return

    url = update.message.text.strip()

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:

        await update.message.reply_text(
            "❌ Invalid URL. Send a valid HTTP/HTTPS URL."
        )

        return

    waiting_for_url.discard(chat_id)

    alert_chat_id = chat_id

    if start_download(url):

        await update.message.reply_text(
            "▶️ Download started!"
        )

        message = await update.message.reply_text(
            dashboard(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        dashboard_messages[chat_id] = message.message_id

    else:

        await update.message.reply_text(
            "A download is already running."
        )


async def settings_command(update, context):

    await update.message.reply_text(
        "⚙ <b>DOWNLOAD SETTINGS</b>\n\n"
        f"Connections: {connections}\n"
        f"Speed limit: {speed_limit or 'Unlimited'} MB/s\n\n"
        "Commands:\n"
        "/connections 8\n"
        "/speed 5\n"
        "/speed 0",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
    )


async def connections_command(update, context):

    global connections

    if not context.args:
        await update.message.reply_text(
            f"Current connections: {connections}"
        )
        return

    try:
        value = int(context.args[0])

        if not 1 <= value <= MAX_CONNECTIONS:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "Choose connections between 1 and 16."
        )

        return

    connections = value

    await update.message.reply_text(
        f"✅ Connections changed to {value}.\n"
        "Applies to the next download."
    )


async def speed_command(update, context):

    global speed_limit

    if not context.args:

        await update.message.reply_text(
            f"Current speed limit: {speed_limit or 'Unlimited'} MB/s"
        )

        return

    try:

        value = float(context.args[0])

        if not 0 <= value <= 1000:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "Enter a value between 0 and 1000."
        )

        return

    speed_limit = value

    await update.message.reply_text(
        f"✅ Speed limit: {value or 'Unlimited'} MB/s"
    )


async def stop_command(update, context):

    if stop_download():

        await update.message.reply_text(
            "⏹ Download stopped."
        )

    else:

        await update.message.reply_text(
            "No active download."
        )


async def stats_command(update, context):

    await update.message.reply_text(
        dashboard(),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ==========================================
# BUTTON HANDLER
# ==========================================

async def button_handler(update, context):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "new":

        if stats["running"]:

            await query.message.reply_text(
                "Stop the current download first."
            )

            return

        waiting_for_url.add(chat_id)

        await query.message.reply_text(
            "🔗 Send your new download URL."
        )

        return

    elif query.data == "stop":

        stop_download()

    elif query.data == "settings":

        await query.message.reply_text(
            "⚙ Settings",
            reply_markup=settings_keyboard()
        )

        return

    elif query.data.startswith("conn:"):

        global connections

        connections = int(
            query.data.split(":")[1]
        )

    elif query.data.startswith("speed:"):

        global speed_limit

        speed_limit = float(
            query.data.split(":")[1]
        )

    try:

        await query.edit_message_text(
            dashboard(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        dashboard_messages[chat_id] = query.message.message_id

    except Exception:
        pass


# ==========================================
# LIVE DASHBOARD
# ==========================================

async def dashboard_loop(application):

    previous_bytes = 0
    previous_time = time.monotonic()

    while True:

        await asyncio.sleep(3)

        now = time.monotonic()

        with lock:

            current_bytes = stats["bytes"]

            stats["speed"] = (
                current_bytes - previous_bytes
            ) / max(0.001, now - previous_time)

            if not stats["running"]:
                stats["speed"] = 0

        previous_bytes = current_bytes
        previous_time = now

        text = dashboard()

        for chat_id, message_id in list(
            dashboard_messages.items()
        ):

            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=main_keyboard()
                )

            except Exception:
                dashboard_messages.pop(chat_id, None)


# ==========================================
# INTERRUPTION ALERTS
# ==========================================

async def alert_loop(application):

    while True:

        await asyncio.sleep(1)

        messages = []

        while True:

            try:
                messages.append(
                    error_queue.get_nowait()
                )

            except queue.Empty:
                break

        if messages and alert_chat_id:

            text = (
                "⚠️ <b>DOWNLOAD INTERRUPTION</b>\n\n"
                + "\n".join(
                    "• " + html.escape(msg[:250])
                    for msg in messages[:5]
                )
            )

            try:

                await application.bot.send_message(
                    chat_id=alert_chat_id,
                    text=text,
                    parse_mode="HTML"
                )

            except Exception:
                pass


async def post_init(application):

    application.create_task(
        dashboard_loop(application)
    )

    application.create_task(
        alert_loop(application)
    )


# ==========================================
# MAIN
# ==========================================

def main():

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":

        raise SystemExit(
            "Please add your Telegram bot token."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("settings", settings_command)
    )

    application.add_handler(
        CommandHandler("connections", connections_command)
    )

    application.add_handler(
        CommandHandler("speed", speed_command)
    )

    application.add_handler(
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_url
        )
    )

    print("BandwidthBot started!")

    application.run_polling()


if __name__ == "__main__":
    main()
