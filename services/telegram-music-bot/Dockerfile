FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg  — audio processing / re-encoding for py-tgcalls
# curl    — needed for bun installer
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# ── Bun (JS runtime for yt-dlp EJS n-challenge solving) ──────────────────────
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY services/telegram-music-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Bot source ────────────────────────────────────────────────────────────────
COPY services/telegram-music-bot/ .

# Runtime directories
RUN mkdir -p downloads .session_tmp

EXPOSE 8000

CMD ["python", "main.py"]
