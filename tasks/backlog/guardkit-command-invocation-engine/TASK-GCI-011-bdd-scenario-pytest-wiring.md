---
id: TASK-GCI-011
title: BDD scenario pytest wiring (R2 oracle activation)
task_type: testing
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-GCI0
feature_id: FEAT-FORGE-005
wave: 5
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-GCI-009
- TASK-GCI-010
- TASK-GCI-006
- TASK-GCI-007
tags:
- bdd
- pytest
- testing
- scenarios
- oracle
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T10:03:39.218898'
  last_updated: '2026-04-26T10:23:22.553056'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T10:03:39.218898'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: BDD scenario pytest wiring (R2 oracle activation)

## Description

Wire pytest-bdd against `features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature`
so every one of the 32 BDD scenarios is executed by an automated test. These
become Coach-blocking oracles via the R2 task-level BDD runner once the
`bdd-linker` subagent has tagged each scenario with the matching
`@task:<TASK-ID>` (Step 11 of `/feature-plan` runs the linker; see
`docs/design/contracts/API-tool-layer.md` §6 + `installer/core/agents/bdd-linker.md`).

This is the test layer that exercises the integrated subprocess engine:
context resolver + parser + runner + tool wrappers + git/gh adapter, against
fakes/mocks for DeepAgents `execute` and the NATS client.

## Acceptance Criteria

- [ ] `pyproject.toml` (or equivalent) declares `pytest-bdd` and any bdd
      glue dependencies in the dev/test extra
- [ ] All 32 scenarios from the feature file resolve to a step-definition
      file (typically `tests/bdd/test_guardkit_command_invocation_engine.py`
      using the `@scenarios(...)` or per-`@scenario` pattern)
- [ ] At least one Background fixture wires:
      - a tmp-path "build worktree" with the right structure
      - a `forge.yaml` with permissions allowlist pointing at the tmp path
      - a tmp `.guardkit/context-manifest.yaml` in the target repo
      (the Background block at the top of the feature file)
- [ ] Step definitions use the **public** tool layer surface
      (`guardkit_*` from TASK-GCI-009/010) plus the git/gh adapters
      (TASK-GCI-006/007) — not the private `run()` directly. The point is
      to validate the contract the reasoning model sees
- [ ] DeepAgents `execute` is **stubbed** to return canned subprocess
      outputs (success / non-zero / timeout / unknown shape / shell-meta
      passthrough) — no real `guardkit` binary required for the suite
- [ ] NATS client is faked — for streaming scenarios, drive
      `pipeline.stage-complete.*` events via the fake client and verify
      they reach the `ProgressSink`
- [ ] Boundary `Scenario Outline` for the 1/300/599-second timeout examples
      uses `pytest-bdd`'s outline support (or per-example tests)
- [ ] Each scenario passes; all 32 are visible in pytest collection output
- [ ] Tests run in CI as part of the standard pytest invocation; no
      separate `make` target required to discover them
- [ ] Coverage of touched modules (`src/forge/adapters/guardkit/*` and
      `src/forge/tools/guardkit.py`, `src/forge/tools/graphiti.py`) ≥ 85%
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Scenario → task home (for `bdd-linker` reference; Step 11 confirms with thresholds)

The `bdd-linker` subagent runs in Step 11 and writes the actual
`@task:<TASK-ID>` tags into the `.feature` file based on matching scores.
This map is the planner's reference for how the breakdown was conceived:

| Scenario group | Primary task home |
|---|---|
| A — Key examples (success, context flags, streaming, error contract, worktree confinement, PR creation, Graphiti bypass) | 008, 003, 005, 008, 008, 007, 010 |
| B — Boundary (timeouts, depth-2 chase, depth-cap warn, stdout-tail) | 008, 003, 003, 004 |
| C — Negative (missing manifest, allowlist refusal, cwd refusal, non-zero exit, unknown shape, omitted docs, missing creds) | 003, 008, 008, 008, 004, 003, 007 |
| D — Edge cases (cycle, cleanup failure, progress unavailable, retry, internal error, parallel) | 003, 006, 005, 008, 008, 008 |
| E — Security/concurrency (path traversal, shell-meta, concurrent builds, cancellation, back-pressure, stalled) | 003, 008, 003, 008, 005, 008 |

## Implementation Notes

- pytest-bdd 7.x: import `from pytest_bdd import scenarios, given, when, then`;
  use a single `scenarios("guardkit-command-invocation-engine.feature")`
  call to load all scenarios at once
- Reuse fixtures across scenarios via `conftest.py` at the BDD test
  directory level
- For the streaming scenario, the synchronous `run()` blocks until the
  subprocess "exits"; in a fake setup, emit fake progress events via the
  fake NATS client during a `await asyncio.sleep(...)` between fake
  stdout and fake exit — that gives the subscriber a real concurrent
  observation point
- The `bdd-linker` ran by Step 11 of `/feature-plan` adds the real
  `@task:` tags; this map is the planner's reference, not the
  authoritative tagging
