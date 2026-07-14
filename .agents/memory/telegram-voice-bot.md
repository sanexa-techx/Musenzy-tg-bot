---
name: Telegram voice-chat music bots (py-tgcalls)
description: Dependency and process gotchas when building a Telegram bot that joins group voice chats and streams audio via py-tgcalls/pytgcalls.
---

- Telegram's Bot API cannot join or stream into a group voice chat. Only a real user account (MTProto client) can. Architecture needs two Telegram clients: a bot (BotFather token) for commands/buttons, and an "assistant" user account (session string) that actually joins/streams.
- `py-tgcalls`'s pyrogram backend imports error classes (e.g. `GroupcallForbidden`) that do not exist in mainline PyPI `pyrogram`. Install `pyrofork` instead — it's a maintained fork that installs into the same `pyrogram` import namespace (drop-in, no import changes) and has the classes py-tgcalls expects. Do not pip-install `pyrogram` and `pyrofork` together; uninstall one before installing the other since both occupy the `pyrogram` module path.
- `py-tgcalls`'s client-type detection (`BridgedClient.package_name`) only recognizes modules literally named `pyrogram` or `telethon` — `hydrogram` is not detected and raises `InvalidMTProtoClient` even though its API is pyrogram-compatible.
- **Why:** discovered by trial — hydrogram failed silently on backend detection; pyrogram (mainline) failed on missing error class at PyTgCalls init.

## Generating a session string interactively

- Do not use `nohup ... & disown` from ShellExec to keep a login flow alive across turns — the sandbox kills backgrounded child processes once the shell command that spawned them returns, even with `setsid`.
- Instead, run the interactive login step (connect → send_code → wait for code → sign_in → export_session_string) as a **workflow** (`configureWorkflow`), since workflows are the platform's actual persistent-process primitive and survive across tool calls. Poll for user-provided input via a small file the workflow process reads, written by a later ShellExec call.
- Repeatedly reconnecting a **new** process/session per login step (send in one process, sign-in in another) reliably produced `PHONE_CODE_EXPIRED` immediately, even when the code was used within seconds — keeping one long-lived connected client across the whole login flow fixed it.
- Remove the temporary login workflow once the session string is obtained; delete any local session/state files that contain it before finishing.
