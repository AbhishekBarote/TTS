
-- Migration 006: Create the speaker duration ledger
--
-- Tracks accepted duration per speaker to enforce the 3.5-hour (12600 s) cap.
-- Rows are inserted/updated only inside the approval transaction, so the total
-- always reflects committed, non-duplicate records.

CREATE TABLE speaker_ledger (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Matches speakers_id in research_records / revalidation_queue.
    speakers_id   TEXT NOT NULL UNIQUE,

    -- Sum of after_processing_duration for all approved records for this speaker.
    accepted_seconds BIGINT NOT NULL DEFAULT 0 CHECK (accepted_seconds >= 0),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Quick cap-check lookups by speaker ID.
CREATE INDEX idx_speaker_ledger_speakers_id
    ON speaker_ledger (speakers_id);
