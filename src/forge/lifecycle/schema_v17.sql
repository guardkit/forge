-- Forge SQLite schema v17 (the exact commit a prepared feature is built from).
--
-- Additive migration that adds ONE column to ``builds``: ``source_commit``.
--
-- WHY THIS COLUMN EXISTS (4 October 2026, project initialisation, Part 6). A
-- feature whose spec and plan were written elsewhere (for example in Pi) is
-- queued straight to a build on its own branch, with no planning run behind
-- it. Admission fetches the project's remote, records the default branch as
-- the build's target and the commit the queued branch names as BOTH its
-- ``start_commit`` (schema_v12: the revision its declarations, deploy profile
-- and launch settings are read at, and the merge card's comparison base) and
-- this ``source_commit``. This column is what tells the runner to build that
-- exact commit, so a branch that moves after admission changes nothing.
--
-- NULL-able with no default. NULL means "not a prepared build": every
-- historical row, every factory-planned build (which launches from its
-- planning branch exactly as before) and every build queued by hand reads
-- back as that, and is launched exactly as before.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v16.sql
-- remain frozen. ``ALTER TABLE ... ADD COLUMN`` is not IF-NOT-EXISTS-aware, so
-- the runner relies on the schema_version ledger to apply it once.

ALTER TABLE builds
    ADD COLUMN source_commit TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (17, datetime('now'));
