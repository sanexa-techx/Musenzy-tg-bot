"""Per-user named playlist storage — persists across restarts."""
from __future__ import annotations

import contextlib
import json
import os

_STATE_FILE = os.path.join(os.path.dirname(__file__), "user_playlists.json")

# Maximum tracks the bot will queue from a single playlist.
MAX_PLAYLIST_TRACKS = 50


def _load() -> dict[str, dict[str, str]]:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with contextlib.suppress(Exception):
        with open(_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)


class PlaylistManager:
    """Stores named YouTube playlist URLs per Telegram user."""

    def __init__(self) -> None:
        raw = _load()
        # { str(user_id): { name: url } }
        self._data: dict[str, dict[str, str]] = raw

    def _user(self, user_id: int) -> dict[str, str]:
        key = str(user_id)
        if key not in self._data:
            self._data[key] = {}
        return self._data[key]

    def save(self, user_id: int, name: str, url: str) -> None:
        """Save or overwrite a named playlist for this user."""
        self._user(user_id)[name.lower()] = url
        _save(self._data)

    def get(self, user_id: int, name: str) -> str | None:
        """Return the URL for a saved playlist name, or None."""
        return self._user(user_id).get(name.lower())

    def list_playlists(self, user_id: int) -> dict[str, str]:
        """Return all saved playlists for a user as {name: url}."""
        return dict(self._user(user_id))

    def delete(self, user_id: int, name: str) -> bool:
        """Delete a named playlist. Returns True if it existed."""
        playlists = self._user(user_id)
        if name.lower() in playlists:
            del playlists[name.lower()]
            _save(self._data)
            return True
        return False
