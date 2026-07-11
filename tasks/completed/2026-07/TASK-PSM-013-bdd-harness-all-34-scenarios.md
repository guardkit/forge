---
id: TASK-PSM-013
title: BDD harness wiring all 34 scenarios via pytest-bdd
task_type: testing
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 5
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
status: completed
dependencies:
- TASK-PSM-001
- TASK-PSM-002
- TASK-PSM-003
- TASK-PSM-004
- TASK-PSM-005
- TASK-PSM-006
- TASK-PSM-007
- TASK-PSM-008
- TASK-PSM-009
- TASK-PSM-010
- TASK-PSM-011
tags:
- testing
- bdd
- pytest-bdd
- acceptance
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T14:12:59.304558'
  last_updated: '2026-04-27T14:39:07.234861'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T14:12:59.304558'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-013: feature-rollup completed"
---

# Task: BDD harness wiring all 34 scenarios via pytest-bdd

## Description

Wire the 34 BDD scenarios from
[`features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature`](../../../features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature)
into the pytest-bdd harness as the feature's acceptance test suite.

Scope:

- `tests/bdd/test_pipeline_state_machine.py` — pytest-bdd module that
  references the `.feature` file with `scenarios(...)` and provides step
  definitions
- `tests/bdd/conftest.py` — shared fixtures:
  - `sqlite_db` — in-memory SQLite with schema applied
  - `persistence` — `SqliteLifecyclePersistence` wired to `sqlite_db`
  - `stub_publisher` — records calls to `pipeline_publisher.publish` but
    never connects to real NATS
  - `stub_approval_publisher` — records calls to
    `approval_publisher.publish`
  - `forge_runner` — invokes `forge.cli.main:main` via `click.testing.CliRunner`

Two fixture clusters per the review F10 plan:

- **SQLite fixture**: `:memory:` connection, schema once per session,
  rollback per test
- **Pipeline fixture**: stub publisher/consumer that records published
  payloads but never actually connects to NATS

## Acceptance Criteria

- [ ] All 34 BDD scenarios from
      `pipeline-state-machine-and-configuration.feature` are mapped to
      pytest-bdd step definitions
- [ ] Group A (6 key examples) — all pass
- [ ] Group B (6 boundary conditions) — all pass; Scenario Outlines fan
      out correctly (turn-budget bounds × 4 examples; history-limit × 3
      examples; terminal-states-after-crash × 4 examples)
- [ ] Group C (7 negative cases) — all pass
- [ ] Group D (9 edge cases) — all pass; crash recovery scenarios use
      `reconcile_on_boot` against a seeded SQLite state
- [ ] Group E (2 security) — path-traversal includes URL-encoded variants
      (sc_003 invariant); cancel-operator-distinct from originator (sc_005)
- [ ] Group F (2 concurrency) — uses two threads invoking `forge queue`
      concurrently against the same SQLite (with WAL); reader fixture
      exercises the consistent-snapshot guarantee
- [ ] Group G (2 data integrity) — terminal-state-completion-time
      invariant + write-then-publish-failure (sc_002)
- [ ] Group H (1 integration boundary) — NATS unreachable + `forge status`
      / `forge history` succeed
- [ ] No real NATS connections are made by the test suite (CI-friendly)
- [ ] Test run completes in under 30 seconds on developer laptop
- [ ] BDD scenarios are tagged with `@task:TASK-PSM-NNN` (this is wired
      automatically by `/feature-plan` Step 11 — do NOT add them by hand
      here)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
# tests/bdd/test_pipeline_state_machine.py
from pytest_bdd import scenarios, given, when, then, parsers

scenarios("../../features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature")


@given("a feature description at a permitted repository path")
def permitted_repo(forge_config_with_allowlist, tmp_path):
    repo = tmp_path / "permitted_repo"
    repo.mkdir()
    return repo


@when(parsers.parse('I queue the feature for a build'))
def queue_feature(forge_runner, persistence, stub_publisher, permitted_repo):
    result = forge_runner.invoke(["queue", "FEAT-TEST", "--repo", str(permitted_repo)])
    forge_runner.last_result = result


@then("a new build should be recorded as pending pickup")
def assert_pending_build(persistence):
    assert persistence.exists_active_build("FEAT-TEST")
```

(And so on for the remaining steps — typical pytest-bdd boilerplate.)

## Coach Validation

- `tests/bdd/test_pipeline_state_machine.py` exists and runs
- `tests/bdd/conftest.py` provides the four fixtures
- All 34 scenarios are reachable via `pytest tests/bdd -v`
- No real NATS connection (verified by network blocking in CI or
  `--no-network` pytest plugin)
- Test run < 30s on dev laptop
- Lint/format pass
