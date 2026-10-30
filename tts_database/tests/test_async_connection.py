
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from async_connection import async_check_database, get_async_connection


def _async_cm(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@patch("async_connection.AsyncConnection.connect", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_get_async_connection_calls_connect(mock_connect):
    """get_async_connection() passes DATABASE_URL to AsyncConnection.connect."""
    mock_connect.return_value = MagicMock()

    await get_async_connection()

    mock_connect.assert_awaited_once()


@patch("async_connection.get_async_connection")
@pytest.mark.asyncio
async def test_async_check_database_returns_true(mock_get_conn):
    """async_check_database() returns True when SELECT 1 returns 1."""
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value={"ok": 1})

    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value = _async_cm(mock_cur)

    mock_get_conn.return_value = mock_conn

    result = await async_check_database()

    assert result is True
