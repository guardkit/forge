---
id: TASK-FWD-002
title: "Observer exits on identity-unresolved without arming the deadline timer (no synthetic build-failed)"
status: in_review
created: 2026-07-04T11:00:00Z
updated: 2026-07-09T00:00:00Z
priority: high
task_type: feature
tags: [wire-dispatch, lifecycle-bridge, found-2026-07-04, ws3-s6]
complexity: 4
---

> **✅ DONE 2026-07-09 (WS3-S6).** Observer-owned fix (NOT the blanket
> bridge deadline timer — that 300s "no terminal within budget" timer is
> never cancelled on stream activity, so wiring it in prod would false-fail
> every build running >5min, e.g. FEAT-3ED2 at 74min; that is why it was
> left unwired). On identity-unresolved the observer now polls to the
> per-build deadline (`_await_identity_until_deadline` — a slow dispatch is
> still picked up) and, if still unresolved, publishes a synthetic
> `build-failed` (reason `identity-unresolved`) + acks + detaches
> (`_publish_identity_unresolved_failure`). Payload built by the translator's
> new `build_synthetic_failed` factory (AC-2: the wireup never constructs
> payloads). Durable build_id via an injected SQLite resolver
> (`_build_build_id_resolver` in `_serve_production`, threaded through the
> parts) so the terminal write hits the real queued row. AC-1/AC-2 met;
> `TestIdentityUnresolvedPublishesBuildFailed` (4 tests) green + full
> lifecycle_bridge suite (244) green. Publish-failure leaves the message
> un-acked (redelivery/next-boot retry).

# Arm the deadline timer when identity resolution fails

`wireup._wait_for_identity` gives up after 3 attempts and the observer exits
WITHOUT arming the per-attach deadline timer, so the promised synthetic
build-failed never publishes. Combined with redelivery this is a silent
infinite loop: the operator's phone shows build-queued then nothing (exactly
the "notification path failure masks a stuck build" risk in the UBS scope §6).

## Acceptance Criteria
- [ ] Identity-unresolved path arms the same deadline timer as the
      sidecar-unreachable path; synthetic build-failed publishes at deadline.
- [ ] Integration test: identity 404s -> deadline fires -> build-failed envelope.
- [ ] All modified files pass project-configured lint/format checks with zero errors
