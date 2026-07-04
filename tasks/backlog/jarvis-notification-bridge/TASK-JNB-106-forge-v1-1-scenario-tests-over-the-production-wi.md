---
id: TASK-JNB-106
title: "forge: v1.1 scenario tests over the production wiring"
status: backlog
created: 2026-07-03T15:30:00Z
updated: 2026-07-03T15:30:00Z
priority: high
task_type: testing
parent_review: TASK-REV-C951
feature_id: "pending-v1.1"
version: v1.1
wave: 9
repo: forge
implementation_mode: task-work
complexity: 5
dependencies: [TASK-JNB-101, TASK-JNB-102]
tags: [ubs-003, jarvis-notification-bridge, slack, v1.1]
---

# Task: forge: v1.1 scenario tests over the production wiring

## Description

Integration tests exercising the production-wired chain for the forge-owned v1.1 scenarios, using TASK-JNB-101's in-memory fakes: within-window approve resumes, after-window reply not applied, unrecognised decision refused and logged, wrong-correlation refused as an anomaly, duplicate response request_id-deduped, reply-after-terminal ignored, and the approve-vs-expiry race resolving to exactly one recorded outcome. Window enforcement is exclusively forge-side, so this suite is the single authoritative validation of window/expiry semantics for the entire notification bridge.

Architecture context: TASK-JNB-101 constructs `ApprovalSubscriber` + `ApprovalSubscriberDeps` in the forge-serve runtime and injects the subscriber as the already-typed `ApprovalGateDeps.subscriber` (gating/wrappers.py:396), so the existing `await_response` call sites (gating/wrappers.py:556 and 801) consume `agents.approval.forge.{build_id}.response` through the complete, untouched validation chain: payload validation -> `decided_by` allowlist vs `expected_approver` -> `correlation_id` match -> `request_id` 300s dedup. Approve/override dispatch wires the first-ever `autobuild_runner.mark_resume_pending` call sites. TASK-JNB-102 wires the existing `publish_build_cancelled` (pipeline_publisher.py:272) onto the three CANCELLED transitions — the reject branch (gating/wrappers.py:725-837), the REASON_MAX_WAIT breach (gating/wrappers.py:563-574), and `CliSteeringHandler.handle_cancel` — best-effort per DDR-007 (the SQLite ledger is authoritative). Because a reply-vs-expiry race must resolve in exactly one place to exactly one outcome, the race scenario here is the load-bearing test: it pins the single-locus property the whole v1.1 reply path relies on.

## Acceptance Criteria

- [ ] Every listed scenario (seven, enumerated under Test Requirements) has a named test whose class/method name maps to its Gherkin counterpart in the FEAT-UBS-003 spec.
- [ ] Within-window approve: a valid `ApprovalResponsePayload` arriving inside the wait window resumes the build (`mark_resume_pending` invoked; exactly one approved outcome recorded).
- [ ] After-window reply: a response arriving after the window has expired is not applied — no resume, the expiry outcome stands.
- [ ] Unrecognised decision: a payload whose decision is neither approve nor reject is refused and logged; no state transition occurs.
- [ ] Wrong `correlation_id`: refused and logged as an anomaly; no state transition occurs.
- [ ] Duplicate response: a second payload carrying the same `request_id` within the 300s dedup horizon is ignored; exactly one recorded outcome.
- [ ] Reply after terminal state: ignored without error and without state change.
- [ ] Approve-vs-expiry race: regardless of interleaving, exactly one outcome is recorded (window enforcement is exclusively forge-side; no jarvis-side arbitration exists or is simulated).
- [ ] A collect-only count assertion confirms all seven scenario tests are collected (guards against silent scenario loss).
- [ ] The suite runs green alongside the existing adapters/nats/approval_subscriber.py and gating/wrappers.py test modules — no fixture or naming collisions, full run green via `.venv/bin/python -m pytest`.

## Test Requirements

- Plain pytest ONLY — no pytest-bdd `.feature` glue anywhere (operator decision 2026-07-03; eliminates a known silent-false-green class).
- Test classes mirror the spec scenario names; one named test per scenario, mapped to its Gherkin counterpart.
- Explicit scenario list (each requires a named test):
  1. Within-window approve resumes the paused build.
  2. After-window reply is not applied.
  3. Unrecognised decision is refused and logged.
  4. Wrong `correlation_id` is refused as an anomaly.
  5. Duplicate response is `request_id`-deduped.
  6. Reply after terminal state is ignored.
  7. Approve-vs-expiry race resolves to exactly one recorded outcome.
- Collect-only count assertion requirement: include a check that `.venv/bin/python -m pytest <suite path> --collect-only -q` yields at least the seven scenario tests above, so a refactor cannot silently drop a scenario while the run stays green.
- Exercise the production-wired chain, not re-mocked internals: drive `await_response` through the injected `ApprovalGateDeps.subscriber` using TASK-JNB-101's in-memory fakes for the NATS subscription and clock — no live broker, no real JetStream consumer.
- Run from the forge repo root with `.venv/bin/python -m pytest` (the default interpreter lacks `nats_core`; the project venv is required).

## Implementation Notes

- Dependency summaries: TASK-JNB-101 — forge: ApprovalSubscriber production wiring into the serve runtime (the highest-uncertainty task in the plan; `await_response` had zero production call sites before it). TASK-JNB-102 — forge: emit build-cancelled on CANCELLED transitions (ASSUM-010 closure). Both edit gating/wrappers.py and were serialized (101 then 102); this task lands in wave 9 after both have merged.
- Workqueue err-10100 single-consumer rule: these tests must not bind (or fake in a way that implies) a second PIPELINE consumer. The ApprovalSubscriber consumes the AGENTS stream, where limits retention makes consumer overlap legal — keep the fakes faithful to that distinction.
- DDR-007 never-regress: the cancelled emit is best-effort — a failing `publish_build_cancelled` must never regress the SQLite-recorded CANCELLED transition. Where tests touch TASK-JNB-102's emit paths, assert the transition survives a publish failure rather than requiring the publish to succeed.
- DDR-027 no-replay: in-memory approval/dedup state is not replayed after a restart; do not write tests that expect dedup to persist across a simulated process restart.
- Correlation-INDEPENDENT fan-out is a deliberate jarvis-side semantic (the phone surface is per-operator, not per-session) and is out of scope here — do not "tighten" any test to demand correlation gating of notifications. Forge-side response validation, by contrast, DOES require the `correlation_id` match (scenario 4) — the two are different layers and must not be conflated.
- Window/expiry-race enforcement is exclusively forge-side, so scenario 7 in this suite is the sole authoritative coverage of that race anywhere in the bridge; make its interleaving control explicit (fake clock / ordered task scheduling), not sleep-based.
- The `decided_by` allowlist step compares against `expected_approver` from forge config; a mismatched value refuses silently by design (config alignment is pinned in TASK-JNB-101/104 and probed live in TASK-JNB-107) — fixtures here should set the two values equal except where a scenario deliberately exercises refusal.
