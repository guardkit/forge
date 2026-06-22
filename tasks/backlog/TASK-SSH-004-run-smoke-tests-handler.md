---
id: TASK-SSH-004
title: run_smoke_tests handler + verdict mapping
status: backlog
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
  - smoke-test
consumer_context:
  - task: TASK-SSH-002
    consumes: STEP_OUTCOME
    framework: "forge.executor.registry.StepHandler protocol: (step: Step) -> StepOutcome"
    driver: "forge.persistence.repositories.runbook_models"
    format_note: "Handler returns StepOutcome(status, result); for run_smoke_tests the script exit status IS the verdict (0 -> passed, non-zero -> failed). result is a JSON-serializable dict carrying exit_code and scrubbed captured_output."
---

# TASK-SSH-004 — `run_smoke_tests` handler

## Context

Twin of TASK-SSH-003 for the smoke-test step type. Same `StepHandler` Protocol,
same shared core (TASK-SSH-002). The only semantic distinction the spec draws is
that for smoke tests **the script's exit status *is* the verdict** — which, in
the closed `passed`/`failed` vocabulary, is the same `0 → passed`, non-zero →
`failed` mapping. The distinction is documentary, not behavioural; it is captured
here so the boundary scenario outline (exit 0/1/137) is satisfied.

## Scope

In `src/forge/executor/shell_steps.py`:

```python
def run_smoke_tests(step: Step) -> StepOutcome: ...
```

- Extract `cwd`, `script`, `env_file` (and optional overrides) from
  `step.params`.
- Call `_run_script_step(...)`.
- The exit status is the verdict: `0 → passed`, every non-zero → `failed`.
- Build `StepOutcome` with the scrubbed `result` dict.

## Acceptance Criteria

- [ ] `run_smoke_tests` satisfies the `StepHandler` Protocol structurally.
- [ ] A smoke script exiting `0` yields `StepOutcome(status=passed, …)` and the
      exit status is the verdict (covers key-example "smoke-test … recorded as
      passed").
- [ ] A smoke script exiting non-zero yields `StepOutcome(status=failed, …)`,
      the failing exit status being the verdict (covers key-example "smoke-test …
      recorded as failed").
- [ ] The verdict follows the exit status across the boundary outline
      (`0 → passed`, `1 → failed`, `137 → failed`).
- [ ] A password in the script output (stdout or stderr) does not appear in the
      `result` dict — it is replaced by the redaction marker (covers
      "password … scrubbed before … published").
- [ ] A smoke step whose `env_file` path does not exist is **not** rejected
      before running; the verdict is the script's own exit status (covers
      ASSUM-013 deferred behaviour).
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Coach Validation

```bash
pytest tests/forge/executor/test_run_smoke_tests_handler.py -v
ruff check src/forge/executor/shell_steps.py
ruff format --check src/forge/executor/shell_steps.py
```

## Implementation Notes

- Consider factoring the shared param-extraction + verdict-mapping into a small
  internal helper used by both handlers, so `deploy_compose` and
  `run_smoke_tests` differ only by name/registration. Keep it private to the
  module.
