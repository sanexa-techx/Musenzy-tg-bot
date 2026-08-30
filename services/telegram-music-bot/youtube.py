"""YouTube search + audio extraction backed by yt-dlp."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import shutil
import subprocess
import time
import uuid

import yt_dlp
import aiohttp

from config import DOWNLOAD_DIR, MAX_TRACK_SECONDS

log = logging.getLogger("youtube")

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
    # On Render, main.py writes this file from YOUTUBE_COOKIES_B64 after
    # imports have completed. Include the path now so yt-dlp sees it later.
    if os.path.exists(COOKIES_FILE) or os.environ.get("YOUTUBE_COOKIES_B64"):
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

_VIDEO_FORMAT = (
    "bestvideo[height<=720]+bestaudio"
    "/best[height<=720]"
    "/best"
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

_VIDEO_SEARCH_OPTS: dict = {
    **_COMMON_OPTS,
    **_base_opts(),
    "format": _VIDEO_FORMAT,
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


class YouTubeBlocked(Exception):
    """YouTube rejected the request because authentication/anti-bot checks failed."""


# ── Sync helpers (run in thread executor) ─────────────────────────────────────

def _extract_info_sync(query: str, opts: dict | None = None) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts or _SEARCH_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).lower()
        if any(
            phrase in message
            for phrase in (
                "sign in to confirm",
                "not a bot",
                "confirm you're not a bot",
                "confirm you’re not a bot",
                "use --cookies",
            )
        ):
            raise YouTubeBlocked(
                "YouTube rejected this server. A fresh YouTube cookies export is required."
            ) from exc
        raise
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


async def resolve_video_stream_url(query: str) -> dict:
    """Resolve a YouTube result to separate direct video and audio URLs."""
    key = f"video:{_ck(query)}"
    cached = _cache_get(key)
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(2):
        if attempt:
            await asyncio.sleep(1)
        try:
            info = await loop.run_in_executor(
                None,
                _extract_info_sync,
                query,
                _VIDEO_SEARCH_OPTS,
            )
            duration = int(info.get("duration") or 0)
            if duration and duration > MAX_TRACK_SECONDS:
                raise TrackTooLong(
                    f"{info.get('title')} is longer than the {MAX_TRACK_SECONDS}s limit"
                )

            requested_formats = info.get("requested_formats") or []
            video_format = next(
                (
                    item
                    for item in requested_formats
                    if item.get("vcodec") not in (None, "none")
                ),
                None,
            )
            audio_format = next(
                (
                    item
                    for item in requested_formats
                    if item.get("acodec") not in (None, "none")
                ),
                None,
            )

            video_url = (video_format or {}).get("url")
            audio_url = (audio_format or {}).get("url")
            if not video_url and info.get("vcodec") not in (None, "none"):
                video_url = info.get("url")
            if not audio_url and info.get("acodec") not in (None, "none"):
                audio_url = info.get("url")
            if not video_url:
                raise TrackNotFound(query)

            video_page_url = (
                info.get("webpage_url")
                or info.get("original_url")
                or query
            )
            result = {
                "title": info.get("title") or "Unknown title",
                "url": video_page_url,
                "duration": duration,
                "thumbnail": info.get("thumbnail"),
                "file_path": video_url,
                "audio_path": audio_url,
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
            vid_url = _entry_watch_url(e)
            if not vid_url:
                continue
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


def _to_jpeg(content: bytes) -> bytes | None:
    """Return Telegram-compatible JPEG bytes for any supported image input."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-err_detect",
                "explode",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "3",
                "pipe:1",
            ],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
        if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8\xff"):
            return result.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _atomic_write(path: str, content: bytes) -> None:
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "wb") as output:
            output.write(content)
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


async def download_thumbnail(
    thumbnail_url: str | None,
    video_url: str = "",
) -> str | None:
    """Download a Telegram-safe thumbnail and return its local path.

    Telegram's servers occasionally cannot fetch YouTube's remote thumbnail
    URL, especially ``maxresdefault.jpg`` when that rendition is unavailable.
    Downloading it here lets the Bot API upload the bytes directly and also
    gives us several YouTube quality fallbacks.
    """
    if thumbnail_url and os.path.isfile(thumbnail_url):
        return thumbnail_url

    video_id = _extract_video_id(video_url) or _extract_video_id(thumbnail_url or "")
    cache_key = video_id or hashlib.sha1(
        (thumbnail_url or video_url).encode("utf-8")
    ).hexdigest()
    if not cache_key:
        return None

    # Bump the cache namespace so malformed files written by older versions
    # cannot be reused after the validation/format fixes.
    cache_path = os.path.join(DOWNLOAD_DIR, f"thumbnail-v2-{cache_key}.jpg")
    try:
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1024:
            with open(cache_path, "rb") as cached_file:
                cached_content = cached_file.read(8 * 1024 * 1024)
            cached_jpeg = await asyncio.to_thread(_to_jpeg, cached_content)
            if cached_jpeg:
                if not cached_content.startswith(b"\xff\xd8\xff"):
                    await asyncio.to_thread(_atomic_write, cache_path, cached_jpeg)
                return cache_path
            os.remove(cache_path)
    except OSError:
        pass

    candidates: list[str] = []
    if thumbnail_url:
        candidates.append(thumbnail_url)
    if video_id:
        for host in ("i.ytimg.com", "img.youtube.com"):
            for quality in ("maxresdefault", "hqdefault", "sddefault", "default"):
                candidates.append(
                    f"https://{host}/vi/{video_id}/{quality}.jpg"
                )

    unique_candidates = list(dict.fromkeys(candidates))
    if not unique_candidates:
        return None

    timeout = aiohttp.ClientTimeout(total=12)
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for candidate in unique_candidates:
                try:
                    async with session.get(candidate) as response:
                        if response.status != 200:
                            continue
                        content = await response.content.read(8 * 1024 * 1024)
                        content_type = response.headers.get("Content-Type", "").lower()
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) != len(content):
                                    continue
                            except ValueError:
                                pass
                        if not content or (
                            not content_type.startswith("image/")
                            and not content.startswith(
                                (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")
                            )
                        ):
                            continue
                        jpeg = await asyncio.to_thread(_to_jpeg, content)
                        if not jpeg:
                            continue
                        await asyncio.to_thread(_atomic_write, cache_path, jpeg)
                        return cache_path
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass

    log.debug("Could not download thumbnail for %s", video_url or thumbnail_url)
    return None


def _entry_watch_url(entry: dict) -> str | None:
    """Normalize yt-dlp flat-playlist entries to a usable watch URL.

    In extract_flat mode yt-dlp can put a bare video ID in ``url``. Passing
    that value back to another extractor causes autoplay to fail immediately.
    """
    for value in (entry.get("webpage_url"), entry.get("url")):
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    video_id = entry.get("id")
    if isinstance(video_id, str) and video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def _get_radio_mix_entries_sync(video_id: str) -> list[dict]:
    """Fetch usable candidates from one YouTube Radio Mix.

    Keeping the source order lets the async resolver score candidates across
    multiple mood seeds. A recommendation appearing in several mixes is a
    stronger mood match than one appearing in only the current mix.
    """
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": True,
        "playlistend": 25,
        **_base_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(mix_url, download=False)
    except Exception:
        return []

    if not info or "entries" not in info:
        return []

    all_entries = []
    for entry in info["entries"]:
        if not entry or not entry.get("id") or entry["id"] == video_id:
            continue
        duration = int(entry.get("duration") or 0)
        title = str(entry.get("title") or "")
        if duration and (
            duration < 30
            or duration > MAX_TRACK_SECONDS
        ):
            continue
        if "#short" in title.lower() or "/shorts/" in str(entry.get("webpage_url") or ""):
            continue
        if not _entry_watch_url(entry):
            continue
        all_entries.append(entry)

    return all_entries[:20]


async def get_related_track(
    last_url: str,
    played_ids: frozenset[str] = frozenset(),
    seed_urls: list[str] | tuple[str, ...] | None = None,
) -> dict | None:
    """Return a ready-to-stream track dict for the next autoplay song.

    Uses YouTube Radio Mixes from the current and recent songs as mood seeds.
    Candidates are ranked by how often and how highly they appear across those
    mixes. ``played_ids`` is the active session's strict no-repeat set; no
    fallback bypasses it.
    Stream-URL path — transitions are near-instant.
    """
    seed_urls = seed_urls or [last_url]
    seed_ids: list[str] = []
    seen_seed_ids: set[str] = set()
    for url in [last_url, *seed_urls]:
        video_id = _extract_video_id(url)
        if video_id and video_id not in seen_seed_ids:
            seen_seed_ids.add(video_id)
            seed_ids.append(video_id)
    if not seed_ids:
        return None

    loop = asyncio.get_running_loop()
    mix_results = await asyncio.gather(
        *(
            loop.run_in_executor(None, _get_radio_mix_entries_sync, video_id)
            for video_id in seed_ids
        ),
        return_exceptions=True,
    )

    # Score candidates by cross-seed agreement. The current song is first and
    # therefore gets the highest weight; older songs still preserve the mood.
    ranked: dict[str, tuple[dict, int]] = {}
    for seed_index, entries in enumerate(mix_results):
        if isinstance(entries, Exception):
            log.debug(
                "Mood seed %s recommendation lookup failed",
                seed_ids[seed_index],
                exc_info=entries,
            )
            continue
        seed_weight = len(seed_ids) - seed_index
        for position, entry in enumerate(entries):
            candidate_id = entry.get("id")
            if (
                not candidate_id
                or candidate_id in seed_ids
                or candidate_id in played_ids
            ):
                continue
            related_url = _entry_watch_url(entry)
            if not related_url:
                continue
            # Repeated appearance across mixes dominates; source position is
            # the tie-breaker so YouTube's strongest recommendations win.
            score = seed_weight * 10 + max(0, 20 - position)
            if candidate_id in ranked:
                previous, previous_score = ranked[candidate_id]
                ranked[candidate_id] = (previous, previous_score + score)
            else:
                ranked[candidate_id] = (entry, score)

    candidates = list(ranked.values())
    random.shuffle(candidates)
    candidates.sort(key=lambda item: item[1], reverse=True)
    for entry, _score in candidates:
        related_url = _entry_watch_url(entry)
        if not related_url:
            continue
        try:
            # Use fast stream-URL path for instant autoplay transitions.
            return await resolve_stream_url(related_url)
        except (TrackNotFound, TrackTooLong):
            log.debug("Skipping unusable autoplay recommendation %s", entry.get("id"))
        except Exception:
            log.debug(
                "Autoplay recommendation %s could not be resolved",
                entry.get("id"),
                exc_info=True,
            )
    return None
