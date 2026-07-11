---
complexity: 6
dependencies:
- TASK-FW10-007
- TASK-FW10-008
- TASK-FW10-009
- TASK-FW10-010
estimated_minutes: 120
feature_id: FEAT-FORGE-010
id: TASK-FW10-011
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- integration-test
- e2e
- lifecycle
- correlation-id
- capstone
task_type: testing
title: End-to-end lifecycle integration test (build-queued → terminal envelope, all
  subjects)
wave: 4
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-011 — End-to-end lifecycle integration test

## Why

The capstone test for FEAT-FORGE-010. Asserts that a `pipeline.build-queued.<feature_id>` envelope routed to `forge serve` produces the full lifecycle envelope sequence on JetStream, with the inbound `correlation_id` threaded through every event, in the correct order, against the wired-in-production stack rather than a unit test with mocked dispatchers.

Per finding F7, the test mocks `AutobuildDispatcher` at the boundary so the test does not actually run a real autobuild (that's Mode A territory, FEAT-FORGE-007). This is a wiring test: it proves the production composition sends every envelope it should send.

## Files to create / modify

- `tests/integration/test_forge_serve_orchestrator_e2e.py` (NEW):
  - Spins up `forge serve` against an embedded NATS server (or a `docker-compose` fixture if embedded NATS isn't viable) and a temporary SQLite database.
  - Mocks `AutobuildDispatcher.dispatch(...)` at the boundary so the autobuild "runs" by emitting a scripted sequence of `_update_state` transitions through the real `PipelineLifecycleEmitter` — no real worktree, no real DeepAgents subagent invocation.
  - Publishes one `pipeline.build-queued.FEAT-XXX` envelope with a known `correlation_id`.
  - Subscribes to all eight `pipeline.*.FEAT-XXX` subjects and collects envelopes in publish order.
  - Asserts the canonical sequence + correlation-id threading + ordering invariants.
- `tests/integration/conftest.py` (MODIFY if needed):
  - Add fixture for the embedded NATS / temp SQLite combination if not already present.

## Test scenarios covered

The test asserts the following from the FEAT-FORGE-010 spec:

| Scenario | Group | Assertion in this test |
|---|---|---|
| Full lifecycle envelope sequence end-to-end | A | `build-started → stage-complete×N → terminal` published in order. |
| Correlation-id threaded through every envelope | A | Every envelope carries the inbound `correlation_id`; no envelope carries a different one. |
| Build-started precedes first stage; terminal closes | A | First envelope is `build-started`; last is `build-{complete,failed,cancelled}`. |
| AsyncSubAgent dispatch returns task_id without blocking | A | Dispatch returns within a small timeout; supervisor still answers status query. |
| Real-stage-only `stage-complete` (no synthetic dispatch envelope) | A | No envelope has `stage_label="dispatch"` (ASSUM-004). |
| Single shared NATS connection | A | Asserted via observability hook: only one `nats.connect(...)` call across the daemon's startup. |
| Lifecycle ordering invariant | E | `build-started` precedes any `stage-complete`; every `stage-complete` precedes the terminal envelope; terminal envelope appears exactly once. |
| In-subagent `stage-complete` carries `target_kind="subagent"` and `target_identifier=task_id` | E | At least one envelope in the sequence has these fields populated as expected (ASSUM-018). |
| Two-replica failover (max_ack_pending=1) | D | Optional second sub-test: spin up two replicas; assert exactly one fetches the message. |
| Fail-fast on NATS unreachable | E | Sub-test: start daemon with bad NATS URL; assert it fails to start with a diagnostic naming the broker. |

## Acceptance criteria

- [ ] Test spins up `forge serve` end-to-end against embedded/containerised NATS + temp SQLite without manual setup.
- [ ] `AutobuildDispatcher.dispatch` is mocked at the boundary; no real autobuild runs.
- [ ] On a single `pipeline.build-queued.FEAT-XXX` envelope, the test observes the full lifecycle envelope sequence on JetStream.
- [ ] Every observed envelope carries the inbound `correlation_id`; no envelope carries a different one.
- [ ] Ordering invariant holds: `build-started` precedes every `stage-complete`; every `stage-complete` precedes the terminal envelope; terminal appears exactly once.
- [ ] No envelope carries `stage_label="dispatch"` (ASSUM-004 — synthetic envelope dropped).
- [ ] At least one in-subagent `stage-complete` envelope has `target_kind="subagent"` and `target_identifier` matching the autobuild's async task_id (ASSUM-018).
- [ ] Sub-test: two replicas → exactly one fetches the message; the other idles (ADR-ARCH-027 / Group D scenario).
- [ ] Sub-test: NATS unreachable → daemon fails to start with a diagnostic naming the broker (Group E `@integration` scenario).
- [ ] Test runs cleanly in CI (deterministic, no flakiness from real-time delays).
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Implementation notes

- Prefer embedded NATS (e.g., `nats-server` binary launched as a subprocess on a random port) over docker-compose for CI determinism. If the project already has a NATS test fixture, reuse it.
- Asserting "single shared NATS connection" requires either an instrumentation hook on `nats.connect` (monkey-patch + counter) or a check that the daemon's `_run_serve` is structured so only one `connect` call site exists.
- The mocked dispatcher should script a realistic transition sequence: `starting → planning_waves → running_wave → completed`. This exercises the full ordering invariant.
- For the two-replica sub-test, use two daemon coroutines bound to the same durable name in the same test process. Real two-process testing is overkill for a wiring test.
- Cancel propagation (Group D scenario "cancel propagates through subagent") is exercised here only if the dispatcher mock supports cancellation — otherwise file a follow-up. Per the review, this is acceptable.

## Coach validation

- `pytest tests/integration/test_forge_serve_orchestrator_e2e.py -x -v`.
- Full integration suite: `pytest tests/integration -x`.
- Lint: project-configured ruff/format.

## References

- [API-nats-pipeline-events.md](../../../docs/design/contracts/API-nats-pipeline-events.md) (the eight subjects + payload shapes the test asserts on)
- [DDR-007](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md) (`target_kind="subagent"` shape)
- [ADR-ARCH-027](../../../docs/architecture/decisions/ADR-ARCH-027-no-horizontal-scaling.md) (two-replica failover semantics)
- IMPLEMENTATION-GUIDE.md §3 (sequence) and §8 (acceptance set rollup)