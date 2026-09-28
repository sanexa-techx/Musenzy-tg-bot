---
name: Telegram thumbnail delivery
description: Reliable delivery of YouTube artwork in Telegram music messages.
---

YouTube artwork should be downloaded locally before sending a now-playing card whenever possible, with quality fallbacks such as `hqdefault.jpg`; upload the local bytes through the Bot API instead of relying only on Telegram fetching a remote URL.

**Why:** Telegram intermittently fails to fetch some YouTube thumbnail URLs, particularly unavailable high-resolution variants, even though the same image is reachable from the bot server.

**How to apply:** Keep thumbnail preparation optional and non-blocking for playback. Cache by video ID, try alternate YouTube thumbnail qualities, retry photo delivery through the native client if the styled Bot API call fails, and only use a text fallback after photo attempts are exhausted.

Some YouTube CDN responses can be truncated WebP bytes saved under a `.jpg` name. Validate/decode cached bytes and normalize them to JPEG before uploading; discard malformed cache entries and try the alternate thumbnail host.

**Why:** Telegram photo uploads reject mislabeled, truncated, or unsupported image bytes even when the HTTP response reports success.

**How to apply:** Never trust the extension or HTTP 200 alone; validate the image payload and keep the now-playing text card as the final delivery fallback.