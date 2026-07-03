# Unattended Build Service — DF-006 & Supervisor Addendum
## Delta to `unattended-build-service-scope.md` · **forge only** · 2 July 2026

---

## Purpose

A short **delta** to the existing `unattended-build-service-scope.md` (Phase UBS) — **not a replacement, not a duplicate.** The "AutoBuild Supervisor" is the UBS; this addendum records three things the 2 July session added on top of that already-complete scope, and points a `forge` session at the existing keystone rather than re-scoping it.

**This session works only in `forge`.** It consumes `guardkit` as an installed dependency across the frozen `guardkit autobuild` / `--coach-model` seam and never edits it. Strategic frame + the seam contract: `dependable-forge-overview-qa-verifier-supervisor-improve-loop.md` (read first).

---

## 1. The supervisor already exists — do not re-scope it

The weekend "Claude-Code-drives-AutoBuild-and-fixes-failures" pattern is already productised, locally, as **Phase UBS**: the Forge daemon (`forge serve`) + queue + Mode B/C planners, driving AutoBuild through `adapters/guardkit/run.py`, with Mode C doing review → fix → re-review. It is **local by design** — UBS §3.5: "DF-001 holds… frontier never enters the unattended path."

**The keystone is FEAT-UBS-001.** ⚠️ **2026-07-02 correction (verified from source):** the `autobuild_runner` node bodies are **no longer placeholders** — TASK-ABW-001 wired `_node_running_wave` to invoke `guardkit autobuild` on 2026-05-14 (coach-ft-v3 routing added 2026-06-21). The keystone's *core deliverable is code-complete.* What remains to make the night shift real: operational validation (TASK-ABW-OPS, operator-handoff), closing the **coach-score population gap** (`last_coach_score`/`aggregate_coach_score` are plumbed but never set — ADR-ARCH-033; a prerequisite for UBS-002), and the sibling features UBS-002/003/004. This addendum's *strategy* is unchanged — only the keystone's status is corrected.

---

## 2. DF-006 confirmation for the build loop

`DECISION-DF-006` (frontier is a revocable teacher, not a critical-path worker) has been filed. For the UBS it **confirms rather than changes** the existing constraint: UBS §3.5's "frontier never enters the unattended path" is DF-006 for the build loop, now generalised from *cost* (DF-001) to *availability* — which is the risk the 15 June Max access-pull and the Fable suspension made concrete.

Action: when FEAT-UBS-004 (GB10 deployment) lands, confirm the daemon's model resolution (llama-swap `:9000` / LiteLLM `:4000`) carries DF-004's `fallbacks: []` + `context_window_fallbacks: []` guard, so an unattended build can never silently escalate to cloud. This is an audit line-item, not new design.

---

## 3. Tightened QA-Verifier coupling (the autonomy ratchet)

UBS design constraint 1 already names it: "autonomy follows verification quality — conservative thresholds at launch, ratcheted as the QA Verifier fine-tune lands behind `--coach-model`." The 2 July work sharpens *what* the ratchet keys off:

- The QA Verifier's **Phase 0 behavioural-evidence gates** (anti-stub AST, coverage/reachability, behavioural round-trip) are deterministic and land **before** any fine-tune. They are the first thing that makes a GREEN mean "it works" rather than "it type-checks."
- **Therefore the UBS budget-guard thresholds (FEAT-UBS-002) should ratchet against the presence of Phase 0 gates, not wait for the fine-tune.** A permissive Coach overnight is exactly the risk UBS constraint 1 flags ("mass-produces unwired features at machine speed") — and unwired/stubbed features are the `fs-01` class the Phase 0 gates catch. Concretely: keep unattended autonomy conservative until Phase 0 gates are live on the features being built; loosen as they come online.
- This is a **coupling via `--coach-model` and thresholds only** (UBS §5, out-of-scope confirms the QA Verifier fine-tune is a separate thread). No forge code implements the gates; forge only reads the richer verdict and sets policy over it.

---

## 4. Where dcode / RLMs fit (and where they do not)

The 2 July ideation considered a LangChain Deep Agents *code* harness (`dcode`) / recursive-agent pattern as the unattended supervisor. Grounded against the existing scope, the honest placement:

- **Not a replacement for the UBS.** The UBS's Mode C (review → fix → re-review via the Forge daemon + guardkit adapter) is already the local, frontier-independent self-healing loop. Introducing a second supervisor harness would be adding, not subtracting, and would discard a complete scope. The build-side substrate question is **settled**.
- **A candidate implementation for one node, at most.** If FEAT-UBS-001's Mode C fix-agent ever wants a more capable code-editing loop than the current planner provides, `dcode` (model-agnostic `--model provider:model` → llama-swap; headless `-n` + `--max-turns` + exit-124 budget + `-r` resume + shell allowlist) is a reasonable option to evaluate *for that node* — but only if the built-in Mode C proves insufficient. Prove the keystone first.
- **The genuinely open place is the *output* side.** The 14 June capture §6 explicitly flags the output-side deploy/verify **fix-agent** substrate as open (frontier Claude Code vs local). That is where "swap Claude Code for a local coding harness" is a live question, and where `dcode` most plausibly earns its place. DF-006 §6 resolves it in principle (local if unattended; frontier only if attended-by-exception). Carry `dcode` into the **output-side** loop's `/system-arch`, not the UBS.

---

## 5. Seam note

`forge` consumes `guardkit autobuild` via `adapters/guardkit/run.py` and reads the `--coach-model` verdict — now including the QA Verifier's additive `behavioural_evidence` block — as a black box. The `AutobuildState` / graph shape stays frozen (UBS FEAT-UBS-001 AC). This session does not edit `guardkit`; the evidence-bundle schema is owned there and versioned through the seam contract in the shared overview.

---

## Related documents

- `unattended-build-service-scope.md` + `unattended-build-service-build-plan.md` — the canonical UBS scope this addendum sits on top of. **Start here for the forge build.**
- `dependable-forge-overview-qa-verifier-supervisor-improve-loop.md` — shared frame + seam contract.
- `DECISION-DF-006` (`guardkit/docs/decisions/`) — the substrate constraint.
- `conversation-capture-2026-06-14-forge-meta-harness.md` — §6 (output-side fix-agent substrate open) and the improve loop this feeds.

---

*Prepared 2 July 2026 · forge-only. A delta to Phase UBS, not a new phase.*
*Keystone unchanged: FEAT-UBS-001. Do not edit `guardkit` from this session.*
