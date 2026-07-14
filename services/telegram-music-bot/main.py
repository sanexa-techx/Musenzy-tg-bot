"""Entrypoint: starts the bot client (commands/buttons) and the assistant
client (joins & streams into group voice chats) together."""
import asyncio
import logging

from pyrogram import Client
from pytgcalls import PyTgCalls

from config import API_HASH, API_ID, ASSISTANT_SESSION, BOT_TOKEN
from handlers import register_handlers
from player import VoiceChatPlayer
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

    bot = Client("music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
    assistant = Client("music_assistant", api_id=API_ID, api_hash=API_HASH, session_string=ASSISTANT_SESSION, in_memory=True)
    calls = PyTgCalls(assistant)
    queues = QueueManager()
    player = VoiceChatPlayer(calls, queues)

    register_handlers(bot, assistant, player, queues)

    await assistant.start()
    await calls.start()
    await bot.start()

    log.info("Bot and assistant are online. Multi-group voice chat music is ready.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
