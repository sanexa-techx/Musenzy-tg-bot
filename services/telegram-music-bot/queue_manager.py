"""Per-chat playback queue, shared across all groups the bot is active in."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    duration: int
    thumbnail: str | None
    requested_by: str
    file_path: str


@dataclass
class ChatState:
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    paused: bool = False


class QueueManager:
    """Keeps independent playback state per Telegram group chat_id."""

    def __init__(self) -> None:
        self._states: dict[int, ChatState] = {}

    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self._states:
            self._states[chat_id] = ChatState()
        return self._states[chat_id]

    def enqueue(self, chat_id: int, track: Track) -> int:
        """Add a track to the queue. Returns its position (0 = playing now)."""
        state = self.state(chat_id)
        if state.current is None:
            state.current = track
            return 0
        state.queue.append(track)
        return len(state.queue)

    def next_track(self, chat_id: int) -> Track | None:
        """Advance to the next track in queue, returning it (or None if empty)."""
        state = self.state(chat_id)
        if state.queue:
            state.current = state.queue.pop(0)
        else:
            state.current = None
        state.paused = False
        return state.current

    def clear(self, chat_id: int) -> None:
        state = self.state(chat_id)
        state.queue.clear()
        state.current = None
        state.paused = False

    def active_chats(self) -> list[int]:
        return [cid for cid, s in self._states.items() if s.current is not None]
