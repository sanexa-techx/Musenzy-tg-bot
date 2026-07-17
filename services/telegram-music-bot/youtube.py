"""YouTube search + audio extraction backed by yt-dlp."""
from __future__ import annotations

import asyncio
import os
import uuid

import yt_dlp

from config import DOWNLOAD_DIR, MAX_TRACK_SECONDS

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Cookies file: authenticates with YouTube to bypass bot-check on cloud IPs.
# Drop a Netscape-format cookies.txt here and it's picked up automatically.
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

# JS runtime: yt-dlp needs a JS runtime to solve YouTube's signature/n-challenges
# for DASH/bestaudio formats. Node 20 (present here) is below yt-dlp's minimum
# of v22, but Bun 1.3.6 meets the requirement (>=1.2.11). Pass js_runtimes
# explicitly — yt-dlp defaults to deno-only which is not installed here.
# yt_dlp_ejs (Python package, installed via pip) provides the solver scripts.
_JS_RUNTIMES = {"bun": {}}


def _base_opts() -> dict:
    opts: dict = {"js_runtimes": _JS_RUNTIMES}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


_SEARCH_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "skip_download": True,
    **_base_opts(),
}

_DOWNLOAD_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    **_base_opts(),
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "192",
        }
    ],
}


class TrackNotFound(Exception):
    pass


class TrackTooLong(Exception):
    pass


def _extract_info_sync(query: str) -> dict:
    with yt_dlp.YoutubeDL(_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    if not info:
        raise TrackNotFound(query)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise TrackNotFound(query)
        info = entries[0]
    return info


def _download_sync(video_url: str, out_id: str) -> str:
    opts = dict(_DOWNLOAD_OPTS)
    opts["outtmpl"] = os.path.join(DOWNLOAD_DIR, f"{out_id}.%(ext)s")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)
    final_path = os.path.join(DOWNLOAD_DIR, f"{out_id}.opus")
    if not os.path.exists(final_path):
        # yt-dlp may keep the original extension if postprocessing didn't run.
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(out_id):
                return os.path.join(DOWNLOAD_DIR, fname)
        raise TrackNotFound(video_url)
    return final_path


async def resolve_and_download(query: str) -> dict:
    """Search YouTube for `query` (or accept a direct URL), download the audio,
    and return track metadata including the local file path to stream.

    Retries up to 2 times on transient errors (e.g. bun cold-start, JS
    challenge flakiness). TrackNotFound and TrackTooLong are not retried.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(3):
        if attempt:
            # Brief pause before retry so bun/yt-dlp can settle.
            await asyncio.sleep(2)
        try:
            info = await loop.run_in_executor(None, _extract_info_sync, query)

            duration = int(info.get("duration") or 0)
            if duration and duration > MAX_TRACK_SECONDS:
                raise TrackTooLong(f"{info.get('title')} is longer than the {MAX_TRACK_SECONDS}s limit")

            video_url = info.get("webpage_url") or info.get("url") or query
            out_id = uuid.uuid4().hex
            file_path = await loop.run_in_executor(None, _download_sync, video_url, out_id)

            return {
                "title": info.get("title") or "Unknown title",
                "url": video_url,
                "duration": duration,
                "thumbnail": info.get("thumbnail"),
                "file_path": file_path,
            }
        except (TrackNotFound, TrackTooLong):
            raise  # definitive — no point retrying
        except Exception as exc:
            last_exc = exc
            continue

    raise last_exc  # type: ignore[misc]


def cleanup_file(file_path: str) -> None:
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Autoplay: YouTube Radio Mix
# ---------------------------------------------------------------------------

import re as _re


def _extract_video_id(url: str) -> str | None:
    """Pull the 11-char video ID from any YouTube watch URL."""
    m = _re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _get_radio_mix_entry_sync(video_id: str) -> dict | None:
    """Fetch the first *different* entry from the YouTube Radio Mix for video_id.

    YouTube builds a personalised "RD<video_id>" mix playlist that contains
    the seed track as entry[0] followed by related recommendations — exactly
    what YouTube's own autoplay uses.
    """
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # fast: only metadata, no full extraction
        "playlistend": 6,       # grab a few in case entry[0] is the seed itself
        **_base_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(mix_url, download=False)
    except Exception:
        return None

    if not info or "entries" not in info:
        return None

    entries = [e for e in info["entries"] if e and e.get("id") and e.get("id") != video_id]
    return entries[0] if entries else None


async def get_related_track(last_url: str) -> dict | None:
    """Return a fully downloaded track dict for the next autoplay song.

    Uses the YouTube Radio Mix seeded on the last-played video.
    Returns None if no related track can be found or downloaded.
    """
    video_id = _extract_video_id(last_url)
    if not video_id:
        return None

    loop = asyncio.get_running_loop()
    entry = await loop.run_in_executor(None, _get_radio_mix_entry_sync, video_id)
    if not entry:
        return None

    related_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    try:
        return await resolve_and_download(related_url)
    except Exception:
        return None
