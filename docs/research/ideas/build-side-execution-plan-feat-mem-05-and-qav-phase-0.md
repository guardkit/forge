# Build-Side Execution Plan — FEAT-MEM-05 run + QA Verifier Phase 0
## Execution handoff (NOT an ideation doc) · 2 July 2026 · Claude Desktop

---

## Why this document exists — read this first

We keep hitting the same failure mode: **ideate → get busy → lose the thread → re-ideate.** Today's session spent its first half reconstructing findings that were lost (the June findings doc never persisted), re-deriving the stub problem from scratch, and re-discovering that the "AutoBuild Supervisor" already exists as the UBS. None of that was new thinking — it was re-thinking.

This document is the fix. It is an **execution handoff**, not another planning doc. The next session's job is to *do the NEXT ACTIONS*, not to re-open what is already decided. Two rules for whoever picks this up (human or Claude):

1. **Do not re-open anything under "Decided — do not reopen."** If you feel the urge to re-derive it, the answer is already here or in the linked doc. Re-derivation is the trap.
2. **Execute the NEXT ACTIONS.** They are concrete, with the exact files and commands. If one is blocked, record *why* in this doc (append a dated note) and move to the next — so the blocker is captured, not rediscovered.

If you do nothing else: **capture as you go, and commit immediately** (an uncommitted MCP-written file cost us a repo sync today — see "Findings discipline").

---

## Status snapshot — you are here

| Thing | State | Where |
|---|---|---|
| DECISION-DF-006 (frontier = revocable teacher) | ✅ On disk | `guardkit/docs/decisions/DECISION-DF-006-*.md` |
| DF-006 seeded into fleet-memory | ⏳ Blocked on GPU (dataset-factory holds the swap slot); seeder written | `fleet-memory/scripts/seed_df006.py` — run when embed model can load |
| June findings (scaling / output-bottleneck) | ✅ Reconstructed on disk | `forge/docs/research/ideas/factory-scaling-and-output-bottleneck-findings.md` |
| Shared overview + seam contract | ✅ On disk | `forge/docs/research/ideas/dependable-forge-overview-*.md` |
| QA Verifier /feature-spec starter (guardkit) | ✅ On disk | `guardkit/docs/research/ideas/qa-verifier-behavioural-evidence-gates-conversation-starter.md` |
| UBS DF-006/supervisor addendum (forge) | ✅ On disk | `forge/docs/research/ideas/unattended-build-service-df006-and-supervisor-addendum.md` |
| **FEAT-MEM-05 parity harness** | ⚠️ **Function built, NOT runnable end-to-end** | see NEXT ACTION 1 |
| QA Verifier Phase 0 | 🔜 Scope next | see NEXT ACTION 2 |
| FEAT-UBS-001 (wire autobuild_runner) | 🔜 After QAV Phase 0 | `forge/docs/research/ideas/unattended-build-service-scope.md` |

**Immediate next action:** make FEAT-MEM-05 actually runnable (NEXT ACTION 1), because it is both the first QA-Verifier behavioural-oracle gate *and* a time-sensitive dependency — one of its sub-steps must happen before FalkorDB/Graphiti is decommissioned.

---

## The build-side plan in one picture

Three loops (your model, from `conversation-capture-2026-06-14-forge-meta-harness.md`):

- **plan** — attended · frontier (DF-003, unchanged)
- **build** — unattended · local = the Unattended Build Service (UBS)
- **improve** — periodic · local = the meta-harness

Components map to loops: **QA Verifier** = the oracle inside the build loop (makes a GREEN mean "it works") and the dial the UBS ratchets autonomy against · **UBS** = the build loop (keystone FEAT-UBS-001) · **meta-harness** = the improve loop (measures the QA Verifier via the `fs-*` corpus). Substrate for all three is governed by DF-006. Full frame + the guardkit↔forge seam contract: the shared overview doc. **Do not re-derive this here.**

---

## Decided — do not reopen

| # | Decision | Where the reasoning lives |
|---|---|---|
| 1 | **DF-006:** frontier is a revocable teacher, not a critical-path worker. Build + improve local; frontier only for attended planning and a one-time eval yardstick. | `DECISION-DF-006` |
| 2 | **Stub root cause:** AutoBuild's oracle (spec + co-generated tests + code review) cannot see a stub returning plausibly-shaped data; Player and Coach share that oracle. | shared overview |
| 3 | **The fix is evidence, not the fine-tune:** an oracle the Player didn't author — anti-stub AST scan + coverage/reachability + behavioural round-trip. Deterministic Python; no fine-tune needed for Phase 0. | shared overview / QAV starter |
| 4 | **`fs-01` = the stub class, already in the wild** (FEAT-MEM-04, 7/7 SUCCESS hid a lifespan/DI regression). QA Verifier gates fix it; the meta-harness corpus measures it. | shared overview |
| 5 | **The "AutoBuild Supervisor" already exists = UBS** (Phase UBS, NOT STARTED). Keystone FEAT-UBS-001 = wire `autobuild_runner` placeholders to the guardkit adapter. **Do not scope a duplicate.** | UBS scope + addendum |
| 6 | **dcode/RLMs are not the supervisor.** At most a fix-agent option for one node; their genuinely-open home is the *output-side* fix-agent (14 June capture §6). | UBS addendum |
| 7 | **Repo delimiters / seam:** guardkit owns the Coach + `--coach-model` verdict + the additive `behavioural_evidence` block; forge consumes guardkit via `adapters/guardkit/run.py` as a black box. Each session owns one repo; the seam is versioned in the overview and changed only via ADR. | shared overview |
| 8 | **QAV decisions A1–A6 locked:** LLM judge + deterministic evidence; oracle the Player didn't author; Phase 0 before Phase 1; base = Gemma 4 26B-A4B MoE; golden set via Opus opportunistically; Option B-min only. | QAV starter |
| 9 | **Priority reality:** the LPA HSBC demo (9 July) + the output-side loop sit ahead of build-side improve work (June D15; 14 June status update). QAV Phase 0 is the only build-side item cheap enough to run alongside the demo. | findings doc |

---

## NEXT ACTION 1 — Make FEAT-MEM-05 runnable, record baselines, run it

**Mode:** direct implementation (small; not a `/feature-spec`). **This is the first behavioural-oracle gate for the QA Verifier, and it is time-sensitive.**

**What's actually there (verified 2 July):**
- `src/fleet_memory/retrieval/probe_harness.py` — `run_probe_harness(probe_set, search_fn, assemble_fn)` is real logic (loops probes → `search` → `assemble_context` → compare → `ParityReport`), with `MIN_PROBE_SET_SIZE = 15` and `PARITY_TOLERANCE = 0`.
- `eval/probe_set.json` — 16 guardkit queries (clears the ≥15 gate), created 2026-06-27.
- `tests/unit/test_probe_harness.py` — unit test (passed; this is what the Coach approved).

**The three gaps that mean it has never actually run (this is the fs-01 class, inside the oracle itself):**

1. **No runner / no baselines.** `run_probe_harness` takes its `probe_set` as an argument and there is no `load_probe_set()` (the docstring references one that does not exist). Worse, `ProbeQuery.baseline_answer` is a *required* field and `probe_set.json` has **no baselines** — only queries. So nothing can even construct the `ProbeQuery` list yet.
2. **Baselines must be recorded from Graphiti — before it is torn down.** The probe set's `_meta` records the capture command: `guardkit graphiti search "<query>" -n 5`. The baselines are the *recorded Graphiti answers* for the 16 queries. FalkorDB/Graphiti is being decommissioned (FEAT-MEM-09). **Record the baselines while it is still live, or the parity gate can never be established.** This is the time-sensitive piece.
3. **The parity metric cannot work as written.** Exact-string equality (`actual != expected`, tolerance 0) between fleet-memory's assembled `context_block` and a Graphiti graph result will flag all 16 as divergent — the two systems will never be byte-identical. The metric needs to be **semantic/overlap-based** (did the expected key documents / `source_ref`s / facts come back?), not string identity. The task file flagged `ASSUM-007` (tolerance) as low-confidence; the deeper issue is the comparison itself.

**Concrete sub-steps:**

- **1A — Record Graphiti baselines (do first; time-sensitive).** For each of the 16 probes, run `guardkit graphiti search "<query>" -n 5`, capture the top results, and store them as the baseline for that query (extend `probe_set.json` with a `baseline` field per probe, or write `eval/probe_baselines.json` keyed by query). Consider recording the returned `source_ref`s / entity set, not raw prose — that is what a semantic-overlap metric will compare against.
- **1B — Write the runner** (`scripts/run_parity.py`, mirror `seed_df006.py`): load `eval/probe_set.json` + baselines → build `ProbeQuery` list → `async with async_store_context(settings) as store` → call `run_probe_harness(probes, lambda req: search(req, store), assemble_context)` where `search` is `fleet_memory.retrieval.core.search` and `assemble_context` is `fleet_memory.retrieval.assembly.assemble_context` (confirm signatures) → print/save the `ParityReport`. **Note: this needs the embed model resident** (16 query embeddings) — same GPU-contention caveat as the seeder; run when the dataset-factory frees the slot.
- **1C — Replace exact-string parity with overlap.** Change the comparison to a set-overlap / recall@k over retrieved `source_ref`s (or key entities) against the recorded baseline, and make `PARITY_TOLERANCE` express an overlap threshold, not a divergence count of exact strings. This is the one real design decision in this action.

**Done means:** the runner executes all 16 probes against the live fleet-memory store, emits a parity report with a *meaningful* score, and the ≥15-size gate passes. **What it tells us:** (a) whether fleet-memory retrieval is at parity with Graphiti on guardkit's harvested-knowledge domain — the FEAT-MEM-08 cutover gate; (b) that the behavioural-oracle *pattern* works end-to-end before it is wired into the Coach as a QA-Verifier gate; (c) it hands NEXT ACTION 2 a proven independent oracle.

**Capture on completion:** append the parity result + any metric decision to this doc, and file `FEAT-MEM-08` (cutover) / `FEAT-MEM-09` (decommission) if the gate passes — they are named in memory but not yet on disk as feature files.

---

## NEXT ACTION 2 — Scope QA Verifier Phase 0

**Mode:** `/feature-spec` → `/feature-plan`, **guardkit session only.** Open `guardkit/docs/research/ideas/qa-verifier-behavioural-evidence-gates-conversation-starter.md` (self-contained; decisions A1–A6 locked; open questions listed there).

**Phase 0 = three deterministic gates, no fine-tune:** anti-stub AST scan · coverage/reachability gate · behavioural acceptance oracle. **NEXT ACTION 1 makes the behavioural oracle concrete** — the parity-harness pattern (an independent round-trip the Player didn't author) generalised into a per-feature hook behind `--coach-model`.

**Use FEAT-MEM-05 as the live case study in the spec:** the parity harness passed its unit test and Coach review yet was non-functional end-to-end (no runner, no baselines, impossible metric). That is exactly what Phase 0's gates must catch — a coverage/reachability gate would flag `run_probe_harness` as never exercised against a real store; an anti-stub/behavioural gate would catch "unit test passes, end-to-end doesn't." Dogfood: every task in the QAV Phase 0 buildplan must itself carry an independent behavioural check, not only co-generated unit tests.

**One cross-repo consequence only:** Phase 0 adds a `behavioural_evidence` block to what `--coach-model` emits. That is the frozen seam forge consumes — keep it additive and versioned (overview §seam), never a forge edit.

---

## Then (sequence after the two next actions)

3. **FEAT-UBS-001** (forge session) — wire the `autobuild_runner` placeholders to the guardkit adapter; gated by QAV Phase 0 for unattended safety. Scope is complete in `unattended-build-service-scope.md`; the DF-006/coupling delta is in the addendum. Do not re-scope.
4. **QAV Phase 1** — golden calibration set (Opus via Max, opportunistic per DF-006) → fine-tune Gemma 4 26B-A4B to read the evidence bundle and generalise the verdict.
5. **UBS-002/003/004** (budget guards, Telegram notifications, GB10 deploy + first supervised overnight) then the **improve loop** (meta-harness) — measured by the `fs-*` corpus.

---

## Open loops — genuinely undecided (this is what IS still to think about)

These are *not* settled; the next session may decide them (unlike the "do not reopen" list):

1. **Parity metric shape** (NEXT ACTION 1C) — set-overlap of `source_ref`s vs recall@k vs something richer; the tolerance value.
2. **Missing-vs-failed oracle policy** (QAV) — is an *absent* behavioural oracle a RED, a WARN, or a spec-gate requiring one? (A failed oracle is a hard RED.)
3. **COACHGATHER01 root cause** — four guards compensate for Phase-A gather degrading to B-min 100%; does the evidence-gate work let gather be fixed or retired?
4. **Output-side fix-agent substrate** — DF-006 §6 resolves it in principle (local if unattended; frontier only if attended-by-exception); the concrete choice belongs to the output-side `/system-arch`, where dcode may earn its place.
5. **UBS driver model** — which local model drives the harness (workhorse MoE vs Qwen3.6-27B dense vs GPT-OSS-120B); converges with the "one model, no swap" question.

---

## Findings discipline — how we stop re-ideating

- **Capture concretely, in the repo, as you go** — decisions → ADRs in `guardkit/docs/decisions/`; session state → these `docs/research/ideas/` docs. A finding not written down is a finding you will re-derive.
- **Commit immediately after writing.** MCP-written files are untracked; an uncommitted file does not survive a repo sync (this bit us today — the seeder was written but not pushed, so the GB10 pull had nothing). `git add` is the step that gets missed.
- **Sessions are execution, not re-ideation.** Start from the Status snapshot, do the NEXT ACTION, append what happened. If a decision genuinely needs revisiting, mark it explicitly and say why — don't silently re-open it.

---

## Index — everything on disk for the build-side plan

- `guardkit/docs/decisions/DECISION-DF-006-frontier-is-a-revocable-teacher-not-a-critical-path-worker.md`
- `forge/docs/research/ideas/dependable-forge-overview-qa-verifier-supervisor-improve-loop.md` (frame + seam)
- `guardkit/docs/research/ideas/qa-verifier-behavioural-evidence-gates-conversation-starter.md` (NEXT ACTION 2)
- `forge/docs/research/ideas/unattended-build-service-scope.md` + `-build-plan.md` (step 3) + `-df006-and-supervisor-addendum.md`
- `forge/docs/research/ideas/factory-scaling-and-output-bottleneck-findings.md` (June anchor)
- `forge/docs/research/ideas/conversation-capture-2026-06-14-forge-meta-harness.md` (improve loop, `fs-*` corpus)
- `fleet-memory/src/fleet_memory/retrieval/probe_harness.py` + `fleet-memory/eval/probe_set.json` (NEXT ACTION 1)
- `fleet-memory/scripts/seed_df006.py` (run to seed DF-006 when the GPU frees)
- **this doc** — the execution handoff; start here.

---

*Prepared 2 July 2026 · build-side execution handoff. Commit it. Start the next session from the Status snapshot, not from scratch.*
