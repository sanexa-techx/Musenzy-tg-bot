"""Broadcast management: send a message to every group the bot is in,
optionally on a repeating schedule (every 1 / 2 / 3 hours)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from pyrogram import enums

log = logging.getLogger("broadcast")

_CHATS_FILE = os.path.join(os.path.dirname(__file__), "known_chats.json")


def _load_chats() -> set[int]:
    try:
        with open(_CHATS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_chats(chats: set[int]) -> None:
    with contextlib.suppress(Exception):
        with open(_CHATS_FILE, "w") as f:
            json.dump(list(chats), f)


class BroadcastManager:
    """Tracks known group chats and manages scheduled / on-demand broadcasts."""

    def __init__(self) -> None:
        self._chats: set[int] = _load_chats()
        self._schedule_task: asyncio.Task | None = None
        self._schedule_interval: int = 0          # hours
        self._pending_text: dict[int, str] = {}   # owner_id → pending message text

    # ------------------------------------------------------------------
    # Chat tracking
    # ------------------------------------------------------------------

    def register_chat(self, chat_id: int) -> None:
        """Call whenever the bot receives an update from a group."""
        if chat_id not in self._chats:
            self._chats.add(chat_id)
            _save_chats(self._chats)

    def known_chats(self) -> list[int]:
        return list(self._chats)

    # ------------------------------------------------------------------
    # Pending message state (owner sets text, then picks schedule)
    # ------------------------------------------------------------------

    def set_pending(self, owner_id: int, text: str) -> None:
        self._pending_text[owner_id] = text

    def get_pending(self, owner_id: int) -> str | None:
        return self._pending_text.get(owner_id)

    def clear_pending(self, owner_id: int) -> None:
        self._pending_text.pop(owner_id, None)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def send_now(self, bot: Any, text: str) -> tuple[int, int]:
        """Send *text* to every known group. Returns (sent, failed) counts."""
        sent = failed = 0
        for chat_id in list(self._chats):
            try:
                await bot.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
                sent += 1
            except Exception as exc:
                log.warning("Broadcast failed for chat %s: %s", chat_id, exc)
                failed += 1
            await asyncio.sleep(0.05)   # stay well under Telegram rate limits
        return sent, failed

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def schedule(self, bot: Any, text: str, hours: int) -> None:
        """Start (or replace) a repeating broadcast every *hours* hours."""
        self._cancel_task()
        self._schedule_interval = hours
        self._schedule_task = asyncio.create_task(self._run(bot, text, hours))
        log.info("Broadcast scheduled every %dh", hours)

    def cancel_schedule(self) -> bool:
        """Cancel any active schedule. Returns True if one was running."""
        had = self._schedule_task is not None and not self._schedule_task.done()
        self._cancel_task()
        self._schedule_interval = 0
        return had

    def active_schedule_hours(self) -> int:
        """Return the current schedule interval in hours, or 0 if none."""
        if self._schedule_task and not self._schedule_task.done():
            return self._schedule_interval
        return 0

    def _cancel_task(self) -> None:
        if self._schedule_task and not self._schedule_task.done():
            self._schedule_task.cancel()
        self._schedule_task = None

    async def _run(self, bot: Any, text: str, hours: int) -> None:
        try:
            while True:
                await asyncio.sleep(hours * 3600)
                log.info("Running scheduled broadcast (every %dh)", hours)
                await self.send_now(bot, text)
        except asyncio.CancelledError:
            pass
