"""YouTube search + audio extraction backed by yt-dlp."""
from __future__ import annotations

import asyncio
import os
import uuid

import yt_dlp

from config import DOWNLOAD_DIR, MAX_TRACK_SECONDS

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# When YouTube's bot-check blocks every anonymous client from this
# environment's IP (message: "Sign in to confirm you're not a bot"), the
# only reliable fix is authenticating with real browser cookies exported
# from a logged-in YouTube account (Netscape cookies.txt format). Drop that
# file at COOKIES_FILE and it's picked up automatically; without it we fall
# back to anonymous clients, which may get blocked.
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

_EXTRACTOR_ARGS = {
    # The default "web" client increasingly demands sign-in/PO-token
    # verification from cloud IPs. "tv_embedded" and "tv" avoid that
    # requirement for public videos; ios/android/mediaconnect are fallbacks.
    # If YouTube still blocks, drop a cookies.txt (see COOKIES_FILE) from a
    # logged-in browser account -- the code picks it up automatically.
    "youtube": {
        "player_client": ["tv_embedded", "tv", "ios", "android", "mediaconnect"],
        "player_skip": ["webpage", "configs"],
    }
}


def _base_opts() -> dict:
    opts = {"extractor_args": _EXTRACTOR_ARGS}
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
    and return track metadata including the local file path to stream."""
    loop = asyncio.get_running_loop()
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


def cleanup_file(file_path: str) -> None:
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass
