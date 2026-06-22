---
id: TASK-RBX-006
title: Security & data-integrity scenario tests
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: testing
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 5
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies:
- TASK-RBX-004
- TASK-RBX-005
tags:
- forge
- runbook
- executor
- testing
- security
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-22T09:00:12.659769'
  last_updated: '2026-06-22T09:12:28.815565'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T09:00:12.659769'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Security & data-integrity scenario tests

## TL;DR

Lock in the Phase-4 Security (Group E) and Data-Integrity (Group G) properties
of the executor as executable BDD scenarios bound to step functions. All
in-memory fakes — no subprocess, no broker.

## Scope

`tests/bdd/test_runbook_executor.py` (Security + Data-Integrity bindings) and
any shared fixtures/conftest. Binds the following `runbook-executor.feature`
scenarios:

**Security (Group E):**
- Scenario Outline "An adversarial step type with no handler is escalated,
  never executed" — `run; DROP TABLE steps`, `` exec`whoami` ``,
  `${jndi:ldap://evil}`. Asserts the run stops at that step, `escalated` is
  announced, and the `step_type` is used **only** as a registry lookup key
  (never evaluated/executed/interpolated).
- "A handler that raises an unexpected error is contained as a step failure" —
  first step recorded `failed`, `escalated` announced, second handler does not
  run, executor stops cleanly (no crash).

**Data Integrity (Group G):**
- "Persisted step state is the source of truth for resume, not the event
  stream" — a passed-and-persisted step whose `step-result` announcement was
  lost is **not** re-run; resume continues at the next step.
- "A step result is committed before the resume pointer advances past it" —
  after an interrupt between result-commit and advance, resume lands on the
  first or second step but never skips the first step's result; no step is
  advanced past without its result persisted.

## Acceptance Criteria

- [ ] All three adversarial `step_type` examples stop the run and escalate;
      a test asserts the `step_type` value is never passed to any
      eval/exec/format/subprocess sink (only `registry.resolve`).
- [ ] The handler-raises scenario records `failed`, announces `escalated`,
      leaves the later handler un-run, and the executor returns without
      propagating the exception.
- [ ] The lost-announcement scenario does not re-run the persisted step and
      resumes at the next step (persisted state authoritative).
- [ ] The commit-before-advance scenario never skips a step's result and never
      leaves the pointer past an unpersisted result.
- [ ] All bound scenarios pass: `pytest -m "security or data_integrity"`
      (runbook-executor subset) is green; no unknown-mark warnings.
- [ ] Tests use in-memory fake handlers and a `tmp_path` SQLite file; no
      subprocess, no NATS broker.

## Coach Validation

```bash
python -m pytest tests/bdd/test_runbook_executor.py -q -m "security or data_integrity"
python -m pytest tests/bdd/test_runbook_executor.py -q
```

## Implementation Notes

- The "never executed" assertion is the security crux: inject a registry/
  handler-runner spy and assert the adversarial string only ever appears as a
  dict-key argument to `resolve`, never reaching a shell/eval. This is what
  proves `step_type` is inert data.
- Mirror the persistence suite's fixture style
  (`tests/forge/persistence/test_runbook.py`): one SQLite file per test.
