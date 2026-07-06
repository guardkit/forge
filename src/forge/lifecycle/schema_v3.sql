-- Forge SQLite schema v3 (TASK-MP-002).
--
-- Additive migration that introduces planning run persistence:
-- - ``planning_runs`` table: durable records keyed by correlation_id
-- - ``planning_run_events`` table: history log for every state transition
--
-- This script is **delta-only**: schema.sql (v1) and schema_v2.sql remain
-- unchanged. The migration runner (``forge.lifecycle.migrations.apply_at_boot``)
-- executes v1, v2, and v3 in order for fresh databases, and only applies v3
-- to existing v2 databases. The additive discipline ensures that
-- ``builds`` and ``stage_log`` tables are **never modified** by this migration,
-- satisfying the byte-identical schema requirement (AC-001).
--
-- Design rationale (from panel review):
-- - Planning runs do NOT reuse ``builds`` rows — builds has NOT NULL constraints
--   on feature_id/repo/branch (schema.sql:15-21), and build_id derives from
--   feature identity (persistence.py:658).
-- - History lives in ``planning_run_events``, not ``stage_log``, because
--   stage_log.build_id has an **enforced FK to builds** (schema.sql:68)
--   and the relationship cannot be NULL.
-- - The TRANSITION_TABLE in Mode A/B has no PLANNED-HANDOFF terminal state,
--   so planning runs need their own state machine (PLANNING_TRANSITIONS in
--   forge.planning.states).

-- =====================================================================
-- planning_runs — one row per planning request, lifecycle-tracked
-- =====================================================================
CREATE TABLE IF NOT EXISTS planning_runs (
    correlation_id TEXT PRIMARY KEY,

    state TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'RUNNING', 'PAUSED', 'FAILED', 'CANCELLED',
        'TIMED_OUT', 'PLANNED_HANDOFF'
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

-- Index for listing runs by user and time
CREATE INDEX IF NOT EXISTS idx_planning_runs_user
    ON planning_runs (originating_user, queued_at DESC);

-- Index for state-based queries (active runs)
CREATE INDEX IF NOT EXISTS idx_planning_runs_state
    ON planning_runs (state, queued_at DESC);

-- =====================================================================
-- planning_run_events — history log for state transitions
-- =====================================================================
CREATE TABLE IF NOT EXISTS planning_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL REFERENCES planning_runs(correlation_id),

    stage_label TEXT NOT NULL,
    status TEXT NOT NULL,
    gate_mode TEXT,
    coach_score REAL,

    actor_identity TEXT,
    details_json TEXT,

    recorded_at TEXT NOT NULL
) STRICT;

-- Index for fetching events for a single run
CREATE INDEX IF NOT EXISTS idx_planning_run_events_correlation
    ON planning_run_events (correlation_id, recorded_at);

-- Index for trace-capture continuity (FEAT-SPL-005)
CREATE INDEX IF NOT EXISTS idx_planning_run_events_recorded
    ON planning_run_events (recorded_at DESC);

-- =====================================================================
-- Schema version ledger entry
-- =====================================================================
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (3, datetime('now'));
