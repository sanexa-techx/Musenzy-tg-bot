"""Small Bot API transport for features not yet exposed by Pyrofork.

Pyrofork uses Telegram's MTProto layer and its InlineKeyboardButton model does
not yet expose Bot API 9.4's button ``style`` field. The now-playing card is
sent through the HTTP Bot API so Telegram receives the style values unchanged.
Callback queries continue to be handled by the existing Pyrofork client.
"""
from __future__ import annotations

from typing import Any

import aiohttp


class BotApiError(RuntimeError):
    """Telegram Bot API returned an unsuccessful response."""


class BotApiMessage:
    """Message reference with the methods used by NowPlayingTracker."""

    def __init__(self, api: "BotApiClient", chat_id: int, message_id: int) -> None:
        self._api = api
        self.chat_id = chat_id
        self.id = message_id

    async def delete(self) -> None:
        await self._api.call(
            "deleteMessage",
            {"chat_id": self.chat_id, "message_id": self.id},
        )

    async def edit_reply_markup(self, reply_markup: dict[str, Any]) -> None:
        await self._api.call(
            "editMessageReplyMarkup",
            {
                "chat_id": self.chat_id,
                "message_id": self.id,
                "reply_markup": reply_markup,
            },
        )


class BotApiClient:
    """Minimal async JSON client for Telegram Bot API methods."""

    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        session = await self._get_session()
        async with session.post(f"{self._base_url}/{method}", json=payload) as response:
            data = await response.json(content_type=None)
        if not data.get("ok"):
            raise BotApiError(
                f"{method} failed: {data.get('description', 'unknown Telegram error')}"
            )
        return data.get("result")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> BotApiMessage:
        result = await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                **({"reply_markup": reply_markup} if reply_markup else {}),
            },
        )
        return BotApiMessage(self, chat_id, int(result["message_id"]))

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> BotApiMessage:
        result = await self.call(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
                **({"reply_markup": reply_markup} if reply_markup else {}),
            },
        )
        return BotApiMessage(self, chat_id, int(result["message_id"]))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()