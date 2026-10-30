from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


from connection import get_connection
from repositories.records import (
    METADATA_FIELDS,
    _validate_record_fields,
)


ALLOWED_QUEUE_STATUSES = {
    "pending",
    "ready_for_approval",
    "approved",
    "rejected",
}

ALLOWED_TRANSITIONS = {
    "pending": {"ready_for_approval", "rejected"},
    "ready_for_approval": {"rejected"},
    "approved": set(),
    "rejected": set(),
}

ALLOWED_DUPLICATE_STATUSES = {
    "not_checked",
    "unique",
    "duplicate",
    "review_required",
    "check_failed",
}


def submit_for_revalidation(record: dict) -> int:
    """Insert metadata into the queue and return its ID."""
    _validate_record_fields(record)

    columns = list(record)
    columns_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    values = [record[column] for column in columns]

    sql = f"""
        INSERT INTO revalidation_queue ({columns_sql})
        VALUES ({placeholders})
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.fetchone()[0]


def get_queue_record(queue_id: int):
    """Return a queue record as a dictionary, or None."""
    sql = """
        SELECT *
        FROM revalidation_queue
        WHERE id = %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (queue_id,))
            return cur.fetchone()


def get_queue_records(
    queue_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """List queue records, optionally filtered by status."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")

    if offset < 0:
        raise ValueError("offset cannot be negative.")

    if queue_status is not None:
        if queue_status not in ALLOWED_QUEUE_STATUSES:
            raise ValueError(f"Invalid queue status: {queue_status}")

        sql = """
            SELECT *
            FROM revalidation_queue
            WHERE queue_status = %s
            ORDER BY id DESC
            LIMIT %s OFFSET %s;
        """
        params = (queue_status, limit, offset)
    else:
        sql = """
            SELECT *
            FROM revalidation_queue
            ORDER BY id DESC
            LIMIT %s OFFSET %s;
        """
        params = (limit, offset)

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def update_queue_status(queue_id: int, new_status: str) -> bool:
    """
    Apply an allowed queue status transition.

    Approval must use approve_queue_record() so the permanent record,
    queue status, and audit event are committed together.
    """
    if new_status not in ALLOWED_QUEUE_STATUSES:
        raise ValueError(f"Invalid queue status: {new_status}")

    if new_status == "approved":
        raise ValueError(
            "Use approve_queue_record() to approve and insert "
            "the permanent record."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT queue_status
                FROM revalidation_queue
                WHERE id = %s
                FOR UPDATE;
                """,
                (queue_id,),
            )
            row = cur.fetchone()

            if row is None:
                return False

            current_status = row[0]

            if new_status not in ALLOWED_TRANSITIONS[current_status]:
                raise ValueError(
                    f"Transition not allowed: "
                    f"{current_status} -> {new_status}"
                )

            cur.execute(
                """
                UPDATE revalidation_queue
                SET queue_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
                """,
                (new_status, queue_id),
            )

            return True


def update_duplicate_status(
    queue_id: int,
    new_status: str,
) -> bool:
    """Update the result of duplicate checking."""
    if new_status not in ALLOWED_DUPLICATE_STATUSES:
        raise ValueError(
            f"Invalid duplicate status: {new_status}"
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE revalidation_queue
                SET duplicate_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING id;
                """,
                (new_status, queue_id),
            )
            return cur.fetchone() is not None


def approve_queue_record(queue_id: int) -> int:
    """
    Atomically approve a queue record.

    In one transaction:
    1. Validate and lock the queue entry.
    2. Insert the permanent research record.
    3. Mark the queue entry approved and link the permanent ID.
    4. Create an audit log entry.

    Only records with queue_status='ready_for_approval' and
    duplicate_status='unique' can be approved.

    Returns the permanent research_records ID.
    Raises ValueError if approval is not allowed.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Lock the queue row to prevent concurrent approvals.
            cur.execute(
                """
                SELECT
                    queue_status,
                    duplicate_status,
                    approved_record_id,
                    source,
                    speakers_id
                FROM revalidation_queue
                WHERE id = %s
                FOR UPDATE;
                """,
                (queue_id,),
            )
            queue_row = cur.fetchone()

            if queue_row is None:
                raise ValueError(
                    f"Queue record {queue_id} does not exist."
                )

            (
                queue_status,
                duplicate_status,
                approved_record_id,
                source,
                speakers_id,
            ) = queue_row

            if approved_record_id is not None:
                raise ValueError(
                    "This queue record already references a permanent record."
                )

            if queue_status != "ready_for_approval":
                raise ValueError(
                    "Record must be ready_for_approval before approval."
                )

            if duplicate_status != "unique":
                raise ValueError(
                    "Only records confirmed as unique can be approved. "
                    f"Current duplicate status: {duplicate_status}"
                )

            # Retrieve the 22 metadata fields in their defined order.
            columns_sql = ", ".join(METADATA_FIELDS)

            cur.execute(
                f"""
                SELECT {columns_sql}
                FROM revalidation_queue
                WHERE id = %s;
                """,
                (queue_id,),
            )
            metadata_values = cur.fetchone()

            placeholders = ", ".join(
                ["%s"] * len(METADATA_FIELDS)
            )

            # Insert the permanent record.
            cur.execute(
                f"""
                INSERT INTO research_records ({columns_sql})
                VALUES ({placeholders})
                RETURNING id;
                """,
                metadata_values,
            )
            permanent_id = cur.fetchone()[0]

            # Mark the queue entry approved and link the permanent ID.
            cur.execute(
                """
                UPDATE revalidation_queue
                SET queue_status = 'approved',
                    approved_record_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
                """,
                (permanent_id, queue_id),
            )

            # Log the approval in the same transaction.
            cur.execute(
                """
                INSERT INTO audit_logs (
                    action,
                    entity_type,
                    entity_id,
                    performed_by,
                    details
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    "record_approved",
                    "research_record",
                    permanent_id,
                    None,
                    Jsonb({
                        "queue_id": queue_id,
                        "source": source,
                        "speakers_id": speakers_id,
                        "duplicate_status": duplicate_status,
                    }),
                ),
            )

            return permanent_id
