# Ship's Computer Fleet — Master Index

## All Repos, All Docs, All Agents · April 2026 (Rewrite v2)

---

## Overview

The Ship's Computer is a distributed multi-agent system orchestrated through NATS
JetStream, with an intent router (Jarvis) dispatching requests to specialist agents.
The system is accessible through multiple adapters: Reachy Mini (voice), Telegram,
Slack, Dashboard, CLI.

The fleet forms a **Software Factory** — a complete pipeline from ideation through to
deployed code. This is not a faster way to do what teams already do. It is a different
way to think about what software delivery is: the collapse of planning, knowledge, and
build into one outcome-driven pipeline where coordination is emergent, not explicit.

This document is the master index across all repos in the `guardkit/` organisation.

---

## The Software Factory Pipeline

```
Product idea / raw information
        ↓
Ideation Agent (score and rank ideas)
        ↓
Product Owner Agent (structured product docs)
        ↓  ← calls Architect Agent via NATS: "Is this feasible?"
Architect Agent (conversation starter with C4, ADRs)
        ↓
Forge (checkpoint manager — confidence-gated quality gates)
        ↓
GuardKit Commands (/system-arch → /system-design → /feature-spec → build)
        ↓
Outcome Gate (did the thing work as intended?)
        ↓
Deployed software
```

All three specialist agents (Ideation, Product Owner, Architect) are deployments of the
same binary — `specialist-agent serve --role X` — differentiated by role configuration,
fine-tuned model weights, and per-role knowledge graph. The Forge coordinates them,
evaluates Coach scores against per-stage thresholds, and only interrupts a human when
the Coach has specific concerns.

```
                    ┌─────────────────┐
All agents ←───────→│  NATS JetStream  │←── nats-infrastructure (deployment)
All adapters ←─────→│  (nats-core)     │
                    └─────────────────┘
```

YouTube Planner is a separate pipeline for content creation, drawing from the same
fleet infrastructure.

---

## Context-First Delivery — Why There Are No Tickets

Traditional software delivery maintains three separate systems that must be kept in
synchronisation at all times:

1. A **planning system** — what we are building and why (Jira, Linear, Notion)
2. A **knowledge system** — what we have decided and learned (Confluence, Google Docs)
3. A **build system** — what is actually being made (GitHub, CI/CD, deployment)

Every handoff between these systems is a translation step. Every translation step is an
opportunity for drift, loss of context, and wasted hours. The coordination tax is
structural, baked into the architecture of how software delivery has been organised for
thirty years.

### The Steam Engine Trap

When electric motors replaced steam engines in factories, the first generation of
adopters simply swapped one power source for another — same layout, same belts, same
pulleys. The productivity gains only arrived when engineers asked: *if we are not
constrained by a central power source, how should a factory actually be organised?*

Bolting AI onto existing PM tools — even Linear's agent approach — is electrifying the
steam engine. The Software Factory skips the intermediate step entirely.

### How the Pipeline Replaces Coordination

In this system:

- The **structured documents** produced by the Product Owner Agent *are* the project
  management. They contain decisions, assumptions, requirements, and scope — everything
  a ticket would contain, but as living reasoning rather than atomised tasks.
- The **git history and agent logs** *are* the audit trail.
- The **Coach scores at each pipeline stage** *are* the quality reporting.
- A **query to the system** answers "where are we?" — there is no separate reporting
  layer to maintain.

The coordination overhead does not get automated. It ceases to exist as a distinct
concern.

### Outcome Gates Replace Progress Tracking

Traditional delivery measures activity: epics, stories, tasks, sprints, velocity,
burn-down. Outcome-based delivery asks different questions:

- Does the GCSE tutor correctly answer a real past paper question?
- Does the authentication flow handle the edge cases we identified?
- Can a non-technical stakeholder get a meaningful answer when they ask "where are we?"

These are gates, not stages. A gate is either passed or it isn't. An agent can monitor
for it, signal when it is met or at risk, and surface the relevant context when human
judgment is needed. **Outcome gates replace progress tracking. Decisions replace status
updates. The pipeline signals rather than reports.**

### What Stakeholders Actually Need

Stripping away the process and asking what outcomes stakeholders genuinely need:

- **Rich (builder):** know what to work on next, stay aligned on direction without
  coordination drag, receive fast signals when something is going wrong
- **James (product owner):** confidence the right things are being built, ability to
  make decisions when needed, visibility for external stakeholders without clerical
  overhead
- **Both:** know when to course-correct, and do so before it becomes expensive

None of these jobs require a Kanban board. None require a weekly status report. None
require a timesheet. They require **queryable context** and **proactive signals** —
available on demand, not produced on a schedule.

### The FinProxy Evidence

The FinProxy collaboration made this concrete: Rich produced fourteen structured
documents over a weekend — documents that decomposed the product into its constituent
decisions, assumptions, risks, and requirements. These provoked genuine thinking. They
*were* the work. The comparable time spent on weekly reporting spreadsheets, timesheets,
and status updates was pure friction layered on top of something that was already
working.

### PM Tools as Optional Visibility Adapters

The PM Adapter layer (Linear, GitHub Projects) remains in the architecture but is
repositioned as an **optional visibility adapter** for stakeholders who want a familiar
interface. It subscribes to pipeline events and projects them into a PM tool view. But
it is not in the critical path. The pipeline runs with or without it. The adapter is a
viewport, not a control surface.

The `feature_ready_for_build` event is emitted by the Forge (Pipeline Orchestrator)
when the GuardKit command sequence completes, not by a webhook from a kanban card state
change. The `ticket_updated` event has been dropped entirely — there are no tickets.

---

## Repository Map

```
guardkit/
│
│── INFRASTRUCTURE ──────────────────────────────────────────────────
│
├── nats-core                 ← Shared contract layer (pip-installable library)
│   │                            STATUS: ✅ Implemented, 97% test coverage, 17 test files
│   ├── src/nats_core/
│   │   ├── client.py             ← NATSClient (async, typed, fleet convenience methods)
│   │   ├── envelope.py           ← MessageEnvelope + EventType enum (19 event types)
│   │   ├── topics.py             ← Topics namespace registry with resolve() + for_project()
│   │   ├── config.py             ← AgentConfig (pydantic-settings, AGENT_ prefix)
│   │   ├── manifest.py           ← AgentManifest + ToolCapability + IntentCapability
│   │   ├── registry.py           ← ManifestRegistry ABC + NATSKVManifestRegistry + InMemory
│   │   └── payloads/             ← 19 typed payload classes (Fleet, Agent, Pipeline, Jarvis)
│   ├── docs/design/
│   │   ├── contracts/
│   │   │   └── agent-manifest-contract.md
│   │   ├── specs/
│   │   │   └── nats-core-system-spec.md          ← 6 features, BDD acceptance criteria
│   │   └── decisions/
│   │       ├── ADR-001-nats-as-event-bus.md
│   │       ├── ADR-002-schema-versioning.md
│   │       ├── ADR-003-nats-py-vs-faststream.md
│   │       └── ADR-004-dynamic-fleet-registration.md
│   ├── command-history.md        ← Full /system-arch → /system-design → /feature-spec × 6
│   └── system-arch-history.md    ← Rich's responses during /system-arch session
│
├── nats-infrastructure       ← NATS server deployment, accounts, streams (config/ops)
│   │                            STATUS: ✅ Configured, ready to run (docker compose up)
│   ├── config/
│   │   ├── nats-server.conf      ← JetStream enabled, 1GB mem / 10GB file, :8222 monitoring
│   │   └── accounts/
│   │       └── accounts.conf.template  ← APPMILLA (Rich+James), FINPROXY (Mark), SYS
│   ├── scripts/
│   │   ├── docker-entrypoint.sh  ← envsubst password injection
│   │   └── verify-nats.sh        ← Startup verification
│   └── docs/design/
│       ├── specs/
│       │   └── nats-infrastructure-system-spec.md  ← 6 features, 26 tasks
│       └── decisions/
│           ├── ADR-001-standalone-infra-repo.md
│           └── ADR-002-account-multi-tenancy.md
│
│── SPECIALIST AGENTS ───────────────────────────────────────────────
│
├── specialist-agent          ← Unified agent harness — one codebase, many roles
│   │                            STATUS: Phase 0 ✅ (0.75), Phase 1 ✅ (0.93)
│   │                            Renamed from architect-agent (ADR-ARCH-010)
│   │                            Absorbs: architect-agent, product-owner-agent, ideation-agent
│   ├── src/specialist_agent/
│   │   ├── cli/                  ← CLI interface (greenfield, serve, metrics)
│   │   ├── core/                 ← Generation loop, scoring engine, session lifecycle
│   │   ├── providers/            ← LLM client abstraction (Claude, GPT, Gemini, vLLM)
│   │   └── roles/                ← Role-specific configs (Phase 1B)
│   ├── docs/
│   │   ├── architecture/
│   │   │   └── ARCHITECTURE.md
│   │   ├── decisions/
│   │   │   ├── ADR-ARCH-006-three-interface-layers.md
│   │   │   ├── ADR-ARCH-008-unified-harness.md
│   │   │   ├── ADR-ARCH-009-role-config-pattern.md
│   │   │   └── ADR-ARCH-010-repo-rename.md
│   │   └── research/ideas/
│   │       ├── architect-agent-vision.md              ← Updated project vision (12 April 2026)
│   │       ├── unified-agent-harness-conversation-starter.md
│   │       ├── mcp-deployment-architecture.md
│   │       ├── fine-tuned-architect-agent-strategy.md  ← Three-layer architecture
│   │       ├── landscape-conversation-starter.md       ← Devin, MetaGPT, ChatDev positioning
│   │       ├── ideation-role-extracted-ideas.md         ← From archived ideation-agent
│   │       ├── phase1c-domain-fidelity-scope.md + build-plan.md
│   │       ├── phase1b-unified-harness-build-plan.md
│   │       ├── phaseG-scope.md + build-plan.md
│   │       ├── phase2-scope.md + build-plan.md
│   │       ├── phase3-scope.md + build-plan.md
│   │       └── phaseF-scope.md + build-plan.md
│   ├── command_history.md        ← Phase 0 through Phase 1 commands
│   ├── feature-spec-FEAT-001-history.md through FEAT-007-history.md
│   ├── feature-spec-domain-fidelity-history.md
│   ├── feature-plan-fidelity-history.md
│   ├── system-arch-history.md
│   ├── system-design-history.md
│   └── system-plan-history.md
│
│── PIPELINE ORCHESTRATION ──────────────────────────────────────────
│
├── forge                     ← Pipeline orchestrator and checkpoint manager
│   │                            STATUS: Blocked on specialist-agent Phase 3 + nats infra
│   │                            Renamed from guardkitfactory
│   │                            agent_id: "forge", topic: agents.command.forge
│   └── docs/research/
│       ├── ideas/
│       │   ├── fleet-master-index.md                    ← THIS DOCUMENT
│       │   ├── forge-pipeline-orchestrator-refresh.md    ← v3, 11 April 2026
│       │   ├── forge-ideas-overhaul-conversation-starter.md
│       │   ├── big-picture-vision-and-durability.md
│       │   ├── conversation-starter-gap-analysis.md
│       │   └── architect-agent-finproxy-build-plan.md   ← SUPERSEDED (historical evidence)
│       ├── pipeline-orchestrator-conversation-starter.md  ← Original March 2026 motivation
│       ├── pipeline-orchestrator-consolidated-build-plan.md
│       ├── pipeline-orchestrator-motivation.md
│       └── c4-*.svg                                     ← Architecture diagrams
│
│── INTENT ROUTER ───────────────────────────────────────────────────
│
├── jarvis                    ← Intent router + General Purpose Agent
│   └── docs/research/ideas/
│       ├── jarvis-vision.md               ← Overall Jarvis vision & fleet architecture
│       ├── general-purpose-agent.md       ← The "everything else" ReAct agent
│       ├── nemoclaw-assessment.md         ← Why we're not using NemoClaw (yet)
│       └── reachy-mini-integration.md     ← Embodied voice interface design
│
│── CONTENT PIPELINE ────────────────────────────────────────────────
│
├── youtube-planner           ← AI-powered content planning pipeline
│   └── docs/research/
│       ├── ideas/
│       │   └── youtube-planner-vision.md
│       └── conversation-starters/
│           ├── 01-youtube-research-intelligence-starter.md
│           ├── 02-video-planning-pipeline-starter.md
│           └── 03-youtube-transcript-map-starter.md
│
│── GUARDKIT PLATFORM ───────────────────────────────────────────────
│
├── guardkit                  ← GuardKit CLI (slash commands, templates, AutoBuild)
│   ├── installer/core/templates/
│   │   ├── langchain-deepagents/                    ← Base template (production)
│   │   └── langchain-deepagents-weighted-evaluation/ ← Adversarial template (production)
│   └── docs/research/dark_factory/
│       ├── template-spec-python-library.md
│       ├── template-spec-nats-asyncio-service.md
│       └── archive/
│
│── EXEMPLARS & DATA ────────────────────────────────────────────────
│
├── deepagents-player-coach-exemplar    ← Source for base template
├── deepagents-orchestrator-exemplar    ← Source for orchestrator template
│
├── agentic-dataset-factory             ← Training data pipeline (multi-domain)
│   │                                      STATUS: Production — ~2,500 examples, first fine-tune complete
│   ├── scripts/
│   │   └── train_gemma4.py               ← Unsloth + TRL SFTTrainer fine-tuning script
│   ├── domains/                          ← Domain-agnostic pipeline, per-domain GOAL.md + sources/
│   │   └── gcse-english/                 ← First domain (GCSE English tutor)
│   └── docs/research/ideas/
│       └── fine-tuning-getting-started.md ← End-to-end fine-tuning guide
│
│── CLIENT PROJECTS ─────────────────────────────────────────────────
│
├── lpa-platform              ← FinProxy .NET platform (FastEndpoints, modular monolith)
│   │                            STATUS: Blocked on dotnet exemplar + template-create
│   ├── docs/
│   │   └── buildplan.md          ← Full GuardKit command sequence with --context flags
│   └── command-history.md        ← Pre-command setup, planned sequence
│
├── finproxy-docs             ← FinProxy LPA product documentation (310 KB, 14 docs)
│   │                            Proof point for Product Owner Agent
│   └──                           First domain for Architect Agent (Phase 0 + 1 validated)
│
│── ARCHIVED ────────────────────────────────────────────────────────
│
├── ideation-agent            ← ARCHIVED — ideas extracted to specialist-agent
│   └── (3 ideas preserved in specialist-agent/docs/research/ideas/ideation-role-extracted-ideas.md)
│
├── product-owner-agent       ← ARCHIVED — absorbed into specialist-agent unified harness
│
└── architect-agent-mcp       ← SUPERSEDED — MCP built into specialist-agent serve --role X --transport stdio
                                 (FEAT-014, ADR-ARCH-006)
```

---

## Agent Fleet Summary

### Specialist Agents (single codebase: `specialist-agent`)

| Role | Model | Fine-Tuned | Hosting | Purpose |
|------|-------|-----------|---------|---------|
| **Architect** | Gemma 4 31B | Yes — architecture books (Ousterhout, Ford, Farley, Evans) | Bedrock CMI | Product docs → C4 diagrams, ADRs, conversation starters for `/system-arch` |
| **Product Owner** | Gemma 4 31B | Yes — product management books (Cagan, Patton, Ries) | Bedrock CMI | Raw info → structured product documentation (user stories, acceptance criteria, backlog) |
| **Ideation** | Base Gemma 4 or Claude API | No | API call | Weighted evaluation of ideas — value is in criteria and Coach loop, not learned behaviour |

All three roles share the same generation loop, scoring engine, session lifecycle, LLM
client, CLI interface, MCP adapter framework, and Graphiti query/write tools. They
differ in Player prompt, Coach prompt + criteria weights, detection patterns, output
types, fine-tuned model path, fleet manifest, and knowledge graph group_ids.

**CLI:**
```bash
specialist-agent greenfield --role architect --docs ./finproxy-docs --scope "..."
specialist-agent greenfield --role product-owner --docs ./product-brief --scope "..."
specialist-agent serve --role architect --transport stdio    # MCP for Claude Desktop
specialist-agent serve --nats nats://localhost:4222          # NATS for fleet
specialist-agent metrics --role architect                    # Compounding report
```

### Pipeline Orchestration

| Agent | Repo | Model | Purpose |
|-------|------|-------|---------|
| **Forge** | `forge` | Claude Sonnet (orchestration reasoning) | Checkpoint manager — coordinates specialist agents, applies confidence-gated quality gates, manages pipeline state |

The Forge is a coordinator, not a specialist. It does not have a Player-Coach loop. It
does not need fine-tuning. It delegates domain judgment to specialist agents via NATS
`call_agent_tool()` and uses GuardKit slash commands for implementation stages. Its core
competency is: managing the state machine between checkpoints, knowing which agent to
call, what context to pass, evaluating Coach scores against thresholds, routing to
humans when confidence is low, and recovering from failures.

### Intent Routing & General Purpose

| Agent | Repo | Model | Purpose |
|-------|------|-------|---------|
| **Intent Router** | `jarvis` | Custom (thin) | Classify intent, dispatch to specialist |
| **General Purpose** | `jarvis` | Base model | Everything else — research, drafts, chores, tools |

### Content Pipeline

| Agent | Repo | Model | Purpose |
|-------|------|-------|---------|
| **YouTube Planner** | `youtube-planner` | Weighted evaluation | Content planning from idea to script |

### Training Data

| Agent | Repo | Model | Purpose |
|-------|------|-------|---------|
| **GCSE Tutor** | `agentic-dataset-factory` (pipeline) | Fine-tuned Gemma 4 31B | Multi-subject GCSE tutor — deploys to Bedrock first to validate import pipeline |

### Infrastructure Components

| Component | Repo | Type | Status | Purpose |
|-----------|------|------|--------|---------|
| **nats-core** | `nats-core` | Python library (pip-installable) | ✅ 97% coverage | Message schemas, topic constants, typed NATS client, fleet registration |
| **NATS Server** | `nats-infrastructure` | Config/ops (Docker Compose) | ✅ Configured | Server deployment, accounts, streams, monitoring |

---

## The Three-Layer Architecture

Every specialist agent operates on three independently updatable layers:

```
┌──────────────────────────────────────────────────────────────┐
│                      Specialist Agent                        │
│                                                              │
│  Layer 1: BEHAVIOUR (Fine-tuned Model Weights)               │
│  ──────────────────────────────────────────                   │
│  HOW to think. Taste. Judgment. Anti-pattern recognition.    │
│  Source: Gemma 4 31B fine-tuned via agentic dataset factory   │
│  on domain books. Deployed to AWS Bedrock CMI.               │
│  Phase F.                                                    │
│                                                              │
│  Layer 2: KNOWLEDGE (Per-Role + Per-Project Graph)           │
│  ──────────────────────────────────────────                   │
│  WHAT has been learned. Prior ADRs, patterns, signals.       │
│  Source: Graphiti with FalkorDB on GB10.                      │
│  Three scopes: project → role → fleet.                       │
│  Compounds with every session.                               │
│  Phase G.                                                    │
│                                                              │
│  Layer 3: CONTEXT (Project Documentation)                    │
│  ──────────────────────────────────────────                   │
│  WHAT to think about now. This project's docs.               │
│  Source: Direct file reading via doc_reader tool.             │
│  Phase 0. ✅ Working.                                        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**Key principles:**
- Fine-tuning teaches behaviour, not facts — the model learns Rich's architectural
  taste, not FinProxy's API contracts
- Graphiti stores relationships and decisions, not documents — validated limitation;
  use doc_reader for content
- Two independently updatable layers — retrain the model without touching the knowledge
  graph, or seed new project docs without retraining

---

## Confidence-Gated Checkpoint Protocol

The Forge applies confidence-gated quality gates at every pipeline stage. Every
specialist agent stage runs through the Coach (Player-Coach adversarial loop). The
Coach produces a weighted score with specific criterion breakdowns and detection pattern
findings. The Forge uses this score to decide the checkpoint mode:

**🟢 AUTO-APPROVE (score ≥ auto_threshold)**
Pipeline continues automatically. Human gets a notification (not a gate) with the
score, criterion breakdown, and a link to the output. Informational — the human can
review at leisure but the pipeline does not wait.

**🟡 FLAG FOR REVIEW (min_threshold ≤ score < auto_threshold)**
Pipeline pauses. Human gets the output plus the Coach's specific concerns — *which
criteria scored low* and *which detection patterns fired*. The human can approve,
request revision, or reject.

**🔴 HARD STOP (score < min_threshold, or critical detection fired)**
Pipeline blocks until human resolves. This fires when a critical detection pattern
triggers (PHANTOM, UNGROUNDED, contradicts existing ADRs) regardless of overall score.

Thresholds are configurable **per stage** and **per project**. The final PR review
remains a hard human checkpoint — this is the one gate that should never auto-approve.
Everything else is conditional on Coach confidence.

**Why this works:** The "93% defaults accepted" observation from the GuardKit
`/feature-spec` history docs validates that most of the time, the output is fine and
the human just says "approved." Hard checkpoints at every stage create unnecessary
friction — blocking the pipeline for a rubber stamp. The confidence-gated protocol
makes the 93% stat architectural: it is built into the checkpoint logic, not dependent
on human response time.

**NATS integration:** The checkpoint protocol maps directly to existing nats-core event
payloads — `ApprovalRequestPayload`, `ApprovalResponsePayload`, `NotificationPayload`.
No new wire formats needed.

---

## The Forge — Pipeline Orchestrator

The Forge delegates domain judgment to specialist agents via NATS, uses GuardKit slash
commands for implementation, and applies confidence-gated checkpoints at every stage:

```
Forge Agent
  │
  ├── DELEGATE to specialist agents (via NATS call_agent_tool)
  │   ├── product-owner-agent: "Generate product documentation from these raw inputs"
  │   │   → returns ProductDocument + Coach score + criterion breakdown
  │   ├── architect-agent: "Generate architecture from these product docs"
  │   │   → returns ConversationStarter + Coach score + criterion breakdown
  │   ├── architect-agent: "Is this feature feasible given our architecture?"
  │   │   → returns FeasibilityAssessment
  │   └── (future) ux-designer-agent
  │
  ├── EVALUATE (confidence-gated checkpoints)
  │   ├── Check Coach score against per-stage thresholds
  │   ├── Check for critical detection patterns
  │   ├── 🟢 Auto-approve → NotificationPayload to jarvis.notification.*
  │   ├── 🟡 Flag → ApprovalRequestPayload to agents.approval.*
  │   └── 🔴 Hard stop → ApprovalRequestPayload (risk_level: high)
  │
  ├── INVOKE GuardKit commands (for implementation stages)
  │   ├── /system-arch      (consumes architect-agent output as conversation starter)
  │   ├── /system-design    (consumes /system-arch output)
  │   ├── /feature-spec     (consumes product-owner output + architecture)
  │   ├── /feature-plan     (consumes /feature-spec output)
  │   ├── autobuild         (implements tasks from /feature-plan)
  │   └── /task-review      (reviews issues, creates fix tasks)
  │
  ├── COORDINATE
  │   ├── graphiti_seed     (seed outputs into knowledge graph after each stage)
  │   ├── verify            (run tests, integration checks)
  │   ├── git operations    (branch, commit, PR)
  │   └── ci_trigger        (GitHub Actions)
  │
  └── HARD CHECKPOINT (always human, never auto-approved)
      └── PR review — final gate before merge
```

**Degraded mode:** If a specialist agent is unavailable (not registered in
`NATSKVManifestRegistry`), the Forge falls back to invoking GuardKit commands directly.
Without a Coach score, the Forge cannot auto-approve — it always flags for human review.
Less confidence in the output → more human oversight.

**Three orchestration modes:**
- **Mode A: Greenfield** — full pipeline from raw input to deployed code
- **Mode B: Feature** — add a feature to an existing project
- **Mode C: Review-Fix** — review and fix issues in existing code

**Agent identity:**
```
agent_id: forge
trust_tier: core
nats_topic: agents.command.forge
max_concurrent: 3 (one pipeline per project, up to 3 projects)
```

See `forge-pipeline-orchestrator-refresh.md` (v3, 11 April 2026) for full tool
inventory, NATS integration details, pipeline event publishing, and configuration
schemas.

---

## Proof Points — Validation Milestones

| Milestone | Date | Result | Significance |
|-----------|------|--------|-------------|
| **Phase 0 FinProxy run** | March 2026 | 0.75 score, 3 iterations, 93s, 1006 tests green | First real specialist agent run — proves the Player-Coach adversarial loop works on real product docs |
| **Phase 1 FinProxy run** | April 2026 | 0.93 score, 2 iterations, 162s | Quality improvement from ADR format, narrative flow, failure mode detection. Fewer iterations = Coach confidence rising. |
| **Player-Coach flaw detection** | March 2026 | 4/4 planted flaws detected by Coach | Validates that two-model separation prevents self-confirmation bias |
| **First fine-tune (GCSE tutor)** | March 2026 | Gemma 4 31B, 1,736 examples, loss 2.45→0.50, 2h 5min on GB10 | End-to-end fine-tuning pipeline proven — Unsloth + TRL SFTTrainer on DGX Spark hardware |
| **Agentic dataset factory** | March 2026 | 94.8% acceptance rate, ~2,500 examples production run | Player-Coach adversarial loop generates high-quality training data at scale |
| **Docling PDF pipeline** | March 2026 | Standard + VLM modes validated on GB10 | PDF ingestion is not an open risk — both digital and scanned paperback paths work |
| **nats-core library** | April 2026 | 97% test coverage, 17 test files, 6 features implemented | Messaging backbone is working code, not a design contract |
| **FinProxy product docs** | January 2026 | 14 docs, 310 KB, produced in one weekend, approved by James with minimal feedback | Product Owner Agent proof point — manual version of the workflow already proven |
| **Feature spec defaults acceptance** | April 2026 | Rich accepts defaults ~95% of the time across 7 feature specs | GuardKit prompt calibration validated — provides training signal for future automation |

---

## Knowledge Compounding (Phase G)

The system learns from every session, per role:

| Scope | Group ID | What Compounds |
|-------|----------|---------------|
| Project | e.g. `finproxy` | Decisions on THIS project — ADRs, trade-offs, constraints |
| Role | `role:architect` | Patterns that work for THIS role across ALL projects |
| Fleet | `appmilla-fleet` | Conventions across ALL roles, ALL projects |

**Measurable compounding signal:**
- Iterations-to-pass should decrease over sessions
- First-iteration-score should increase over sessions
- Detection count should decrease as the Player learns to avoid anti-patterns

---

## Resolved Decisions (Fleet-Wide)

These apply across all repos. Do NOT reopen.

### Infrastructure & Framework (D1–D15)

| # | Decision | Resolution |
|---|----------|-----------|
| D1 | Agent framework | LangChain DeepAgents SDK |
| D2 | Reasoning model | Claude API or Gemini API (configurable per agent). Coach always uses Claude for two-model separation. |
| D3 | Implementation model | Claude Code SDK (cloud) or vLLM on GB10 (local) |
| D4 | Event bus | NATS JetStream |
| D5 | Two-model separation | Player and Coach use different model families to prevent self-confirmation bias. Coach has zero tools — evaluation is pure judgment, no data retrieval. |
| D6 | NemoClaw | Rejected — not production-ready on DGX Spark. Revisit Q3-Q4 2026. |
| D7 | Tool interface stability | Signatures identical across cloud and local modes |
| D8 | Multi-project | Concurrent pipelines with NATS topic prefix isolation |
| D9 | Template strategy | Option C — enhance base + create adversarial (harvest from production) |
| D10 | ChromaDB over NVIDIA RAG | ChromaDB PersistentClient for vector storage |
| D11 | nats-core uses nats-py | Library uses nats-py (minimal deps); services use FastStream (ADR-003) |
| D12 | NATS infrastructure standalone | Own repo — backbone middleware, not coupled to any consumer (ADR-001) |
| D13 | Account-based multi-tenancy | NATS accounts with scoped permissions per project (ADR-002). Rich sees everything. James sees FinProxy only. |
| D14 | Containerisation | Phase 2 — containers for lifecycle, concurrency, fleet scaling |
| D15 | Agent discovery | Dynamic CAN bus registration via NATS `fleet.register` (ADR-004) |

### Agent Architecture (D16–D22)

| # | Decision | Resolution |
|---|----------|-----------|
| D16 | Agent logic separate from adapters | Same core logic, different entry points (MCP, NATS, CLI). Adapter pattern. Three interface layers (ADR-ARCH-006). MCP must NOT carry pipeline traffic — serialises full tool schema into context window on every invocation. |
| D17 | AgentManifest as single source of truth | Both MCP tools and NATS registration derived from one manifest. Two-level registry: intents (routing) + tools (direct interaction). Each role's `agent-role.yaml` generates its AgentManifest — new roles are automatically discoverable by the Forge without any Forge code changes. |
| D18 | Tool risk classification | Every tool declares `read_only`, `mutating`, or `destructive` from day one |
| D19 | Trust tiers | `core` (infrastructure), `specialist` (fleet), `extension` (future plugins) |
| D20 | Direct tool topics | `agents.{agent_id}.tools.{tool_name}` for agent-to-agent calls bypassing Jarvis |
| D21 | Graphiti for relationships and decisions, not documents | Validated limitation — Graphiti cannot store document content reliably (fidelity failures documented Feb/Mar 2026). Use doc_reader for content, Graphiti for entities, decisions, and learned patterns. |
| D22 | AgentConfig standardisation | Shared Pydantic model in nats-core for runtime config (model endpoints, Graphiti connection, NATS URL, API keys, timeouts). Companion to AgentManifest — manifest is public (what), config is private (how). All agents import from nats-core. Uses pydantic-settings with `AGENT_` env prefix. |

### Unified Harness & Specialisation (D23–D32)

| # | Decision | Resolution |
|---|----------|-----------|
| D23 | Unified harness | One codebase, many roles. Architect + Product Owner + Ideation in single `specialist-agent` repo. 70% shared, 30% role-specific. (ADR-ARCH-008) |
| D24 | Role config pattern | YAML/markdown for values (criteria weights, prompts, detection patterns), Python code for structure (generation loop, scoring engine). (ADR-ARCH-009) |
| D25 | Repo rename strategy | Two-stage: repo rename first (`architect-agent` → `specialist-agent`, done), package rename in Phase 2. (ADR-ARCH-010) |
| D26 | Fine-tuning teaches behaviour, not facts | Two-layer: fine-tune for taste/judgment, RAG (ChromaDB) for knowledge. Independently updatable. The model learns Rich's architectural taste, not FinProxy's API contracts. |
| D27 | Graphiti for facts/relationships, not document storage | Validated limitation. doc_reader for content, Graphiti for entities, decisions, and learned patterns. Per-role learning via `role:{role_id}` group IDs. |
| D28 | Skills layer dropped | Superseded by fine-tuning strategy. System prompt carries patterns + criteria. Fine-tuning teaches the behaviour that skills would have constrained. |
| D29 | Three specialist deployments | Architect (fine-tuned Bedrock), Product Owner (fine-tuned Bedrock), Ideation (base model, no fine-tuning). UX Designer future when Stitch MCP + agent-browser ready. |
| D30 | GCSE tutor deploys to Bedrock first | Validates Bedrock Custom Model Import pipeline, frees GB10 for development. ~$1.50-3.00 per run, scales to zero. |
| D31 | MCP adapter built into specialist-agent | `architect-agent-mcp` repo superseded. `specialist-agent serve --role X --transport stdio` provides role-specific MCP server. One binary, multiple MCP tool sets derived from AgentManifest. (ADR-ARCH-006, FEAT-014) |
| D32 | Per-role knowledge compounding | `role:{role_id}` group IDs in Graphiti. Three-scope query: project → role → fleet. Measurable signal: iterations-to-pass, first-iteration-score, detection count trending. |

### Software Factory & Coordination (D33–D38)

| # | Decision | Resolution |
|---|----------|-----------|
| D33 | Context-first delivery — no kanban, no tickets | The Software Factory collapses planning, knowledge, and build into one pipeline. Structured docs are the project management. Coordination is emergent, not explicit. Bolting AI onto existing PM tools (Linear, Jira) is electrifying the steam engine — the productivity gains only come when you abandon the assumption that coordination requires a separate system. |
| D34 | Outcome gates replace progress tracking | Pass/fail gates at pipeline stages replace sprint velocity, burn-down charts, and status reports. A gate is either passed or it is not. An agent monitors for it, signals when it is met or at risk, and surfaces context when human judgment is needed. Queryable context and proactive signals replace scheduled reporting. |
| D35 | Confidence-gated checkpoints | Coach score determines human engagement: 🟢 auto-approve (≥ threshold), 🟡 flag for review, 🔴 hard stop (critical detection). Replaces hard human checkpoints at every stage. Uses existing nats-core payloads (`ApprovalRequestPayload`, `ApprovalResponsePayload`, `NotificationPayload`) — no new wire formats. Thresholds configurable per stage and per project. |
| D36 | Forge is checkpoint manager, not specialist | Delegates domain judgment to specialist agents via NATS `call_agent_tool()`. No Player-Coach loop for Forge itself. Uses strong reasoning model (Claude Sonnet) for orchestration decisions, not fine-tuned domain judgment. Specialist agents return Coach scores — Forge reads them, does not re-evaluate. |
| D37 | PR review always human | The final gate before merge never auto-approves regardless of Coach score. Every other stage is conditional on Coach confidence. |
| D38 | `feature_ready_for_build` replaces kanban-triggered events | Emitted by the Forge when the GuardKit command sequence completes — not by a webhook from a PM tool card state change. `ticket_updated` event dropped entirely. PM tools (Linear, GitHub Projects) repositioned as optional visibility adapters that subscribe to pipeline events — they are viewports, not control surfaces. |

---

## Build Sequence (Per-Repo)

The old 11-phase linear sequence is replaced by a per-repo phase structure that
reflects actual dependencies:

### specialist-agent

```
Phase 0   ✅  Basic working agent (0.75 score, March 2026)
Phase 1   ✅  Output quality (0.93 score, April 2026)
Phase 1C  ◻   Domain fidelity — fix 5 regressions: SOURCE_COLLAPSE, DOMAIN_DILUTION,
              missing .NET/Python boundary, NATS removed, thin research topics (FEAT-009)
Phase 1B  ◻   Unified harness — --role flag, role-aware codebase, product owner as
              second role (FEAT-008, ADR-ARCH-008/009)
Phase G   ◻   Graphiti runtime — query tool (read), write-back (write), per-role
              learning metrics, `role:{role_id}` group IDs (FEAT-G01-G03)
              Can run parallel with Phase 2.
Phase 2   ◻   Web search, DDD context maps, Mode 2 alignment judgments
Phase 3   ◻   NATS + MCP adapters — fleet integration, Claude Desktop tools,
              agent-to-agent communication. Import nats-core, register manifest on
              startup, subscribe to command topics, return results. (FEAT-011-014)
Phase F   ◻   Per-role fine-tuning — book extraction via Docling, training data via
              agentic-dataset-factory, Gemma 4 31B per role on GB10, deploy to Bedrock CMI
```

**DDD Southwest forcing function:** Phase 0 demonstrated as minimum. Phase 1B (second
role) and Phase 3 (agent-to-agent NATS calls) are the demo vehicles for the talk on
16 May 2026.

### nats-core

```
✅ Implemented — 97% test coverage, 17 test files, 6 features
   NATSClient, MessageEnvelope, Topics, AgentConfig, AgentManifest,
   NATSKVManifestRegistry, 19 typed event payloads

◻  Integration tests against live NATS server (weekend task — validates end-to-end)
```

### nats-infrastructure

```
✅ Configured — Docker Compose, accounts, streams, entrypoint, verification script

◻  Spin up on GB10 — cp .env.example .env, set passwords, docker compose up -d
   (weekend task — prerequisite for nats-core integration tests)
```

### forge

```
◻  Blocked on:
   1. nats-infrastructure running on GB10 ✅ (configured)
   2. nats-core integration tests passing (weekend task)
   3. specialist-agent Phase 3 (NATS fleet integration)

   Build sequence when unblocked:
   - /system-arch with forge pipeline orchestrator conversation starters as input
   - /system-design
   - /feature-spec × N
   - /feature-plan × N
   - autobuild × N
```

The Forge is the capstone. It is the last major agent to build because it coordinates
everything else. But it is also the highest-leverage: once it works, the Software
Factory is real.

### lpa-platform (FinProxy)

```
◻  Blocked on:
   1. dotnet-fastendpoints exemplar built and validated
   2. /template-create from exemplar
   3. Build plan ready: lpa-platform/docs/buildplan.md (full command sequence written)
```

### agentic-dataset-factory

```
✅ Production — ~2,500 examples, 94.8% acceptance rate
✅ First fine-tune complete (GCSE English, Gemma 4 31B, loss 2.45→0.50)
✅ Docling PDF pipeline validated (standard + VLM modes)

◻  Multi-subject expansion:
   - Maths: ~500-600 additional behaviour examples
   - French (AQA 8652) + Spanish (AQA 8692): new specs, first exams summer 2027,
     specimen papers only
   - Multi-domain merge script (validates 75/25 <think> ratio, controls subject
     balance, shuffles)
```

### youtube-planner, jarvis, guardkit

Blocked on forge + nats infrastructure for fleet integration. YouTube Planner has
vision docs and conversation starters ready. Jarvis has vision docs for intent routing
and general purpose agent. GuardKit is working and in production daily.

---

## Templates

### Built & Proven (Production)

| Template | Location | Created From |
|----------|----------|-------------|
| `langchain-deepagents` | `guardkit/installer/core/templates/langchain-deepagents/` | `deepagents-player-coach-exemplar` — Base template with ALL TRF-12 universal fixes |
| `langchain-deepagents-weighted-evaluation` | `guardkit/installer/core/templates/langchain-deepagents-weighted-evaluation/` | Extended from base — IntensityRouter, weighted Coach, HITL, sprint contracts |

### Built (Pending Review Task → Built-In)

| Template | Location | Created From |
|----------|----------|-------------|
| `langchain-deepagents-orchestrator` | `~/.agentecflow/templates/langchain-deepagents-orchestrator/` | `deepagents-orchestrator-exemplar` — Multi-model orchestration, subagent composition, domain context injection. **Review task pending to add as GuardKit built-in.** |

### To Create

| Template | Spec Location | Purpose | First Project |
|----------|--------------|---------|---------------|
| `python-library` | `guardkit/docs/research/dark_factory/template-spec-python-library.md` | Pure Python installable package | `nats-core` |
| `nats-asyncio-service` | `guardkit/docs/research/dark_factory/template-spec-nats-asyncio-service.md` | Asyncio daemon via NATS/JetStream | Jarvis adapters |
| `dotnet-fastendpoints` | (planned from lpa-platform exemplar) | .NET modular monolith (FastEndpoints, FAPI 2.0, Keycloak) | `lpa-platform` |

---

## Formalised Patterns

Three patterns have emerged organically across repos and are now codified as standard
practice for all GuardKit-managed projects.

### Pattern 1: Build Plans

**What it is:** A document per phase or per repo that captures the full GuardKit
command sequence with all `--context` flags. Includes prerequisites, expected outputs
per step, feature dependency graph, validation criteria, and estimated timeline.

**Why it matters:** The build plan is the bridge between scope docs (what to build) and
the actual GuardKit commands (how to build). Without it, each session rediscovers the
`--context` flags, prerequisite ordering, and validation steps. With it, Claude Code
can be handed the build plan directly and execute the sequence.

**Where it is proven:**

| Build Plan | Repo | Coverage |
|-----------|------|---------|
| `phase1-build-plan.md` | specialist-agent | Phase 1 output quality |
| `phase1b-unified-harness-build-plan.md` | specialist-agent | Phase 1B unified harness |
| `phase1c-domain-fidelity-build-plan.md` | specialist-agent | Phase 1C domain fidelity |
| `phaseG-build-plan.md` | specialist-agent | Phase G Graphiti runtime |
| `phase2-build-plan.md` | specialist-agent | Phase 2 web search/DDD |
| `phase3-build-plan.md` | specialist-agent | Phase 3 NATS/MCP |
| `phaseF-build-plan.md` | specialist-agent | Phase F fine-tuning |
| `buildplan.md` | lpa-platform | FinProxy LPA platform (full command sequence) |

**Standard structure:**

```markdown
# Phase X Build Plan — [Title]
## Status: Ready for `/feature-spec FEAT-XXX`
## Repo: guardkit/[repo]
## Target: [timeline]

## Prerequisites
- [ ] List of blocking dependencies

## Feature Summary
| # | Feature | Depends On | Est. Duration |

## GuardKit Command Sequence
### Step 1: /feature-spec
[exact command with all --context flags]
### Step 2: /feature-plan
### Step 3: Build
### Step 4: Validation

## Files That Will Change
| File | Feature | Change Type |

## Expected Timeline
```

### Pattern 2: Command History

**What it is:** A running log at the repo root that captures every GuardKit command
run, its output, and any decisions made during the session. Pre-command setup
documented at the top.

**Why it matters:** The command history is evidence of what was actually run, in what
order, with what results. It is the ground truth that conversation starters and build
plans are derived from. It enables reproducibility and serves as training signal for
future pipeline automation.

**Where it is proven:**

| File | Repo | Coverage |
|------|------|---------|
| `command-history.md` | nats-core | Full /system-arch → /system-design → /feature-spec × 6 → build |
| `command_history.md` | specialist-agent | Phase 0 through Phase 1 commands |
| `command-history.md` | lpa-platform | Pre-command setup, planned sequence (not yet executed) |

### Pattern 3: Feature Spec History

**What it is:** A per-feature log at the repo root that captures the full
`/feature-spec` session — Rich's responses to each acceptance group, assumptions
resolved, and final outcome.

**Why it matters:** These documents reveal a critical pattern — **Rich almost always
accepts defaults.** Across seven feature specs in specialist-agent, Rich accepts
default acceptance groups almost always ("A" for accept), accepts default edge case
expansions almost always ("Y"), and accepts assumptions at face value with review
required only for high-risk items. Rare interventions occur only when the spec misses
something domain-specific.

This validates two things:
1. **GuardKit prompt calibration is well-tuned.** The defaults are right ~95% of the
   time, which means the system understands what "good" looks like for these project
   types.
2. **Training signal for automation.** If the human accepts 95% of defaults, the
   system can learn when NOT to ask. This directly informs the confidence-gated
   checkpoint protocol (D35) — the thresholds can be set empirically from observed
   acceptance rates.

**Where it is proven:**

| File(s) | Repo | Coverage |
|---------|------|---------|
| `feature-spec-FEAT-001-history.md` through `FEAT-007-history.md` | specialist-agent | 7 feature specs |
| `feature-spec-domain-fidelity-history.md` | specialist-agent | Domain fidelity feature |
| `feature-plan-fidelity-history.md` | specialist-agent | Feature plan session |
| `system-arch-history.md` | specialist-agent | System architecture session |
| `system-design-history.md` | specialist-agent | System design session |
| `system-plan-history.md` | specialist-agent | System plan session |
| `system-arch-history.md` | nats-core | System architecture session |

---

## Landscape Positioning

Research completed April 2026 covering Devin, MetaGPT, ChatDev, CrewAI, and LangGraph.
Five differentiators identified for the Software Factory approach:

1. **Fine-tuned domain models** — no public project combines role-specific fine-tuned
   models with adversarial quality evaluation
2. **Adversarial quality evaluation** — Player-Coach with weighted criteria and
   detection patterns, not just generate-and-ship
3. **Per-role knowledge compounding** — Graphiti-backed learning that improves with
   every session, not stateless runs
4. **NATS fleet coordination** — agents discover and call each other dynamically, not
   hardcoded pipelines
5. **Serverless Bedrock deployment** — production hosting that scales to zero, not
   always-on GPU infrastructure

**Solow Paradox framing for DDD Southwest:** "You can see the AI age everywhere except
in the productivity statistics." Accelerating coding without fixing coordination simply
exposes the coordination bottleneck. The Software Factory addresses the whole system,
not just the code generation step.

See `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` for full
positioning analysis.

---

## DDD Southwest Talk — 16 May 2026

**"2026: The Year of the Software Factory"** — Engine Shed, Bristol

Demo sequence:
1. "Here's the architect agent producing architecture from product docs" (Phase 0/1
   FinProxy run)
2. "Here's the same harness configured as a product owner" (Phase 1B second role)
3. "The two agents call each other via NATS — the product owner asks the architect if
   an idea is feasible" (Phase 3 agent-to-agent)
4. "Each agent runs a different fine-tuned model, trained on different books, evaluated
   by different criteria — but the harness code is identical" (the engineering insight)
5. "The training data was generated by yet another agent, using the same adversarial
   quality pattern" (the flywheel)
6. "The system learns from every session — the 10th project is easier than the 1st"
   (Phase G compounding)

Narrative arc: Solow Paradox → coordination bottleneck → steam engine trap → context-
first delivery → outcome gates → the factory runs end-to-end → human only engaged when
the Coach has specific concerns.

---

## Hardware Topology

| Machine | Role |
|---------|------|
| **MacBook Pro M2 Max** | Planning/research. Claude Desktop. Dashboard client. CLI adapter. Cloud API calls. Primary pair-programming environment. |
| **Dell DGX Spark GB10 (128GB)** | NATS server. vLLM inference. FalkorDB (Graphiti). ChromaDB. Fine-tuning (Unsloth). Agent execution. Docker. Reachy USB. |
| **Synology DS918+ NAS (32TB)** | Storage/backup. Not compute. |
| **Reachy Mini ×2** | Scholar (tutoring) + Bridge (Jarvis interface). On order. |

### Port Allocation on GB10

| Port | Service | Used By |
|------|---------|---------|
| 4222 | NATS server (client connections) | All agents, adapters, clients |
| 8222 | NATS monitoring (HTTP API) | Dashboard, health checks |
| 8000 | Graphiti LLM (Qwen2.5-14B-Instruct-FP8) | Graphiti entity extraction |
| 8001 | Embedding model (nomic-embed-text-v1.5, 768-dim) | Graphiti + ChromaDB |
| 8002 | AutoBuild LLM (Qwen3-Coder-Next) | Implementation model (local mode) |

Fine-tuned models (Gemma 4 31B) served via vLLM on GB10 for development, deployed to
AWS Bedrock CMI for production.

Connected via Tailscale mesh VPN. Accessible from MacBook Pro, team devices, and
(future) Reachy Mini robots.

---

## Related Documents Outside These Repos

| Document | Location | Relevance |
|----------|----------|-----------|
| YouTube Channel Research Starters | `~/Projects/YouTube Channel/01-*.md`, `02-*.md`, `03-*.md` | Input for YouTube Planner (also in youtube-planner repo) |
| YouTube System Arch (Draft) | `~/Projects/YouTube Channel/system-arch-youtube-pipeline.md` | Previous arch draft — NemoClaw refs need updating |
| YouTube Feature Specs | `~/Projects/YouTube Channel/feature-01` through `feature-05` | Individual feature specifications |
| Channel Briefing | `~/Projects/YouTube Channel/youtube-channel-project-briefing.md` | Channel strategy and context |
| DDD Southwest Talk | `~/Projects/YouTube Channel/ddd-southwest-adversarial-cooperation-talk.md` | Talk material drawing from all this work |
| Ship's Computer Architecture v1.0 | Project knowledge (Claude Desktop) | Original NATS + Reachy architecture from Jan 2026 |
| Software Factory Coordination Framing | (produced in PM tools conversation, 7 April 2026) | Steam engine trap, context-first delivery, DDD talk framing |

---

## Superseded Documents

These documents remain in the repo as historical evidence of the build approach but
should not be used as current sources of truth.

| Document | Location | Superseded By |
|----------|----------|--------------|
| `architect-agent-finproxy-build-plan.md` | `forge/docs/research/ideas/` | Phase-specific build plans in `specialist-agent/docs/research/ideas/` |
| Original `architect-agent-vision.md` (March 2026) | — | `specialist-agent/docs/research/ideas/architect-agent-vision.md` (12 April 2026) |
| `pipeline-orchestrator-conversation-starter.md` (March 2026) | `forge/docs/research/` | `forge-pipeline-orchestrator-refresh.md` (v3, 11 April 2026) — addendum, not replacement; read together |

---

## Document Provenance

This fleet-master-index was rewritten on 12 April 2026 from the following source
documents:

| Source | What It Provided |
|--------|-----------------|
| `forge/docs/research/ideas/forge-ideas-overhaul-conversation-starter.md` | Delta table, update plan, three patterns to formalise |
| `specialist-agent/docs/research/ideas/architect-agent-vision.md` | Current project state, three-layer architecture, phase sequence, three roles |
| `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` (v3) | Forge identity, confidence-gated checkpoints, tool inventory, NATS integration |
| `dev-pipeline-architecture.md` + `dev-pipeline-system-spec.md` | Build Agent lifecycle, topic taxonomy, PM adapter layer, multi-tenancy |
| Conversation: "Project management tools and Linear's agent-based approach" (7 April 2026) | No-kanban decision, steam engine framing, context-first delivery, outcome gates |
| Conversation: "Balancing extensibility and simplicity in NATS core design" (8 April 2026) | `feature_ready_for_build` event redesign, `ticket_updated` dropped |

---

*Fleet master index v2: 12 April 2026*
*"The factory doesn't mine the ore or design the blueprint — it does the making. And it knows when to ask for help."*
