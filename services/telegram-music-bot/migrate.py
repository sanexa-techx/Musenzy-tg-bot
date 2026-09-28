"""Idempotent database bootstrap for the Render container."""
from __future__ import annotations

import asyncio
import os

import psycopg


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS favorite_tracks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    video_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    duration INTEGER NOT NULL DEFAULT 0,
    thumbnail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS favorite_tracks_user_video_idx
    ON favorite_tracks (user_id, video_id);
"""


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError(
            "Missing DATABASE_URL. Create a Render PostgreSQL database and attach "
            "its internal connection string to this service."
        )

    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(SCHEMA_SQL)
        await connection.commit()
    print("Database schema is ready.")


if __name__ == "__main__":
    asyncio.run(main())