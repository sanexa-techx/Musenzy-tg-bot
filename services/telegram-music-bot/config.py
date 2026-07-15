"""Environment configuration for the voice chat music bot."""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in Replit Secrets before starting the bot."
        )
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name) or None


API_ID = int(_require("TELEGRAM_API_ID"))
API_HASH = _require("TELEGRAM_API_HASH")
BOT_TOKEN = _optional("TELEGRAM_BOT_TOKEN")

# The "assistant" is a normal Telegram user account session. Telegram's Bot API
# cannot join or stream audio into a group voice chat -- only a user account
# (MTProto client) can. The bot handles commands/buttons; the assistant is the
# account that actually joins the voice chat and plays audio.
#
# This is optional at import time so generate_session.py (which only needs
# API_ID/API_HASH) can run before this value exists. main.py enforces that it
# is actually set before starting the bot.
ASSISTANT_SESSION = _optional("TELEGRAM_SESSION_STRING")

# Optional: restrict playback duration to protect against extremely long videos.
MAX_TRACK_SECONDS = int(os.environ.get("MAX_TRACK_SECONDS", "10800"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "services/telegram-music-bot/downloads")

# Branding shown in the private-chat welcome message.
LOGO_PATH = os.environ.get("LOGO_PATH", "services/telegram-music-bot/assets/logo.jpg")
OWNER_URL = os.environ.get("OWNER_URL", "https://t.me/NONKO_0")
SUPPORT_GROUP_URL = os.environ.get("SUPPORT_GROUP_URL", "https://t.me/+-uT8Owz9aKg5N2M1")
