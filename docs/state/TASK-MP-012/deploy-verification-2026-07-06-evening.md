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
