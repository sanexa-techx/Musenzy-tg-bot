"""Live, updating "now playing" progress bar for the currently streaming track.

Keeps one tracking session per chat and periodically edits the now-playing
message so it looks like a real music player (progress bar + elapsed/total
time), accounting correctly for time spent paused.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from pyrogram import enums
from dataclasses import dataclass, field
from typing import Any, Callable

BAR_LENGTH = 12
UPDATE_INTERVAL_SECONDS = 5


def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def render_bar(elapsed: int, duration: int, paused: bool, length: int = BAR_LENGTH) -> str:
    icon = "⏸" if paused else "▶️"
    if not duration:
        return f"{icon}  🔴 <b>LIVE</b>  •  <code>{format_time(elapsed)}</code>"
    ratio = min(1.0, elapsed / duration) if duration else 0.0
    filled = min(length - 1, int(ratio * length))
    bar = "▬" * filled + "🔘" + "▬" * (length - filled - 1)
    return (
        f"{icon}  {bar}\n"
        f"<code>{format_time(elapsed)}</code>  /  <code>{format_time(duration)}</code>"
    )


@dataclass
class _Session:
    message: Any
    duration: int
    caption_prefix: str
    reply_markup_fn: Callable[[], Any]
    started_at: float = field(default_factory=time.monotonic)
    paused_at: float | None = None
    paused_total: float = 0.0
    task: asyncio.Task | None = None

    def elapsed(self) -> int:
        now = time.monotonic()
        paused_total = self.paused_total
        if self.paused_at is not None:
            paused_total += now - self.paused_at
        return int(now - self.started_at - paused_total)


class NowPlayingTracker:
    """Drives a live-updating progress bar for each chat's now-playing message."""

    def __init__(self) -> None:
        self._sessions: dict[int, _Session] = {}

    def start(
        self,
        chat_id: int,
        message: Any,
        duration: int,
        caption_prefix: str,
        reply_markup_fn: Callable[[], Any],
    ) -> None:
        self.stop(chat_id)
        session = _Session(
            message=message,
            duration=duration,
            caption_prefix=caption_prefix,
            reply_markup_fn=reply_markup_fn,
        )
        self._sessions[chat_id] = session
        session.task = asyncio.create_task(self._run(chat_id))

    def stop(self, chat_id: int) -> None:
        session = self._sessions.pop(chat_id, None)
        if session and session.task:
            session.task.cancel()

    def pause(self, chat_id: int) -> None:
        session = self._sessions.get(chat_id)
        if session and session.paused_at is None:
            session.paused_at = time.monotonic()

    def resume(self, chat_id: int) -> None:
        session = self._sessions.get(chat_id)
        if session and session.paused_at is not None:
            session.paused_total += time.monotonic() - session.paused_at
            session.paused_at = None

    async def _run(self, chat_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(UPDATE_INTERVAL_SECONDS)
                session = self._sessions.get(chat_id)
                if session is None:
                    return
                elapsed = session.elapsed()
                if session.duration and elapsed >= session.duration:
                    return
                paused = session.paused_at is not None
                text = f"{session.caption_prefix}\n\n{render_bar(elapsed, session.duration, paused)}"
                with contextlib.suppress(Exception):
                    if getattr(session.message, "photo", None):
                        await session.message.edit_caption(
                            text, reply_markup=session.reply_markup_fn(),
                            parse_mode=enums.ParseMode.HTML,
                        )
                    else:
                        await session.message.edit_text(
                            text, reply_markup=session.reply_markup_fn(),
                            parse_mode=enums.ParseMode.HTML,
                        )
        except asyncio.CancelledError:
            pass
