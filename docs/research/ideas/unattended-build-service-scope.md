# Unattended Build Service — Scope (Phase UBS)

**Status:** PARTIALLY STARTED — keystone core (runner→guardkit wiring) landed 2026-05-14 (TASK-ABW-001); sibling features (UBS-002/003/004) still open
**Date:** 2026-06-11 · **Corrected:** 2026-07-02 (see §2 banner)
**Repo focus:** forge (primary) · guardkitfactory (gate fix) · jarvis (notification surface) · ops (GB10 deployment)
**Decision frame:** DF-003 (build half unattended on local, Forge orchestrating the build half only) · DF-002 (Rich-hours ledger; plant recovery) · DECISION-DF-001 (local inference on the unattended critical path)
**Companion findings:** `ai-transition/docs/fine-tuned-judgment-agents-findings.md` (§6 addendum)
**Build plan:** `unattended-build-service-build-plan.md` (same directory)

---

## 1. Objective

Convert the existing Forge daemon + queue + Mode B/C planners into a **24/7
unattended build service on the GB10s**: feature-plans queued (by Rich via
`forge queue`, or via Jarvis from Open WebUI/Telegram — intake already proven
live), executed through guardkit AutoBuild on local inference, remediated
through Mode C **up to a configured point**, with every terminal state and
escalation reaching Rich's phone.

Not in scope: automating the planning half (ideation → `/feature-plan` stays
attended on frontier per DF-003); orchestration as external positioning. **The
loop is not the product; it is the factory's night shift.**

## 2. Current state (verified from source, 2026-06-11)

> **⚠️ 2026-07-02 correction (re-verified from source).** The `autobuild_runner`
> row below was already stale when this table was written. TASK-ABW-001 landed the
> real `guardkit autobuild` subprocess wiring on **2026-05-14** (coach-ft-v3 Coach
> routing added 2026-06-21) — ~4 weeks before this table's "verified 2026-06-11"
> date, which did not re-check that row. The keystone's *core deliverable is
> code-complete, not a placeholder.* What actually remains: (a) operational
> validation (TASK-ABW-OPS, operator-handoff — never run; the FEAT-9E59 rehearsal
> was tied to the since-passed 2026-05-16 demo); (b) the **coach-score population
> gap** — `last_coach_score`/`aggregate_coach_score` are plumbed through the bridge
> but the runner never sets them (always `None`), a prerequisite for UBS-002
> (see ADR-ARCH-033); (c) the genuinely-unstarted sibling features UBS-002/003/004.
> All *other* rows below were re-verified accurate as of 2026-07-02.

| Component | State |
|---|---|
| `forge serve` daemon (healthz, dispatcher, state channel, recovery) | ✅ Built |
| Queue + SQLite lifecycle persistence + state machine | ✅ Built |
| NATS: `pipeline.build-queued` consumer, approval pub/sub, fleet registration | ✅ Built |
| Jarvis → `BuildQueuedPayload` → Forge queue | ✅ **Validated live** (Open WebUI, 2026-06-11) |
| Mode B planner (feature chain) / Mode C planner (review → fix → re-review) | ✅ Built; Mode C assumptions catalogued (ASSUM-004…017) |
| guardkit adapter (`adapters/guardkit/run.py`, parser, progress subscriber, D39 context resolver) | ✅ Built |
| Lifecycle bridge → `pipeline.*` envelopes (paused/resumed/complete/failed) | ✅ Built |
| `autobuild_runner` subagent node bodies | ✅ **Wired (2026-05-14, TASK-ABW-001)** — `_node_running_wave` shells `guardkit autobuild feature <id> --fresh --verbose --coach-model coach-ft-v3` with timeout + exit-code→lifecycle mapping. ⚠️ **Gap:** Coach score not parsed into `last_coach_score`/`aggregate_coach_score` (always `None`) — UBS-002 prerequisite (ADR-ARCH-033) |
| Progress/escalation notifications → Telegram | ❌ Not wired |
| Unattended budget guards (Mode C cycle cap, wall-clock/token budget) | ❌ Not present (ASSUM-010: no numeric cap, reviewer-driven) |
| guardkit AutoBuild on local inference (FEAT-AOF) | ⚠️ Blocked at run-24 — TASK-FIX-COACHSYNTH (guardkitfactory) |
| Forge daemon deployed on GB10 | ❌ Runs on Mac today |

## 3. Design constraints

1. **Throughput × Coach quality are one system.** The current local Coach
   substrate is weak (runs 12–14). Unattended Mode C without caps risks
   oscillation/churn; a permissive Coach mass-produces unwired features at
   machine speed. Mitigations: budget guards (FEAT-UBS-002) and **autonomy
   follows verification quality** — conservative thresholds at launch,
   ratcheted as the QA Verifier fine-tune lands behind `--coach-model`.
   Thresholds are the dial coupling this phase to the judgment-agents thread.
2. **ASSUM-010 is preserved for attended mode.** Unattended is a *profile*
   layered on top, not a rewrite of Mode C semantics.
3. **Worktree confinement is non-negotiable** — `assert_within_worktree` is
   already in the runner; real node bodies must route all writes through it.
4. **SQLite remains authoritative; emits are best-effort** (DDR-007 failure
   contract) — notification loss must never regress a build.
5. **DF-001 holds**: the loop runs local inference only. Frontier never enters
   the unattended path.

## 4. Features

### FEAT-UBS-001 — Wire `autobuild_runner` node bodies to the guardkit adapter
The keystone. Replace placeholder lifecycle nodes with real work:
`planning_waves` reads the feature's task graph; `running_wave` invokes
`forge/adapters/guardkit/run.py` per task/wave, parsing progress into
`AutobuildState` deltas (wave/task indices, `last_coach_score`,
`aggregate_coach_score`) via the existing `_update_state` boundary;
terminal mapping to `completed`/`failed`; `stage_complete` envelope per
ASSUM-018; worktree confinement on every write path. Graph shape and
state schema **must not change** (the bridge translator contract is frozen).

### FEAT-UBS-002 — Unattended-mode budget guards
A config profile (`forge` config models + loader) adding: `max_review_cycles`
(Mode C follow-up review count), `max_build_wallclock`, optional
`max_build_tokens` (LangSmith-tagged or parsed from harness output). On cap:
pause build, emit `ApprovalRequestPayload` (risk_level high) with the budget
breach as rationale — never silent termination, never silent continuation.
Attended profile = caps off (ASSUM-010 unchanged). Per-build override at
queue time (`forge queue FEAT-X --profile unattended`).

### FEAT-UBS-003 — Pipeline notifications → Jarvis → Slack (and approvals back)
> **2026-07-03 delta:** surface pivoted **Telegram → Slack** by operator
> decision (no Telegram account; Telegram was an ideation default never
> actually chosen). Slack Socket Mode keeps the reply path outbound-only — the
> same no-public-endpoint property that motivated Telegram. v1 scope widened
> to the full lifecycle (queued → running → terminal + pauses). Spec:
> `features/jarvis-notification-bridge/` (31 scenarios, assumptions resolved).

Jarvis subscribes to `pipeline.*` lifecycle envelopes and
`agents.approval.forge.*`; routes to Slack with build/feature/correlation
context and coach scores. v1: one-way notifications for queued (jarvis-intake),
running, all terminal states + pauses. v1.1: approve/reject interactive-button
replies from Slack → `ApprovalResponsePayload` → Forge approval subscriber →
resume (the `mark_resume_pending` path — note: production wiring for the
approval subscriber does not exist yet, verified 2026-07-03).
Notification failure is logged-and-continue per DDR-007.

### FEAT-UBS-004 — GB10 deployment + overnight runbook (ops)
Forge daemon + NATS on the GB10 (container or systemd), llama-swap warm-model
policy for the build window (workhorse + coach pinned; tutor contention
documented), log rotation, `forge status` reachable over Tailscale, restart
recovery validated (kill daemon mid-build → recover from SQLite). Deliverable
includes the runbook and the **first supervised overnight run**: ≥3 features
queued, all terminal states notified, zero mid-run manual interventions.

### FEAT-UBS-005 — Two-Spark concurrent dispatch (deferred until evidence)
ADR-SP-012 amendment: `max_concurrent: 2` with per-build inference-endpoint
affinity (per-build `OPENAI_BASE_URL` host selection across the two llama-swap
instances). Topology: two independent inference hosts, not a fused 256GB pool.
**Revisit condition:** ≥5 clean single-Spark overnight builds and observed
queue depth exceeding single-Spark overnight capacity. **Second motivation**
(capability-utilisation assessment §4): specialist models on Spark B remove
the model-swap contention that currently blocks advisory specialist calls
during build windows.

### FEAT-UBS-006 — Architect align-advisory annotations (deferred)
When a completed build's diff touches architecture-relevant paths, attach an
`architect_align` judgment (local fine-tune, ~10s warm) to the terminal-state
Telegram notification — advisory riding the 🟡 flag, never gating.
**Scheduling:** terminal-state time only (model swap harmless then; see
contention note in `specialist-agent/docs/research/ideas/capability-utilisation-assessment.md`
§4). **Promotion condition:** Phase GRAM FEAT-GRAM-002 shows 4/4 schema
validity. The Forge ledger records agreement rate — the evidence for any
future promotion from annotation to gate.

### FEAT-UBS-007 — Scheduled `architect_explore` drift reports (deferred)
Weekly Forge-queued night-shift job per repo: `architect_explore`
(architecture docs vs codebase) → drift report (undocumented components, ADR
contradictions, stale index entries) to `docs/reviews/drift/`. Motivated by
the 2026-06-11 finding that the April fleet index materially mis-described
Forge's state. Serves D33 (docs-as-coordination requires docs to be true).
Idle-window scheduling via the queue, not cron. **Promotion condition:** UBS
core loop proven (first clean overnight) — this is the first non-build job
class the night shift takes on.

## 5. Explicitly out of scope

- Feature-plan *detection* (filesystem/git watching) — v1 intake is deliberate:
  `forge queue` / Jarvis. A watcher is a later nicety.
- PM-tool adapters (D38: viewports, not control surfaces).
- Any change to the planning half or to `/feature-spec`–`/feature-plan`.
- The QA Verifier fine-tune itself (separate thread; couples via
  `--coach-model` and thresholds only).

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Weak Coach → churn or permissive approvals overnight | Budget guards (UBS-002); conservative thresholds; ratchet only with QA Verifier evidence |
| Model-swap contention (build models vs tutor evening use) | Warm-model policy in runbook; build window scheduling; second Spark relieves (UBS-005) |
| Notification path failure masks a stuck build | SQLite authoritative; `forge status` over Tailscale; daily morning status habit until trust earned |
| DeepAgents `start_async_task` in-process emitter contract (F3 risk, ADR-ARCH-031) | Smoke test is the canary; pin DeepAgents version in deployment |
| Runner work changes graph shape and breaks bridge translator | AC in UBS-001 freezing schema; bridge translation tests must pass unchanged |

## 7. Success criteria (phase level)

1. Rich queues N feature-plans before bed from Open WebUI or CLI; wakes to
   terminal notifications for all N, with PRs raised for approved work and
   flagged items carrying Coach rationale.
2. No unattended build exceeds its budget without escalating.
3. Run artefacts (coach turns, verdicts, outcomes) accumulate in the harvest
   corpus shape — the loop feeds the QA Verifier dataset as exhaust.
4. Metrics for the Workstream A "production deployment with metrics" narrative
   come from the Forge ledger (SQLite + LangSmith), not manual bookkeeping.
