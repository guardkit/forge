---
id: TASK-SAD-009
title: correlate_outcome() writer + degraded-path synthesis
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 4
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-SAD-001
- TASK-SAD-002
- TASK-SAD-006
tags:
- dispatch
- outcome-correlation
- degraded-path
- idempotent
consumer_context:
- task: TASK-SAD-001
  consumes: CapabilityResolution
  framework: Pydantic v2
  driver: persisted via TASK-SAD-002
  format_note: Updates the existing outcome_correlated boolean flag declared in TASK-SAD-001
    / FEAT-FORGE-002. This task implements the correlate_outcome() helper referenced
    in CapabilityResolution docstrings.
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-003
  base_branch: main
  started_at: '2026-04-25T17:04:27.759582'
  last_updated: '2026-04-25T17:16:01.905166'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T17:04:27.759582'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: correlate_outcome() writer + degraded-path synthesis

## Description

Two related domain helpers that close out the dispatch lifecycle:

**1. `correlate_outcome()`** — the missing helper referenced in
`CapabilityResolution` docstrings. Updates the `outcome_correlated: bool`
flag on a resolution record after the gate decision (downstream
FEAT-FORGE-004) has been made. **Must be idempotent** so that the gating
layer can call it without coordinating with retries.

Implements scenario A.outcome-correlation.

**2. Degraded-path synthesis** — when no specialist resolves, produce a
synthetic stage outcome that the reasoning loop consumes as a normal
"failed stage" outcome. The reasoning loop must not need a special branch
for degraded results.

Implements scenarios:
- C.unresolved-capability (degraded result fed to reasoning loop)
- C.degraded-status-exclusion (specialists in degraded state never selected)
- E.bus-disconnect (bus unreachable → degraded outcome surfaced)
- E.registry-outage (registry unreadable → degraded with stale-snapshot flag)

## Interface

```python
# src/forge/dispatch/outcome.py
from typing import Literal, Optional
from forge.discovery.models import CapabilityResolution
from forge.dispatch.models import Degraded


def correlate_outcome(
    resolution_id: str,
    gate_decision_id: str,
    *,
    db_writer: "SqliteHistoryWriter",
) -> CapabilityResolution:
    """Link a resolution record to its downstream gate decision.

    Idempotent: calling twice with the same args is a no-op.
    Sets outcome_correlated=True and records the gate_decision_id.

    Returns the (possibly already-updated) resolution record.
    """
    ...


def synthesize_degraded(
    *,
    capability: str,
    reason: Literal[
        "no_specialist_resolvable",
        "all_resolvable_specialists_degraded",
        "bus_disconnected",
        "registry_unreadable_stale_snapshot",
    ],
    snapshot_stale: bool = False,
    attempt_no: int = 1,
) -> Degraded:
    """Synthesise a Degraded outcome for the reasoning loop.

    The reasoning loop consumes Degraded as a regular stage outcome.
    `snapshot_stale=True` is set when resolving against a stale cache
    snapshot (registry-outage path).
    """
    ...
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/outcome.py` defines `correlate_outcome()` and
      `synthesize_degraded()`.
- [ ] `correlate_outcome()` is idempotent: two consecutive calls with
      identical args produce equal records and do not write twice.
- [ ] Test (idempotency): mock the db_writer; assert exactly one
      UPDATE statement is issued across two consecutive calls.
- [ ] Test (A.outcome-correlation): after `correlate_outcome()`, the
      resolution record has `outcome_correlated=True` and references the
      gate_decision_id.
- [ ] Test (C.unresolved-capability): `synthesize_degraded(reason="no_specialist_resolvable")`
      produces a `Degraded` whose `reason` field carries the input value.
- [ ] Test (E.registry-outage-stale-snapshot): `snapshot_stale=True`
      produces a `Degraded` whose `reason` indicates staleness; the
      reasoning loop is informed via the outcome.
- [ ] Test (E.bus-disconnect): when the orchestrator catches a bus
      disconnect (transport-layer error), it calls
      `synthesize_degraded(reason="bus_disconnected")` rather than
      hanging.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Tests

```python
"""Seam test: verify CapabilityResolution outcome_correlated contract."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("CapabilityResolution")
def test_correlate_outcome_idempotent(db_writer):
    """Verify correlate_outcome() is idempotent on outcome_correlated.

    Contract: CapabilityResolution.outcome_correlated from TASK-SAD-001.
    Producer: TASK-SAD-001 (declared the field)
    """
    from forge.dispatch.outcome import correlate_outcome
    # ... seed a resolution row ...
    r1 = correlate_outcome("res-001", "gate-A", db_writer=db_writer)
    r2 = correlate_outcome("res-001", "gate-A", db_writer=db_writer)
    assert r1 == r2
    assert db_writer.update_count_for("res-001") == 1
```

## Implementation Notes

- Idempotency is implemented at the persistence layer: a row that already
  has `outcome_correlated=True` and `gate_decision_id=<id>` is not
  re-written. The check is via a SELECT-then-UPDATE in a transaction.
- Do NOT make `correlate_outcome()` async unless the persistence layer
  forces it. A sync helper composes more cleanly with the gating layer.
- The `synthesize_degraded` function is pure (no I/O). The persistence
  side-effect happens at the orchestrator's normal `persist_resolution`
  step earlier in the dispatch lifecycle (before degraded synthesis).
- Edge case: if `correlate_outcome()` is called for a non-existent
  resolution_id, raise `KeyError`. Do NOT silently no-op — that would
  hide bugs in the gating layer's call site.
