
import pytest

from repositories.records import (
    create_record,
    get_record_by_id,
    get_records,
    update_record,
    delete_record,
)


@pytest.fixture
def sample_record():
    record_id = create_record({
        "source": "PYTEST_TEMPORARY",
        "description": "Automated test record",
        "accent": "Indian English",
    })

    yield record_id

    delete_record(record_id)


def test_create_and_read_record(sample_record):
    record = get_record_by_id(sample_record)

    assert record is not None
    assert record["source"] == "PYTEST_TEMPORARY"
    assert record["description"] == "Automated test record"


def test_update_record(sample_record):
    updated = update_record(
        sample_record,
        {"description": "Updated test description"},
    )

    assert updated is True

    record = get_record_by_id(sample_record)
    assert record["description"] == "Updated test description"
    assert record["updated_at"] is not None


def test_get_records():
    records = get_records(limit=10, offset=0)

    assert isinstance(records, list)
    assert len(records) <= 10


def test_delete_record(sample_record):
    deleted = delete_record(sample_record)

    assert deleted is True
    assert get_record_by_id(sample_record) is None


def test_reject_invalid_field():
    with pytest.raises(ValueError):
        create_record({"invalid_column": "test"})



def test_reject_invalid_update_field():
    with pytest.raises(ValueError):
        update_record(1, {"invalid_column": "test"})


def test_reject_invalid_limit():
    with pytest.raises(ValueError):
        get_records(limit=0)
