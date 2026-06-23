---
id: TASK-SSH-003
title: deploy_compose handler + verdict mapping
status: in_review
priority: high
task_type: feature
parent_review: TASK-REV-SSH1
parent_feature: FEAT-SSH
feature_slug: shell-script-step-handlers
wave: 3
implementation_mode: task-work
complexity: 3
estimated_minutes: 45
dependencies:
- TASK-SSH-002
tags:
- forge
- runbook
- shell-step
- deploy
consumer_context:
- task: TASK-SSH-002
  consumes: STEP_OUTCOME
  framework: 'forge.executor.registry.StepHandler protocol: (step: Step) -> StepOutcome'
  driver: forge.persistence.repositories.runbook_models
  format_note: Handler returns StepOutcome(status, result); status is StepStatus.passed
    for exit 0, StepStatus.failed otherwise. result is a JSON-serializable dict carrying
    exit_code and scrubbed captured_output.
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-SSH
  base_branch: main
  started_at: '2026-06-22T15:40:44.924052'
  last_updated: '2026-06-22T15:49:48.295644'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T15:40:44.924052'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-SSH-003 — `deploy_compose` handler

## Context

A thin handler conforming to the upstream `StepHandler` Protocol
(`(step: Step) -> StepOutcome`, see
[`src/forge/executor/registry.py`](../../../src/forge/executor/registry.py)).
It reads `cwd` / `script` / `env_file` from `step.params`, delegates to the
shared core (TASK-SSH-002), and maps the script's exit status to a verdict.

> Note: the spec summary names the handler `deploy_compose(cwd, script, env_file)`,
> but the executor only ever invokes `(step: Step) -> StepOutcome`. The params
> are therefore extracted from `step.params`, not a bespoke call signature.

## Scope

In `src/forge/executor/shell_steps.py`:

```python
def deploy_compose(step: Step) -> StepOutcome: ...
```

- Extract `cwd`, `script`, `env_file` (and optional `timeout`/`output_cap`
  overrides) from `step.params`.
- Call `_run_script_step(...)`.
- Map exit status: `0 → StepStatus.passed`, non-zero → `StepStatus.failed`.
- Build `StepOutcome(status, result={"exit_code": ..., "captured_output": ...})`
  with the already-scrubbed output.

## Acceptance Criteria

- [ ] `deploy_compose` satisfies the `StepHandler` Protocol structurally
      (`(step: Step) -> StepOutcome`).
- [ ] A deploy script exiting `0` yields `StepOutcome(status=passed, …)`; the
      script runs **once** in the step's working directory (covers key-example
      "deploy step … recorded as passed").
- [ ] A deploy script exiting non-zero yields `StepOutcome(status=failed, …)`
      (covers negative "deploy … recorded as failed").
- [ ] The returned `result` dict records the exit status and the scrubbed
      captured output and is JSON-serializable (executor persists it verbatim).
- [ ] A postgres DSN in the script output does not appear in `result`, even when
      the script exits non-zero (covers "DSN scrubbed even when the script
      fails").
- [ ] Re-running the step re-invokes the script with no handler-side idempotency
      guard (covers "re-running … preserves its own idempotency").
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Coach Validation

```bash
pytest tests/forge/executor/test_deploy_compose_handler.py -v
ruff check src/forge/executor/shell_steps.py
ruff format --check src/forge/executor/shell_steps.py
```

## Implementation Notes

- Keep the handler body small — all subprocess/scrub mechanics live in the core.
  This handler is essentially param-extraction + verdict mapping + result dict.
- `StepOutcome` rejects non-terminal statuses; only ever construct it with
  `passed` / `failed` here (`awaiting_approval` is not reachable for a script
  step).
