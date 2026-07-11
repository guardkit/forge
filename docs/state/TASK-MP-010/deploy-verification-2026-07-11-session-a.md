# Deploy verification — Session A activation bundle (2026-07-11, GB10)

**Session:** attended Opus on `promaxgb10-41b1`, with Rich at the phone/console.
**Runbook:** `ai-transition/docs/handoff-2026-07-07-post-gate-g1-remaining-work.md` §1–§2
+ `session-a-kickoff-2026-07-11.md`. JNB-107 addenda pattern (one record, phases as addenda).
**Scope:** JARVIS_NATS_PASSWORD rotation → J04 planning intake → MP-010 live planning
validation → close-out. JNB-009 live build-probes deferred (pytest matrix green; Rich's call).

---

## Addendum 0 — backups + perishable pre-flights (all green)

- Identity confirmed `promaxgb10-41b1`. Containers healthy: forge-prod, ships-computer-nats,
  both specialist-agents, fleet-memory-relay, study-tutor, jarvis-serve-nats.
- **Restart-freeze trap:** `nats consumer info PIPELINE forge-serve` → `Outstanding Acks 0` —
  restart-safe (re-checked before EVERY forge-prod / broker recreate this session).
- forge `/healthz` :8088 → healthy. Broker in-container config valid. jarvis env carried the
  rotated structure + 4 Slack vars; planning vars correctly absent (pre-J04).
- Backups: `~/forge-prod-state/.forge` stop-copy-start → clean WAL-checkpointed `forge.db`
  (`~/forge-prod-state/.forge.bak-session-a-<ts>`); both `.env` files snapshotted to a 700
  out-of-repo dir (`~/.rotation-backups/session-a-<ts>/`, incl. later `.pre-j04` / `.pre-mp010`).

## Addendum 1 — JARVIS_NATS_PASSWORD rotation (DONE, verified)

**Consumer map CORRECTED (drift beyond the runbook §1 / kickoff D2):**
- Broker renders `accounts.conf` from `nats-infrastructure/.env` via `envsubst` on every start
  (docker-entrypoint.sh) — **D2's "docker cp / a recreate loses it" is moot**; rotation edits
  `.env` + recreate; the entrypoint re-renders (changeme/`${VAR}` guard proves a clean render).
- **Per-service NATS identities (D3):** `JARVIS_NATS_PASSWORD` is the `jarvis` user ONLY.
  forge=`forge`/`FORGE_NATS_PASSWORD`, fleet-memory=`fleet-memory`/`FLEET_MEMORY_NATS_PASSWORD`,
  specialists ×2 + study-tutor = user `rich`/`RICH_NATS_PASSWORD` (resolves the 07-07 DISCOVERY
  rows; least-privilege finding: those containers ride the full-`>` `rich` principal). True blast
  radius = broker `.env` + `~/.config/guardkit/jarvis.env` + the ad-hoc CLI (auto-follows).

**Execution:** new 48-char value written to both files (600); broker recreated (`up -d
--force-recreate nats`, Ack-0 gated) → **new pw ACCEPTED, old pw REJECTED (Authorization
Violation)**; forge-serve durable survived (JS volume). jarvis restarted (`stop; sleep 10; start`)
→ new PID clean connect, all boot events green, 0 auth failures. Fleet reconverged: `/connz` = 7
conns (fleet-memory 1, forge 2, jarvis 1, rich 3), 0 violations on non-jarvis clients.
**Founding fleet-secrets-register entry** committed to ai-transition
(`docs/secrets-register/PAGE-jarvis-nats-password.md`, `d63942d`).

## Addendum 2 — J04 planning intake (DONE, end-to-end, + latent ACL fix)

- Slack app: `message.channels`+`message.groups` added + reinstalled (Rich); bot `/invite`d to
  **#factory-planning** (`C0BHHKMP18Q`, distinct from notification `C0BF2FPQXAM`); originator =
  `U03QR8WKT29` (already the operator id, matches forge `expected_approver`).
- Env keys wired to `~/.config/guardkit/jarvis.env`; jarvis restarted → boot
  `slack_planning_intake_configured channel_id=C0BHHKMP18Q originator_ids=[U03QR8WKT29]`
  (verbatim), no `_no_op`, no channel-match warn, 0 auth violations.
- **FINDING + FIX (nats-infrastructure `399c494`):** first live planning post hit
  `permissions violation for publish to pipeline.planning-queued.*` — the `jarvis` NATS user had
  `pipeline.build-queued.>` but not `pipeline.planning-queued.>` (latent since FEAT-SPL-001; the
  same class as the 2026-07-04 build-queued grant). Added the grant to the accounts template,
  re-rendered + reconnected.
- **Round-trip verified:** real #factory-planning post → jarvis `planning_intake_queued` →
  in-thread "Queued for planning · 523adb76…" → `PlanningQueuedPayload` on stream seq 180 with
  `originating_adapter=slack`, `originating_user=U03QR8WKT29`, `parent_request_id=1783765090…`.

## Addendum 3 — MP-010 live planning validation (core loop VALIDATED; 2 gaps filed)

Deployed `planning.enabled:true` + `escalation_approver:U03QR8WKT29` + waits 3600/14400 +
`default_target_repo: guardkit/api_test` in `~/forge-state/forge.yaml` (schema-validated
pre-deploy); forge-prod restarted (Ack-0 gated). The **parked J04 message drove a REAL run:**

- **AC-1 ✅** `planning composition: durable forge-serve-planning bound (filter=
  pipeline.planning-queued.*, ack_wait=3600s)`; `recorded planning run correlation_id=523adb76…
  originating_user=U03QR8WKT29 triggered_by=jarvis`; PAUSED at `product_docs` + approval request
  published + rendered to the phone (`planning_checkpoint_rendered`, coach_score=None → paused per
  DF-009).
- **AC-3 ✅** phone approve: `dialogue_decision_published decision=approve decided_by=U03QR8WKT29`
  → forge `_dispatch_approval_response: approved … by U03QR8WKT29; resumed`.
- **Terminal (AC-4) — filed gap, clean failure:** `GitRunner failed: repo_path is not a
  directory: …/api_test` → run `FAILED` (never raised — ADR-ARCH-025 boundary held).

**GAP 1 (git+mount):** forge-prod (Debian, no git) does not bind-mount the Projects dir, but the
planning handoff runs `WorktreeGitRunner()` IN-PROCESS (`_serve_planning.py:719`). Builds delegate
to the host `forge-langgraph-sidecar` (has git+repos); planning handoff does not. → filed
`TASK-FWD-PLAN-GITMOUNT`.
**GAP 2 (fleet_watcher):** `fleet_watcher: transient error 'NoneType'…'operation'` loops →
specialist discovery empty → PO dispatch `degraded reason=no_specialist_resolvable` → degraded
plan content (TASK-MP-012 review had pre-warned the watcher-client composition, `_serve_planning.py:465`).
→ filed `TASK-FWD-PLAN-FLEETWATCHER`.

**Reverted clean:** `planning.enabled:false` + forge-prod restart + deleted the orphaned
`forge-serve-planning` durable → back to exact pre-MP-010 state (only `forge-serve` durable; no
fleet_watcher loop; healthz healthy). **MP-010 reconciliation:** the WS3-S8 sweep marked MP-010
`completed` by feature-rollup, but its operator ACs had never run; this session validated the
core loop to the terminal — MP-010 stays open pending the two gaps before "live planning" is
production-ready (J05 / live-planning unblock gated on GAP-1 + GAP-2).

## Addendum 4 — FWD-004 (no live change needed)

Unit-disable ✅ (`forge-autobuild-runner` is `disabled`); JARVIS_NATS_PASSWORD rotation ✅
(addendum 1). Attended-override revert: **already in the desired state** — tree clean (no
uncommitted `autobuild_runner.py` coach-model deletion to `git checkout`), no
`GUARDKIT_HARNESS=sdk` on the sidecar. State drifted to post-revert; nothing to revert. FWD-004
items satisfied.

---
*Written 2026-07-11 by the attended Opus Session A. Rotation + J04 fully green; MP-010 core loop
validated with two forge gaps filed; JNB-009 live build-probes deferred (pytest matrix 122/122
green). Cross-refs: nats-infrastructure `399c494`, ai-transition `d63942d` + exec-plan §8.*
