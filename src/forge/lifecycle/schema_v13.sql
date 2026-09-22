-- Forge SQLite schema v13 (which memory this work belongs to).
--
-- Additive migration that adds ONE column to ``planning_runs`` (v3) and the
-- same one to ``builds`` (v1): ``memory_project``.
--
-- WHY THIS COLUMN EXISTS (the project's own memory, item 2, 2026-09-21). Every
-- build read and wrote memory under the name "guardkit", because that name came
-- from one setting nothing set. One project's decisions were therefore filed
-- under another project's name, where no build ever looked. The rule now is
-- that a project declares its own memory name, in two lines in its own
-- ``.guardkit/config.yaml``; Forge reads that declaration AT THE RECORDED
-- STARTING COMMIT (``start_commit``, schema_v12), refuses at the door when a
-- project declares none, and hands the name to the build on purpose when it
-- launches it.
--
-- This column is where the name that was read is written down: the one place
-- that says what memory a piece of work actually belonged to, so that the
-- question "where did this build's outcomes go" is answered from the record
-- rather than reconstructed from a setting nobody can see any more.
--
-- The column is NULL-able with no default, on both tables. NULL means NOT
-- RECORDED, and that is the honest reading for every row written before this
-- migration, and for a build queued by hand with no planning run behind it. A
-- row with nothing recorded is not a row that used "guardkit": nobody wrote it
-- down, and the record says so. Nothing here ever substitutes a name it found
-- some other way.
--
-- The copy onto ``builds`` happens at the single ``INSERT INTO builds`` site
-- (``persistence.record_pending_build``), beside the copy of ``start_commit``
-- and ``target_branch`` that schema_v12 added, and from the same planning run.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v12.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v13 in order for fresh databases and applies
-- only v13 to existing v12 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql … schema_v12.sql use.

ALTER TABLE planning_runs
    ADD COLUMN memory_project TEXT;

ALTER TABLE builds
    ADD COLUMN memory_project TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (13, datetime('now'));
