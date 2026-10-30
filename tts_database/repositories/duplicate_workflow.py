
from datetime import datetime, timedelta, timezone

from repositories.duplicate_cache import (
    get_cache_entry,
    save_cache_entry,
)
from repositories.revalidation_queue import (
    get_queue_record,
    update_duplicate_status,
)


ALLOWED_RESULTS = {
    "unique",
    "duplicate",
    "review_required",
    "check_failed",
}


def get_cached_duplicate_result(cache_key: str) -> dict | None:
    """Return a reusable, unexpired cached result, if available."""
    entry = get_cache_entry(cache_key)

    if entry is None:
        return None

    # Incomplete or failed checks must be retried, not reused as
    # evidence that a record is unique.
    if entry["duplicate_status"] in {"not_checked", "check_failed"}:
        return None

    return entry


def record_duplicate_result(
    queue_id: int,
    cache_key: str,
    duplicate_status: str,
    duplicate_evidence: dict | list | None = None,
    content_hash: str | None = None,
    rules_version: str = "v1",
    cache_ttl_hours: int = 24,
) -> bool:
    """
    Store a completed duplicate-check result in the queue and cache.

    A failed check is recorded as check_failed and is never interpreted
    as unique. The queue record must exist before the result is saved.
    """
    if duplicate_status not in ALLOWED_RESULTS:
        raise ValueError(
            f"Invalid duplicate result: {duplicate_status}"
        )

    if cache_ttl_hours <= 0:
        raise ValueError("cache_ttl_hours must be greater than zero.")

    queue_record = get_queue_record(queue_id)
    if queue_record is None:
        return False

    # Update the queue result first. If this fails, don't cache it.
    if not update_duplicate_status(queue_id, duplicate_status):
        return False

    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=cache_ttl_hours
    )

    save_cache_entry(
        cache_key=cache_key,
        duplicate_status=duplicate_status,
        rules_version=rules_version,
        expires_at=expires_at,
        content_hash=content_hash,
        duplicate_evidence=duplicate_evidence,
    )

    return True
