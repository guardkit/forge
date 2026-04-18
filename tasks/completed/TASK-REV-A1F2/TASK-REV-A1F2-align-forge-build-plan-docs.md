---
id: TASK-REV-A1F2
title: Review and align Forge build plan docs with NATS, specialist-agent, and Jarvis repos
status: completed
task_type: review
review_mode: architectural
review_depth: comprehensive
decision_required: true
created: 2026-04-15T00:00:00Z
updated: 2026-04-15T00:00:00Z
completed: 2026-04-15T00:00:00Z
priority: high
tags: [architecture-review, alignment, forge, nats, specialist-agent, jarvis, pre-build]
complexity: 7
test_results:
  status: pending
  coverage: null
  last_run: null
review_results:
  mode: architectural
  depth: comprehensive
  verdict: ready-after-corrections
  decision: implement
  findings_count: 40
  recommendations_count: 13
  report_path: docs/research/forge-build-plan-alignment-review.md
  completed_at: 2026-04-15T00:00:00Z
  followup_tasks_created:
    forge:
      - tasks/backlog/forge-v2-doc-alignment/TASK-FVD1-apply-v2.2-anchor-additions.md
      - tasks/backlog/forge-v2-doc-alignment/TASK-FVD2-correct-forge-build-plan.md
      - tasks/backlog/forge-v2-doc-alignment/TASK-FVD3-correct-orchestrator-refresh.md
      - tasks/backlog/forge-v2-doc-alignment/TASK-FVD4-correct-fleet-master-index.md
    nats-core:
      - tasks/backlog/forge-v2-alignment/TASK-NCFA-001-add-pipeline-payloads.md
      - tasks/backlog/forge-v2-alignment/TASK-NCFA-002-integration-tests-new-payloads.md
    specialist-agent:
      - tasks/backlog/dual-role-deployment/TASK-DRD-001-role-registry-and-manifest-factory.md
      - tasks/backlog/dual-role-deployment/TASK-DRD-002-role-aware-command-router-and-cli.md
      - tasks/backlog/dual-role-deployment/TASK-DRD-003-forge-shaped-result-wrapper.md
      - tasks/backlog/dual-role-deployment/TASK-DRD-004-dual-role-compose-and-e2e.md
    nats-infrastructure:
      - tasks/backlog/TASK-PSKV-001-decide-pipeline-state-kv-fate.md
    jarvis:
      - tasks/backlog/TASK-JFT-001-bootstrap-forge-build-trigger.md
  adrs_added:
    - ADR-SP-014 Jarvis as Upstream Build Trigger (Pattern A) — Proposed in forge-pipeline-architecture.md §9
    - ADR-SP-015 Specialist-Agent Dual-Role Deployment Model — Proposed
    - ADR-SP-016 Singular agents.command/agents.result topic convention — Proposed
    - ADR-SP-017 PIPELINE/AGENTS/SYSTEM stream retention reconciliation — Proposed
  graphiti_episodes_seeded: 5
  graphiti_group_id: architecture_decisions
---

# Task: Review and align Forge build plan docs with NATS, specialist-agent, and Jarvis repos

## Why this is a review task

Rich is about to start building the Forge. Ideas have evolved significantly — multiple docs have been updated at different times. Before any code is written, we need to course-correct so the build plan reflects the *current* thinking captured in [forge-pipeline-architecture.md](docs/research/forge-pipeline-architecture.md) (v2.1, 15 April 2026), and so it is actually aligned with what already exists in the sibling repos.

Use `/task-review` to execute. **Do not implement.**

## Source of truth (read first)

1. [docs/research/forge-pipeline-architecture.md](docs/research/forge-pipeline-architecture.md) — v2.1, most recently updated. This is the anchor. Everything else is measured against this.
2. **Graphiti knowledge graph** — query before and during the review. Graphiti holds the accumulated project memory across conversations and should set the scene for *why* decisions were taken, not just *what* they are. Use the `mcp__graphiti__*` tools:
   - `search_nodes` — entities (Forge, Jarvis, specialist-agent, NATS, Product Owner role, Architect role, Software Factory, PM Adapter, RequireKit, etc.)
   - `search_memory_facts` — relationships and historical decisions (why PM Adapter was removed, when NATS-native was adopted, Jarvis's intended role, dual-role agent deployment rationale)
   - `get_episodes` — recent conversation episodes to catch anything that hasn't yet landed in docs
   - Filter by relevant `group_id`s if the graph is scoped
   - **Use Graphiti findings as evidence** in the alignment report. If Graphiti contains a decision or rationale that contradicts current docs, that is a drift finding. If Graphiti is silent on something the docs assert strongly, note the gap — it may need to be added to the graph via `add_memory` after the review.

Key positions asserted by v2.1 that must be preserved across all docs:
- NATS-native from day one — no subprocess/v0 phasing, no `PipelineTransport` ABC
- PM Adapter, Linear, Kanban boards, tickets, `ready-for-dev`, `feature-planned` are all **removed**
- Triggers: CLI `forge queue` → JetStream `pipeline.build-queued`
- JetStream owns the queue; SQLite owns history (`~/.forge/forge.db`)
- Sequential builds (ADR-SP-005 / SP-012)
- Specialist agents invoked via NATS commands (`agents.commands.{agent_id}`)
- Confidence-gated checkpoints with 🟢 / 🟡 / 🔴 modes
- RequireKit deprecated; `/feature-spec` + `/feature-plan` in GuardKit replace it

## What Rich has also specified (not yet fully reflected in older docs)

- **Jarvis is the human interface.** Rich interacts with Jarvis. Jarvis discovers the Forge's capabilities on the fleet and sends build requests to the Forge. This is upstream of `forge queue` being typed by a human at a terminal — or replaces it as the primary trigger path.
- **Specialist-agent dual-role deployment.** The first two specialist agents to run with `nats-infrastructure` / `nats-core` are two deployments of the same specialist-agent binary: one in the **Product Owner** role and one in the **Architect** role. The Forge calls both via NATS commands during the pipeline's early stages.
- No Kanban, no Linear, no traditional PM tool integration — confirmed and should be scrubbed anywhere it lingers.

## In-scope documents to audit (within forge repo)

- [docs/research/forge-pipeline-architecture.md](docs/research/forge-pipeline-architecture.md) — anchor; check internal consistency only
- [docs/research/ideas/forge-build-plan.md](docs/research/ideas/forge-build-plan.md)
- [docs/research/ideas/forge-pipeline-orchestrator-refresh.md](docs/research/ideas/forge-pipeline-orchestrator-refresh.md)
- [docs/research/ideas/fleet-master-index.md](docs/research/ideas/fleet-master-index.md)
- Anything else under [docs/research/ideas/](docs/research/ideas/) or [docs/product/](docs/product/) that touches the pipeline, trigger model, agent wiring, or PM integration

## External repos to cross-reference (read-only)

All under `/Users/richardwoollcott/Projects/appmilla_github/`:

- **nats-core** — confirm message schemas, topic registry, Python client match what the architecture v2.1 describes (`BuildQueuedPayload`, `BuildPausedPayload`, `BuildCompletePayload`, `StageCompletePayload`; topics `pipeline.build-queued`, `pipeline.build-paused`, `pipeline.build-resumed`, `pipeline.stage-complete`, `pipeline.stage-gated`; `agents.commands.*`, `agents.results.*`). Flag any lingering `PMAdapter`, `ReadyForDevPayload`, `feature-planned`, `ticket-updated`.
- **nats-infrastructure** — confirm Docker Compose / JetStream streams (`PIPELINE` 30d, `AGENTS` 7d, `SYSTEM` 24h), accounts, and that it is actually runnable on GB10. Note what Phase 1 of the roadmap still needs.
- **specialist-agent** — confirm the unified harness exists and that Product Owner + Architect roles are configured (role YAML, RAG, fine-tuned model hooks). Check NATS subscription to `agents.commands.{agent_id}` and result publishing to `agents.results.{agent_id}`. Verify agent-manifest registration on `fleet.register`.
- **jarvis** — understand how Jarvis discovers fleet capabilities and dispatches build requests. Does Jarvis already publish to `pipeline.build-queued`, or does it call a `forge` CLI/API? The build plan must describe this integration explicitly; today it reads as CLI-only.

For each external repo: read the README, any `docs/` directory, feature specs / feature plans, and enough of the implementation to verify that the built reality matches what the Forge architecture assumes about it.

## Output — required deliverables from `/task-review`

1. **Alignment report** — a single document, saved to `docs/research/forge-build-plan-alignment-review.md`, with:
   - Per-document findings (one section per in-scope forge doc): matches v2.1 / drift / contradictions / stale references to removed concepts (Kanban, Linear, PM Adapter, `ready-for-dev`, RequireKit, v0 subprocess, `PipelineTransport`).
   - Per-repo findings (nats-core, nats-infrastructure, specialist-agent, jarvis): what exists today, what the Forge architecture assumes, and any gap between the two.
   - **Jarvis → Forge integration gap**: propose how Jarvis-initiated builds are expressed in the NATS topic hierarchy and build plan. Recommend whether Jarvis publishes `pipeline.build-queued` directly, goes through a thin Forge API, or some other pattern.
   - **Specialist-agent dual-role wiring**: confirm (or flag missing) configuration for PO + Architect deployments, role YAML, and which pipeline stages invoke each.

2. **Correction list** — an ordered, file-scoped punch list of edits needed in forge repo docs to eliminate drift. Format: `<file>:<section>` → `<what to change>` → `<why>`. Do **not** make the edits in this task; a follow-up `/task-work` task will apply them.

3. **Build-readiness verdict** — one of:
   - ✅ Ready to start Phase 1 (NATS infra) — proceed as written
   - ⚠️ Ready to start after applying correction list — list blocking items
   - 🔴 Not ready — missing prerequisites (list them)

4. **Recommended follow-up tasks** — proposed `/task-create` commands for the corrections and any newly discovered work (e.g., "document Jarvis → Forge handoff in architecture §5", "update fleet-master-index to remove Kanban references").

## Acceptance criteria

- [ ] `forge-pipeline-architecture.md` v2.1 has been read end-to-end and treated as the anchor
- [ ] Graphiti has been queried (`search_nodes`, `search_memory_facts`, `get_episodes`) for Forge, Jarvis, specialist-agent, NATS decisions, PM Adapter removal, and dual-role agent deployment — findings incorporated into the alignment report
- [ ] Every doc under `docs/research/ideas/` and `docs/product/` that references the pipeline has been checked against v2.1
- [ ] nats-core, nats-infrastructure, specialist-agent, and jarvis repos have each been inspected (READMEs, docs/, feature specs, key implementation files)
- [ ] Every removed concept (Kanban, Linear, PM Adapter, `ready-for-dev`, RequireKit NATS, `feature-planned`, `ticket-updated`, v0 subprocess, `PipelineTransport`) is either gone from forge docs or listed in the correction list
- [ ] Jarvis's role as the human-facing entry point is explicitly addressed in the alignment report and the build plan
- [ ] The Product Owner + Architect dual-role deployment of specialist-agent is explicitly addressed
- [ ] A build-readiness verdict is recorded with justification
- [ ] No code is written and no doc edits are made under this task — only the alignment report and correction list

## Out of scope

- Actually applying doc corrections (follow-up task)
- Writing any Forge code
- Editing nats-core, nats-infrastructure, specialist-agent, or jarvis
- Building any NATS infrastructure or running any services

## Next step

Run `/task-review TASK-REV-A1F2 --mode=architectural`.
