"""Command and callback handlers for the music bot."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import html
import logging

from pyrogram import Client, enums, filters
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, UserAlreadyParticipant, UserNotParticipant
from pyrogram.types import CallbackQuery, Message

from autoplay import AutoplayManager
from bot_api import BotApiClient, BotApiMessage
from broadcast import BroadcastManager
from config import BOT_TOKEN, LOGO_PATH, OWNER_ID
from favorites import FavoriteTrack, FavoritesStore
from keyboards import (
    broadcast_schedule_menu,
    player_card_editor_menu,
    player_controls,
    player_controls_api,
    player_button_editor_menu,
    queue_card_controls,
    queue_card_controls_api,
    welcome_menu,
)
from player import VoiceChatPlayer
from player_button_config import (
    BUTTON_NAMES,
    CARD_TEXT_NAMES,
    get_player_button_settings,
)
from progress import NowPlayingTracker
from queue_manager import QueueManager, Track
from youtube import (
    TrackNotFound, TrackTooLong, YouTubeBlocked,
    download_thumbnail, get_related_track,
    resolve_and_download, resolve_stream_url, resolve_video_stream_url,
    _extract_video_id,
)

log = logging.getLogger("handlers")

# Module-level dedup sets — survive handler re-registration and are shared
# across all closures so a duplicate delivery is always caught.
_seen_message_ids: set[int] = set()
_seen_callback_ids: set[str] = set()

COMMANDS_TEXT = (
    "Commands:\n"
    "/play <song name or link> -- play or queue a track\n"
    "/vplay <video name or link> -- play video in the voice chat\n"
    "/skip -- skip the current track\n"
    "/pause -- pause playback\n"
    "/resume -- resume playback\n"
    "/stop -- stop and leave the voice chat\n"
    "/queue -- show the current queue\n"
    "/favplay -- queue all favorite songs in order\n"
    "/favplay <number> -- play one favorite song\n"
    "/autoplay -- toggle related-song autoplay\n"
    "/stopautoplay -- turn autoplay off"
)


def _format_duration(seconds: int) -> str:
    if not seconds:
        return "Live"
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _format_track(track: Track, position: int | None = None) -> str:
    settings = get_player_button_settings()
    duration = _format_duration(track.duration)
    title = html.escape(track.title)
    requester_name = html.escape(track.requested_by or "someone")
    requester = (
        f'<a href="tg://user?id={track.requester_id}">{requester_name}</a>'
        if track.requester_id
        else requester_name
    )
    heading_key = "now_playing" if position is None else "queued"
    heading = f"<b>{html.escape(settings.card(heading_key))}</b>"
    if position is not None:
        heading += f"  <code>#{position}</code>"

    rendered = {
        "heading": heading,
        "divider": html.escape(settings.card("divider")),
        "song": f"{html.escape(settings.card('song_prefix'))} <b>{title}</b>",
        "time": f"{html.escape(settings.card('time_prefix'))} <code>{duration}</code>",
        "requester": f"{html.escape(settings.card('requester_prefix'))} {requester}",
        "meta": (
            f"{html.escape(settings.card('time_prefix'))} <code>{duration}</code>"
            f"   {html.escape(settings.card('separator'))}   "
            f"{html.escape(settings.card('requester_prefix'))} {requester}"
        ),
        "spacer": "",
    }
    return "\n".join(rendered[item] for item in settings.layout(
        "playing" if position is None else "queue"
    ))


def _track_callback_key(track: Track) -> str:
    """Return a compact stable key that fits Telegram callback data limits."""
    video_id = _extract_video_id(track.url)
    if video_id:
        return video_id
    return hashlib.sha1(track.url.encode("utf-8")).hexdigest()[:16]


async def _is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    """Return True if user_id is an admin or creator in chat_id."""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status.value in ("administrator", "owner", "creator")
    except Exception:
        return False


async def _send_and_delete(chat_id: int, bot: Client, text: str, delay: int = 8) -> None:
    """Send a temporary message and delete it after `delay` seconds."""
    try:
        msg = await bot.send_message(chat_id, text)
        await asyncio.sleep(delay)
        with contextlib.suppress(Exception):
            await msg.delete()
    except Exception:
        pass


_SEARCH_EMOJIS = ["🦋", "🕊️", "👾"]


async def _animate_searching(status: Message, query: str) -> None:
    """Cycle the status message through a small emoji-only animation while
    the track is being resolved and downloaded."""
    i = 0
    while True:
        # Sleep first — the initial "Searching..." text is already visible.
        # 5 s between edits keeps us well under Telegram's EditMessage flood limit.
        await asyncio.sleep(5)
        emoji = _SEARCH_EMOJIS[i % len(_SEARCH_EMOJIS)]
        with contextlib.suppress(Exception):
            await status.edit_text(emoji)
        i += 1


async def _ensure_assistant_in_chat(client: Client, assistant: Client, chat_id: int) -> str | None:
    """Make sure the music assistant account is a member of this chat, joining it
    automatically via a fresh invite link if it isn't yet. Returns an error
    message to show the user, or None on success."""
    try:
        await assistant.get_chat_member(chat_id, "me")
        return None
    except (UserNotParticipant, ChannelInvalid, ChannelPrivate):
        # ChannelInvalid / ChannelPrivate fires when the assistant has never
        # seen this chat before — treat it the same as not being a member.
        pass

    me = await client.get_chat_member(chat_id, "me")
    if not me.privileges or not me.privileges.can_invite_users:
        return (
            "I need to be an admin here with \"Invite users via link\" permission so I can bring "
            "the music assistant in automatically."
        )

    try:
        link = await client.create_chat_invite_link(chat_id, member_limit=1)
        await assistant.join_chat(link.invite_link)
    except UserAlreadyParticipant:
        return None
    except Exception:
        return "Couldn't bring the music assistant into this group. Please check my admin permissions and try again."

    return None


def register_handlers(
    bot: Client,
    assistant: Client,
    player: VoiceChatPlayer,
    queues: QueueManager,
    broadcaster: BroadcastManager,
    autoplayer: AutoplayManager,
    favorites: FavoritesStore,
) -> None:
    tracker = NowPlayingTracker()
    bot_api = BotApiClient(BOT_TOKEN or "")

    # Per-chat locks: prevent two concurrent /play downloads in the same chat.
    _chat_locks: dict[int, asyncio.Lock] = {}

    # Autoplay repeat protection: no YouTube video may be selected twice during
    # one active autoplay session. This is intentionally a set, not a rolling
    # window, because the user wants new songs rather than repeats after 4/5.
    _played_history: dict[int, set[str]] = {}
    _mood_history: dict[int, collections.deque] = {}

    def _track_key(url: str) -> str:
        return _extract_video_id(url) or url.strip().lower()

    def _record_played(chat_id: int, track: Track) -> None:
        """Record a started track for cooldown and recommendation mood."""
        key = _track_key(track.url)
        _played_history.setdefault(chat_id, set()).add(key)

        mood = _mood_history.setdefault(chat_id, collections.deque(maxlen=5))
        mood = collections.deque(
            (item for item in mood if _track_key(item.url) != key),
            maxlen=5,
        )
        mood.append(track)
        _mood_history[chat_id] = mood

    def _played_ids(chat_id: int) -> frozenset:
        return frozenset(_played_history.get(chat_id, []))

    def _clear_autoplay_memory(chat_id: int) -> None:
        _played_history.pop(chat_id, None)
        _mood_history.pop(chat_id, None)

    def _mood_seed_urls(chat_id: int, current: Track) -> list[str]:
        """Return the current song plus the four most recent mood anchors."""
        urls: list[str] = []
        seen: set[str] = set()
        for track in [current, *reversed(_mood_history.get(chat_id, []))]:
            video_id = _extract_video_id(track.url)
            key = video_id or track.url
            if key in seen:
                continue
            seen.add(key)
            urls.append(track.url)
            if len(urls) == 5:
                break
        return urls

    _track_urls: dict[int, str] = {}

    def _controls(chat_id: int, elapsed: int = 0, duration: int = 0):
        paused = queues.state(chat_id).paused
        if elapsed == 0 and duration == 0:
            elapsed, duration = tracker.current_elapsed(chat_id)
        return player_controls_api(
            paused=paused, elapsed=elapsed, duration=duration,
            track_url=_track_urls.get(chat_id, ""),
            autoplay_enabled=autoplayer.is_enabled(chat_id),
        )

    async def _refresh_controls(chat_id: int) -> None:
        """Refresh the autoplay state without disturbing the progress bar."""
        message = tracker.current_message(chat_id)
        if message is None:
            return

        try:
            await bot_api.call(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message.id,
                    "reply_markup": _controls(chat_id),
                },
            )
            return
        except Exception:
            # Cards sent through Pyrofork still need a native markup fallback.
            if not getattr(message, "chat", None):
                return
            elapsed, duration = tracker.current_elapsed(chat_id)
            state = queues.state(chat_id)
            await message.edit_reply_markup(
                player_controls(
                    paused=state.paused,
                    elapsed=elapsed,
                    duration=duration,
                    track_url=_track_urls.get(chat_id, ""),
                    autoplay_enabled=autoplayer.is_enabled(chat_id),
                )
            )

    async def _send_queued_card(
        chat_id: int,
        track: Track,
        text: str,
        photo: str | None = None,
    ):
        """Send a queued request card with Play Now and Fav actions."""
        track_key = _track_callback_key(track)
        styled_markup = queue_card_controls_api(track_key)
        fallback_markup = queue_card_controls(track_key)
        sent = None
        photo = photo or track.thumbnail

        if photo:
            with contextlib.suppress(Exception):
                sent = await bot_api.send_photo(
                    chat_id,
                    photo,
                    text,
                    reply_markup=styled_markup,
                )
            if sent is None:
                with contextlib.suppress(Exception):
                    sent = await bot.send_photo(
                        chat_id,
                        photo,
                        caption=text,
                        reply_markup=fallback_markup,
                        parse_mode=enums.ParseMode.HTML,
                    )

        if sent is None:
            with contextlib.suppress(Exception):
                sent = await bot_api.send_message(
                    chat_id,
                    text,
                    reply_markup=styled_markup,
                )
        if sent is None:
            sent = await bot.send_message(
                chat_id,
                text,
                reply_markup=fallback_markup,
                parse_mode=enums.ParseMode.HTML,
            )
        return sent

    async def _toggle_favorite_for_track(user_id: int, track: Track) -> bool:
        """Toggle a track while never persisting a temporary local thumbnail."""
        video_id = _extract_video_id(track.url) or track.url.strip()
        thumbnail = (
            track.thumbnail
            if track.thumbnail and track.thumbnail.startswith("http")
            else None
        )
        return await favorites.toggle(
            user_id,
            video_id=video_id,
            url=track.url,
            title=track.title,
            duration=track.duration,
            thumbnail=thumbnail,
        )

    async def _post_now_playing(chat_id: int, track: Track) -> None:
        """Sends a fresh "now playing" message and starts its live progress
        bar. Fires on every track start -- the initial /play, /skip, button
        skips, and automatic advance when a track finishes."""
        # Keep the previous card until the replacement is actually delivered.
        # Deleting it first leaves the chat with no card when Telegram rejects
        # a photo upload or the Bot API is temporarily unavailable.
        old_message = tracker.current_message(chat_id)

        caption = _format_track(track)
        # Record in play history so autoplay avoids repeating this song.
        _record_played(chat_id, track)
        # Remember this chat's track URL so the blue bar button can link to it.
        _track_urls[chat_id] = track.url
        # Upload a local copy when possible. Telegram cannot always fetch
        # YouTube's remote thumbnail URL reliably.
        try:
            # Do not let artwork retrieval block the now-playing card forever.
            local_thumbnail = await asyncio.wait_for(
                download_thumbnail(track.thumbnail, track.url),
                timeout=8,
            )
        except Exception:
            log.warning(
                "Thumbnail preparation failed for chat %s",
                chat_id,
                exc_info=True,
            )
            local_thumbnail = None
        if local_thumbnail:
            track.thumbnail = local_thumbnail
        # Bar lives in the keyboard button — caption is track info only.
        initial_markup = player_controls_api(
            paused=False,
            elapsed=0,
            duration=track.duration,
            track_url=track.url,
            autoplay_enabled=autoplayer.is_enabled(chat_id),
        )
        fallback_markup = player_controls(
            paused=False,
            elapsed=0,
            duration=track.duration,
            track_url=track.url,
            autoplay_enabled=autoplayer.is_enabled(chat_id),
        )
        photo = local_thumbnail or track.thumbnail
        message = None

        # Try styled photo delivery first, then native photo delivery. Every
        # photo failure must continue to a text-card fallback so playback
        # never loses its now-playing message.
        if photo:
            try:
                message = await bot_api.send_photo(
                    chat_id,
                    photo,
                    caption,
                    reply_markup=initial_markup,
                )
            except Exception as exc:
                log.warning(
                    "Bot API thumbnail delivery failed for chat %s: %s",
                    chat_id,
                    exc,
                )
            if message is None:
                try:
                    message = await bot.send_photo(
                        chat_id,
                        photo,
                        caption=caption,
                        reply_markup=fallback_markup,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except FloodWait as exc:
                    log.warning(
                        "FloodWait %ds on native thumbnail delivery for chat %s",
                        exc.value,
                        chat_id,
                    )
                    await asyncio.sleep(min(exc.value, 10))
                except Exception as exc:
                    log.warning(
                        "Native thumbnail delivery failed for chat %s: %s",
                        chat_id,
                        exc,
                    )

        # Text is a final, reliable card fallback for bad image formats,
        # inaccessible remote thumbnails, and transient photo API failures.
        if message is None:
            try:
                message = await bot_api.send_message(
                    chat_id,
                    caption,
                    reply_markup=initial_markup,
                )
            except Exception as exc:
                log.warning(
                    "Bot API text-card delivery failed for chat %s: %s",
                    chat_id,
                    exc,
                )
        if message is None:
            try:
                message = await bot.send_message(
                    chat_id,
                    caption,
                    reply_markup=fallback_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                log.exception("Failed to post now-playing message for chat %s", chat_id)
                return

        if old_message is not None:
            with contextlib.suppress(Exception):
                await old_message.delete()

        if isinstance(message, BotApiMessage):
            progress_markup = lambda e, d, p, cid=chat_id: _controls(cid, e, d)
        else:
            progress_markup = lambda e, d, p, cid=chat_id: player_controls(
                paused=p,
                elapsed=e,
                duration=d,
                track_url=_track_urls.get(cid, ""),
                autoplay_enabled=autoplayer.is_enabled(cid),
            )
        tracker.start(
            chat_id, message, track.duration, caption,
            progress_markup,
        )

    async def _post_queue_empty(chat_id: int) -> None:
        """Fires once the queue runs out and the assistant has left the
        voice chat -- stops the progress tracker and lets everyone know."""
        tracker.stop(chat_id)
        _clear_autoplay_memory(chat_id)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, "✅ Queue finished, left the voice chat.")

    async def _silent_autoplay_fetch(chat_id: int, last_track: Track) -> Track | None:
        """Background prefetch: silently fetch next autoplay track while current
        song plays. No Telegram messages — called early so the track is ready
        the moment the current song ends."""
        if not autoplayer.is_enabled(chat_id):
            return None
        try:
            info = await get_related_track(
                last_track.url,
                played_ids=_played_ids(chat_id),
                seed_urls=_mood_seed_urls(chat_id, last_track),
                video=last_track.is_video,
            )
        except Exception:
            return None
        if not info:
            return None
        return Track(
            title=info["title"],
            url=info["url"],
            stream_url=info["url"],
            duration=info["duration"],
            thumbnail=info["thumbnail"],
            requested_by="🔄 Autoplay",
            file_path=info["file_path"],
            is_video=last_track.is_video,
            audio_path=info.get("audio_path") if last_track.is_video else None,
        )

    async def _autoplay_next(chat_id: int, last_track: Track) -> Track | None:
        """Fallback: called only when the silent prefetch missed or failed.
        Shows a "fetching" message since there will be a visible wait."""
        if not autoplayer.is_enabled(chat_id):
            return None

        notify_task = asyncio.create_task(
            bot.send_message(chat_id, "🔄 <b>Autoplay</b> — finding next song…",
                             parse_mode=enums.ParseMode.HTML)
        )
        try:
            info = await get_related_track(
                last_track.url,
                played_ids=_played_ids(chat_id),
                seed_urls=_mood_seed_urls(chat_id, last_track),
                video=last_track.is_video,
            )
        except Exception:
            info = None

        # Delete the "fetching" message as soon as we have a result
        with contextlib.suppress(Exception):
            msg = await notify_task
            await msg.delete()

        if not info:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id,
                    "🔄 Autoplay couldn't find a related track. Leaving voice chat.",
                )
            return None

        return Track(
            title=info["title"],
            url=info["url"],
            stream_url=info["url"],
            duration=info["duration"],
            thumbnail=info["thumbnail"],
            requested_by="🔄 Autoplay",
            file_path=info["file_path"],
            is_video=last_track.is_video,
            audio_path=info.get("audio_path") if last_track.is_video else None,
        )

    async def _recover_stream(_chat_id: int, track: Track) -> bool:
        """Refresh an expiring direct URL after an unexpected early stop."""
        info = await (
            resolve_video_stream_url(track.url, fresh=True)
            if track.is_video
            else resolve_stream_url(track.url, fresh=True)
        )
        track.stream_url = info["url"]
        track.file_path = info["file_path"]
        track.audio_path = info.get("audio_path") if track.is_video else None
        return True

    player.on_track_start = _post_now_playing
    player.on_queue_empty = _post_queue_empty
    player.on_autoplay_next = _autoplay_next
    player.on_autoplay_prefetch = _silent_autoplay_fetch
    player.on_stream_recovery = _recover_stream

    @bot.on_message(filters.command("start") & filters.private)
    async def start_cmd(_client: Client, message: Message) -> None:
        user = message.from_user.mention if message.from_user else "there"
        caption = (
            f"Welcome {user} ,this is Musenzy a powerfull,free,music bot for you\n\n"
            "Add me to a group as admin with \"Invite users via link\" permission, start the group's "
            "voice chat, then use /play <song name or link> for audio or "
            "/vplay <video name or link> for video -- I'll bring the music assistant in "
            "automatically. Works independently in every group I'm in."
        )
        await message.reply_photo(LOGO_PATH, caption=caption, reply_markup=welcome_menu())

    @bot.on_callback_query(filters.regex(r"^menu:commands$"))
    async def menu_cb(_client: Client, query: CallbackQuery) -> None:
        await query.answer()
        await query.message.reply_text(COMMANDS_TEXT)

    async def _play_media_command(
        client: Client,
        message: Message,
        *,
        video: bool = False,
    ) -> None:
        command_name = "vplay" if video else "play"
        # Drop duplicate deliveries of the same message (Telegram re-sends
        # unacknowledged updates when the bot is slow, e.g. during yt-dlp fetch).
        if message.id in _seen_message_ids:
            return
        _seen_message_ids.add(message.id)
        # Keep the set bounded — discard old IDs after 500 entries.
        if len(_seen_message_ids) > 500:
            _seen_message_ids.discard(next(iter(_seen_message_ids)))

        # Track this group so broadcast can reach it.
        broadcaster.register_chat(message.chat.id)

        query = message.text.split(maxsplit=1)
        chat_id = message.chat.id
        if len(query) < 2:
            asyncio.create_task(
                _send_and_delete(
                    chat_id,
                    bot,
                    f"Usage: /{command_name} <song name or YouTube link>",
                )
            )
            with contextlib.suppress(Exception):
                await message.delete()
            return

        join_error = await _ensure_assistant_in_chat(client, assistant, chat_id)
        if join_error:
            asyncio.create_task(_send_and_delete(chat_id, bot, join_error))
            with contextlib.suppress(Exception):
                await message.delete()
            return

        # Per-chat lock: only one download at a time per group.
        lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            # Silently discard — a download is already in progress.
            with contextlib.suppress(Exception):
                await message.delete()
            return

        async with lock:
            # Delete the command immediately.
            with contextlib.suppress(Exception):
                await message.delete()

            # Send a searching indicator — a lone animated emoji spins in Telegram.
            searching_msg = None
            with contextlib.suppress(Exception):
                searching_msg = await bot.send_message(chat_id, "🔍")

            try:
                if video:
                    # Resolve separate direct video and audio URLs. PyTgCalls
                    # combines them into a video voice-chat stream.
                    info = await resolve_video_stream_url(query[1])
                else:
                    # Resolve only first so normal playback starts quickly.
                    # If the direct stream is rejected, the playback block
                    # below retries once with a local ffmpeg download.
                    info = await resolve_stream_url(query[1])
            except TrackTooLong as exc:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()
                asyncio.create_task(_send_and_delete(chat_id, bot, str(exc)))
                return
            except TrackNotFound:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()
                asyncio.create_task(_send_and_delete(chat_id, bot, "❌ Couldn't find that track."))
                return
            except YouTubeBlocked:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()
                asyncio.create_task(
                    _send_and_delete(
                        chat_id,
                        bot,
                        "❌ YouTube blocked this server. Please refresh the YouTube cookies in "
                        "YOUTUBE_COOKIES_B64, then restart the bot.",
                    )
                )
                return
            except Exception:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()
                log.exception("Failed to resolve track for chat %s", chat_id)
                asyncio.create_task(_send_and_delete(
                    chat_id,
                    bot,
                    "❌ Could not fetch that video right now."
                    if video
                    else "❌ Could not fetch that track right now.",
                ))
                return
            finally:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()

            user = message.from_user
            if user:
                requester = user.first_name or "User"
                requester_id = user.id
            else:
                requester = "someone"
                requester_id = None
            track = Track(
                title=info["title"],
                url=info["url"],
                stream_url=info["url"],
                duration=info["duration"],
                thumbnail=info["thumbnail"],
                requested_by=requester,
                file_path=info["file_path"],
                is_video=video,
                audio_path=info.get("audio_path") if video else None,
                requester_id=requester_id,
            )

            try:
                position = await player.play_or_enqueue(message.chat.id, track)
            except Exception:
                if video:
                    log.exception("Failed to start video playback for chat %s", chat_id)
                    asyncio.create_task(
                        _send_and_delete(
                            chat_id,
                            bot,
                            "❌ The assistant joined, but video playback could not start. "
                            "Please make sure the group voice chat is active and try /vplay again.",
                        )
                    )
                    return

                # Direct URL playback is fast but can be rejected by YouTube
                # or ffmpeg on a cloud host. Retry once with a local file
                # rather than making the user search again.
                log.warning(
                    "Direct audio playback failed for chat %s; retrying with local download",
                    chat_id,
                    exc_info=True,
                )
                try:
                    fallback = await resolve_and_download(track.url)
                    track.stream_url = fallback["url"]
                    track.file_path = fallback["file_path"]
                    position = await player.play_or_enqueue(message.chat.id, track)
                except Exception:
                    log.exception("Local audio playback fallback failed for chat %s", chat_id)
                    asyncio.create_task(
                        _send_and_delete(
                            chat_id,
                            bot,
                            "❌ The assistant joined, but playback could not start. "
                            "Please make sure the group voice chat is active and try /play again.",
                        )
                    )
                    return
            if position > 0:
                # Queued — on_track_start won't fire yet, so post the queued message here.
                text = _format_track(track, position)
                try:
                    local_thumbnail = await download_thumbnail(track.thumbnail, track.url)
                except Exception:
                    log.warning(
                        "Queued thumbnail preparation failed for chat %s",
                        chat_id,
                        exc_info=True,
                    )
                    local_thumbnail = None
                if local_thumbnail:
                    await _send_queued_card(chat_id, track, text, photo=local_thumbnail)
                else:
                    await _send_queued_card(chat_id, track, text)
            # position == 0: on_track_start already posted the "Now playing" card.

    @bot.on_message(filters.command("play") & filters.group)
    async def play_cmd(client: Client, message: Message) -> None:
        await _play_media_command(client, message)

    @bot.on_message(filters.command("vplay") & filters.group)
    async def vplay_cmd(client: Client, message: Message) -> None:
        await _play_media_command(client, message, video=True)

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(client: Client, message: Message) -> None:
        if not await _is_admin(client, message.chat.id, message.from_user.id):
            asyncio.create_task(_send_and_delete(message.chat.id, bot, "🚫 Only admins can skip tracks."))
            with contextlib.suppress(Exception):
                await message.delete()
            return
        nxt = await player.play_next(message.chat.id)
        if nxt:
            await message.reply_text("⏭ Skipped.")

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(client: Client, message: Message) -> None:
        if not await _is_admin(client, message.chat.id, message.from_user.id):
            asyncio.create_task(_send_and_delete(message.chat.id, bot, "🚫 Only admins can pause playback."))
            with contextlib.suppress(Exception):
                await message.delete()
            return
        await player.pause(message.chat.id)
        tracker.pause(message.chat.id)
        await message.reply_text("⏸ Paused.")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(client: Client, message: Message) -> None:
        if not await _is_admin(client, message.chat.id, message.from_user.id):
            asyncio.create_task(_send_and_delete(message.chat.id, bot, "🚫 Only admins can resume playback."))
            with contextlib.suppress(Exception):
                await message.delete()
            return
        await player.resume(message.chat.id)
        tracker.resume(message.chat.id)
        await message.reply_text("▶️ Resumed.")

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(client: Client, message: Message) -> None:
        if not await _is_admin(client, message.chat.id, message.from_user.id):
            asyncio.create_task(_send_and_delete(message.chat.id, bot, "🚫 Only admins can stop playback."))
            with contextlib.suppress(Exception):
                await message.delete()
            return
        tracker.stop(message.chat.id)
        await player.stop(message.chat.id)
        _clear_autoplay_memory(message.chat.id)
        await message.reply_text("Stopped and left the voice chat.")

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_client: Client, message: Message) -> None:
        state = queues.state(message.chat.id)
        ap_status = "🟢 On" if autoplayer.is_enabled(message.chat.id) else "🔴 Off"
        if not state.current:
            await message.reply_text(f"Nothing is playing right now.\n🔄 Autoplay: {ap_status}")
            return
        lines = [_format_track(state.current)]
        for i, track in enumerate(state.queue, start=1):
            lines.append(f"{i}. {track.title} -- requested by {track.requested_by}")
        lines.append(f"\n🔄 Autoplay: {ap_status}")
        await message.reply_text("\n".join(lines))

    # ──────────────────────────────────────────────
    # Favorites commands
    # ──────────────────────────────────────────────

    def _favorite_list_text(saved: list[FavoriteTrack]) -> str:
        lines = [
            "❤️ <b>Your favorite songs</b>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        for position, favorite in enumerate(saved, start=1):
            lines.append(
                f"{position}. <b>{html.escape(favorite.title)}</b>\n"
                f"   <code>/favplay {position}</code>"
            )
        lines.append(
            "\nUse <code>/favplay</code> to queue all favorites in order, or "
            "<code>/favplay &lt;number&gt;</code> to play one. "
            "Tap ❤️ Fav on a player card to save or remove the current song."
        )
        return "\n".join(lines)

    @bot.on_message(filters.command("favplay") & filters.group)
    async def favplay_cmd(client: Client, message: Message) -> None:
        """Play all favorites in order, or one favorite when requested."""
        user_id = message.from_user.id if message.from_user else 0
        try:
            saved = await favorites.list_for_user(user_id)
        except Exception:
            log.exception("Failed to load favorites for user %s", user_id)
            await message.reply_text("❌ Could not load your favorites right now.")
            return
        if not saved:
            await message.reply_text(
                "❤️ You have no favorite songs yet.\n"
                "Tap the ❤️ Fav button on a playing song to save it."
            )
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            favorites_to_play = saved
            single_favorite = False
        else:
            choice = parts[1].strip()
            favorite: FavoriteTrack | None = None
            if choice.isdigit():
                position = int(choice)
                if 1 <= position <= len(saved):
                    favorite = saved[position - 1]
            else:
                favorite = next(
                    (item for item in saved if choice.casefold() in item.title.casefold()),
                    None,
                )

            if favorite is None:
                await message.reply_text(
                    "❌ Favorite not found.\n\n" + _favorite_list_text(saved),
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            favorites_to_play = [favorite]
            single_favorite = True

        chat_id = message.chat.id
        broadcaster.register_chat(chat_id)
        join_error = await _ensure_assistant_in_chat(client, assistant, chat_id)
        if join_error:
            asyncio.create_task(_send_and_delete(chat_id, bot, join_error))
            with contextlib.suppress(Exception):
                await message.delete()
            return

        lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            with contextlib.suppress(Exception):
                await message.delete()
            return

        async with lock:
            with contextlib.suppress(Exception):
                await message.delete()
            searching_msg = None
            with contextlib.suppress(Exception):
                searching_msg = await bot.send_message(
                    chat_id,
                    "❤️ Loading your favorite songs one by one…",
                )

            queued_count = 0
            skipped: list[str] = []
            user = message.from_user
            requester = (
                user.first_name or "User"
                if user
                else "someone"
            )
            requester_id = user.id if user else None
            try:
                for favorite in favorites_to_play:
                    try:
                        info = await resolve_and_download(favorite.url)
                    except (TrackTooLong, TrackNotFound, YouTubeBlocked):
                        skipped.append(favorite.title)
                        continue
                    except Exception:
                        log.exception(
                            "Failed to resolve favorite %s for chat %s",
                            favorite.title,
                            chat_id,
                        )
                        skipped.append(favorite.title)
                        continue

                    track = Track(
                        title=info["title"],
                        url=info["url"],
                        stream_url=info["url"],
                        duration=info["duration"],
                        thumbnail=info["thumbnail"],
                        requested_by=requester,
                        file_path=info["file_path"],
                        requester_id=requester_id,
                    )
                    position = await player.play_or_enqueue(chat_id, track)
                    queued_count += 1
                    if position > 0:
                        await _send_queued_card(
                            chat_id,
                            track,
                            _format_track(track, position),
                        )
            finally:
                with contextlib.suppress(Exception):
                    if searching_msg:
                        await searching_msg.delete()

            if queued_count:
                if single_favorite:
                    summary = "❤️ Favorite added to the queue."
                else:
                    summary = (
                        f"❤️ Queued {queued_count} favorite song"
                        f"{'s' if queued_count != 1 else ''} in order."
                    )
                if skipped:
                    summary += f"\n⚠️ Skipped {len(skipped)} unavailable favorite(s)."
                await bot.send_message(chat_id, summary)
            elif skipped:
                await bot.send_message(
                    chat_id,
                    "❌ None of your favorites could be played right now.",
                )

    @bot.on_message(filters.command("autoplay") & filters.group)
    async def autoplay_cmd(_client: Client, message: Message) -> None:
        """Toggle autoplay — anyone in the group can change it."""
        broadcaster.register_chat(message.chat.id)
        with contextlib.suppress(Exception):
            await message.delete()

        chat_id = message.chat.id
        if autoplayer.is_enabled(chat_id):
            autoplayer.disable(chat_id)
            _clear_autoplay_memory(chat_id)
            response = (
                "🔄 <b>Autoplay disabled</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "The bot will leave the voice chat when the queue runs out."
            )
        else:
            # Starting a new autoplay session gets a clean no-repeat set, but
            # the currently playing song remains blocked if one is active.
            _clear_autoplay_memory(chat_id)
            current = queues.state(chat_id).current
            if current:
                _record_played(chat_id, current)
            autoplayer.enable(chat_id)
            response = (
                "🔄 <b>Autoplay enabled</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "When the queue ends I'll automatically play related songs "
                "using YouTube's recommendations.\n\n"
                "Use <code>/autoplay</code> again or <code>/stopautoplay</code> "
                "to turn it off."
            )
        await _refresh_controls(chat_id)

        await bot.send_message(
            chat_id,
            response,
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.command("stopautoplay") & filters.group)
    async def stopautoplay_cmd(_client: Client, message: Message) -> None:
        """Stop autoplay — anyone in the group can disable it."""
        broadcaster.register_chat(message.chat.id)
        with contextlib.suppress(Exception):
            await message.delete()

        autoplayer.disable(message.chat.id)
        _clear_autoplay_memory(message.chat.id)
        await _refresh_controls(message.chat.id)

        await bot.send_message(
            message.chat.id,
            "🔄 <b>Autoplay disabled</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "The bot will leave the voice chat when the queue runs out.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_callback_query(filters.regex(r"^q:"))
    async def queued_card_cb(client: Client, query: CallbackQuery) -> None:
        """Handle actions on a queued song request card."""
        parts = query.data.split(":", 2)
        if len(parts) != 3 or query.message is None:
            with contextlib.suppress(Exception):
                await query.answer("This card is no longer active.", show_alert=True)
            return

        action, track_key = parts[1], parts[2]
        chat_id = query.message.chat.id
        state = queues.state(chat_id)
        track = next(
            (item for item in state.queue if _track_callback_key(item) == track_key),
            None,
        )
        if track is None:
            with contextlib.suppress(Exception):
                await query.answer("That song is no longer in the queue.", show_alert=True)
            return

        if action == "fav":
            try:
                saved = await _toggle_favorite_for_track(query.from_user.id, track)
            except Exception:
                log.exception(
                    "Failed to toggle queued favorite for user %s",
                    query.from_user.id,
                )
                with contextlib.suppress(Exception):
                    await query.answer(
                        "Favorites database is unavailable.",
                        show_alert=True,
                    )
                return
            with contextlib.suppress(Exception):
                await query.answer(
                    "❤️ Added to favorites" if saved else "💔 Removed from favorites"
                )
            return

        if action != "play":
            with contextlib.suppress(Exception):
                await query.answer("Unknown queue action.", show_alert=True)
            return

        if not await _is_admin(client, chat_id, query.from_user.id):
            with contextlib.suppress(Exception):
                await query.answer(
                    "🚫 Only admins can play a queued song now.",
                    show_alert=True,
                )
            return

        try:
            started = await player.play_now(chat_id, track)
        except Exception:
            log.exception("Failed to play queued track now in chat %s", chat_id)
            with contextlib.suppress(Exception):
                await query.answer("Could not start that song.", show_alert=True)
            return

        if not started:
            with contextlib.suppress(Exception):
                await query.answer("That song is no longer in the queue.", show_alert=True)
            return

        with contextlib.suppress(Exception):
            await query.answer("▶️ Playing now")
            await query.message.delete()

    @bot.on_callback_query(filters.regex(r"^ctl:"))
    async def controls_cb(client: Client, query: CallbackQuery) -> None:
        # Deduplicate: Telegram re-delivers unacknowledged callback queries.
        if query.id in _seen_callback_ids:
            return
        _seen_callback_ids.add(query.id)
        if len(_seen_callback_ids) > 500:
            _seen_callback_ids.discard(next(iter(_seen_callback_ids)))

        action = query.data.split(":", 1)[1]
        chat_id = query.message.chat.id

        # queue, close, and favorites are user actions — playback controls
        # remain restricted to group admins.
        if action not in ("queue", "close", "fav"):
            if not await _is_admin(client, chat_id, query.from_user.id):
                with contextlib.suppress(Exception):
                    await query.answer("🚫 Only admins can control playback.", show_alert=True)
                return

        state = queues.state(chat_id)

        if action == "pauseresume":
            if state.paused:
                await player.resume(chat_id)
                tracker.resume(chat_id)
                with contextlib.suppress(Exception):
                    await query.answer("Resumed")
            else:
                await player.pause(chat_id)
                tracker.pause(chat_id)
                with contextlib.suppress(Exception):
                    await query.answer("Paused")
            with contextlib.suppress(Exception):
                await _refresh_controls(chat_id)
        elif action == "skip":
            await player.play_next(chat_id)
            with contextlib.suppress(Exception):
                await query.answer("Skipped")
        elif action == "stop":
            tracker.stop(chat_id)
            await player.stop(chat_id)
            with contextlib.suppress(Exception):
                await query.answer("Stopped")
            await query.message.reply_text("Stopped and left the voice chat.")
        elif action == "queue":
            if not state.current:
                with contextlib.suppress(Exception):
                    await query.answer("Nothing playing", show_alert=True)
                return
            lines = [_format_track(state.current)]
            for i, track in enumerate(state.queue, start=1):
                lines.append(f"{i}. {track.title} -- requested by {track.requested_by}")
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text("\n".join(lines))
        elif action == "autoplay":
            was_enabled = autoplayer.is_enabled(chat_id)
            enabled = autoplayer.toggle(chat_id)
            if enabled and not was_enabled:
                # Start a fresh no-repeat session while keeping the current
                # track excluded from recommendations.
                _clear_autoplay_memory(chat_id)
                if state.current:
                    _record_played(chat_id, state.current)
            elif not enabled:
                _clear_autoplay_memory(chat_id)

            with contextlib.suppress(Exception):
                await query.answer("Autoplay enabled" if enabled else "Autoplay disabled")
            with contextlib.suppress(Exception):
                await _refresh_controls(chat_id)
        elif action == "fav":
            if not state.current:
                with contextlib.suppress(Exception):
                    await query.answer("Nothing is playing", show_alert=True)
                return

            track = state.current
            try:
                saved = await _toggle_favorite_for_track(query.from_user.id, track)
            except Exception:
                log.exception("Failed to toggle favorite for user %s", query.from_user.id)
                with contextlib.suppress(Exception):
                    await query.answer("Favorites database is unavailable.", show_alert=True)
                return

            with contextlib.suppress(Exception):
                await query.answer("❤️ Added to favorites" if saved else "💔 Removed from favorites")
        elif action == "close":
            with contextlib.suppress(Exception):
                await query.answer()
            with contextlib.suppress(Exception):
                await query.message.delete()

    # ──────────────────────────────────────────────
    # Group auto-registration (for broadcast coverage)
    # ──────────────────────────────────────────────

    @bot.on_message(filters.group & ~filters.service)
    async def _register_group(_client: Client, message: Message) -> None:
        """Silently register every group the bot receives a message from
        so broadcast can reach it even if /play has never been used there."""
        broadcaster.register_chat(message.chat.id)

    # ──────────────────────────────────────────────
    # Broadcast — owner only
    # ──────────────────────────────────────────────

    def _is_owner(user_id: int) -> bool:
        return OWNER_ID != 0 and user_id == OWNER_ID

    def _player_button_editor_text() -> str:
        settings = get_player_button_settings()
        lines = [
            "🎛 <b>Player button editor</b>",
            "━━━━━━━━━━━━━━━━━━",
            "Customize the labels and emojis on every now-playing card.",
            "Unicode fonts, symbols, and emojis are supported.",
            "",
            "<b>Current labels:</b>",
        ]
        for key, name in BUTTON_NAMES.items():
            style = settings.style(key)
            lines.append(
                f"• <b>{html.escape(name)}</b>: "
                f"<code>{html.escape(settings.label(key))}</code> "
                f"· <i>{style}</i>"
            )
        lines.extend(
            [
                "",
                "Tap a button below, then use:",
                "<code>/setbutton &lt;name&gt; &lt;new label&gt;</code>",
                "<code>/setbuttonstyle &lt;name&gt; &lt;primary|success|danger&gt;</code>",
                "",
                "Names: <code>pause</code>, <code>resume</code>, <code>skip</code>, "
                "<code>stop</code>, <code>queue</code>, <code>close</code>, "
                "<code>autoplay_on</code>, <code>autoplay_off</code>, <code>fav</code>, "
                "<code>play_now</code>",
                "",
                "<b>Card text above the buttons:</b>",
                "Tap <code>📝 Edit card text</code> to customize the heading, "
                "song/time/requester labels, divider, and separator.",
            ]
        )
        return "\n".join(lines)

    def _player_card_editor_text() -> str:
        settings = get_player_button_settings()
        lines = [
            "📝 <b>Player card text editor</b>",
            "━━━━━━━━━━━━━━━━━━",
            "Change the text and emojis shown with the song title, duration, and requester.",
            "The actual song name, time, and requester stay dynamic.",
            "",
            "<b>Current card text:</b>",
        ]
        for key, name in CARD_TEXT_NAMES.items():
            lines.append(
                f"• <b>{html.escape(name)}</b>: "
                f"<code>{html.escape(settings.card(key))}</code>"
            )
        lines.extend(
            [
                "",
                "Use:",
                "<code>/setcard &lt;field&gt; &lt;new text&gt;</code>",
                "",
                "Fields: <code>now_playing</code>, <code>queued</code>, "
                "<code>song_prefix</code>, <code>time_prefix</code>, "
                "<code>requester_prefix</code>, <code>divider</code>, "
                "<code>separator</code>",
                "",
                "<b>Card layouts:</b>",
                f"• Playing: <code>{html.escape(', '.join(settings.layout('playing')))}</code>",
                f"• Queue: <code>{html.escape(', '.join(settings.layout('queue')))}</code>",
                "Use <code>/setcardlayout &lt;playing|queue&gt; "
                "&lt;items in order&gt;</code>",
                "Items: <code>heading</code>, <code>divider</code>, "
                "<code>song</code>, <code>time</code>, <code>requester</code>, "
                "<code>meta</code>, <code>spacer</code>",
                "",
                "Examples:",
                "<code>/setcard now_playing 🎶 ɴᴏᴡ ᴘʟᴀʏɪɴɢ</code>",
                "<code>/setcard requester_prefix Requested by</code>",
                "<code>/setcard time_prefix Duration</code>",
                "<code>/setcardlayout playing heading,song,divider,meta</code>",
            ]
        )
        return "\n".join(lines)

    def _player_button_style_help() -> str:
        return (
            "🎨 <b>Button styles</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<code>primary</code> — blue\n"
            "<code>success</code> — green\n"
            "<code>danger</code> — red\n\n"
            "Example:\n"
            "<code>/setbuttonstyle skip success</code>\n\n"
            "The label command accepts any Unicode text, for example:\n"
            "<code>/setbutton skip ⏩ NEXT</code>"
        )

    @bot.on_message(filters.command("playerbuttons") & filters.private)
    async def playerbuttons_cmd(_client: Client, message: Message) -> None:
        """Owner-only editor for the labels/styles on player cards."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return
        await message.reply_text(
            _player_button_editor_text(),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=player_button_editor_menu(),
        )

    @bot.on_message(filters.command("setbutton") & filters.private)
    async def setbutton_cmd(_client: Client, message: Message) -> None:
        """Owner-only command to set a player button's Unicode label."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text(
                "Usage: <code>/setbutton &lt;name&gt; &lt;new label&gt;</code>\n"
                "Example: <code>/setbutton queue 🎶 QUEUE</code>\n\n"
                "Open <code>/playerbuttons</code> for all button names.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        settings = get_player_button_settings()
        try:
            key = settings.set_label(parts[1], parts[2])
        except ValueError as exc:
            await message.reply_text(f"❌ {html.escape(str(exc))}")
            return

        await message.reply_text(
            f"✅ <b>{html.escape(BUTTON_NAMES[key])}</b> label updated to "
            f"<code>{html.escape(settings.label(key))}</code>.\n"
            "New and active player cards will use it.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.command("setbuttonstyle") & filters.private)
    async def setbuttonstyle_cmd(_client: Client, message: Message) -> None:
        """Owner-only command to set a Bot API button style."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text(
                _player_button_style_help(),
                parse_mode=enums.ParseMode.HTML,
            )
            return

        settings = get_player_button_settings()
        try:
            key = settings.set_style(parts[1], parts[2])
        except ValueError as exc:
            await message.reply_text(f"❌ {html.escape(str(exc))}")
            return

        await message.reply_text(
            f"✅ <b>{html.escape(BUTTON_NAMES[key])}</b> style set to "
            f"<code>{html.escape(settings.style(key))}</code>.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.command("setcard") & filters.private)
    async def setcard_cmd(_client: Client, message: Message) -> None:
        """Owner-only command to set text/symbols in the player card."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text(
                _player_card_editor_text(),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=player_card_editor_menu(),
            )
            return

        settings = get_player_button_settings()
        try:
            key = settings.set_card_text(parts[1], parts[2])
        except ValueError as exc:
            await message.reply_text(f"❌ {html.escape(str(exc))}")
            return

        await message.reply_text(
            f"✅ <b>{html.escape(CARD_TEXT_NAMES[key])}</b> updated to "
            f"<code>{html.escape(settings.card(key))}</code>.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.command("setcardlayout") & filters.private)
    async def setcardlayout_cmd(_client: Client, message: Message) -> None:
        """Owner-only command to reorder the playing or queue card."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text(
                _player_card_editor_text(),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=player_card_editor_menu(),
            )
            return

        settings = get_player_button_settings()
        try:
            key = settings.set_layout(parts[1], parts[2])
        except ValueError as exc:
            await message.reply_text(
                f"❌ {html.escape(str(exc))}",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        label = "Playing" if key == "playing" else "Queue"
        await message.reply_text(
            f"✅ <b>{label} card layout</b> updated to "
            f"<code>{html.escape(', '.join(settings.layout(key)))}</code>.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.command("resetbuttons") & filters.private)
    async def resetbuttons_cmd(_client: Client, message: Message) -> None:
        """Owner-only reset to the original player keyboard."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return
        get_player_button_settings().reset()
        await message.reply_text(
            "♻️ Player buttons, card text, and card layouts have been reset to defaults.",
            reply_markup=player_button_editor_menu(),
        )

    @bot.on_callback_query(filters.regex(r"^pbtn:"))
    async def playerbuttons_cb(_client: Client, query: CallbackQuery) -> None:
        """Handle the owner editor menu."""
        if not _is_owner(query.from_user.id):
            with contextlib.suppress(Exception):
                await query.answer("🚫 Owner only.", show_alert=True)
            return

        action = query.data.split(":", 1)[1]
        if action == "close":
            with contextlib.suppress(Exception):
                await query.answer()
                await query.message.delete()
            return

        if action == "preview":
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text(
                "👁 <b>Player card preview</b>",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=player_controls(
                    paused=False,
                    elapsed=83,
                    duration=245,
                    track_url="https://youtube.com",
                    autoplay_enabled=True,
                ),
            )
            return

        if action == "stylehelp":
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text(
                _player_button_style_help(),
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if action == "cardhelp":
            with contextlib.suppress(Exception):
                await query.answer()
                await query.message.edit_text(
                    _player_card_editor_text(),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=player_card_editor_menu(),
                )
            return

        if action == "back":
            with contextlib.suppress(Exception):
                await query.answer()
                await query.message.edit_text(
                    _player_button_editor_text(),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=player_button_editor_menu(),
                )
            return

        if action == "reset":
            get_player_button_settings().reset()
            with contextlib.suppress(Exception):
                await query.answer("Reset to defaults.")
                await query.message.edit_text(
                    _player_button_editor_text(),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=player_button_editor_menu(),
                )
            return

        if action.startswith("edit:"):
            key = action.split(":", 1)[1]
            if key not in BUTTON_NAMES:
                with contextlib.suppress(Exception):
                    await query.answer("Unknown button.", show_alert=True)
                return
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text(
                f"✏️ Editing <b>{html.escape(BUTTON_NAMES[key])}</b>\n\n"
                f"Send:\n<code>/setbutton {key} &lt;new label&gt;</code>\n\n"
                f"Current: <code>{html.escape(get_player_button_settings().label(key))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if action.startswith("editcard:"):
            key = action.split(":", 1)[1]
            if key not in CARD_TEXT_NAMES:
                with contextlib.suppress(Exception):
                    await query.answer("Unknown card field.", show_alert=True)
                return
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text(
                f"✏️ Editing <b>{html.escape(CARD_TEXT_NAMES[key])}</b>\n\n"
                f"Send:\n<code>/setcard {key} &lt;new text&gt;</code>\n\n"
                f"Current: <code>{html.escape(get_player_button_settings().card(key))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if action.startswith("layout:"):
            key = action.split(":", 1)[1]
            if key not in ("playing", "queue"):
                with contextlib.suppress(Exception):
                    await query.answer("Unknown card layout.", show_alert=True)
                return
            settings = get_player_button_settings()
            label = "Playing" if key == "playing" else "Queue"
            with contextlib.suppress(Exception):
                await query.answer()
            await query.message.reply_text(
                f"📐 Editing <b>{label} card layout</b>\n\n"
                f"Send:\n<code>/setcardlayout {key} &lt;items in order&gt;</code>\n\n"
                "Items: <code>heading</code>, <code>divider</code>, "
                "<code>song</code>, <code>time</code>, <code>requester</code>, "
                "<code>meta</code>, <code>spacer</code>\n"
                f"Current: <code>{html.escape(', '.join(settings.layout(key)))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )

    @bot.on_message(filters.command("groups") & filters.private)
    async def groups_cmd(client: Client, message: Message) -> None:
        """Owner-only: list every group the bot is present in."""
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        chat_ids = broadcaster.known_chats()
        if not chat_ids:
            await message.reply_text("📭 The bot hasn't been added to any groups yet.")
            return

        lines = [f"👥 <b>Groups the bot is in</b> ({len(chat_ids)} total)\n━━━━━━━━━━━━━━━━━━"]
        for i, chat_id in enumerate(chat_ids, 1):
            try:
                chat = await client.get_chat(chat_id)
                name = html.escape(chat.title or str(chat_id))
                members = f"  · {chat.members_count} members" if chat.members_count else ""
                username = f" (@{chat.username})" if chat.username else ""
                lines.append(f"{i}. <b>{name}</b>{username}{members}\n   <code>{chat_id}</code>")
            except Exception:
                lines.append(f"{i}. <i>Unknown group</i>\n   <code>{chat_id}</code>")

        await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)

    @bot.on_message(filters.command("broadcast") & filters.private)
    async def broadcast_cmd(_client: Client, message: Message) -> None:
        if not _is_owner(message.from_user.id):
            await message.reply_text("🚫 This command is only for the bot owner.")
            return

        # Determine the message text: either inline, a reply, or prompt.
        text: str | None = None
        if message.reply_to_message and message.reply_to_message.text:
            text = message.reply_to_message.text.html
        elif len(message.text.split(maxsplit=1)) > 1:
            text = html.escape(message.text.split(maxsplit=1)[1])

        if not text:
            await message.reply_text(
                "📝 Please send the message you want to broadcast as a reply to this command, "
                "or write it after the command:\n\n<code>/broadcast Hello everyone!</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        broadcaster.set_pending(message.from_user.id, text)
        active = broadcaster.active_schedule_hours()
        chats = len(broadcaster.known_chats())
        await message.reply_text(
            f"📢 <b>Broadcast ready</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Message:</b>\n{text}\n\n"
            f"<b>Groups:</b> {chats}\n"
            + (f"<b>Active schedule:</b> every {active}h\n" if active else "")
            + "\nChoose when to send:",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=broadcast_schedule_menu(active_hours=active),
        )

    @bot.on_callback_query(filters.regex(r"^bcast:"))
    async def broadcast_cb(_client: Client, query: CallbackQuery) -> None:
        if not _is_owner(query.from_user.id):
            with contextlib.suppress(Exception):
                await query.answer("🚫 Owner only.", show_alert=True)
            return

        action = query.data.split(":", 1)[1]
        owner_id = query.from_user.id
        text = broadcaster.get_pending(owner_id)

        if action == "close":
            with contextlib.suppress(Exception):
                await query.answer()
            with contextlib.suppress(Exception):
                await query.message.delete()
            return

        if action == "cancel":
            had = broadcaster.cancel_schedule()
            with contextlib.suppress(Exception):
                await query.answer("Schedule cancelled." if had else "No active schedule.", show_alert=True)
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(broadcast_schedule_menu(active_hours=0))
            return

        if not text:
            with contextlib.suppress(Exception):
                await query.answer("No message set. Use /broadcast first.", show_alert=True)
            return

        if action == "now":
            with contextlib.suppress(Exception):
                await query.answer("Sending…")
            sent, failed = await broadcaster.send_now(bot, text)
            broadcaster.clear_pending(owner_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    f"✅ Broadcast sent to <b>{sent}</b> group(s)"
                    + (f", failed on <b>{failed}</b>." if failed else "."),
                    parse_mode=enums.ParseMode.HTML,
                )
        elif action in ("1", "2", "3"):
            hours = int(action)
            broadcaster.schedule(bot, text, hours)
            broadcaster.clear_pending(owner_id)
            with contextlib.suppress(Exception):
                await query.answer(f"Scheduled every {hours}h ✅")
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    f"⏰ Broadcast scheduled every <b>{hours}h</b> to "
                    f"<b>{len(broadcaster.known_chats())}</b> group(s).\n\n"
                    f"<b>Message:</b>\n{text}\n\n"
                    "Use /broadcast → 🚫 Cancel Schedule to stop it.",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=broadcast_schedule_menu(active_hours=hours),
                )
