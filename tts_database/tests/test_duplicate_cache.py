from datetime import datetime, timedelta, timezone
import uuid

import pytest

from connection import get_connection
from repositories.duplicate_cache import (
    save_cache_entry,
    get_cache_entry,
    delete_expired_cache_entries,
)


def unique_cache_key():
    return f"PYTEST_CACHE_{uuid.uuid4().hex}"


def cleanup_cache_entry(cache_key):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM duplicate_cache WHERE cache_key = %s",
                (cache_key,),
            )


def test_save_and_retrieve_cache_entry():
    cache_key = unique_cache_key()

    try:
        cache_id = save_cache_entry(
            cache_key=cache_key,
            duplicate_status="unique",
            rules_version="v1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            content_hash="test_hash_123",
            duplicate_evidence={"matches": [], "reason": "No matches"},
        )

        entry = get_cache_entry(cache_key)

        assert cache_id is not None
        assert entry is not None
        assert entry["cache_key"] == cache_key
        assert entry["duplicate_status"] == "unique"
        assert entry["content_hash"] == "test_hash_123"
        assert entry["rules_version"] == "v1"
        assert entry["duplicate_evidence"]["reason"] == "No matches"

    finally:
        cleanup_cache_entry(cache_key)


def test_refresh_existing_cache_entry():
    cache_key = unique_cache_key()
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)

    try:
        first_id = save_cache_entry(
            cache_key=cache_key,
            duplicate_status="review_required",
            rules_version="v1",
            expires_at=expiry,
        )

        second_id = save_cache_entry(
            cache_key=cache_key,
            duplicate_status="unique",
            rules_version="v2",
            expires_at=expiry,
            content_hash="updated_hash",
            duplicate_evidence={"reason": "Rechecked"},
        )

        entry = get_cache_entry(cache_key)

        assert first_id == second_id
        assert entry is not None
        assert entry["duplicate_status"] == "unique"
        assert entry["rules_version"] == "v2"
        assert entry["content_hash"] == "updated_hash"
        assert entry["duplicate_evidence"]["reason"] == "Rechecked"

    finally:
        cleanup_cache_entry(cache_key)


def test_expired_cache_entry_is_not_returned():
    cache_key = unique_cache_key()

    try:
        save_cache_entry(
            cache_key=cache_key,
            duplicate_status="unique",
            rules_version="v1",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        assert get_cache_entry(cache_key) is None

    finally:
        cleanup_cache_entry(cache_key)


def test_missing_cache_entry_returns_none():
    assert get_cache_entry(unique_cache_key()) is None


def test_invalid_duplicate_status_is_rejected():
    with pytest.raises(ValueError, match="Invalid duplicate status"):
        save_cache_entry(
            cache_key=unique_cache_key(),
            duplicate_status="unknown_status",
            rules_version="v1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_timezone_naive_expiry_is_rejected():
    with pytest.raises(ValueError, match="must include a timezone"):
        save_cache_entry(
            cache_key=unique_cache_key(),
            duplicate_status="unique",
            rules_version="v1",
            expires_at=datetime.now() + timedelta(hours=1),
        )


def test_cleanup_deletes_expired_cache_only():
    cache_key = unique_cache_key()

    try:
        save_cache_entry(
            cache_key=cache_key,
            duplicate_status="unique",
            rules_version="v1",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        deleted_count = delete_expired_cache_entries()

        assert deleted_count >= 1
        assert get_cache_entry(cache_key) is None

    finally:
        cleanup_cache_entry(cache_key)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
