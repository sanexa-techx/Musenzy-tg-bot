"""Per-chat autoplay state — persists across restarts."""
from __future__ import annotations

import contextlib
import json
import os

_STATE_FILE = os.path.join(os.path.dirname(__file__), "autoplay_state.json")


def _load() -> dict[str, bool]:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: dict[str, bool]) -> None:
    with contextlib.suppress(Exception):
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)


class AutoplayManager:
    """Tracks whether autoplay is enabled per chat_id."""

    def __init__(self) -> None:
        raw = _load()
        self._enabled: dict[int, bool] = {int(k): v for k, v in raw.items()}

    def toggle(self, chat_id: int) -> bool:
        """Flip autoplay on/off. Returns the new state (True = on)."""
        self._enabled[chat_id] = not self._enabled.get(chat_id, False)
        _save({str(k): v for k, v in self._enabled.items()})
        return self._enabled[chat_id]

    def is_enabled(self, chat_id: int) -> bool:
        return self._enabled.get(chat_id, False)

    def disable(self, chat_id: int) -> None:
        """Force-disable (used when autoplay fails or bot is stopped)."""
        if self._enabled.get(chat_id):
            self._enabled[chat_id] = False
            _save({str(k): v for k, v in self._enabled.items()})
