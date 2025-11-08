# Telegram YouTube HQ Bot

A Telegram bot that accepts a YouTube link in a **private chat**, downloads the **highest-quality** video with `yt-dlp` + `ffmpeg`, and sends the file back (≤ ~2GB Telegram bot limit).

> ⚠️ Use only with content you own or have permission to download. Some videos may be DRM-protected or restricted and will not download.

## Features
- Highest-available **video+audio** via `yt-dlp` (`bestvideo*+bestaudio/best`)
- Auto-merge with **FFmpeg** (container chosen automatically)
- Progress updates while downloading
- Private chats only (ignores groups)
- Size check before sending (Telegram limit)
- Clear error messages for DRM / geo / rate-limit / too large, etc.

## Quick start (local)
```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=123456:ABC...   # your token
python app.py
