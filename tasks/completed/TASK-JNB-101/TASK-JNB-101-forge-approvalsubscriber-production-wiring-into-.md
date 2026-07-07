---
id: TASK-JNB-101
title: 'forge: ApprovalSubscriber production wiring into the serve runtime'
status: completed
created: 2026-07-03 15:30:00+00:00
updated: 2026-07-07T09:15:00Z
completed: 2026-07-07T09:15:00Z
completed_location: tasks/completed/TASK-JNB-101/
state_transition_reason: "Rollup 2026-07-07 (Rich's instruction, ops session): JNB-107 live validation complete 2026-07-07 (all four scenarios, Gate G1 PASS) validated the gate/approval chain; Mode P assumptions all 16 accepted by Rich 2026-07-07 (1909a40); MP-010 remains the live planning validation."
state_note: >-
  2026-07-05: moved backlog -> in_progress by the Fable forge-JNB session.
  The autobuild_state block below is a stale false-green from 2026-07-04
  (turn approved with "Files actual: 0" - nothing was built; verified in
  the jnb-live-roundtrip handoff). Implemented via interactive /task-work.
priority: high
task_type: feature
parent_review: TASK-REV-C951
feature_id: FEAT-1872
version: v1.1
wave: 7
repo: forge
implementation_mode: task-work
complexity: 7
dependencies:
- TASK-JNB-004
tags:
- ubs-003
- jarvis-notification-bridge
- slack
- v1.1
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-1872
  base_branch: main
  started_at: '2026-07-04T15:28:35.552865'
  last_updated: '2026-07-04T15:42:04.694149'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-07-04T15:28:35.552865'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: forge: ApprovalSubscriber production wiring into the serve runtime

## Description

Construct `ApprovalSubscriber(ApprovalSubscriberDeps)` in the forge-serve composition root (`src/forge/cli/_serve_deps*.py`) and inject it as the already-typed `ApprovalGateDeps.subscriber` (`gating/wrappers.py:396`) so the existing `await_response` call sites (`wrappers.py` lines 556 and 801) consume `agents.approval.forge.{build_id}.response` through the complete, untouched validation chain: payload validation -> `decided_by` allowlist vs `expected_approver` -> `correlation_id` match -> `request_id` 300s dedup. Wire approve/override decision dispatch to the first-ever `autobuild_runner.mark_resume_pending` call sites so `build-resumed` emits on approval. Load `expected_approver` from forge config and pin it to the shared identity value jarvis will send as `decided_by` (this is a named config-alignment acceptance criterion).

This is the first v1.1 wave, hard-gated behind the live v1 checkpoint (TASK-JNB-004). The task is deliberately minimal in scope: construct the subscriber and inject it at the existing typed seam plus the `mark_resume_pending` call sites — the validation chain and decision dispatch are reused byte-for-byte. The `ApprovalSubscriber` binds the AGENTS stream (limits retention), where consumer overlap is legal, so this adds no second PIPELINE consumer and cannot trigger workqueue err-10100. On the jarvis side, a separate subscriber captures `ApprovalRequestPayload.request_id` per `build_id` (TASK-JNB-103) and a Socket Mode click publishes `ApprovalResponsePayload(request_id, decision approve|reject, decided_by=slack_decided_by)` to `approval_subject + '.response'` carrying the request's `correlation_id` (TASK-JNB-104); this task makes forge actually consume those replies in production. Window and expiry-race enforcement stay exclusively forge-side so a reply-vs-expiry race resolves in exactly one place: the 300s response window plus the 3600s max-wait ceiling must produce `transition_to_cancelled` (the cancelled *emit* onto NATS is a separate, serialized task, TASK-JNB-102, because both tasks edit `gating/wrappers.py`).

## Acceptance Criteria

- [ ] `ApprovalSubscriber` is constructed with `ApprovalSubscriberDeps` in the forge-serve composition root (`src/forge/cli/_serve_deps*.py`) and injected as `ApprovalGateDeps.subscriber` (`gating/wrappers.py:396`); no changes to the validation chain or `await_response` internals.
- [ ] The existing `await_response` call sites (`wrappers.py:556` and `wrappers.py:801`) consume `agents.approval.forge.{build_id}.response` end-to-end through the untouched four-step chain (payload validation -> `decided_by` allowlist vs `expected_approver` -> `correlation_id` match -> `request_id` 300s dedup).
- [ ] Approve/override decision dispatch calls `autobuild_runner.mark_resume_pending` (first production call sites) so `build-resumed` emits after an approval.
- [ ] `expected_approver` is loaded from forge config and set to the shared identity value jarvis publishes as `decided_by` (`slack_decided_by`) — **the pinned value is `rich`** (operator-chosen 2026-07-04); the alignment is asserted in a test (config-alignment AC — a mismatch silently refuses every phone approval).
- [ ] Integration test with in-memory NATS fakes: an approve reply resumes the build exactly once (a duplicate reply with the same `request_id` inside 300s is deduplicated and does not resume twice).
- [ ] Integration test: a reject reply transitions the build to CANCELLED.
- [ ] Integration test: a defer republishes the approval request with `attempt_count + 1` and a refreshed `derive_request_id`.
- [ ] Integration test: expiry of the 300s response window and breach of the 3600s max-wait ceiling each produce `transition_to_cancelled`.
- [ ] A reply with a non-allowlisted `decided_by`, a mismatched `correlation_id`, or a stale `request_id` is refused without any state transition.
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Test Requirements

Plain pytest only — NO pytest-bdd `.feature` glue (operator decision 2026-07-03; eliminates a known silent-false-green class). Test classes mirror the spec scenario names for the reply-path validation scenarios (approve-resumes-once, reject-cancels, defer-republish-with-refreshed-request-id, window-expiry-cancels, ceiling-breach-cancels, spoofed/mismatched-reply-refused, config-alignment). Use in-memory NATS fakes for the subscriber; drive `await_response` through the real injected `ApprovalGateDeps.subscriber` rather than mocking the chain. Run via `.venv/bin/python -m pytest` from the forge repo root.

## Implementation Deviations (recorded 2026-07-05, interactive /task-work)

1. **AC-3 mechanism replaced (intent fully satisfied).** AC-3 names
   `autobuild_runner.mark_resume_pending` as the resume-emit mechanism. The
   pre-implementation architectural review (scored the v1 plan 64/100)
   proved that mechanism broken for its own cited scenario:
   `LifecycleEmitterAdapter`'s routing guard requires
   `_last_lifecycle == "awaiting_approval"`, which a freshly-constructed
   adapter (the daemon-restart case `mark_resume_pending` exists for) never
   has — and the adapter path is dead in production (never constructed;
   `lifecycle_emitter` stripped from the sidecar launch payload). The
   intent — `build-resumed` emitted on approve/override dispatch, exactly
   once, with real decision/responder values — is satisfied via the
   subscriber's own FW10-010 seam: `make_gate_check_deps` binds the daemon's
   `PipelineLifecycleEmitter` + `BuildContext` + `expected_correlation_id`
   into every `await_response` call (`_BoundContextSubscriber`). The emit is
   awaited BEFORE the wait loop returns (on the wire before PAUSED→RUNNING)
   and gained a decision gate: only approve/override emit — a reject would
   otherwise have rendered resumed-then-cancelled on the phone. The
   `mark_resume_pending` guard bug is a documented follow-up for the
   runner-side pause-activation task.
2. **wrappers.py gained additive correlation threading (review-driven).**
   The outbound `ApprovalRequestPayload` envelope previously carried no
   correlation_id, so jarvis had nothing to echo and the four-step chain's
   correlation step was inert against real traffic. Added
   `GateCheckDeps.correlation_id` (default None) + `correlation_id`
   parameter on the envelope builder, stamped at all three publish sites
   (pause, defer republish, boot recovery — recovery uses the persisted
   snapshot's value). Validation-chain logic and `await_response` internals
   untouched.
3. **Latent activation (in-scope boundary).** `gate_check` still has no
   production caller — this task constructs and injects the seam
   (`ApprovalGateParts` bound in serve's `_compose`); the activation point
   (plus production SQLite GateRepository/StateMachine adapters and the
   `reconcile_on_boot` binding) are documented follow-ups in
   `docs/state/TASK-JNB-101/implementation_plan.md`.
4. **Review-process note.** The multi-lens review workflow's contract lens
   and all adversarial verifiers hit a Fable usage limit mid-run
   (2026-07-05); 3/4 lenses completed and their 10 raw findings were
   verified inline by the session (7 fixed — bridge-probe correlation match,
   newest-row refresh selection, correlation threading, override-quadrant
   test, serve-compose seam tests + DDR-007 soft-fail guard, refresh-closure
   tests, 5s test budgets; 3 assessed not-defects — black-vs-ruff formatter
   letter, latent activation, and the task-file recording gap which this
   section closes).

## Implementation Notes

- **Dependency**: TASK-JNB-004 — LIVE V1 CHECKPOINT: toy feature Open WebUI -> phone queued->running->terminal. All v1.1 work is hard-gated behind this checkpoint passing live.
- **Highest-uncertainty task in the plan**: `await_response` has zero production call sites exercised today; the wiring must reconcile with forge-serve's current pause-and-park flow. It is deliberately isolated so slippage delays only v1.1 replies, never the v1 surface.
- **Scope discipline**: reuse the validation chain and decision dispatch byte-for-byte. The build-cancelled NATS emit onto the CANCELLED transitions is explicitly OUT of scope here — it belongs to TASK-JNB-102, sequenced after this task specifically to serialize `gating/wrappers.py` edits. Do not touch `pipeline_publisher.py:272` in this task.
- **No second PIPELINE consumer (err-10100)**: the `ApprovalSubscriber` binds the AGENTS stream, where limits retention permits consumer overlap; the workqueue single-consumer rule applies to the PIPELINE stream only and this task must not add any PIPELINE consumer.
- **DDR-007 (never-regress / best-effort messaging)**: SQLite state is authoritative; messaging failures must never raise into the approval-gate flow.
- **DDR-027 (no-replay)**: dedup and pending state are in-memory; the `request_id` 300s dedup window is the authoritative backstop against duplicate replies, including jarvis-side first-click-wins races.
- **Correlation-INDEPENDENT fan-out is deliberate** on the jarvis notification side; the reply path here, by contrast, requires an exact `correlation_id` match — the asymmetry is intentional and must not be "fixed".
- **APPROVER_IDENTITY contract (this task is the producer)**: TASK-JNB-104 consumes the config string equality `forge expected_approver == jarvis slack_decided_by` (pydantic-settings on the jarvis side). It is exact string match; a mismatch silently refuses every phone approval with no error surfaced to the operator. Document the configured value clearly (config key and expected shared value) so TASK-JNB-104 and the live probe in TASK-JNB-107 can verify alignment. **The pinned shared value is `rich`** (operator-chosen 2026-07-04); forge config default and the jarvis `slack_decided_by` must both be exactly `rich`.
- **Window/expiry-race ownership**: enforcement stays exclusively forge-side (validated further in TASK-JNB-106) so a reply-vs-expiry race resolves in exactly one place to one outcome.
