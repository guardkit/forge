-- Forge SQLite schema v12 (where a piece of work started).
--
-- Additive migration that adds two columns to ``planning_runs`` (v3) and the
-- same two to ``builds`` (v1): ``start_commit`` and ``target_branch``.
--
-- WHY THESE COLUMNS EXIST (one true copy, item 1, 2026-09-21). Until now the
-- factory cut every new piece of work from whatever its own copy of the
-- project happened to have checked out, and wrote down nowhere what commit
-- the work started from. The starting rule changes that: before the first
-- branch is cut, the project's remote named ``origin`` is fetched, the branch
-- that remote calls its default is looked up once, and the branch is cut from
-- exactly that commit. These two columns are where that commit and that
-- branch name are written down, so that everything afterwards uses the SAME
-- two facts instead of looking them up again and possibly getting different
-- ones.
--
-- Both columns are NULL-able with no default, on both tables. NULL means NOT
-- RECORDED, and that is the honest reading for every row written before this
-- migration: the readers say "not recorded" and never guess a commit. A run
-- or a build with no recorded starting point is not a run that started from
-- the remote's tip — nobody knows where it started, and the record says so.
--
-- ``builds.branch`` IS NOT CHANGED AND MUST NOT BE: it holds the branch the
-- build was queued ON. ``target_branch`` is a different fact — the name of
-- the branch on the remote that this work is aimed at, decided once when the
-- work started (the design's "one target branch, decided once").
--
-- The copy onto ``builds`` happens at the single ``INSERT INTO builds`` site
-- (``persistence.record_pending_build``), which reads the planning run for the
-- build's correlation id in the same transaction. A build with no planning run
-- — a hand-queued Mode A build — simply leaves both columns NULL, exactly as
-- every historical row does.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v11.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v12 in order for fresh databases and applies
-- only v12 to existing v11 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql … schema_v11.sql use.

ALTER TABLE planning_runs
    ADD COLUMN start_commit TEXT;

ALTER TABLE planning_runs
    ADD COLUMN target_branch TEXT;

ALTER TABLE builds
    ADD COLUMN start_commit TEXT;

ALTER TABLE builds
    ADD COLUMN target_branch TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (12, datetime('now'));
