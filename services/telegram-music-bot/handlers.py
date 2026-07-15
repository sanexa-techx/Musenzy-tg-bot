"""Command and callback handlers for the music bot."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from pyrogram import Client, filters
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, UserAlreadyParticipant, UserNotParticipant
from pyrogram.types import CallbackQuery, Message

from config import LOGO_PATH
from keyboards import player_controls, welcome_menu
from player import VoiceChatPlayer
from progress import NowPlayingTracker, render_bar
from queue_manager import QueueManager, Track
from youtube import TrackNotFound, TrackTooLong, resolve_and_download

log = logging.getLogger("handlers")

COMMANDS_TEXT = (
    "Commands:\n"
    "/play <song name or link> -- play or queue a track\n"
    "/skip -- skip the current track\n"
    "/pause -- pause playback\n"
    "/resume -- resume playback\n"
    "/stop -- stop and leave the voice chat\n"
    "/queue -- show the current queue"
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
    duration = _format_duration(track.duration)
    if position is None:
        return f"🎶 Now playing: {track.title}\n⏱ Duration: {duration}\n👤 Requested by: {track.requested_by}"
    return (
        f"➕ Queued at #{position}: {track.title}\n⏱ Duration: {duration}\n👤 Requested by: {track.requested_by}"
    )


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


def register_handlers(bot: Client, assistant: Client, player: VoiceChatPlayer, queues: QueueManager) -> None:
    tracker = NowPlayingTracker()

    # Deduplication: track message IDs we've already started handling so that
    # Telegram re-deliveries (which happen when the bot is slow) don't fire the
    # handler a second time for the same /play command.
    _seen_message_ids: set[int] = set()
    # Per-chat locks: prevent two concurrent /play downloads in the same chat.
    _chat_locks: dict[int, asyncio.Lock] = {}

    def _controls(chat_id: int):
        return player_controls(paused=queues.state(chat_id).paused)

    async def _post_now_playing(chat_id: int, track: Track) -> None:
        """Sends a fresh "now playing" message and starts its live progress
        bar. Fires on every track start -- the initial /play, /skip, button
        skips, and automatic advance when a track finishes."""
        caption = _format_track(track)
        bar = render_bar(0, track.duration, paused=False)
        text = f"{caption}\n\n{bar}"
        try:
            if track.thumbnail:
                try:
                    message = await bot.send_photo(
                        chat_id, track.thumbnail, caption=text, reply_markup=_controls(chat_id)
                    )
                except FloodWait as e:
                    log.warning("FloodWait %ds on send_photo for chat %s — falling back to text", e.value, chat_id)
                    await asyncio.sleep(min(e.value, 10))
                    # Fall back to a text-only message so the user always gets a response.
                    message = await bot.send_message(chat_id, text, reply_markup=_controls(chat_id))
            else:
                message = await bot.send_message(chat_id, text, reply_markup=_controls(chat_id))
        except Exception:
            log.exception("Failed to post now-playing message for chat %s", chat_id)
            return
        tracker.start(chat_id, message, track.duration, caption, lambda cid=chat_id: _controls(cid))

    async def _post_queue_empty(chat_id: int) -> None:
        """Fires once the queue runs out and the assistant has left the
        voice chat -- stops the progress tracker and lets everyone know."""
        tracker.stop(chat_id)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, "✅ Queue finished, left the voice chat.")

    player.on_track_start = _post_now_playing
    player.on_queue_empty = _post_queue_empty

    @bot.on_message(filters.command("start") & filters.private)
    async def start_cmd(_client: Client, message: Message) -> None:
        user = message.from_user.mention if message.from_user else "there"
        caption = (
            f"Welcome {user} ,this is Musenzy a powerfull,free,music bot for you\n\n"
            "Add me to a group as admin with \"Invite users via link\" permission, start the group's "
            "voice chat, then use /play <song name or link> -- I'll bring the music assistant in "
            "automatically. Works independently in every group I'm in."
        )
        await message.reply_photo(LOGO_PATH, caption=caption, reply_markup=welcome_menu())

    @bot.on_callback_query(filters.regex(r"^menu:commands$"))
    async def menu_cb(_client: Client, query: CallbackQuery) -> None:
        await query.answer()
        await query.message.reply_text(COMMANDS_TEXT)

    @bot.on_message(filters.command("play") & filters.group)
    async def play_cmd(client: Client, message: Message) -> None:
        # Drop duplicate deliveries of the same message (Telegram re-sends
        # unacknowledged updates when the bot is slow, e.g. during yt-dlp fetch).
        if message.id in _seen_message_ids:
            return
        _seen_message_ids.add(message.id)
        # Keep the set bounded — discard old IDs after 500 entries.
        if len(_seen_message_ids) > 500:
            _seen_message_ids.discard(next(iter(_seen_message_ids)))

        query = message.text.split(maxsplit=1)
        if len(query) < 2:
            await message.reply_text("Usage: /play <song name or YouTube link>")
            return

        join_error = await _ensure_assistant_in_chat(client, assistant, message.chat.id)
        if join_error:
            await message.reply_text(join_error)
            return

        # Per-chat lock: only one download at a time per group.
        lock = _chat_locks.setdefault(message.chat.id, asyncio.Lock())
        if lock.locked():
            await message.reply_text("⏳ Already fetching a track — please wait.")
            return

        async with lock:
            status = await message.reply_text(f"🔍 Searching for \"{query[1]}\"")
            with contextlib.suppress(Exception):
                await message.delete()
            anim_task = asyncio.create_task(_animate_searching(status, query[1]))
            try:
                info = await resolve_and_download(query[1])
            except TrackTooLong as exc:
                await status.edit_text(str(exc))
                return
            except TrackNotFound:
                await status.edit_text("Couldn't find that track. Try a different search.")
                return
            except Exception:
                await status.edit_text("Something went wrong fetching that track. Try again.")
                raise
            finally:
                anim_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await anim_task

            requester = message.from_user.mention if message.from_user else "someone"
            track = Track(
                title=info["title"],
                url=info["url"],
                stream_url=info["url"],
                duration=info["duration"],
                thumbnail=info["thumbnail"],
                requested_by=requester,
                file_path=info["file_path"],
            )

            position = await player.play_or_enqueue(message.chat.id, track)
            if position == 0:
                # The on_track_start callback already posted the live now-playing
                # message with its progress bar -- just clear the search status.
                await status.delete()
            else:
                text = _format_track(track, position)
                if track.thumbnail:
                    await status.delete()
                    await message.reply_photo(track.thumbnail, caption=text)
                else:
                    await status.edit_text(text)

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_client: Client, message: Message) -> None:
        nxt = await player.play_next(message.chat.id)
        if nxt:
            await message.reply_text("⏭ Skipped.")

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(_client: Client, message: Message) -> None:
        await player.pause(message.chat.id)
        tracker.pause(message.chat.id)
        await message.reply_text("⏸ Paused.")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_client: Client, message: Message) -> None:
        await player.resume(message.chat.id)
        tracker.resume(message.chat.id)
        await message.reply_text("▶️ Resumed.")

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(_client: Client, message: Message) -> None:
        tracker.stop(message.chat.id)
        await player.stop(message.chat.id)
        await message.reply_text("Stopped and left the voice chat.")

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_client: Client, message: Message) -> None:
        state = queues.state(message.chat.id)
        if not state.current:
            await message.reply_text("Nothing is playing right now.")
            return
        lines = [_format_track(state.current)]
        for i, track in enumerate(state.queue, start=1):
            lines.append(f"{i}. {track.title} -- requested by {track.requested_by}")
        await message.reply_text("\n".join(lines))

    @bot.on_callback_query(filters.regex(r"^ctl:"))
    async def controls_cb(_client: Client, query: CallbackQuery) -> None:
        action = query.data.split(":", 1)[1]
        chat_id = query.message.chat.id
        state = queues.state(chat_id)

        if action == "pauseresume":
            if state.paused:
                await player.resume(chat_id)
                tracker.resume(chat_id)
                await query.answer("Resumed")
            else:
                await player.pause(chat_id)
                tracker.pause(chat_id)
                await query.answer("Paused")
            if query.message.reply_markup:
                await query.message.edit_reply_markup(player_controls(paused=state.paused))
        elif action == "skip":
            await player.play_next(chat_id)
            await query.answer("Skipped")
        elif action == "stop":
            tracker.stop(chat_id)
            await player.stop(chat_id)
            await query.answer("Stopped")
            await query.message.reply_text("Stopped and left the voice chat.")
        elif action == "queue":
            if not state.current:
                await query.answer("Nothing playing", show_alert=True)
                return
            lines = [_format_track(state.current)]
            for i, track in enumerate(state.queue, start=1):
                lines.append(f"{i}. {track.title} -- requested by {track.requested_by}")
            await query.answer()
            await query.message.reply_text("\n".join(lines))
        elif action == "close":
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.delete()
