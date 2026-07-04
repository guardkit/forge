---
id: TASK-JNB-101
title: "forge: ApprovalSubscriber production wiring into the serve runtime"
status: backlog
created: 2026-07-03T15:30:00Z
updated: 2026-07-03T15:30:00Z
priority: high
task_type: feature
parent_review: TASK-REV-C951
feature_id: pending-v1.1
version: v1.1
wave: 7
repo: forge
implementation_mode: task-work
complexity: 7
dependencies: [TASK-JNB-004]
tags: [ubs-003, jarvis-notification-bridge, slack, v1.1]
---

# Task: forge: ApprovalSubscriber production wiring into the serve runtime

## Description

Construct `ApprovalSubscriber(ApprovalSubscriberDeps)` in the forge-serve composition root (`src/forge/cli/_serve_deps*.py`) and inject it as the already-typed `ApprovalGateDeps.subscriber` (`gating/wrappers.py:396`) so the existing `await_response` call sites (`wrappers.py` lines 556 and 801) consume `agents.approval.forge.{build_id}.response` through the complete, untouched validation chain: payload validation -> `decided_by` allowlist vs `expected_approver` -> `correlation_id` match -> `request_id` 300s dedup. Wire approve/override decision dispatch to the first-ever `autobuild_runner.mark_resume_pending` call sites so `build-resumed` emits on approval. Load `expected_approver` from forge config and pin it to the shared identity value jarvis will send as `decided_by` (this is a named config-alignment acceptance criterion).

This is the first v1.1 wave, hard-gated behind the live v1 checkpoint (TASK-JNB-004). The task is deliberately minimal in scope: construct the subscriber and inject it at the existing typed seam plus the `mark_resume_pending` call sites — the validation chain and decision dispatch are reused byte-for-byte. The `ApprovalSubscriber` binds the AGENTS stream (limits retention), where consumer overlap is legal, so this adds no second PIPELINE consumer and cannot trigger workqueue err-10100. On the jarvis side, a separate subscriber captures `ApprovalRequestPayload.request_id` per `build_id` (TASK-JNB-103) and a Socket Mode click publishes `ApprovalResponsePayload(request_id, decision approve|reject, decided_by=slack_decided_by)` to `approval_subject + '.response'` carrying the request's `correlation_id` (TASK-JNB-104); this task makes forge actually consume those replies in production. Window and expiry-race enforcement stay exclusively forge-side so a reply-vs-expiry race resolves in exactly one place: the 300s response window plus the 3600s max-wait ceiling must produce `transition_to_cancelled` (the cancelled *emit* onto NATS is a separate, serialized task, TASK-JNB-102, because both tasks edit `gating/wrappers.py`).

## Acceptance Criteria

- [ ] `ApprovalSubscriber` is constructed with `ApprovalSubscriberDeps` in the forge-serve composition root (`src/forge/cli/_serve_deps*.py`) and injected as `ApprovalGateDeps.subscriber` (`gating/wrappers.py:396`); no changes to the validation chain or `await_response` internals.
- [ ] The existing `await_response` call sites (`wrappers.py:556` and `wrappers.py:801`) consume `agents.approval.forge.{build_id}.response` end-to-end through the untouched four-step chain (payload validation -> `decided_by` allowlist vs `expected_approver` -> `correlation_id` match -> `request_id` 300s dedup).
- [ ] Approve/override decision dispatch calls `autobuild_runner.mark_resume_pending` (first production call sites) so `build-resumed` emits after an approval.
- [ ] `expected_approver` is loaded from forge config and set to the shared identity value jarvis publishes as `decided_by` (`slack_decided_by`); the alignment is asserted in a test (config-alignment AC — a mismatch silently refuses every phone approval).
- [ ] Integration test with in-memory NATS fakes: an approve reply resumes the build exactly once (a duplicate reply with the same `request_id` inside 300s is deduplicated and does not resume twice).
- [ ] Integration test: a reject reply transitions the build to CANCELLED.
- [ ] Integration test: a defer republishes the approval request with `attempt_count + 1` and a refreshed `derive_request_id`.
- [ ] Integration test: expiry of the 300s response window and breach of the 3600s max-wait ceiling each produce `transition_to_cancelled`.
- [ ] A reply with a non-allowlisted `decided_by`, a mismatched `correlation_id`, or a stale `request_id` is refused without any state transition.
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Test Requirements

Plain pytest only — NO pytest-bdd `.feature` glue (operator decision 2026-07-03; eliminates a known silent-false-green class). Test classes mirror the spec scenario names for the reply-path validation scenarios (approve-resumes-once, reject-cancels, defer-republish-with-refreshed-request-id, window-expiry-cancels, ceiling-breach-cancels, spoofed/mismatched-reply-refused, config-alignment). Use in-memory NATS fakes for the subscriber; drive `await_response` through the real injected `ApprovalGateDeps.subscriber` rather than mocking the chain. Run via `.venv/bin/python -m pytest` from the forge repo root.

## Implementation Notes

- **Dependency**: TASK-JNB-004 — LIVE V1 CHECKPOINT: toy feature Open WebUI -> phone queued->running->terminal. All v1.1 work is hard-gated behind this checkpoint passing live.
- **Highest-uncertainty task in the plan**: `await_response` has zero production call sites exercised today; the wiring must reconcile with forge-serve's current pause-and-park flow. It is deliberately isolated so slippage delays only v1.1 replies, never the v1 surface.
- **Scope discipline**: reuse the validation chain and decision dispatch byte-for-byte. The build-cancelled NATS emit onto the CANCELLED transitions is explicitly OUT of scope here — it belongs to TASK-JNB-102, sequenced after this task specifically to serialize `gating/wrappers.py` edits. Do not touch `pipeline_publisher.py:272` in this task.
- **No second PIPELINE consumer (err-10100)**: the `ApprovalSubscriber` binds the AGENTS stream, where limits retention permits consumer overlap; the workqueue single-consumer rule applies to the PIPELINE stream only and this task must not add any PIPELINE consumer.
- **DDR-007 (never-regress / best-effort messaging)**: SQLite state is authoritative; messaging failures must never raise into the approval-gate flow.
- **DDR-027 (no-replay)**: dedup and pending state are in-memory; the `request_id` 300s dedup window is the authoritative backstop against duplicate replies, including jarvis-side first-click-wins races.
- **Correlation-INDEPENDENT fan-out is deliberate** on the jarvis notification side; the reply path here, by contrast, requires an exact `correlation_id` match — the asymmetry is intentional and must not be "fixed".
- **APPROVER_IDENTITY contract (this task is the producer)**: TASK-JNB-104 consumes the config string equality `forge expected_approver == jarvis slack_decided_by` (pydantic-settings on the jarvis side). It is exact string match; a mismatch silently refuses every phone approval with no error surfaced to the operator. Document the configured value clearly (config key and expected shared value) so TASK-JNB-104 and the live probe in TASK-JNB-107 can verify alignment.
- **Window/expiry-race ownership**: enforcement stays exclusively forge-side (validated further in TASK-JNB-106) so a reply-vs-expiry race resolves in exactly one place to one outcome.
