from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from connection import get_connection


def create_audit_log(
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    performed_by: str | None = None,
    details: dict | list | None = None,
) -> int:
    """
    Record an audit event and return its ID.

    Examples of actions:
    record_submitted, duplicate_check_completed,
    record_approved, record_rejected.
    """
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string.")

    if not isinstance(entity_type, str) or not entity_type.strip():
        raise ValueError("entity_type must be a non-empty string.")

    if entity_id is not None:
        if isinstance(entity_id, bool) or not isinstance(entity_id, int):
            raise ValueError("entity_id must be an integer or None.")

    if performed_by is not None and not isinstance(performed_by, str):
        raise ValueError("performed_by must be a string or None.")

    if details is not None and not isinstance(details, (dict, list)):
        raise ValueError("details must be a dictionary, list, or None.")

    details_json = Jsonb(details) if details is not None else None

    sql = """
        INSERT INTO audit_logs (
            action,
            entity_type,
            entity_id,
            performed_by,
            details
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    action.strip(),
                    entity_type.strip(),
                    entity_id,
                    performed_by,
                    details_json,
                ),
            )
            return cur.fetchone()[0]


def get_audit_log(log_id: int) -> dict | None:
    """Return one audit log by ID, or None if it doesn't exist."""
    sql = """
        SELECT *
        FROM audit_logs
        WHERE id = %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (log_id,))
            return cur.fetchone()


def get_audit_logs(
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    performed_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """
    Retrieve audit logs with optional filters.

    Results are ordered newest first.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")

    if offset < 0:
        raise ValueError("offset cannot be negative.")

    if entity_id is not None:
        if isinstance(entity_id, bool) or not isinstance(entity_id, int):
            raise ValueError("entity_id must be an integer or None.")

    conditions = []
    params = []

    if entity_type is not None:
        conditions.append("entity_type = %s")
        params.append(entity_type)

    if entity_id is not None:
        conditions.append("entity_id = %s")
        params.append(entity_id)

    if action is not None:
        conditions.append("action = %s")
        params.append(action)

    if performed_by is not None:
        conditions.append("performed_by = %s")
        params.append(performed_by)

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    sql = f"""
        SELECT *
        FROM audit_logs
        {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s;
    """

    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
