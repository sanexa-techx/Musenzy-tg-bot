"""Entrypoint: starts the bot client (commands/buttons) and the assistant
client (joins & streams into group voice chats) together.

Render deployment notes
-----------------------
* Render sets RENDER=true automatically — the code detects this and uses
  in_memory=True for both Pyrogram clients so no session files need to
  persist across restarts.
* A minimal aiohttp HTTP server binds to PORT (default 8000) and serves
  /health so Render's health checks pass.
* YouTube cookies are loaded from YOUTUBE_COOKIES_B64 (base64-encoded
  Netscape cookies.txt) when the local cookies.txt file is absent.
"""
import asyncio
import base64
import contextlib
import logging
import os

from aiohttp import web
from pyrogram import Client
from pyrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from pytgcalls import PyTgCalls

from autoplay import AutoplayManager
from broadcast import BroadcastManager
from config import API_HASH, API_ID, ASSISTANT_SESSION, BOT_TOKEN, OWNER_ID
from handlers import register_handlers
from player import VoiceChatPlayer
from playlist_manager import PlaylistManager
from queue_manager import QueueManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

# Render sets this env var automatically.
ON_RENDER = bool(os.environ.get("RENDER"))


# ── YouTube cookies bootstrap ─────────────────────────────────────────────────

def _bootstrap_cookies() -> None:
    """Write cookies.txt from YOUTUBE_COOKIES_B64 if the file is missing."""
    cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.exists(cookies_path):
        return
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not b64:
        return
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
        with open(cookies_path, "w") as f:
            f.write(decoded)
        log.info("cookies.txt written from YOUTUBE_COOKIES_B64")
    except Exception as exc:
        log.warning("Failed to decode YOUTUBE_COOKIES_B64: %s", exc)


# ── Health check HTTP server ──────────────────────────────────────────────────

async def _start_health_server() -> web.AppRunner:
    """Bind a minimal HTTP server on PORT so Render health checks pass."""
    port = int(os.environ.get("PORT", "8000"))

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server listening on port %d", port)
    return runner


# ── Bot startup ───────────────────────────────────────────────────────────────

async def run() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Set it in Replit Secrets / Render env before starting the bot.")
    if not ASSISTANT_SESSION:
        raise RuntimeError(
            "Missing TELEGRAM_SESSION_STRING. Generate it with "
            "services/telegram-music-bot/generate_session.py, then set it in Replit Secrets / Render env."
        )

    _bootstrap_cookies()

    # ── Pyrogram clients ──────────────────────────────────────────────────────
    # On Render the filesystem is ephemeral, so we use in_memory=True.
    # in_memory + session_string reuses the *existing* session (no new auth);
    # it just doesn't write the session back to disk.
    # On Replit we keep named session files so restarts don't re-auth.
    session_dir = os.path.dirname(os.path.abspath(__file__))

    bot = Client(
        os.path.join(session_dir, "music_bot"),
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=ON_RENDER,
    )
    assistant = Client(
        os.path.join(session_dir, "music_assistant"),
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=ASSISTANT_SESSION,
        in_memory=ON_RENDER,
    )

    calls      = PyTgCalls(assistant)
    queues     = QueueManager()
    player     = VoiceChatPlayer(calls, queues)
    broadcaster = BroadcastManager()
    autoplayer = AutoplayManager()
    playlist_mgr = PlaylistManager()

    register_handlers(bot, assistant, player, queues, broadcaster, autoplayer, playlist_mgr)

    # Start health server first so Render marks the service healthy ASAP.
    health_runner = await _start_health_server()

    await assistant.start()
    await calls.start()
    await bot.start()

    # ── Bot command menus ─────────────────────────────────────────────────────
    public_commands = [
        BotCommand("start",          "Show welcome message and instructions"),
        BotCommand("play",           "Play a song by name or YouTube link"),
        BotCommand("skip",           "Skip the current track"),
        BotCommand("pause",          "Pause playback"),
        BotCommand("resume",         "Resume playback"),
        BotCommand("stop",           "Stop and leave the voice chat"),
        BotCommand("queue",          "Show the current queue"),
        BotCommand("playlist",       "📋 Play a YouTube playlist or saved playlist"),
        BotCommand("saveplaylist",   "💾 Save a playlist under a name"),
        BotCommand("myplaylists",    "📂 List your saved playlists"),
        BotCommand("deleteplaylist", "🗑 Delete a saved playlist"),
        BotCommand("autoplay",       "🔄 Enable autoplay of related songs"),
        BotCommand("stopautoplay",   "⏹ Stop autoplay"),
    ]
    await bot.set_bot_commands(public_commands, scope=BotCommandScopeDefault())

    if OWNER_ID:
        owner_commands = public_commands + [
            BotCommand("broadcast", "📢 Broadcast a message to all groups"),
            BotCommand("groups",    "👥 List all groups the bot is in"),
        ]
        with contextlib.suppress(Exception):
            await bot.set_bot_commands(
                owner_commands,
                scope=BotCommandScopeChat(chat_id=OWNER_ID),
            )

    env_label = "Render" if ON_RENDER else "Replit"
    log.info("Bot and assistant are online on %s. Multi-group voice chat music is ready.", env_label)

    try:
        await asyncio.Event().wait()
    finally:
        await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
