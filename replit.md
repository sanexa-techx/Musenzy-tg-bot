# Telegram Voice Chat Music Bot

A Telegram bot that joins a group's live voice chat and plays music from YouTube on command, with independent queues across every group it's added to.

## Run & Operate

- Workflow `Telegram Music Bot` — runs `python services/telegram-music-bot/main.py`
- `pnpm --filter @workspace/api-server run dev` — run the shared API server (unused by the bot today)
- Required secrets: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SESSION_STRING`

## Stack

- Python 3.11, Pyrogram (MTProto client), `py-tgcalls` (voice chat streaming), `yt-dlp` + `yt-dlp-ejs` (YouTube search/audio + JS challenge solver), Bun (JS runtime for yt-dlp signature solving), ffmpeg
- Node/TypeScript pnpm workspace remains for other artifacts (API server, mockup sandbox) but is not used by the bot

## Where things live

- `services/telegram-music-bot/` — the whole bot
  - `main.py` — starts the bot client and the assistant client together
  - `config.py` — env/secret loading
  - `handlers.py` — `/play`, `/skip`, `/pause`, `/resume`, `/stop`, `/queue` commands + inline button callbacks
  - `player.py` — wraps `py-tgcalls` to join/stream/leave voice chats per group
  - `queue_manager.py` — per-chat queue and now-playing state
  - `youtube.py` — YouTube search + audio download via yt-dlp
  - `keyboards.py` — inline keyboard (Pause/Resume, Skip, Stop, Queue buttons)
  - `generate_session.py` — one-time helper to produce `TELEGRAM_SESSION_STRING`

## Architecture decisions

- Telegram's Bot API cannot join or stream audio into a group voice chat — only a regular user account (MTProto) can. So there are two Telegram clients: a **bot** (via BotFather token) that handles commands/buttons, and an **assistant** (a real user account, logged in via a saved session string) that actually joins the voice chat and streams audio. Both must be in the group, and the group's voice chat must already be started.
- Each group chat gets its own queue (`QueueManager`), so multiple groups can play different tracks simultaneously.
- Audio is downloaded to a local file (not live-streamed) before playback, so `py-tgcalls` can stream a stable local media source; files are deleted after they finish playing.

## Product

- Add the bot to any group, start that group's voice chat, then `/play <song name or link>` to queue a track.
- `/skip`, `/pause`, `/resume`, `/stop`, `/queue` plus inline buttons (Pause/Resume, Skip, Stop, Queue) on the now-playing message.
- Now-playing messages show the track's YouTube thumbnail when available.
- Works independently across every group the bot is a member of.

## User preferences

_None recorded yet._

## Gotchas

- The group's voice chat must be manually started in Telegram before `/play` will work — the assistant account can only join an already-active voice chat.
- `TELEGRAM_SESSION_STRING` belongs to a real Telegram user account (the "assistant"), not the bot. Generate it once with `services/telegram-music-bot/generate_session.py` (see script docstring) and never share that string — it grants full access to that account.
- YouTube extraction depends on yt-dlp staying ahead of YouTube's changes; if `/play` starts failing, `yt-dlp` may need an upgrade.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details (mostly not exercised by this bot).
