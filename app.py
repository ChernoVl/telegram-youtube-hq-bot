import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

YOUTUBE_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN env var")

# Format selector:
# - Default tries best video+audio
# - You can override via env YTDLP_FORMAT
YTDLP_FORMAT = os.environ.get("YTDLP_FORMAT", "bv*+ba/b")

# Telegram max file size for bots (bytes). Using 1.95GB to be safe under limits.
TELEGRAM_MAX_BYTES = int(1.95 * 1024 * 1024 * 1024)

# Only handle private chats
def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private_chat(update):
        return
    await update.message.reply_text(
        "Send me a YouTube link and I’ll fetch the highest-quality video I can (≤ ~2GB) and send it back.\n\n"
        "Note: I can’t download DRM-protected or paid content."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private_chat(update):
        return
    await update.message.reply_text("Just paste a YouTube link in this chat.")

async def send_typing(chat, action: ChatAction, context: ContextTypes.DEFAULT_TYPE, seconds: float = 5):
    try:
        await chat.send_action(action=action)
        await asyncio.sleep(seconds)
    except Exception:
        pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Private-only
    if not is_private_chat(update):
        return

    msg = (update.message.text or "").strip()
    if not msg or not YOUTUBE_REGEX.search(msg):
        await update.message.reply_text("Please send a valid YouTube link.")
        return

    url = msg

    status = await update.message.reply_text("Analyzing link…")
    chat = update.effective_chat

    # temp workdir
    workdir = Path(tempfile.mkdtemp(prefix="ytdlp_"))
    outtmpl = str(workdir / "%(title).200s.%(ext)s")

    # Progress hook to keep the user updated
    async def progress_hook(d):
        try:
            if d.get("status") == "downloading":
                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total_bytes:
                    pct = downloaded / total_bytes * 100
                    text = f"Downloading… {pct:.1f}%"
                else:
                    text = "Downloading…"
                await status.edit_text(text)
            elif d.get("status") == "finished":
                await status.edit_text("Merging and finalizing…")
        except Exception:
            pass

    ydl_opts = {
        "format": YTDLP_FORMAT,                             # best video+audio combo
        "outtmpl": outtmpl,                                 # output file template
        "noplaylist": True,                                 # single video only
        "merge_output_format": "mkv",                       # safest container; Telegram supports it
        "concurrent_fragment_downloads": 5,                 # faster
        "quiet": True,
        "progress_hooks": [lambda d: asyncio.create_task(progress_hook(d))],
        # "verbose": True,
    }

    final_file = None
    try:
        # Show typing for a bit so chat feels alive
        asyncio.create_task(send_typing(chat, ChatAction.TYPING, context, 2))

        # Extract info first to fail-fast on DRM/restrictions
        await status.edit_text("Checking availability…")
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("is_live"):
                await status.edit_text("This is a live stream. I can’t download live streams.")
                return

        # Download now
        await status.edit_text("Starting download…")
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Determine merged file path
            # yt-dlp returns filename for the best format via prepare_filename
            file_path = ydl.prepare_filename(info)
            # When merging, container may change; look for the final file in the workdir
            parent = Path(file_path).parent
            # Find the largest media file produced in workdir
            candidates = sorted(
                [p for p in parent.glob("*") if p.suffix.lower() in {".mkv", ".mp4", ".webm"}],
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            final_file = candidates[0] if candidates else Path(file_path)

        # Size check
        size = final_file.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            mb = size / (1024 * 1024)
            await status.edit_text(
                f"Downloaded file is {mb:.1f} MB which exceeds Telegram’s ~2GB limit. "
                "Try a shorter video or constrain quality (set YTDLP_FORMAT)."
            )
            return

        # Send the document
        await status.edit_text("Uploading to Telegram…")
        await chat.send_document(
            document=final_file.open("rb"),
            filename=final_file.name,
            caption="Here you go ✅",
        )
        await status.delete()

    except DownloadError as e:
        message = str(e)
        if "DRM" in message or "No video formats found" in message:
            await status.edit_text(
                "This video appears to be DRM-protected or restricted and cannot be downloaded."
            )
        elif "copyright" in message.lower():
            await status.edit_text("This video is blocked by copyright restrictions.")
        else:
            await status.edit_text(f"Download failed:\n{message[:1000]}")
    except Exception as e:
        await status.edit_text(f"Error: {e}")
    finally:
        # cleanup
        try:
            if workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # private-only guard is inside handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot is running (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
