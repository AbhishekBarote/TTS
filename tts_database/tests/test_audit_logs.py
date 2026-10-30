import uuid

import pytest

from connection import get_connection
from repositories.audit_logs import (
    create_audit_log,
    get_audit_log,
    get_audit_logs,
)


def unique_action():
    return f"PYTEST_{uuid.uuid4().hex}"


def cleanup_audit_log(log_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE id = %s",
                (log_id,),
            )


def test_create_and_read_audit_log():
    log_id = None
    action = unique_action()

    try:
        log_id = create_audit_log(
            action=action,
            entity_type="research_record",
            entity_id=987654,
            performed_by="pytest",
            details={"reason": "test event", "count": 2},
        )

        entry = get_audit_log(log_id)

        assert entry is not None
        assert entry["id"] == log_id
        assert entry["action"] == action
        assert entry["entity_type"] == "research_record"
        assert entry["entity_id"] == 987654
        assert entry["performed_by"] == "pytest"
        assert entry["details"]["reason"] == "test event"

    finally:
        if log_id is not None:
            cleanup_audit_log(log_id)


def test_filter_audit_logs():
    log_id = None
    action = unique_action()

    try:
        log_id = create_audit_log(
            action=action,
            entity_type="research_record",
            entity_id=987655,
            performed_by="pytest_filter",
            details={"test": True},
        )

        results = get_audit_logs(
            entity_type="research_record",
            entity_id=987655,
            action=action,
            performed_by="pytest_filter",
        )

        assert any(entry["id"] == log_id for entry in results)

    finally:
        if log_id is not None:
            cleanup_audit_log(log_id)


def test_missing_audit_log_returns_none():
    entry = get_audit_log(-987654321)
    assert entry is None


def test_invalid_action_is_rejected():
    with pytest.raises(ValueError, match="action"):
        create_audit_log(
            action=" ",
            entity_type="research_record",
        )


def test_invalid_entity_type_is_rejected():
    with pytest.raises(ValueError, match="entity_type"):
        create_audit_log(
            action="test_action",
            entity_type=" ",
        )


def test_invalid_details_are_rejected():
    with pytest.raises(ValueError, match="details"):
        create_audit_log(
            action="test_action",
            entity_type="research_record",
            details="not structured JSON data",
        )


def test_invalid_limit_is_rejected():
    with pytest.raises(ValueError, match="limit"):
        get_audit_logs(limit=0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])