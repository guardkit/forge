# Unattended Build Service — Build Plan (Phase UBS)

## Status (2026-07-03)

- **FEAT-UBS-001 (keystone):** core SHIPPED (TASK-ABW-001, 2026-05-14) — do NOT
  `/feature-spec FEAT-UBS-001`. Operational validation still pending →
  `TASK-ABW-OPS` (operator-handoff: GB10 allowlist + sidecar restart + FEAT-9E59
  rehearsal, now also capturing the autobuild transcript for the coach-score gap).
- **FEAT-UBS-002 (budget guards):** SKELETON DONE + under process — config
  profiles + pure `budget_guard` evaluator + `forge queue --profile`, 30 unit
  tests; formalised via `/feature-spec` + `/feature-plan`
  (`.guardkit/features/FEAT-UBS-002.yaml`, `features/unattended-build-service-budget-guards/`);
  independently reviewed via `/code-review` (one silent-mismatch defect found +
  fixed). Live enforcement (supervisor wiring + queue→daemon profile plumbing +
  coach-score activation) DEFERRED → `TASK-UBS-002-integration`.
- **FEAT-UBS-003 (notifications → Jarvis → Slack, was Telegram):** SPEC
  REVISED (2026-07-03, same day as written) — **surface pivoted to Slack by
  operator decision** (no Telegram account; Telegram was an ideation-doc
  default never actually chosen; Slack Socket Mode keeps the v1.1 reply path
  outbound-only). Now 31 scenarios (8 key / 5 boundary / 7 negative / 12 edge;
  2 security; 6 smoke), parser-validated. v1 scope widened to **full
  lifecycle** (queued → running → terminal + pauses) per the operator and the
  original validation line. Both low-confidence assumptions RESOLVED:
  ASSUM-003 coach-score range source-verified (0.0–1.0 forge-enforced
  contract; wire unconstrained; **always None today** per ADR-ARCH-033);
  ASSUM-007 → Slack bot/channel binding (JARVIS_SLACK_* env vars). Two new
  assumptions recorded: ASSUM-010 (build-cancelled has no verified forge
  producer today) + ASSUM-011 (queued notification fires at jarvis intake
  publish-time; CLI-queued builds get none). Local NATS prerequisite fixed
  (see P3 note). **PLANNED (2026-07-03, TASK-REV-C951):** judge-panel decision
  review chose the validation-first in-process Slack sink (88/100;
  correlation-independent fan-out; plain pytest, no BDD glue — both
  operator-confirmed). 16 tasks generated (13 jarvis + 3 forge, repo-local per
  the autobuild sibling-repo constraint); jarvis v1 feature YAML
  **FEAT-28FF** validated (5 waves, smoke gates after waves 2/3/4); v1.1
  YAMLs deliberately deferred until the live v1 checkpoint (TASK-JNB-004)
  passes. Report: `.claude/reviews/TASK-REV-C951-review-report.md`.
  **v1 SHIPPED + CHECKPOINT PASSED (2026-07-04):** FEAT-28FF built (7/7
  Coach-approved), merged to jarvis main (`736399b`, suite 2419/0), deployed
  to the GB10; live evidence in #forge-builds — queued 07:14/11:07, RUNNING
  09:58, complete (PASSED) 09:58/11:07, exactly-once held throughout.
  TASK-JNB-004 completed with evidence record; TASK-JNB-009 (hardening
  validation) still pending operator. The checkpoint doubled as the deferred
  TASK-ABW-OPS validation and surfaced 5 forge wire-dispatch bugs (filed:
  `tasks/backlog/forge-wire-dispatch-fixes/TASK-FWD-001..004` + ABW-002),
  the jarvis NATS-user pipeline grant (fixed, nats-infrastructure `d252c35`),
  and the GB10 sidecar harness default (attended override GUARDKIT_HARNESS=sdk
  + coach-model argv removal — revert for P2, tracked in TASK-FWD-004).
  **v1.1 PLANNED:** jarvis `FEAT-BF39` (103→104→105→107 live validation) +
  forge `FEAT-1872` (101→102→106), both validated with venv-explicit smoke
  gates. Next: `/feature-build FEAT-BF39` (jarvis) and `/feature-build
  FEAT-1872` (forge), then TASK-JNB-107 live approve/reject from the phone.
- **Substrate/fork decision:** `ADR-ARCH-033` (runner's direct-shell path
  ratified as interim; coach-score population gap is the UBS-002 prerequisite).
- **Next per this plan (visibility before autonomy):** FEAT-UBS-003
  `/feature-plan` (spec done), then the UBS-002 integration, then FEAT-UBS-004.

## Original status (2026-06-11): Ready for `/feature-spec FEAT-UBS-001` once Prerequisite 1 clears
## Repo: forge (primary); guardkitfactory (prerequisite); jarvis (UBS-003)
## Scope doc: `unattended-build-service-scope.md` (same directory)

---

## Prerequisites

- [ ] **P1 (GATE):** `TASK-FIX-COACHSYNTH` in guardkitfactory
  (`tasks/backlog/TASK-FIX-COACHSYNTH-provider-side-signatures.md`) — both
  provider-side signatures landed; verification run-25 reaches Player turn 1.
  *Small enough for a direct OpenCode session; does not need the pipeline.*
- [ ] **P2:** One clean FEAT-AOF run end-to-end on local inference (Player +
  LLM Coach gather/synthesis) — freezes the harness contract UBS-001 wires to.
- [ ] **P3:** NATS up on GB10 (`nats-infrastructure` docker compose) — required
  for UBS-003 testing and the lifecycle bridge. *(Local dev broker fixed +
  verified 2026-07-03: container had been crash-looping since the ~Jun-24
  accounts-template update — three new account passwords missing from `.env`
  (FORGE/FLEET_MEMORY/GUARDKIT_NATS_PASSWORD, now generated + appended) and the
  Apr-17 image baked a stale entrypoint that clobbered `$JS.>` subjects (image
  rebuilt). PIPELINE/AGENTS/JARVIS streams verified live as the `forge` user.)*
- [ ] Second GB10 racked + Tailscale'd (arrives 2026-06-12) — needed only for
  UBS-005, not for the critical path.

## Feature summary

| # | Feature | Repo | Depends on | Est. | Status (2026-07-03) |
|---|---------|------|-----------|------|---------------------|
| 1 | FEAT-UBS-001 — runner node bodies → guardkit adapter | forge | P1, P2 | 1–2 days | ✅ core shipped; ⏳ TASK-ABW-OPS validation |
| 2 | FEAT-UBS-003 — notifications → Jarvis → **Slack** (was Telegram) | forge + jarvis | UBS-001 (envelope flow to observe) | 1 day | 🟡 **spec revised** (31 scenarios; Slack pivot; assumptions resolved) → `/feature-plan` |
| 3 | FEAT-UBS-002 — unattended budget guards | forge | UBS-001 | 0.5–1 day | 🟡 skeleton done + reviewed; ⏳ TASK-UBS-002-integration |
| 4 | FEAT-UBS-004 — GB10 deployment + runbook + first overnight | ops | UBS-001..003 | 0.5 day + 1 night | ⬜ |
| 5 | FEAT-UBS-005 — two-Spark dispatch (ADR-SP-012 amendment) | forge | ≥5 clean overnights + queue-depth evidence | deferred | ⬜ deferred |

Ordering note: UBS-003 before UBS-002 — visibility before autonomy. Watching
the first capped-off attended runs through Telegram is itself validation input
for the budget defaults.

## GuardKit command sequence

> Context flags lean on forge's `.guardkit/context-manifest.yaml` (D39).
> Adjust paths at invocation if the manifest resolver supplies them
> automatically. Run from the forge repo root unless stated.

### Step 0 — Prerequisite fix (no pipeline; OpenCode direct)
```
# In guardkitfactory: implement TASK-FIX-COACHSYNTH per the task file, then:
guardkit autobuild FEAT-AOF --verify   # run-25; expect Player turn 1 reached
```

### Step 1 — FEAT-UBS-001
```
/feature-spec "Wire autobuild_runner lifecycle node bodies to the guardkit \
adapter: planning_waves reads the feature task graph; running_wave invokes \
forge/adapters/guardkit/run.py per task, updating AutobuildState via \
_update_state (wave/task indices, coach scores); terminal mapping to \
completed/failed; stage_complete per ASSUM-018; all writes through \
assert_within_worktree. Graph shape and AutobuildRunnerState schema are \
FROZEN (bridge translator contract)." \
  --context src/forge/subagents/autobuild_runner.py \
  --context src/forge/adapters/guardkit/ \
  --context src/forge/lifecycle_bridge/translation.py \
  --context docs/research/ideas/unattended-build-service-scope.md
```
```
/feature-plan FEAT-UBS-001
```
```
guardkit autobuild FEAT-UBS-001
```
Validation: bridge translation tests pass unchanged; a queued toy feature
drives real lifecycle transitions on the wire (`forge status` shows wave/task
progression); kill-and-recover mid-build leaves SQLite consistent.

### Step 2 — FEAT-UBS-003
`/feature-spec` **DONE (2026-07-03)** → `features/jarvis-notification-bridge/`
(`.feature` + `_assumptions.yaml` + `_summary.md`; parser-validated). The
invocation used (note: approval subscriber lives at
`src/forge/adapters/nats/approval_subscriber.py`, not `src/forge/nats/`):
```
/feature-spec "Jarvis notification bridge: subscribe to pipeline.* lifecycle \
envelopes and agents.approval.forge.*; route to Telegram with build_id, \
feature_id, correlation_id, stage, coach score, rationale. v1 one-way \
(terminal states + pauses). v1.1 approval replies: Telegram approve/reject -> \
ApprovalResponsePayload -> Forge approval subscriber resume path. \
Notification failure = log WARNING and continue (DDR-007)." \
  --context ../jarvis \
  --context src/forge/adapters/nats/approval_subscriber.py \
  --context docs/research/ideas/unattended-build-service-scope.md
```
~~Before `/feature-plan`: resolve the 2 low-confidence assumptions~~ **DONE
(2026-07-03):** ASSUM-003 source-verified (0.0–1.0 forge-enforced contract;
wire unconstrained; always None today per ADR-ARCH-033 — the no-score render
path is the live default); ASSUM-007 resolved by the **Slack pivot** (operator
decision: no Telegram account; single operator Slack channel, bot token +
channel id; v1.1 replies via Socket Mode buttons authorized against the
operator member id). v1 scope widened to full lifecycle (queued → running →
terminal + pauses); spec revised to 31 scenarios + 2 new assumptions
(ASSUM-010 build-cancelled producer gap; ASSUM-011 queued-at-intake).

**Planning fork — RESOLVED by verification (2026-07-03):** jarvis ships
`ForgeNotificationsSubscriber` (FEAT-JARVIS-005) routing
started/stage-complete/complete/failed to the **CLI** FIFO via a correlation
map, as one ephemeral push consumer with a multi-subject filter. The workqueue
PIPELINE stream rejects any second consumer with overlapping filters
(err_code=10100, TASK-FRR-F010Db) — so the Slack surface **must extend that
subscriber in-process** (its docstring already reserves promotion to
`jarvis.notification.{adapter}` wire payloads — the FEAT-JARVIS-006 pattern;
the JARVIS stream is provisioned and live). `build-paused`/`build-cancelled`
can be added to the existing filter (no other consumer binds them — verified
live). The queued notification fires at jarvis intake publish-time
(`tools/dispatch.py`), never from the stream. v1.1 forge-side gap to plan for:
no production wiring instantiates `ApprovalSubscriber` today.
```
/feature-plan "Jarvis Notification Bridge" \
  --context features/jarvis-notification-bridge/jarvis-notification-bridge_summary.md
```
Then `guardkit autobuild` as above (jarvis repo for the adapter half if the
plan splits it).
Validation (v1 checkpoint, gates v1.1): queue a toy feature from Open WebUI;
phone receives queued → running → terminal; a deliberately paused build
delivers its approval request with rationale. Only after v1 passes: a Slack
approve button resumes it (v1.1).

> Note: `/feature-spec` Step 8's `installer.core.commands.lib.feature_spec_normalize`
> module is not present in this repo — validate the emitted `.feature` with the
> vendored `gherkin` parser directly (it is what `/feature-plan` Step 11 uses).

### Step 3 — FEAT-UBS-002
```
/feature-spec "Unattended build profile with budget guards: config additions \
max_review_cycles, max_build_wallclock, optional max_build_tokens; \
enforcement in Mode C planning tick and runner supervision; on breach pause \
the build and emit ApprovalRequestPayload (risk_level=high) with budget \
rationale — never silent stop or silent continue. Attended profile keeps \
ASSUM-010 semantics (no caps). Per-build override: forge queue --profile." \
  --context src/forge/pipeline/mode_c_planner.py \
  --context src/forge/config/ \
  --context src/forge/cli/queue.py \
  --context docs/research/ideas/unattended-build-service-scope.md
```
Validation: synthetic loop hitting `max_review_cycles=2` pauses + notifies;
wall-clock cap test with a stalled stage.

### Step 4 — FEAT-UBS-004 (ops tasks, not autobuilt)
- Containerise/systemd `forge serve` on GB10; NATS compose up; Tailscale
  health endpoint; log rotation.
- llama-swap warm-model policy for the build window (workhorse + coach
  pinned); documented contention note for tutor evening hours.
- Restart-recovery drill; runbook in `forge/docs/runbooks/`.
- **First supervised overnight:** queue ≥3 real features (candidates: the
  dataset-factory seeded-defect mode; jarvis notification v1.1; small backlog
  items), conservative thresholds, budget caps on. Morning review = the
  validation evidence + first harvest-grade outcome-labelled run set.

### Step 5 — FEAT-UBS-005 (deferred)
ADR-SP-012 amendment first (document in forge `docs/architecture/decisions/`),
then `/feature-spec` for `max_concurrent: 2` + per-build inference-endpoint
affinity. **Do not start before the revisit condition in the scope doc.**

## Files that will change (primary)

| File | Feature | Change |
|---|---|---|
| `src/forge/subagents/autobuild_runner.py` | UBS-001 | Node bodies (graph/schema frozen) |
| `src/forge/adapters/guardkit/run.py` (+parser/progress) | UBS-001 | Invocation surface hardening as needed |
| `jarvis` notification adapter module | UBS-003 | Extend ForgeNotificationsSubscriber + Slack routing (was Telegram) |
| `src/forge/adapters/nats/approval_subscriber.py` | UBS-003 v1.1 | Slack reply → resume (+ production wiring — none exists today) |
| `src/forge/config/models.py` + `loader.py` | UBS-002 | Unattended profile + caps |
| `src/forge/pipeline/mode_c_planner.py` (or supervision wrapper) | UBS-002 | Cycle-cap enforcement point |
| `src/forge/cli/queue.py` | UBS-002 | `--profile` flag |
| `forge/docs/runbooks/unattended-build-service.md` | UBS-004 | New runbook |

## Timeline

With P1/P2 cleared early next week: UBS-001 (1–2 days) → UBS-003 (1 day) →
UBS-002 (1 day) → deployment + first overnight by end of week. The PO dataset
domain runs through the factory in parallel throughout (config-only). The QA
Verifier thread proceeds per the findings doc §6 sequence and couples back in
via `--coach-model` + threshold ratchet — no UBS feature blocks on it.
