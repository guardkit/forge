---
id: TASK-FRR-PEB-006
title: "Pause/resume canonicalisation \u2014 bridge owns both, FW10-010 resume site\
  \ amended out"
status: in_review
created: 2026-05-06 00:00:00+00:00
updated: 2026-05-06 00:00:00+00:00
priority: high
task_type: refactor
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 3
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies:
- TASK-FRR-PEB-005
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- pause-resume
- fw10-010-amendment
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-PEBR
  base_branch: main
  started_at: '2026-05-07T14:46:54.122095'
  last_updated: '2026-05-07T15:20:45.597915'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `the stack-specific Phase-3 specialist` (Implementation)\n- Plan audit\
      \ detected high-severity discrepancies \u2014 2 missing file(s): src/forge/cli/_approval_subscriber.py,\
      \ tests/forge/test_approval_subscriber.py"
    timestamp: '2026-05-07T14:46:54.122095'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `the stack-specific Phase-3 specialist` (Implementation)\n- Plan audit\
      \ detected high-severity discrepancies \u2014 2 missing file(s): src/forge/cli/_approval_subscriber.py,\
      \ tests/forge/test_approval_subscriber.py"
    timestamp: '2026-05-07T14:56:34.043047'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-05-07T15:04:24.535740'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Pause/resume canonicalisation — bridge owns both, FW10-010 resume site amended out

## TL;DR

Make the lifecycle bridge the canonical site for both `build-paused` and
`build-resumed` envelope emission (Q4 sub-option (a) per scoping doc).
**Amend `approval_subscriber.py` to skip its own `build-resumed` emit
when a bridge is wired**. This folds FW10-010 into F010M's wave-plan
rather than allowing two emit sites to coexist.

This task **changes FW10-010's design**. FW10-010's existing test suite
must be amended (not deleted) to assert the new "skip if bridge wired"
behaviour.

## Locks BDD scenarios

- @edge-case @regression `A mandatory-approval pause inside the sidecar
  produces exactly one build-paused envelope` (ASSUM-005)
- @edge-case @regression `An approval response for a paused build
  produces exactly one build-resumed envelope`

## Acceptance criteria

- AC-1: The bridge's translator (T3) maps `awaiting_approval` SSE events
  to `BuildPausedPayload` and `running_wave-after-awaiting_approval`
  events to `BuildResumedPayload`. T3's translator is extended; no new
  translator class.
- AC-2: `src/forge/adapters/nats/approval_subscriber.py` (or wherever FW10-010's
  resume emit lives — verify path during implementation) is amended:
  before publishing `build-resumed`, it queries the
  `lifecycle_bridge_registry` for the `(feature_id, correlation_id)`;
  if the registry has an active entry, the subscriber skips its emit
  and logs at INFO that the bridge is canonical.
- AC-3: When no bridge is wired (test path), the existing FW10-010
  resume emit continues to fire — preserving backward compatibility
  for tests that don't exercise the bridge.
- AC-4: Pause/resume scenarios produce exactly one envelope per
  transition; correlation-id is threaded through both.
- AC-5: FW10-010's existing tests are updated to cover both paths
  (bridge-wired skips, bridge-absent emits). No FW10-010 test is
  deleted; the file is annotated with a header comment referencing
  TASK-FRR-PEB-006 as the amendment task.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Pause emit test: SSE stream emits `awaiting_approval` →
  exactly one `build-paused` envelope; `BuildPausedPayload` carries
  inbound correlation-id.
- Resume emit test (bridge wired): SSE stream emits
  `running_wave-after-awaiting_approval` → exactly one `build-resumed`
  from the bridge; FW10-010's subscriber path **does not emit**.
- Resume emit test (bridge absent): FW10-010's subscriber path emits
  exactly one `build-resumed` (existing behaviour preserved).
- FW10-010 regression suite passes (with amendments).

## Files to Create

- `tests/forge/lifecycle_bridge/test_pause_resume.py`

## Files to Modify

- `src/forge/lifecycle_bridge/translation.py`
- `src/forge/adapters/nats/approval_subscriber.py`
- `tests/forge/adapters/test_approval_subscriber.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/translation.py` (extend);
  `src/forge/adapters/nats/approval_subscriber.py` (amend);
  `tests/forge/adapters/test_approval_subscriber.py` (update, do not delete).
- Reference: FW10-010 task file in `tasks/completed/`.
- The "bridge wired" check is a registry lookup: if
  `BridgeRegistry.get(feature_id)` returns a non-None entry, bridge is
  active and subscriber skips emit.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_pause_resume.py -x -v
PYTHONPATH=src python -m pytest tests/forge/adapters/test_approval_subscriber.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/lifecycle_bridge/translation.py src/forge/adapters/nats/approval_subscriber.py
```
