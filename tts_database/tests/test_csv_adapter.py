
import csv

import pytest

from csv_adapter import (
    CSV_HEADERS,
    duration_to_seconds,
    seconds_to_duration,
    _read_csv,
)


def write_test_csv(path, headers=None, row=None):
    headers = headers or list(CSV_HEADERS)

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)

        if row is not None:
            writer.writerow(row)


def test_duration_conversion():
    assert duration_to_seconds("1:02:03") == 3723
    assert duration_to_seconds("0:00:00") == 0
    assert duration_to_seconds("12:00:00") == 43200
    assert duration_to_seconds("") is None

    assert seconds_to_duration(3723) == "1:02:03"
    assert seconds_to_duration(0) == "0:00:00"
    assert seconds_to_duration(None) == ""


@pytest.mark.parametrize(
    "value",
    ["1:60:00", "1:00:60", "-1:00:00", "abc", "01:20", "1:2:3"],
)
def test_invalid_duration(value):
    with pytest.raises(ValueError):
        duration_to_seconds(value)


def test_rejects_wrong_header_order(tmp_path):
    path = tmp_path / "wrong_headers.csv"
    headers = list(CSV_HEADERS)
    headers[0], headers[1] = headers[1], headers[0]
    write_test_csv(path, headers=headers)

    with pytest.raises(ValueError, match="CSV headers"):
        _read_csv(path)


def test_reads_and_converts_csv_row(tmp_path):
    path = tmp_path / "valid.csv"
    row = [""] * len(CSV_HEADERS)

    row[0] = "2026-10-26"  # Date
    row[2] = "YouTube"  # Source
    row[3] = "https://example.com/audio"  # Audio_Link
    row[8] = "1:02:03"  # Before_Processing_Duration
    row[10] = "5"  # Total_files
    row[15] = "Yes"  # Data_available

    write_test_csv(path, row=row)

    records = _read_csv(path)

    assert len(records) == 1
    assert records[0]["research_date"].isoformat() == "2026-10-26"
    assert records[0]["source"] == "YouTube"
    assert records[0]["audio_link"] == "https://example.com/audio"
    assert records[0]["before_processing_duration"] == 3723
    assert records[0]["total_files"] == 5
    assert records[0]["data_available"] == "Yes"
    assert records[0]["completion_date"] is None
    assert records[0]["after_processing_duration"] is None


def test_rejects_invalid_date(tmp_path):
    path = tmp_path / "invalid_date.csv"
    row = [""] * len(CSV_HEADERS)
    row[0] = "26/10/2026"

    write_test_csv(path, row=row)

    with pytest.raises(ValueError, match="line 2"):
        _read_csv(path)


def test_rejects_negative_total_files(tmp_path):
    path = tmp_path / "negative_files.csv"
    row = [""] * len(CSV_HEADERS)
    row[10] = "-1"

    write_test_csv(path, row=row)

    with pytest.raises(ValueError, match="cannot be negative"):
        _read_csv(path)
