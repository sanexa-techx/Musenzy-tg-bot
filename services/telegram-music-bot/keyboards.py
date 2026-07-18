"""Inline keyboard builders for player controls."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_URL, SUPPORT_GROUP_URL


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


def player_controls(paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
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
