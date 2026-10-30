
-- Migration 004: Create the duplicate-check cache

CREATE TABLE duplicate_cache (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Unique key identifying the duplicate-check inputs
    cache_key TEXT NOT NULL UNIQUE,

    -- Optional hash of normalized content
    content_hash TEXT,

    -- Result of the duplicate check
    duplicate_status TEXT NOT NULL,

    -- Candidate matches and supporting evidence
    duplicate_evidence JSONB,

    -- Version of the duplicate-check rules used
    rules_version TEXT NOT NULL,

    -- Cache expiration
    expires_at TIMESTAMPTZ NOT NULL,

    -- Database-managed timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Find cache entries by content hash
CREATE INDEX idx_duplicate_cache_content_hash
    ON duplicate_cache (content_hash);

-- Find entries that have expired
CREATE INDEX idx_duplicate_cache_expires_at
    ON duplicate_cache (expires_at);

-- Filter cached results by duplicate status
CREATE INDEX idx_duplicate_cache_duplicate_status
    ON duplicate_cache (duplicate_status);
