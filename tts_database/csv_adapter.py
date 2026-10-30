
import csv
from datetime import date, datetime
from pathlib import Path
import re

from psycopg.rows import dict_row

try:
    from tts_database.connection import get_connection
except ImportError:
    from connection import get_connection
try:
    from tts_database.repositories.records import METADATA_FIELDS
except ImportError:
    from repositories.records import METADATA_FIELDS


# Exact original CSV headers, in the required order.
CSV_HEADERS = (
    "Date",
    "Completion_date",
    "Source",
    "Audio_Link",
    "Text_Link",
    "Description",
    "Dataset Name",
    "Gender",
    "Before_Processing_Duration",
    "After_Processing_Duration",
    "Total_files",
    "Researched_By",
    "Assigned_to",
    "Status",
    "Data_type (Augmentation_Status)",
    "Data_available",
    "Speakers_ID",
    "Speaker/Channel_Name",
    "Accent",
    "Licensing",
    "Handover",
    "Remark",
)

CSV_TO_DB = dict(
    zip(CSV_HEADERS, METADATA_FIELDS)
)

DATE_FIELDS = {
    "research_date",
    "completion_date",
}

DURATION_FIELDS = {
    "before_processing_duration",
    "after_processing_duration",
}

INTEGER_FIELDS = {
    "total_files",
}


def duration_to_seconds(value: str) -> int | None:
    """Convert strict H:MM:SS to seconds. Blank values become None."""
    if value is None or not value.strip():
        return None

    value = value.strip()

    # Hours: one or more digits; minutes and seconds: exactly two digits.
    if not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", value):
        raise ValueError(
            f"Invalid duration {value!r}; expected H:MM:SS "
            "with two-digit minutes and seconds."
        )

    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds

def seconds_to_duration(value: int | None) -> str:
    """Convert integer seconds to H:MM:SS."""
    if value is None:
        return ""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Duration seconds must be a non-negative integer.")

    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _parse_value(field: str, value: str | None):
    """Convert one CSV field into a database-compatible value."""
    if value is None or not value.strip():
        return None

    value = value.strip()

    if field in DATE_FIELDS:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"Invalid date {value!r} for field {field}; "
                "expected YYYY-MM-DD."
            ) from exc

    if field in DURATION_FIELDS:
        return duration_to_seconds(value)

    if field in INTEGER_FIELDS:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(
                f"Invalid integer {value!r} for field {field}."
            ) from exc

        if parsed < 0:
            raise ValueError(f"{field} cannot be negative.")

        return parsed

    return value


def _format_value(field: str, value):
    """Convert a database value into its CSV representation."""
    if value is None:
        return ""

    if field in DURATION_FIELDS:
        return seconds_to_duration(value)

    if field in DATE_FIELDS:
        if isinstance(value, (date, datetime)):
            return value.strftime("%Y-%m-%d")
        return str(value)

    return str(value)


def _read_csv(path: str | Path) -> list[dict]:
    """Read and validate a CSV file before any database changes."""
    path = Path(path)

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames is None:
            raise ValueError("CSV file is empty or has no header row.")

        if tuple(reader.fieldnames) != CSV_HEADERS:
            raise ValueError(
                "CSV headers do not match the expected 22 headers "
                "in the required order.\n"
                f"Expected: {list(CSV_HEADERS)}\n"
                f"Received: {reader.fieldnames}"
            )

        records = []

        for line_number, row in enumerate(reader, start=2):
            # Reject rows with extra or missing CSV cells.
            if None in row or any(value is None for value in row.values()):
                raise ValueError(
                    f"Malformed CSV row at line {line_number}."
                )

            try:
                record = {
                    CSV_TO_DB[header]: _parse_value(
                        CSV_TO_DB[header], row[header]
                    )
                    for header in CSV_HEADERS
                }
            except ValueError as exc:
                raise ValueError(
                    f"CSV line {line_number}: {exc}"
                ) from exc

            records.append(record)

    return records


def import_csv_to_records(path: str | Path) -> int:
    """
    Import CSV rows directly into indian_english_records.

    Validates the complete file before writing. Inserts all rows in
    one transaction, so a database failure rolls back the whole batch.

    Returns the number of inserted records.
    """
    records = _read_csv(path)

    if not records:
        return 0

    columns_sql = ", ".join(METADATA_FIELDS)
    placeholders = ", ".join(["%s"] * len(METADATA_FIELDS))

    sql = f"""
        INSERT INTO indian_english_records ({columns_sql})
        VALUES ({placeholders});
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            for record in records:
                cur.execute(
                    sql,
                    [record[field] for field in METADATA_FIELDS],
                )

    return len(records)


def export_records_to_csv(
    path: str | Path,
    limit: int = 1000,
    offset: int = 0,
) -> int:
    """
    Export permanent research records to CSV using the original
    headers and duration format.

    Returns the number of exported records.
    """
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000.")

    if offset < 0:
        raise ValueError("offset cannot be negative.")

    columns_sql = ", ".join(METADATA_FIELDS)

    sql = f"""
        SELECT {columns_sql}
        FROM indian_english_records
        ORDER BY id
        LIMIT %s OFFSET %s;
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (limit, offset))
            records = cur.fetchall()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADERS)
        writer.writeheader()

        for record in records:
            writer.writerow({
                header: _format_value(
                    CSV_TO_DB[header],
                    record[CSV_TO_DB[header]],
                )
                for header in CSV_HEADERS
            })

    return len(records)
