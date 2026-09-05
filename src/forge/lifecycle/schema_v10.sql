-- Forge SQLite schema v10 (the work queue the factory keeps for itself).
--
-- Lane B stage one, binding spec ``docs/work-queue-spec-2026-09-05.md``
-- contract 4. Two new tables; nothing existing is touched.
--
-- WHY THESE TABLES EXIST. Today every sentence that passes the intake's six
-- gates becomes a planning run immediately, and the only thing stopping two
-- sentences from racing is a broker setting nobody can read
-- (``max_ack_pending=1``). A sentence that arrives while another is building
-- is therefore invisible: there is no list to read, no order to change, and
-- nothing to say back to the person who typed it. ``work_queue`` is that
-- list. A sentence becomes a row here first; a take-next loop admits rows one
-- at a time and only then creates the planning run, with the sentence's
-- ORIGINAL correlation id, so every downstream receipt is unchanged.
--
-- ``rank`` is a REAL on purpose: putting one row in front of another is then
-- one arithmetic step (min - 1.0, or the midpoint of two neighbours) instead
-- of rewriting every row. Ranks are renumbered 1.0, 2.0, ... only when two
-- neighbours collide or come within 1e-6 of each other.
--
-- ``status`` is a closed vocabulary and a row is NEVER deleted: ``drop 9``
-- closes the row WITHDRAWN so the record of what was asked for survives the
-- decision not to build it.
--
-- ``correlation_id`` is UNIQUE because it is the idempotency key: a
-- redelivered planning-queued message must file one row, not two.
--
-- ``work_queue_events`` is the queue's own history — who did what to which
-- row, in the same shape as ``planning_run_events``. ``actor_identity`` is
-- the Slack identity carried on the message that caused the change, so a
-- reordering is attributable.
--
-- This script is **delta-only**: schema.sql ... schema_v9.sql remain frozen.
-- Both statements are ``IF NOT EXISTS``, so the script is safe to re-run; the
-- schema_version ledger row below is what stops it running twice in practice.

CREATE TABLE IF NOT EXISTS work_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sentence TEXT NOT NULL,
  target_repo TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('feature','fix','question')),
  status TEXT NOT NULL CHECK (status IN ('QUEUED','ADMITTED','DONE','WITHDRAWN','BLOCKED')),
  rank REAL NOT NULL,
  after_id INTEGER REFERENCES work_queue(id),
  originating_user TEXT NOT NULL,
  correlation_id TEXT NOT NULL UNIQUE,
  queued_at TEXT NOT NULL, admitted_at TEXT, closed_at TEXT,
  stale_pinged_at TEXT, keep_count INTEGER NOT NULL DEFAULT 0,
  closed_reason TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_work_queue_open ON work_queue (status, rank, queued_at);
CREATE TABLE IF NOT EXISTS work_queue_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  queue_id INTEGER NOT NULL REFERENCES work_queue(id),
  action TEXT NOT NULL, actor_identity TEXT NOT NULL,
  details_json TEXT, recorded_at TEXT NOT NULL
) STRICT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (10, datetime('now'));
