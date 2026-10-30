
from psycopg.rows import dict_row

try:
    from tts_database.connection import get_connection
except ImportError:
    from connection import get_connection


METADATA_FIELDS = (
    "research_date",
    "completion_date",
    "source",
    "audio_link",
    "text_link",
    "description",
    "dataset_name",
    "gender",
    "before_processing_duration",
    "after_processing_duration",
    "total_files",
    "researched_by",
    "assigned_to",
    "status",
    "data_type_augmentation_status",
    "data_available",
    "speakers_id",
    "speaker_channel_name",
    "accent",
    "licensing",
    "handover",
    "remark",
)

METADATA_FIELD_SET = frozenset(METADATA_FIELDS)


def _validate_record_fields(data: dict) -> None:
    """Reject fields that are not part of the 22 metadata fields."""
    if not isinstance(data, dict) or not data:
        raise ValueError("A non-empty dictionary is required.")

    invalid_fields = set(data) - METADATA_FIELD_SET
    if invalid_fields:
        names = ", ".join(sorted(invalid_fields))
        raise ValueError(f"Unsupported metadata fields: {names}")


def create_record(record: dict) -> int:
    """Create a permanent research record and return its ID."""
    _validate_record_fields(record)

    columns = list(record)
    column_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    values = [record[column] for column in columns]

    sql = f"""
        INSERT INTO indian_english_records ({column_sql})
        VALUES ({placeholders})
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.fetchone()[0]


def get_record_by_id(record_id: int):
    """Return one record as a dictionary, or None if not found."""
    sql = """
        SELECT *
        FROM indian_english_records
        WHERE id = %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (record_id,))
            return cur.fetchone()


def get_records(limit: int = 100, offset: int = 0):
    """Return a page of records as dictionaries, newest IDs first."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")

    if offset < 0:
        raise ValueError("offset cannot be negative.")

    sql = """
        SELECT *
        FROM indian_english_records
        ORDER BY id DESC
        LIMIT %s OFFSET %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (limit, offset))
            return cur.fetchall()


def update_record(record_id: int, updates: dict) -> bool:
    """Update supplied metadata fields and return whether a row existed."""
    _validate_record_fields(updates)

    assignments = ", ".join(
        f"{column} = %s" for column in updates
    )
    values = list(updates.values()) + [record_id]

    sql = f"""
        UPDATE indian_english_records
        SET {assignments},
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.fetchone() is not None


def delete_record(record_id: int) -> bool:
    """Delete a permanent record by ID."""
    sql = """
        DELETE FROM indian_english_records
        WHERE id = %s
        RETURNING id;
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (record_id,))
            return cur.fetchone() is not None
