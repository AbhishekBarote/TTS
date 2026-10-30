
-- Migration 005: Create the audit logs table

CREATE TABLE audit_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- What operation occurred
    action TEXT NOT NULL,

    -- Entity affected by the operation
    entity_type TEXT NOT NULL,
    entity_id BIGINT,

    -- Who or what performed the operation
    performed_by TEXT,

    -- Optional details about the change
    details JSONB,

    -- When the operation occurred
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Find history for a particular entity
CREATE INDEX idx_audit_logs_entity
    ON audit_logs (entity_type, entity_id);

-- Filter logs by action
CREATE INDEX idx_audit_logs_action
    ON audit_logs (action);

-- Retrieve recent activity efficiently
CREATE INDEX idx_audit_logs_created_at
    ON audit_logs (created_at DESC);

-- Filter activity by actor
CREATE INDEX idx_audit_logs_performed_by
    ON audit_logs (performed_by);
