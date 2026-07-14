"""Helper to generate the TELEGRAM_SESSION_STRING for the assistant account.

Telegram's Bot API cannot join or stream into a group voice chat -- only a
regular user account (via MTProto) can. This script logs in as that user
account once and prints a reusable session string, so the bot never needs
your password/OTP again after this step.

Usage (run each step manually, in order, from the shell):

  python services/telegram-music-bot/generate_session.py send +15551234567
      -> Telegram sends a login code to that account. Note the phone number
         you used.

  python services/telegram-music-bot/generate_session.py signin +15551234567 12345
      -> Completes login with the code you received. If the account has a
         2FA password, this will print a note asking you to run the next step.

  python services/telegram-music-bot/generate_session.py 2fa +15551234567 <password>
      -> Only needed if the account has a cloud/2FA password enabled.

The final successful step prints "SESSION_STRING: <value>" -- save that value
as the TELEGRAM_SESSION_STRING secret.
"""
import asyncio
import json
import os
import sys

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded

from config import API_HASH, API_ID

STATE_DIR = "services/telegram-music-bot/.session_tmp"
os.makedirs(STATE_DIR, exist_ok=True)


def _state_path(phone: str) -> str:
    safe = phone.replace("+", "").replace(" ", "")
    return os.path.join(STATE_DIR, f"{safe}.json")


def _session_name(phone: str) -> str:
    safe = phone.replace("+", "").replace(" ", "")
    return os.path.join(STATE_DIR, f"session_{safe}")


async def send(phone: str) -> None:
    client = Client(_session_name(phone), api_id=API_ID, api_hash=API_HASH)
    await client.connect()
    sent = await client.send_code(phone)
    with open(_state_path(phone), "w") as f:
        json.dump({"phone_code_hash": sent.phone_code_hash}, f)
    await client.disconnect()
    print(f"Code sent to {phone}. Now run: signin {phone} <code>")


async def signin(phone: str, code: str) -> None:
    with open(_state_path(phone)) as f:
        state = json.load(f)

    client = Client(_session_name(phone), api_id=API_ID, api_hash=API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone, state["phone_code_hash"], code)
    except SessionPasswordNeeded:
        await client.disconnect()
        print("This account has a 2FA password. Now run: 2fa " + phone + " <password>")
        return

    session_string = await client.export_session_string()
    await client.disconnect()
    print(f"SESSION_STRING: {session_string}")


async def twofa(phone: str, password: str) -> None:
    client = Client(_session_name(phone), api_id=API_ID, api_hash=API_HASH)
    await client.connect()
    await client.check_password(password)
    session_string = await client.export_session_string()
    await client.disconnect()
    print(f"SESSION_STRING: {session_string}")


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "send":
        asyncio.run(send(sys.argv[2]))
    elif action == "signin":
        asyncio.run(signin(sys.argv[2], sys.argv[3]))
    elif action == "2fa":
        asyncio.run(twofa(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit("Unknown action. Use send | signin | 2fa")
