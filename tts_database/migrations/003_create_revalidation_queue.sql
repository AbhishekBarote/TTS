
-- Migration 003: Create the revalidation queue
-- Stores all 22 original CSV metadata fields before approval.

CREATE TABLE revalidation_queue (
    -- Unique queue entry identifier
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Original CSV metadata: 22 fields
    research_date DATE,
    completion_date DATE,
    source TEXT,
    audio_link TEXT,
    text_link TEXT,
    description TEXT,
    dataset_name TEXT,
    gender TEXT,

    -- Durations stored as total seconds
    before_processing_duration INTEGER,
    after_processing_duration INTEGER,

    total_files INTEGER,
    researched_by TEXT,
    assigned_to TEXT,
    status TEXT,
    data_type_augmentation_status TEXT,
    data_available TEXT,
    speakers_id TEXT,
    speaker_channel_name TEXT,
    accent TEXT,
    licensing TEXT,
    handover TEXT,
    remark TEXT,

    -- Queue processing and duplicate-check information
    queue_status TEXT NOT NULL DEFAULT 'pending',
    duplicate_status TEXT NOT NULL DEFAULT 'not_checked',
    duplicate_evidence JSONB,

    -- Optional link to the approved record, once approved
    approved_record_id BIGINT
        REFERENCES research_records(id)
        ON DELETE SET NULL,

    -- Database-managed timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Data integrity checks
    CONSTRAINT chk_queue_before_duration_nonnegative
        CHECK (
            before_processing_duration IS NULL
            OR before_processing_duration >= 0
        ),

    CONSTRAINT chk_queue_after_duration_nonnegative
        CHECK (
            after_processing_duration IS NULL
            OR after_processing_duration >= 0
        ),

    CONSTRAINT chk_queue_total_files_nonnegative
        CHECK (total_files IS NULL OR total_files >= 0)
);

-- Indexes for common queue operations
CREATE INDEX idx_revalidation_queue_status
    ON revalidation_queue (queue_status);

CREATE INDEX idx_revalidation_queue_duplicate_status
    ON revalidation_queue (duplicate_status);

CREATE INDEX idx_revalidation_queue_audio_link
    ON revalidation_queue (audio_link);

CREATE INDEX idx_revalidation_queue_speakers_id
    ON revalidation_queue (speakers_id);

CREATE INDEX idx_revalidation_queue_created_at
    ON revalidation_queue (created_at);


ALLOWED_QUEUE_STATUSES = {
    "pending",
    "ready_for_approval",
    "approved",
    "rejected",
}


def update_queue_status(queue_id: int, new_status: str) -> bool:
    """Update queue status using allowed status transitions."""
    allowed_transitions = {
        "pending": {"ready_for_approval", "rejected"},
        "ready_for_approval": {"approved", "rejected"},
        "approved": set(),
        "rejected": set(),
    }

    if new_status not in ALLOWED_QUEUE_STATUSES:
        raise ValueError(f"Invalid queue status: {new_status}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT queue_status
                FROM revalidation_queue
                WHERE id = %s
                FOR UPDATE;
                """,
                (queue_id,),
            )
            row = cur.fetchone()

            if row is None:
                return False

            current_status = row[0]

            if new_status not in allowed_transitions[current_status]:
                raise ValueError(
                    f"Transition not allowed: "
                    f"{current_status} -> {new_status}"
                )

            cur.execute(
                """
                UPDATE revalidation_queue
                SET queue_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
                """,
                (new_status, queue_id),
            )

            return True
