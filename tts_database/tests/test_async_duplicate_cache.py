
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repositories.async_duplicate_cache import (
    delete_expired_cache_entries,
    get_cache_entry,
    save_cache_entry,
)


def _future_dt():
    return datetime.now(timezone.utc) + timedelta(hours=24)


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


@pytest.mark.asyncio
@patch("repositories.async_duplicate_cache.get_async_connection")
async def test_save_cache_entry_returns_id(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return={"id": 5})
    mock_get_conn.return_value = mock_conn

    result = await save_cache_entry(
        cache_key="key1",
        duplicate_status="unique",
        rules_version="v1",
        expires_at=_future_dt(),
    )

    assert result == 5


@pytest.mark.asyncio
async def test_save_cache_entry_rejects_empty_key():
    with pytest.raises(ValueError, match="cache_key"):
        await save_cache_entry(
            cache_key="  ",
            duplicate_status="unique",
            rules_version="v1",
            expires_at=_future_dt(),
        )


@pytest.mark.asyncio
async def test_get_cache_entry_rejects_empty_key():
    with pytest.raises(ValueError, match="cache_key"):
        await get_cache_entry("")


@pytest.mark.asyncio
@patch("repositories.async_duplicate_cache.get_async_connection")
async def test_get_cache_entry_returns_none(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    result = await get_cache_entry("missing-key")

    assert result is None


@pytest.mark.asyncio
@patch("repositories.async_duplicate_cache.get_async_connection")
async def test_delete_expired_cache_entries_returns_count(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchall_return=[{"id": 1}, {"id": 2}])
    mock_get_conn.return_value = mock_conn

    count = await delete_expired_cache_entries()

    assert count == 2
