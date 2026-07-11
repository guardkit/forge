---
complexity: 7
consumer_context:
- consumes: SCHEMA_INITIALIZED
  driver: stdlib
  format_note: STRICT tables; WAL+STRICT pragmas applied per-connection; UNIQUE(feature_id,
    correlation_id) index for duplicate detection
  framework: sqlite3 (stdlib)
  task: TASK-PSM-002
- consumes: STATE_TRANSITION_API
  driver: in-process call
  format_note: apply_transition() consumes only Transition value objects produced
    by state_machine.transition(); raw status kwargs are forbidden by API design
  framework: Python module import
  task: TASK-PSM-004
dependencies:
- TASK-PSM-002
- TASK-PSM-004
estimated_minutes: 105
feature_id: FEAT-FORGE-001
id: TASK-PSM-005
implementation_mode: task-work
parent_review: TASK-REV-3EEE
status: completed
tags:
- lifecycle
- persistence
- protocols
- cli-steering
task_type: feature
title: Persistence — concrete cli_steering Protocol implementations
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-005: ['rollup: feature FEAT-FORGE-001 status=completed']"
---

# Task: Persistence — concrete cli_steering Protocol implementations

## Description

Create `src/forge/lifecycle/persistence.py` exporting concrete SQLite-backed
implementations of every Protocol seam already defined in
`src/forge/pipeline/cli_steering.py`:

- `SqliteBuildSnapshotReader` — reads current build state for cancel/skip
- `SqliteBuildCanceller` — terminal CANCELLED transition + record reason
- `SqliteBuildResumer` — re-enter RUNNING from PAUSED
- `SqliteStageLogReader` — reads stage_log entries for status `--full`
- `SqliteStageSkipRecorder` — appends a SKIPPED stage entry
- `SqlitePauseRejectResolver` — synthesises the cancel-on-paused
  ApprovalResponsePayload(reject) for `synthetic_response_injector`
- (`AsyncTaskCanceller`, `AsyncTaskUpdater` — passthroughs to the
  in-process autobuild registry; no SQLite involvement, but they live in
  this module for cohesion)

Plus the public write API used by the agent runtime:

- `record_pending_build(payload: BuildQueuedPayload) -> None`
- `apply_transition(transition: Transition) -> None` ← **the SOLE function
  in the entire src tree that emits `UPDATE builds SET status = ?`**
- `record_stage(entry: StageLogEntry) -> None`
- `mark_paused(build_id: str, request_id: str) -> None` (writes
  `pending_approval_request_id`; also calls
  `apply_transition(state_machine.transition(b, PAUSED,
  pending_approval_request_id=request_id))`)
- Read API used by CLI:
  - `read_status(feature_id: str | None = None) -> list[BuildStatusView]`
  - `read_history(limit: int = 50, feature_id: str | None = None) -> list[BuildRow]`
  - `read_stages(build_id: str) -> list[StageLogEntry]`
  - `exists_active_build(feature_id: str) -> bool` (Group C
    "active in-flight duplicate" check)
  - `find_active_or_recent(feature_id: str) -> Build | None` (Group C
    "cancel of unknown" check)

This module enforces concern **sc_001** (state-mutation exclusivity) at the
implementation level: no method takes a raw `status` kwarg; everything goes
through `apply_transition(Transition)`.

It also produces the `pending_approval_request_id` value at PAUSED entry —
the producer side of the `PENDING_APPROVAL_REQUEST_ID` integration contract
consumed by TASK-PSM-007.

## Acceptance Criteria

- [ ] All seven Protocol classes are concrete and pass `isinstance` checks
      against the runtime_checkable Protocols defined in
      `pipeline/cli_steering.py`
- [ ] `apply_transition(t: Transition) -> None` is the ONLY public method
      that issues `UPDATE builds SET status = ?` (verified by static analysis
      grep)
- [ ] `apply_transition` rejects raw kwargs — its signature accepts only
      `Transition` objects (Pydantic-validated)
- [ ] `mark_paused(build_id, request_id)` writes `pending_approval_request_id`
      atomically with the state transition (single `UPDATE` statement)
- [ ] `record_pending_build(payload)` writes the build row and translates
      `IntegrityError` on the UNIQUE `(feature_id, correlation_id)` index
      into a domain-level `DuplicateBuildError` (Group B "duplicate refused")
- [ ] `read_status` returns active builds (`QUEUED`, `PREPARING`, `RUNNING`,
      `PAUSED`, `FINALISING`) plus the last 5 terminal builds, sorted by
      `queued_at DESC` (per `API-cli.md §4.2`)
- [ ] `read_history(limit=50)` returns at most 50 most recent builds; clamps
      `limit` to a max of e.g. 1000 to prevent unbounded queries
- [ ] `exists_active_build(feature_id)` returns True iff any build for that
      feature is in {QUEUED, PREPARING, RUNNING, PAUSED, FINALISING}
- [ ] All write paths use `BEGIN IMMEDIATE` to acquire the write lock
      atomically (per F7 in the review report — avoids busy-retry under
      concurrent queue commands)
- [ ] All read paths use `read_only_connect()` from
      `forge.adapters.sqlite.connect`
- [ ] Unit tests cover each Protocol method against a real in-memory SQLite
      database
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Producer of integration contracts

- **PERSISTENCE_PROTOCOLS** — consumed by TASK-PSM-008, TASK-PSM-009,
  TASK-PSM-010, TASK-PSM-011. See IMPLEMENTATION-GUIDE.md §4.
- **PENDING_APPROVAL_REQUEST_ID** — consumed by TASK-PSM-007. See
  IMPLEMENTATION-GUIDE.md §4.

## Seam Tests

```python
"""Seam test: verify SCHEMA_INITIALIZED contract from TASK-PSM-002."""
import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.migrations import apply_at_boot
from forge.lifecycle.persistence import SqliteLifecyclePersistence


@pytest.mark.seam
@pytest.mark.integration_contract("SCHEMA_INITIALIZED")
def test_persistence_uses_strict_schema(tmp_path):
    """Persistence must rely on the STRICT schema and pragmas from TASK-PSM-002."""
    db = tmp_path / "forge.db"
    cx = connect_writer(db)
    apply_at_boot(cx)
    persistence = SqliteLifecyclePersistence(connection=cx)
    # Format assertion: PRAGMA values match contract
    assert cx.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    # Format assertion: STRICT rejects type-mismatch
    # (verified separately in TASK-PSM-002 seam test)
```

```python
"""Seam test: verify STATE_TRANSITION_API contract from TASK-PSM-004."""
import pytest

from forge.lifecycle import state_machine
from forge.lifecycle.persistence import SqliteLifecyclePersistence


@pytest.mark.seam
@pytest.mark.integration_contract("STATE_TRANSITION_API")
def test_persistence_consumes_only_transitions(tmp_path):
    """apply_transition must consume only Transition value objects.

    Contract: persistence.apply_transition() takes Transition, not raw kwargs;
              state_machine.transition() is the sole producer.
    Producer: TASK-PSM-004
    """
    persistence = ...  # constructed with seeded build
    build = ...
    t = state_machine.transition(build, state_machine.BuildState.PREPARING)
    persistence.apply_transition(t)  # must succeed
    # Format assertion: signature rejects raw status
    import inspect
    sig = inspect.signature(persistence.apply_transition)
    assert list(sig.parameters) == ["transition"]
    assert sig.parameters["transition"].annotation is state_machine.Transition
```

## Coach Validation

- `persistence.py` exports all seven Sqlite* classes
- Each class implements its respective Protocol from
  `pipeline/cli_steering.py` (verified via `isinstance`)
- `apply_transition(Transition)` is the only writer of `builds.status`
- `mark_paused` writes `pending_approval_request_id` atomically
- BEGIN IMMEDIATE used for write transactions
- Unit tests pass against in-memory SQLite