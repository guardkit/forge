# Ship's Computer Fleet — Master Index

## All Repos, All Docs, All Agents · April 2026

---

## Overview

The Ship's Computer is a distributed multi-agent system orchestrated through NATS
JetStream, with an intent router (Jarvis) dispatching requests to specialist agents.
The system is accessible through multiple adapters: Reachy Mini (voice), Telegram,
Slack, Dashboard, CLI.

This document is the master index across all repos in the `guardkit/` organisation.

---

## The Full Pipeline

The fleet forms a complete pipeline from ideation through to deployed code:

```
Ideation Agent → Product Owner Agent → Architect Agent → GuardKit Factory
(explore)        (document)             (architect)       (implement)
     ↑                                                        ↓
     └──────────── General Purpose Agent ─────────────────────┘
                   (everything else)

YouTube Planner ← (content ideas about the above)

                    ┌─────────────────┐
All agents ←───────→│  NATS JetStream  │←── nats-infrastructure (deployment)
All adapters ←─────→│  (nats-core)     │
                    └─────────────────┘
```

All agents use the `langchain-deepagents-weighted-evaluation` template (except General
Purpose which uses the base template). All use Gemini 3.1 Pro for reasoning. All
communicate via NATS JetStream. All dispatched by the Jarvis intent router.

---

## Repository Map

```
guardkit/
│
│── INFRASTRUCTURE ──────────────────────────────────────────────────
│
├── nats-core                 ← Shared contract layer (pip-installable library)
│   └── docs/design/
│       ├── contracts/
│       │   └── agent-manifest-contract.md        ← AgentManifest, ToolCapability, IntentCapability schemas
│       ├── specs/
│       │   └── nats-core-system-spec.md          ← 6 features (was 5), BDD acceptance criteria
│       └── decisions/
│           ├── ADR-001-nats-as-event-bus.md
│           ├── ADR-002-schema-versioning.md
│           ├── ADR-003-nats-py-vs-faststream.md
│           └── ADR-004-dynamic-fleet-registration.md
│
├── nats-infrastructure       ← NATS server deployment, accounts, streams (config/ops)
│   └── docs/design/
│       ├── specs/
│       │   └── nats-infrastructure-system-spec.md  ← 6 features, 26 tasks
│       └── decisions/
│           ├── ADR-001-standalone-infra-repo.md
│           └── ADR-002-account-multi-tenancy.md
│
│── ADAPTERS ────────────────────────────────────────────────────────
│
├── architect-agent-mcp       ← MCP adapter for Architect Agent (Claude Desktop access)
│   └── docs/
│       ├── contracts/
│       │   └── contract-reference.md              ← References nats-core + architect-agent contracts
│       ├── decisions/
│       │   └── ADR-001-manifest-derived-adapters.md ← MCP tools derived from AgentManifest
│       └── design/specs/
│           └── architect-agent-mcp-spec.md        ← 5 MCP tools, async lifecycle, BDD criteria
│
│── AGENTS ──────────────────────────────────────────────────────────
│
├── jarvis                    ← Intent router + General Purpose Agent
│   └── docs/research/ideas/
│       ├── jarvis-vision.md               ← Overall Jarvis vision & fleet architecture
│       ├── general-purpose-agent.md       ← The "everything else" ReAct agent
│       ├── nemoclaw-assessment.md         ← Why we're not using NemoClaw (yet)
│       └── reachy-mini-integration.md     ← Embodied voice interface design
│
├── guardkitfactory           ← Autonomous software development pipeline
│   └── docs/research/
│       ├── ideas/
│       │   └── fleet-master-index.md      ← THIS DOCUMENT
│       ├── pipeline-orchestrator-conversation-starter.md  ← For /system-arch
│       ├── pipeline-orchestrator-consolidated-build-plan.md
│       ├── pipeline-orchestrator-motivation.md
│       └── c4-*.svg                       ← Architecture diagrams
│
├── product-owner-agent       ← Raw information → structured product documentation
│   └── docs/research/ideas/
│       └── product-owner-agent-vision.md  ← Product documentation agent vision
│
├── architect-agent           ← Product docs → system architecture → /system-arch input
│   └── docs/
│       ├── design/contracts/
│       │   └── architect-agent-manifest.md    ← Specific manifest: 3 intents, 5 tools, 6 eval criteria
│       └── research/ideas/
│           └── architect-agent-vision.md      ← Architecture generation agent vision
│
├── youtube-planner           ← AI-powered content planning pipeline
│   └── docs/research/
│       ├── ideas/
│       │   └── youtube-planner-vision.md  ← Content pipeline vision & architecture
│       └── conversation-starters/         ← Transferred from ~/Projects/YouTube Channel/
│           ├── 01-youtube-research-intelligence-starter.md
│           ├── 02-video-planning-pipeline-starter.md
│           └── 03-youtube-transcript-map-starter.md
│
├── ideation-agent            ← Structured brainstorming with weighted evaluation
│   └── docs/research/ideas/
│       └── ideation-agent-vision.md       ← Ideation system vision & criteria
│
│── GUARDKIT PLATFORM ───────────────────────────────────────────────
│
├── guardkit                  ← GuardKit CLI (slash commands, templates, AutoBuild)
│   ├── installer/core/templates/
│   │   ├── langchain-deepagents/                    ← Base template (production)
│   │   └── langchain-deepagents-weighted-evaluation/ ← Adversarial template (production)
│   └── docs/research/dark_factory/
│       ├── template-spec-python-library.md          ← Spec for /template-create
│       ├── template-spec-nats-asyncio-service.md    ← Spec for /template-create
│       └── archive/                                 ← Superseded docs
│
│── EXEMPLARS & DATA ────────────────────────────────────────────────
│
├── deepagents-player-coach-exemplar    ← Source for base template
├── deepagents-orchestrator-exemplar    ← Source for orchestrator template
├── agentic-dataset-factory             ← Training data pipeline (GCSE English first domain)
│
└── finproxy-docs             ← FinProxy LPA product documentation (310 KB, 14 docs)
    └── Proof point for Product Owner Agent, first domain for Architect Agent
```

---

## Agent Fleet Summary

| Agent | Repo | Template | Complexity | Purpose |
|-------|------|----------|-----------|---------|
| **Intent Router** | `jarvis` | Custom (thin) | Low | Classify intent, dispatch to specialist |
| **General Purpose** | `jarvis` | `langchain-deepagents` | Low | Everything else — research, drafts, chores, tools |
| **Ideation Agent** | `ideation-agent` | `langchain-deepagents-weighted-evaluation` | Medium | Structured brainstorming with scored evaluation |
| **Product Owner Agent** | `product-owner-agent` | `langchain-deepagents-weighted-evaluation` | Medium | Raw info → structured product documentation |
| **Architect Agent** | `architect-agent` | `langchain-deepagents-weighted-evaluation` | Medium | Product docs → system architecture → `/system-arch` input |
| **GuardKit Factory** | `guardkitfactory` | `langchain-deepagents-orchestrator` | High | Autonomous software development pipeline |
| **YouTube Planner** | `youtube-planner` | `langchain-deepagents-weighted-evaluation` | Medium | Content planning from idea to script |
| **GCSE Tutor** | (future) | TBD | Medium | Fine-tuned Nemotron Nano via Reachy "Scholar" |

### Infrastructure Components

| Component | Repo | Type | Purpose |
|-----------|------|------|---------|
| **nats-core** | `nats-core` | Python library (pip-installable) | Message schemas, topic constants, typed NATS client |
| **NATS Server** | `nats-infrastructure` | Config/ops (Docker Compose) | Server deployment, accounts, streams, monitoring |

### Adapter Components

| Component | Repo | Type | Purpose |
|-----------|------|------|---------|
| **Architect Agent MCP** | `architect-agent-mcp` | MCP server | Claude Desktop access to Architect Agent. Derives tools from AgentManifest. Zero business logic — pure adapter. |

### Pipeline Flow

```
Ideation → Product Owner → Architect → GuardKit Factory
   │            │              │              │
   │ weighted   │ weighted     │ weighted     │ adversarial
   │ eval       │ eval         │ eval         │ Player-Coach
   │            │              │              │
   │ explore    │ document     │ C4 + ADRs    │ /system-arch
   │ score      │ structure    │ conversation │ /system-design
   │ rank       │ evaluate     │ starter      │ /feature-spec
   │            │              │              │ /feature-plan
   │            │              │              │ autobuild
   └────────────┴──────────────┴──────────────┘
         All use Gemini 3.1 Pro for reasoning
         All communicate via NATS JetStream
         All import nats-core for message contracts
```

### Proof Points

| Agent | Evidence |
|-------|---------|
| **GuardKit Factory** | TASK-REV-F5F5: 43 tasks, 3 human decisions, 93% defaults accepted |
| **Product Owner Agent** | FinProxy: 14 docs (310 KB) in one weekend, James approved with minimal feedback |
| **Ideation Agent** | Every Claude Desktop session — manual version of this workflow already proven |
| **Architect Agent** | Every conversation starter doc — manual version of this workflow already proven |

---

## Resolved Decisions (Fleet-Wide)

These apply across all repos. Do NOT reopen.

| # | Decision | Resolution |
|---|----------|-----------|
| D1 | Agent framework | LangChain DeepAgents SDK |
| D2 | Reasoning model | Gemini 3.1 Pro API or Claude API (configurable) |
| D3 | Implementation model | Claude Code SDK (cloud) or vLLM on GB10 (local) |
| D4 | Event bus | NATS JetStream |
| D5 | Two-model separation | Orchestration model ≠ implementation model |
| D6 | NemoClaw | Rejected — not production-ready on DGX Spark. Revisit Q3-Q4 2026. |
| D7 | Tool interface stability | Signatures identical across cloud and local modes |
| D8 | Multi-project | Concurrent pipelines with NATS topic prefix isolation |
| D9 | Template strategy | Option C — enhance base + create adversarial (harvest from production) |
| D10 | ChromaDB over NVIDIA RAG | ChromaDB PersistentClient for vector storage |
| D11 | nats-core uses nats-py | Library uses nats-py (minimal deps); services use FastStream (ADR-003) |
| D12 | NATS infrastructure standalone | Own repo — backbone middleware, not coupled to any consumer (ADR-001) |
| D13 | Account-based multi-tenancy | NATS accounts with scoped permissions per project (ADR-002) |
| D14 | Containerisation | Phase 2 — containers for lifecycle, concurrency, fleet scaling |
| D15 | Agent discovery | Dynamic CAN bus registration via NATS fleet.register (ADR-004) |
| D16 | Agent logic separate from adapters | Same core logic, different entry points (MCP now, NATS later). Adapter pattern. |
| D17 | AgentManifest as single source of truth | Both MCP tools and NATS registration derived from one manifest. Two-level registry: intents (routing) + tools (direct interaction). |
| D18 | Tool risk classification | Every tool declares read_only, mutating, or destructive from day one |
| D19 | Trust tiers | core (infrastructure), specialist (fleet), extension (future plugins) |
| D20 | Direct tool topics | `agents.{agent_id}.tools.{tool_name}` for agent-to-agent calls bypassing Jarvis |
| D21 | Graphiti for architectural memory | Working MCP setup and proven Python client retained for Architect Agent |

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
| `langchain-deepagents-orchestrator` | `~/.agentecflow/templates/langchain-deepagents-orchestrator/` | `deepagents-orchestrator-exemplar` — Multi-model orchestration, subagent composition, domain context injection. 7 specialist agents. **Review task pending to add as GuardKit built-in.** |

### To Create

| Template | Spec Location | Purpose | First Project |
|----------|--------------|---------|---------------|
| `python-library` | `guardkit/docs/research/dark_factory/template-spec-python-library.md` | Pure Python installable package | `nats-core` |
| `nats-asyncio-service` | `guardkit/docs/research/dark_factory/template-spec-nats-asyncio-service.md` | Asyncio daemon via NATS/JetStream | Jarvis adapters |

### Workflow

```bash
# 1. Create remaining templates
/template-create --name python-library --path ~/Projects/appmilla_github/youtube-transcript-mcp
/template-create --name nats-asyncio-service --path <bootstrap-from-faststream-cookiecutter>

# 2. Review task: add all three new templates as built-in to guardkit installer
# (langchain-deepagents-orchestrator + python-library + nats-asyncio-service)
```

---

## Build Sequence (Updated)

**Priority change (April 2026):** Phase 8 (Architect Agent) has been brought forward
to run concurrently with Phases 1-2. FinProxy needs architecture work now, and the
Architect Agent is designed to work standalone (via MCP) before NATS infrastructure
exists. The AgentManifest contract ensures zero refactoring when NATS arrives later.
See `architect-agent-finproxy-build-plan.md` for the detailed build plan.

### Phase 0: Already Done
- GuardKit CLI + slash commands (`/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`)
- Three DeepAgents templates (base, weighted-evaluation, orchestrator)
- Graphiti knowledge graph (FalkorDB-backed, MCP integration)
- vLLM on GB10 (3 models: embedding, Qwen3-Coder-Next, Graphiti LLM)
- Agentic Dataset Factory (11 runs, 31 fixes, Player-Coach adversarial loop)
- Ship's Computer Architecture (designed, v1.0)
- FinProxy product documentation (14 docs, 310 KB — PO Agent proof point)
- All vision docs and system specs across 8 repos

### Phase 1: Templates (In Progress)
- `python-library` template → `/template-create`
- `nats-asyncio-service` template → `/template-create`
- Review task: add orchestrator + python-library + nats-asyncio-service as built-in to installer

### Phase 2: NATS Infrastructure
- **`nats-core`** library (from `python-library` template) — message envelope, event schemas, topic registry, typed client
  - **Repo:** `nats-core`
  - **Spec ready:** `docs/design/specs/nats-core-system-spec.md` (5 features, BDD acceptance criteria)
  - **Slash command:** `/feature-spec` (BDD scenarios for schema validation contracts)
- **`nats-infrastructure`** deployment (Docker Compose, accounts, streams, ops scripts)
  - **Repo:** `nats-infrastructure`
  - **Spec ready:** `docs/design/specs/nats-infrastructure-system-spec.md` (6 features, 26 tasks)
  - **Slash command:** `/feature-plan` (task-oriented deployment config)

### Phase 3: GuardKit Factory (Primary Deliverable)
- Orchestrator agent (Gemini 3.1 Pro reasoning + slash commands as tools)
- Pipeline state persistence
- NATS adapter for input/output
- Human-in-the-loop checkpoint flow
- **Repo:** `guardkitfactory`
- **Template:** `langchain-deepagents-orchestrator`
- **Conversation starter ready:** `pipeline-orchestrator-conversation-starter.md`

### Phase 4: Jarvis Intent Router
- Intent classification (local model or rules + LLM fallback)
- Dispatch logic (intent → agent topic mapping)
- Session context management
- **Repo:** `jarvis`

### Phase 5: General Purpose Agent
- ReAct agent with Phase 1 tools (web search, calendar, email, Slack, weather)
- Model routing (local for simple, cloud for complex)
- **Repo:** `jarvis` (co-located)

### Phase 6: Ideation Agent
- Weighted evaluation criteria for ideas
- Player (explorer) + Coach (evaluator) + Orchestrator (session manager)
- Graphiti integration for project landscape context
- **Repo:** `ideation-agent`

### Phase 7: Product Owner Agent
- Raw info → structured product documentation
- Domain-configurable document templates
- FinProxy docs as exemplar/few-shot
- Cross-document consistency evaluation
- **Repo:** `product-owner-agent`

### Phase 8: Architect Agent (BROUGHT FORWARD — active now, supports FinProxy)
- Product docs → C4 diagrams + ADRs + conversation starter
- Graphiti integration (reads prior ADRs, writes new ones — compounds over time)
- Encodes Rich's architectural patterns (C4 validation, stay-at-altitude, review-before-fix)
- Output: GuardKit conversation starter document for `/system-arch`
- **AgentManifest-driven:** 3 intents, 5 tools, 6 weighted evaluation criteria
- **Repo:** `architect-agent` (core logic) + `architect-agent-mcp` (MCP adapter)
- **First domain:** FinProxy LPA Platform (14 product docs → Phase 1 architecture)
- **Build plan:** `guardkitfactory/docs/research/ideas/architect-agent-finproxy-build-plan.md`
- **Key contract:** `nats-core/docs/design/contracts/agent-manifest-contract.md`

### Phase 9: YouTube Planner
- Transcript Map sub-system (foundation)
- Research Intelligence sub-system
- Planning Pipeline sub-system
- **Repo:** `youtube-planner`

### Phase 10: Adapters (from `nats-asyncio-service` template)
- Telegram adapter (quickest to test)
- Dashboard adapter (React + WebSocket)
- CLI adapter
- Reachy Mini adapter (when hardware arrives)

### Phase 11: Template Harvest
- Extract `langchain-deepagents-toolbox` pattern from General Purpose Agent (if distinct enough)

**Note:** Phases 6-9 can be built in any order once the intent router exists.
The numbering reflects logical dependency, not strict sequence. Product Owner
and Architect agents could be built before Ideation if there's immediate demand
(e.g., FinProxy needs architecture work now).

---

## Hardware Topology

| Machine | Role |
|---------|------|
| **MacBook Pro M2 Max** | Planning/research. Dashboard client. CLI adapter. Cloud API calls. |
| **Dell DGX Spark GB10 (128GB)** | NATS server. vLLM (3 models). Graphiti (FalkorDB). Agent execution. Docker. Reachy USB. |
| **Synology DS918+ NAS (32TB)** | FalkorDB persistence. JetStream backup. Shared storage. |
| **Reachy Mini ×2** | Scholar (tutoring) + Bridge (Jarvis interface). On order. |

### Port Allocation on GB10

| Port | Service | Used By |
|------|---------|---------|
| 4222 | NATS server (client connections) | All agents, adapters, clients |
| 8222 | NATS monitoring (HTTP API) | Dashboard, health checks |
| 8000 | Graphiti LLM (Qwen2.5-14B) | Graphiti entity extraction |
| 8001 | Embedding model (nomic-embed) | Graphiti + ChromaDB |
| 8002 | AutoBuild LLM (Qwen3-Coder-Next) | Implementation model (local mode) |

Connected via Tailscale mesh VPN.

---

## Related Documents Outside These Repos

| Document | Location | Relevance |
|----------|----------|-----------|
| YouTube Channel Research Starters | `~/Projects/YouTube Channel/01-*.md`, `02-*.md`, `03-*.md` | Input for YouTube Planner (also transferred to youtube-planner repo) |
| YouTube System Arch (Draft) | `~/Projects/YouTube Channel/system-arch-youtube-pipeline.md` | Previous arch draft — **NemoClaw refs need updating** |
| YouTube Feature Specs | `~/Projects/YouTube Channel/feature-01` through `feature-05` | Individual feature specifications |
| Channel Briefing | `~/Projects/YouTube Channel/youtube-channel-project-briefing.md` | Channel strategy and context |
| DDD Southwest Talk | `~/Projects/YouTube Channel/ddd-southwest-adversarial-cooperation-talk.md` | Talk material drawing from all this work |
| Ship's Computer Architecture v1.0 | Project knowledge (Claude Desktop) | Original NATS + Reachy architecture from Jan 2026 |
| FinProxy Product Docs | `finproxy-docs/` (14 docs, 310 KB) | Proof point for Product Owner Agent, first domain for Architect Agent |
| Architect Agent + FinProxy Build Plan | `guardkitfactory/docs/research/ideas/architect-agent-finproxy-build-plan.md` | Master TODO, all decisions, build sequence, file references |
| Agent Manifest Contract | `nats-core/docs/design/contracts/agent-manifest-contract.md` | Shared schema for AgentManifest, ToolCapability, IntentCapability |
