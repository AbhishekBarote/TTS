
from async_connection import get_async_connection
from repositories.records import METADATA_FIELDS, _validate_record_fields


async def create_record(record: dict) -> int:
    """Create a permanent research record and return its ID."""
    _validate_record_fields(record)

    columns = list(record)
    column_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    values = [record[col] for col in columns]

    sql = f"""
        INSERT INTO research_records ({column_sql})
        VALUES ({placeholders})
        RETURNING id;
    """

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, values)
            row = await cur.fetchone()
            return row["id"]


async def get_record_by_id(record_id: int) -> dict | None:
    """Return one record as a dictionary, or None if not found."""
    sql = "SELECT * FROM research_records WHERE id = %s;"

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (record_id,))
            return await cur.fetchone()


async def get_records(limit: int = 100, offset: int = 0) -> list[dict]:
    """Return a page of records as dictionaries, newest IDs first."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    if offset < 0:
        raise ValueError("offset cannot be negative.")

    sql = """
        SELECT * FROM research_records
        ORDER BY id DESC
        LIMIT %s OFFSET %s;
    """

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (limit, offset))
            return await cur.fetchall()


async def update_record(record_id: int, updates: dict) -> bool:
    """Update supplied metadata fields; return True when a row was found."""
    _validate_record_fields(updates)

    assignments = ", ".join(f"{col} = %s" for col in updates)
    values = list(updates.values()) + [record_id]

    sql = f"""
        UPDATE research_records
        SET {assignments}, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        RETURNING id;
    """

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, values)
            return (await cur.fetchone()) is not None


async def delete_record(record_id: int) -> bool:
    """Delete a permanent record by ID."""
    sql = "DELETE FROM research_records WHERE id = %s RETURNING id;"

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (record_id,))
            return (await cur.fetchone()) is not None
