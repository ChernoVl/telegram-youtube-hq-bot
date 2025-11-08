import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# ========== Config via Environment Variables ==========
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # <-- set this on Railway
# Optional: change default container tmp dir if needed
WORK_DIR = os.getenv("WORK_DIR", "/tmp")

# Telegram’s hard limit (as of 2025) is 2GB per file for standard bots
TELEGRAM_MAX_BYTES = 2_000_000_000

# Simple YouTube URL regex
YOUTUBE_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=[\w\-]+|youtu\.be/[\w\-]+)\S*"
)


def pick_best_output_file(tmp_dir: Path) -> Path | None:
    """Pick the merged/remuxed file produced by yt-dlp."""
    # Prefer .mp4 (remuxed), else mkv
    mp4s = sorted(tmp_dir.glob("*.mp4"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    if mp4s:
        return mp4s[0]
    mkvs = sorted(tmp_dir.glob("*.mkv"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    if mkvs:
        return mkvs[0]
    # Fallback: any large media file
    alls = sorted(tmp_dir.glob("*"), key=lambda p: p.stat().st_size if p.is_file() else 0, reverse=True)
    return alls[0] if alls else None


async def run_yt_dlp(url: str, out_dir: Path, prefer_mp4: bool = True, timeout_sec: int = 1800) -> tuple[int, str]:
    """
    Run yt-dlp to download highest quality video+audio and merge to one file.
    Returns (exit_code, combined_stdout_stderr).
    """
    # Format note:
    # - bestvideo*+bestaudio -> DASH separate streams
    # - --merge-output-format mp4 to force container MP4 (safer for Telegram preview)
    # - -S sorts by resolution,fps,codec and tries AV1/Vp9 if available, then falls back
    # - --no-playlist ensures single link behavior
    # - --concurrent-fragments speeds up
    # - --restrict-filenames avoids weird chars
    # - --no-warnings keeps logs tidy (we’ll still capture stderr)
    fmt = "bestvideo*+bestaudio/best"
    args = [
        "yt-dlp",
        "-f", fmt,
        "-S", "res:desc,fps:desc,codec:av1,codec:vp9,br:desc",
        "--no-playlist",
        "--concurrent-fragments", "5",
        "--merge-output-format", "mp4" if prefer_mp4 else "mkv",
        "--remux-video", "mp4" if prefer_mp4 else "mkv",
        "--restrict-filenames",
        "-o", "%(title)s.%(ext)s",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(out_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        out = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        return (124, "yt-dlp timed out")

    code = proc.returncode
    text = out[0].decode("utf-8", errors="ignore") if out and out[0] else ""
    return code, text


async def send_safe_edit(message, text: str):
    """Try to edit a status message; if Telegram times out, just ignore."""
    try:
        await message.edit_text(text)
    except Exception:
        # Your log showed httpx.ReadTimeout here; not fatal.
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a YouTube link and I’ll fetch the highest quality video I can.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # Only respond in private chats (you asked for private only)
    if msg.chat.type != "private":
        return

    match = YOUTUBE_RE.search(msg.text)
    if not match:
        await msg.reply_text("Please send a valid YouTube URL.")
        return

    url = match.group(0)

    # Status message to update throughout (edit can timeout; we swallow those)
    status = await msg.reply_text("Analyzing link…")

    # Typing / upload action hints (not required, but nice)
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)

    # Work directory
    tmp_root = Path(WORK_DIR)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_dir_obj = tempfile.TemporaryDirectory(dir=tmp_root)
    tmp_dir = Path(tmp_dir_obj.name)

    # Run yt-dlp
    await send_safe_edit(status, "Downloading the best video + audio…")
    code, log = await run_yt_dlp(url, tmp_dir, prefer_mp4=True)

    if code != 0:
        # Common cases: DRM, SABR, signature issues
        err_note = (
            "Download failed.\n\n"
            "• Make sure the video isn’t DRM-protected (Movies/TV often are).\n"
            "• Some formats may be temporarily unavailable (SABR/‘signature’ warnings).\n"
            "• Ensure the bot runs a recent yt-dlp build.\n\n"
            f"yt-dlp log:\n```\n{log[-1500:]}\n```"
        )
        try:
            await status.edit_text(err_note, parse_mode="Markdown")
        except Exception:
            await msg.reply_text(err_note, parse_mode="Markdown")
        tmp_dir_obj.cleanup()
        return

    # Pick the merged file
    out_file = pick_best_output_file(tmp_dir)
    if not out_file or not out_file.exists():
        try:
            await status.edit_text("I couldn’t find the merged output file. The video might be protected.")
        except Exception:
            await msg.reply_text("I couldn’t find the merged output file. The video might be protected.")
        tmp_dir_obj.cleanup()
        return

    size = out_file.stat().st_size
    if size > TELEGRAM_MAX_BYTES:
        human_mb = round(size / (1024 * 1024), 1)
        try:
            await status.edit_text(
                f"The file is {human_mb} MB, which exceeds Telegram’s ~2000 MB limit. "
                "I can’t upload it. Try a lower resolution with a different link."
            )
        except Exception:
            await msg.reply_text(
                f"The file is {human_mb} MB, which exceeds Telegram’s ~2000 MB limit. "
                "I can’t upload it. Try a lower resolution with a different link."
            )
        tmp_dir_obj.cleanup()
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO)
    await send_safe_edit(status, "Uploading to Telegram…")

    # Try send_video (gets an inline preview). If that fails, fall back to send_document.
    try:
        # Use a readable filename (Telegram uses this for downloads)
        filename = out_file.name
        with out_file.open("rb") as f:
            await msg.reply_video(video=f, filename=filename, caption="Here you go 🎬")
        await send_safe_edit(status, "Done ✅")
    except Exception:
        # Some containers or formats won’t preview – use send_document instead
        try:
            with out_file.open("rb") as f:
                await msg.reply_document(document=f, filename=out_file.name, caption="Here you go 🎬")
            await send_safe_edit(status, "Done ✅")
        except Exception as e:
            # Final failure message with tail of yt-dlp logs for troubleshooting
            err = f"Upload failed: {e}\n\nLog tail:\n```\n{log[-1000:]}\n```"
            try:
                await status.edit_text(err, parse_mode="Markdown")
            except Exception:
                await msg.reply_text(err, parse_mode="Markdown")

    # Cleanup
    try:
        tmp_dir_obj.cleanup()
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        # Timeouts to reduce ReadTimeout issues you saw
        .get_updates_request_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Long-polling run
    print("Bot is running (long polling)…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
