---
complexity: 3
dependencies:
- TASK-SSH-005
estimated_minutes: 50
feature_slug: shell-script-step-handlers
id: TASK-SSH-006
implementation_mode: task-work
parent_feature: FEAT-SSH
parent_review: TASK-REV-SSH1
priority: medium
status: completed
tags:
- forge
- runbook
- shell-step
- integration
- slow
task_type: testing
title: Integration test — fleet-memory smoke.sh against a throwaway target
wave: 5
---

# TASK-SSH-006 — Integration test against the real fleet-memory smoke script

## Context

Real-script proof: the `@integration @slow` scenario invokes the actual
fleet-memory smoke script through `run_smoke_tests`, mapping its exit status to
the step verdict. Confirmed present at
`~/Projects/appmilla_github/fleet-memory/deploy/nas/smoke.sh`.

**Safety (ASSUM-010):** this test MUST run only against a disposable / throwaway
target, never production. It is marker-gated so the default unit run skips it.

## Scope

Add a marker-gated pytest (e.g. `tests/forge/executor/test_shell_steps_integration.py`)
that:
1. Builds a smoke-test `Step` whose params name the fleet-memory `smoke.sh` and a
   throwaway target.
2. Resolves the handler via `register_shell_handlers` + the registry and runs it.
3. Asserts the step verdict equals the script's exit status, and that no
   credentials from the script output appear in the step result.

## Acceptance Criteria

- [ ] The test is gated behind the `@integration`/`slow` markers and is **not**
      collected by the default `pytest` unit run (e.g. excluded via
      `-m "not slow"`).
- [ ] When run, the fleet-memory smoke script executes to completion against the
      throwaway target and the step's verdict equals the script's exit status.
- [ ] No credentials from the script output appear in the step result (the scrub
      boundary holds end-to-end on real output).
- [ ] The test skips cleanly (pytest `skip`, not error) when the smoke script
      path or the throwaway target is unavailable, so CI without the target
      stays green.
- [ ] A guard prevents the test from ever pointing at a production target
      (asserts/justifies the throwaway target; never reads production creds).

## Coach Validation

```bash
# default run must NOT execute the slow integration test
pytest tests/forge/executor -m "not slow" -v
# opt-in execution (operator, against a throwaway target)
pytest tests/forge/executor/test_shell_steps_integration.py -m slow -v
```

## Implementation Notes

- Register the `slow`/`integration` markers in `pyproject.toml`/`pytest.ini` if
  not already present, to avoid `PytestUnknownMarkWarning`.
- Keep the throwaway-target setup explicit and local to the test; do not bake any
  real host into the committed test.