import pytest

from connection import get_connection

from repositories.records import (
    get_record_by_id,
    delete_record,
)

from repositories.audit_logs import (
    get_audit_logs,
)

from repositories.revalidation_queue import (
    submit_for_revalidation,
    get_queue_record,
    update_queue_status,
    update_duplicate_status,
    approve_queue_record,
)


def create_test_record(source):
    return submit_for_revalidation({
        "source": source,
        "description": "Temporary approval workflow test",
        "accent": "Indian English",
        "licensing": "CC BY",
        "data_available": "Not_transferred",
    })


def cleanup(queue_id, permanent_id=None):
    if permanent_id is not None:
        delete_record(permanent_id)

    if queue_id is not None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM revalidation_queue WHERE id = %s",
                    (queue_id,),
                )


def test_successful_approval():
    queue_id = None
    permanent_id = None
    audit_log_id = None

    try:
        queue_id = create_test_record("PYTEST_APPROVAL_SUCCESS")

        assert get_queue_record(queue_id)["queue_status"] == "pending"

        assert update_duplicate_status(queue_id, "unique") is True
        assert update_queue_status(
            queue_id, "ready_for_approval"
        ) is True

        permanent_id = approve_queue_record(queue_id)

        permanent_record = get_record_by_id(permanent_id)
        queue_record = get_queue_record(queue_id)

        assert permanent_record is not None
        assert permanent_record["source"] == "PYTEST_APPROVAL_SUCCESS"
        assert queue_record["queue_status"] == "approved"
        assert queue_record["approved_record_id"] == permanent_id

        # Verify that approval created an audit event.
        audit_entries = get_audit_logs(
            entity_type="research_record",
            entity_id=permanent_id,
            action="record_approved",
        )

        matching_entries = [
            entry
            for entry in audit_entries
            if entry["details"]["queue_id"] == queue_id
        ]

        assert len(matching_entries) == 1
        assert matching_entries[0]["details"]["duplicate_status"] == "unique"

        audit_log_id = matching_entries[0]["id"]

        print("APPROVAL AND AUDIT LOGGING: passed")

    finally:
        if audit_log_id is not None:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM audit_logs WHERE id = %s",
                        (audit_log_id,),
                    )

        cleanup(queue_id, permanent_id)



def test_pending_record_cannot_be_approved():
    queue_id = None

    try:
        queue_id = create_test_record("PYTEST_PENDING_REJECTION")

        with pytest.raises(ValueError, match="ready_for_approval"):
            approve_queue_record(queue_id)

        assert get_queue_record(queue_id)["queue_status"] == "pending"
        print("PENDING APPROVAL REJECTION: passed")

    finally:
        cleanup(queue_id)


def test_duplicate_record_cannot_be_approved():
    queue_id = None

    try:
        queue_id = create_test_record("PYTEST_DUPLICATE_REJECTION")

        assert update_duplicate_status(queue_id, "duplicate") is True
        assert update_queue_status(
            queue_id, "ready_for_approval"
        ) is True

        with pytest.raises(ValueError, match="confirmed as unique"):
            approve_queue_record(queue_id)

        queue_record = get_queue_record(queue_id)
        assert queue_record["queue_status"] == "ready_for_approval"
        assert queue_record["approved_record_id"] is None

        print("DUPLICATE APPROVAL REJECTION: passed")

    finally:
        cleanup(queue_id)


def test_not_checked_record_cannot_be_approved():
    queue_id = None

    try:
        queue_id = create_test_record("PYTEST_UNCHECKED_REJECTION")

        assert update_queue_status(
            queue_id, "ready_for_approval"
        ) is True

        with pytest.raises(ValueError, match="confirmed as unique"):
            approve_queue_record(queue_id)

        print("UNCHECKED APPROVAL REJECTION: passed")

    finally:
        cleanup(queue_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_direct_approval_status_update_is_rejected():
    queue_id = None

    try:
        queue_id = create_test_record(
            "PYTEST_DIRECT_APPROVAL_REJECTION"
        )

        update_duplicate_status(queue_id, "unique")
        update_queue_status(queue_id, "ready_for_approval")

        with pytest.raises(ValueError, match="approve_queue_record"):
            update_queue_status(queue_id, "approved")

        queue_record = get_queue_record(queue_id)

        assert queue_record["queue_status"] == "ready_for_approval"
        assert queue_record["approved_record_id"] is None

        print("DIRECT APPROVAL REJECTION: passed")

    finally:
        cleanup(queue_id)
