
import csv
import uuid

import pytest

from connection import get_connection
from csv_adapter import (
    CSV_HEADERS,
    import_csv_to_records,
    export_records_to_csv,
)
from repositories.records import get_record_by_id, delete_record


def make_csv(path, audio_link):
    row = [""] * len(CSV_HEADERS)
    row[0] = "2026-10-26"
    row[2] = "Integration Test"
    row[3] = audio_link
    row[8] = "1:02:03"
    row[10] = "5"
    row[15] = "Yes"

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_HEADERS)
        writer.writerow(row)


def find_record_id(audio_link):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM research_records
                WHERE audio_link = %s
                ORDER BY id DESC
                LIMIT 1;
                """,
                (audio_link,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def test_csv_import_and_export_round_trip(tmp_path):
    unique_link = f"https://example.com/test/{uuid.uuid4()}"
    input_csv = tmp_path / "input.csv"
    output_csv = tmp_path / "output.csv"
    record_id = None

    try:
        make_csv(input_csv, unique_link)

        imported_count = import_csv_to_records(input_csv)
        assert imported_count == 1

        record_id = find_record_id(unique_link)
        assert record_id is not None

        record = get_record_by_id(record_id)
        assert record["source"] == "Integration Test"
        assert record["before_processing_duration"] == 3723
        assert record["total_files"] == 5
        assert record["data_available"] == "Yes"

        exported_count = export_records_to_csv(
            output_csv,
            limit=1000,
            offset=0,
        )
        assert exported_count >= 1

        with output_csv.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)
            assert reader.fieldnames == list(CSV_HEADERS)

            exported_row = next(
                row for row in reader
                if row["Audio_Link"] == unique_link
            )

        assert exported_row["Before_Processing_Duration"] == "1:02:03"
        assert exported_row["Total_files"] == "5"
        assert exported_row["Date"] == "2026-10-26"

    finally:
        if record_id is None:
            record_id = find_record_id(unique_link)

        if record_id is not None:
            delete_record(record_id)


def test_invalid_csv_does_not_import_any_rows(tmp_path):
    unique_link = f"https://example.com/test/{uuid.uuid4()}"
    input_csv = tmp_path / "invalid.csv"

    make_csv(input_csv, unique_link)

    # Replace the valid duration with an invalid one.
    with input_csv.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))

    rows[1][8] = "1:99:00"

    with input_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerows(rows)

    with pytest.raises(ValueError):
        import_csv_to_records(input_csv)

    assert find_record_id(unique_link) is None


def test_import_rolls_back_if_second_insert_fails(tmp_path, monkeypatch):
    import csv
    import uuid

    import csv_adapter

    first_link = f"https://example.com/test/{uuid.uuid4()}"
    second_link = f"https://example.com/test/{uuid.uuid4()}"
    input_csv = tmp_path / "rollback.csv"

    rows = []
    for link in (first_link, second_link):
        row = [""] * len(CSV_HEADERS)
        row[2] = "Rollback Test"
        row[3] = link
        rows.append(row)

    with input_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)

    real_get_connection = csv_adapter.get_connection

    class FailingConnection:
        def __init__(self):
            self.conn_context = None
            self.insert_count = 0

        def __enter__(self):
            self.conn_context = real_get_connection()
            self.conn = self.conn_context.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self.conn_context.__exit__(exc_type, exc, tb)

        def cursor(self):
            return FailingCursor(self.conn.cursor())

    class FailingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.cursor.close()

        def execute(self, sql, params=None):
            if "INSERT INTO research_records" in sql:
                test_state["count"] += 1
                if test_state["count"] == 2:
                    raise RuntimeError("Simulated second-insert failure")
            return self.cursor.execute(sql, params)

    test_state = {"count": 0}
    monkeypatch.setattr(
        csv_adapter,
        "get_connection",
        lambda: FailingConnection(),
    )

    try:
        with pytest.raises(RuntimeError, match="Simulated second-insert failure"):
            csv_adapter.import_csv_to_records(input_csv)

        with real_get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM research_records
                    WHERE audio_link IN (%s, %s);
                    """,
                    (first_link, second_link),
                )
                assert cur.fetchone()[0] == 0
    finally:
        monkeypatch.setattr(
            csv_adapter,
            "get_connection",
            real_get_connection,
        )

