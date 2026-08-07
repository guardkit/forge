-- Forge SQLite schema v9 (timeout truth).
--
-- Additive migration that adds the ``terminal_class`` column to the ``builds``
-- table (v1 schema.sql). Monitored-supervision lane, stage 1.
--
-- WHY THIS COLUMN EXISTS. ``builds.status`` spells FIVE structurally different
-- deaths the same way: ``FAILED``. A build the semantic monitor killed for
-- going quiet, a build killed at its budget wall-clock cap, a build whose
-- runner-side wall clock expired, a build whose guardkit SDK call timed out
-- in-band, and a build that simply did not compile were indistinguishable to
-- everything downstream of the runner — the only carrier of the difference was
-- the free-form prose in ``builds.error``. "This ran out of time" and "this is
-- broken" are opposite verdicts with opposite next actions (re-queue with more
-- room vs. fix the code), and an operator reading ``forge status`` could not
-- tell them apart. This column is the machine-readable answer.
--
-- ``builds.status`` IS NOT CHANGED BY THIS MIGRATION AND MUST NOT BE. There is
-- no new status value; a timeout is still a FAILED build. The distinction is
-- strictly additive so every existing reader of ``status`` — jarvis cards, the
-- CLI, the state machine — keeps its exact current behaviour.
--
-- The column is NULL-able with no default. NULL means "not classified": every
-- historical row, every build that predates this lane, and every ORDINARY
-- failure — the runner deliberately never writes the ``error`` class, so its
-- absence is its value, and the common path stays a plain FAILED row with no
-- extra write. Readers treat NULL as "no timeout of any kind is claimed".
--
-- The vocabulary is closed and lives in
-- ``forge.subagents.build_monitor.TERMINAL_CLASSES``:
--   timeout-wedge       the semantic build monitor's kill
--   timeout-budget-cap  the FEAT-UBS-002 per-build wall-clock cap kill
--   timeout-wall-clock  the runner's own insanity bound expired
--   timeout-in-band     guardkit's own task/SDK clock fired inside the child
--   error               everything else — never written; NULL says it
--
-- First-write-wins on the write side (``persistence.record_terminal_class``),
-- mirroring ``budget_breach``: a redelivered envelope re-recording the same
-- truth is a no-op, and the write is status-preserving — it never claims a
-- state the daemon did not effect. ``apply_transition`` remains the sole
-- ``builds.status`` writer.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v8.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v9 in order for fresh databases and applies only
-- v9 to existing v8 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql … schema_v8.sql use.

ALTER TABLE builds
    ADD COLUMN terminal_class TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (9, datetime('now'));
