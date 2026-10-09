-- Forge SQLite schema v18 (durable feature-router seed authority).
--
-- One admitted routing identity may issue exactly one placement request.  The
-- row is deliberately independent of planning/build lifecycle rows because a
-- single admission crosses both ledgers.  There is no lease, expiry, retry
-- counter or successor generation: UNKNOWN and FAILED are terminal.

CREATE TABLE feature_routing_seeds (
    feature_routing_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('STARTED', 'SUCCEEDED', 'UNKNOWN', 'FAILED')),
    attempt_id TEXT NOT NULL UNIQUE,
    origin_kind TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    server_id INTEGER,
    error TEXT,
    CHECK (
        (state = 'SUCCEEDED' AND completed_at IS NOT NULL AND server_id IS NOT NULL AND server_id > 0 AND error IS NULL)
        OR
        (state = 'STARTED' AND completed_at IS NULL AND server_id IS NULL AND error IS NULL)
        OR
        (state IN ('UNKNOWN', 'FAILED') AND completed_at IS NOT NULL AND server_id IS NULL AND error IS NOT NULL)
    )
) STRICT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (18, datetime('now'));
