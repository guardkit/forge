---
id: TASK-MP-004A
title: Planning-backed gate protocol adapters (GateRepository/StateMachine over planning_runs)
task_type: feature
status: backlog
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 2
implementation_mode: task-work
complexity: 4
estimated_minutes: 45
dependencies: [TASK-MP-002]
tags: [mode-p, gating, adapters]
---

# TASK-MP-004A — Planning-backed gate protocol adapters

## Description

First half of the product_docs checkpoint (pre-split at the panel's PS-004
recommendation): implementations of the `GateRepository` / `StateMachine`
Protocols from `src/forge/gating/wrappers.py:268-335` over `SqlitePlanningRunStore`,
mirroring `src/forge/gating/sqlite_adapters.py` (including the `_PauseHandoff`
request-id bridge pattern). This is what lets the D659 gate primitives operate on
planning runs without a builds row — the gating module itself is NOT modified.

## BDD Scenarios

- "Completed product docs pause the run at the product docs checkpoint" (persistence half)
- "The planning run's history records every transition with its identities" (gate-decision events)

## Files

- Creates: `src/forge/planning/gate_adapters.py` (`PlanningGateRepository`, `PlanningStateMachine`, `build_planning_gate_adapters(store, clock)` factory mirroring `build_sqlite_gate_adapters`)
- Modifies: `src/forge/gating/wrappers.py` **__all__ ONLY** — export the existing `derive_request_id` re-export surface and `_atomic_pause_and_publish`/`_build_request_envelope` as public names (zero behaviour change; a test pins that the module's functions' bytecode is unchanged apart from the export list) — IF the Player judges importing privates safer than an __all__ edit, document the choice in the task log and pin the import in a seam test instead
- Tests: `tests/forge/planning/test_gate_adapters.py`

## Acceptance Criteria

- [ ] `PlanningGateRepository` and `PlanningStateMachine` structurally satisfy the wrappers Protocols (a test binds them into `GateCheckDeps`-shaped usage via the same duck-typed call pattern gate_check uses: record_decision, record_paused_build, list_paused_builds / transition_to_paused, transition_to_cancelled, transition_to_failed equivalents over planning states)
- [ ] `record_paused_*` stores `pending_approval_request_id` on the planning_runs row and stamps `paused_at` (durable wall-clock anchor — RT-04); gate decisions write planning_run_events rows with gate metadata (gate_mode, details_json)
- [ ] `list_paused_runs()` reconstructs paused-run snapshots (correlation_id, expected_approver, pending request id, paused_at/escalated_at) sufficient for TASK-MP-009's rearm sweep
- [ ] State-changing methods delegate to the store's CAS transitions and translate refusals to the sentinel shapes the gate expects — never raise on stale transitions
- [ ] `src/forge/gating/` behaviour unchanged: existing gating test suite green; diff limited to the export list (or zero if the import-pin route is chosen)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit with tmp_path SQLite; crib fixture shapes from `tests/forge/gating/`.

## Implementation Notes

- Run ids on the wire are namespaced `plan-{correlation_id}` (ARCH-007) so approval
  subjects (`agents.approval.forge.{run_id}`) and request ids never collide with
  build ids; the adapters own that mapping in one place.
- `derive_request_id(run_id, "product_docs", attempt)` is already generic over any
  id string (gating/identity.py) — no gating edits needed for it.
