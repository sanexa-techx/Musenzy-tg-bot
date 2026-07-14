"""Inline keyboard builders for player controls."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
