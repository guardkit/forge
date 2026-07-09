# `agents.>` approval-request publish ack-posture — JNB-111 AC5 discharge

**Date:** 2026-07-09 · **Session:** WS3-S6 (forge dispatch-reliability bundle) ·
**Carry-item:** jarvis `TASK-JNB-111` AC5 ("cross-repo note for forge — carry to
next forge session") · **Verdict:** ✅ **CONFORMS — no code change.**

## The check

JNB-111 (jarvis) found that jarvis's `slack_reply` published approval
**responses** via `js.publish` to the `agents.>` AGENTS stream, which is
configured `no_ack: true` — so a JetStream publish STORES the message but the
PubAck never arrives, `js.publish` raises `TimeoutError`, and the caller
mis-reports a stored publish as a failure. jarvis fixed this by switching to
core `nc.publish` (+ bounded flush), matching forge's `ApprovalPublisher`.

AC5 carried a verification to the next forge session: **MP-012's planning
checkpoint/escalation publishes approval REQUESTS on `agents.>` subjects —
confirm forge uses the acked-publisher seam / CORE publish (the
`ApprovalPublisher` pattern), NOT a raw `js.publish` that would hit the same
no-PubAck trap.**

## Evidence (this session, read-only)

1. **Forge-wide sweep — zero `js.publish` sites.**
   `grep -rn "js\.publish" src/forge` → **no matches.** The no-PubAck trap
   that bit jarvis's `slack_reply` does not exist anywhere in forge.

2. **MP-012's planning approval-request publish uses the core seam.**
   The planning driver / checkpoint publish approval **requests** via
   `publisher.publish_request(envelope)`
   ([`planning/checkpoint.py:263`](../../../src/forge/planning/checkpoint.py#L263),
   [`planning/driver.py:817`](../../../src/forge/planning/driver.py#L817)).
   In production ([`cli/_serve_planning.py:649`](../../../src/forge/cli/_serve_planning.py#L649))
   the publisher is `ApprovalPublisher(nats_client=nats_client)` wrapped by
   `_PlanningPausePublisher`, whose `publish_request`
   ([`cli/_serve_planning.py:365`](../../../src/forge/cli/_serve_planning.py#L365))
   delegates to the inner `ApprovalPublisher.publish_request` — which publishes
   via **core** `self._nc.publish(subject, body)`
   ([`adapters/nats/approval_publisher.py:487`](../../../src/forge/adapters/nats/approval_publisher.py#L487)).
   The pause-mirror leg publishes `pipeline.build-paused.*` (the acked PIPELINE
   stream) also via `self._nc.publish` — correct as-is.

3. **The posture is a fleet convention, not an accident (LES1 parity rule).**
   All four forge publishers (`pipeline`, `deploy`, `runbook`, `approval`)
   publish via `self._nc.publish`. The pipeline publisher documents the rule
   verbatim: *"PubAck is informational only. JetStream may or may not return
   one depending on stream configuration; either way, do NOT treat this as
   proof of delivery (LES1 parity rule)."*
   ([`adapters/nats/pipeline_publisher.py:200-210`](../../../src/forge/adapters/nats/pipeline_publisher.py#L200-L210)).
   `ApprovalPublisher` conforms — it uses core publish and never relies on a
   PubAck. Persistence-at-publish is intentionally unconfirmed and is
   compensated by the durable SQLite pause mirror + boot `rearm_paused_gates`
   re-emit + (as of this session) the live refresh-on-timeout loop.

## Disposition

The check **passes** — MP-012's `agents.>` approval-request publish already uses
the CORE-publish `ApprovalPublisher` seam, the same convention JNB-111
converged jarvis onto. **No conversion to a fix is warranted** (the task's
condition: "convert to a fix ONLY if the check fails"). JNB-111 AC5 is
**DISCHARGED**.

A drive-by parity nit (non-blocking, not fixed): the other three publishers
capture the informational PubAck into a debug log line (`ack = await
self._nc.publish(...)`) while `ApprovalPublisher` discards it. This is cosmetic
— the convention is that the ack is non-load-bearing — and converting it would
be an unrequested change outside AC5's "fix only if the check fails" scope.
