-- First response wins durably, including decisions that grant no authority.
CREATE TABLE build_requirement_amendment_responses (
    build_id TEXT NOT NULL REFERENCES build_requirement_amendments(build_id),
    request_id TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'defer')),
    responder TEXT NOT NULL,
    PRIMARY KEY (build_id, request_id)
);
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (21, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
