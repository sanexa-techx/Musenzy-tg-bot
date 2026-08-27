"""Single long-lived process version of session generation.

Keeps one connected Pyrogram client alive for the whole login flow (send code
-> wait for code -> sign in -> optional 2FA), instead of reconnecting a fresh
process per step. Reconnecting fresh processes was invalidating the code
before it could be used.

Usage:
  python services/telegram-music-bot/generate_session_interactive.py <phone>

It polls small text files for input instead of stdin (so an external process
can feed it the code once the user provides it):
  services/telegram-music-bot/.session_tmp/code_input.txt      -- the login code
  services/telegram-music-bot/.session_tmp/password_input.txt  -- 2FA password, if needed

Progress / result is written to:
  services/telegram-music-bot/.session_tmp/result.txt
"""
import asyncio
import os
import sys

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded

from config import API_HASH, API_ID

STATE_DIR = os.path.abspath("services/telegram-music-bot/.session_tmp")
os.makedirs(STATE_DIR, exist_ok=True)

CODE_FILE = os.path.join(STATE_DIR, "code_input.txt")
PASSWORD_FILE = os.path.join(STATE_DIR, "password_input.txt")
RESULT_FILE = os.path.join(STATE_DIR, "result.txt")


def _write_result(text: str) -> None:
    with open(RESULT_FILE, "w") as f:
        f.write(text)
    print(text, flush=True)


async def _wait_for_file(path: str, timeout: int = 300) -> str:
    waited = 0
    while waited < timeout:
        if os.path.exists(path):
            with open(path) as f:
                value = f.read().strip()
            os.remove(path)
            if value:
                return value
        await asyncio.sleep(1)
        waited += 1
    raise TimeoutError(f"Timed out waiting for {path}")


async def main(phone: str) -> None:
    for f in (CODE_FILE, PASSWORD_FILE, RESULT_FILE):
        if os.path.exists(f):
            os.remove(f)

    client = Client(f"live_{phone.lstrip('+')}", api_id=API_ID, api_hash=API_HASH, workdir=STATE_DIR, in_memory=False)
    await client.connect()

    sent = await client.send_code(phone)
    _write_result(f"WAITING_FOR_CODE sent to {phone}")

    code = await _wait_for_file(CODE_FILE)

    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except SessionPasswordNeeded:
        _write_result("WAITING_FOR_PASSWORD 2FA required")
        password = await _wait_for_file(PASSWORD_FILE)
        await client.check_password(password)

    session_string = await client.export_session_string()
    await client.disconnect()
    _write_result(f"SESSION_STRING: {session_string}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
