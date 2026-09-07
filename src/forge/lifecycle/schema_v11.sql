-- Forge SQLite schema v11 (the branch the merge word merges).
--
-- Additive migration that adds the ``merge_branch`` column to the ``builds``
-- table (v1 schema.sql). Rewrite-on-refusal spec 2026-09-06, Part M, rule 54.
--
-- WHY THIS COLUMN EXISTS. Every build so far has been a feature build on
-- ``autobuild/<feature id>``, so the branch the merge word merges was implied
-- and every reader derived it. A repair's commits land on the fix journey's
-- own branch (``fix/<task id>-<build8>``, cut from ``repair/<task id>``), and
-- nothing recorded that name: the merge offer, the candidate check, the
-- landed-merge detection and the merge command would all have merged
-- ``autobuild/<feature id>`` — the original feature's branch, already on main
-- — and the repair would never have reached main. This column is where the
-- conductor writes the branch it cut, so the merge word merges the branch the
-- build actually made, whatever it is called.
--
-- ``builds.branch`` IS NOT CHANGED BY THIS MIGRATION AND MUST NOT BE: it holds
-- the branch the build was queued ON (its base), which is a different fact.
--
-- The column is NULL-able with no default. NULL means "the feature's own
-- branch": every historical row and every feature build leaves it empty, and
-- every reader falls back to ``autobuild/<feature id>`` exactly as before, so
-- a feature build behaves byte for byte as it does today.
--
-- Last write wins on the write side (``persistence.record_merge_branch``),
-- mirroring ``worktree_path``: the conductor's reuse arm re-records the same
-- branch on a redelivery. The write is status-preserving — ``apply_transition``
-- remains the sole ``builds.status`` writer.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v10.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v11 in order for fresh databases and applies
-- only v11 to existing v10 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql … schema_v9.sql use.

ALTER TABLE builds
    ADD COLUMN merge_branch TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (11, datetime('now'));
