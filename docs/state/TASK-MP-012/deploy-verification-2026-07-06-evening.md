# forge-prod redeploy verification — 2026-07-06 ~21:08 UTC (JNB-109 fix live)

Redeploy of forge-prod onto the MP-012 + JNB-109 code (clean worktree build of
`1ea8209`; main checkout had TASK-MP-013 WIP so was not used). All claims
verified live on the GB10 at ~21:45 UTC:

```
image   = sha256:43402d07226be45974d705c653039579d1296975cf3178c534344d9b1e51ecce
started = 2026-07-06T21:08:42Z        health = healthy
forge-serve: applied 1 SQLite migration(s) at boot            <- schema_v3
recovery complete: interrupted=0 paused_reissued=9 skipped=24 warnings=0 failures=0
approval gate parts composed (expected_approver='U03QR8WKT29' ...)  <- member ID (identity contract v2)
PIPELINE forge-serve durable: filter pipeline.build-queued.*, Ack Wait 1h0m0s (unchanged)
forge-serve-planning durable: correctly ABSENT (planning.enabled=False)
```

Backup: `~/forge-prod-state/.forge.bak-20260706-pre1ea8209` (container stopped
first — WAL checkpointed, single consistent forge.db). Rollback:
`docker tag forge:gate-nc050 forge:latest && docker compose ... up -d` +
restore the backup dir.

Config folded in: `~/forge-state/forge.yaml` now sets
`approval.expected_approver: U03QR8WKT29` (Rich's Slack member ID — the value
jarvis publishes as `decided_by` once JNB-110 is deployed). Code default
`"rich"` untouched, per the JNB-110 decision.

## Live findings at verification time

1. **The 9 `paused_reissued` builds are today's CLI-triggered toy builds**
   (13:20–20:20, all repo guardkit/forge, random FEAT-suffix ids:
   076F33, 8E78A9, BFA49D, 966908, DA5279, 9D562A, CB1195, 0A718F, 798741) —
   gated test dispatches that accumulated PAUSED because the pre-JNB-109
   reply path could never complete them. Ledger also holds 24 QUEUED
   (recovery skipped) and 7 CANCELLED. Nothing precious: triage = bulk
   `forge cancel`, or keep one or two as JNB-107 reject/window-breach fodder.
2. **jarvis could NOT deliver the re-issued prompts**: since 21:05, 10
   `slack_approval_request_captured` but 13 `slack_delivery_failed` with
   Slack API `not_in_channel` — the bot lost its `#forge-builds` membership,
   almost certainly during the OPS-001 token rotation (revoke + reinstall).
   Token auth itself is fine. Jarvis re-parked the requests correctly
   (JNB-103 behavior). **Operator: `/invite` the bot back into
   `#forge-builds`** — this was the handoff §4 "perishable prereq".
3. **Do not tap any approval prompt until jarvis is redeployed on JNB-110**:
   the running jarvis process (started 15:34, pre-JNB-110 code) still
   publishes `decided_by="rich"`, while forge now expects `U03QR8WKT29` —
   every tap would be silently refused. Sequence: re-invite bot → push
   jarvis JNB-110 (`590fb72`, review in flight) → restart jarvis-serve-nats
   (stop; sleep 10; start — the JNB-108 fix is in that commit too) → restart
   forge-prod (rearm re-emits the paused prompts) → taps resolve.

## Addendum 2 (2026-07-06 ~21:50 UTC) — live queue-freeze incident + tap-window fix

1. **FWD-003-class freeze observed live:** the 21:28 UTC `docker restart`
   killed the old daemon mid-dispatch of a just-delivered build-queued
   message (stream seq 127, FEAT-96A391 redelivery). The delivery was never
   acked; with `max_ack_pending=1` + `ack_wait=1h` the broker then refused to
   deliver ANYTHING on the build-queued filter until the ack window expires
   (~22:28:32 UTC) — `Waiting Pulls: 1`, `Unprocessed: 1`, no deliveries, and
   a second restart provably does not clear it (broker-side timer). Every
   deploy restart therefore risks a silent 1-hour dispatch freeze if a
   message is in flight. Fold this into TASK-FWD-003's scope: drain/ack
   in-flight dispatches on shutdown, or reduce ack_wait exposure, or handle
   restart-in-dispatch explicitly.
2. **Tap-window fix:** every gated prompt so far died at `default_wait=300s`
   → terminal TIMED_OUT (no escalation configured) before a human could tap
   — 10 unattended window-breach validations, zero completed round-trips.
   `~/forge-state/forge.yaml` now sets `approval.default_wait_seconds: 1800`;
   boot-verified `default_wait=1800s`, `expected_approver='U03QR8WKT29'`
   unchanged (restart at 21:46:50 UTC was safe: the frozen slot's timer is
   broker-side and no new delivery was in flight).
3. A fresh smoke dispatch (`smoke-bbf0ffcf…`, PIPELINE seq 132) is queued
   behind the freeze; expected to dispatch ~22:28-22:30 UTC → gate pause →
   phone prompt with the 30-minute window. The stale seq-127 redelivery
   lands first and should dedupe against the CANCELLED build row — if it
   instead double-dispatches, that is FWD-001/002 evidence.

## Addendum 3 (2026-07-06 ~22:15 UTC) — the first real tap, and the no-ack discovery

Rich tapped Approve (~21:30:46 UTC) on the FEAT-8BA35C prompt. Result chain:
1. jarvis handled it perfectly: allowlist auth, truthful identity — the
   response is STORED in the broker (AGENTS #894: `decision=approve`,
   `decided_by=U03QR8WKT29`, correct subject + envelope). Contract v2 works.
2. But jarvis logged `slack_reply_publish_failed` (TimeoutError) and restored
   the buttons: the AGENTS stream is `no_ack: true` (core request-reply
   traffic on `agents.>` — PubAcks would collide), so `js.publish` NEVER gets
   an ack there. Reproduced 3/3 by probe; messages store anyway. Forge's own
   ApprovalPublisher core-publishes (approval_publisher.py:487) — jarvis's
   `js.publish` is the outlier. Fix filed: jarvis TASK-JNB-111 (core publish
   + flush; blocks JNB-107 sign-off, though taps DO deliver meanwhile).
3. The tap failed to resolve the gate only because FEAT-8BA35C had already
   TIMED_OUT at 21:28:32 under the old 300s window — the armed subscriber was
   gone. With the 1800s window (addendum 2) and the fact that responses store
   despite the timeout, the NEXT tap should complete the round-trip; jarvis
   will cosmetically mis-report it as failed until JNB-111 lands.

## Addendum 4 (2026-07-06 22:35 UTC) — unfreeze verified; loop proven to the last hop

The frozen ack window expired exactly on schedule (22:28:32 UTC):
1. Stale seq-127 redelivery **deduped cleanly** — "duplicate already-terminal
   build ... ack + skip". No double-dispatch (positive evidence for the
   duplicate-terminal guard on this path).
2. `smoke-bbf0ffcf` dispatched immediately → `build-FEAT-96A391-20260706214251`
   PAUSED at the gate under the new `default_wait=1800s`.
3. jarvis delivered the prompt WITH buttons at 22:28:32
   (`slack_pause_message_upgraded_with_buttons`), zero delivery failures.
4. Operator asleep by design — the prompt times out ~22:58:32 UTC
   (harmless CANCELLED + phone terminal signal).

**Every hop of the loop is now individually live-proven** — dispatch → gate
pause → prompt+buttons → tap → authorized truthful response STORED on the
broker (AGENTS#894) → [gate resolution: the only unexercised hop, blocked
tonight solely by expired gates] → cancellation terminal signal delivery.

Morning sequence: (1) review + land jarvis TASK-JNB-111 (task-work ran
overnight; fixes the cosmetic publish mis-report) → restart jarvis →
(2) dispatch a fresh smoke and run the formal JNB-107 scenarios (approve /
reject / unauthorized) inside the 30-min window → (3) MP-010 pre-flights:
JARVIS_NATS_PASSWORD rotation, `planning.enabled=true` + escalation config
(NB: `escalation_approver` must be a Slack member ID — JNB-110 review note).

Re-dispatch snippet (from the forge venv; publishes an enveloped
BuildQueuedPayload; requires requested_at/queued_at):
```
set -a; source ~/.config/guardkit/jarvis.env; set +a
.venv/bin/python - <<'PY'
import asyncio, os, uuid; from datetime import datetime, timezone
import nats
from nats_core.envelope import MessageEnvelope, EventType
from nats_core.events import BuildQueuedPayload
corr=f"smoke-{uuid.uuid4()}"; now=datetime.now(timezone.utc)
p=BuildQueuedPayload(feature_id="FEAT-96A391", repo="guardkit/forge", branch="main",
  feature_yaml_path="features/FEAT-96A391/fix-task.yaml", triggered_by="cli",
  originating_adapter="cli-wrapper", correlation_id=corr, requested_at=now, queued_at=now)
env=MessageEnvelope(source_id="ops-live-check", event_type=EventType.BUILD_QUEUED,
  correlation_id=corr, payload=p.model_dump(mode="json"))
async def m():
    nc=await nats.connect(os.environ["JARVIS_NATS_URL"], user=os.environ["JARVIS_NATS_USER"],
                          password=os.environ["JARVIS_NATS_PASSWORD"])
    ack=await nc.jetstream().publish(f"pipeline.build-queued.{p.feature_id}", env.model_dump_json().encode())
    print("published", corr, ack.stream, ack.seq); await nc.close()
asyncio.run(m())
PY
```

## Addendum 5 (2026-07-07 06:48 UTC) — FIRST COMPLETED PHONE APPROVAL ROUND-TRIP

Rich tapped Approve on `build-FEAT-96A391-20260707063340` (dispatched 06:33:40,
prompt+buttons same second). At **06:48:17 UTC, all hops in one second**:
- jarvis `slack_reply_decision_published` — decision=approve, correct
  `.response` subject, NO publish failure (TASK-JNB-111's core-publish fix,
  first live use);
- forge ApprovalSubscriber accepted the response (verbatim
  `decided_by=U03QR8WKT29` == per-config `expected_approver` — identity
  contract v2 validated live) and published **build-resumed**;
- `maybe_gate_build: gate decided outcome=RESUMED` →
  `dispatch_build: gate approved … registering observer + launching autobuild`
  — the tap resumed and launched a real build;
- the toy build then ran and FAILED (its feature yaml does not exist in the
  container checkout — expected; the failure IS the terminal signal).

**JNB-107 scenario 1 (approve loop): VALIDATED.** Window-breach (scenario 4):
validated ×11 with phone terminal delivery. Remaining for formal JNB-107
completion: scenario 2 (reject → CANCELLED in SQLite first, then phone
signal) and scenario 3 (unauthorized click from a non-operator account →
ephemeral refusal, nothing published) — then `/task-complete TASK-JNB-107`
and SPL **Gate G1 flips to PASS**. Re-dispatch snippet: addendum 4.
