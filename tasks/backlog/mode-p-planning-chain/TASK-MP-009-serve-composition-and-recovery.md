---
id: TASK-MP-009
title: Serve composition + boot audit gating + planning rearm/boot-sweep recovery
task_type: integration
status: backlog
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 5
implementation_mode: task-work
complexity: 7
estimated_minutes: 90
dependencies: [TASK-MP-004B, TASK-MP-005, TASK-MP-006, TASK-MP-007, TASK-MP-008]
tags: [mode-p, serve, recovery]
---

# TASK-MP-009 — Serve composition + boot audit gating + recovery

## Description

The task that makes Mode P real in the live daemon — without it Mode P is dead
code (PS-002: `DispatchOrchestrator`/`NatsSpecialistDispatchAdapter` have ZERO
production call sites today; the 2026-07-06 verification also found the Supervisor
mode-reader gap at serve.py:654, which is explicitly OUT of scope — Mode P never
routes through the Supervisor). Composes: PlanningConfig load -> DF-004 audit
(violation -> loud ERROR + planning durable NOT attached + build intake boots
normally) -> first production composition of the specialist dispatch stack behind
ONE injectable callable seam -> consumer + planner + checkpoint + escalation +
terminal registry wiring -> boot recovery: `rearm_paused_planning_runs()` (single
re-emit owner, arm-before-post, re-issues each PAUSED run's persisted request_id to
the row's CURRENT expected_approver) + the RT-05 **boot sweep** for non-terminal
non-paused runs (QUEUED/RUNNING re-driven or failed with a structured reason —
ack-on-persist's compensating twin).

## BDD Scenarios

- "A daemon restart while paused re-arms the checkpoint and preserves the pause"
- "A daemon restart after an escalation re-arms the checkpoint to the escalation approver"
- "A planning run interrupted before its stage dispatch is recovered at boot"
- "A message-bus outage during a paused run does not lose the run"
- "Planning intake and build intake coexist without interference" (daemon half)
- "A cloud fallback in planning model resolution fails the planning audit loudly" (boot-integration half)
- "The product owner stage is dispatched to the local specialist and coach-scored" (production composition half)

## Files

- Creates: `src/forge/cli/_serve_planning.py` (composition + `rearm_paused_planning_runs` + `sweep_interrupted_planning_runs` + the dispatch-stack seam: shared NATS client -> CorrelationRegistry + NatsSpecialistDispatchAdapter -> DispatchOrchestrator(DiscoveryCache/TimeoutCoordinator/SqliteHistoryWriter) + FleetWatcher; wrapped as one `dispatch_stage` callable = partial over `dispatch_specialist_stage` with a PlanningForwardContextBuilder emitting request_text and a PlanningStageLogWriter over planning_run_events)
- Modifies: `src/forge/cli/serve.py` (additive `_compose` hook only), `src/forge/cli/_serve_daemon.py` OR a sibling attach point (start the planning durable beside the build consumer)
- Tests: `tests/integration/test_mode_p_planning_chain.py`, `tests/cli/test_serve_planning.py`

## Acceptance Criteria

- [ ] Restart re-arm (offline): drive intake -> PO-complete -> PAUSED against a tmp_path SQLite file with fakes; discard all composition objects; re-run boot composition against the SAME file -> run still PAUSED, fake approval publisher shows exactly ONE re-issued request carrying the PERSISTED request_id, and a subsequent fake approval resumes the run to PLANNED_HANDOFF
- [ ] Restart-after-escalation: same shape with an escalated row -> the re-issued request names the escalation approver (row's current expected_approver), and elapsed wait is not reset (paused_at/escalated_at anchors honoured)
- [ ] Boot sweep (RT-05): a run left QUEUED (crash before dispatch) is re-driven or failed with a structured reason at boot — never stuck forever; a RUNNING run likewise
- [ ] Fake bus publisher failing N calls then recovering -> run remains PAUSED throughout, checkpoint answerable after recovery, no state loss
- [ ] Build fixture + planning fixture processed side by side -> build follows builds tables, planning follows planning_runs; neither consumer's ack slot consumed by the other (fake-consumer isolation test)
- [ ] Config with non-empty planning fallbacks -> boot logs the audit failure loudly, planning consumer never starts, build consumer starts (both predicates asserted)
- [ ] `planning.enabled=False` (default) -> zero planning wiring composed and ALL existing serve tests still green; serve.py diff is additive-only (the Supervisor construction at ~line 654 is untouched — scope predicate)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Integration with fakes only; pattern sources:
  `tests/integration/test_gate_restart_recovery.py`,
  `tests/integration/test_gate_activation_production_wiring.py`,
  `tests/integration/test_durable_decision_on_publish_failure.py`.

## Implementation Notes

- Everything soft-fails (DDR-007): planning composition failure must never brick
  dispatch boot — mirror the try/except posture around bind_gate_parts
  (serve.py:396-402).
- rearm ownership rule: `rearm_paused_planning_runs` is the ONLY planning re-emit
  site at boot (mirror rearm_paused_gates' arm-before-post; reuse/extract the
  `_ArmSignallingClient` shape from _serve_gate_activation.py if practical without
  modifying that module's behaviour).
- Dispatch outcome mapping at this seam: Degraded -> FLAG_FOR_REVIEW (feeds the
  DF-006/human-review path), transport exception -> run FAILED, AsyncPending -> ERROR.
