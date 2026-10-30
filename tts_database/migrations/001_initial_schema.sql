
-- Migration 001: Create the main research records table

CREATE TABLE research_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Original CSV metadata (22 fields)
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

    -- Database-managed timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Prevent invalid negative values
    CONSTRAINT chk_before_duration_nonnegative
        CHECK (before_processing_duration IS NULL OR before_processing_duration >= 0),

    CONSTRAINT chk_after_duration_nonnegative
        CHECK (after_processing_duration IS NULL OR after_processing_duration >= 0),

    CONSTRAINT chk_total_files_nonnegative
        CHECK (total_files IS NULL OR total_files >= 0)
);
