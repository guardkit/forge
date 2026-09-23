-- Forge SQLite schema v16 (the deployment lock, and the target's own counter).
--
-- Additive migration that adds ONE new table, ``deployment_targets``. No
-- existing table, column or index is touched.
--
-- WHY THIS TABLE EXISTS (23 September 2026; the one-true-copy design pass,
-- item 1, third revision F and fifth revision I). The reservation the factory
-- had says of itself that it is "correct within a single forge process", so it
-- cannot say who owns a deployment target across two coordinators, or across a
-- coordinator that was restarted. And a build's own turn number cannot be
-- compared across builds: a build picked up twice is on turn 3 and a fresh
-- build starts at turn 1, so the higher number belongs to the older work.
--
-- So there are TWO counters and they count different things:
--
--   * the BUILD's turn number lives on ``publication_records`` and is used
--     only for that build's own record and its requests to the publisher;
--   * the TARGET's deployment counter lives here, one row per deployment
--     target, and goes up by one EVERY time this lock is granted or taken
--     over, by any build. Granting records the counter together with the build
--     it was granted to, and everything downstream — the executor that runs
--     the project's deploy step — is enforced on THIS number, bound to that
--     build.
--
-- THE LOCK IS HELD ACROSS THE WHOLE DECISION: from before reading what is
-- running, through the only-forwards rule, the project's own deploy step, and
-- the confirmation of what is now running. It is released only after that
-- confirmation is recorded, or when it expires. A takeover cancels the
-- previous holder: every write here is conditional on the stored counter still
-- equalling the writer's own, in the same statement, so the previous holder's
-- next write changes no row.
--
-- WHAT IS RUNNING (R) lives here too, because the only-forwards rule needs it
-- and because it must survive a coordinator restart: the commit that was
-- deployed at the last deploy, and the identity the running thing REPORTED
-- when that deploy was confirmed. Both are text. Nothing in this factory knows
-- what an identity is — that belongs to the project, whose own deploy step is
-- handed one and reports one back.
--
-- A TARGET WITH NO ROW is a target nothing has ever been deployed to. It reads
-- as "nothing is running", which is a fact about the record and never a guess.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v15.sql
-- remain frozen.

CREATE TABLE IF NOT EXISTS deployment_targets (
    -- WHICH deployment target. Text the caller composes; central code never
    -- parses it. Today it is the project and the deploy profile's environment
    -- name, which is what a project's own deploy step changes.
    target              TEXT PRIMARY KEY,
    -- The target's OWN deployment counter: +1 on every grant or takeover, by
    -- any build. Never goes down.
    counter             INTEGER NOT NULL DEFAULT 0,
    -- Who holds it now, and with which turn of their own record.
    holder_build        TEXT,
    holder_turn         INTEGER,
    holder_name         TEXT,
    granted_at          TEXT,
    expires_at          TEXT,
    -- R — what is running on this target, as far as anybody here knows: the
    -- commit recorded at the last deploy, and the identity the running thing
    -- reported when that deploy was confirmed.
    running_commit      TEXT,
    running_identity    TEXT,
    running_build       TEXT,
    running_at          TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (16, datetime('now'));
