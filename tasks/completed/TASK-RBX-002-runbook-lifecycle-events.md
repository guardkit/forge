---
id: TASK-RBX-002
title: Runbook lifecycle events + payloads (nats-core)
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: declarative
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 50
dependencies: []
tags:
- forge
- runbook
- executor
- nats-core
- events
autobuild_state:
  current_turn: 5
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-21T21:49:40.132972'
  last_updated: '2026-06-21T22:24:57.065621'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/envelope.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/__init__.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_runbook.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      ... and 7 more issues'
    timestamp: '2026-06-21T21:49:40.132972'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/envelope.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/__init__.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_runbook.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      ... and 10 more issues'
    timestamp: '2026-06-21T21:58:17.688414'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/envelope.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/__init__.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_runbook.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      ... and 9 more issues'
    timestamp: '2026-06-21T22:06:43.381354'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 4
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/envelope.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/__init__.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_runbook.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      ... and 11 more issues'
    timestamp: '2026-06-21T22:15:19.191961'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 5
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/envelope.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/__init__.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file /Users/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_runbook.py.
      Actual: Path absent from ''git status --porcelain'' so ''git add -A'' would
      not stage it. Probes: path_exists=True; gitignore_match=probe failed; tracked=unknown.
      The ''git check-ignore'' probe itself failed (logged separately); falling back
      to critical classification to preserve the FEAT-39E1 detection floor..

      ... and 9 more issues'
    timestamp: '2026-06-21T22:19:44.764141'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Runbook lifecycle events + payloads (nats-core)

## TL;DR

Producer of the `runbook_lifecycle_events` §4 contract. Add the five new
runbook lifecycle event types to the shared `nats-core` package, each with a
typed Pydantic payload, and register them in `_EVENT_TYPE_REGISTRY` so
`payload_class_for_event_type()` resolves them. This is the cross-package
foundation the `RunbookPublisher` (TASK-RBX-003) builds on.

## Scope

Edits the **sibling package** `nats-core`
(`/Users/richardwoollcott/Projects/appmilla_github/nats-core`).

- **`src/nats_core/envelope.py`** — add a "Runbook domain (5)" block to the
  `EventType(str, Enum)`:
  - `RUNBOOK_STARTED = "runbook_started"`
  - `STEP_STARTED = "step_started"`
  - `STEP_RESULT = "step_result"`
  - `RUNBOOK_COMPLETE = "runbook_complete"`
  - `ESCALATED = "escalated"`
  Values are lowercase snake_case (valid NATS subject segments), matching the
  existing convention.
- **`src/nats_core/events.py`** — add five Pydantic payload models, mirroring
  the existing Build* payloads (shared base fields, `model_config` parity):
  - `RunbookStartedPayload` — `runbook_id`, `target`, `step_count`,
    `correlation_id`.
  - `StepStartedPayload` — `runbook_id`, `sequence_index`, `step_type`,
    `correlation_id`.
  - `StepResultPayload` — `runbook_id`, `sequence_index`, `step_type`,
    `status` (the `StepStatus` value), `result` (JSON dict | None),
    `correlation_id`.
  - `RunbookCompletePayload` — `runbook_id`, `step_count`, `correlation_id`.
  - `EscalatedPayload` — `runbook_id`, `sequence_index`, `reason`
    (`unknown_handler` | `step_failed` | `awaiting_approval`),
    `correlation_id`.
- Register each `EventType -> Payload` mapping in `_EVENT_TYPE_REGISTRY`.
- Export the new payloads from the package `__all__` / `events` re-exports as
  the Build* payloads are exported.

> Subject family for these events (constructed by the publisher in
> TASK-RBX-003, **not** here): `runbook.{event}.{runbook_id}`.

## Acceptance Criteria

- [ ] `EventType` gains exactly the five members above with the snake_case
      values listed; existing members are untouched.
- [ ] Each of the five payloads is a Pydantic model carrying `runbook_id` and
      `correlation_id` and the fields listed above; each round-trips through
      `model_dump(mode="json")` / re-parse without loss.
- [ ] `payload_class_for_event_type(EventType.STEP_RESULT)` (and the other
      four) returns the matching payload class; no `KeyError`.
- [ ] `StepResultPayload.status` accepts the five `StepStatus` values and
      rejects an out-of-set value.
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.
- [ ] Tests added in the `nats-core` test suite
      (`tests/test_runbook_events.py` or the repo's existing events test
      module), written **test-first** (TDD).

## Coach Validation

```bash
# Run from the nats-core package root
python -m pytest tests/ -q -k "runbook or event_type or registry"
```

## Implementation Notes

- Keep payload field names aligned with the persistence vocabulary
  (`runbook_id`, `sequence_index`, `step_type`, `status`) so the publisher and
  any subscriber speak the same language as the SQLite columns.
- `status` on `StepResultPayload` should reuse the `StepStatus` string values;
  if `nats-core` must not import from `forge`, mirror the closed set as a
  `Literal[...]` / local `StrEnum` whose values **equal** the forge
  `StepStatus` values — the §4 seam test in TASK-RBX-003 guards this equality.
- This task is intentionally declarative: enum members + payload models +
  registry entries. No publish logic, no I/O.
