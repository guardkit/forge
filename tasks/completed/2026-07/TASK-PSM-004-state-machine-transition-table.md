---
id: TASK-PSM-004
title: "State machine \u2014 transition table and invariants"
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 2
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
status: completed
dependencies:
- TASK-PSM-002
consumer_context:
- task: TASK-PSM-002
  consumes: SCHEMA_INITIALIZED
  framework: sqlite3 (stdlib)
  driver: stdlib
  format_note: STRICT tables; WAL/STRICT pragmas applied per-connection; build state
    column constrained to enum values; schema_version=1 row seeded on first apply
tags:
- lifecycle
- state-machine
- transition-table
- invariants
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:06:25.400260'
  last_updated: '2026-04-27T13:16:13.985185'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:06:25.400260'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-004: feature-rollup completed"
---

# Task: State machine — transition table and invariants

## Description

Create `src/forge/lifecycle/state_machine.py`. This module is the **sole
producer of `Transition` value objects** — the only objects the persistence
layer's `apply_transition()` method accepts. CLI commands and recovery code
import this module to compose state changes; they never write
`builds.status` directly.

This is the single most important architectural rule in the feature
(concern **sc_001**). Without it, the cancel/skip handlers, queue command,
and recovery pass could each write status independently and produce illegal
states.

The module exports:

- `BuildState` enum (re-exported from `pipeline/supervisor.py`'s existing
  enum to avoid drift; this module owns the *transitions*, not the *states*)
- `Transition` Pydantic value object: `(build_id, from_state, to_state,
  occurred_at, completed_at?, error?, pr_url?, pending_approval_request_id?)`
- `transition(build, to_state, **fields) -> Transition` — validates against
  the table, sets `completed_at` automatically on terminal states, raises
  `InvalidTransitionError` if not allowed
- `InvalidTransitionError` exception
- `TRANSITION_TABLE: dict[BuildState, frozenset[BuildState]]` — the
  authoritative graph

## Allowed transitions

```
QUEUED       → PREPARING, INTERRUPTED, CANCELLED
PREPARING    → RUNNING, FAILED, INTERRUPTED, CANCELLED
RUNNING      → PAUSED, FINALISING, FAILED, INTERRUPTED, CANCELLED, SKIPPED
PAUSED       → RUNNING, FINALISING, FAILED, CANCELLED, SKIPPED
FINALISING   → COMPLETE, FAILED, INTERRUPTED
INTERRUPTED  → QUEUED, PREPARING       # re-pickup after recovery
COMPLETE     → ()  (terminal — no transitions out)
FAILED       → ()  (terminal)
CANCELLED    → ()  (terminal)
SKIPPED      → ()  (terminal)
```

Terminal states: `COMPLETE`, `FAILED`, `CANCELLED`, `SKIPPED`. Any
transition into one of these MUST set `completed_at` (Group G data
integrity invariant).

## Acceptance Criteria

- [ ] `BuildState` enum re-exported (single source of truth — the
      `pipeline/supervisor.py` enum); no parallel definition
- [ ] `TRANSITION_TABLE` defined as a frozen mapping; immutable at runtime
- [ ] `transition(build, to_state, **fields)` returns a `Transition` value
      object on success
- [ ] `transition()` raises `InvalidTransitionError(build_id, from_state,
      to_state)` for any out-of-table transition (Group C "Invalid lifecycle
      jump refused")
- [ ] Terminal-state transitions set `completed_at` automatically if not
      provided (Group G "completion time recorded")
- [ ] PAUSED transitions accept and carry forward
      `pending_approval_request_id` (per F4 — required for PAUSED-recovery
      idempotency)
- [ ] Property test: every state has a documented transition row; every
      transition in the table has at least one BDD scenario covering it
- [ ] Property test: no transition out of a terminal state succeeds
- [ ] Static-analysis test (Coach should run this): `grep -r
      "UPDATE builds SET status" src/` returns exactly the one location
      inside `persistence.apply_transition` (TASK-PSM-005 will own that
      single location)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
from datetime import datetime, UTC
from typing import Final
from pydantic import BaseModel

from forge.pipeline.supervisor import BuildState  # SOLE source

TERMINAL: Final = frozenset({
    BuildState.COMPLETE, BuildState.FAILED,
    BuildState.CANCELLED, BuildState.SKIPPED,
})

TRANSITION_TABLE: Final[dict[BuildState, frozenset[BuildState]]] = {
    BuildState.QUEUED:      frozenset({BuildState.PREPARING, BuildState.INTERRUPTED, BuildState.CANCELLED}),
    BuildState.PREPARING:   frozenset({BuildState.RUNNING, BuildState.FAILED, BuildState.INTERRUPTED, BuildState.CANCELLED}),
    BuildState.RUNNING:     frozenset({BuildState.PAUSED, BuildState.FINALISING, BuildState.FAILED, BuildState.INTERRUPTED, BuildState.CANCELLED, BuildState.SKIPPED}),
    BuildState.PAUSED:      frozenset({BuildState.RUNNING, BuildState.FINALISING, BuildState.FAILED, BuildState.CANCELLED, BuildState.SKIPPED}),
    BuildState.FINALISING:  frozenset({BuildState.COMPLETE, BuildState.FAILED, BuildState.INTERRUPTED}),
    BuildState.INTERRUPTED: frozenset({BuildState.QUEUED, BuildState.PREPARING}),
    BuildState.COMPLETE:    frozenset(),
    BuildState.FAILED:      frozenset(),
    BuildState.CANCELLED:   frozenset(),
    BuildState.SKIPPED:     frozenset(),
}


class Transition(BaseModel):
    build_id: str
    from_state: BuildState
    to_state: BuildState
    occurred_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    pr_url: str | None = None
    pending_approval_request_id: str | None = None


class InvalidTransitionError(ValueError):
    def __init__(self, build_id: str, from_state: BuildState, to_state: BuildState) -> None:
        super().__init__(
            f"Invalid transition for {build_id}: {from_state.value} -> {to_state.value}"
        )
        self.build_id = build_id
        self.from_state = from_state
        self.to_state = to_state


def transition(build, to_state: BuildState, **fields) -> Transition:
    from_state = build.status
    if to_state not in TRANSITION_TABLE.get(from_state, frozenset()):
        raise InvalidTransitionError(build.build_id, from_state, to_state)
    now = datetime.now(UTC)
    completed_at = fields.pop("completed_at", None)
    if to_state in TERMINAL and completed_at is None:
        completed_at = now
    return Transition(
        build_id=build.build_id,
        from_state=from_state,
        to_state=to_state,
        occurred_at=now,
        completed_at=completed_at,
        **fields,
    )
```

## Producer of integration contracts

- **STATE_TRANSITION_API** — consumed by TASK-PSM-005, TASK-PSM-007,
  TASK-PSM-008, TASK-PSM-011. See IMPLEMENTATION-GUIDE.md §4.

## Seam Tests

The following seam test validates the SCHEMA_INITIALIZED contract from
TASK-PSM-002. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify SCHEMA_INITIALIZED contract from TASK-PSM-002."""
import sqlite3
import pytest

from forge.lifecycle.migrations import apply_at_boot
from forge.adapters.sqlite.connect import connect_writer


@pytest.mark.seam
@pytest.mark.integration_contract("SCHEMA_INITIALIZED")
def test_schema_initialized_format(tmp_path):
    """Verify SCHEMA_INITIALIZED matches the expected format.

    Contract: STRICT tables, WAL+STRICT pragmas applied per-connection,
              schema_version=1 row seeded.
    Producer: TASK-PSM-002
    """
    db = tmp_path / "forge.db"
    cx = connect_writer(db)
    apply_at_boot(cx)

    # Format assertion: pragmas applied
    assert cx.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert cx.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # schema_version row seeded
    assert cx.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    # STRICT enforcement
    with pytest.raises(sqlite3.IntegrityError):
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, "
            "queued_at, max_turns, sdk_timeout_seconds) VALUES "
            "('b1', 'f', '/r', 'main', '/p', 'QUEUED', 'cli', 'cid', "
            "'2026-04-27T00:00:00Z', 'not-an-int', 1800)"
        )
```

## Coach Validation

- `state_machine.py` exists with `transition()`, `Transition`,
  `InvalidTransitionError`, `TRANSITION_TABLE`
- No duplicate `BuildState` definition (must be re-exported from
  `pipeline/supervisor.py`)
- Property tests cover the full transition matrix
- Static-analysis: only one `UPDATE builds SET status` location across the
  src tree (will be inside `persistence.apply_transition` in TASK-PSM-005)
