# Conversation Capture — Forge as a Continual-Learning Meta-Harness

> **Status update — 20 June 2026** (after the output-side-loop session). Content valid; two clarifications:
> - **Priority resequenced.** The *improve* loop described here is a real direction but now sits **behind** the LPA HSBC demo (9 July) and the output-side deploy/verify loop. The 20 June constraint analysis placed the bottleneck on the *output side*, not the build — so the improve loop is in the same "optimises a stage that is no longer the bottleneck" bucket as the deprioritised QA-verifier/Coach fine-tunes (findings D15). Revisit after the demo and the output-side loop.
> - **Different loop from the output-side loop.** The meta-harness *improve* loop (a proposer rewrites the build harness from traces) and the 20 June *output-side* loop (deploy → verify → debug a live system) are complementary, not competing. Two notes so they don't read as conflicting: "kill procedural Forge" here and "keep the executor minimal" there *agree* — the value is the diagnose-and-resolve loop plus harvest, not the executor; and DF-001 substrate is settled *for this loop* (proposer local) but **open** for the output-side fix-agent (frontier Claude Code vs local), so don't read "all local" as covering both.

**Date:** 2026-06-14 · **Repo:** `forge` (guardkitfactory orchestrator) · **Status:** FINDINGS — **pre-ADR, not a decision.** Captures the synthesis and open questions from an ideation session; lists what evidence would graduate it to an ADR.

**Working mode:** Claude Desktop ideation. Implementation stays in Claude Code / OpenCode. Builds on `conversation-starter-forge-ideation.md`.

**Grounded in:** Meta-Harness paper (Lee et al., arXiv:2603.28052), Interrupt 2026 keynote (Harrison Chase / LangChain), Block "Operation Pale Fire" red-team writeup, Spark Arena, and current model-spec sources. See References.

---

## 0. The organizing insight (the spine)

**The meta-harness is not a new idea to invent — it is the productization of a move we already run by hand, repeatedly: harvest the things we naturally fall into doing while driving Claude Code, then build tooling from them.**

This weekend's by-hand AutoBuild babysitting *is* the natural behaviour: diagnosing harness false-positives, hand-merging six features, writing failure modes into `guardkit-autobuild-quirks.md`. Forge-as-meta-harness is the tooling harvested from it — the loop that does, systematically and eventually locally-and-unattended, what Rich currently does manually each Saturday. Everything below is downstream of that single framing. It is the same "create tooling from the things we fall into doing naturally" pattern we have used time and again; this is just the highest-leverage application of it yet.

---

## 1. The shape we landed on (not yet a decision)

- **Kill "procedural Forge."** A dumb dispatcher plateaus at the quality of whatever it dispatches. The weekend's value was the intelligent *diagnose-and-resolve* loop, not the orchestration plumbing.
- **Three-loop model:** **plan** (attended, frontier-class) · **build** (unattended, local) · **improve** (periodic, local). The *improve* loop is the meta-harness outer loop — the thing that was missing a name. It does **not** contradict DECISION-DF-003; it **completes** it by naming harness-improvement as a periodic, design-time activity — the local analogue of the attended planning stage.
- **Proposer ≠ Player (the load-bearing distinction).** The outer-loop **proposer** authors harness edits, skills, and improvement tasks — this is *origination*. The inner-loop **Player** executes builds and is *wrapped* by the harness. Different seats, different model requirements. Conflating them is the trap.

---

## 2. Sequencing — climb the cheap layers first

Harrison's three-layer stack (model → harness → context) with the cost gradient running context → harness → model:

| Layer | Mechanism | Status |
|---|---|---|
| **Context** (cheapest) | Skills + `fleet-memory` accumulation (Hermes/Voyager pattern) | Already alive — the weekend agent wrote quirks to memory unprompted |
| **Harness** (middle) | Proposer rewrites AutoBuild harness code from execution traces | The Meta-Harness method proper; raw traces are the key ingredient (summaries *hurt*) |
| **Model** (most expensive) | Fine-tuned SLM Player/Coach | **Last, and gated** on corpus volume + ledger — exactly as QA-Verifier is staged |

**Why model-layer is last:** the weekend shows the SLM is **not** the bottleneck — Opus sailed; the *harness* kept tripping. Harness-layer learning precedes any fine-tune. Don't fine-tune a model to fix a problem that is actually a harness false-positive.

---

## 3. The model question — capability availability ≠ selection criterion

**Hardware reality (corrected — the "140GB Kimi" figure was wrong):**

- **Kimi K2.5 / K2.6** are **1T-parameter MoEs (32B active)**. Real footprint ~340–350GB at aggressive 2-bit, ~610–630GB at INT4. **They do not fit 256GB of pooled Spark** except via heavy RAM/NVMe offload at single-digit tok/s — a non-starter for a proposer reading dozens of trace files per iteration. (The "140GB" is the classic active-params miscalculation.)
- **DeepSeek V4 Flash** — **284B total / 13B active, MIT-licensed**, native FP4+FP8, ~158GB weights / ~170–175GB with KV cache. **Fits the 2-node cluster** (≈256GB pooled) via cross-node expert parallelism, with headroom. Being sparse MoE, it EP-shards far more bandwidth-friendly over ConnectX-7 than a dense model of the same size.

**Principle — "can ≠ should."** The fact that the cluster *can* run V4 Flash does not make it the best proposer. Single-node **GPT-OSS-120B** or a **Qwen ~122B-class** model may be all we need — especially for the first improvement class (recurring harness false-positives), which is pattern-recognition over traces, not deep reasoning. Selection is by **DF-002 ledger** (eval performance per Rich-hour and plant-cost), not by raw capability.

**Selection method (Rich's existing working pattern):** *change things, change models, run evals.* This is exactly how the LangChain-DeepAgents AutoBuild-via-Opus work proceeds now. **Once that AutoBuild variant is working**, run the same diagnosis/build tasks across candidate proposers and grade them against the **gold-standard Opus weekend traces**. Tiered plan:

1. **Start single-node** (GPT-OSS-120B / Qwen-122B-class) — preserves both-node concurrency.
2. **Escalate to fused-cluster V4 Flash** (expert-parallel, via `sparkrun`) only if the cheaper proposer plateaus.
3. **Frontier (Opus) = eval yardstick only**, never a runtime dependency. The gold traces are already captured, for free.

**Topology consequence:** V4 Flash > 128GB forces both-node fusion when the proposer runs → **temporal separation**: the build loop runs the SLM single-node continuously; the improve loop spins V4 Flash across both nodes periodically, then spins down. The improve loop's latency-insensitivity is what makes monopolising the cluster briefly affordable.

---

## 4. The weekend already wrote the proposer's first backlog

The `fleet-memory` AutoBuild logs (FEAT-MEM-02 → 07, Opus-in-Claude-Code) aren't just a result — they're the meta-harness's opening work items, written by hand.

**The headline, in Rich's own words:** *"Every stall was a harness false-positive, not a code defect."* That is the empirical case for the entire harness-layer thesis.

**Backlog already enumerated in the logs:**

- `guardkit autobuild complete` merge/archive phases are placeholders → **hand-merged six times** → automate. (Highest-frequency item.)
- BDD gate exit-4: missing `features/conftest.py` collection bridge (repo-wide fix).
- `plan_audit` path-label bug — a markdown-link label read as a repo path.
- Honesty/pollution gate false stalls (`coverage.json` flagged on a verified-good run).
- venv pollution (editable install pointing at the worktree's `src`); `--fresh` footgun (hard-reset wiped approved work).

**Governance signal — item 5, a false *approval*:** a green Coach hid a real `app.py` lifespan/DI regression because the smoke gate missed wave-4 changes and per-task verification scoped tests too narrowly. This is the verifier-gaming failure the Block writeup warns about (harden the system around the model; treat its output as untrusted; never let it be its own check), **caught in the wild**. It is the prioritised Coach-strengthening task and the concrete proof of "autonomy follows verification quality." Reassuring property: the other four modes were false *failures* (stricter than reality) — the safe direction to hill-climb against.

The proposer's job-one is therefore not speculative: read `guardkit-autobuild-quirks.md`, fix the false-positive gate classes, automate the merge, close the smoke-gate hole.

---

## 5. `fleet-memory` is now the substrate

- Replaced Graphiti, freeing ~28GB of GPU memory tax — headroom that helps fit a bigger Player or proposer on a single node.
- Doing **double duty**: the RAG/memory in the SLM scaffolding **and** the context-layer learning store (skills, failure modes, fed-forward approved assumptions). The structured-uncertainty §3.4 feed-forward loop moved here intact and is already running.
- **Caution (exemplar-before-template):** `fleet-memory` is days old, BDD binding ~58%, `TASK-RLY-007` (DLQ contract) deferred pending a live broker. It is about to become the memory spine for the whole learning architecture. **Validate its retrieval quality before** the proposer and Player both lean on it — a meta-harness built on an unvalidated memory layer learns from a distorted mirror.

---

## 6. How this sits with existing decisions

- **DF-001 (no cloud API on the critical path):** *fully satisfied* — proposer, Player, and Coach all local. Frontier touches only the eval yardstick (weekend traces, already paid for).
- **DF-003 (Forge orchestrates the build half):** *completed, not challenged* — the improve loop is a periodic design-time activity, the local analogue of the attended planning stage.
- **Cost-inversion thesis:** *strongest form* — the market meters frontier for implementation *and* improvement; the factory runs both local and borrows frontier once, as calibration.
- **"Orchestration is keep-warm, not the spine":** *held* — the defensible part is the harvest/flywheel (the `agentic-dataset-factory` pattern pointed at harness improvement), not the orchestration plumbing. Forge is the vehicle where they meet.
- **Trace richness:** connects to the existing `ADR-FLEET-001-trace-richness.md` — the Meta-Harness "raw traces are the key ingredient" finding likely *extends* that decision into the build-loop trace store (see §8).

---

## 7. Open questions — what would graduate this to an ADR

1. **Proposer eval result:** does a local proposer — and which one — match Opus on the weekend traces? (The `architect_align`-vs-`architect_greenfield` discipline, pointed at the proposer seat. The proposer is origination, the mode our specialist eval has *not* yet validated.)
2. **Single-node sufficiency:** do GPT-OSS-120B / Qwen-122B-class clear the first improvement class, or is fused-cluster V4 Flash actually needed?
3. **`fleet-memory` retrieval validation** before the learning loops depend on it.
4. **Plateau test:** whether harness-layer improvements plateau such that the SLM fine-tune earns its place (corpus volume + ledger).
5. **Trace-capture schema** (§8) — the one concrete near-term dependency for everything above.

---

## 8. The immediate, near-zero-cost move

**Capture AutoBuild runs in proposer-readable shape now** — per run: harness version + full execution trace + verdict/scores + outcome (the Meta-Harness filesystem shape). This reframes "hook up build monitoring": captured as *traces* rather than status pings, monitoring **is** the first brick of the improve loop, and the same artifact becomes the fine-tune corpus later. Dual-purpose; the cheap use (a proposer reads the traces) is available immediately.

This is the harvest pattern made concrete: the thing we already do — eyeball the run, note what tripped — becomes a structured store a proposer reads. It should be sequenced as a UBS-001-adjacent keystone, not a deferred nicety, and is the natural input to the proposer eval in §7.1.

---

## References

- **Meta-Harness: End-to-End Optimization of Model Harnesses** — Lee, Nair, Zhang, Lee, Khattab, Finn (Stanford / MIT / KRAFTON). arXiv:2603.28052 — https://arxiv.org/abs/2603.28052 · project page https://yoonholee.com/meta-harness/ · TerminalBench-2 artifact https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact
- **Interrupt 2026 Keynote — Continual Learning, Agent Divergence, and Fleet** (Harrison Chase, LangChain). https://www.youtube.com/watch?v=R9K2574YEAg · local insights & transcript under `YouTube Channel/insights|transcripts/`
- **How We Red-Teamed Our Own AI Agent (Operation Pale Fire)** — Block Engineering. https://engineering.block.xyz/blog/how-we-red-teamed-our-own-ai-agent-
- **Spark Arena** — DGX Spark LLM leaderboard; `sparkrun` workload launcher. https://spark-arena.com · https://github.com/spark-arena/sparkrun
- **Model specs:** DeepSeek V4 Flash (284B/13B, MIT, ~170GB) — NVIDIA developer blog & HF `deepseek-ai/DeepSeek-V4-Flash`; Kimi K2.5/K2.6 (1T/32B, ~340GB+ @ 2-bit) — Unsloth docs & HF `moonshotai/Kimi-K2.6`.

---

*Prepared 2026-06-14. Pre-ADR findings — revisit per §7 once the LangChain-DeepAgents AutoBuild variant is working and the proposer eval has run.*
