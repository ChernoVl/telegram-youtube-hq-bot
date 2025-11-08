# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install ffmpeg (needed by yt-dlp to merge video+audio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specs first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py /app/app.py

# Environment
ENV PYTHONUNBUFFERED=1

# Expose nothing (bot uses long polling)
# EXPOSE 8080

# The bot reads BOT_TOKEN from env at runtime
CMD ["python", "-u", "app.py"]
