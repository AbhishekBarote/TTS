from psycopg.types.json import Jsonb

from async_connection import get_async_connection
from repositories.revalidation_queue import (
    ALLOWED_DUPLICATE_STATUSES,
    ALLOWED_QUEUE_STATUSES,
    ALLOWED_TRANSITIONS,
)
from repositories.records import METADATA_FIELDS


async def submit_for_revalidation(record: dict) -> int:
    """Insert metadata into the queue and return its ID."""
    from repositories.records import _validate_record_fields
    _validate_record_fields(record)

    columns = list(record)
    placeholders = ", ".join(["%s"] * len(columns))
    values = [record[col] for col in columns]

    sql = f"""
        INSERT INTO revalidation_queue ({", ".join(columns)})
        VALUES ({placeholders})
        RETURNING id;
    """
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, values)
            row = await cur.fetchone()
            return row["id"]


async def get_queue_record(queue_id: int) -> dict | None:
    """Return a queue record as a dict, or None."""
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM revalidation_queue WHERE id = %s;", (queue_id,))
            return await cur.fetchone()


async def get_queue_records(
    queue_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List queue records, optionally filtered by status."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    if offset < 0:
        raise ValueError("offset cannot be negative.")

    if queue_status is not None:
        if queue_status not in ALLOWED_QUEUE_STATUSES:
            raise ValueError(f"Invalid queue status: {queue_status}")
        sql = """
            SELECT * FROM revalidation_queue
            WHERE queue_status = %s
            ORDER BY id DESC LIMIT %s OFFSET %s;
        """
        params = (queue_status, limit, offset)
    else:
        sql = "SELECT * FROM revalidation_queue ORDER BY id DESC LIMIT %s OFFSET %s;"
        params = (limit, offset)

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def update_queue_status(queue_id: int, new_status: str) -> bool:
    """Apply an allowed queue status transition."""
    if new_status not in ALLOWED_QUEUE_STATUSES:
        raise ValueError(f"Invalid queue status: {new_status}")
    if new_status == "approved":
        raise ValueError("Use approve_queue_record() to approve and insert the permanent record.")

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT queue_status FROM revalidation_queue WHERE id = %s FOR UPDATE;",
                (queue_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return False

            current = row["queue_status"]
            if new_status not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"Transition not allowed: {current} -> {new_status}")

            await cur.execute(
                "UPDATE revalidation_queue SET queue_status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                (new_status, queue_id),
            )
            return True


async def update_duplicate_status(queue_id: int, new_status: str) -> bool:
    """Update the duplicate-check result for a queue record."""
    if new_status not in ALLOWED_DUPLICATE_STATUSES:
        raise ValueError(f"Invalid duplicate status: {new_status}")

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE revalidation_queue
                SET duplicate_status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s RETURNING id;
                """,
                (new_status, queue_id),
            )
            return (await cur.fetchone()) is not None


async def approve_queue_record(queue_id: int) -> int:
    """
    Atomically approve a queue record:
      1. Validates it is ready_for_approval and confirmed unique.
      2. Inserts a permanent row into research_records.
      3. Marks the queue entry approved and links the new record ID.
      4. Writes an audit log entry — all in one transaction.

    Returns the permanent research_records ID.
    """
    columns_sql = ", ".join(METADATA_FIELDS)
    placeholders = ", ".join(["%s"] * len(METADATA_FIELDS))

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            # Lock the row to prevent concurrent approvals
            await cur.execute(
                """
                SELECT queue_status, duplicate_status, approved_record_id, source, speakers_id
                FROM revalidation_queue
                WHERE id = %s
                FOR UPDATE;
                """,
                (queue_id,),
            )
            queue_row = await cur.fetchone()

            if queue_row is None:
                raise ValueError(f"Queue record {queue_id} does not exist.")
            if queue_row["approved_record_id"] is not None:
                raise ValueError("This queue record already references a permanent record.")
            if queue_row["queue_status"] != "ready_for_approval":
                raise ValueError("Record must be ready_for_approval before approval.")
            if queue_row["duplicate_status"] != "unique":
                raise ValueError(
                    f"Only records confirmed as unique can be approved. "
                    f"Current duplicate status: {queue_row['duplicate_status']}"
                )

            # Pull the 22 metadata fields
            await cur.execute(
                f"SELECT {columns_sql} FROM revalidation_queue WHERE id = %s;",
                (queue_id,),
            )
            metadata = await cur.fetchone()
            values = [metadata[col] for col in METADATA_FIELDS]

            # Insert permanent record
            await cur.execute(
                f"INSERT INTO research_records ({columns_sql}) VALUES ({placeholders}) RETURNING id;",
                values,
            )
            permanent_id = (await cur.fetchone())["id"]

            # Mark queue entry approved
            await cur.execute(
                """
                UPDATE revalidation_queue
                SET queue_status = 'approved', approved_record_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
                """,
                (permanent_id, queue_id),
            )

            # Audit log in the same transaction
            await cur.execute(
                """
                INSERT INTO audit_logs (action, entity_type, entity_id, performed_by, details)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    "record_approved",
                    "research_record",
                    permanent_id,
                    None,
                    Jsonb({
                        "queue_id": queue_id,
                        "source": queue_row["source"],
                        "speakers_id": queue_row["speakers_id"],
                        "duplicate_status": queue_row["duplicate_status"],
                    }),
                ),
            )

            return permanent_id
