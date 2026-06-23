---
id: TASK-SSH-004
title: run_smoke_tests handler + verdict mapping
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
- smoke-test
consumer_context:
- task: TASK-SSH-002
  consumes: STEP_OUTCOME
  framework: 'forge.executor.registry.StepHandler protocol: (step: Step) -> StepOutcome'
  driver: forge.persistence.repositories.runbook_models
  format_note: Handler returns StepOutcome(status, result); for run_smoke_tests the
    script exit status IS the verdict (0 -> passed, non-zero -> failed). result is
    a JSON-serializable dict carrying exit_code and scrubbed captured_output.
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-SSH
  base_branch: main
  started_at: '2026-06-22T15:40:44.925425'
  last_updated: '2026-06-22T16:02:08.429173'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Independent test verification failed (independent_tests.tests_passed
      = false) despite Player-reported tests passing. The independent test raw_output
      shows ''\nI''ll run that test command for you.\n'' which does not match expected
      pytest output format, suggesting a technical failure in test execution or output
      capture.: Re-run independent test verification to confirm actual test status.
      The test command was: pytest tests/forge/executor/test_deploy_compose_handler.py
      tests/forge/executor/test_run_smoke_tests_handler.py -v --tb=short. Investigate
      why the output capture produced conversational text instead of pytest results.

      - Wiring evidence shows parse_degraded status for src/forge/executor/shell_steps.py
      with no findings - this is absent evidence per Guard 7. Cannot independently
      verify that run_smoke_tests is properly wired into the step execution flow.:
      Verify that run_smoke_tests is registered and callable through the execution
      flow, not just structurally compliant. Check for registration in handler maps
      or factory functions.'
    timestamp: '2026-06-22T15:40:44.925425'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file src/forge/executor/shell_steps.py. Actual:\
      \ Path is tracked in git but 'git status --porcelain' shows no change for it\
      \ \u2014 the Player claimed work on a file it did not actually modify this turn.\
      \ Most likely cause: the report writer swept an orchestrator-managed path (e.g.\
      \ a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file tests/forge/executor/test_deploy_compose_handler.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Independent test verification still failing\
      \ with identical output to Turn 1. The raw_output shows '\nI'll run the test\
      \ command for you.\n' instead of pytest results. This suggests the test command\
      \ (pytest tests/forge/executor/test_deploy_compose_handler.py tests/forge/executor/test_run_smoke_tests_handler.py\
      \ -v --tb=short) is not actually executing, or output capture is broken.: Diagnose\
      \ why pytest is not running. Check: (1) Are the test files in the expected location?\
      \ (2) Is pytest installed? (3) Is there an infrastructure issue with test execution?\
      \ (4) Try running the exact command manually to see actual output. The orchestrator's\
      \ independent test runner appears broken - this may require orchestrator-level\
      \ debugging.\n... and 2 more issues"
    timestamp: '2026-06-22T15:49:27.764733'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-06-22T15:56:24.127390'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
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
