-- Forge SQLite schema v15 (the publication record).
--
-- Additive migration that adds ONE new table, ``publication_records``. No
-- existing table, column or index is touched, so a forge that never reads the
-- record behaves exactly as it does today.
--
-- WHY THIS TABLE EXISTS (22 September 2026; the one-true-copy design pass,
-- item 1, second revision A and third revision E). The merge word used to be
-- a thing that happened: a merge inside the factory's own copy, a deploy, and
-- a sentence. Nothing wrote down what had been done, so a coordinator that
-- stopped part-way through could not tell a step that had finished from one
-- that had not, and the only way to find out was to ask the owner for the
-- merge word a second time.
--
-- ONE ROW PER BUILD, holding:
--
--   * who gave the merge word and when, and which branch of the remote this
--     piece of work is aimed at (the branch recorded when the work started —
--     never one chosen now);
--   * G, the commit the remote's recorded target branch was at when the merge
--     word was acted on, and J, the joined commit made from G and the build's
--     own tip;
--   * what the checks produced, and the identity of the thing that was
--     checked;
--   * the lines: every step written down TWICE — "about to", with the attempt
--     number and the exact inputs, BEFORE anything is done, and "done", with
--     the result, after. A step that succeeded and whose "done" line was never
--     written is then a question that can be answered by looking at the world,
--     instead of a step that is silently done twice;
--   * the lease (who holds this record and until when) and the TURN NUMBER.
--
-- THE TURN NUMBER is what makes a worker that was only paused safe. A lease
-- with an expiry stops a worker that has died; it does not stop one that
-- stalled, was taken over, and then woke up. So the turn number goes up by one
-- every time the lease is taken or taken over, in the same transaction, and
-- EVERY write to this row is made only where the stored turn number still
-- equals the writer's own, in the same statement. A write that changes no row
-- means "you have been replaced", and the worker stops at once without tidying
-- up — because tidying up is itself a change, and the new holder is the one
-- entitled to make it.
--
-- NOT RECORDED. A build with no row here reads as "not recorded": every build
-- that was pressed before this migration, and every build that has not reached
-- the merge word. That is a fact about the record, never a guess at what
-- happened.
--
-- WHAT IS NOT HERE YET, and is named rather than assumed: the publication
-- itself. The publisher does not exist, so no row ever reaches "published" or
-- "running" in this version; the record stops at "checked" and the result is
-- "publication pending". The deployment target's own lock row (the design's
-- section F) and the fixed identity of what was checked (section C) belong to
-- the later stages and are not created here.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v14.sql
-- remain frozen. The migrations runner
-- (``forge.lifecycle.migrations.apply_at_boot``) executes v1–v15 in order for
-- fresh databases and applies only v15 to existing v14 databases.

CREATE TABLE IF NOT EXISTS publication_records (
    build_id            TEXT PRIMARY KEY,
    feature_id          TEXT,
    repo                TEXT,
    -- Who gave the merge word, and when.
    decided_by          TEXT,
    decided_at          TEXT,
    -- The branch of the remote this work is aimed at, as it was recorded when
    -- the work started. Never chosen at the merge word.
    target_branch       TEXT,
    -- G: where that branch was when the merge word was acted on.
    g_commit            TEXT,
    -- The build's own tip, and J: the commit made by joining it onto G.
    build_tip           TEXT,
    j_commit            TEXT,
    -- Which attempt at the join this record is on.
    attempt             INTEGER NOT NULL DEFAULT 0,
    -- The result word, from the three-name vocabulary. Only
    -- "publication pending" is reachable while publication is switched off.
    result              TEXT,
    -- What the checks produced, and the identity of what was checked, as one
    -- JSON object written by the press.
    checked_json        TEXT,
    -- The lease and the turn number.
    lease_holder        TEXT,
    lease_expires_at    TEXT,
    turn                INTEGER NOT NULL DEFAULT 0,
    -- The lines, oldest first, as a JSON list.
    lines_json          TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT,
    updated_at          TEXT
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (15, datetime('now'));
