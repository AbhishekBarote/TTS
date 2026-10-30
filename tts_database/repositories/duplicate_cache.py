import json
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from psycopg.rows import dict_row

from connection import get_connection


ALLOWED_DUPLICATE_STATUSES = {
    "not_checked",
    "unique",
    "duplicate",
    "review_required",
    "check_failed",
}


def _validate_cache_entry(
    cache_key: str,
    duplicate_status: str,
    rules_version: str,
    expires_at: datetime,
) -> None:
    """Validate required cache-entry fields."""
    if not isinstance(cache_key, str) or not cache_key.strip():
        raise ValueError("cache_key must be a non-empty string.")

    if duplicate_status not in ALLOWED_DUPLICATE_STATUSES:
        raise ValueError(
            f"Invalid duplicate status: {duplicate_status}"
        )

    if not isinstance(rules_version, str) or not rules_version.strip():
        raise ValueError("rules_version must be a non-empty string.")

    if not isinstance(expires_at, datetime):
        raise ValueError("expires_at must be a datetime.")

    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must include a timezone.")


def save_cache_entry(
    cache_key: str,
    duplicate_status: str,
    rules_version: str,
    expires_at: datetime,
    content_hash: str | None = None,
    duplicate_evidence: dict | list | None = None,
) -> int:
    """
    Insert or refresh a cached duplicate-check result.

    Returns the cache-entry ID.
    """
    _validate_cache_entry(
        cache_key,
        duplicate_status,
        rules_version,
        expires_at,
    )

    evidence_json = (
        Jsonb(duplicate_evidence)
        if duplicate_evidence is not None
        else None
    )

    sql = """
        INSERT INTO duplicate_cache (
            cache_key,
            content_hash,
            duplicate_status,
            duplicate_evidence,
            rules_version,
            expires_at
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
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
            return cur.fetchone()[0]


def get_cache_entry(cache_key: str) -> dict | None:
    """
    Return a valid, non-expired cache entry.

    Return None when the entry does not exist or has expired.
    Expired entries are not deleted by this function.
    """
    if not isinstance(cache_key, str) or not cache_key.strip():
        raise ValueError("cache_key must be a non-empty string.")

    sql = """
        SELECT
            id,
            cache_key,
            content_hash,
            duplicate_status,
            duplicate_evidence,
            rules_version,
            expires_at,
            created_at,
            updated_at
        FROM duplicate_cache
        WHERE cache_key = %s
          AND expires_at > CURRENT_TIMESTAMP;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (cache_key.strip(),))
            return cur.fetchone()


def delete_expired_cache_entries() -> int:
    """
    Delete expired cache entries only.

    This function does not delete queue or permanent records.
    Returns the number of deleted cache entries.
    """
    sql = """
        DELETE FROM duplicate_cache
        WHERE expires_at <= CURRENT_TIMESTAMP
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return len(cur.fetchall())
