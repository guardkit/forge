# TASK-JNB-106 Implementation Plan — v1.1 scenario tests over production wiring

**Status**: v1 (light intensity — testing task, complexity 5; harness
already built and reviewed in TASK-JNB-101/102)
**Date**: 2026-07-05

## Coverage audit (existing vs the seven required scenarios)

| # | Scenario | Existing coverage | This task |
|---|---|---|---|
| 1 | Within-window approve resumes | TestApproveResumesOnce (JNB-101) | named scenario test in the JNB-106 suite |
| 2 | After-window reply not applied | none | NEW |
| 3 | Unrecognised decision refused + logged | subscriber unit tier only | NEW (production-wiring tier) |
| 4 | Wrong correlation_id refused as anomaly | TestSpoofedReplyRefused (JNB-101) | named scenario test + anomaly-log assertion |
| 5 | Duplicate response request_id-deduped | TestApproveResumesOnce duplicate leg | named scenario test + dedup-log assertion |
| 6 | Reply after terminal state ignored | none | NEW |
| 7 | Approve-vs-expiry race → exactly one outcome | none | NEW (the load-bearing single-locus test) |

The task is explicitly a scenario-suite task: every scenario gets its own
named class/test in ONE file so the collect-only count guard can pin all
seven, even where the mechanics overlap earlier suites.

## Design

- File: `tests/integration/test_jnb106_v11_scenarios.py`, driving
  `gate_check` through the TASK-JNB-101 production factory
  (`_production_deps` harness re-used from
  `test_jnb101_production_wiring`) — production-wired chain, in-memory
  NATS + clocks, no live broker, no JetStream consumer (AGENTS-stream
  core-subscribe faithfulness preserved).
- Scenario classes (names mirror the FEAT-UBS-003 spec counterparts as
  enumerated in the task file):
  `TestWithinWindowApproveResumes`, `TestAfterWindowReplyNotApplied`,
  `TestUnrecognisedDecisionRefused`, `TestWrongCorrelationIdRefused`,
  `TestDuplicateResponseDeduped`, `TestReplyAfterTerminalIgnored`,
  `TestApproveVsExpiryRace`.
- Race interleaving control (scenario 7): explicit event-loop
  scheduling, not sleeps — approve-wins leg delivers the response the
  moment the subscription registers (queue populated before the
  zero-window timeout evaluates); expiry-wins leg lets the gate run to
  completion first, then delivers late. Both legs assert EXACTLY ONE
  recorded outcome (resumed xor cancelled, state + wire consistent).
- Collect-only guard: subprocess `pytest <file> --collect-only -q`
  asserting all seven scenario test ids are collected (per the task's
  Test Requirements — guards silent scenario loss).
- mark_resume_pending note: scenario 1's AC line "(mark_resume_pending
  invoked)" predates the recorded TASK-JNB-101 AC-3 deviation; the
  equivalent assertion here is the wire-level build-resumed emit
  (exactly once, full fidelity) per the deviation record.
- DDR-027 respected: no test simulates dedup surviving a restart.

## Files
- tests/integration/test_jnb106_v11_scenarios.py (NEW, ~380 LOC)
