-- Forge SQLite schema v8 (the fix journey's durable subject).
--
-- Additive migration that adds the ``task_id`` column to the ``builds`` table
-- (v1 schema.sql). Conductor revival Stage 2, shakeout item 1.
--
-- WHY THIS COLUMN EXISTS. ``forge queue --mode c TASK-XXX`` already puts the
-- task identifier on the WIRE (``BuildQueuedPayload.task_id``, required iff
-- ``mode == "mode-c"`` by the nats-core model validator) — but nothing ever
-- persisted it. The wire payload lives for one dequeue; the conductor's fix
-- journey is a whole journey of stages, each of which must be dispatched
-- against that subject (``/task-review --task-id TASK-XXX``). The subprocess
-- dispatcher REFUSES a subject-less fix-journey dispatch rather than review an
-- inferred subject, so without a durable anchor the very first Mode C dispatch
-- fails — and a daemon restart mid-journey could never recover the subject at
-- all. This column is that anchor: the same durable-row discipline the mode
-- and profile columns already follow.
--
-- The column is NULL-able with no default: NULL means "no task subject" —
-- which is every Mode A / Mode B build and every historical row. Readers treat
-- NULL as "not a fix journey's subject", so the change is backward-compatible
-- by construction and the routine path is untouched.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v7.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v8 in order for fresh databases and applies only
-- v8 to existing v7 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql / schema_v6.sql / schema_v7.sql use.

ALTER TABLE builds
    ADD COLUMN task_id TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (8, datetime('now'));
