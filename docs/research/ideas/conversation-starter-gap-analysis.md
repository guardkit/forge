# Ship's Computer / Jarvis — Conversation Starter & Gap Analysis

## For: Sanity check, gap analysis, and onboarding context · April 2026
## Updated: 12 April 2026 — repo references, capabilities, resolved gaps

---

## How to Use This Document

This is the single document that explains the entire Ship's Computer / Jarvis project.
Read it to understand what we're building, why, where all the documentation lives, and
what gaps still need addressing. It consolidates a long ideation session (April 2026)
into a structured brief.

**Note (12 April 2026):** The project has evolved significantly since this document was
first written. The fleet-master-index (v2, 12 April 2026) is now the authoritative
technical inventory. This document remains useful as a onboarding overview and for the
gap analysis, but repo references and build sequences should be cross-checked against
the fleet-master-index.

---

## 1. The Problem Statement

### What we're solving

A solo software engineer (Rich, 52, Appmilla) with 25+ years of systems experience is
navigating the transition to AI-augmented development. The daily workflow involves:

- **Ideation** in Claude Desktop — freeform back-and-forth exploring concepts
- **Product documentation** — manually synthesising brain dumps into structured docs
- **Architecture** — manually writing C4 diagrams, ADRs, and conversation starters
- **Implementation** — driving GuardKit slash commands (`/system-arch` → `autobuild`)
- **Content creation** — planning YouTube videos about the journey
- **General tasks** — research, drafting, scheduling, chores

Each of these is currently a separate manual workflow. Context is lost between sessions.
Architecture decisions get re-learned. The human is the orchestrator, the context carrier,
and the bottleneck.

### What the Software Factory replaces

The human-as-orchestrator role. The Forge (pipeline orchestrator) coordinates specialist
agents via NATS JetStream, applying confidence-gated quality gates at each stage. The
human moves from operator to approver — giving direction, reviewing output when the
Coach flags concerns, making decisions at checkpoints.

The coordination overhead between planning, knowledge, and build systems ceases to exist
as a distinct concern. Structured documents *are* the project management. Outcome gates
replace progress tracking. There are no tickets, no kanban boards, no status reports.

---

## 2. Motivations (Why Build This?)

### 2.1 Learning Vehicle

The system is a vehicle for learning AI-augmented development hands-on:
- Fine-tuning small language models (Unsloth + TRL SFTTrainer on Gemma 4 31B Dense)
- RAG with knowledge graphs (Graphiti, FalkorDB, ChromaDB)
- Agent harness design (LangChain DeepAgents SDK, unified harness pattern)
- Adversarial cooperation (Player-Coach, weighted evaluation, detection patterns)
- Local inference (vLLM on DGX Spark GB10)
- Multi-agent orchestration (NATS JetStream, CAN bus registration)
- Training data generation (agentic dataset factory, Player-Coach adversarial loop)
- Container-based agent deployment (Docker Compose fleet)

The combination of these skills is genuinely rare. The war stories from building are
the durable asset — 180+ review reports, the FB01-FB28 cascading fix series, the
NemoClaw saga, the C4 validation discovery, Phase 0/1 FinProxy scores, the first
fine-tune, domain fidelity regressions.

### 2.2 YouTube Content

Every problem solved is a video. Every honest failure builds audience trust.
70% browse (story-driven) / 30% search (tutorials). The Rory Sutherland insight:
sell how you think, not what you do. The system IS the content.

Key content arcs:
- "I'm Building My Own Jarvis" — the vision
- "Why I'm Not Using NemoClaw (Yet)" — marketing vs reality
- "What I Learned from 180 Review Reports" — methodology
- "2026: The Year of the Software Factory" — DDD Southwest talk (May 16)

### 2.3 DDD Southwest Talk

16 May 2026, Engine Shed, Bristol. "2026: The Year of the Software Factory." Narrative
arc: Solow Paradox → coordination bottleneck → steam engine trap → context-first
delivery → outcome gates → the factory runs end-to-end. Five differentiators identified
via landscape research (Devin, MetaGPT, ChatDev, CrewAI, LangGraph).

### 2.4 Practical Productivity

Beyond learning and content — this genuinely makes Rich more productive:
- FinProxy proof point: 14 product docs (310 KB) in one weekend, James approved
- Phase 0 FinProxy: 0.75 score, 93 seconds, first real specialist agent run
- Phase 1 FinProxy: 0.93 score, 162 seconds, quality improvement validated
- nats-core: 97% test coverage, 6 features, GuardKit command sequence produced
  production-quality library code
- AutoBuild proof point: 43 tasks, 3 human decisions, 93% defaults accepted
- Feature spec defaults: Rich accepts ~95% of proposed defaults across 7 specs
- First fine-tune: Gemma 4 31B, 1,736 examples, loss 2.45→0.50 in 2h 5min

### 2.5 Durability

Three-layer durability analysis (see big-picture doc):
- **Permanent:** Methodology, domain knowledge, war stories, coordination insights
- **2-3+ years:** Architectural patterns (NATS, intent routing, unified harness,
  confidence-gated checkpoints, context-first delivery)
- **12-18 months:** Specific tools and templates (designed to be replaceable)

---

## 3. What We're Building

### 3.1 The Full Pipeline

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

YouTube Planner ← (content ideas about the above)

                    ┌─────────────────┐
All agents ←───────→│  NATS JetStream  │←── nats-infrastructure
All adapters ←─────→│  (nats-core)     │
                    └─────────────────┘
                           ↑
                    Jarvis Intent Router
                    (CAN bus discovery)
                           ↑
              ┌────────────┼────────────┐
              │            │            │
         Reachy Mini   Telegram     Dashboard
         (voice)       (text)       (web UI)
```

### 3.2 Agent Fleet

**Specialist Agents** (single codebase: `specialist-agent`, unified harness pattern):

| Role | Model | Fine-Tuned | Purpose |
|------|-------|-----------|---------|
| **Architect** | Gemma 4 31B | Yes (architecture books) | Product docs → C4/ADRs → `/system-arch` input |
| **Product Owner** | Gemma 4 31B | Yes (PM books) | Raw info → structured product docs |
| **Ideation** | Base model / Claude API | No | Weighted evaluation of ideas |

**Other Fleet Agents:**

| Agent | Repo | Purpose |
|-------|------|---------|
| **Forge** | `forge` | Pipeline orchestrator + checkpoint manager |
| **Intent Router** | `jarvis` | Classify intent, dispatch via CAN bus |
| **General Purpose** | `jarvis` | Everything else — research, drafts, chores |
| **YouTube Planner** | `youtube-planner` | Content planning: idea → filmable script |
| **GCSE Tutor** | `agentic-dataset-factory` (pipeline) | Fine-tuned multi-subject GCSE tutor |

### 3.3 Infrastructure

| Component | Repo | Status | Purpose |
|-----------|------|--------|---------|
| **nats-core** | `nats-core` | ✅ 97% coverage | Message schemas, topic constants, fleet registration, typed NATS client |
| **NATS Server** | `nats-infrastructure` | ✅ Configured | Docker Compose deployment, accounts, streams, monitoring |

### 3.4 Key Architectural Patterns

| Pattern | What It Does |
|---------|-------------|
| **Unified harness** | One codebase, many roles via `specialist-agent serve --role X` |
| **Three-layer architecture** | Behaviour (fine-tuned) + Knowledge (Graphiti) + Context (project docs) |
| **CAN bus registration** | Agents self-announce capabilities; Jarvis builds routing table dynamically |
| **Weighted evaluation** | Subjective quality → gradable scores via Player-Coach with criteria + detection patterns |
| **Two-model separation** | Player and Coach use different model families (prevents self-confirmation) |
| **Confidence-gated checkpoints** | Coach score determines auto-approve / flag / hard stop |
| **Context-first delivery** | No tickets, no kanban — docs are the coordination, outcome gates replace progress tracking |
| **Provider independence** | Cloud/local/Bedrock switchable via config |
| **AgentManifest as source of truth** | Both MCP tools and NATS registration derived from one manifest |
| **Container lifecycle = agent lifecycle** | Container starts → agent registers; stops → deregisters |

---

## 4. Document Map

### Strategic Documents

| Document | Location | What It Covers |
|----------|----------|---------------|
| **Big Picture Vision & Durability** | `forge/docs/research/ideas/big-picture-vision-and-durability.md` | Why we're building, three goals, durability analysis, containerisation strategy, compounding flywheel, DDD narrative |
| **Fleet Master Index** | `forge/docs/research/ideas/fleet-master-index.md` | Technical inventory: all repos, all agents, decisions D1-D38, per-repo build sequence, formalised patterns, hardware topology |
| **Forge Pipeline Orchestrator Refresh** | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | Forge identity, confidence-gated checkpoints, tool inventory, NATS integration, degraded mode |
| **This Document** | `forge/docs/research/ideas/conversation-starter-gap-analysis.md` | Sanity check, gap analysis, onboarding |

### Agent Vision Documents

| Document | Location |
|----------|----------|
| Specialist Agent Vision (architect, product owner, ideation) | `specialist-agent/docs/research/ideas/architect-agent-vision.md` |
| Unified Agent Harness | `specialist-agent/docs/research/ideas/unified-agent-harness-conversation-starter.md` |
| MCP Deployment Architecture | `specialist-agent/docs/research/ideas/mcp-deployment-architecture.md` |
| Fine-Tuned Strategy & Three-Layer Architecture | `specialist-agent/docs/research/ideas/fine-tuned-architect-agent-strategy.md` |
| Landscape Research (Devin, MetaGPT, etc.) | `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` |
| Jarvis Vision (intent router + CAN bus) | `jarvis/docs/research/ideas/jarvis-vision.md` |
| General Purpose Agent | `jarvis/docs/research/ideas/general-purpose-agent.md` |
| Reachy Mini Integration | `jarvis/docs/research/ideas/reachy-mini-integration.md` |
| NemoClaw Assessment | `jarvis/docs/research/ideas/nemoclaw-assessment.md` |
| YouTube Planner Vision | `youtube-planner/docs/research/ideas/youtube-planner-vision.md` |

### System Specifications

| Document | Location | Status |
|----------|----------|--------|
| nats-core System Spec (6 features, BDD) | `nats-core/docs/design/specs/nats-core-system-spec.md` | ✅ Implemented, 97% coverage |
| nats-infrastructure System Spec (6 features) | `nats-infrastructure/docs/design/specs/nats-infrastructure-system-spec.md` | ✅ Configured |

### Phase Build Plans (specialist-agent)

| Document | Location | Coverage |
|----------|----------|---------|
| Phase 1C Domain Fidelity | `specialist-agent/docs/research/ideas/phase1c-domain-fidelity-build-plan.md` | Fix 5 regressions |
| Phase 1B Unified Harness | `specialist-agent/docs/research/ideas/phase1b-unified-harness-build-plan.md` | Role-aware codebase |
| Phase G Graphiti Runtime | `specialist-agent/docs/research/ideas/phaseG-build-plan.md` | Per-role knowledge compounding |
| Phase 2 Web Search / DDD | `specialist-agent/docs/research/ideas/phase2-build-plan.md` | External research |
| Phase 3 NATS / MCP | `specialist-agent/docs/research/ideas/phase3-build-plan.md` | Fleet integration |
| Phase F Fine-Tuning | `specialist-agent/docs/research/ideas/phaseF-build-plan.md` | Per-role model training |
| LPA Platform Build Plan | `lpa-platform/docs/buildplan.md` | FinProxy .NET full command sequence |

### Architecture Decision Records

| ADR | Location | Decision |
|-----|----------|---------|
| ADR-001 through ADR-004 | `nats-core/docs/design/decisions/` | NATS, schema versioning, nats-py vs FastStream, dynamic fleet registration |
| ADR-001, ADR-002 | `nats-infrastructure/docs/design/decisions/` | Standalone infra repo, account multi-tenancy |
| ADR-ARCH-006 through ADR-ARCH-010 | `specialist-agent/docs/decisions/` | Three interface layers, unified harness, role config, repo rename |

### Templates

| Template | Status | Location |
|----------|--------|----------|
| `langchain-deepagents` | Production (built-in) | `guardkit/installer/core/templates/` |
| `langchain-deepagents-weighted-evaluation` | Production (built-in) | `guardkit/installer/core/templates/` |
| `langchain-deepagents-orchestrator` | Built (pending review → built-in) | `~/.agentecflow/templates/` |
| `python-library` | Spec ready | `guardkit/docs/research/dark_factory/` |
| `nats-asyncio-service` | Spec ready | `guardkit/docs/research/dark_factory/` |
| `dotnet-fastendpoints` | Planned (from lpa-platform exemplar) | — |

### Proof Points

| Proof Point | Location |
|------------|----------|
| FinProxy product docs (14 docs, 310 KB) | `finproxy-docs/` |
| Phase 0/1 FinProxy architect agent runs | `specialist-agent/` (output + command history) |
| First fine-tune outputs (GCSE tutor) | GB10: `~/fine-tuning/output/gcse-tutor-gemma4-31b/` |
| GCSE dataset factory (~2,500 examples) | `agentic-dataset-factory/output/` |
| nats-core library (97% coverage) | `nats-core/` |
| Feature spec history (7 specs) | `specialist-agent/feature-spec-FEAT-001-history.md` through `FEAT-007` |
| AutoBuild review reports | `guardkit/` (various TASK-REV files) |

---

## 5. Resolved Decisions (Fleet-Wide, Do NOT Reopen)

See `fleet-master-index.md` (v2, 12 April 2026) for the complete list of 38 resolved
decisions (D1–D38), organised into four groups:

- **D1–D15:** Infrastructure & Framework
- **D16–D22:** Agent Architecture
- **D23–D32:** Unified Harness & Specialisation
- **D33–D38:** Software Factory & Coordination

Key additions since this document was first written include: unified harness (D23),
fine-tuning teaches behaviour not facts (D26), three specialist deployments (D29),
context-first delivery with no kanban/tickets (D33), outcome gates replace progress
tracking (D34), confidence-gated checkpoints (D35), and Forge as checkpoint manager
not specialist (D36).

---

## 6. Build Sequence

The old 11-phase linear sequence has been replaced by a per-repo phase structure. See
`fleet-master-index.md` for the full breakdown. Summary:

```
specialist-agent:  Phase 0 ✅ → 1 ✅ → 1C → 1B → G → 2 → 3 → F
nats-core:         ✅ Implemented (97% coverage)
nats-infrastructure: ✅ Configured (ready to run)
forge:             Blocked on specialist-agent Phase 3 + nats infra
lpa-platform:      Blocked on dotnet exemplar + template-create
```

The Forge is the capstone — the last major agent to build because it coordinates
everything else.

---

## 7. Gap Analysis — Known Gaps & Open Questions

**Note (12 April 2026):** Several gaps from the original analysis have been resolved or
significantly de-risked. Status updated below.

### 7.1 Graphiti Integration Strategy — PARTIALLY RESOLVED

**Original gap:** No unified spec for how agents connect to Graphiti.

**What's resolved:**
- Per-role knowledge compounding designed (Phase G scope + build plan in specialist-agent)
- Three-scope model defined: project → role → fleet, via `group_id`
- Validated limitation documented: Graphiti stores relationships and decisions, not
  documents (D27)
- `doc_reader` for content, Graphiti for entities and learned patterns

**What remains:** Phase G not yet built. Integration tests against live Graphiti +
specialist-agent needed. Schema evolution strategy still open.

### 7.2 Monitoring & Observability Dashboard — OPEN (Gap: MEDIUM)

No spec change. Dashboard deferred until agents produce data worth displaying. The
NATS monitoring endpoint (:8222) provides infrastructure health. Coach scores and
pipeline events provide application-level observability when the Forge is running.

### 7.3 Error Handling & Dead Letter Queues — PARTIALLY RESOLVED

**What's resolved:** nats-core implements `MessageEnvelope` with `correlation_id` for
tracing related messages. `BuildFailedPayload` has `recoverable` flag. Forge pipeline
orchestrator refresh (v3) defines degraded mode behaviour.

**What remains:** Dead letter queue configuration, retry strategy per stream, partial
state recovery for multi-stage pipeline failures. These should be addressed when the
Forge build begins.

### 7.4 End-to-End Testing Strategy — OPEN (Gap: MEDIUM)

No spec change. nats-core has 97% unit test coverage with mocked NATS. Integration
tests against a live NATS server are the weekend task. Multi-agent workflow testing
remains unaddressed.

### 7.5 GCSE Tutor Integration — PARTIALLY RESOLVED

**What's resolved:**
- First fine-tune complete (Gemma 4 31B, loss 2.45→0.50)
- Docling pipeline validated (standard + VLM modes on GB10)
- Multi-subject expansion planned (Maths, French, Spanish)
- Decision to deploy to Bedrock first (D30) — validates import pipeline, frees GB10

**What remains:** Fleet integration decision — standalone Open WebUI deployment or
NATS agent? Reachy Mini "Scholar" interface design. Multi-domain merge script for
combining training data from multiple subjects.

### 7.6 API Cost Management — RESOLVED (Gap: LOW)

**Resolution:** AWS Bedrock CMI provides serverless hosting at ~$1.50-3.00 per run,
scales to zero. Local vLLM on GB10 for development. The cost model is now
well-understood: fine-tuned models on Bedrock for production, API calls (Claude, Gemini)
for Coach evaluation, local inference for iteration.

### 7.7 Migration Path from Current Workflow — RESOLVED

**Resolution:** The migration is already happening incrementally:
- GuardKit CLI is used daily (Phase 0 already done)
- specialist-agent Phase 0/1 validated on FinProxy
- nats-core implemented
- Each phase adds capability without disrupting current workflow
- The Forge is the step that replaces the manual pipeline operator role

### 7.8 Adapter Priority & Telegram Bot — OPEN (Gap: LOW)

No change. Deferred until fleet is running.

### 7.9 Security: Agent-to-Agent Trust — OPEN (Gap: LOW)

No change. NATS account-level isolation (APPMILLA vs FINPROXY) handles project
isolation. Per-agent permissions within an account deferred until fleet is larger.

### 7.10 Merge Addendum Specs — RESOLVED

**Resolution:** Addendums merged into parent specs during nats-core implementation.
Both specs are now complete and implemented.

---

## 8. Hardware Topology

| Machine | Role | Key Ports |
|---------|------|-----------|
| **Dell DGX Spark GB10 (128GB)** | NATS server, vLLM (3 models), FalkorDB, ChromaDB, Docker, fine-tuning, agent execution | 4222, 8222, 8000, 8001, 8002 |
| **MacBook Pro M2 Max** | Planning/research, Claude Desktop, primary pair-programming | — |
| **Synology DS918+ NAS (32TB)** | Storage/backup (not compute) | — |
| **Reachy Mini ×2** (on order) | Scholar (tutoring) + Bridge (Jarvis voice interface) | USB |

Connected via Tailscale mesh VPN.

---

## 9. What We're NOT Building

- Not a product for others (yet) — personal/Appmilla system
- Not a replacement for Claude Desktop — agents jumpstart, humans deep-dive
- Not a competitor to Cursor/Windsurf — full lifecycle, not just coding
- Not an enterprise platform — single developer + small team
- Not using NemoClaw — rejected, revisit Q3-Q4 2026
- Not a better Jira — the coordination layer ceases to exist, not improves

---

## 10. Next Actions

See `fleet-master-index.md` for the current per-repo build sequence and blocking
dependencies. The immediate path is:

1. Spin up NATS on GB10, run nats-core integration tests (validates messaging backbone)
2. specialist-agent Phase 1C (domain fidelity fixes)
3. specialist-agent Phase 1B (unified harness — second role)
4. specialist-agent Phase 3 (NATS fleet integration)
5. Forge build (capstone — coordinates everything)


---

*Original: April 2026*
*Updated: 12 April 2026 — repo references, capabilities, resolved gaps, context-first delivery*
