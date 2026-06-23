# Factory Scaling, Presentation Layer & Output-Side Bottleneck — Findings & Decisions

## Ideation session capture · 19 June 2026 · Claude Desktop

---

## Purpose of this document

Captures the decisions from the ideation session triggered by James's scaling
questions and Rich's output-side bottleneck. It is the strategic anchor that
the per-workstream conversation-starters reference. Durable decisions flagged in
Section 9 should graduate to `DECISION-DF-xxx` records or repo ADRs.

---

## Context

James raised three questions; Rich added a fourth problem that turned out to be
the sharpest:

1. **How do we scale beyond Rich + Claude + the factory?**
2. **How can James contribute?** (non-technical; overwhelmed by the terminal)
3. **FinProxy is nearly out of money** — do work as debt, possibly deploy them
   something that lets them use the factory themselves.
4. **(Rich) The output side is the bottleneck.** Development is now fast
   (AutoBuild done, both local and Claude SDK). Integrate/test/deploy/debug —
   everything *either side* of development — is what eats focused blocks of
   Rich's time and stalls delivery.

---

## What this session resolved

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | The scaling ceiling is **curation bandwidth at the attended front stages**, not compute. | DF-003: planning stages are the irreducibly human part. "Scale beyond Rich" = more product-owner/curator seats at the front feeding a parallelised build back-end, not cloning Rich. Much smaller ask. |
| D2 | Two of James's three questions are **already answered by the existing dev-pipeline architecture**. | Multi-tenancy is by-design (FINPROXY NATS account already provisions james, rich_finproxy, mark scoped to `finproxy.>`; "Rich sees everything, James sees only FinProxy"). Per-project cost tracking already sits in the future-extensions list. The genuinely new work is the presentation layer + the FinProxy productization fork. |
| D3 | The real missing piece is a **presentation tier**. | Everything built so far is developer-facing (agents wired as MCP in Claude Desktop for Rich's own use; Claude Code in the terminal). James can use neither. NATS permissions are plumbing, not a door. |
| D4 | **NATS stays the orchestration source of truth.** The prior steer against "orchestration" was against *framework-as-brain*, not against the event bus. | ADR-SP-002 already makes the event bus authoritative, deliberately, because a typed transport + JetStream does not churn. "We need orchestration to tie this into a system" is true and *already satisfied*. Tying it together means more producers/consumers on the bus you have — not a new brain (LangGraph / NeMo / LAP) on top of it. |
| D5 | **Own the spine; treat channels as adapters.** | Same pattern the architecture already uses for PM tools: interchangeable adapters behind NATS, inputs-and-outputs, never source of truth. Applies identically to Slack, Cowork, Codex, web UI. |
| D6 | The **delivery dashboard is the presentation spine and build #1** of the presentation layer. | It is the client artifact, and for the work-as-debt deal it is the commercial instrument — the ledger of what FinProxy's deferred fee bought. Pure NATS-consumer web app on owned hardware: cannot be switched off or held to ransom. The PO-agent door later folds into this same app, so the core idea→spec→approve loop never touches anyone else's platform. |
| D7 | **Slack = thin adapter (low-regret); Cowork/Codex = optional, off the critical path.** | James lives in Slack today, so a thin publish-out/listen-in bot is the fastest door — and because the canonical record lives in NATS/fleet-memory, it is swappable for Mattermost/Zulip in an afternoon. Cowork/Codex are frontier-model agent *clients* over MCP (the wiring that already exists); kept strictly optional because their cost/availability is outside Rich's control. DF-001 applied to the presentation tier. |
| D8 | **PM-tool integration deferred.** A clean delivery dashboard is enough for FinProxy (confirmed). | Only wire a thin adapter to whatever FinProxy *already* uses if they culturally expect a named board. Do not adopt Linear/Jira for Rich's own sake. |
| D9 | **FinProxy productization fork: default to *managed*, design toward *hosted self-serve*, park *ship-the-factory*.** | Managed (they send work in, get PRs back; models/memories/guardkit stay on Rich's hardware) is lowest-risk, highest-moat, and matches the near-zero-marginal-cost thesis. Hosted self-serve (FinProxy gets a scoped seat on infra Rich operates) is the natural step up. Shipping the factory to their infra leaks the moat (the fine-tuned models + guardkit + memories *are* the asset) and breaks the cost thesis (no GPU budget). A managed-infra FinProxy deployment is a worthwhile future capability-demo — but local-first must work first. |
| D10 | **LiteLLM Agent Platform (LAP): take the inspiration, not the dependency.** | Real (BerriAI, MIT) but pre-v0/alpha. It is a control-plane-on-top-of-runtimes (lists Deep Agents) that bundles a strong *surface* (team UI, Slack/Teams, credential vault) with its own *brain* (runtime, sessions, memory). The surface ideas are exactly right for James and FinProxy self-serve; the brain overlaps validated NATS + fleet-memory. Revisit only as a spike if its surface proves worth it — never let it become the brain while alpha. |
| D11 | **The output side is higher blast radius with a different feedback loop, not "harder".** | AutoBuild automated cleanly because it is sandboxed and reversible (branch/PR, tests as oracle, discard-and-rerun is free). Deploy/integrate/debug has real side effects that do not cleanly undo, and the verdict is a live system behaving correctly in its real environment. That is why it resisted the same automation — and the key to doing it right: automate the repeatable middle, gate the irreversible edges. |
| D12 | **Decompose the output side into three.** | (a) **Repeatable deploy** (artifact → running system): fully automatable, Forge's job — build the deploy *exemplar* once as IaC + smoke tests. (b) **Verify-and-debug**: supervised loop, AutoBuild's shape pointed at a running system — Forge invokes Claude Code as a subprocess against the live deploy (the same subprocess-not-subscriber pattern as GuardKit, ADR-SP-003); smoke tests are the verdict; the *environment* is the Coach. (c) **Irreversible / external edges** (prod credential injection, first IAM setup, waiting on Moneyhub): not automated — turned into a single async approval. Moneyhub is a waiting-on-someone problem, not an automation problem. |
| D13 | **The unlock is hours-present → seconds-async.** | A deploy currently costs a focused block of Rich's day. Target: Forge runs the deploy, hits the one irreversible gate, pings Rich ("LPA ready for AWS staging, approve?"), proceeds on a tap, smoke-tests, reports green or kicks Claude Code at the failures, and only pings again at the next gate or if genuinely stuck. Same notification-and-approve ergonomics as James in Slack, pointed at Rich. |
| D14 | **fleet-memory is the first exemplar for the output-side loop.** | Local, owned, reversible, zero external dependencies, just built, and needs standing up anyway. Build the Forge deploy/verify loop *with fleet-memory as the test subject* — walk away with fleet-memory deployed (the need) and the reusable loop (the capability that compounds). LPA is then the same loop plus approval gates for the AWS/credential steps. Prove the shape where a mistake costs nothing, then apply it where blast radius is real. |
| D15 | **Reprioritise: the output-side loop jumps ahead of the QA-verifier and coach fine-tunes.** | The bottleneck has moved off development. By Rich's own ROI logic, those fine-tunes now polish a stage that is no longer the constraint. Do not drop them — resequence them behind the output-side loop. |

---

## Orchestration: clarifying the steer

Worth recording explicitly because it caused confusion mid-session. There are
two different things the word "orchestration" was doing:

- **Durable coordination** — NATS JetStream as the event bus and source of
  truth (ADR-SP-002). This is *chosen and good*. A typed transport with
  persistence does not break on v2.
- **Framework-as-brain** — making LangGraph / NeMo / LAP the thing that *owns*
  workflow state and agent lifecycle. This is what the steer warned off, because
  it is the lock-in that breaks on upgrade.

Conclusion: Rich is not missing orchestration. He already picked the durable
kind. Everything in this session — presentation surfaces, the output-side loop —
is **producers and consumers on the bus that already exists**, not a new brain
on top of it.

---

## Workstream backlog & sequencing

Ordered by leverage, with the reprioritisation (D15) applied. One exemplar at a
time — pace is strategy.

| Order | Workstream | What it is | Status |
|-------|-----------|-----------|--------|
| **1 — IMMEDIATE** | **Output-side deploy/verify loop** | Forge gains a deploy/verify capability; Claude Code invoked as subprocess against the live deploy; approval gates at irreversible edges. **fleet-memory is the first exemplar** (this weekend); LPA follows with AWS/credential gates. | Conversation-starter produced (`forge-output-loop-conversation-starter.md`). Ready for `/system-arch` + `/system-design`. |
| 2 | **Delivery dashboard** | NATS-consumer web app on owned hardware. Client artifact + commercial instrument (work-as-debt ledger). No external dependency. PO-agent door folds in later. | Scoped here (D6). Needs its own conversation-starter — confirm whether it is a new repo and the web stack before drafting. |
| 3 | **Slack adapter + PO-agent door** | Thin publish-out/listen-in Slack bot so James drives idea→spec→approve without the terminal. Swappable. | Scoped here (D7). Needs its own conversation-starter — depends on the PO-agent's current OpenAI-compatible / MCP exposure. |
| 4 — RESEQUENCED | **QA-verifier / coach fine-tunes** | Already in flight; now behind the output-side loop per D15. COACHGATHER01 Option A/B decision still gates the QA-verifier fine-tune. | Unchanged; deprioritised, not dropped. |

The smallest first exemplar across the whole backlog: **James drives one feature
from idea to "sent to build" through Slack, no terminal** (workstream 2/3) and
**fleet-memory stood up via the Forge loop** (workstream 1). The first de-risks
"James contributes"; the second de-risks "shipping anything."

---

## Decisions to graduate

These are durable enough to promote out of this session doc:

- **D4** (NATS as orchestration source of truth) — already covered by ADR-SP-002;
  cross-reference, no new record needed.
- **D5 + D7** (presentation surfaces are swappable adapters; nothing external on
  the critical path) — new `DECISION-DF-xxx`. This is DF-001 extended to the
  presentation tier and is load-bearing for every channel decision that follows.
- **D9** (FinProxy fork: managed → hosted self-serve, ship-the-factory parked) —
  new `DECISION-DF-xxx`. Commercial-strategy decision with a revisit condition
  (a real licensing + model-protection story).
- **D11 + D12 + D13** (output-side decomposition: automate the repeatable middle,
  gate the irreversible edges, hours-present → seconds-async) — belongs as ADRs
  in `forge` once `/system-arch` runs.

Work-as-debt commercial structure is out of scope for these records: it is a
financial/legal matter for James and a solicitor/accountant to paper.

---

## Related documents

- `forge-output-loop-conversation-starter.md` — companion to this doc; the
  conversation-starter for workstream 1.
- `dev-pipeline-architecture.md` / `dev-pipeline-system-spec.md` — the existing
  architecture that already provides multi-tenancy (FINPROXY account), the
  PM-tool adapter pattern, the Build-Agent-invokes-GuardKit subprocess pattern
  (ADR-SP-003), and the cost-tracking future extension.
- The AI-augmented-development blog post — supports the managed/hosted framing
  ("the memories are the deliverable" → you operate them, you don't hand them
  over).

---

*Prepared: 19 June 2026 | factory scaling + output bottleneck ideation session*
*Strategic anchor for the presentation-layer and output-side workstreams*
