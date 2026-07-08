# Factory Scaling, Presentation Layer & Output-Side Bottleneck — Findings & Decisions

## Ideation session capture · 19–20 June 2026 · Claude Desktop

> **Reconstruction note (2 July 2026).** The original of this document was produced in the 19–20 June "FinProxy architecture and deployment model" session but only ever delivered as a Claude Desktop download — the Filesystem MCP write did not land, so it never reached the repo, even though the three companion conversation-starters and the session wrap-up all cite it as their strategic anchor. This file is a faithful reconstruction from the conversation transcript. Decisions **D1–D10** are recovered near-verbatim; **D11–D15** are reconstructed from the "decisions to graduate" summary and the companion `forge-output-loop-conversation-starter.md` / `output-loop-exemplar-scope.md`, where the verbatim rows were lost — their *substance* is faithful but the exact wording may differ from the original. Cross-check against those companions before treating any D11–D15 row as canonical.

> **Graduated 2026-07-08.** The D11–D15 substance is register-filed as clauses **DF-014.1–.5**
> (`ai-transition/docs/decisions/DECISION-DF-014-output-side-spine-graduated-d11-d15-by-substance.md`,
> ACCEPTED by Rich 2026-07-08). **Cite DF-014.n, never bare D-ids.** This copy is **witness W2**
> — DF-014 §1 records that its reconstructed D11–D15 table diverges from the 06-23 committed copy
> (witness W1, `../factory-scaling-and-output-bottleneck-findings.md`) including on what D14 was,
> and flags this note's "never reached the repo" premise against W1's existence as an open
> provenance question (DF-014 §1 Finding 3, unadjudicated at acceptance). D1–D10 are unaffected.

---

## Purpose of this document

Captures the decisions from the ideation session triggered by James's scaling questions and Rich's output-side bottleneck. It is the strategic anchor that the per-workstream conversation-starters reference. Durable decisions flagged in "Decisions to graduate" should be promoted to `DECISION-DF-xxx` records or repo ADRs.

---

## Context

James raised three questions; Rich added a fourth problem that turned out to be the sharpest:

1. **How do we scale beyond Rich + Claude + the factory?**
2. **How can James contribute?** (non-technical; overwhelmed by the terminal)
3. **FinProxy is nearly out of money** — do work as debt, possibly deploy them something that lets them use the factory themselves.
4. **(Rich) The output side is the bottleneck.** Development is now fast (AutoBuild done, both local and Claude SDK). Integrate / test / deploy / debug — everything *either side* of development — is what eats focused blocks of Rich's time and stalls delivery.

---

## What this session resolved

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | The scaling ceiling is **curation bandwidth at the attended front stages**, not compute. | DF-003: planning stages are the irreducibly human part. "Scale beyond Rich" = more product-owner / curator seats at the front feeding a parallelised build back-end, not cloning Rich. Much smaller ask. |
| D2 | Two of James's three questions are **already answered by the existing dev-pipeline architecture**. | Multi-tenancy is by-design (FINPROXY NATS account already provisions james, rich_finproxy, mark scoped to `finproxy.>`; "Rich sees everything, James sees only FinProxy"). Per-project cost tracking already sits in the future-extensions list. The genuinely new work is the presentation layer + the FinProxy productization fork. |
| D3 | The real missing piece is a **presentation tier**. | Everything built so far is developer-facing (agents wired as MCP in Claude Desktop for Rich's own use; Claude Code in the terminal). James can use neither. NATS permissions are plumbing, not a door. |
| D4 | **NATS stays the orchestration source of truth.** The prior steer against "orchestration" was against *framework-as-brain*, not against the event bus. | ADR-SP-002 already makes the event bus authoritative, deliberately, because a typed transport + JetStream does not churn. "We need orchestration to tie this into a system" is true and *already satisfied*. Tying it together means more producers / consumers on the bus you have — not a new brain (LangGraph / NeMo / LAP) on top of it. |
| D5 | **Own the spine; treat channels as adapters.** | Same pattern the architecture already uses for PM tools: interchangeable adapters behind NATS, inputs-and-outputs, never source of truth. Applies identically to Slack, Cowork, Codex, web UI. |
| D6 | The **delivery dashboard is the presentation spine and build #1** of the presentation layer. | It is the client artifact, and for the work-as-debt deal it is the commercial instrument — the ledger of what FinProxy's deferred fee bought. Pure NATS-consumer web app on owned hardware: cannot be switched off or held to ransom. The PO-agent door later folds into this same app, so the core idea→spec→approve loop never touches anyone else's platform. |
| D7 | **Slack = thin adapter (low-regret); Cowork / Codex = optional, off the critical path.** | James lives in Slack today, so a thin publish-out / listen-in bot is the fastest door — and because the canonical record lives in NATS / fleet-memory, it is swappable for Mattermost / Zulip in an afternoon. Cowork / Codex are frontier-model agent *clients* over MCP (wiring that already exists); kept strictly optional because their cost / availability is outside Rich's control. DF-001 applied to the presentation tier. |
| D8 | **PM-tool integration deferred.** A clean delivery dashboard is enough for FinProxy (confirmed). | Only wire a thin adapter to whatever FinProxy *already* uses if they culturally expect a named board. Do not adopt Linear / Jira for Rich's own sake. |
| D9 | **FinProxy productization fork: default to *managed*, design toward *hosted self-serve*, park *ship-the-factory*.** | Managed (they send work in, get PRs back; models / memories / guardkit stay on Rich's hardware) is lowest-risk, highest-moat, matches the near-zero-marginal-cost thesis. Hosted self-serve is the natural step up. Shipping the factory to their infra leaks the moat (the fine-tuned models + guardkit + memories *are* the asset) and breaks the cost thesis. Local-first must work first. |
| D10 | **LiteLLM Agent Platform (LAP): take the inspiration, not the dependency.** | Real (BerriAI, MIT) but pre-v0 / alpha. Borrow the shape; do not put an alpha external platform on the critical path. |
| D11 *(reconstructed)* | **Automate the repeatable deploy.** The mechanical, idempotent middle of the output side (stand-up, migrate, smoke) is scripted and run by an executor, not hand-cranked. | fleet-memory's NAS deploy already exists as idempotent scripts (`deploy.sh`, `smoke.sh`); the output-side loop is a *harvest* of these, not new authoring. |
| D12 *(reconstructed)* | **Supervised verify-and-debug loop.** The non-mechanical part (does the live system actually behave?) is a supervised loop that reads real failures and remediates. | This is the output-side analogue of Mode C. The oracle is the live system, not co-generated tests (see D14). |
| D13 *(reconstructed)* | **Single async approval gate at the irreversible edges.** Everything reversible runs unattended; a mistake at a real blast-radius edge (AWS, credentials, publish) requires one async human approval. | "Autonomy is bought per step at the output side, not per pipeline." Notify-and-approve at irreversible edges; the same mechanism serves James at the front and Rich at the back. |
| D14 *(reconstructed)* | **The output-side Coach is the *environment*, not a model.** Smoke tests against a live system are the oracle, exactly as unit tests were the oracle for AutoBuild. | The verifier for deploy/verify is behavioural evidence from the running system — the same principle that later drives the QA Verifier's behavioural-evidence gates on the build side. |
| D15 *(reconstructed)* | **Reprioritise the output-side loop ahead of the QA-verifier / coach fine-tunes.** The bottleneck moved off development, so those fine-tunes now polish a stage that is no longer the constraint. | Deprioritised, not dropped. (Superseded in emphasis by the 2 July session, which surfaces that the build-side *stub/false-approval* class is itself a dependability gap the QA Verifier must close before unattended build is safe.) |

---

## The load-bearing open question (deliberately left unresolved in June)

> Does the verify-and-debug loop invoking Claude Code on frontier sit on the DF-001 critical path, or does the approval gate make it attended-by-exception?

Left open in June rather than asserted, because it is a per-stage substrate call best made with cost / speed / risk in front of the operator.

**Update (2 July 2026):** now addressed. For the *build* side, the answer is settled — the Unattended Build Service is local by design (DF-001; UBS §3.5), and `DECISION-DF-006` generalises this to availability. For the *output* side, DF-006 §6 resolves it in principle: the fix-agent must be local if it runs unattended; frontier is permitted only if the loop is attended-by-exception with a human approving each irreversible step. Carry into the output-side loop's `/system-arch`.

---

## Decisions to graduate

- **D4** (NATS as orchestration source of truth) — already covered by ADR-SP-002; cross-reference, no new record needed.
- **D5 + D7** (presentation surfaces are swappable adapters; nothing external on the critical path) — a `DECISION-DF-xxx`. This is DF-001 extended to the *presentation* tier; distinct from `DECISION-DF-006`, which extends DF-001 to *frontier availability* across all stages. Both are children of DF-001, applied to different surfaces.
- **D9** (FinProxy fork: managed → hosted self-serve, ship-the-factory parked) — a `DECISION-DF-xxx`. Commercial-strategy decision with a revisit condition (a real licensing + model-protection story).
- **D11 + D12 + D13** (output-side decomposition: automate the repeatable middle, gate the irreversible edges, hours-present → seconds-async) — belong as ADRs in `forge` once the output-side `/system-arch` runs.

Work-as-debt commercial structure is out of scope for these records: a financial / legal matter for James and a solicitor / accountant to paper.

---

## Workstream backlog & sequencing

| # | Workstream | Shape | Status |
|---|---|---|---|
| 1 | **fleet-memory stood up via the Forge loop** | The output-loop exemplar: a minimal executor plus two subprocess step types wrapping fleet-memory's existing idempotent scripts. | Scoped — `forge/docs/research/ideas/output-loop-exemplar-scope.md` + build plan. |
| 2 | **Delivery dashboard** | NATS-consumer web app on owned hardware. Client artifact + work-as-debt ledger. PO-agent door folds in later. | Scoped here (D6). Needs its own conversation-starter — confirm new repo + web stack. |
| 3 | **Slack adapter + PO-agent door** | Thin publish-out / listen-in Slack bot so James drives idea→spec→approve without the terminal. Swappable. | Scoped here (D7). Depends on the PO-agent's current OpenAI-compatible / MCP exposure. |
| 4 — RESEQUENCED | **QA-verifier / Coach fine-tunes** | Behind the output-side loop per D15. | Deprioritised, not dropped. (2 July: Phase 0 *deterministic* gates re-elevated — cheap, no fine-tune, close the false-approval class.) |

**The smallest first exemplar across the whole backlog:** James drives one feature from idea to "sent to build" through Slack, no terminal (workstream 2/3), *and* fleet-memory stood up via the Forge loop (workstream 1). The first de-risks "James contributes"; the second de-risks "shipping anything."

---

## Related documents

- `forge-output-loop-conversation-starter.md` · `output-loop-exemplar-scope.md` · `output-loop-exemplar-build-plan.md` — workstream 1.
- `dev-pipeline-architecture.md` / `dev-pipeline-system-spec.md` — the existing architecture providing multi-tenancy (FINPROXY account), the PM-tool adapter pattern, the Build-Agent-invokes-GuardKit subprocess pattern (ADR-SP-003), and the cost-tracking future extension.
- `conversation-capture-2026-06-14-forge-meta-harness.md` — the *improve* loop; complementary to this doc's *output-side* loop (see its 20 June status update, which reconciles the two).
- `DECISION-DF-006` (`guardkit/docs/decisions/`) — resolves this doc's load-bearing open question on the frontier-substrate axis.

---

*Reconstructed 2 July 2026 from the 19–20 June transcript. Original never persisted (MCP write failure).*
*Strategic anchor for the presentation-layer and output-side conversation-starters.*
