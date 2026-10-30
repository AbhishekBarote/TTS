
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from repositories.duplicate_workflow import (
    get_cached_duplicate_result,
    record_duplicate_result,
)


@patch("repositories.duplicate_workflow.get_cache_entry")
def test_returns_cached_unique_result(mock_get_cache):
    mock_get_cache.return_value = {
        "duplicate_status": "unique",
        "duplicate_evidence": {"match": False},
    }

    result = get_cached_duplicate_result("test-key")

    assert result["duplicate_status"] == "unique"
    mock_get_cache.assert_called_once_with("test-key")


@patch("repositories.duplicate_workflow.get_cache_entry")
def test_returns_none_when_cache_misses(mock_get_cache):
    mock_get_cache.return_value = None

    assert get_cached_duplicate_result("missing-key") is None


@pytest.mark.parametrize("status", ["not_checked", "check_failed"])
@patch("repositories.duplicate_workflow.get_cache_entry")
def test_does_not_reuse_incomplete_or_failed_results(
    mock_get_cache, status
):
    mock_get_cache.return_value = {
        "duplicate_status": status,
    }

    assert get_cached_duplicate_result("test-key") is None


@pytest.mark.parametrize(
    "status",
    ["unique", "duplicate", "review_required", "check_failed"],
)
@patch("repositories.duplicate_workflow.save_cache_entry")
@patch("repositories.duplicate_workflow.update_duplicate_status")
@patch("repositories.duplicate_workflow.get_queue_record")
def test_records_duplicate_result(
    mock_get_queue,
    mock_update_status,
    mock_save_cache,
    status,
):
    mock_get_queue.return_value = {"id": 123}
    mock_update_status.return_value = True

    result = record_duplicate_result(
        queue_id=123,
        cache_key="test-key",
        duplicate_status=status,
        duplicate_evidence={"test": True},
    )

    assert result is True
    mock_update_status.assert_called_once_with(123, status)
    mock_save_cache.assert_called_once()

    saved_status = mock_save_cache.call_args.kwargs["duplicate_status"]
    assert saved_status == status


@patch("repositories.duplicate_workflow.save_cache_entry")
@patch("repositories.duplicate_workflow.update_duplicate_status")
@patch("repositories.duplicate_workflow.get_queue_record")
def test_missing_queue_record_is_not_cached(
    mock_get_queue,
    mock_update_status,
    mock_save_cache,
):
    mock_get_queue.return_value = None

    result = record_duplicate_result(
        queue_id=999999,
        cache_key="missing-queue-key",
        duplicate_status="unique",
    )

    assert result is False
    mock_update_status.assert_not_called()
    mock_save_cache.assert_not_called()


@pytest.mark.parametrize(
    "status",
    ["not_checked", "invalid", ""],
)
def test_rejects_invalid_result_status(status):
    with pytest.raises(ValueError, match="Invalid duplicate result"):
        record_duplicate_result(
            queue_id=123,
            cache_key="test-key",
            duplicate_status=status,
        )


@patch("repositories.duplicate_workflow.save_cache_entry")
@patch("repositories.duplicate_workflow.update_duplicate_status")
@patch("repositories.duplicate_workflow.get_queue_record")
def test_does_not_cache_when_queue_update_fails(
    mock_get_queue,
    mock_update_status,
    mock_save_cache,
):
    mock_get_queue.return_value = {"id": 123}
    mock_update_status.return_value = False

    result = record_duplicate_result(
        queue_id=123,
        cache_key="test-key",
        duplicate_status="unique",
    )

    assert result is False
    mock_save_cache.assert_not_called()


def test_rejects_nonpositive_cache_ttl():
    with pytest.raises(ValueError, match="cache_ttl_hours"):
        record_duplicate_result(
            queue_id=123,
            cache_key="test-key",
            duplicate_status="unique",
            cache_ttl_hours=0,
        )
