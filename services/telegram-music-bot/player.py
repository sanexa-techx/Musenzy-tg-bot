"""Wraps py-tgcalls to join/stream/leave group voice chats per group."""
from __future__ import annotations

import asyncio
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
        # `on_update` is a decorator *factory* (`on_update(filters=None)` ->
        # decorator); calling it directly with the handler as `filters` never
        # registers anything. `add_handler` is the actual registration call.
        self.calls.add_handler(self._on_stream_end)
        # Notified whenever a track actually starts streaming (initial /play
        # and every automatic/manual advance), so the chat layer can post a
        # fresh now-playing message with a live progress bar.
        self.on_track_start: Optional[Callable[[int, Track], Awaitable[None]]] = None
        # Notified once the queue runs out and the assistant leaves the
        # voice chat -- covers natural end-of-queue, /skip, and the skip
        # button, so the chat layer can stop the progress tracker and post
        # a single "leaving" message from one place.
        self.on_queue_empty: Optional[Callable[[int], Awaitable[None]]] = None
        # Optional autoplay hook: called with (chat_id, last_track) when the
        # queue empties. If it returns a Track the player streams it instead
        # of leaving. If it returns None the normal on_queue_empty path runs.
        self.on_autoplay_next: Optional[Callable[[int, "Track"], Awaitable[Optional["Track"]]]] = None
        # Silent prefetch hook: same signature but called in the background
        # while the current track still plays so the next autoplay track is
        # ready the moment it's needed (no gap between songs).
        self.on_autoplay_prefetch: Optional[Callable[[int, "Track"], Awaitable[Optional["Track"]]]] = None
        # Per-chat locks: py-tgcalls can fire StreamEnded more than once for
        # the same track; the lock ensures only the first event is processed.
        self._stream_end_locks: dict[int, asyncio.Lock] = {}
        # Background prefetch state
        self._prefetch_tasks: dict[int, asyncio.Task] = {}
        self._prefetch_result: dict[int, Optional["Track"]] = {}

    async def _on_stream_end(self, _client: PyTgCalls, update: Update) -> None:
        if not isinstance(update, StreamEnded):
            return
        chat_id = update.chat_id
        lock = self._stream_end_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            # A StreamEnded for this chat is already being handled — drop duplicate.
            return
        async with lock:
            current = self.queues.state(chat_id).current
            if current:
                cleanup_file(current.file_path)
            await self.play_next(chat_id)

    async def play_or_enqueue(self, chat_id: int, track: Track) -> int:
        position = self.queues.enqueue(chat_id, track)
        if position == 0:
            await self._start(chat_id, track)
        return position

    def _cancel_prefetch(self, chat_id: int) -> None:
        task = self._prefetch_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
        self._prefetch_result.pop(chat_id, None)

    async def _do_prefetch(self, chat_id: int, track: Track) -> None:
        """Silently fetch the next autoplay track in the background."""
        try:
            result = await self.on_autoplay_prefetch(chat_id, track)  # type: ignore[misc]
        except Exception:
            result = None
        self._prefetch_result[chat_id] = result
        self._prefetch_tasks.pop(chat_id, None)

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
        # If the queue is now empty and autoplay prefetch is configured,
        # start silently fetching the next song while this one plays.
        if self.on_autoplay_prefetch and not self.queues.state(chat_id).queue:
            self._cancel_prefetch(chat_id)
            self._prefetch_tasks[chat_id] = asyncio.create_task(
                self._do_prefetch(chat_id, track)
            )

    async def play_next(self, chat_id: int) -> Track | None:
        # Save the current track before next_track() clears it — autoplay needs it.
        last_track = self.queues.state(chat_id).current
        nxt = self.queues.next_track(chat_id)
        if nxt is None:
            # Queue is empty — check prefetch buffer first for instant transition.
            if last_track and (self.on_autoplay_prefetch or self.on_autoplay_next):
                # Wait briefly for an in-flight prefetch (it usually finished already).
                prefetch_task = self._prefetch_tasks.get(chat_id)
                if prefetch_task and not prefetch_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=10.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

                prefetched = self._prefetch_result.pop(chat_id, None)
                self._cancel_prefetch(chat_id)

                if prefetched is not None:
                    # Instant — track was fetched while previous song played.
                    self.queues.enqueue(chat_id, prefetched)
                    await self._start(chat_id, prefetched)
                    return prefetched

                # Prefetch missed or failed — fall back to on_autoplay_next (shows messages).
                if self.on_autoplay_next:
                    try:
                        autoplay_track = await self.on_autoplay_next(chat_id, last_track)
                    except Exception:
                        autoplay_track = None
                    if autoplay_track is not None:
                        self.queues.enqueue(chat_id, autoplay_track)
                        await self._start(chat_id, autoplay_track)
                        return autoplay_track

            # No autoplay or nothing found — leave normally.
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                pass
            if self.on_queue_empty:
                await self.on_queue_empty(chat_id)
            return None

        # There's a queued track — cancel any pending prefetch (not needed).
        self._cancel_prefetch(chat_id)
        await self._start(chat_id, nxt)
        return nxt

    async def pause(self, chat_id: int) -> None:
        await self.calls.pause(chat_id)
        self.queues.state(chat_id).paused = True

    async def resume(self, chat_id: int) -> None:
        await self.calls.resume(chat_id)
        self.queues.state(chat_id).paused = False

    async def stop(self, chat_id: int) -> None:
        self._cancel_prefetch(chat_id)
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
