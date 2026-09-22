-- Forge SQLite schema v14 (the settings a project says its builds need).
--
-- Additive migration that adds ONE column to ``planning_runs`` (v3) and the
-- same one to ``builds`` (v1): ``launch_settings``.
--
-- WHY THIS COLUMN EXISTS (22 September 2026, the second half of item 1's
-- section D). A build is launched with a short named list of settings rather
-- than a copy of everything the launching process was holding. That list is
-- the FACTORY'S own — its binary, its settings file, its receipts folder, its
-- bus, its memory, the model seat — and it deliberately carries nothing
-- belonging to any project's own tools, because central code that named one
-- tool's setting would be a factory that had a favourite language.
--
-- So a project says for itself what its builds need beyond that list, in its
-- own ``.guardkit/config.yaml``, by NAME:
--
--     launch:
--       settings: [SOME_TOOL_CACHE, ANOTHER_HOME]
--
-- NAMES ONLY. A value never comes out of a project's settings file: the launch
-- takes each value from the launching process, and only if it has one. This
-- column is where the NAMES that were read are written down — the one place
-- that says what a piece of work was launched with, so "why did this build
-- behave differently" is answered from the record rather than guessed at.
--
-- Read like the memory name, from the same file at the same recorded starting
-- commit (``start_commit``, schema_v12), through the same bounded parser, and
-- checked against the names this factory keeps for itself. A project asking
-- for one of those is refused at the door in plain words.
--
-- The column holds the names as a JSON list of text, written and read by
-- ``forge.planning.run_store`` and ``forge.lifecycle.persistence``. It is
-- NULL-able with no default, on both tables. NULL means NOT DECLARED, which is
-- the honest reading for every row written before this migration and for a
-- build queued by hand. That is NOT the same fact as ``[]``, which means the
-- project was read and asked for nothing extra.
--
-- The copy onto ``builds`` happens at the single ``INSERT INTO builds`` site
-- (``persistence.record_pending_build``), beside the copy of ``start_commit``,
-- ``target_branch`` and ``memory_project``, and from the same planning run.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v13.sql
-- remain frozen. The migrations runner (``forge.lifecycle.migrations.
-- apply_at_boot``) executes v1–v14 in order for fresh databases and applies
-- only v14 to existing v13 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v5.sql … schema_v13.sql use.

ALTER TABLE planning_runs
    ADD COLUMN launch_settings TEXT;

ALTER TABLE builds
    ADD COLUMN launch_settings TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (14, datetime('now'));
