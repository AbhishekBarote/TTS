
from datetime import datetime

from psycopg.types.json import Jsonb

from async_connection import get_async_connection
from repositories.duplicate_cache import (
    ALLOWED_DUPLICATE_STATUSES,
    _validate_cache_entry,
)


async def save_cache_entry(
    cache_key: str,
    duplicate_status: str,
    rules_version: str,
    expires_at: datetime,
    content_hash: str | None = None,
    duplicate_evidence: dict | list | None = None,
) -> int:
    """Insert or refresh a cached duplicate-check result. Returns cache-entry ID."""
    _validate_cache_entry(cache_key, duplicate_status, rules_version, expires_at)

    evidence_json = Jsonb(duplicate_evidence) if duplicate_evidence is not None else None

    sql = """
        INSERT INTO duplicate_cache (
            cache_key, content_hash, duplicate_status,
            duplicate_evidence, rules_version, expires_at
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (cache_key)
        DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            duplicate_status = EXCLUDED.duplicate_status,
            duplicate_evidence = EXCLUDED.duplicate_evidence,
            rules_version = EXCLUDED.rules_version,
            expires_at = EXCLUDED.expires_at,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id;
    """

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    cache_key.strip(),
                    content_hash,
                    duplicate_status,
                    evidence_json,
                    rules_version.strip(),
                    expires_at,
                ),
            )
            row = await cur.fetchone()
            return row["id"]


async def get_cache_entry(cache_key: str) -> dict | None:
    """Return a valid, non-expired cache entry, or None."""
    if not isinstance(cache_key, str) or not cache_key.strip():
        raise ValueError("cache_key must be a non-empty string.")

    sql = """
        SELECT
            id, cache_key, content_hash, duplicate_status,
            duplicate_evidence, rules_version, expires_at, created_at, updated_at
        FROM duplicate_cache
        WHERE cache_key = %s AND expires_at > CURRENT_TIMESTAMP;
    """

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (cache_key.strip(),))
            return await cur.fetchone()


async def delete_expired_cache_entries() -> int:
    """Delete expired cache entries. Returns the count of deleted rows."""
    sql = "DELETE FROM duplicate_cache WHERE expires_at <= CURRENT_TIMESTAMP RETURNING id;"

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return len(await cur.fetchall())
