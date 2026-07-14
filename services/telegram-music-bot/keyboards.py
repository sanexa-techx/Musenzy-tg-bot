"""Inline keyboard builders for player controls."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_URL, SUPPORT_GROUP_URL


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
                InlineKeyboardButton("Resume" if paused else "Pause", callback_data="ctl:pauseresume"),
                InlineKeyboardButton("Skip", callback_data="ctl:skip"),
                InlineKeyboardButton("Stop", callback_data="ctl:stop"),
            ],
            [
                InlineKeyboardButton("Queue", callback_data="ctl:queue"),
            ],
        ]
    )
