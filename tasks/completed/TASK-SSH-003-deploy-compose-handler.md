---
complexity: 3
consumer_context:
- consumes: STEP_OUTCOME
  driver: forge.persistence.repositories.runbook_models
  format_note: Handler returns StepOutcome(status, result); status is StepStatus.passed
    for exit 0, StepStatus.failed otherwise. result is a JSON-serializable dict carrying
    exit_code and scrubbed captured_output.
  framework: 'forge.executor.registry.StepHandler protocol: (step: Step) -> StepOutcome'
  task: TASK-SSH-002
dependencies:
- TASK-SSH-002
estimated_minutes: 45
feature_slug: shell-script-step-handlers
id: TASK-SSH-003
implementation_mode: task-work
parent_feature: FEAT-SSH
parent_review: TASK-REV-SSH1
priority: high
status: completed
tags:
- forge
- runbook
- shell-step
- deploy
task_type: feature
title: deploy_compose handler + verdict mapping
wave: 3
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