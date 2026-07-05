---
id: TASK-GATE-D659
title: "Gate activation: real build pauses at an approval gate, phone round-trip, restart-safe"
status: in_review
created: 2026-07-05T16:45:00Z
updated: 2026-07-05T21:20:00Z
previous_state: design_approved
state_transition_reason: "Phase 5 complete: all 3 waves implemented, quality gates passed (in_review)"
design:
  status: approved
  approved_at: "2026-07-05T18:05:00Z"
  approved_by: human
  implementation_plan: docs/state/TASK-GATE-D659/implementation_plan.md
  implementation_plan_version: v2
  architectural_review_score: 66
  complexity_score: 8
  execution_shape: "one task, three internally-green waves (operator choice at checkpoint)"
  design_notes: "3-designer/3-judge panel -> predispatch + R1/R2 repairs + grafts; arch review C1/C2/M1/M2 folded"
priority: critical
task_type: feature
tags: [gating, approval, feat-bf39-v1.1, jnb-107-blocker, feat-1872]
feature: FEAT-1872
depends_on: [TASK-JNB-101, TASK-JNB-102, TASK-JNB-106]
blocks: [TASK-JNB-107]
complexity: 8
complexity_evaluation:
  score: 8
  level: complex
  factors:
    - name: requirements_complexity
      score: 3
      justification: "Five coupled work items across dispatch, persistence, recovery, and runner seams"
    - name: pattern_complexity
      score: 2
      justification: "Cross-process activation-point decision; protocol-adapter composition over existing SQLite facades"
    - name: risk_level
      score: 2
      justification: "Touches the dispatch/ack path (JetStream redelivery) and the restart-recovery contract"
    - name: dependencies
      score: 1
      justification: "No new libraries; all seams exist in-repo"
  breakdown_suggested: true
  breakdown_accepted: null
  user_decision: create_as_is
  user_justification: "Operator brief: single task first; /task-work --design-only decides the split (via /feature-plan if >1 task)"
test_results:
  status: passed
  coverage: "AC-mapped scenario suites (no numeric coverage run); ~2,680 test LOC"
  last_run: 2026-07-05T21:18:00Z
  summary: "2986 passed, 4 skipped (tests/forge + tests/cli); 3 new gate suites green; 8 integration failures are pre-existing external-infra (Postgres/Docker/live-NATS-auth), not this task"
implementation:
  waves_completed: [1, 2, 3]
  review: "Phase 5 code-review: 1 critical + 2 major + 2 minor found, ALL fixed with regression tests (bounded rearm arm-wait, hold-slot on publish/hop failure, status-guarded refresh, dead emit_resumed branch removed)"
  plan_audit: docs/state/TASK-GATE-D659/plan_audit.md
  new_src: [gating/sqlite_adapters.py, gating/degraded.py, cli/_serve_gate_activation.py, cli/_cancel_gate_inject.py]
  git: "uncommitted — working tree only (commit deferred to operator)"
---

# Task: Gate activation — real build pauses at an approval gate, phone round-trip, restart-safe

## Description

The sole remaining CODE blocker before TASK-JNB-107 (live approve/reject from
the phone). Both v1.1 code halves shipped (forge JNB-101/102/106; jarvis
JNB-103/104/105), but **nothing in the live forge daemon ever pauses a build**:
`gate_check` (`src/forge/gating/wrappers.py:423`) and
`SqliteLifecyclePersistence.mark_paused` (`src/forge/lifecycle/persistence.py:844`
— the only producer of `BuildState.PAUSED`) have zero production callers, and
the autobuild runner graph never enters `awaiting_approval`
(`src/forge/subagents/autobuild_runner.py:1584-1604`). This task makes a real
forge build pause, round-trip an operator decision, and recover across a
daemon restart — proven by scenario tests over the production wiring.

## Ground truth (verified 2026-07-05, 10-agent sweep + orchestrator checks)

Authoritative baseline — do NOT re-derive; anchors are working-tree (the
uncommitted TASK-FWD-004 revert shifts autobuild_runner.py by -7 vs older docs).

1. **Process topology (crux):** the runner graph executes in a SEPARATE OS
   process — `langgraph dev` sidecar on :8124 (`FORGE_AUTOBUILD_RUNNER_URL`;
   boot fail-fast without it, `_serve_production.py:466-477`). The sidecar has
   NO NATS client, NO forge.db access, and cannot reach daemon module globals
   (`_serve_deps_gating._bound_gate_parts`). Launch boundary is JSON-only
   (`_serve_async_task_starter.py:131-140`). The F010G "in-process ASGI"
   docstrings are stale history (superseded by F010I/F010J).
2. **`running_wave` is one node running the whole `guardkit autobuild feature`
   subprocess** — no per-wave seam is observable anywhere daemon-side;
   `coach_score` is never populated (ADR-ARCH-033 known gap; UBS-002 prereq).
3. **The bridge already translates a runner-side `awaiting_approval` lifecycle**
   into BuildPaused/BuildResumed envelopes (`translation.py:478-490`) — but no
   graph node ever writes that lifecycle.
4. **`reconcile_on_boot` is fully implemented** (`recovery.py:353`, PAUSED
   re-emit with verbatim `pending_approval_request_id` via
   `build_recovery_approval_envelope`); serve.py calls the seams at boot
   (`serve.py:722-723`) but they are bound to warning stubs (`serve.py:117-151`).
   Only the composition-root binding is missing.
5. **Production SQLite pause machinery exists — compose, don't rebuild:**
   `mark_paused` (atomic PAUSED + request_id), `SqliteBuildCanceller`
   (idempotent on terminals), `SqliteBuildResumer`, `SqliteStageSkipRecorder`,
   stage_log with gate columns + `details_json`. Gaps: `record_decision` →
   `details_json["gate"]` writer, `list_paused_builds`/`PausedBuildSnapshot`
   backing (no `paused_builds` view/table exists), attempt_count durable home
   (parseable from `derive_request_id` format), sync→async shims, and the
   single-owner rule: exactly ONE adapter may own each SQL transition or
   `apply_transition`'s optimistic-concurrency check fails.
6. **Post-restart response consumer does NOT exist (hidden work item):** the
   only subscriber to `agents.approval.forge.{build_id}.response` lives inside
   a live `await_response` frame; all three recovery paths are publish-only.
   After a restart, an operator tap is dropped with zero subscribers and the
   build stays PAUSED forever. Resume/cancel side effects live exclusively in
   `_dispatch_response` (`wrappers.py:786-911`).
7. **Recovery envelope correlation landmine:** `build_recovery_approval_envelope`
   does not stamp `BuildRow.correlation_id` (`approval_publisher.py:355-359`),
   while the live path does; the subscriber's step-2b guard refuses mismatches.
8. **Jarvis needs BOTH envelopes for phone buttons:** PIPELINE
   `pipeline.build-paused.{feature_id}` triggers the Slack post; the AGENTS
   approval request supplies the buttons (joined on build_id = 4th subject
   token). Approval request alone → no post; build-paused alone → buttonless.
   NOTE: JNB-101's "C4 dissolved — no daemon-side build-paused emit" was scoped
   to JNB-101's ACs; a daemon-side activation point MUST also emit build-paused
   or the phone never shows the pause.
9. **Recovery contract disagreement to resolve:** implemented `_handle_paused`
   re-emits only the approval request (matches API-sqlite-schema §6);
   API-nats-pipeline-events §4 + FEAT-FORGE-010 Gherkin require a build-paused
   re-emit too. Without it, a restart mid-pause never re-posts the phone message.
10. **Minimal honest toy pause (ADR-ARCH-019-compliant):** readers return `[]`
    (rules list is runtime-inert; constitutional enforcement is the hardcoded
    frozenset), coach_score=None (degraded mode), and either
    `target_identifier="review_pr"` (constitutional → MANDATORY, reasoning
    model never invoked) or a static-JSON reasoning callable. No production
    PriorsReader/AdjustmentsReader/RulesReader/reasoning_model_call exists.
11. **Recorded risks assigned to this task** (docs/state/TASK-JNB-102/plan_audit.md):
    CLI-cancel-vs-live-gate optimistic-concurrency race (uncaught RuntimeError
    inside gate_check); CLI-cancel double-emit once a real injector is wired.
12. **C1 guard bug** (`autobuild_runner.py:556-605`): `emit_resumed` requires
    `_last_lifecycle=="awaiting_approval"`, so `mark_resume_pending()` on a
    fresh adapter never fires. JNB-101 assigned the fix to the runner-side
    pause activation task; the adapter has zero production constructors.
13. **DF-007** ("gates travel with the agent, not the caller") is RESERVED with
    no body text anywhere in the fleet; this task plausibly IS its filing
    trigger. Nearest precedent: DF-009 (gate-property framing). Confirm with
    operator before filing.

## Design decisions to resolve in /task-work --design-only (BEFORE implementing)

- **D1 — Activation point** (the crux). Honest options mapped by the sweep:
  (A) daemon-side pre-dispatch gate inside the dispatch flow (full daemon
  context; reuses the entire tested gate_check path; build-resumed-before-
  build-started semantic wrinkle; long await inside the JetStream consumer
  callback — ack_wait/redelivery posture must be designed, noting
  FEAT-FORGE-010 Gherkin holds the queue slot un-acked while paused);
  (B) daemon-side post-runner (gates the outcome, not execution; coach_score
  still None; blocks terminal ack); (C) runner-graph gate node (cannot reach
  gate parts — requires sidecar NATS, violating the FMDR topology rule, or
  ADR-ARCH-021 interrupt() with undocumented sidecar checkpointer semantics);
  (D) bridge-mediated hybrid (runner writes `awaiting_approval`, daemon
  observes and runs the pause half). CGCP/TASK-CGCP-010 intent ≈ daemon-side
  stage gate; DDR-007/FEAT-FORGE-010 intent ≈ runner-side. Neither is wired;
  the design must pick, justify against both intents, and honour
  ADR-ARCH-019 (no static gate-stage registry).
- **D2 — Who emits `build-paused`/`build-resumed` on PIPELINE** for a
  daemon-side gate (emitter exists in-daemon; ordering vs the AGENTS publish;
  jarvis dual-envelope join per ground truth #8).
- **D3 — Adapter shapes**: GateRepository/StateMachine over the existing
  facades (mapping table in ground truth #5), incl. which adapter owns each
  SQL transition, `transition_to_paused`'s missing request_id (capture from
  record_paused_build vs re-derive), decision durability
  (`details_json["gate"]` vs new table/view), and stage_label choice
  (`"autobuild"` round-trips `SqliteBuildSnapshotReader`).
- **D4 — Boot recovery binding + post-restart response consumer**: bind
  `recovery_reconcile_on_boot` (deps all exist in `_compose`); resolve ground
  truth #9's contract disagreement; fix the correlation landmine (#7); and
  design WHO consumes the response after restart (re-armed per-build await
  with rebased window vs standing daemon subscription on
  `agents.approval.forge.*.response`) — interacts with DDR-027 in-memory
  posture and JNB-107 scenario 5 (REASON_MAX_WAIT breach).
- **D5 — C1 guard bug disposition**: fix the guard vs deprecate/delete the
  dead adapter path (depends on D1; if daemon-side wins, the adapter stays
  unconstructed — a fixed-but-dead mechanism vs honest removal).
- **D6 — Recorded risks** (#11): address or consciously defer with rationale.
- **D7 — DF-007**: draft the fleet decision (gate-property framing per
  DF-009) for operator sign-off, or record why deferred.

## Acceptance Criteria

- [x] A real forge build dispatched through the production daemon reaches a
      gate and enters PAUSED (SQLite `builds.status=PAUSED` +
      `pending_approval_request_id` set) with `ApprovalRequestPayload`
      published on `agents.approval.forge.{build_id}` AND `BuildPausedPayload`
      on `pipeline.build-paused.{feature_id}` (jarvis dual-envelope contract).
- [x] APPROVE (decided_by string-equal `expected_approver`, default `"rich"`)
      resumes the build: PAUSED→RUNNING, exactly one `build-resumed` on the
      wire (decision/responder real values), work proceeds.
- [x] REJECT cancels: CANCELLED in SQLite FIRST, then `build-cancelled`
      (JNB-102 seam) — zero `build-resumed` envelopes.
- [x] Window expiry (REASON_MAX_WAIT) cancels with `build-cancelled`.
- [x] Daemon restart mid-pause: boot re-emits the approval request with the
      VERBATIM persisted request_id (+ correlation_id stamped), the operator
      can still decide, and the decision is consumed and drives the
      transition (post-restart response consumer exists).
- [x] Spoofed/mismatched responder, correlation, or stale request_id →
      refused, zero transitions, zero emits (four-step chain intact).
- [x] `gate_check` gains its first production call site; gating stack config
      remains ADR-ARCH-019-compliant (no static stage registry) and
      ADR-ARCH-026-compliant (constitutional targets force MANDATORY).
- [x] C1 disposition implemented per approved design (fix or removal — no
      silently-dead documented mechanism remains).
- [x] `expected_approver` stays pinned `"rich"` unless deliberately re-pinned
      with operator notification (OPS-001 alignment).

## Test Requirements

- [x] Scenario tests over the PRODUCTION wiring (template:
      `tests/integration/test_jnb101_production_wiring.py` — real parts over
      `InMemoryNats`, real `PipelineLifecycleEmitter`, order-log asserting
      envelope-before-transition), covering every AC above including the
      restart scenario (kill/recreate composition, re-arm, decide, resume).
- [x] SQLite adapter tests against a tmp DB proving semantic parity with the
      in-memory fakes (`tests/integration/conftest.py:226,299`), incl.
      idempotency and the single-transition-owner rule.
- [x] Boot-binding test: serve composition binds `recovery_reconcile_on_boot`
      (no warning stub in production wiring).
- [x] Suite stays green under the scoped pytest-9 baseline (see live-validation
      handoff §6 flags); new code passes the clock-hygiene guard
      (`Clock.now()`, never `datetime.now()`).

## References (read before designing)

- `docs/state/TASK-JNB-101/implementation_plan.md` (ground truth, C1/C2 kill,
  §Follow-ups) + both `docs/state/TASK-JNB-10{1,2}/plan_audit.md` risks
- `docs/research/ideas/ubs-003-v1.1-live-validation-handoff-2026-07-05.md`
  (incl. the 2026-07-05 correction block)
- `jarvis/docs/handoff/jnb-live-roundtrip-handoff-2026-07-05.md` (live-run
  authority; dual-envelope + button contracts)
- Contracts: `API-nats-approval-protocol.md`, `API-nats-pipeline-events.md`,
  `API-sqlite-schema.md` §6 (recovery matrix; note the §9 disagreement)
- ADR-ARCH-019/021/026/031/033, DDR-007, DDR-027; TASK-CGCP-010 (activation
  intent); FEAT-FORGE-010 Gherkin :133-141, :245-249
- `../ai-transition/docs/decisions/REGISTER.md` (DF-007 RESERVED; DF-009
  precedent)

## Implementation Notes

Implemented via `/task-work --implement-only` (all 3 waves, one session) against
the approved v2 plan. Daemon-side PRE-DISPATCH gate (D1): `maybe_gate_build`
runs `gate_check` after `record_pending_build`, before launch, in a degraded/
honest posture (empty readers + `degraded_dispatch_gate_model` → MANDATORY_
HUMAN_APPROVAL), so every dispatched build pauses for phone approval.

- **Wave 1** (foundations): `gating/sqlite_adapters.py` (repo+SM over the SQLite
  facades, shared `_PauseHandoff`, single-transition-owner, `StaleTransitionError`),
  `parse_request_id`, `await_and_dispatch` public refactor of the gate tail,
  `refresh_pending_approval_request_id` facade, recovery-envelope correlation
  stamp, `gating/degraded.py`.
- **Wave 2** (live round-trip — JNB-107 unblock): `maybe_gate_build` +
  idempotency pre-read, `_MirroredApprovalPublisher` (AGENTS request → PIPELINE
  build-paused), R1 deferred observer registration, R2 state-conditional ack,
  `_compose` wiring (`repository=None` to parts / SQLite repo to deps,
  `bridge_registry=None`), `ack_wait` pin.
- **Wave 3** (restart + closure): `rearm_paused_gates` (arm-before-post),
  boot-seam bindings (no-op ApprovalRepublisher; suppressed-PAUSED consumer
  twin seam), three-arm duplicate (INTERRUPTED→redispatch), C1 removal,
  CLI-cancel synthetic-reject injector, API-sqlite-schema §6 note, DF-007 draft.
- **Phase 5 review fixes**: bounded `rearm` arm-wait (was unbounded → boot-wedge
  risk); hold-slot on ApprovalPublishError / concurrent-terminal hop (was
  spurious build-failed + premature ack); status-guarded `refresh_pending`;
  dead `emit_resumed` branch removed.

Residuals / follow-ups: see `docs/state/TASK-GATE-D659/plan_audit.md`.
Cross-repo unverifiable-here items: JNB-107 live-run checklist (plan §Checklist),
GB10 durable `ack_wait` recreation. **DF-007 draft awaits operator sign-off.**
Changes are in the working tree only — **not committed** (deferred to operator).
