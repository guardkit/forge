# Forge + fleet-memory state review (2026-06-24)

Zoom-out review of forge (runbook executor / output-side loop), fleet-memory (Graphiti-migration
readiness), and the QA-Verifier, grounded in `docs/research/ideas/` + current code. Produced from a
5-way parallel doc+code assessment.

## The three loops (orientation)

1. **Build loop** — AutoBuild Player↔Coach. Working.
2. **Output-side deploy/verify loop** (FORGE-OL) — **first exemplar just landed** (this is the bottleneck the 2026-06-20 analysis identified; prioritised over the improve loop on 2026-06-14).
3. **Improve loop / QA-Verifier** — fine-tuned Coach; *deliberately last and gated*. "Autonomy follows verification quality."

## 1. Runbook executor — fully implemented for the exemplar; fuller vision deferred by design

**Verdict: fully implemented against the minimal scope (FORGE-OL-01/02/03); PARTIAL vs the full vision (intentional, harvest-after-exemplar per D13).**

Built & proven (ran the real fleet-memory stand-up end-to-end):
- Typed Runbook/Step model + STRICT SQLite persistence (`runbooks`, `runbook_steps`), per-step status queryable.
- Dispatch-by-step-type executor + `StepTypeRegistry` (open/closed); result-before-advance ordering.
- Two shell step types: `deploy_compose`, `run_smoke_tests` (subprocess core: 600s timeout, 1 MiB output cap, credential scrubbing; env_file passed as path only).
- NATS lifecycle events: `runbook.{started,step-started,step-result,complete,escalated}.{id}`, in order, fire-and-forget (truth in persistence).
- Resume via `current_step_index`; **crash-recovery claim-lease** (15-min, no double-run).
- `forge runbook run <path>` CLI: persist-before-execute (ASSUM-007), NATS auth (`FORGE_NATS_CREDS|TOKEN|USER+PASS`), best-effort connect (NoOp fallback).
- `awaiting_approval` status **modeled** (gates-as-data) — not enforced.

Deferred (design leaves slots; not rework):
- **Gate *enforcement*** (transition `awaiting_approval`→`pending` on external decision; re-entry after approval).
- More step types: `run_autobuild` (autobuild_runner placeholder only), `invoke_claude_code_debug` (supervised debug), approval-gate, credential-step, `deploy_cloud`, `run_tests`.
- **Fix-agent / supervised-debug loop** (DF-001 substrate question).
- **Runbook generator** (`generate.py`) — harvest after 2–3 hand-authored exemplars (D13).
- **Dashboard / projection** layer (record + events emitted; presentation separate).
- Irreversible-edge safeguards (forced by the LPA / AWS target).

The output-side loop is **not complete** — it has its first exemplar (the proving ground). A `/system-arch` pass on the full loop is expected now that an exemplar exists.

## 2. fleet-memory — ~85% there; FEAT-MEM-04 relay is the single blocker to start the Graphiti cutover

| Feature | Status |
|---|---|
| MEM-01 storage substrate (Postgres 16 + pgvector, 769-dim nomic-embed, LangGraph AsyncPostgresStore) | ✅ **+ LIVE on NAS `whitestocks:5433`** (TASK-MEM-008 done 2026-06-23) |
| MEM-02 typed payload registry (7 Pydantic types) | ✅ |
| MEM-03 deterministic writer (UUIDv5 natural keys, content-hash upsert, supersession, embed-on-write, **zero-LLM**) | ✅ (73/73 tests) |
| **MEM-04 relay integration (MEMORY stream → writer)** | ❌ **PLANNED, not built — THE gap** |
| MEM-05 retrieval API + token-budgeted assembly + **probe-set parity harness** | ✅ |
| MEM-06 Memory MCP server (FastMCP: search/write/supersede) | ✅ |
| MEM-07 re-index pipeline (markdown→typed payloads, idempotent <5 min) | ✅ merged |
| MEM-08/09 guardkit cutover + decommission runbook | ❌ not started (cross-repo) |

**Graphiti today** = the active memory substrate (FalkorDB `whitestocks:6379`): coach-context + feature-plan context + CLI retrieval. But `TASK-REV-GROI` found **0/10 consumption paths high-value**, ~**28 GB always-on** qwen-graphiti extraction, **£30/weekend** Gemini fallback. fleet-memory replaces it with identical retrieval semantics, deterministic LLM-free writes, NAS-hosted (storage-only cost).

**Migration readiness: PARTIAL — ready to START once the relay is built.** Storage is already live (one prerequisite the original plan listed is now done). Critical path:
1. **Build FEAT-MEM-04 relay** (~3–5 days) — FastStream durable consumer on the MEMORY stream → registry/chunking → writer. *Only missing write-path component; without it the store stays empty.*
2. Capture **Graphiti baseline** on the fixed probe set (harness exists).
3. Run **FEAT-MEM-07 full re-index** into the live NAS Postgres.
4. **Probe-set parity eval** (floor = parity) → go/no-go.
5. If parity holds → **FEAT-MEM-08 cutover** (Graphiti behind a feature flag, default off) → **FEAT-MEM-09 decommission** (reclaim ~28 GB).

## 3. QA-Verifier — far end of the roadmap, gated, not started (by intent)

The fine-tuned Coach the autonomy dial couples to. Sits at the END of: (1) **UBS core** (unattended build service, FEAT-UBS-001..004 — the night shift), (2) **proposer-eval** (Stages A–I), (3) **QA-Verifier fine-tune** (gated on corpus). Deprioritised per 2026-06-14 (behind the LPA/HSBC demo + output loop).

- **Prerequisites:** accumulated own-Player trace corpus (harvest exhaust), `--coach-model` seam, fine-tune gating (≥N production traces; Meta-Harness gold-for-eval / production-for-fine-tune split).
- **What the output loop unblocks:** the *dataset accumulation pathway* — terminal states + Coach rationale become harvest corpus as a byproduct. But traces only accrue once **UBS is actually running**.
- **Remaining before the fine-tune:** proposer-eval Stages D–I; UBS core deployed (GB10 daemon + first clean overnight); trace-richness validation; own-Player-trace corpus accrual; fine-tune gating threshold policy.

## Net assessment + highest-leverage next move

- **forge:** build loop works; output loop has its first real exemplar (executor done for that scope); full output-loop arch + deferred step-types/gates/fix-agent are the next forge investment (LPA-driven).
- **fleet-memory:** storage live on the NAS; writer/retrieval/MCP/re-index done; **FEAT-MEM-04 relay is the single blocker** to begin the Graphiti cutover.
- **QA-Verifier:** gated/deferred; the loop now exists to feed it, but it waits on UBS + proposer-eval + corpus.

**Highest-leverage next build: FEAT-MEM-04 (the relay).** It flips fleet-memory from "infrastructure ready" to "migration in progress" and starts the trace-exhaust flywheel the QA-Verifier eventually needs.
