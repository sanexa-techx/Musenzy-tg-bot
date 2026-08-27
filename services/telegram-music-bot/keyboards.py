"""Inline keyboard builders for player controls."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_URL, SUPPORT_GROUP_URL
from progress import render_bar_button


def broadcast_schedule_menu(active_hours: int = 0) -> InlineKeyboardMarkup:
    """Keyboard shown after the owner composes a broadcast message.

    *active_hours* – if a schedule is already running, that button shows a
    checkmark so the owner knows the current setting.
    """
    def _label(hours: int, label: str) -> str:
        return f"✅ {label}" if active_hours == hours else label

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Send Now", callback_data="bcast:now"),
        ],
        [
            InlineKeyboardButton(_label(1, "⏰ Every 1h"), callback_data="bcast:1"),
            InlineKeyboardButton(_label(2, "⏰ Every 2h"), callback_data="bcast:2"),
            InlineKeyboardButton(_label(3, "⏰ Every 3h"), callback_data="bcast:3"),
        ],
        [
            InlineKeyboardButton("🚫 Cancel Schedule", callback_data="bcast:cancel"),
            InlineKeyboardButton("✖️ Close", callback_data="bcast:close"),
        ],
    ])


def welcome_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Menu", callback_data="menu:commands")],
            [
                InlineKeyboardButton("Owner", url=OWNER_URL),
                InlineKeyboardButton("Support Group", url=SUPPORT_GROUP_URL),
            ],
        ]
    )


_FALLBACK_URL = "https://youtube.com"


def player_controls(
    paused: bool,
    elapsed: int = 0,
    duration: int = 0,
    track_url: str = "",
) -> InlineKeyboardMarkup:
    bar_label = render_bar_button(elapsed, duration, paused)
    # URL buttons render in Telegram's accent colour (blue).
    # Callback buttons render in the neutral/grey message colour.
    bar_url = track_url if track_url else _FALLBACK_URL
    return InlineKeyboardMarkup(
        [
            [
                # Full-width BLUE progress bar — URL button opens the song link on tap.
                InlineKeyboardButton(bar_label, url=bar_url),
            ],
            [
                InlineKeyboardButton("▶️" if paused else "⏸", callback_data="ctl:pauseresume"),
                InlineKeyboardButton("⏭", callback_data="ctl:skip"),
                InlineKeyboardButton("⏹", callback_data="ctl:stop"),
                InlineKeyboardButton("🎵", callback_data="ctl:queue"),
                InlineKeyboardButton("✖️", callback_data="ctl:close"),
            ],
            [
                InlineKeyboardButton("🔵 Add Playlist+", callback_data="ctl:addplaylist"),
            ],
        ]
    )


def player_controls_api(
    paused: bool,
    elapsed: int = 0,
    duration: int = 0,
    track_url: str = "",
) -> dict:
    """Bot API 9.4 keyboard with actual button background styles.

    ``primary`` is blue, ``success`` is green, and ``danger`` is red.
    This is a JSON payload because the installed Pyrofork model predates the
    Bot API 9.4 ``style`` property.
    """
    bar_label = render_bar_button(elapsed, duration, paused)
    bar_url = track_url if track_url else _FALLBACK_URL

    def button(
        text: str,
        *,
        style: str,
        callback_data: str | None = None,
        url: str | None = None,
    ) -> dict:
        item = {"text": text, "style": style}
        if callback_data is not None:
            item["callback_data"] = callback_data
        if url is not None:
            item["url"] = url
        return item

    return {
        "inline_keyboard": [
            [button(bar_label, style="primary", url=bar_url)],
            [
                button(
                    "▶️" if paused else "⏸",
                    style="success",
                    callback_data="ctl:pauseresume",
                ),
                button("⏭", style="primary", callback_data="ctl:skip"),
                button("⏹", style="danger", callback_data="ctl:stop"),
                button("🎵", style="primary", callback_data="ctl:queue"),
                button("✖️", style="danger", callback_data="ctl:close"),
            ],
            [
                button(
                    "🔵 Add Playlist+",
                    style="primary",
                    callback_data="ctl:addplaylist",
                ),
            ],
        ]
    }
