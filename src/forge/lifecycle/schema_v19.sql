-- Forge SQLite schema v19 (review-preview slot leases, web readiness W7).
--
-- Additive migration that adds ONE new table, ``preview_leases``. No existing
-- table, column or index is touched.
--
-- WHY THIS TABLE EXISTS (12 October 2026). A web project's merge card can now
-- carry a preview of the candidate, joined onto the target branch, running in
-- one of a few fixed slots for a day. The slot is the only thing held while
-- the card waits, so who holds which slot, with which link token, until when,
-- has to survive a coordinator restart: the restart reconciles these rows
-- against the preview projects actually running, and the sweep tears a
-- preview down when its day is up and deletes its evidence after its
-- retention.
--
-- A lease holds its slot while its state is 'preparing', 'ready' or 'ending'
-- ('ending' = its teardown has not been confirmed yet). The partial unique
-- index makes two live leases on one port impossible, whichever process asks.
-- 'failed' and 'ended' hold nothing; their rows stay as the record, and the
-- evidence folder they name is deleted when evidence_expires_at passes.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v18.sql
-- remain frozen.

CREATE TABLE IF NOT EXISTS preview_leases (
    lease_id             TEXT PRIMARY KEY,
    -- The slot's position in the project's profile, and its public port.
    -- NULL when preparation stopped before a slot was granted.
    slot                 INTEGER,
    port                 INTEGER,
    token                TEXT NOT NULL,
    project              TEXT NOT NULL,
    feature_id           TEXT NOT NULL,
    build_id             TEXT NOT NULL,
    candidate_sha        TEXT NOT NULL,
    candidate_tree       TEXT NOT NULL,
    preview_tree         TEXT,
    run_id               TEXT NOT NULL,
    compose_project      TEXT,
    evidence_dir         TEXT,
    state                TEXT NOT NULL
        CHECK (state IN ('preparing', 'ready', 'ending', 'failed', 'ended')),
    reason               TEXT,
    created_at           TEXT NOT NULL,
    app_expires_at       TEXT NOT NULL,
    evidence_expires_at  TEXT NOT NULL,
    ended_at             TEXT,
    evidence_deleted_at  TEXT,
    updated_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS preview_leases_one_live_lease_per_port
    ON preview_leases (port)
    WHERE state IN ('preparing', 'ready', 'ending');

CREATE INDEX IF NOT EXISTS preview_leases_by_build
    ON preview_leases (build_id);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (19, datetime('now'));
