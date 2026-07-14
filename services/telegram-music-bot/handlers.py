"""Command and callback handlers for the music bot."""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.errors import UserAlreadyParticipant, UserNotParticipant
from pyrogram.types import CallbackQuery, Message

from config import LOGO_PATH
from keyboards import player_controls, welcome_menu
from player import VoiceChatPlayer
from queue_manager import QueueManager, Track
from youtube import TrackNotFound, TrackTooLong, resolve_and_download

COMMANDS_TEXT = (
    "Commands:\n"
    "/play <song name or link> -- play or queue a track\n"
    "/skip -- skip the current track\n"
    "/pause -- pause playback\n"
    "/resume -- resume playback\n"
    "/stop -- stop and leave the voice chat\n"
    "/queue -- show the current queue"
)


def _format_track(track: Track, position: int | None = None) -> str:
    mins, secs = divmod(track.duration, 60)
    duration = f"{mins}:{secs:02d}" if track.duration else "live"
    if position is None:
        return f"Now playing: {track.title} ({duration})\nRequested by {track.requested_by}"
    return f"Queued at #{position}: {track.title} ({duration})\nRequested by {track.requested_by}"


async def _ensure_assistant_in_chat(client: Client, assistant: Client, chat_id: int) -> str | None:
    """Make sure the music assistant account is a member of this chat, joining it
    automatically via a fresh invite link if it isn't yet. Returns an error
    message to show the user, or None on success."""
    try:
        await assistant.get_chat_member(chat_id, "me")
        return None
    except UserNotParticipant:
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
        query = message.text.split(maxsplit=1)
        if len(query) < 2:
            await message.reply_text("Usage: /play <song name or YouTube link>")
            return

        join_error = await _ensure_assistant_in_chat(client, assistant, message.chat.id)
        if join_error:
            await message.reply_text(join_error)
            return

        status = await message.reply_text(f"Searching for \"{query[1]}\"...")
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
        text = _format_track(track, position if position else None)
        if track.thumbnail:
            await status.delete()
            await message.reply_photo(
                track.thumbnail,
                caption=text,
                reply_markup=player_controls(paused=False) if position == 0 else None,
            )
        else:
            await status.edit_text(text, reply_markup=player_controls(paused=False) if position == 0 else None)

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_client: Client, message: Message) -> None:
        nxt = await player.play_next(message.chat.id)
        if nxt:
            await message.reply_text(f"Skipped. {_format_track(nxt)}", reply_markup=player_controls(paused=False))
        else:
            await message.reply_text("Skipped. Queue is empty, leaving the voice chat.")

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(_client: Client, message: Message) -> None:
        await player.pause(message.chat.id)
        await message.reply_text("Paused.", reply_markup=player_controls(paused=True))

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_client: Client, message: Message) -> None:
        await player.resume(message.chat.id)
        await message.reply_text("Resumed.", reply_markup=player_controls(paused=False))

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(_client: Client, message: Message) -> None:
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
                await query.answer("Resumed")
            else:
                await player.pause(chat_id)
                await query.answer("Paused")
            if query.message.reply_markup:
                await query.message.edit_reply_markup(player_controls(paused=not state.paused))
        elif action == "skip":
            nxt = await player.play_next(chat_id)
            await query.answer("Skipped")
            if nxt:
                await query.message.reply_text(_format_track(nxt), reply_markup=player_controls(paused=False))
            else:
                await query.message.reply_text("Queue is empty, leaving the voice chat.")
        elif action == "stop":
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
