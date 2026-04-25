---
complexity: 4
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-SAD-010
feature_id: FEAT-FORGE-003
id: TASK-SAD-011
implementation_mode: task-work
parent_review: TASK-REV-SAD3
priority: high
status: design_approved
tags:
- testing
- bdd
- pytest-bdd
- smoke
- key-example
task_type: testing
test_results:
  coverage: null
  last_run: null
  status: pending
title: BDD smoke + key-example pytest wiring
updated: 2026-04-25 00:00:00+00:00
wave: 5
---

# Task: BDD smoke + key-example pytest wiring

## Description

Wire the `@smoke` and `@key-example` scenarios from
`features/specialist-agent-delegation/specialist-agent-delegation.feature`
into the existing pytest-bdd harness. The harness already exists
(`tests/bdd/conftest.py` from FEAT-FORGE-002) — this task adds:

- A new `tests/bdd/test_specialist_agent_delegation.py` module.
- Step definitions that exercise the `DispatchOrchestrator` end-to-end
  using the existing `FakeNatsClient` and the new dispatch components.
- Smoke scenarios:
  - **A.exact-tool-dispatch** (Forge delegates a stage to a specialist
    advertising the exact tool)
  - **A.coach-output-parsing** (Forge reads Coach output preferring
    top-level fields over nested)

Other key-example scenarios wired in this task:
- A.intent-pattern-fallback
- A.retry-with-additional-context
- A.outcome-correlation

The remaining 28 scenarios are deferred to follow-up testing tasks (one
per group) — out of scope for this feature.

## Acceptance Criteria

- [ ] `tests/bdd/test_specialist_agent_delegation.py` created using
      `pytest-bdd`'s `scenarios("specialist-agent-delegation.feature")`
      pattern.
- [ ] Step definitions for the Background, all 5 key-example scenarios
      (including 2 smoke scenarios) are implemented and passing.
- [ ] Reuses `tests/bdd/conftest.py:FakeNatsClient` — does NOT introduce
      a parallel test transport. Extend conftest with subscribe/unsubscribe
      recording if needed.
- [ ] Smoke scenarios run in the project's `pytest -m smoke` selection.
- [ ] Step definitions assert subscribe-before-publish ordering using the
      `FakeNatsClient`'s recording-order property (the canonical LES1
      assertion).
- [ ] No flaky scenarios. Use the deterministic `Clock` fixture from
      conftest for any time-sensitive steps.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Implementation Notes

- The `@task:` tag injection on each scenario is handled by Step 11 of
  `/feature-plan` (BDD scenario linking via `feature-plan-bdd-link`). Do
  NOT manually add `@task:` tags here — Step 11 will tag scenarios
  according to the linker's mapping.
- Step definitions should be parametrised by scenario fixtures
  (`pytest-bdd`'s `parsers.parse(...)`), not by ad-hoc string matching.
- Use the existing fake `Clock` to make timeout-related assertions
  deterministic. Do NOT use `time.sleep()` or `asyncio.sleep()` in
  step definitions.
- Defer broader scenario coverage (groups B, C, D, E) to follow-up
  testing tasks. This task covers the smoke + key-example surface only.