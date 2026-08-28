---
name: Telegram voice-chat music bots (py-tgcalls)
description: Setup, session generation, and gotchas for Telegram voice-chat bots using pyrofork + py-tgcalls on Replit.
---

# Telegram voice-chat music bots (py-tgcalls)

## Library choice
- Use **pyrofork** (not pyrogram). The package name is `pyrofork` in pyproject.toml but it patches the `pyrogram` namespace, so `from pyrogram import Client` works.

**Why:** pyrogram is no longer maintained; pyrofork is the active fork and is what py-tgcalls targets.

## Session string generation

**Use a Replit Workflow, not ShellExec background/nohup.**

ShellExec background processes (`nohup ... &`, `setsid ... &`) are killed between ShellExec calls. Workflows are managed by Replit and stay alive across turns.

**How to apply:** When generating a TELEGRAM_SESSION_STRING:
1. `configureWorkflow({ name: "Session Generator", command: "python services/telegram-music-bot/generate_session_interactive.py <phone>", outputType: "console", autoStart: true })`
2. Poll `.session_tmp/result.txt` for "WAITING_FOR_CODE"
3. Ask user for OTP, then write it to `.session_tmp/code_input.txt`
4. Poll result.txt for "SESSION_STRING:"
5. `removeWorkflow({ name: "Session Generator" })`
6. Write session to `.session_tmp/session_string.txt` — config.py reads this first

**Why:** The interactive script keeps one live Pyrogram connection open (avoids PHONE_CODE_EXPIRED). ShellExec background processes die between turns.

## Session string copy-paste corruption
Avoid asking users to copy-paste session strings — they frequently truncate or corrupt them (base64 decoding errors, struct unpack size mismatches). Instead:
- `generate_session_interactive.py` writes the session to `.session_tmp/result.txt` and `.session_tmp/session_string.txt`
- `config.py` reads the local file FIRST (before the Replit Secret), so no paste is needed
- Replit Secrets can have a stale/corrupted value; the local file silently takes priority

**Why:** Session strings are 360+ chars of base64; partial copies cause `binascii.Error: Invalid base64` or `struct.error: unpack requires a buffer of N bytes`.

## Required secrets
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — from https://my.telegram.org
- `TELEGRAM_BOT_TOKEN` — from @BotFather (can expire; get a fresh one if `ACCESS_TOKEN_EXPIRED`)
- `TELEGRAM_SESSION_STRING` — generated once via the workflow method above

## yt-dlp EJS JS runtime configuration (bun on Replit)

yt-dlp defaults to deno for JS challenge solving (signature + n-parameter), but only bun is available on Replit. Without a working JS runtime, all audio formats are missing and only storyboard/image formats survive.

**Fix:** Pass `js_runtimes` as a dict with an explicit path:
```python
import shutil
bun = shutil.which("bun")
opts["js_runtimes"] = {"bun": {"path": bun}}  # NOT a string — must be dict
```

**Why:** The `js_runtimes` option takes `{runtime_name: {"path": "..."}}`. Passing a string raises `ValueError: Invalid js_runtimes format, expected a dict of {runtime: {config}}`. The path key inside the config dict is what tells yt-dlp where to find the binary (useful when bun is in the Nix store, not a standard PATH location).

**Also:** With a logged-in cookies file where YouTube's SABR experiment is active on that account, the `web_safari` client returns storyboard-only formats too. The JS runtime fix unlocks the web client's DASH audio streams for non-SABR accounts.

## Session source priority

The `TELEGRAM_SESSION_STRING` environment secret is authoritative when present; only use the local `.session_tmp/session_string.txt` fallback when no environment value exists.

**Why:** Telegram can revoke an old exported session. A stale local file must not override a newly rotated Replit or Render secret.

**How to apply:** After rotating the assistant session, update the Replit Secret and Render environment variable. Restart the bot so the process reloads the value.

## YouTube cookies can expire or be rejected by the server IP

When yt-dlp reports `Sign in to confirm you're not a bot` across all player clients, upgrading yt-dlp or changing the JS runtime will not fix it; the cookies need to be freshly exported from a logged-in browser and supplied to the running environment.

**Why:** YouTube may reject older browser cookies or a cloud provider IP even when Telegram authentication is healthy.

**How to apply:** Keep the local cookies file out of deployment configuration where possible. For Render, set `YOUTUBE_COOKIES_B64` from a fresh Netscape cookie export; for Replit, restart after replacing the local file or secret.

## Session file fallback (config.py pattern)
```python
def _get_session():
    local = os.path.join(os.path.dirname(__file__), ".session_tmp", "session_string.txt")
    if os.path.exists(local):
        val = open(local).read().strip()
        if val:
            return val
    return os.environ.get("TELEGRAM_SESSION_STRING") or None
```

## Video playback
PyTgCalls video playback can combine a direct video URL and a separate direct audio URL with `MediaStream(audio_path=..., video_parameters=...)`; use a required video flag when the command is explicitly video.

**Why:** yt-dlp commonly selects separate adaptive video and audio formats, while passing only the video format produces a silent or incomplete voice-chat stream.

**How to apply:** Keep media type on each queued track so mixed `/play` and `/vplay` queues select the correct `MediaStream` configuration at start time.
