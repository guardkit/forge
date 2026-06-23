---
id: TASK-FMDR-001
title: Author the fleet-memory runbook exemplar JSON + shape/round-trip test
status: blocked
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: declarative
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 1
implementation_mode: task-work
complexity: 3
estimated_minutes: 45
dependencies: []
tags:
- forge-output-loop
- runbook-exemplar
- fleet-memory
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FMDR
  base_branch: main
  started_at: '2026-06-23T06:31:03.487096'
  last_updated: '2026-06-23T07:07:41.120660'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- The implementation broke an existing test: tests/forge/test_cli_runbook.py::TestRunbookExecution::test_run_valid_runbook_succeeds.\
      \ This regression must be fixed before approval. The orchestrator's test-orchestrator\
      \ specialist reported this as a 'substrate failure' \u2014 your changes are\
      \ incompatible with the existing test suite.: Investigate why test_run_valid_runbook_succeeds\
      \ fails. The likely cause is that your changes to src/forge/cli/runbook.py modified\
      \ the Runbook class or _parse_runbook_file function in a way that breaks existing\
      \ functionality. Review the test to understand what it expects, then adjust\
      \ your implementation to maintain backward compatibility while still satisfying\
      \ the new requirements.\n- Evidence gathering aborted (gathering_status: partial_gate_abort)\
      \ before all verification gates completed. Cannot independently verify acceptance\
      \ criteria without complete evidence.: Fix the failing test so that all quality\
      \ gates pass and evidence gathering completes successfully. Once tests pass,\
      \ the orchestrator can collect complete evidence for all acceptance criteria."
    timestamp: '2026-06-23T06:31:03.487096'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file forge/runbooks/RUNBOOK-fleet-memory-nas.json.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/cli/runbook.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file tests/forge/test_runbook_exemplar.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 3 more issues"
    timestamp: '2026-06-23T06:38:25.043689'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file forge/runbooks/RUNBOOK-fleet-memory-nas.json.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/cli/runbook.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file tests/forge/test_cli_runbook.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 4 more issues"
    timestamp: '2026-06-23T06:59:29.472174'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-FMDR-001 — Author the fleet-memory runbook exemplar JSON

## Summary

Hand-author the first harvested runbook exemplar — a typed, two-step record that
the executor runs to stand fleet-memory up. No inline shell, no approval gates.
Save it under `forge/runbooks/` (the directory does not exist yet — create it)
as `RUNBOOK-fleet-memory-nas.json` (ASSUM-001), runbook id
`fleet-memory-nas-deploy` (ASSUM-002, confirmed).

This is a **data artefact**, not code. The handlers (`deploy_compose`,
`run_smoke_tests`) already exist on `main` from FEAT-SSH and read their config
from `step.params` — see the §4 Integration Contract in the IMPLEMENTATION-GUIDE
for the exact param shape this JSON must produce.

## Canonical artefact shape

```json
{
  "runbook_id": "fleet-memory-nas-deploy",
  "target": "nas",
  "current_step_index": 0,
  "status": "pending",
  "created_at": "2026-06-22T00:00:00+00:00",
  "steps": [
    {
      "step_type": "deploy_compose",
      "params": {"cwd": "fleet-memory/deploy/nas", "script": "deploy.sh", "env_file": ".env.deploy"},
      "status": "pending",
      "sequence_index": 0
    },
    {
      "step_type": "run_smoke_tests",
      "params": {"cwd": "fleet-memory/deploy/nas", "script": "smoke.sh", "env_file": ".env.deploy"},
      "status": "pending",
      "sequence_index": 1
    }
  ]
}
```

The disposable-target variant differs **only** in the `cwd`/`env_file` pointing at
`fleet-memory/deploy/local` (D3). The typed steps are otherwise identical.

## Acceptance Criteria

- [ ] `forge/runbooks/RUNBOOK-fleet-memory-nas.json` exists and parses cleanly through
      `forge.cli.runbook._parse_runbook_file` into a `Runbook` (A1).
- [ ] The runbook contains exactly two steps in order: a `deploy_compose` step then a
      `run_smoke_tests` step; both reference the deploy directory and `.env.deploy`;
      neither carries any inline shell; no step requires an approval gate (A1).
- [ ] A freshly-parsed runbook has `current_step_index == 0` and its first step is the
      deploy step (B2).
- [ ] A unit test loads the saved JSON, re-serialises it, and asserts the round-trip is
      lossless — the loaded `Runbook` equals the authored one and its typed steps are
      unchanged (D9).
- [ ] A test asserts the saved record is self-contained (no external `$refs`, no inline
      shell) and reusable as a template — i.e. the only edit needed for a different
      target is the `cwd`/`env_file` (A5, D3).
- [ ] The step `params` keys exactly match what `deploy_compose`/`run_smoke_tests`
      read (`cwd`, `script`, `env_file`) — see §4 Integration Contract.
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Coach Validation

- `pytest tests/forge -k runbook_exemplar -v`
- Confirm `forge/runbooks/RUNBOOK-fleet-memory-nas.json` is committed and gitignore
  does **not** exclude it.

## Implementation Notes

- Put the round-trip/shape test under `tests/forge/` (e.g.
  `test_runbook_exemplar.py`), reusing `_parse_runbook_file`.
- `created_at` must be ISO-8601 (`datetime.fromisoformat`-parseable).
- Do **not** embed any secret — `env_file` is a path string only.
