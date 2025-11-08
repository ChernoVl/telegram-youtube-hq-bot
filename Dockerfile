# Use slim Python base
FROM python:3.11-slim

# Ensure logs flush immediately
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install system deps: ffmpeg (required), and basic tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better build caching)
WORKDIR /app
COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt

# Copy the rest of the app
COPY . /app

# Use tini as entrypoint for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Start the bot
CMD ["python", "app.py"]
