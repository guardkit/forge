---
id: TASK-PSM-007
title: Crash-recovery reconciliation across all non-terminal states
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 3
implementation_mode: task-work
complexity: 7
estimated_minutes: 105
status: completed
dependencies:
- TASK-PSM-004
- TASK-PSM-005
consumer_context:
- task: TASK-PSM-002
  consumes: SCHEMA_INITIALIZED
  framework: sqlite3 (stdlib)
  driver: stdlib
  format_note: STRICT tables, WAL pragmas, builds.pending_approval_request_id column
    populated when state=PAUSED
- task: TASK-PSM-004
  consumes: STATE_TRANSITION_API
  framework: Python module import
  driver: in-process call
  format_note: "Use state_machine.transition() to mark INTERRUPTED; never write status\
    \ directly. PAUSED\u2192PAUSED is a no-op (no transition emitted)"
- task: TASK-PSM-005
  consumes: PENDING_APPROVAL_REQUEST_ID
  framework: SQLite STRICT column
  driver: sqlite3
  format_note: "builds.pending_approval_request_id is the original ApprovalRequestPayload.request_id\
    \ (UUID string). Recovery MUST reuse it verbatim when re-publishing \u2014 generating\
    \ a new UUID breaks responder correlation"
tags:
- lifecycle
- recovery
- reconciliation
- crash-safety
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:46:01.310658'
  last_updated: '2026-04-27T13:59:47.058613'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:46:01.310658'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-007: feature-rollup completed"
---

# Task: Crash-recovery reconciliation across all non-terminal states

## Description

Create `src/forge/lifecycle/recovery.py` with the boot-time reconciliation
pass. On agent runtime startup, this scans all non-terminal builds and
applies the per-state recovery action from
[`API-sqlite-schema.md §6`](../../../docs/design/contracts/API-sqlite-schema.md):

| Status on boot | Action |
|---|---|
| `QUEUED`     | No-op — JetStream will redeliver if message was unacked |
| `PREPARING`  | Mark `INTERRUPTED`, publish `pipeline.build-failed` with `recoverable=True`; JetStream redelivers → re-pickup |
| `RUNNING`    | Mark `INTERRUPTED`; build re-enters lifecycle at QUEUED via NACK on the next pull (retry-from-scratch policy) |
| `PAUSED`     | **Re-issue the original approval request** with the preserved `request_id` (concern sc_004) |
| `FINALISING` | Mark `INTERRUPTED` with a warning recorded in `error` field — PR may have been created on GitHub; operator reconciles manually |
| Terminal     | Filter out — ack any residual JetStream message; no transition emitted |

API:

- `reconcile_on_boot(persistence, publisher, approval_publisher) -> RecoveryReport` —
  the single entry point called from agent runtime startup
- `RecoveryReport` Pydantic model: counts per state + list of warnings
  (FINALISING crashes are warnings)

This module implements the highest-stakes correctness invariant in the
feature: PAUSED-recovery `request_id` idempotency (concern **sc_004**).
A missed `request_id` makes a Rich-held approval response un-correlatable
to the rehydrated PAUSED build.

## Acceptance Criteria

- [ ] `reconcile_on_boot()` runs the full per-state matrix per the table
      above
- [ ] PAUSED handling reads `pending_approval_request_id` from the build
      row and passes it verbatim to `approval_publisher.publish(...)` — the
      published payload's `request_id` matches the original
- [ ] Unit test: build paused with request_id="abc-123" → simulate crash →
      run `reconcile_on_boot()` → assert the published approval request has
      request_id="abc-123" (NOT a fresh UUID)
- [ ] PREPARING handling marks INTERRUPTED via
      `state_machine.transition(b, INTERRUPTED, error="recoverable: ...")`
      — never writes status directly
- [ ] RUNNING handling marks INTERRUPTED; the next pull consumer message
      for that feature re-enters the lifecycle (retry-from-scratch is
      per-build, not per-pipeline — see review F9 cautionary note)
- [ ] FINALISING handling marks INTERRUPTED with error message
      `"finalising-interrupted: PR may exist at <pr_url>"` if `pr_url` was
      already recorded; operator reads via `forge history`
- [ ] Recovery is **idempotent** — running it twice in a row produces no
      additional state changes (Group D crash-recovery scenarios)
- [ ] Recovery never throws partial-success; if a per-state handler fails,
      the report records the failure but other handlers still run
- [ ] `RecoveryReport` includes counts per state and a list of FINALISING
      warnings for the operator
- [ ] Unit test: every Group D crash scenario (5 scenarios — preparing,
      running, finalising, paused, terminal-no-op) passes
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
from dataclasses import dataclass, field
from forge.lifecycle import state_machine
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.supervisor import BuildState
from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.adapters.nats.approval_publisher import ApprovalPublisher


@dataclass
class RecoveryReport:
    interrupted_count: int = 0
    paused_reissued_count: int = 0
    finalising_warnings: list[str] = field(default_factory=list)
    failures: list[tuple[str, Exception]] = field(default_factory=list)


async def reconcile_on_boot(
    persistence: SqliteLifecyclePersistence,
    publisher: PipelinePublisher,
    approval_publisher: ApprovalPublisher,
) -> RecoveryReport:
    report = RecoveryReport()
    non_terminal = persistence.read_non_terminal_builds()

    for build in non_terminal:
        try:
            if build.status == BuildState.QUEUED:
                continue  # JetStream redelivery handles this
            elif build.status == BuildState.PAUSED:
                # CRITICAL: reuse the original request_id verbatim
                request_id = build.pending_approval_request_id
                assert request_id is not None, "PAUSED without request_id — schema invariant violated"
                await approval_publisher.publish(
                    build_id=build.build_id,
                    request_id=request_id,
                    # ... other fields from build / details_json ...
                )
                report.paused_reissued_count += 1
            elif build.status == BuildState.FINALISING:
                msg = (
                    f"finalising-interrupted: PR may exist at {build.pr_url}"
                    if build.pr_url
                    else "finalising-interrupted: PR creation status unknown"
                )
                t = state_machine.transition(build, BuildState.INTERRUPTED, error=msg)
                persistence.apply_transition(t)
                report.finalising_warnings.append(f"{build.build_id}: {msg}")
                report.interrupted_count += 1
            elif build.status in (BuildState.PREPARING, BuildState.RUNNING):
                t = state_machine.transition(build, BuildState.INTERRUPTED, error="recoverable: pipeline restart")
                persistence.apply_transition(t)
                report.interrupted_count += 1
        except Exception as e:
            report.failures.append((build.build_id, e))

    return report
```

## Seam Tests

```python
"""Seam test: verify PENDING_APPROVAL_REQUEST_ID contract from TASK-PSM-005."""
import pytest

from forge.lifecycle.recovery import reconcile_on_boot


@pytest.mark.seam
@pytest.mark.integration_contract("PENDING_APPROVAL_REQUEST_ID")
async def test_paused_recovery_preserves_request_id(persistence, publisher, approval_publisher):
    """Verify PAUSED recovery re-issues with the original request_id.

    Contract: builds.pending_approval_request_id is the original
              ApprovalRequestPayload.request_id (UUID string). Recovery MUST
              reuse it verbatim when re-publishing.
    Producer: TASK-PSM-005 (writes the column on PAUSED transition)
    """
    persistence.mark_paused("build-X", "abc-123-original-uuid")

    # Simulate crash (drop in-memory state, retain SQLite)
    report = await reconcile_on_boot(persistence, publisher, approval_publisher)

    # Format assertion: published request_id matches the persisted one
    published = approval_publisher.last_published()
    assert published.request_id == "abc-123-original-uuid"
    assert report.paused_reissued_count == 1


@pytest.mark.seam
@pytest.mark.integration_contract("STATE_TRANSITION_API")
def test_recovery_uses_state_machine_transitions(persistence, publisher, approval_publisher):
    """Recovery must use state_machine.transition() for INTERRUPTED writes.

    Contract: Use state_machine.transition() — never write status directly.
    Producer: TASK-PSM-004
    """
    # Static-grep assertion (Coach should run this)
    import subprocess
    result = subprocess.run(
        ["grep", "-r", "UPDATE builds SET status", "src/forge/lifecycle/recovery.py"],
        capture_output=True, text=True
    )
    assert result.stdout == "", "recovery.py must NOT issue raw status updates"
```

## Coach Validation

- `recovery.py` exists with `reconcile_on_boot` and `RecoveryReport`
- PAUSED handling reuses `pending_approval_request_id` verbatim
- All transitions go through `state_machine.transition()`
- Idempotent — re-running produces no new transitions
- Unit tests cover every row of the API-sqlite-schema.md §6 table
- Lint/format pass
