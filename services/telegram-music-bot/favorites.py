"""Database-backed per-user favorite tracks."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class FavoriteTrack:
    id: int
    user_id: int
    video_id: str
    url: str
    title: str
    duration: int
    thumbnail: str | None


class FavoritesStore:
    """Async PostgreSQL store for tracks saved by individual Telegram users."""

    def __init__(self) -> None:
        self._database_url = os.environ.get("DATABASE_URL", "")
        if not self._database_url:
            raise RuntimeError(
                "Missing DATABASE_URL. The favorites feature requires the project PostgreSQL database."
            )
        self._connection: psycopg.AsyncConnection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Verify the database and schema are available without running DDL."""
        async with self._lock:
            connection = await self._get_connection_locked()
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM favorite_tracks LIMIT 1")

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def toggle(
        self,
        user_id: int,
        *,
        video_id: str,
        url: str,
        title: str,
        duration: int,
        thumbnail: str | None,
    ) -> bool:
        """Toggle a favorite and return True when the track is now saved."""
        async with self._lock:
            connection = await self._get_connection_locked()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    DELETE FROM favorite_tracks
                    WHERE user_id = %s AND video_id = %s
                    RETURNING id
                    """,
                    (user_id, video_id),
                )
                removed = await cursor.fetchone()
                if removed is not None:
                    await connection.commit()
                    return False

                await cursor.execute(
                    """
                    INSERT INTO favorite_tracks
                        (user_id, video_id, url, title, duration, thumbnail)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, video_id) DO NOTHING
                    """,
                    (user_id, video_id, url, title, max(0, duration), thumbnail),
                )
                await connection.commit()
                return True

    async def list_for_user(self, user_id: int) -> list[FavoriteTrack]:
        async with self._lock:
            connection = await self._get_connection_locked()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT id, user_id, video_id, url, title, duration, thumbnail
                    FROM favorite_tracks
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (user_id,),
                )
                rows = await cursor.fetchall()
        return [FavoriteTrack(*row) for row in rows]

    async def get_for_user(self, user_id: int, favorite_id: int) -> FavoriteTrack | None:
        async with self._lock:
            connection = await self._get_connection_locked()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT id, user_id, video_id, url, title, duration, thumbnail
                    FROM favorite_tracks
                    WHERE user_id = %s AND id = %s
                    """,
                    (user_id, favorite_id),
                )
                row = await cursor.fetchone()
        return FavoriteTrack(*row) if row else None

    async def delete_for_user(self, user_id: int, favorite_id: int) -> bool:
        async with self._lock:
            connection = await self._get_connection_locked()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    DELETE FROM favorite_tracks
                    WHERE user_id = %s AND id = %s
                    RETURNING id
                    """,
                    (user_id, favorite_id),
                )
                deleted = await cursor.fetchone()
                await connection.commit()
        return deleted is not None

    async def _get_connection_locked(self) -> psycopg.AsyncConnection:
        if self._connection is None or self._connection.closed:
            self._connection = await psycopg.AsyncConnection.connect(
                self._database_url,
                autocommit=False,
            )
        return self._connection