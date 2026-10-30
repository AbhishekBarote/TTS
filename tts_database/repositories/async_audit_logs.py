from psycopg.types.json import Jsonb

from async_connection import get_async_connection


async def create_audit_log(
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    performed_by: str | None = None,
    details: dict | list | None = None,
) -> int:
    """Record an audit event and return its ID."""
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string.")
    if not isinstance(entity_type, str) or not entity_type.strip():
        raise ValueError("entity_type must be a non-empty string.")
    if entity_id is not None and (isinstance(entity_id, bool) or not isinstance(entity_id, int)):
        raise ValueError("entity_id must be an integer or None.")
    if performed_by is not None and not isinstance(performed_by, str):
        raise ValueError("performed_by must be a string or None.")
    if details is not None and not isinstance(details, (dict, list)):
        raise ValueError("details must be a dict, list, or None.")

    sql = """
        INSERT INTO audit_logs (action, entity_type, entity_id, performed_by, details)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
    """
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (
                action.strip(),
                entity_type.strip(),
                entity_id,
                performed_by,
                Jsonb(details) if details is not None else None,
            ))
            row = await cur.fetchone()
            return row["id"]


async def get_audit_log(log_id: int) -> dict | None:
    """Return one audit log by ID, or None."""
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM audit_logs WHERE id = %s;", (log_id,))
            return await cur.fetchone()


async def get_audit_logs(
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    performed_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List audit logs with optional filters, newest first."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    if offset < 0:
        raise ValueError("offset cannot be negative.")
    if entity_id is not None and (isinstance(entity_id, bool) or not isinstance(entity_id, int)):
        raise ValueError("entity_id must be an integer or None.")

    conditions, params = [], []
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

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT * FROM audit_logs
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s;
    """
    params.extend([limit, offset])

    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()
