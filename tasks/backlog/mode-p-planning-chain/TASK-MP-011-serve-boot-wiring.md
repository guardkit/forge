---
id: TASK-MP-011
title: Wire Mode P planning composition into serve boot (call-site pin)
task_type: integration
status: backlog
parent_review: TASK-REV-83E4
feature_ref: FEAT-SPL-002
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 45
dependencies: []
tags: [mode-p, serve, wiring, fix]
---

# TASK-MP-011 — Wire Mode P planning composition into serve boot

## Description

FEAT-3ED2 merge review found the per-task-green-but-feature-dead gap the plan
itself predicted (PS-002): `src/forge/cli/_serve_planning.py` ships
`compose_planning_consumer_and_dispatch`, `rearm_paused_planning_runs` and
`sweep_interrupted_planning_runs` fully tested — but NOTHING in `serve.py` calls
them. `grep -rn "compose_planning_consumer_and_dispatch" src/forge/cli/` matches
only the defining module. Mode P is dead code in the live daemon. TASK-MP-009's
AC ("serve.py additive `_compose` hook") was satisfied by tests that drive the
composition function directly, which a deterministic Coach cannot distinguish
from real wiring — so this task adds the wiring AND a call-site pin test that
makes the regression structurally impossible to miss.

## BDD Scenarios (features/mode-p-planning-chain/mode-p-planning-chain.feature)

- "The product owner stage is dispatched to the local specialist and coach-scored" (production wiring half)
- "A cloud fallback in planning model resolution fails the planning audit loudly" (boot-integration half)
- "Planning intake and build intake coexist without interference" (daemon half)

## Files

- Modifies: `src/forge/cli/serve.py` ONLY additively — inside
  `bind_production_dispatch_chain`'s `_compose` (after the gate-parts binding and
  its rearm, mirroring that exact soft-fail posture at serve.py:396-402): when
  `config.planning.enabled`, call `compose_planning_consumer_and_dispatch(...)`
  then `sweep_interrupted_planning_runs(...)` then `rearm_paused_planning_runs(...)`
  wrapped in try/except that logs loudly and NEVER breaks build-chain binding
  (DDR-007). When `enabled=False` (default): zero planning calls.
- Creates: `tests/cli/test_serve_planning_wiring.py`

## Acceptance Criteria

- [ ] Call-site pin: a test monkeypatches `forge.cli._serve_planning.compose_planning_consumer_and_dispatch` (and the two recovery functions) with recording fakes, drives the PRODUCTION composition path in `serve.py` (`bind_production_dispatch_chain`/`_compose`) with a `planning.enabled=True` config and fakes for the NATS client/SQLite pool, and asserts the composition fake was invoked exactly once with the loaded PlanningConfig — proving the production path, not the module, is wired
- [ ] Recovery order: the recording fakes show sweep and rearm are invoked after composition, and rearm is invoked at most once (single re-emit owner preserved)
- [ ] `planning.enabled=False` (default config) → zero invocations of any planning function, and the full existing `tests/cli/` suite remains green
- [ ] Soft-fail: the composition fake raising -> the build dispatch chain still binds successfully and the error is logged (caplog predicate); daemon boot is never bricked by planning wiring (DDR-007)
- [ ] `src/forge/cli/serve.py` diff is additive-only: the Supervisor construction and all existing composition steps are untouched (existing serve tests pass unmodified)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit/integration with fakes only (no live NATS); pattern sources:
  `tests/cli/test_serve_planning.py` fixtures, and the gate-parts soft-fail tests
  around `bind_gate_parts`.

## Implementation Notes

- `compose_planning_consumer_and_dispatch` already owns the DF-004 boot audit and
  the enabled check internally — the serve hook should still guard on
  `config.planning.enabled` to keep the default path zero-cost, but must not
  duplicate audit logic.
- Do NOT modify `_serve_planning.py`'s behaviour; this task is the missing caller
  plus its pin. Do NOT touch the gate-parts/build-chain composition steps.
