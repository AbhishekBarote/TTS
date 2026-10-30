
-- Migration 002: Add indexes and additional data-integrity constraints

-- 1. Index for searching records by audio link
CREATE INDEX IF NOT EXISTS idx_research_records_audio_link
ON research_records (audio_link);

-- 2. Index for filtering records by speaker ID
CREATE INDEX IF NOT EXISTS idx_research_records_speakers_id
ON research_records (speakers_id);

-- 3. Index for filtering records by status
CREATE INDEX IF NOT EXISTS idx_research_records_status
ON research_records (status);

-- 4. Index for filtering records by research date
CREATE INDEX IF NOT EXISTS idx_research_records_research_date
ON research_records (research_date);

-- 5. Index for filtering records by dataset name
CREATE INDEX IF NOT EXISTS idx_research_records_dataset_name
ON research_records (dataset_name);
