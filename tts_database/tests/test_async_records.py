
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repositories.async_records import (
    create_record,
    delete_record,
    get_record_by_id,
    get_records,
    update_record,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _async_cm(value):
    """Return an object usable as 'async with' that yields value."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(fetchone_return=None, fetchall_return=None):
    """Build a minimal async connection mock.

    psycopg cursor() is synchronous; conn itself is an async context manager.
    """
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock(return_value=fetchone_return)
    mock_cur.fetchall = AsyncMock(return_value=fetchall_return or [])

    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    # cursor() is sync → returns an async context manager
    mock_conn.cursor.return_value = _async_cm(mock_cur)

    return mock_conn, mock_cur


SAMPLE = {
    "source": "youtube",
    "dataset_name": "test-dataset",
    "status": "pending",
}


# ---------------------------------------------------------------------------
# create_record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_create_record_returns_id(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return={"id": 42})
    mock_get_conn.return_value = mock_conn

    result = await create_record(SAMPLE)

    assert result == 42


@pytest.mark.asyncio
async def test_create_record_rejects_invalid_fields():
    with pytest.raises(ValueError, match="Unsupported metadata fields"):
        await create_record({"bad_field": "x"})


# ---------------------------------------------------------------------------
# get_record_by_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_get_record_by_id_returns_dict(mock_get_conn):
    expected = {"id": 1, "source": "youtube"}
    mock_conn, _ = _mock_conn(fetchone_return=expected)
    mock_get_conn.return_value = mock_conn

    result = await get_record_by_id(1)

    assert result == expected


@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_get_record_by_id_returns_none(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    result = await get_record_by_id(99999)

    assert result is None


# ---------------------------------------------------------------------------
# get_records
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_records_rejects_bad_limit():
    with pytest.raises(ValueError, match="limit"):
        await get_records(limit=0)


@pytest.mark.asyncio
async def test_get_records_rejects_negative_offset():
    with pytest.raises(ValueError, match="offset"):
        await get_records(offset=-1)


@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_get_records_returns_list(mock_get_conn):
    rows = [{"id": 1}, {"id": 2}]
    mock_conn, _ = _mock_conn(fetchall_return=rows)
    mock_get_conn.return_value = mock_conn

    result = await get_records(limit=10, offset=0)

    assert result == rows


# ---------------------------------------------------------------------------
# update_record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_update_record_returns_true(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return={"id": 1})
    mock_get_conn.return_value = mock_conn

    result = await update_record(1, {"status": "approved"})

    assert result is True


@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_update_record_returns_false_when_missing(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    result = await update_record(99999, {"status": "approved"})

    assert result is False


# ---------------------------------------------------------------------------
# delete_record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_delete_record_returns_true(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return={"id": 1})
    mock_get_conn.return_value = mock_conn

    result = await delete_record(1)

    assert result is True


@pytest.mark.asyncio
@patch("repositories.async_records.get_async_connection")
async def test_delete_record_returns_false_when_missing(mock_get_conn):
    mock_conn, _ = _mock_conn(fetchone_return=None)
    mock_get_conn.return_value = mock_conn

    result = await delete_record(99999)

    assert result is False
