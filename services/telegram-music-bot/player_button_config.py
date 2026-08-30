"""Persistent owner customization for the now-playing keyboard."""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any


_STATE_FILE = os.path.join(
    os.path.dirname(__file__), "player_button_settings.json"
)

DEFAULT_LABELS: dict[str, str] = {
    "pause": "⏸",
    "resume": "▶️",
    "skip": "⏭",
    "stop": "⏹",
    "queue": "🎵",
    "close": "✖️",
    "autoplay_on": "✔️ 𝙰𝚄𝚃𝙾",
    "autoplay_off": "𝙰𝚄𝚃𝙾",
}

DEFAULT_STYLES: dict[str, str] = {
    "pause": "success",
    "resume": "success",
    "skip": "primary",
    "stop": "danger",
    "queue": "primary",
    "close": "danger",
    "autoplay_on": "success",
    "autoplay_off": "danger",
}

BUTTON_NAMES: dict[str, str] = {
    "pause": "Pause",
    "resume": "Resume",
    "skip": "Skip",
    "stop": "Stop",
    "queue": "Queue",
    "close": "Close",
    "autoplay_on": "Autoplay enabled",
    "autoplay_off": "Autoplay disabled",
}

BUTTON_ALIASES: dict[str, str] = {
    "pause": "pause",
    "play": "resume",
    "resume": "resume",
    "skip": "skip",
    "next": "skip",
    "stop": "stop",
    "queue": "queue",
    "close": "close",
    "x": "close",
    "autoplay": "autoplay_off",
    "auto": "autoplay_off",
    "autoplay_on": "autoplay_on",
    "auto_on": "autoplay_on",
    "autoplay_off": "autoplay_off",
    "auto_off": "autoplay_off",
}

VALID_STYLES = frozenset({"primary", "success", "danger"})


def _load() -> dict[str, Any]:
    try:
        with open(_STATE_FILE, encoding="utf-8") as state_file:
            raw = json.load(state_file)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save(labels: dict[str, str], styles: dict[str, str]) -> None:
    payload = {"labels": labels, "styles": styles}
    temporary = f"{_STATE_FILE}.tmp"
    with contextlib.suppress(Exception):
        with open(temporary, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False, indent=2)
        os.replace(temporary, _STATE_FILE)


class PlayerButtonSettings:
    """Owner-editable labels and Bot API styles for player controls."""

    def __init__(self) -> None:
        raw = _load()
        saved_labels = raw.get("labels", {})
        saved_styles = raw.get("styles", {})
        self.labels = dict(DEFAULT_LABELS)
        self.styles = dict(DEFAULT_STYLES)

        if isinstance(saved_labels, dict):
            for key, value in saved_labels.items():
                if key in self.labels and isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        self.labels[key] = self.validate_label(value)
        if isinstance(saved_styles, dict):
            for key, value in saved_styles.items():
                if key in self.styles and value in VALID_STYLES:
                    self.styles[key] = value

    @staticmethod
    def validate_label(label: str) -> str:
        label = label.strip()
        if not label:
            raise ValueError("The button label cannot be empty.")
        if len(label) > 64:
            raise ValueError("The button label must be 64 characters or fewer.")
        if any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise ValueError("Button labels cannot contain line breaks or control characters.")
        return label

    @staticmethod
    def resolve_key(name: str) -> str | None:
        return BUTTON_ALIASES.get(name.strip().lower())

    def label(self, key: str) -> str:
        return self.labels[key]

    def style(self, key: str) -> str:
        return self.styles[key]

    def set_label(self, name: str, label: str) -> str:
        key = self.resolve_key(name)
        if key is None:
            raise ValueError("Unknown button name.")
        self.labels[key] = self.validate_label(label)
        _save(self.labels, self.styles)
        return key

    def set_style(self, name: str, style: str) -> str:
        key = self.resolve_key(name)
        if key is None:
            raise ValueError("Unknown button name.")
        style = style.strip().lower()
        if style not in VALID_STYLES:
            raise ValueError("Style must be primary, success, or danger.")
        self.styles[key] = style
        _save(self.labels, self.styles)
        return key

    def reset(self) -> None:
        self.labels = dict(DEFAULT_LABELS)
        self.styles = dict(DEFAULT_STYLES)
        _save(self.labels, self.styles)


_SETTINGS = PlayerButtonSettings()


def get_player_button_settings() -> PlayerButtonSettings:
    """Return the process-wide settings instance used by all keyboards."""
    return _SETTINGS