"""Inline keyboard builders for player controls."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_URL, SUPPORT_GROUP_URL
from player_button_config import get_player_button_settings
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


def player_button_editor_menu() -> InlineKeyboardMarkup:
    """Owner menu for editing the now-playing keyboard labels."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Pause", callback_data="pbtn:edit:pause"),
                InlineKeyboardButton("✏️ Resume", callback_data="pbtn:edit:resume"),
            ],
            [
                InlineKeyboardButton("✏️ Skip", callback_data="pbtn:edit:skip"),
                InlineKeyboardButton("✏️ Stop", callback_data="pbtn:edit:stop"),
            ],
            [
                InlineKeyboardButton("✏️ Queue", callback_data="pbtn:edit:queue"),
                InlineKeyboardButton("✏️ Close", callback_data="pbtn:edit:close"),
            ],
            [
                InlineKeyboardButton("✏️ Auto ON", callback_data="pbtn:edit:autoplay_on"),
                InlineKeyboardButton("✏️ Auto OFF", callback_data="pbtn:edit:autoplay_off"),
            ],
            [
                InlineKeyboardButton("✏️ Fav", callback_data="pbtn:edit:fav"),
                InlineKeyboardButton("✏️ Play now", callback_data="pbtn:edit:play_now"),
            ],
            [
                InlineKeyboardButton("🎨 Style help", callback_data="pbtn:stylehelp"),
                InlineKeyboardButton("👁 Preview", callback_data="pbtn:preview"),
            ],
            [
                InlineKeyboardButton("📝 Edit card text", callback_data="pbtn:cardhelp"),
            ],
            [
                InlineKeyboardButton("♻️ Reset all", callback_data="pbtn:reset"),
                InlineKeyboardButton("✖️ Close", callback_data="pbtn:close"),
            ],
        ]
    )


def player_card_editor_menu() -> InlineKeyboardMarkup:
    """Owner menu for editing the text and symbols above the player buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Now playing", callback_data="pbtn:editcard:now_playing"),
                InlineKeyboardButton("✏️ Queue heading", callback_data="pbtn:editcard:queued"),
            ],
            [
                InlineKeyboardButton("✏️ Song label", callback_data="pbtn:editcard:song_prefix"),
                InlineKeyboardButton("✏️ Time label", callback_data="pbtn:editcard:time_prefix"),
            ],
            [
                InlineKeyboardButton("✏️ Requested by", callback_data="pbtn:editcard:requester_prefix"),
                InlineKeyboardButton("✏️ Divider", callback_data="pbtn:editcard:divider"),
            ],
            [
                InlineKeyboardButton("✏️ Separator", callback_data="pbtn:editcard:separator"),
                InlineKeyboardButton("📐 Playing layout", callback_data="pbtn:layout:playing"),
            ],
            [
                InlineKeyboardButton("📐 Queue layout", callback_data="pbtn:layout:queue"),
            ],
            [
                InlineKeyboardButton("↩️ Back to buttons", callback_data="pbtn:back"),
            ],
        ]
    )


def queue_card_controls(track_key: str) -> InlineKeyboardMarkup:
    """Fallback keyboard for a queued-track request card."""
    settings = get_player_button_settings()
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                settings.label("play_now"),
                callback_data=f"q:play:{track_key}",
            ),
            InlineKeyboardButton(
                settings.label("fav"),
                callback_data=f"q:fav:{track_key}",
            ),
        ]]
    )


def queue_card_controls_api(track_key: str) -> dict:
    """Bot API keyboard for queued cards, including styled action buttons."""
    settings = get_player_button_settings()

    def button(text: str, style: str, callback_data: str) -> dict:
        return {
            "text": text,
            "style": style,
            "callback_data": callback_data,
        }

    return {
        "inline_keyboard": [[
            button(
                settings.label("play_now"),
                settings.style("play_now"),
                f"q:play:{track_key}",
            ),
            button(
                settings.label("fav"),
                settings.style("fav"),
                f"q:fav:{track_key}",
            ),
        ]]
    }


_FALLBACK_URL = "https://youtube.com"


def player_controls(
    paused: bool,
    elapsed: int = 0,
    duration: int = 0,
    track_url: str = "",
    autoplay_enabled: bool = False,
) -> InlineKeyboardMarkup:
    settings = get_player_button_settings()
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
                InlineKeyboardButton(
                    settings.label("resume" if paused else "pause"),
                    callback_data="ctl:pauseresume",
                ),
                InlineKeyboardButton(settings.label("skip"), callback_data="ctl:skip"),
                InlineKeyboardButton(settings.label("stop"), callback_data="ctl:stop"),
                InlineKeyboardButton(settings.label("queue"), callback_data="ctl:queue"),
                InlineKeyboardButton(settings.label("close"), callback_data="ctl:close"),
            ],
            [
                InlineKeyboardButton(
                    settings.label("autoplay_on" if autoplay_enabled else "autoplay_off"),
                    callback_data="ctl:autoplay",
                ),
                InlineKeyboardButton(
                    settings.label("fav"),
                    callback_data="ctl:fav",
                ),
            ],
        ]
    )


def player_controls_api(
    paused: bool,
    elapsed: int = 0,
    duration: int = 0,
    track_url: str = "",
    autoplay_enabled: bool = False,
) -> dict:
    """Bot API 9.4 keyboard with actual button background styles.

    ``primary`` is blue, ``success`` is green, and ``danger`` is red.
    This is a JSON payload because the installed Pyrofork model predates the
    Bot API 9.4 ``style`` property.
    """
    settings = get_player_button_settings()
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
                    settings.label("resume" if paused else "pause"),
                    style=settings.style("resume" if paused else "pause"),
                    callback_data="ctl:pauseresume",
                ),
                button(
                    settings.label("skip"),
                    style=settings.style("skip"),
                    callback_data="ctl:skip",
                ),
                button(
                    settings.label("stop"),
                    style=settings.style("stop"),
                    callback_data="ctl:stop",
                ),
                button(
                    settings.label("queue"),
                    style=settings.style("queue"),
                    callback_data="ctl:queue",
                ),
                button(
                    settings.label("close"),
                    style=settings.style("close"),
                    callback_data="ctl:close",
                ),
            ],
            [
                button(
                    settings.label("autoplay_on" if autoplay_enabled else "autoplay_off"),
                    style=settings.style("autoplay_on" if autoplay_enabled else "autoplay_off"),
                    callback_data="ctl:autoplay",
                ),
                button(
                    settings.label("fav"),
                    style=settings.style("fav"),
                    callback_data="ctl:fav",
                ),
            ],
        ]
    }
