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

---

## Follow-up addendum (2026-07-11) — the two gaps FIXED + deployed + terminal re-validated

Per Rich's "proceed with the followups" (orchestrated-build playbook), the two filed gaps ran as
a background Workflow (`wf_8e66c248-449`, 2 coach-gated stages, both passed) + a coordinator
review/deploy/re-validation:

- **Fix (reviewed + pushed):** the fleet_watcher crash was root-caused NOT to forge but to
  **`nats_core.client.watch_fleet`** (the nats-py KV `_init_done` None sentinel) — guarded
  `if entry is None: continue` (**nats-core `1dc6cef`** + regression test; forge `c2210db`
  composed-watcher regression test). **git added to the forge runtime image** (forge `319a800`;
  FEAT-FORGE-008 equivalence contract confirmed unaffected — it covers `pip install .[providers]`
  + the buildx invocation, not the apt list). Tests re-verified (50 forge + the nats-core suite).
- **Deploy (coordinator, GB10, Ack-0 gated + backups + rollback tag):** rebuilt
  `forge:latest`=`0173e59a` (carries the fixed nats-core via `--build-context`), added the
  **api_test rw mount** + a **git author identity** (`GIT_*` compose env — a 3rd sub-gap: the
  container's `forge` user had no identity → `git commit` "Author identity unknown") to
  `~/forge-prod/docker-compose.yml`, recreated forge-prod (rollback `forge:rollback-pre-gitmount-20260711`).
- **Re-validation (synthetic inject + synthetic identity-pinned approve — no phone):**
  - **TASK-FWD-PLAN-GITMOUNT ✅ FULLY VALIDATED** — run reached **`PLANNED_HANDOFF`**,
    `handoff_branch=planning/{cid}` + `feature_spec_inputs/{cid}.md` committed in api_test
    (`0ab1f62`), `error=None`. The exact prior failure (`repo_path is not a directory`) is gone.
  - **TASK-FWD-PLAN-FLEETWATCHER — crash FIXED** (0 error loops, was ~1/s), watcher reads the
    correct `agent-registry` KV; **but PO still degrades** → a DISTINCT capability-name mismatch
    filed as **TASK-FWD-PLAN-PODISCO** (forge asks `tool_exact` for `product_owner_specialist`;
    the PO agent advertises `po_*` tools + a `product.*` intent — no exact tool match).
- **State:** planning reverted to `enabled:false` (Mode P produces degraded plan content until
  PODISCO lands; the INFRA — durable, dispatch, pause, identity-pinned approval, git terminal —
  all work). forge-prod left healthy on the new image; planning durable deleted; api_test test
  branches removed. **J05 / live-planning: the terminal + crash gaps are cleared; PODISCO (plan
  content) is the remaining Mode-P-quality gap.** Full ai-transition record: exec-plan §8.*

---

## Follow-up addendum 2 (2026-07-11) — PO resolution FULLY fixed (4 layers) + notification ACL; execution layer filed

Chasing PODISCO to the bottom revealed a STACK of masked layers — each fix uncovered the next.
All DISCOVERY/RESOLUTION layers are now fixed + deployed (forge:latest=`ab9cd331`):

1. **PODISCO — intent threading** (forge `8e24b7d`): the dispatch passed `intent_pattern=None`, so
   resolve()'s exact-tool→intent-fallback never ran. Added `SPECIALIST_INTENT_BY_STAGE`
   (PRODUCT_OWNER→`product.*`, ARCHITECT→`architecture.*`) + threaded it. 7 tests.
2. **fleet_watcher initial-state** (nats-core, pushed + regression test): even with the crash guard
   (`1dc6cef`), the same loop routed the KV **initial-state replay** entries (`operation=None`,
   value-bearing) to the DELETE branch → the cache stayed EMPTY on boot. Fixed: value-bearing
   (`PUT` or `op=None`) upsert; only `DEL`/`PURGE`/valueless delete. Live diag: cache = 5 agents.
3. **notification ACL** (nats-infra, pushed + broker re-rendered): `forge` granted
   `jarvis.notification.>` publish (FEAT-SPL-003 return channel — was a `permissions violation`).

**RESULT (verified live):** `discovery.resolve.matched tool=product_owner_specialist
agent=product-owner-agent source=intent_pattern` — **PO now RESOLVES**; the run goes `RUNNING`
(specialist actually invoked) instead of degrading instantly. Terminal + notification paths clear.

4. **REMAINING — execution layer (TASK-FWD-PLAN-DISPATCHFMT):** forge publishes to
   `agents.command.product-owner-agent`, but the specialist parses it as a `MessageEnvelope`
   (needs `event_type`+`payload`) while forge sends a dispatch-shaped body (`resolution_id`/…) →
   the specialist rejects it, never replies, the run hangs `RUNNING`. A forge↔specialist
   EXECUTION-contract mismatch (both repos), and likely NOT the last layer (the whole
   dispatch→run→result path was never end-to-end tested). Recommend a dedicated forge↔specialist
   integration pass coordinated with the specialist-agent lane.

**State:** planning reverted `enabled:false`; forge-prod healthy on `ab9cd331` (all resolution +
terminal + notification fixes live); durable deleted, api_test clean. **Mode P net:** discovery
resolution ✅, terminal ✅, notification ✅ — the specialist EXECUTION contract (DISPATCHFMT+) is
the remaining multi-layer work before real plans are produced.*
