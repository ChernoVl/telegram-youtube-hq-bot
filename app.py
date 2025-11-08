import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- Config from environment ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env var with your Telegram bot token")

# Max size we will attempt to upload to Telegram (bytes). Telegram hard limit is ~2GB.
TELEGRAM_MAX_BYTES = int(os.getenv("TELEGRAM_MAX_BYTES", str(1_950_000_000)))

# A simple YouTube URL matcher
YOUTUBE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=[\w-]{11}|youtu\.be/[\w-]{11}).*",
    re.IGNORECASE,
)


def human(n: int) -> str:
    # human-readable size
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


async def safe_edit(message, text: str):
    """Edit a message but ignore network timeouts/errors so they don't crash the bot."""
    try:
        await message.edit_text(text)
    except Exception:
        # swallow httpx.ReadTimeout or any transient Telegram edit failure
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a YouTube link and I’ll fetch the highest available quality and send the file back.\n"
        "Note: Max file size is ~2GB (Telegram limit)."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if not YOUTUBE_RE.match(text):
        await update.message.reply_text("Please send a valid YouTube video URL.")
        return

    # Acknowledge and show progress
    status = await update.message.reply_text("Analyzing link…")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Download in a temp directory
    tmpdir = Path(tempfile.mkdtemp(prefix="yt_"))
    try:
        file_path = await asyncio.to_thread(download_youtube_best, text, tmpdir)

        if not file_path or not file_path.exists():
            await safe_edit(status, "Sorry, I couldn’t download that video.")
            return

        size = file_path.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            await safe_edit(
                status,
                f"Downloaded **{file_path.name}** but it’s too large for Telegram ({human(size)} > {human(TELEGRAM_MAX_BYTES)}).",
            )
            return

        await safe_edit(status, "Uploading to Telegram…")

        # Prefer send_video if it looks like an MP4; otherwise fall back to document
        try:
            if file_path.suffix.lower() == ".mp4":
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=file_path.open("rb"),
                    supports_streaming=True,
                    caption=f"{file_path.name} ({human(size)})",
                )
            else:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=file_path.open("rb"),
                    caption=f"{file_path.name} ({human(size)})",
                )
        finally:
            # best-effort cleanup message
            await safe_edit(status, "Done ✅")

    finally:
        # Clean up temp dir
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def download_youtube_best(url: str, outdir: Path) -> Path | None:
    """
    Uses yt-dlp to fetch the highest quality video+audio and output MP4 if possible.
    Includes extractor args to mitigate current SABR/client issues.
    """
    from yt_dlp import YoutubeDL

    outtmpl = str(outdir / "%(id)s.%(ext)s")

    # Try to prioritize a widely compatible MP4 container.
    # If best streams are non-mp4, remux to mp4 (no re-encode) when possible.
    ydl_opts = {
        # Highest quality combo; fall back to single best if needed
        "format": "bestvideo*+bestaudio/best",
        # Merge into mp4 when possible (remux; fast, no re-encode)
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "concurrent_fragment_downloads": 5,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Workarounds for current YT behavior (SABR / missing URLs / signature issues)
        "extractor_args": {
            "youtube": {
                # Prefer android clients which currently avoid SABR more often
                "player_client": ["android", "android_embedded", "web_creator"],
            }
        },
        # Some hosts are picky about UA
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    # Do the download
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Resolve the actual output filename (after remux)
        # Prefer the actual file present in outdir with the expected id
        vid_id = info.get("id")
        # probe candidates (mp4 first)
        candidates = list(outdir.glob(f"{vid_id}.mp4")) or list(outdir.glob(f"{vid_id}.*"))
        return candidates[0] if candidates else None


def main():
    # Build application; set sane timeouts for long polling
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        # network T/O for getUpdates long-polling
        .get_updates_connect_timeout(30)  # connect timeout
        # read timeout is passed to run_polling below
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # run_polling has its own timeout knobs
    app.run_polling(
        allowed_updates=None,
        stop_signals=None,  # Railway sends SIGTERM; PTB handles it
        poll_interval=0.0,
        timeout=60,         # long-poll read timeout (fix for your error)
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
