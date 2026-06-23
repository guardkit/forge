# Conversation Starter — Forge Ideation (fresh ideas session)

> **Status update — 20 June 2026** (after the output-side-loop session). Still the live Forge ideation surface; four reconciliations from that session:
> - **Escalation/approval channel is now Slack, not Telegram.** One notification-and-approve mechanism serves James (the door) and Rich (the gates), via the Slack adapter. The Telegram references below — the "one missing link" note, UBS-003, "Rich's phone is the factory's escalation console" — are superseded.
> - **Forge's remit has widened beyond "the build half."** It now also owns the **output-side deploy/verify loop** (the runbook executor — see `handoffs/forge-output-loop-conversation-starter.md`) and the **improve loop** (meta-harness). Three unattended back-half loops, not one. Not a DF-003 change (the attended/unattended boundary holds); "build half only" below is simply narrower than current scope.
> - **UBS-001 ⟷ runbook `run_autobuild` overlap.** "Wire the `autobuild_runner` node bodies to the adapter" (UBS-001, still a placeholder) and the runbook executor's first step `run_autobuild` are the same seam — Forge invoking AutoBuild. Treat the runbook step as the evolved framing of UBS-001 and reconcile before building either.
> - **"AutoBuild is done" means the loop works locally via Claude Code — not that Forge runs it unattended.** The unattended-via-Forge piece (UBS / the `run_autobuild` step) is still to build.

**Purpose:** a clean surface for Rich to bring *new* Forge ideas to explore and ideate on. This is not a build session and not a docs overhaul — it's the grounded starting point so a new conversation knows Forge's current real state and the decisions that constrain it, then gets out of the way.

**Date written:** 2026-06-13 · **Repo:** `forge` (aka guardkitfactory orchestrator) · **Working mode:** Claude Desktop authors via Filesystem MCP (bash container can't reach `/Users`); Claude Code/OpenCode implements.

---

## What Forge is (one paragraph)

Forge is the **pipeline orchestrator for the build half** of the dark factory — the factory's night shift, not a product. Per DECISION-DF-003 the pipeline is split: planning (ideation → `/feature-plan`) stays **attended on frontier**; the build half (AutoBuild → `/task-work` → `/task-review`) runs **unattended on local inference (GB10)**, and Forge orchestrates only that build half. Per DF-001 frontier never touches the unattended path. Per DF-002 the loop must earn its keep on the Rich-hours ledger and recover GB10 plant cost.

## Current real state (verified from source 2026-06-11, do not assume otherwise)

**Built and working:** `forge serve` daemon (healthz, dispatcher, state channel, recovery); queue + SQLite lifecycle persistence + state machine; NATS plumbing (`pipeline.build-queued` consumer, approval pub/sub, fleet registration); Mode B planner (feature chain) and Mode C planner (review→fix→re-review, assumptions catalogued ASSUM-004…017); the guardkit adapter (`adapters/guardkit/run.py`, parser, progress subscriber); lifecycle bridge → `pipeline.*` envelopes. **Jarvis → Forge queue intake is validated live** (queued an AutoBuild from Open WebUI, 2026-06-11) — the whole intake path is proven.

**The one missing link:** the `autobuild_runner` subagent **node bodies are deliberate placeholders** — the graph transitions without actually invoking the adapter. Progress/escalation notifications to Telegram aren't wired. Forge still runs on the Mac, not the GB10.

**So the honest summary:** Forge is much further along than the April fleet index says, but the loop does not yet *execute a real build end-to-end unattended*. That gap is scoped (Phase UBS, below) but NOT yet built.

## The nearest-term scoped work (context, so new ideas don't collide with it)

**Phase UBS — Unattended Build Service** turns the daemon+queue+planners into a 24/7 GB10 build service. Seven features, sequenced visibility-before-autonomy: UBS-001 wire the runner node bodies to the adapter (keystone) → UBS-003 notifications to Jarvis/Telegram → UBS-002 budget guards → UBS-004 GB10 deploy + first supervised overnight run. Deferred and gated: UBS-005 two-Spark dispatch, UBS-006 architect align-advisory annotations, UBS-007 scheduled `architect_explore` drift reports.

A new idea can extend, reorder, or challenge this — just know it exists so the conversation doesn't re-derive it from scratch.

## The decisions / principles a new Forge idea has to respect (or consciously challenge)

- **DF-003 boundary:** Forge orchestrates the *build half only*. "Make Forge also drive planning" contradicts a live decision — allowed to argue, but flag it explicitly.
- **The loop is not the product.** Orchestration is explicitly *not* the differentiator (the rare quadrant is serving-layer fluency + full model lifecycle). Forge/harness work is "keep-warm," not the spine. An idea that re-frames Forge as the headline product should own that tension.
- **Throughput × Coach quality are one system.** A faster/more-autonomous loop atop a weak local Coach mass-produces unwired features at machine speed. **Autonomy follows verification quality** — it ratchets up only as the QA-Verifier fine-tune lands behind `--coach-model`. Any autonomy-increasing idea couples here.
- **SQLite authoritative; emits best-effort** (DDR-007) — notification/visibility ideas must not let emit-loss regress a build.
- **Worktree confinement non-negotiable** — every write path routes through `assert_within_worktree`.
- **Intake is deliberate, not watched** — v1 is `forge queue` / Jarvis; a filesystem/git watcher is a known later nicety, not a gap.
- **D38: viewports, not control surfaces** — PM-tool integrations are read/display, not Forge taking control of external tools.

## Open seams that are natural ideation territory

These are places the current design explicitly leaves room — good places for new thinking to land:
- **The night-shift job classes beyond building.** UBS-007 (drift reports) is framed as "the first non-build job class the night shift takes on" — what else belongs on an idle-GB10 queue? (Re-indexing, eval runs, dataset harvest passes, doc-truth checks…)
- **The ledger as product surface.** Success criterion #4 wants Workstream-A metrics to come from the Forge ledger (SQLite + LangSmith), not manual bookkeeping. What does that ledger expose, and to whom?
- **Two-Spark topology** (UBS-005) — two independent inference hosts, not a fused pool. Concurrency, affinity, and the specialist-contention relief it unlocks are under-explored.
- **Approvals as a two-way channel** (UBS-003 v1.1) — Telegram approve/reject → resume. The interaction design of "Rich's phone is the factory's escalation console" is barely sketched.
- **The harvest exhaust.** Run artefacts (coach turns, verdicts, outcomes) accumulate in the QA-Verifier dataset shape as a *side effect* of the loop running — the loop feeds its own quality flywheel. Ideas that strengthen that exhaust loop are high-leverage.

## Key docs (read on demand — don't pre-load all of these)

**Forge-specific, most current:**
- `forge/docs/research/ideas/unattended-build-service-scope.md` — Phase UBS, the nearest-term scoped work (read this first for current state)
- `forge/docs/research/ideas/unattended-build-service-build-plan.md` — the `/feature-spec` command sequence for UBS
- `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` — the deeper orchestrator architecture (46KB; the big design doc)
- `forge/docs/research/ideas/forge-build-plan.md` — the full Forge build plan (79KB; reference, not a read-through)

**Fleet / strategy context:**
- `forge/docs/research/ideas/fleet-master-index.md` — single source of truth for what exists where (NOTE: known to have been stale on Forge's own state as of the April writing — UBS-007 exists partly to keep it true)
- `forge/docs/research/ideas/big-picture-vision-and-durability.md` — the durability/positioning frame
- `forge/docs/research/ideas/fleet-architecture-v3-coherence-via-flywheel.md` — the flywheel framing
- `ai-transition/docs/fine-tuned-judgment-agents-findings.md` — the QA-Verifier / judgment-agent thread Forge's autonomy couples to
- `ai-transition/docs/DECISION-DF-002-ledger-based-tool-selection-and-plant-rate-economics.md` — the ledger/plant-rate economics behind success criterion #4

**Note on the existing `forge-ideas-overhaul-conversation-starter.md`:** that one (12 April) is about *overhauling stale docs*, a different purpose — not this forward-looking ideation surface. Don't conflate them.

---

## How to start the session

Rich brings the new idea(s). The job is to engage critically — stress-test against the decisions above, find where each idea lands among the open seams or whether it challenges a live constraint, and (only if the idea proves out) produce the usual artefacts: a scope doc + build plan pair in `forge/docs/research/ideas/`, ADR if it's a decision, conversation-starter handoff if it's heading to a build session. Exemplar before template; prove one thing before scaling the pattern.
