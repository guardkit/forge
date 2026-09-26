-- Forge SQLite schema v4 (Lane B / Phase E1 — forge target terminal).
--
-- Additive migration that widens the ``planning_runs.state`` CHECK
-- constraint to admit the target-terminal chain states introduced by the
-- Lane B FSM extension (post-factory-2-three-lanes-handoff.md §3 B1):
--
--     FEATURE_SPEC, FEATURE_PLAN, BUILD_QUEUED
--
-- These states are only ever written when the
-- ``planning.target_terminal.enabled`` flag is on; with the flag off the
-- widened CHECK admits values that are never produced, so this migration is
-- a behavioural byte-for-byte no-op for existing (flag-off) runs.
--
-- Why a table rebuild: SQLite cannot ALTER an existing CHECK constraint in
-- place, so we follow the canonical rebuild recipe (create-new / copy /
-- drop-old / rename). ``schema_v3.sql`` is left frozen (version 3 keeps its
-- meaning); this delta only applies to databases below version 4.
--
-- Foreign keys: ``planning_run_events.correlation_id`` REFERENCES
-- planning_runs, so dropping the old parent table would fail on the implicit
-- DELETE FROM that a DROP performs while enforcement is on.
--
-- UPDATED 26 September 2026. This script used to switch ``foreign_keys`` off
-- itself and wrap the swap in its own BEGIN/COMMIT, on the reasoning that the
-- runner's ``executescript`` committed first anyway. That reasoning was the
-- other side of the defect the runner has now fixed: a migration batch that
-- was never one transaction. The runner
-- (``forge.lifecycle.migrations.apply_at_boot``) now owns the single
-- transaction around the whole batch and switches foreign-key enforcement off
-- outside it — which is the only place that works, because PRAGMA
-- foreign_keys is silently ignored inside a transaction — and it reads the
-- rows of ``PRAGMA foreign_key_check`` before committing, which this script's
-- own copy of that check never did (``executescript`` threw the rows away).
-- So the three PRAGMAs and the BEGIN/COMMIT are gone from here, and a
-- migration that carries its own transaction statements is now refused by the
-- runner by name.

-- Rebuilt planning_runs with the widened state CHECK. All other columns,
-- constraints, and STRICT mode are copied verbatim from schema_v3.sql.
CREATE TABLE planning_runs_v4 (
    correlation_id TEXT PRIMARY KEY,

    state TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'RUNNING', 'PAUSED', 'FAILED', 'CANCELLED',
        'TIMED_OUT', 'PLANNED_HANDOFF',
        'FEATURE_SPEC', 'FEATURE_PLAN', 'BUILD_QUEUED'
    )),

    originating_user TEXT NOT NULL,
    expected_approver TEXT NOT NULL,
    request_text TEXT NOT NULL,

    target_repo TEXT,
    triggered_by TEXT NOT NULL CHECK (triggered_by IN (
        'cli', 'jarvis', 'forge-internal', 'notification-adapter'
    )),
    originating_adapter TEXT,
    parent_request_id TEXT,

    -- PAUSED-state workflow identifiers
    pending_approval_request_id TEXT,

    -- Escalation counters and timestamps (RT-04 durable escalation state)
    defer_count INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    escalated_at TEXT,

    -- Handoff outputs (populated on PLANNED_HANDOFF terminal)
    handoff_branch TEXT,
    handoff_path TEXT,

    -- Lifecycle timestamps (ISO 8601 UTC)
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,

    -- Error details (populated on FAILED terminal)
    error TEXT
) STRICT;

INSERT INTO planning_runs_v4 SELECT * FROM planning_runs;

DROP TABLE planning_runs;

ALTER TABLE planning_runs_v4 RENAME TO planning_runs;

-- Re-create the indexes dropped with the old table.
CREATE INDEX IF NOT EXISTS idx_planning_runs_user
    ON planning_runs (originating_user, queued_at DESC);

CREATE INDEX IF NOT EXISTS idx_planning_runs_state
    ON planning_runs (state, queued_at DESC);

-- Schema version ledger entry. It commits atomically with the rebuild, and
-- with every other migration in the same boot's batch, because the runner
-- holds one transaction around all of them.
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (4, datetime('now'));
