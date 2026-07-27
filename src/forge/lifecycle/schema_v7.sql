-- Forge SQLite schema v7 (budget-breach detection record — the honest wall).
--
-- Additive migration that adds the ``budget_breach`` column to the ``builds``
-- table (v1 schema.sql). FEAT-UBS-002 stage 2 (DETECT): the ``forge serve``
-- daemon's lifecycle-bridge observer evaluates a build's budget after each
-- ``stage-complete`` envelope it publishes; on the FIRST cap breach it lands a
-- compact human-readable record here and escalates a risk=high approval. It
-- does NOT pause / cancel / rewrite ``builds.status`` — a mid-run hard stop is
-- not achievable on this path (the runs.cancel seam is dark), so this column is
-- the daemon's HONEST record of what it detected, never a claim about a state
-- it effected. The run continues to its own bounded end (stage 1's wall-clock
-- self-bound); the terminal contracts stand byte-identical.
--
-- Value shape (illustrative, not enforced): a compact one-liner naming the
-- breached cap, the measurement vs the cap, and an ISO-8601 timestamp, e.g.
-- ``wall_clock: 3712.0s > 3600.0s @ 2026-07-27T09:14:02+00:00`` or
-- ``coach_score: 0.0 < 0.5 floor @ 2026-07-27T09:14:02+00:00``.
--
-- First-write-wins: ``SqliteLifecyclePersistence.record_budget_breach`` writes
-- only ``WHERE budget_breach IS NULL``, so a later cap breach never overwrites
-- the first — mirroring ``budget_guard.evaluate_budget``'s first-breach-wins
-- ordering. ``read_budget_breach`` / ``latest_breach_for_feature`` read it back
-- (the latter feeds the stage-3 pre-dispatch gate).
--
-- The column is NULL-able with no default: a NULL value means "no budget breach
-- detected for this build". Historical rows therefore read back as NULL and the
-- readers treat a missing record as "clean" — backward-compatible by
-- construction.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql / schema_v3.sql
-- / schema_v4.sql / schema_v5.sql / schema_v6.sql remain frozen. The migrations
-- runner (``forge.lifecycle.migrations.apply_at_boot``) executes v1–v7 in order
-- for fresh databases and applies only v7 to existing v6 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql / schema_v6.sql use for their additive columns.

ALTER TABLE builds
    ADD COLUMN budget_breach TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (7, datetime('now'));
