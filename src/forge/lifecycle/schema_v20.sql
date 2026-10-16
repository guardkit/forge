-- Forge SQLite schema v20 (one revision counter per merge card, 16 October 2026).
--
-- Additive migration that adds ONE new table, ``merge_card_revisions``. No
-- existing table, column or index is touched.
--
-- WHY THIS TABLE EXISTS. A merge card with a review preview is now kept true
-- after it is posted: Forge describes the card from its ledger (the latest
-- offer row, the build's newest preview lease, any decision row) and Jarvis
-- edits the Slack message to match. Lease rows change in place, so no row id
-- can say which description is newer. Each card therefore has one counter,
-- raised by one in the same transaction as every change that can alter the
-- card: an offer or amended offer row, a decision row, a refused approval, and
-- any insert or update of that build's preview lease. Every description carries
-- the counter, and Jarvis never applies one older than the last it applied, so
-- a late "ready" can never overwrite a newer "ended" and a decided card is
-- never reopened.
--
-- One row per build (the card belongs to the build's offer). A build with no
-- row has revision 0.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql … schema_v19.sql
-- remain frozen.

CREATE TABLE IF NOT EXISTS merge_card_revisions (
    build_id    TEXT PRIMARY KEY,
    revision    INTEGER NOT NULL CHECK (revision >= 0),
    updated_at  TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (20, datetime('now'));
