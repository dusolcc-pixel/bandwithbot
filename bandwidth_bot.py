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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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

BOT_TOKEN = "9252491638:AAGKzMo27aNlZtRUAcIvt-QUgFWM_fnX3qk"

CONNECTIONS = 8

# ZERO = UNLIMITED

SPEED_LIMIT = 0       # MB/s
MAX_DATA = 0          # GB
MAX_RUNTIME = 0       # Minutes

MAX_CONNECTIONS = 16

CHUNK_SIZE = 256 * 1024

RETRY_DELAY = 3

DASHBOARD_REFRESH = 3

# ==========================================
# GLOBAL VARIABLES
# ==========================================

lock = threading.RLock()
rate_lock = threading.Lock()

stop_event = threading.Event()
run_gate = threading.Event()

error_queue = queue.Queue()

stats = {
    "bytes": 0,
    "completed": 0,
    "deleted": 0,
    "errors": 0,
    "speed": 0,
    "running": False,
    "paused": False,
    "start": None,
    "elapsed": 0,
    "url": "",
}

waiting_for_url = set()
dashboard_messages = {}

alert_chat_id = None
error_notified = False

temp_dir = None
worker_threads = []


# ==========================================
# HELPERS
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


def current_elapsed():

    with lock:

        if stats["running"]:
            return time.monotonic() - stats["start"]

        return stats["elapsed"]


# ==========================================
# DASHBOARD
# ==========================================

def dashboard():

    with lock:

        total = stats["bytes"]
        completed = stats["completed"]
        deleted = stats["deleted"]
        errors = stats["errors"]

        speed = stats["speed"]

        running = stats["running"]
        paused = stats["paused"]

        url = stats["url"]

        conn = CONNECTIONS
        speed_limit = SPEED_LIMIT
        max_data = MAX_DATA
        max_time = MAX_RUNTIME

    if paused:
        status = "⏸ PAUSED"

    elif running:
        status = "🟢 RUNNING"

    else:
        status = "⚪ STOPPED"

    speed_text = (
        f"{speed_limit} MB/s"
        if speed_limit else "Unlimited"
    )

    data_text = (
        f"{max_data} GB"
        if max_data else "Unlimited"
    )

    time_text = (
        f"{max_time} minutes"
        if max_time else "Unlimited"
    )

    url_text = html.escape(url[:65]) if url else "Not selected"

    return (
        "📊 <b>BANDWIDTH DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"Status: <b>{status}</b>\n"
        f"⏱ Duration: {duration(current_elapsed())}\n"
        f"📥 Bandwidth: {human(total)}\n"
        f"🚀 Speed: {human(speed)}/s\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"

        f"✅ Completed downloads: {completed}\n"
        f"🗑 Deleted files: {deleted}\n"
        f"⚠️ Errors: {errors}\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"

        f"🔗 URL: <code>{url_text}</code>\n"
        f"🔌 Connections: {conn}\n"
        f"⚡ Speed limit: {speed_text}\n"
        f"📦 Data limit: {data_text}\n"
        f"⏳ Runtime limit: {time_text}"
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
                "▶ Resume",
                callback_data="resume"
            ),

            InlineKeyboardButton(
                "⚙ Settings",
                callback_data="settings"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh"
            ),
        ],

    ])


# ==========================================
# SETTINGS
# ==========================================

def settings_text():

    return (
        "⚙ <b>DOWNLOAD SETTINGS</b>\n\n"

        f"Connections: {CONNECTIONS}\n"
        f"Speed: {SPEED_LIMIT or 'Unlimited'} MB/s\n"
        f"Data: {MAX_DATA or 'Unlimited'} GB\n"
        f"Runtime: {MAX_RUNTIME or 'Unlimited'} minutes\n\n"

        "<b>Commands</b>\n\n"

        "/connections 8\n"
        "/connections 16\n"
        "/speed 10\n"
        "/speed 0\n"
        "/maxdata 5\n"
        "/maxdata 0\n"
        "/maxtime 60\n"
        "/maxtime 0"
    )


# ==========================================
# URL VALIDATION
# ==========================================

def valid_url(url):

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:

        return False


# ==========================================
# GLOBAL SPEED LIMITER
# ==========================================

def throttle_download(size):

    with rate_lock:

        limit = SPEED_LIMIT

        if limit <= 0:
            return

        delay = size / (limit * 1024 * 1024)

    if delay > 0:
        stop_event.wait(delay)


# ==========================================
# PAUSE ON ERROR
# ==========================================

def pause_download(error):

    global error_notified

    with lock:

        if not stats["running"]:
            return

        stats["paused"] = True
        stats["errors"] += 1

        if not error_notified:

            error_notified = True

            error_queue.put(
                "Download interrupted.\n"
                "Reason: " + str(error)[:250] + "\n\n"
                "All workers have been paused."
            )

    run_gate.clear()


# ==========================================
# DOWNLOAD WORKER
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

        # Wait until started or resumed.

        while not run_gate.wait(0.5):

            if stop_event.is_set():
                return

        if stop_event.is_set():
            break

        try:

            req = Request(
                url,
                headers=headers
            )

            with urlopen(req, timeout=30) as response:

                with open(path, "wb") as file:

                    while not stop_event.is_set():

                        if not run_gate.is_set():
                            break

                        with lock:

                            current_bytes = stats["bytes"]

                            data_limit = MAX_DATA
                            time_limit = MAX_RUNTIME

                            started = stats["start"]

                        # Data limit: 0 means unlimited.

                        if data_limit > 0:

                            max_bytes = data_limit * 1024**3

                            if current_bytes >= max_bytes:

                                stop_download()
                                return

                            remaining = max_bytes - current_bytes

                        else:

                            remaining = CHUNK_SIZE

                        # Runtime limit: 0 means unlimited.

                        if time_limit > 0:

                            elapsed = time.monotonic() - started

                            if elapsed >= time_limit * 60:

                                stop_download()
                                return

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

                        throttle_download(len(data))

        except Exception as error:

            if not stop_event.is_set():

                pause_download(error)

                # Stay paused until manual resume or stop.

                continue

        finally:

            try:

                if os.path.exists(path):

                    os.remove(path)

                    with lock:
                        stats["deleted"] += 1

            except OSError:
                pass


# ==========================================
# START DOWNLOAD
# ==========================================

def start_download(url):

    global temp_dir
    global worker_threads
    global error_notified

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
            "paused": False,

            "start": time.monotonic(),
            "elapsed": 0,

            "url": url,

        })

        count = CONNECTIONS

    error_notified = False

    stop_event.clear()
    run_gate.set()

    temp_dir = tempfile.mkdtemp(
        prefix="bandwidthbot_"
    )

    worker_threads = []

    for number in range(1, count + 1):

        thread = threading.Thread(

            target=worker,

            args=(
                number,
                temp_dir,
                url
            ),

            daemon=True

        )

        thread.start()

        worker_threads.append(thread)

    return True


# ==========================================
# STOP DOWNLOAD
# ==========================================

def stop_download():

    with lock:

        if not stats["running"]:
            return False

        stats["elapsed"] = (
            time.monotonic() - stats["start"]
        )

        stats["running"] = False
        stats["paused"] = False

    stop_event.set()
    run_gate.set()

    return True


# ==========================================
# RESUME DOWNLOAD
# ==========================================

def resume_download():

    global error_notified

    with lock:

        if not stats["running"]:
            return False

        if not stats["paused"]:
            return False

        stats["paused"] = False

    error_notified = False

    run_gate.set()

    return True


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
        "The download will start automatically.",
        parse_mode="HTML"
    )


async def receive_url(update, context):

    global alert_chat_id

    chat_id = update.effective_chat.id

    if chat_id not in waiting_for_url:
        return

    url = update.message.text.strip()

    if not valid_url(url):

        await update.message.reply_text(
            "❌ Invalid HTTP/HTTPS URL."
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


async def stop_command(update, context):

    if stop_download():

        await update.message.reply_text(
            "⏹ Download stopped."
        )

    else:

        await update.message.reply_text(
            "No active download."
        )


async def resume_command(update, context):

    if resume_download():

        await update.message.reply_text(
            "▶️ Download resumed."
        )

    else:

        await update.message.reply_text(
            "Nothing is paused."
        )


async def stats_command(update, context):

    await update.message.reply_text(
        dashboard(),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def settings_command(update, context):

    await update.message.reply_text(
        settings_text(),
        parse_mode="HTML"
    )


# ==========================================
# CHANGE CONNECTIONS
# ==========================================

async def connections_command(update, context):

    global CONNECTIONS

    try:

        value = int(context.args[0])

        if not 1 <= value <= MAX_CONNECTIONS:
            raise ValueError

    except (ValueError, IndexError):

        await update.message.reply_text(
            "Usage: /connections 8\n"
            "Choose 1 to 16."
        )

        return

    CONNECTIONS = value

    await update.message.reply_text(
        f"✅ Connections changed to {value}.\n"
        "Applies to the next download."
    )


# ==========================================
# CHANGE SPEED
# ==========================================

async def speed_command(update, context):

    global SPEED_LIMIT

    try:

        value = float(context.args[0])

        if not 0 <= value <= 1000:
            raise ValueError

    except (ValueError, IndexError):

        await update.message.reply_text(
            "Usage: /speed 10\n"
            "/speed 0 = Unlimited"
        )

        return

    SPEED_LIMIT = value

    await update.message.reply_text(
        f"✅ Speed limit: {value or 'Unlimited'} MB/s"
    )


# ==========================================
# CHANGE DATA LIMIT
# ==========================================

async def maxdata_command(update, context):

    global MAX_DATA

    try:

        value = float(context.args[0])

        if value < 0:
            raise ValueError

    except (ValueError, IndexError):

        await update.message.reply_text(
            "Usage: /maxdata 5\n"
            "/maxdata 0 = Unlimited"
        )

        return

    MAX_DATA = value

    await update.message.reply_text(
        f"✅ Data limit: {value or 'Unlimited'} GB"
    )


# ==========================================
# CHANGE RUNTIME LIMIT
# ==========================================

async def maxtime_command(update, context):

    global MAX_RUNTIME

    try:

        value = float(context.args[0])

        if value < 0:
            raise ValueError

    except (ValueError, IndexError):

        await update.message.reply_text(
            "Usage: /maxtime 60\n"
            "/maxtime 0 = Unlimited"
        )

        return

    MAX_RUNTIME = value

    await update.message.reply_text(
        f"✅ Runtime limit: {value or 'Unlimited'} minutes"
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

    elif query.data == "resume":

        resume_download()

    elif query.data == "settings":

        await query.message.reply_text(
            settings_text(),
            parse_mode="HTML"
        )

        return

    try:

        await query.edit_message_text(
            dashboard(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except Exception:
        pass


# ==========================================
# DASHBOARD REFRESH
# ==========================================

async def dashboard_loop(application):

    previous_bytes = 0
    previous_time = time.monotonic()

    while True:

        await asyncio.sleep(DASHBOARD_REFRESH)

        now = time.monotonic()

        with lock:

            current_bytes = stats["bytes"]

            stats["speed"] = (
                current_bytes - previous_bytes
            ) / max(0.001, now - previous_time)

            if not stats["running"] or stats["paused"]:
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
                pass


# ==========================================
# CONSOLIDATED ERROR NOTIFICATION
# ==========================================

async def alert_loop(application):

    while True:

        await asyncio.sleep(1)

        try:

            message = error_queue.get_nowait()

        except queue.Empty:
            continue

        if alert_chat_id:

            try:

                await application.bot.send_message(

                    chat_id=alert_chat_id,

                    text=(
                        "⚠️ <b>DOWNLOAD PAUSED</b>\n\n"
                        + html.escape(message)
                        + "\n\nUse /resume after checking the problem."
                    ),

                    parse_mode="HTML"

                )

            except Exception:
                pass


# ==========================================
# STARTUP
# ==========================================

async def post_init(application):

    application.create_task(
        dashboard_loop(application)
    )

    application.create_task(
        alert_loop(application)
    )


def main():

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":

        raise SystemExit(
            "Please configure BOT_TOKEN."
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
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("resume", resume_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
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
        CommandHandler("maxdata", maxdata_command)
    )

    application.add_handler(
        CommandHandler("maxtime", maxtime_command)
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
