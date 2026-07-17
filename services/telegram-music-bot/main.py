"""Entrypoint: starts the bot client (commands/buttons) and the assistant
client (joins & streams into group voice chats) together."""
import asyncio
import logging

from pyrogram import Client
from pyrogram.types import BotCommand
from pytgcalls import PyTgCalls

from autoplay import AutoplayManager
from broadcast import BroadcastManager
from config import API_HASH, API_ID, ASSISTANT_SESSION, BOT_TOKEN
from handlers import register_handlers
from player import VoiceChatPlayer
from playlist_manager import PlaylistManager
from queue_manager import QueueManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def run() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Set it in Replit Secrets before starting the bot.")
    if not ASSISTANT_SESSION:
        raise RuntimeError(
            "Missing TELEGRAM_SESSION_STRING. Generate it with "
            "services/telegram-music-bot/generate_session.py, then set it in Replit Secrets."
        )

    # Use a named session file so the same Telegram session is reused on
    # every restart.  in_memory=True creates a fresh session each time,
    # leaving the old one alive on Telegram's side — both then receive
    # updates and everything appears twice.
    import os
    session_dir = os.path.dirname(os.path.abspath(__file__))
    bot = Client(
        os.path.join(session_dir, "music_bot"),
        api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
    )
    assistant = Client(
        os.path.join(session_dir, "music_assistant"),
        api_id=API_ID, api_hash=API_HASH, session_string=ASSISTANT_SESSION,
    )
    calls = PyTgCalls(assistant)
    queues = QueueManager()
    player = VoiceChatPlayer(calls, queues)
    broadcaster = BroadcastManager()
    autoplayer = AutoplayManager()
    playlist_mgr = PlaylistManager()

    register_handlers(bot, assistant, player, queues, broadcaster, autoplayer, playlist_mgr)

    await assistant.start()
    await calls.start()
    await bot.start()
    await bot.set_bot_commands([
        BotCommand("start", "Show welcome message and instructions"),
        BotCommand("play", "Play a song by name or YouTube link"),
        BotCommand("skip", "Skip the current track"),
        BotCommand("pause", "Pause playback"),
        BotCommand("resume", "Resume playback"),
        BotCommand("stop", "Stop and leave the voice chat"),
        BotCommand("queue", "Show the current queue"),
        BotCommand("playlist", "📋 Play a YouTube playlist or saved playlist"),
        BotCommand("saveplaylist", "💾 Save a playlist under a name"),
        BotCommand("myplaylists", "📂 List your saved playlists"),
        BotCommand("deleteplaylist", "🗑 Delete a saved playlist"),
        BotCommand("autoplay", "🔄 Enable autoplay of related songs"),
        BotCommand("stopautoplay", "⏹ Stop autoplay"),
        BotCommand("broadcast", "📢 Owner: broadcast a message to all groups"),
    ])

    log.info("Bot and assistant are online. Multi-group voice chat music is ready.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
