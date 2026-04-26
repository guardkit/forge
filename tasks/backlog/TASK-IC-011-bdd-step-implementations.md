---
id: TASK-IC-011
title: "BDD step implementations for all 43 scenarios"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: testing
tags: [bdd, testing, integration]
complexity: 6
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 5
implementation_mode: task-work
dependencies:
  - TASK-IC-001
  - TASK-IC-002
  - TASK-IC-003
  - TASK-IC-004
  - TASK-IC-005
  - TASK-IC-006
  - TASK-IC-007
  - TASK-IC-008
  - TASK-IC-009
  - TASK-IC-010
estimated_minutes: 240
---

# Task: BDD step implementations for all 43 scenarios

## Description

Implement pytest-bdd step functions wiring all 43 scenarios in
`features/infrastructure-coordination/infrastructure-coordination.feature`
to the production code from units 1-10. Prioritise the 6 `@smoke` scenarios
in the first pass; iterate through `@key-example`, `@boundary`, `@negative`,
`@edge-case`, `@security`, `@concurrency`, `@data-integrity`, `@integration`
in subsequent passes.

This is the unit that turns the BDD spec from documentation into an
executable acceptance suite that R2 (BDD oracle) can run during `/task-work`.

## Module: `tests/bdd/`

Layout:
```
tests/bdd/
├── conftest.py                          # shared fixtures (mocked Graphiti, tmp worktree)
├── test_smoke.py                        # 6 @smoke scenarios first
├── test_key_examples.py                 # 10 @key-example scenarios
├── test_boundary_negative.py            # 5 @boundary + 7 @negative
├── test_edge_security.py                # 14 @edge-case + 6 @security
├── test_concurrency_integrity.py        # 3 @concurrency + 4 @data-integrity
└── test_integration_e2e.py              # 3 @integration (longest scenarios)
```

## Acceptance Criteria

- [ ] All 43 scenarios from
      `features/infrastructure-coordination/infrastructure-coordination.feature`
      have corresponding `@scenario` step bindings
- [ ] All 6 `@smoke` scenarios pass in the first commit (smoke gate for
      autobuild)
- [ ] Shared `conftest.py` provides: mocked Graphiti client, tmp worktree
      fixture, tmp SQLite DB fixture, env-cleared subprocess fixture
- [ ] Each scenario binds to the relevant production module (units 1-10)
      via real imports — NOT mocking the module under test
- [ ] BDD scenarios are tagged with `@task:TASK-IC-011` per the
      `bdd-linker` convention so R2 can match them at task-work time
      (Step 11 of /feature-plan handles this automatically; verify post-tag)
- [ ] All test files pass project-configured lint/format checks with zero errors
- [ ] No production code modified in this task — only tests

## Test Requirements

- [ ] `pytest tests/bdd/ -m smoke` passes (6 scenarios)
- [ ] `pytest tests/bdd/` passes overall (43 scenarios)
- [ ] CI integrates the BDD suite into the standard test command

## Implementation Notes

- Use `pytest-bdd` (already a likely test dep). If not present, add it to
  `pyproject.toml` `[project.optional-dependencies] test`.
- The 6 `@smoke` scenarios MUST land first; they unblock R2 oracle smoke
  gating for the rest of the feature's tasks.
- For scenarios involving "Graphiti is unreachable" (e.g. `@negative
  memory-write-failure-tolerated`), use a fixture that monkey-patches the
  Graphiti client to raise.
- For `@concurrency` scenarios, use `pytest.mark.asyncio` with two
  concurrent coroutines; assert the post-state, not call-order.
- For `@security` scenarios involving the execute-tool allowlist, mock the
  execute tool itself (not the binary) so the test asserts on the
  allowlist check, not on git/gh actually being installed.
- Scenarios involving the end-to-end build (`@integration
  integration-end-to-end-build`) should use a fixture that creates a
  throwaway git repo in a tmp directory; tests should not hit GitHub.
