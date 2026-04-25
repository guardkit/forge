---
id: TASK-SAD-002
title: Resolution-record persistence + sensitive-parameter scrub
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 1
implementation_mode: task-work
complexity: 5
dependencies: []
tags:
- dispatch
- persistence
- security
- sensitive-params
- sqlite
consumer_context:
- task: TASK-SAD-001
  consumes: CapabilityResolution
  framework: Pydantic v2
  driver: sqlite via existing forge_pipeline_history schema
  format_note: Read-only consumer of the model; this task implements the durable write
    path. retry_of field added in TASK-SAD-001 must persist.
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-003
  base_branch: main
  started_at: '2026-04-25T16:36:39.042339'
  last_updated: '2026-04-25T16:48:47.145967'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T16:36:39.042339'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Resolution-record persistence + sensitive-parameter scrub

## Description

Implement the durable write path for `CapabilityResolution` records into the
existing FEAT-FORGE-001 SQLite history table, and the **schema-driven**
sensitive-parameter scrub. Sensitive-parameter hygiene must be enforced by the
persistence layer (not by orchestrator-side discipline) so that "forget once"
bugs are impossible.

Implements scenarios E.sensitive-parameter-hygiene and the
write-before-send invariant of D.write-before-send-invariant.

## Schema additions

```python
# src/forge/dispatch/persistence.py
from forge.discovery.models import CapabilityResolution

class DispatchParameter(BaseModel):
    name: str
    value: str
    sensitive: bool = False        # KEY FIELD — drives scrub

def persist_resolution(
    resolution: CapabilityResolution,
    parameters: list[DispatchParameter],
    *,
    db_writer: SqliteHistoryWriter,
) -> None:
    """Write the resolution record + non-sensitive parameters atomically.

    Sensitive parameter values are dropped at the persistence boundary.
    The non-sensitive *names* of dropped parameters are recorded so the
    audit trail shows that dispatch carried sensitive data without
    revealing values.
    """
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/persistence.py` defines `DispatchParameter` with a
      `sensitive: bool` field.
- [ ] `persist_resolution()` writes the resolution to the existing
      FEAT-FORGE-001 SQLite history table (verify schema is extensible — no
      schema change should be required; if one is, add an Alembic-style
      migration in this task).
- [ ] Sensitive parameters are scrubbed at the persistence boundary: only
      `(name, sensitive=True)` is recorded; `value` is **not** persisted in
      any column.
- [ ] Non-sensitive parameters are persisted in full.
- [ ] Atomic write: either the resolution + all non-sensitive parameters
      land, or none do (use the existing transaction context).
- [ ] Test: passing a `DispatchParameter(name="api_token", value="secret",
      sensitive=True)` results in a row whose `value` column is NULL or
      missing.
- [ ] Test: pipeline-history view of the dispatch shows non-sensitive fields
      and the *names* of sensitive fields, never the values.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Tests

The following seam test validates the integration contract with the producer
task. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify CapabilityResolution contract from TASK-SAD-001."""
import pytest
from forge.discovery.models import CapabilityResolution
from forge.dispatch.persistence import persist_resolution, DispatchParameter


@pytest.mark.seam
@pytest.mark.integration_contract("CapabilityResolution")
def test_capability_resolution_persistence_round_trip(db_writer):
    """Verify CapabilityResolution persists with retry_of field round-trip.

    Contract: CapabilityResolution model from TASK-SAD-001 (Pydantic v2,
    SQLite-backed via existing forge_pipeline_history). retry_of field
    must round-trip.
    Producer: TASK-SAD-001
    """
    res = CapabilityResolution(
        resolution_id="res-001",
        match_source="exact_tool",
        matched_agent_id="po-agent",
        competing_agents=[],
        retry_of=None,
    )
    persist_resolution(res, parameters=[], db_writer=db_writer)
    rows = db_writer.read_resolutions()
    assert len(rows) == 1
    assert rows[0].retry_of is None


@pytest.mark.seam
@pytest.mark.integration_contract("CapabilityResolution")
def test_sensitive_parameter_value_not_persisted(db_writer):
    """Sensitive parameter values must not appear in the persistence layer."""
    res = CapabilityResolution(
        resolution_id="res-002", match_source="exact_tool",
        matched_agent_id="po-agent", competing_agents=[],
    )
    secret = DispatchParameter(name="api_token", value="VERY-SECRET", sensitive=True)
    public = DispatchParameter(name="ticket_id", value="JIRA-123", sensitive=False)
    persist_resolution(res, parameters=[secret, public], db_writer=db_writer)

    raw_rows = db_writer.dump_all_parameter_rows()
    assert "VERY-SECRET" not in str(raw_rows)
    assert "JIRA-123" in str(raw_rows)
    # The *name* of the sensitive parameter should still be recorded for audit
    assert any("api_token" in str(r) for r in raw_rows)
```

## Implementation Notes

- The existing FEAT-FORGE-001 history writer should be extended via
  composition, not by editing FEAT-FORGE-001 internals. If the history table
  cannot accept resolution records, add a sibling table
  `forge_capability_resolutions` and join via `correlation_id`.
- The scrub MUST happen inside `persist_resolution()`. Do not accept a
  pre-scrubbed list — that re-introduces the "forget once" failure mode.
- Confirm the `retry_of` field added by TASK-SAD-001 is persisted (single
  nullable string column).
