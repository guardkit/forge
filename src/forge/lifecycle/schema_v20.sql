-- Original request scope and immutable approved amendment records.
ALTER TABLE planning_runs ADD COLUMN requirement_amendment_paths TEXT NOT NULL DEFAULT '[]';
ALTER TABLE builds ADD COLUMN requirement_amendment_required INTEGER NOT NULL DEFAULT 0;
CREATE TABLE planning_requirement_amendments (
    correlation_id TEXT PRIMARY KEY REFERENCES planning_runs(correlation_id),
    proposal_json TEXT NOT NULL
);
CREATE TABLE build_requirement_amendments (
    build_id TEXT PRIMARY KEY REFERENCES builds(build_id),
    binding_json TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    accepted_request_id TEXT,
    accepted_responder TEXT,
    source_commit TEXT,
    apply_receipt_json TEXT
);
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (20, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
