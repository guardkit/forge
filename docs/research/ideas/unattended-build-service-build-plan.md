# Unattended Build Service — Build Plan (Phase UBS)

## Status: Ready for `/feature-spec FEAT-UBS-001` once Prerequisite 1 clears
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
  for UBS-003 testing and the lifecycle bridge.
- [ ] Second GB10 racked + Tailscale'd (arrives 2026-06-12) — needed only for
  UBS-005, not for the critical path.

## Feature summary

| # | Feature | Repo | Depends on | Est. |
|---|---------|------|-----------|------|
| 1 | FEAT-UBS-001 — runner node bodies → guardkit adapter | forge | P1, P2 | 1–2 days |
| 2 | FEAT-UBS-003 — notifications → Jarvis → Telegram | forge + jarvis | UBS-001 (envelope flow to observe) | 1 day |
| 3 | FEAT-UBS-002 — unattended budget guards | forge | UBS-001 | 0.5–1 day |
| 4 | FEAT-UBS-004 — GB10 deployment + runbook + first overnight | ops | UBS-001..003 | 0.5 day + 1 night |
| 5 | FEAT-UBS-005 — two-Spark dispatch (ADR-SP-012 amendment) | forge | ≥5 clean overnights + queue-depth evidence | deferred |

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
```
/feature-spec "Jarvis notification bridge: subscribe to pipeline.* lifecycle \
envelopes and agents.approval.forge.*; route to Telegram with build_id, \
feature_id, correlation_id, stage, coach score, rationale. v1 one-way \
(terminal states + pauses). v1.1 approval replies: Telegram approve/reject -> \
ApprovalResponsePayload -> Forge approval subscriber resume path. \
Notification failure = log WARNING and continue (DDR-007)." \
  --context ../jarvis \
  --context src/forge/nats/approval_subscriber.py \
  --context docs/research/ideas/unattended-build-service-scope.md
```
Then `/feature-plan` + `guardkit autobuild` as above (jarvis repo for the
adapter half if the spec splits it).
Validation: queue a toy feature from Open WebUI; phone receives queued →
running → terminal; a deliberately paused build delivers its approval request.

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
| `jarvis` notification adapter module | UBS-003 | New subscriber + Telegram routing |
| `src/forge/nats/approval_subscriber.py` | UBS-003 v1.1 | Telegram reply → resume |
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
