---
id: TASK-SSH-005
title: register_shell_handlers registry wiring
status: in_review
priority: high
task_type: feature
parent_review: TASK-REV-SSH1
parent_feature: FEAT-SSH
feature_slug: shell-script-step-handlers
wave: 4
implementation_mode: task-work
complexity: 2
estimated_minutes: 35
dependencies:
- TASK-SSH-003
- TASK-SSH-004
tags:
- forge
- runbook
- shell-step
- registry
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-SSH
  base_branch: main
  started_at: '2026-06-22T16:02:08.470690'
  last_updated: '2026-06-22T16:08:13.201335'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T16:02:08.470690'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-SSH-005 — `register_shell_handlers` registry wiring

## Context

Both handlers must be reachable by the executor through the
`StepTypeRegistry.resolve(step_type)` path (see
[`src/forge/executor/registry.py`](../../../src/forge/executor/registry.py)).
This task provides the single registration entry point that wires them in under
their step-type keys.

Per the planning decision, the step-type keys are **`deploy_compose`** and
**`run_smoke_tests`** (matching the handler names — self-documenting in runbook
YAML and at the registry call site).

## Scope

In `src/forge/executor/shell_steps.py`:

```python
def register_shell_handlers(registry: StepTypeRegistry) -> None:
    registry.register("deploy_compose", deploy_compose)
    registry.register("run_smoke_tests", run_smoke_tests)
```

## Acceptance Criteria

- [ ] `register_shell_handlers(registry)` registers `deploy_compose` under the
      `"deploy_compose"` step type and `run_smoke_tests` under the
      `"run_smoke_tests"` step type.
- [ ] After calling it, `registry.resolve("deploy_compose")` and
      `registry.resolve("run_smoke_tests")` each return a non-`None` handler
      (covers key-example "deploy and smoke handlers are registered under their
      step types").
- [ ] `resolve` for an unrelated step type still returns `None` (registration is
      additive and does not shadow other handlers).
- [ ] The function and both handlers are exported from the `forge.executor`
      package surface for executor wire-up.
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Coach Validation

```bash
pytest tests/forge/executor/test_register_shell_handlers.py -v
ruff check src/forge/executor/shell_steps.py src/forge/executor/__init__.py
ruff format --check src/forge/executor/shell_steps.py src/forge/executor/__init__.py
```

## Implementation Notes

- The registry is last-write-wins; `register_shell_handlers` should be safe to
  call once during executor bootstrap. Do not add a guard against
  double-registration here — that is an executor-lifecycle concern (FEAT-RBX),
  not a handler concern.
