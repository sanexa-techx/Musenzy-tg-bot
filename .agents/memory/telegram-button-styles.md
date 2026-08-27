---
name: Telegram button styles
description: How Bot API 9.4 button colors coexist with the Pyrofork MTProto client.
---

Telegram Bot API 9.4 button colors must be sent as JSON through the HTTP Bot API
when the installed Pyrofork version does not expose the `style` field on its
MTProto keyboard model. Keep callback handling in Pyrofork; use a small
message reference adapter for edits and deletes.

**Why:** Passing `style` to Pyrofork's `InlineKeyboardButton` raises a
constructor error, while sending the JSON payload directly preserves the
official `primary`, `success`, and `danger` styles.

**How to apply:** Prefer the HTTP transport for styled now-playing keyboards
and retain a Pyrofork fallback so playback remains usable if Telegram rejects
the newer field or the HTTP request is temporarily unavailable.