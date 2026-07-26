"""YouTube search + audio extraction backed by yt-dlp."""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import time
import uuid

import yt_dlp

from config import DOWNLOAD_DIR, MAX_TRACK_SECONDS

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Cookies file: authenticates with YouTube to bypass bot-check on cloud IPs.
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

# ── JS runtime (computed once at startup) ────────────────────────────────────
# yt-dlp needs a JS runtime to solve YouTube's signature/n-challenges.
# Only deno is enabled by default; bun (installed here) must be passed
# explicitly as {"runtime": {"path": "..."}}.

def _compute_js_runtimes() -> dict:
    bun = shutil.which("bun")
    if bun:
        return {"bun": {"path": bun}}
    node = shutil.which("node")
    if node:
        return {"node": {"path": node}}
    return {"deno": {}}

_JS_RUNTIMES: dict = _compute_js_runtimes()


def _base_opts() -> dict:
    opts: dict = {"js_runtimes": _JS_RUNTIMES}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


# ── Audio format ─────────────────────────────────────────────────────────────
# Prefer 160 kbps Opus (YouTube's best audio-only stream), then any Opus,
# then 128+ kbps anything, then whatever is available.
_AUDIO_FORMAT = (
    "bestaudio[acodec=opus][abr>=128]"
    "/bestaudio[acodec=opus]"
    "/bestaudio[abr>=128]"
    "/bestaudio"
)

# ── Shared yt-dlp option blocks ───────────────────────────────────────────────
_COMMON_OPTS: dict = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "socket_timeout": 15,        # fail fast on stalled connections
    "retries": 2,                # fewer retries = faster failure
    "concurrent_fragment_downloads": 4,  # speed up DASH segment fetching
}

_SEARCH_OPTS: dict = {
    **_COMMON_OPTS,
    **_base_opts(),
    "format": _AUDIO_FORMAT,
    "default_search": "ytsearch1",
    "skip_download": True,
}

_DOWNLOAD_OPTS: dict = {
    **_COMMON_OPTS,
    **_base_opts(),
    "format": _AUDIO_FORMAT,
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "320",   # 320 kbps — maximum fidelity
        }
    ],
}


# ── TTL result cache ──────────────────────────────────────────────────────────
# YouTube stream URLs are valid for ~6 h; we cache for 4 h.
# Repeat plays of the same song are served instantly from cache.
_CACHE_TTL = 4 * 3600      # seconds
_CACHE_MAX = 200            # max entries before LRU eviction
_cache: dict[str, tuple[float, dict]] = {}   # key -> (expires_monotonic, result)


def _ck(query: str) -> str:
    """Normalised cache key."""
    return query.strip().lower()


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if not entry:
        return None
    expires, result = entry
    if time.monotonic() > expires:
        _cache.pop(key, None)
        return None
    return result


def _cache_set(key: str, result: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic() + _CACHE_TTL, result)


# ── Exceptions ────────────────────────────────────────────────────────────────

class TrackNotFound(Exception):
    pass


class TrackTooLong(Exception):
    pass


# ── Sync helpers (run in thread executor) ─────────────────────────────────────

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
        # yt-dlp may keep the original extension when postprocessing skipped.
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(out_id):
                return os.path.join(DOWNLOAD_DIR, fname)
        raise TrackNotFound(video_url)
    return final_path


# ── Public async API ──────────────────────────────────────────────────────────

async def resolve_stream_url(query: str) -> dict:
    """Fast path: resolve a YouTube search/URL to a direct audio stream URL.

    Cached for 4 h — repeat requests for the same query are instant.
    Falls back to a fresh extraction on cache miss.
    py-tgcalls feeds the URL directly to ffmpeg (no disk I/O needed).
    """
    key = _ck(query)
    cached = _cache_get(key)
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(2):
        if attempt:
            await asyncio.sleep(1)
        try:
            info = await loop.run_in_executor(None, _extract_info_sync, query)

            duration = int(info.get("duration") or 0)
            if duration and duration > MAX_TRACK_SECONDS:
                raise TrackTooLong(
                    f"{info.get('title')} is longer than the {MAX_TRACK_SECONDS}s limit"
                )

            stream_url = info.get("url") or info.get("webpage_url") or query
            video_url  = info.get("webpage_url") or info.get("original_url") or stream_url

            result = {
                "title":     info.get("title") or "Unknown title",
                "url":       video_url,
                "duration":  duration,
                "thumbnail": info.get("thumbnail"),
                "file_path": stream_url,   # direct audio stream → MediaStream(url)
            }
            _cache_set(key, result)
            return result

        except (TrackNotFound, TrackTooLong):
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise last_exc  # type: ignore[misc]


async def resolve_and_download(query: str) -> dict:
    """Full download to disk — used for playlist pre-loading and autoplay prefetch.
    Prefer resolve_stream_url for interactive /play commands.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(2):
        if attempt:
            await asyncio.sleep(2)
        try:
            info = await loop.run_in_executor(None, _extract_info_sync, query)

            duration = int(info.get("duration") or 0)
            if duration and duration > MAX_TRACK_SECONDS:
                raise TrackTooLong(f"{info.get('title')} is longer than the {MAX_TRACK_SECONDS}s limit")

            video_url = info.get("webpage_url") or info.get("url") or query
            out_id    = uuid.uuid4().hex
            file_path = await loop.run_in_executor(None, _download_sync, video_url, out_id)

            return {
                "title":     info.get("title") or "Unknown title",
                "url":       video_url,
                "duration":  duration,
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
    """Return flat playlist entry dicts (id, title, url) without downloading.
    Fast — uses yt-dlp extract_flat mode.
    """
    loop = asyncio.get_running_loop()

    def _sync() -> list[dict]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
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
            vid_url = (
                e.get("url")
                or e.get("webpage_url")
                or f"https://www.youtube.com/watch?v={e['id']}"
            )
            results.append({"id": e["id"], "title": e.get("title") or "Unknown", "url": vid_url})
        return results[:max_tracks]

    return await loop.run_in_executor(None, _sync)


def cleanup_file(file_path: str) -> None:
    """Delete a downloaded audio file. Skips HTTP stream URLs (nothing to delete)."""
    try:
        if file_path and not file_path.startswith("http") and os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass


# ── Autoplay: YouTube Radio Mix ───────────────────────────────────────────────

import re as _re


def _extract_video_id(url: str) -> str | None:
    """Pull the 11-char video ID from any YouTube watch URL."""
    m = _re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _get_radio_mix_entry_sync(
    video_id: str, played_ids: frozenset[str] = frozenset()
) -> dict | None:
    """Fetch candidates from the YouTube Radio Mix and return one at random,
    preferring tracks not in played_ids (recent history).
    """
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": True,
        "playlistend": 15,
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
    fresh = [e for e in all_entries if e["id"] not in played_ids]
    pool  = fresh if fresh else all_entries
    if not pool:
        return None

    return random.choice(pool[:8])


async def get_related_track(
    last_url: str, played_ids: frozenset[str] = frozenset()
) -> dict | None:
    """Return a ready-to-stream track dict for the next autoplay song.

    Uses the YouTube Radio Mix seeded on the last-played video.
    Stream-URL path — transitions are near-instant.
    """
    video_id = _extract_video_id(last_url)
    if not video_id:
        return None

    loop  = asyncio.get_running_loop()
    entry = await loop.run_in_executor(
        None, _get_radio_mix_entry_sync, video_id, played_ids
    )
    if not entry:
        return None

    related_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    try:
        # Use fast stream-URL path for instant autoplay transitions
        return await resolve_stream_url(related_url)
    except Exception:
        return None
