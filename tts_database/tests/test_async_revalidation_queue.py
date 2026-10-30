
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repositories.async_revalidation_queue import (
    get_queue_record,
    get_queue_records,
    submit_for_revalidation,
    update_duplicate_status,
    update_queue_status,
)


def _async_cm(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(fetchone_return=None, fetchall_return=None):
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=fetchone_return)
    mock_cur.fetchall = AsyncMock(return_value=fetchall_return or [])

    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value = _async_cm(mock_cur)
    return mock_conn, mock_cur


SAMPLE = {"source": "youtube", "dataset_name": "ds", "status": "pending"}


@pytest.mark.asyncio
@patch("repositories.async_revalidation_queue.get_async_connection")
async def test_submit_for_revalidation_returns_id(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return={"id": 7})
    mock_get_conn.return_value = mock_conn

    result = await submit_for_revalidation(SAMPLE)

    assert result == 7


@pytest.mark.asyncio
@patch("repositories.async_revalidation_queue.get_async_connection")
async def test_get_queue_record_returns_none(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    assert await get_queue_record(99999) is None


@pytest.mark.asyncio
async def test_get_queue_records_rejects_invalid_status():
    with pytest.raises(ValueError, match="Invalid queue status"):
        await get_queue_records(queue_status="bad_status")


@pytest.mark.asyncio
async def test_update_queue_status_rejects_approved():
    with pytest.raises(ValueError, match="approve_queue_record"):
        await update_queue_status(1, "approved")


@pytest.mark.asyncio
async def test_update_queue_status_rejects_invalid_status():
    with pytest.raises(ValueError, match="Invalid queue status"):
        await update_queue_status(1, "nonexistent")


@pytest.mark.asyncio
async def test_update_duplicate_status_rejects_invalid():
    with pytest.raises(ValueError, match="Invalid duplicate status"):
        await update_duplicate_status(1, "bad_status")


@pytest.mark.asyncio
@patch("repositories.async_revalidation_queue.get_async_connection")
async def test_update_duplicate_status_returns_false_when_missing(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    result = await update_duplicate_status(99999, "unique")

    assert result is False
