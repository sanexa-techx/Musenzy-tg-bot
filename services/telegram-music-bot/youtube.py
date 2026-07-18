"""YouTube search + audio extraction backed by yt-dlp."""
from __future__ import annotations

import asyncio
import os
import random
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


async def resolve_stream_url(query: str) -> dict:
    """Fast path: resolve a YouTube search/URL and return the direct audio
    stream URL — no download, no disk I/O.  Typically 3–6 s vs 15–30 s for
    a full download.  pytgcalls feeds the URL straight to ffmpeg.

    Falls back to resolve_and_download on repeated failures.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(3):
        if attempt:
            await asyncio.sleep(1)
        try:
            info = await loop.run_in_executor(None, _extract_info_sync, query)

            duration = int(info.get("duration") or 0)
            if duration and duration > MAX_TRACK_SECONDS:
                raise TrackTooLong(
                    f"{info.get('title')} is longer than the {MAX_TRACK_SECONDS}s limit"
                )

            # info["url"] is the direct audio stream URL (bestaudio format).
            stream_url = info.get("url") or info.get("webpage_url") or query
            video_url = info.get("webpage_url") or info.get("original_url") or stream_url

            return {
                "title": info.get("title") or "Unknown title",
                "url": video_url,          # YouTube watch page
                "duration": duration,
                "thumbnail": info.get("thumbnail"),
                "file_path": stream_url,   # direct audio stream → MediaStream(url)
            }
        except (TrackNotFound, TrackTooLong):
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise last_exc  # type: ignore[misc]


async def resolve_and_download(query: str) -> dict:
    """Full download to disk (kept for playlist pre-loading).
    Prefer resolve_stream_url for interactive /play commands.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(3):
        if attempt:
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
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise last_exc  # type: ignore[misc]


async def fetch_playlist_entries(url: str, max_tracks: int = 50) -> list[dict]:
    """Return a list of flat playlist entry dicts (id, title, url) without
    downloading audio. Fast — uses yt-dlp extract_flat mode.

    Caps at *max_tracks* entries. Raises ValueError for non-playlist URLs.
    """
    loop = asyncio.get_running_loop()

    def _sync() -> list[dict]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": max_tracks,
            **_base_opts(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("Could not fetch playlist info")
        entries = info.get("entries") or []
        results = []
        for e in entries:
            if not e or not e.get("id"):
                continue
            vid_url = e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}"
            results.append({
                "id": e["id"],
                "title": e.get("title") or "Unknown",
                "url": vid_url,
            })
        return results[:max_tracks]

    return await loop.run_in_executor(None, _sync)


def cleanup_file(file_path: str) -> None:
    """Delete a downloaded audio file.  Skips HTTP stream URLs (nothing to delete)."""
    try:
        if file_path and not file_path.startswith("http") and os.path.exists(file_path):
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


def _get_radio_mix_entry_sync(
    video_id: str, played_ids: frozenset[str] = frozenset()
) -> dict | None:
    """Fetch candidates from the YouTube Radio Mix for *video_id* and return
    one, preferring tracks not in *played_ids* (recent history) and picking
    randomly among the top results so autoplay feels varied.
    """
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 15,          # fetch more candidates for variety
        **_base_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(mix_url, download=False)
    except Exception:
        return None

    if not info or "entries" not in info:
        return None

    all_entries = [e for e in info["entries"] if e and e.get("id") and e["id"] != video_id]

    # Prefer entries not in recent-play history
    fresh = [e for e in all_entries if e["id"] not in played_ids]
    pool = fresh if fresh else all_entries   # fallback: allow repeats if nothing fresh
    if not pool:
        return None

    # Pick randomly from up to the first 8 candidates so each listen is different
    return random.choice(pool[:8])


async def get_related_track(
    last_url: str, played_ids: frozenset[str] = frozenset()
) -> dict | None:
    """Return a ready-to-stream track dict for the next autoplay song.

    Uses the YouTube Radio Mix seeded on the last-played video.  Prefers
    tracks not in *played_ids* and uses the fast stream-URL path so autoplay
    transitions are near-instant.
    Returns None if no related track can be found.
    """
    video_id = _extract_video_id(last_url)
    if not video_id:
        return None

    loop = asyncio.get_running_loop()
    entry = await loop.run_in_executor(
        None, _get_radio_mix_entry_sync, video_id, played_ids
    )
    if not entry:
        return None

    related_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    try:
        return await resolve_and_download(related_url)
    except Exception:
        return None
