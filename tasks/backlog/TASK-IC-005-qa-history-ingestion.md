---
id: TASK-IC-005
title: "Q&A history ingestion pipeline"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [memory, ingestion, idempotency]
complexity: 5
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 3
implementation_mode: task-work
dependencies: [TASK-IC-002]
estimated_minutes: 120
---

# Task: Q&A history ingestion pipeline

## Description

Implement the on-build-start (and post-build) scan over operator Q&A history
files. For each configured file: compute content hash; if changed, parse Q&A
entries; emit `CalibrationEvent` entities into the `forge_calibration_history`
Graphiti group with deterministic `entity_id`. Re-scanning unchanged files
produces zero writes.

Covers `@key-example history-ingestion`, `@boundary boundary-history-file-hash-change`,
`@negative negative-re-ingestion-idempotency`, `@negative partial-parse-tolerance`,
`@data-integrity deterministic-qa-identity`, `@data-integrity re-scan-zero-writes`,
`@edge-case post-build-ingestion-refresh`.

## Module: `forge/memory/qa_ingestion.py`

```python
async def ingest_qa_history(
    file_paths: Sequence[Path],
    snapshot_store: HashSnapshotStore,
) -> IngestionReport:
    """Scan each file; if content hash changed since last snapshot, parse and
    emit CalibrationEvent entities. Returns counts of (scanned, changed,
    events_emitted, partial_parses)."""
```

## Acceptance Criteria

- [ ] Content-hash (SHA-256) comparison against stored snapshot
- [ ] Unchanged files: zero parse, zero writes (`@data-integrity re-scan-zero-writes`)
- [ ] Changed files: full re-parse; emit `CalibrationEvent` per Q&A pair
- [ ] `entity_id` deterministic from `(source_file, line_range_hash)` so a
      second ingestion of the same file content produces the same entity_ids
      (`@data-integrity deterministic-qa-identity`)
- [ ] Partial-parse tolerance: malformed Q&A pairs are skipped with `partial=True`
      flag on the snapshot record; valid pairs still ingested
      (`@negative partial-parse-tolerance`)
- [ ] Snapshot store updated atomically after successful scan (no half-state
      where some files reflect new hashes and others don't)
- [ ] Invoked at build start AND after each successful build
      (`@edge-case post-build-ingestion-refresh`)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_qa_ingestion.py` — file with no change → zero writes;
      file with one new Q&A → one write; file with corrupted middle → partial
      flag + valid surrounding Q&As ingested
- [ ] `tests/unit/test_deterministic_qa_id.py` — same file content twice
      produces the same `entity_id` set
- [ ] BDD step impls for the listed `@key-example`, `@boundary`, `@negative`,
      `@data-integrity`, `@edge-case` scenarios (TASK-IC-011)

## Implementation Notes

- Use `hashlib.sha256(file_bytes).hexdigest()` for content hash.
- The snapshot store can be a JSON file under `.forge/qa_snapshots.json` or
  a SQLite table — a JSON file is fine for the sole-operator use case and
  adds no schema coupling.
- Q&A file format is operator-defined; parser should be tolerant of leading
  whitespace, BOM, and mixed line-endings. Document the expected format in
  the module docstring.
- Use `fire_and_forget_write()` from TASK-IC-002 for each event emission;
  ingestion should not block on Graphiti latency.
