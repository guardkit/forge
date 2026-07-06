---
id: TASK-JNB-109
title: "Envelope-subscribe adapter: fix the build gate's approval reply path over the raw NATS client"
status: in_review
created: 2026-07-06T20:45:00Z
updated: 2026-07-06T21:15:00Z
priority: high
task_type: implementation
repo: forge
complexity: 3
dependencies: []
blocks: [TASK-JNB-107]
tags: [jnb, approval-loop, found-2026-07-06, task-mp-012-followup]
---

# Task: Envelope-subscribe adapter for the build-gate reply path

## Why

TASK-MP-012's review PoC'd a defect class on the fleet watcher — a consumer
written against the envelope-aware `nats_core.NATSClient` subscribe contract
being fed the RAW `nats.aio` client — and code-reading showed the SAME latent
defect live in the build gate since TASK-JNB-101: `build_approval_gate_parts`
and `rearm_paused_gates` thread the raw daemon client into
`ApprovalSubscriberDeps.nats_client`, but `ApprovalSubscriber` calls
`subscribe(subject, callback)` positionally (callback binds to the raw
client's `queue: str` parameter → TypeError) and expects parsed
`MessageEnvelope`s. **The phone-approval reply path could never receive a
response live.** Consistent with JNB-107's live round-trip being the one
thing never validated.

Rich's decision (2026-07-06): fix now, before the JNB-107 live round-trip,
so one GB10 run validates the final contract for both build and planning.

## Implemented (2026-07-06, TASK-MP-012 follow-up session)

- `src/forge/adapters/nats/envelope_subscribe.py` — shared
  `EnvelopeSubscribeClient` (promoted from `_serve_planning.py`'s private
  adapter): parses raw `Msg` → validated `MessageEnvelope`, WARN-drops
  malformed payloads, optional armed-event, `cb=`/positional fallback.
- Wired into `_serve_deps_gating.build_approval_gate_parts` (subscriber only;
  publisher/injector keep the raw client — core publish is compatible).
- Wired into `_serve_gate_activation.rearm_paused_gates`
  (`_ArmSignallingClient(EnvelopeSubscribeClient(client), armed)`).
- Mode P's composition now imports the shared adapter (no duplicate).
- **Regression tests** `tests/forge/adapters/test_envelope_subscribe.py`:
  the fake mimics the PRODUCTION nats-py signature
  (`subscribe(subject, queue="", cb=None)`, raw `Msg` delivery) — the
  test-shape rule that prevents this defect class from going green again.
  Includes an end-to-end pin: `build_approval_gate_parts`-composed
  `ApprovalSubscriber.await_response` resolves a jarvis-shaped response
  over the raw-signature client.

## Verification

- 5 new regression tests green; all 86 existing gating/approval tests green
  (adapter is transparent to envelope-delivering fakes).
