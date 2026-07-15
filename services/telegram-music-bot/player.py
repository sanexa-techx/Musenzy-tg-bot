"""Wraps py-tgcalls to join/stream/leave group voice chats per group."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, Update
from pytgcalls.types.stream import StreamEnded

from queue_manager import QueueManager, Track
from youtube import cleanup_file

log = logging.getLogger("player")


class VoiceChatPlayer:
    """Owns the single PyTgCalls instance (bound to the assistant account) and
    coordinates per-chat queues so multiple groups can play independently."""

    def __init__(self, calls: PyTgCalls, queues: QueueManager) -> None:
        self.calls = calls
        self.queues = queues
        self.calls.on_update(self._on_stream_end)
        # Notified whenever a track actually starts streaming (initial /play
        # and every automatic/manual advance), so the chat layer can post a
        # fresh now-playing message with a live progress bar.
        self.on_track_start: Optional[Callable[[int, Track], Awaitable[None]]] = None
        # Notified once the queue runs out and the assistant leaves the
        # voice chat -- covers natural end-of-queue, /skip, and the skip
        # button, so the chat layer can stop the progress tracker and post
        # a single "leaving" message from one place.
        self.on_queue_empty: Optional[Callable[[int], Awaitable[None]]] = None

    async def _on_stream_end(self, _client: PyTgCalls, update: Update) -> None:
        if not isinstance(update, StreamEnded):
            return
        chat_id = update.chat_id
        current = self.queues.state(chat_id).current
        if current:
            cleanup_file(current.file_path)
        await self.play_next(chat_id)

    async def play_or_enqueue(self, chat_id: int, track: Track) -> int:
        position = self.queues.enqueue(chat_id, track)
        if position == 0:
            await self._start(chat_id, track)
        return position

    async def _start(self, chat_id: int, track: Track) -> None:
        state = self.queues.state(chat_id)
        state.paused = False
        try:
            await self.calls.play(chat_id, MediaStream(track.file_path))
        except Exception:
            log.exception("Failed to join/play voice chat for %s", chat_id)
            raise
        if self.on_track_start:
            await self.on_track_start(chat_id, track)

    async def play_next(self, chat_id: int) -> Track | None:
        nxt = self.queues.next_track(chat_id)
        if nxt is None:
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                pass
            if self.on_queue_empty:
                await self.on_queue_empty(chat_id)
            return None
        await self._start(chat_id, nxt)
        return nxt

    async def pause(self, chat_id: int) -> None:
        await self.calls.pause_stream(chat_id)
        self.queues.state(chat_id).paused = True

    async def resume(self, chat_id: int) -> None:
        await self.calls.resume_stream(chat_id)
        self.queues.state(chat_id).paused = False

    async def stop(self, chat_id: int) -> None:
        current = self.queues.state(chat_id).current
        if current:
            cleanup_file(current.file_path)
        for track in self.queues.state(chat_id).queue:
            cleanup_file(track.file_path)
        self.queues.clear(chat_id)
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
