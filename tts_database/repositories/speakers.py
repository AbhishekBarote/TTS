from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from connection import get_connection
from repositories.audit_logs import create_audit_log


SPEAKER_CAP_SECONDS = 12_600  # 3.5 hours


def get_speaker_ledger(speakers_id: str) -> dict | None:
    """Return the ledger row for a speaker, or None if not found."""
    if not isinstance(speakers_id, str) or not speakers_id.strip():
        raise ValueError("speakers_id must be a non-empty string.")

    sql = """
        SELECT *
        FROM speaker_ledger
        WHERE speakers_id = %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (speakers_id.strip(),))
            return cur.fetchone()


def get_accepted_seconds(speakers_id: str) -> int:
    """Return accepted duration (seconds) for a speaker, 0 if unknown."""
    row = get_speaker_ledger(speakers_id)
    return row["accepted_seconds"] if row else 0


def would_exceed_cap(speakers_id: str, additional_seconds: int) -> bool:
    """Return True if adding additional_seconds would breach the 12600-s cap."""
    if additional_seconds < 0:
        raise ValueError("additional_seconds cannot be negative.")

    return get_accepted_seconds(speakers_id) + additional_seconds > SPEAKER_CAP_SECONDS


def _upsert_speaker_ledger(cur, speakers_id: str, delta_seconds: int) -> None:
    """
    Insert or increment the speaker ledger row inside an open transaction.

    Uses INSERT … ON CONFLICT so concurrent approvals serialise correctly
    via the FOR UPDATE lock on the queue row in approve_queue_record().
    """
    cur.execute(
        """
        INSERT INTO speaker_ledger (speakers_id, accepted_seconds)
        VALUES (%s, %s)
        ON CONFLICT (speakers_id)
        DO UPDATE SET
            accepted_seconds = speaker_ledger.accepted_seconds + EXCLUDED.accepted_seconds,
            updated_at = CURRENT_TIMESTAMP
        RETURNING accepted_seconds;
        """,
        (speakers_id.strip(), delta_seconds),
    )
    new_total = cur.fetchone()[0]
    if new_total > SPEAKER_CAP_SECONDS:
        raise ValueError(
            f"Approval would bring {speakers_id!r} to {new_total} s, "
            f"exceeding the {SPEAKER_CAP_SECONDS}-s cap."
        )
