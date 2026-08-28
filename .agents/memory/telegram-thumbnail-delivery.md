---
name: Telegram thumbnail delivery
description: Reliable delivery of YouTube artwork in Telegram music messages.
---

YouTube artwork should be downloaded locally before sending a now-playing card whenever possible, with quality fallbacks such as `hqdefault.jpg`; upload the local bytes through the Bot API instead of relying only on Telegram fetching a remote URL.

**Why:** Telegram intermittently fails to fetch some YouTube thumbnail URLs, particularly unavailable high-resolution variants, even though the same image is reachable from the bot server.

**How to apply:** Keep thumbnail preparation optional and non-blocking for playback. Cache by video ID, try alternate YouTube thumbnail qualities, and retain a text/native-client fallback if image delivery still fails.