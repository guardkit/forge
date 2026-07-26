-- Forge SQLite schema v6 (coach-score durability — the budget floor's store).
--
-- Additive migration that adds the ``last_coach_score`` column to the
-- ``builds`` table (v1 schema.sql). The UBS1C coach score is born in the
-- autobuild runner and survives the lifecycle-bridge translation only as
-- ``StageCompletePayload.coach_score`` / ``BuildPausedPayload.coach_score``;
-- before this column the daemon had nowhere durable to land it, so the score
-- died with the run. The column is the sink the recorder write-back writes to
-- and the ``min_coach_score`` budget floor reads back via
-- ``SqliteLifecyclePersistence.read_last_coach_score`` (the Supervisor's
-- ``budget_coach_score_reader`` DI). Only ``last_coach_score`` survives
-- translation, so this is the single column the store needs — the per-wave
-- aggregate is dropped at ``translation._Snapshot`` and is not persisted.
--
-- The column is NULL-able with no default: a NULL value means "no coach score
-- recorded for this build". Historical rows therefore read back as NULL and
-- the floor treats a missing score as inert (never a false breach) — backward-
-- compatible by construction.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql / schema_v3.sql
-- / schema_v4.sql / schema_v5.sql remain frozen. The migrations runner
-- (``forge.lifecycle.migrations.apply_at_boot``) executes v1–v6 in order for
-- fresh databases and applies only v6 to existing v5 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql uses for the ``profile`` column.

ALTER TABLE builds
    ADD COLUMN last_coach_score REAL;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (6, datetime('now'));
