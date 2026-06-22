---
id: TASK-RBX-001
title: Step-type registry + handler protocol
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: feature
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 60
dependencies: []
tags:
- forge
- runbook
- executor
- registry
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-21T21:49:40.133311'
  last_updated: '2026-06-21T21:59:49.381150'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-21T21:49:40.133311'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Step-type registry + handler protocol

## TL;DR

Create the dispatch substrate the executor reads from: a `StepHandler`
protocol, a `StepOutcome` value object, and a `StepTypeRegistry` that maps
`step_type -> handler`. The executor (TASK-RBX-004) holds **no** knowledge of
step internals — it only ever resolves a handler by its `step_type` key. Also
register the feature's pytest marks so the BDD suite binds without warnings.

## Scope

New module `src/forge/executor/` (`__init__.py`, `registry.py`).

- **`StepOutcome`** — the value a handler returns. Carries
  `status: StepStatus` (one of `passed` / `failed` / `awaiting_approval`, the
  three terminal outcomes per **ASSUM-008**) and `result: dict | None` (JSON-
  serialisable; persisted verbatim by `update_step_status`). Frozen dataclass.
- **`StepHandler`** — a `typing.Protocol` with a single call signature
  `(step: Step) -> StepOutcome`. The executor never inspects handler internals
  (registry indirection only).
- **`StepTypeRegistry`**
  - `register(step_type: str, handler: StepHandler) -> None`.
  - `resolve(step_type: str) -> StepHandler | None` — returns `None` for an
    unregistered type (the executor turns `None` into an escalation, **never**
    a crash — ASSUM-002). `step_type` is treated **only** as a lookup key;
    nothing in this module ever evaluates, formats, or executes it.
  - Open-closed: a brand-new step type is supported purely by `register(...)`
    — no edit to the registry or executor (Edge "A step type the executor has
    never seen…").
- Register pytest marks `runbook-executor` (feature tag) and `runbook_executor`
  (slug) in `pyproject.toml` `[tool.pytest.ini_options].markers` so the
  `.feature` tags reflected by pytest-bdd do not warn.

`StepStatus` and `Step` are imported from FEAT-RSP
(`forge.persistence.repositories.runbook_models`). Do **not** redefine them.

## Acceptance Criteria

- [ ] `StepTypeRegistry.resolve` returns the handler registered for a given
      `step_type` (Key Example "Each step is dispatched to the handler
      registered for its step type").
- [ ] `resolve` returns `None` for a `step_type` with no registered handler
      (drives the escalation path; no exception raised here).
- [ ] A previously-unknown `step_type` becomes dispatchable purely by calling
      `register` — no other code change (Edge "A step type the executor has
      never seen is handled by registering a handler").
- [ ] `StepOutcome` only admits `status` values in
      `{passed, failed, awaiting_approval}`; constructing it with any other
      `StepStatus` raises `ValueError`.
- [ ] `StepHandler` is a `Protocol`; an in-memory fake handler satisfies it
      structurally with no inheritance (unit fakes need no broker/subprocess).
- [ ] `runbook-executor` / `runbook_executor` marks are registered in
      `pyproject.toml`; `pytest -m runbook_executor` collects without
      unknown-mark warnings.
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.
- [ ] Tests added to `tests/forge/executor/test_registry.py`, written
      **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/executor/test_registry.py -q
python -m pytest --collect-only -m runbook_executor -q
```

## Implementation Notes

- Keep this module dependency-free beyond the FEAT-RSP models — no NATS, no
  SQLite, no subprocess. It is pure data + a dict.
- `StepOutcome.result` must be JSON-serialisable because TASK-RBX-004 hands it
  straight to `update_step_status(..., result=outcome.result)`.
- A handler that *raises* is **not** this module's concern — the executor
  (TASK-RBX-004) contains the exception and maps it to `failed`. Handlers are
  free to return `StepOutcome(status=failed, ...)` for an expected failure.
