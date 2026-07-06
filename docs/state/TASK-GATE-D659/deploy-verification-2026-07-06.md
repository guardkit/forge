# TASK-GATE-D659 — dated deploy verification (GB10, 2026-07-06)

The 2026-07-06 fleet audit flagged that the runtime deployment claims (image
hash, GB10 PIPELINE durable `ack_wait` re-pin) had no committed artifact — the
plan_audit follow-up said the durable "may need recreating". Verified **live on
the GB10 host** today; all claims corroborated:

## forge-prod container

```
image   = sha256:034a28364325c89c8dbed7b8ace9fa77d420823452cf282cb10a8cbacf7caab8
started = 2026-07-06T06:49:12Z
health  = healthy
```

Boot log (2026-07-06T06:49:13, `docker logs forge-prod | grep "gate parts composed"`):

```
forge.cli._serve_deps_gating: forge-serve: approval gate parts composed
(expected_approver='rich' refresh=disabled bridge_lookup=absent
 default_wait=300s max_wait=3600s)
```

## GB10 PIPELINE durable (`nats consumer info PIPELINE forge-serve`)

```
Filter Subject: pipeline.build-queued.*
Deliver Policy: All
Ack Policy:     Explicit
Ack Wait:       1h0m0s          <- the ACK_WAIT_SECONDS=3600 pin IS live
Created:        2026-07-05 22:11:30
Redelivered: 0  Unprocessed: 0
```

The durable was (re)created 2026-07-05 22:11:30 — minutes after the gate code
landed (`75e0c5c`, 22:04) — so the recreation the plan_audit flagged as "may be
needed" did in fact happen. **The mandatory JNB-107 pre-flight durable check is
satisfied** (re-run the one-liner above on the day of the live run; the durable
must still show `Ack Wait: 1h0m0s`).

Remaining JNB-107 pre-flight (unchanged): TASK-JNB-OPS-001, and assessment of
TASK-FWD-002/003/004 (`tasks/backlog/forge-wire-dispatch-fixes/` — FWD-004's
duplicate `forge-autobuild-runner` systemd unit was still **enabled** on the
GB10 as of 2026-07-06).
