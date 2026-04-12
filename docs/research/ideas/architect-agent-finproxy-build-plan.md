# ⚠️ SUPERSEDED — Historical Evidence Only

> **This document has been superseded** by the phase-specific build plans in
> `specialist-agent/docs/research/ideas/`:
>
> - `phase1c-domain-fidelity-build-plan.md`
> - `phase1b-unified-harness-build-plan.md`
> - `phaseG-build-plan.md`
> - `phase2-build-plan.md`
> - `phase3-build-plan.md`
> - `phaseF-build-plan.md`
>
> And for the FinProxy platform specifically:
> - `lpa-platform/docs/buildplan.md`
>
> **Do not use this document for current planning.** It reflects the state of the
> project on 4-5 April 2026, before the unified harness decision (ADR-ARCH-008),
> the repo rename (`architect-agent` → `specialist-agent`, ADR-ARCH-010), and
> Phase 0/1 validation runs.
>
> **Why it is preserved:** This document is evidence of the build approach —
> how decisions were structured, how phases were planned, how Graphiti integration
> was designed. The decisions (DA1–DA15) influenced and were carried forward into
> the fleet-wide decisions (D16–D32) in the fleet-master-index. The Graphiti
> knowledge scoping table and export design remain valid references.
>
> *Superseded: 12 April 2026*

---

# Architect Agent & FinProxy — Build Plan & TODO List

## For: Tracking all decisions and actions from April 2026 ideation session
## Date: 4 April 2026 (updated: 5 April 2026)
## Status: SUPERSEDED — see header above

---

## Context

This plan emerged from a comprehensive gap analysis and ideation session covering:
the Ship's Computer conversation starter review, the Architect Agent vision, FinProxy
product documentation (14 docs, 310KB), the LangChain DeepAgents SDK (Gemini 3 video),
Claude Code's 12 architectural primitives (tool registry, permission tiers, etc.),
and a Graphiti integration deep-dive covering fleet knowledge, project scoping,
embedding confirmation, and client data portability.

The core decision: bring the Architect Agent forward to support FinProxy immediately,
designed so pieces bolt together without refactoring when NATS fleet infrastructure
arrives later.

---

## Key Design Decisions Made

| # | Decision | Rationale |
|---|----------|-----------|
| DA1 | Agent logic separate from adapters | Same core logic, different entry points (MCP now, NATS later). Adapter pattern from Jarvis architecture. |
| DA2 | AgentManifest as single source of truth | Both MCP tools and NATS registration derived from one manifest. Two-level registry: intents (routing) + tools (direct interaction). |
| DA3 | Graphiti stays in | Working MCP setup and proven Python client. Provides architectural memory across projects. |
| DA4 | Use current DeepAgents SDK | Latest pulled, skills installed as plugin. `langchain-deepagents-weighted-evaluation` template is current. |
| DA5 | FinProxy Phase 1 scope | MoneyHub integration, auth, transaction data retrieval, C#/.NET + Python boundary. No mobile, voice, B2B, or knowledge graph in Phase 1. |
| DA6 | Tool risk classification from day one | `read_only`, `mutating`, `destructive`. Cheap to add now, required by FinProxy's regulated context. |
| DA7 | Trust tiers on agents | `core` (infrastructure), `specialist` (fleet), `extension` (future plugins). Simple three-level model. |
| DA8 | Direct tool topics for agent-to-agent | `agents.{agent_id}.tools.{tool_name}` — bypasses Jarvis for targeted calls. |
| DA9 | `appmilla-fleet` group ID for internal knowledge | Separates Appmilla's institutional knowledge (D1-D21, patterns, ADRs) from client projects. Never exported to clients. |
| DA10 | Episode-based Graphiti export for client handover | Receiving party re-extracts entities with their own LLM/embeddings. No dimension coupling. Cleaner and more portable than graph dumps. |
| DA11 | Export script lives in guardkit repo | Guardkit owns the Graphiti client, FalkorDB docker, and vLLM scripts. Export is a Graphiti operational concern alongside backup. |
| DA12 | Agents query both fleet + project scope by default | `group_ids=["appmilla-fleet", "{project_id}"]`. Cross-project pattern discovery is the compounding value. |
| DA13 | Safety guard on export | Export NEVER includes `appmilla-fleet` or `guardkit` group IDs. Prevents accidental leaking of internal knowledge to clients. |
| DA14 | Embedding model confirmed: nomic 768-dim | `nomic-ai/nomic-embed-text-v1.5` on GB10 port 8001. FalkorDB index rebuilt after OpenAI→nomic switch. Dimension mismatch resolved. No migration needed. |
| DA15 | AgentManifest + AgentConfig separation | Manifest = public capabilities (what the agent does), published to fleet. Config = private runtime settings (how the agent runs), local only. Both Pydantic models in nats-core so all agents share the same schema. Prevents config drift across 6+ agents. |

---

## Master TODO List

### IMMEDIATE — Housekeeping

- [x] Create `architect-agent-mcp` repo on GitHub and clone
- [x] Add docs folder structure (specs, contracts, decisions)
- [~] Merge `nats-core-spec-addendum-fleet-registration.md` into `nats-core-system-spec.md` (executing)
  - File: `nats-core/docs/design/specs/nats-core-system-spec.md`
  - Source: `nats-core/docs/design/specs/nats-core-spec-addendum-fleet-registration.md`
  - Action: Follow merge instructions in the addendum, then archive/delete the addendum
- [~] Merge `nats-infrastructure-spec-addendum-fleet-compose.md` into `nats-infrastructure-system-spec.md` (executing)
  - File: `nats-infrastructure/docs/design/specs/nats-infrastructure-system-spec.md`
  - Source: `nats-infrastructure/docs/design/specs/nats-infrastructure-spec-addendum-fleet-compose.md`
- [~] Update fleet master index — add architect-agent-mcp as 11th repo, decisions D14-D21 (executing)
- [ ] Commit all repos with current state (save point before implementation)

### PHASE A — Contracts & Foundations (design before build)

**A1. AgentManifest contract in nats-core**
- File: `nats-core/docs/design/contracts/agent-manifest-contract.md`
- Status: **Created** (this session)
- Contains: `AgentManifest`, `ToolCapability`, `IntentCapability`, `AgentHeartbeatPayload`, `AgentDeregistrationPayload`
- This is the shared contract that all repos depend on

**A2. ADR-005 in nats-core: Two-level capability registry**
- File: `nats-core/docs/design/decisions/ADR-005-two-level-capability-registry.md`
- Status: **To create** in Claude Code session
- Decision: Intents for Jarvis routing + Tools for direct interaction, both in one manifest
- References: Claude Code 12 primitives analysis, existing ADR-004

**A3. Tool topics in nats-core topic registry**
- File: `nats-core/docs/design/specs/nats-core-system-spec.md` (update Topics section)
- Add: `agents.{agent_id}.tools.{tool_name}` namespace
- Add: `NATSClient.call_agent_tool()` convenience method

**A4. ADR-001 in architect-agent-mcp**
- File: `architect-agent-mcp/docs/decisions/ADR-001-manifest-derived-adapters.md`
- Status: **Created** (this session)
- Decision: MCP server derives tool definitions from AgentManifest, zero business logic in adapter

**A5. Architect Agent MCP server spec**
- File: `architect-agent-mcp/docs/design/specs/architect-agent-mcp-spec.md`
- Status: **Created** (this session)
- Contains: MCP tool definitions, async lifecycle, Claude Desktop integration

**A6. Architect Agent manifest definition (worked example)**
- File: `architect-agent/docs/design/contracts/architect-agent-manifest.md`
- Status: **Created** (this session)
- Contains: The specific intents, tools, and evaluation criteria for the Architect Agent

**A7. Seed Graphiti with fleet-wide knowledge** ← UPDATED
- Script: `guardkit/scripts/graphiti-seed-fleet.py` (to create)
- Group ID: `appmilla-fleet`
- Seed content:
  - All 21 resolved decisions (D1-D21) as individual episodes
  - 6 architectural patterns (C4 validation, stay at altitude, review-before-fix, two-model separation, provider independence, exemplar-first)
  - Existing ADRs from nats-core (ADR-001 through ADR-005)
  - Key template descriptions
- Query convention: Agents query `["appmilla-fleet", "{project_id}"]` by default (DA12)
- Method: Graphiti `add_episode()` with `group_id="appmilla-fleet"`
- Spec: `guardkit/docs/research/graphiti-refinement/FEAT-GR-007-fleet-knowledge-and-export.md`
- When: Before first Architect Agent run on FinProxy

**A8. Embedding model confirmation** ← RESOLVED
- Status: **Resolved — no action needed** (DA14)
- Model: `nomic-ai/nomic-embed-text-v1.5` (768-dim) on GB10 port 8001
- Script: `guardkit/scripts/vllm-embed.sh`
- FalkorDB was explicitly cleared and reseeded via `guardkit graphiti seed --force` after switching from OpenAI API to local nomic on GB10
- Dimension mismatch is fully behind us

**A9. Create Graphiti export/import tooling** ← NEW
- Scripts: `guardkit/scripts/graphiti-export.sh` + `guardkit/scripts/graphiti-export.py`
- Purpose: Selective project knowledge export for client handover
- Exports only episodes with matching `group_id` — receiving party replays via `add_episode`
- Safety guard: NEVER exports `appmilla-fleet` or `guardkit` group IDs (DA13)
- Output: `exports/{project}-knowledge-{timestamp}/` containing episodes.json, entities.json, facts.json, metadata.json, import.py, README.md
- Spec: `guardkit/docs/research/graphiti-refinement/FEAT-GR-007-fleet-knowledge-and-export.md`
- When: Before FinProxy handover (not blocking the build, but needs to exist before project completion)

### PHASE B — Concurrent Build (all four repos)

**B1. nats-core library**
- Repo: `guardkit/nats-core`
- Template: `python-library`
- Commands: `guardkit init` (done), then `/feature-spec` → `/feature-plan` → AutoBuild
- Input: Merged system spec + agent manifest contract
- Delivers: Pydantic schemas, topic registry, typed NATS client, fleet registration helpers, ManifestRegistry interface
- Key files: `src/nats_core/events/fleet.py`, `src/nats_core/manifest.py`, `src/nats_core/topics.py`, `src/nats_core/client.py`
- Also delivers: `AgentConfig` schema in `src/nats_core/config.py` — shared runtime config model (ModelConfig, GraphitiConfig, NATSConfig, API keys, timeouts). Companion to AgentManifest. Uses pydantic-settings for environment variable override with `AGENT_` prefix.

**B2. nats-infrastructure deployment**
- Repo: `guardkit/nats-infrastructure`
- Commands: `/feature-plan` → AutoBuild (merged spec)
- Delivers: Docker Compose for NATS server, `agent-registry` KV bucket, fleet streams, monitoring endpoint
- Key files: `docker-compose.yml`, `nats-server.conf`, fleet compose overlay

**B3. architect-agent core logic**
- Repo: `guardkit/architect-agent`
- Template: `langchain-deepagents-weighted-evaluation`
- Commands: `guardkit init` → build manually or via `/feature-spec` → `/feature-plan`
- Delivers: Player-Coach weighted evaluation loop, 6 evaluation criteria, Graphiti integration, conversation starter output formatter
- **Skills design (resolved → superseded 5 April 2026):**
  - Original design: 6 skill markdown files loaded dynamically per phase
  - **Superseded by fine-tuning strategy:** Skills were designed for prompt-injecting
    judgment into a general-purpose API model. With fine-tuning (Phase F), the judgment
    is intrinsic to the model weights. The extracted book principles feed the training
    data pipeline instead of skill files.
  - **What remains:** System prompt carries Rich's 6 architectural patterns + evaluation
    criteria + output format. This is agent identity, not domain knowledge.
  - See `fine-tuned-architect-agent-strategy.md` for the full plan.
  - Phase 0 (immediate): system prompt + Claude API/Gemini. No skills. Validates
    Player-Coach loop mechanics on FinProxy before fine-tuning exists.
- **Knowledge retrieval (revised 5 April 2026):**
  - Graphiti: reads ADRs/patterns from `["appmilla-fleet", "{project_id}"]`, writes new ADRs/patterns to project scope
  - Product docs: read directly from filesystem via `doc_reader` tool — NOT ingested into Graphiti
  - Architecture principles: direct YAML loading into context (Phase 1). Graduate to ChromaDB only when library exceeds ~300 principles.
  - Per-project knowledge graph via Graphiti `group_id` scoping (DA9, DA12)
  - ⚠️ Graphiti is NOT used as a document store (see `skills-and-knowledge-rag-decisions.md` Graphiti Limitation Warning)
  - Intermediate artefacts stay in conversation history — only approved output written to Graphiti
- Graphiti integration: reads from `["appmilla-fleet", "{project_id}"]`, writes new ADRs/patterns to project scope
- Key files:
  - `src/architect_agent/agent.py` — `create_deep_agent()` setup
  - `src/architect_agent/manifest.py` — AgentManifest definition (imports from nats-core)
  - `src/architect_agent/criteria.py` — 6 weighted evaluation criteria
  - `src/architect_agent/tools/graphiti_tools.py` — Graphiti Python client, queries both fleet + project scope
  - `src/architect_agent/tools/doc_reader.py` — product doc reader (filesystem, NOT Graphiti)
  - `src/architect_agent/prompts/player_prompts.py` — architecture generation (system prompt + Rich's patterns)
  - `src/architect_agent/prompts/coach_prompts.py` — evaluation (criteria + scoring guides)
  - `src/architect_agent/output/formatter.py` — conversation starter markdown
  - `agent.py` — CLI entrypoint for standalone testing
- Test: `python agent.py --docs /path/to/finproxy-docs --scope "Phase 1: MoneyHub integration"`

**B4. architect-agent-mcp server**
- Repo: `guardkit/architect-agent-mcp`
- Build after B3 has a working CLI
- Delivers: MCP server that imports architect-agent, auto-generates tools from manifest
- Key files:
  - `src/architect_agent_mcp/server.py` — MCP tool definitions, async lifecycle
  - `pyproject.toml` — depends on `architect-agent` and `nats-core` packages
- Connect: Add to Claude Desktop MCP config
- Test: Invoke from Claude Desktop conversation

### PHASE C — FinProxy (uses Architect Agent)

**C1. Run Architect Agent on FinProxy docs**
- Input: 14 docs from `finproxy-docs/` directory, read directly via `doc_reader` tool (NOT via Graphiti)
- Graphiti context: Agent queries `["appmilla-fleet", "finproxy"]` for fleet patterns + project ADRs only
- Scope brief: "Phase 1 — MoneyHub Open Banking integration, user authentication (Keycloak), transaction data retrieval and storage, C#/.NET + Python service boundary. Out of scope: mobile app, voice agent, B2B white-label, Neo4j knowledge graph, A2A protocol."
- Output: Conversation starter document for `/system-arch`
- Review: Compare against what Rich would have written manually

**C2. Run `/system-arch` for FinProxy Phase 1**
- Input: Conversation starter from C1
- Output: Implementation-ready architecture (C4 diagrams, ADRs, component boundaries)

**C3. Run `/system-design` for FinProxy Phase 1**
- Detailed design: MoneyHub API integration (auth, consent, data retrieval), Open Banking bounded context interface contract, analytical data store schema (PostgreSQL/TimescaleDB), C# → NATS → Python communication

**C4. Run `/feature-spec` → `/feature-plan` → AutoBuild**
- First features: MoneyHub auth flow, transaction data retrieval, donor/attorney user model in Keycloak

**C5. Write FinProxy ADRs and patterns back to Graphiti** ← NEW
- New ADRs from Phase 1 architecture → `group_id="finproxy"`
- Cross-project pattern observations → `group_id="appmilla-fleet"`
- This is the compounding flywheel: FinProxy experience improves the next project

### PHASE D — Fleet Integration (when NATS infrastructure ready)

- [ ] Add NATS adapter to architect-agent (subscribes to dispatch topics, publishes registration)
- [ ] Test fleet registration: agent starts → publishes manifest → Jarvis discovers it
- [ ] Test intent routing: "Architect this project" → Jarvis → Architect Agent
- [ ] Test direct tool call: `agents.architect-agent.tools.evaluate_adr` via NATS
- [ ] Evaluate Coach effectiveness — what did it catch, what did it miss?

### PHASE E — FinProxy Handover (when project completes)

- [ ] Run `graphiti-export.sh export --project finproxy`
- [ ] Verify export contains only FinProxy-scoped knowledge (no `appmilla-fleet` leak)
- [ ] Package export with import instructions for client's infrastructure
- [ ] Client stands up FalkorDB + Graphiti on their AWS instance
- [ ] Client runs `import.py` to replay episodes into their graph

### PHASE F — Fine-Tuned Architect Agent (when book extraction + dataset factory ready)

> **Strategic direction (5 April 2026):** Apply the GCSE tutor two-layer pattern
> (fine-tuning for behaviour + RAG for knowledge) to the Architect Agent. Fine-tune
> Gemma 4 31B on architectural judgment using the agentic dataset factory. See
> `architect-agent/docs/research/ideas/fine-tuned-architect-agent-strategy.md`
> for full research and phased adoption plan.

- [ ] Benchmark Gemma 4 31B inference on GB10 via vLLM (wait for ecosystem to stabilise)
- [ ] Complete Phase 1 book extraction (Ousterhout, Ford, Farley) → curated principles
- [ ] Configure `domains/architecture-judgment/` in agentic-dataset-factory
- [ ] 2 production runs → ~5,000 training examples
- [ ] Fine-tune Gemma 4 31B with Unsloth QLoRA on GB10
- [ ] A/B test: fine-tuned vs base model on FinProxy docs (Rich as human benchmark)
- [ ] Deploy fine-tuned model via vLLM → Architect Agent reasoning engine
- [ ] First autonomous architecture session on FinProxy

### DEFERRED (park until relevant)

- [ ] Dashboard spec — useful but not blocking until agents produce data worth displaying
- [ ] GCSE Tutor fleet decision — recommend standalone with NATS adapter
- [ ] API cost tracking — add once multiple agents running with real usage data
- [ ] Telegram adapter spec — Phase 10
- [ ] Agent-to-agent trust / per-agent NATS permissions — defer until fleet larger
- [ ] Template harvest from Architect Agent → `langchain-deepagents-adversarial` template
- [ ] Review task: add orchestrator + python-library + nats-asyncio-service as built-in GuardKit templates
- [ ] Transfer YouTube conversation starters (`bash transfer-starters.sh`)
- [ ] Monitoring dashboard spec
- [ ] End-to-end testing strategy (consider NATS message replay)
- [ ] Error handling / dead letter queue strategy (pull into nats-core spec)
- [ ] Migration path from current workflow to Jarvis ("Jarvis lite" = router + GP agent)

---

## File Reference — What Lives Where

### nats-core (shared contract layer)
```
nats-core/
├── docs/design/
│   ├── contracts/
│   │   └── agent-manifest-contract.md          ← NEW: AgentManifest schema
│   ├── decisions/
│   │   ├── ADR-001 through ADR-004             ← existing
│   │   └── ADR-005-two-level-capability-registry.md  ← NEW
│   └── specs/
│       └── nats-core-system-spec.md            ← UPDATE: merge addendum + add tool topics
└── src/nats_core/
    ├── manifest.py                             ← NEW: AgentManifest, ToolCapability, IntentCapability
    ├── config.py                               ← NEW: AgentConfig, ModelConfig, GraphitiConfig, NATSConfig
    ├── events/fleet.py                         ← NEW: registration, heartbeat, deregistration
    └── topics.py                               ← UPDATE: add Topics.Agents.Tools
```

### architect-agent (core logic)
```
architect-agent/
├── docs/
│   ├── design/contracts/
│   │   └── architect-agent-manifest.md         ← NEW: specific manifest for this agent
│   └── research/ideas/
│       ├── architect-agent-vision.md           ← existing (no changes needed)
│       ├── conversation-starter-skills-and-knowledge-rag.md  ← RESOLVED: 10 questions answered
│       └── skills-and-knowledge-rag-decisions.md             ← NEW: session decisions + Graphiti limitation warning
├── src/architect_agent/
│   ├── agent.py, criteria.py, manifest.py
│   ├── tools/graphiti_tools.py                 ← queries ["appmilla-fleet", "{project_id}"]
│   ├── tools/doc_reader.py                     ← reads product docs from filesystem (NOT Graphiti)
│   ├── prompts/player_prompts.py               ← architecture generation (system prompt + Rich's patterns)
│   ├── prompts/coach_prompts.py                ← evaluation (criteria + scoring guides)
│   ├── output/
│   └── ...
├── agent.py                                    ← CLI entrypoint
└── pyproject.toml
```

### architect-agent-mcp (MCP adapter)
```
architect-agent-mcp/
├── docs/
│   ├── contracts/                              ← references nats-core contract
│   ├── decisions/
│   │   └── ADR-001-manifest-derived-adapters.md  ← NEW
│   └── design/specs/
│       └── architect-agent-mcp-spec.md         ← NEW
├── src/architect_agent_mcp/
│   └── server.py                               ← MCP tool definitions
└── pyproject.toml
```

### nats-infrastructure (deployment)
```
nats-infrastructure/
├── docs/design/specs/
│   └── nats-infrastructure-system-spec.md      ← UPDATE: merge addendum
└── ...                                         ← no other changes needed for Phase B
```

### guardkit (Graphiti integration — existing repo)
```
guardkit/
├── scripts/
│   ├── graphiti-backup.sh                      ← existing: full FalkorDB backup/restore
│   ├── graphiti-seed-fleet.py                  ← NEW: seed D1-D21, patterns, ADRs into appmilla-fleet
│   ├── graphiti-export.sh                      ← NEW: selective project export (shell wrapper)
│   ├── graphiti-export.py                      ← NEW: export implementation (episodes, entities, facts)
│   ├── vllm-embed.sh                           ← existing: nomic-embed-text-v1.5 on port 8001
│   ├── vllm-graphiti.sh                        ← existing: Qwen2.5-14B on port 8000
│   └── graphiti-validation/                    ← existing: connection tests
└── docs/research/graphiti-refinement/
    ├── FEAT-GR-000 through FEAT-GR-006         ← existing: project namespace, seeding, context
    └── FEAT-GR-007-fleet-knowledge-and-export.md  ← NEW: fleet knowledge, export/import design
```

---

## Graphiti Knowledge Scoping

| Scope | Group ID | Contents | Who Writes | Exportable? |
|-------|----------|----------|------------|-------------|
| GuardKit system | `guardkit` | How commands work, slash command patterns | `guardkit graphiti seed` | NO |
| Fleet architecture | `appmilla-fleet` | D1-D21 decisions, 6 patterns, ADRs, cross-project lessons | `graphiti-seed-fleet.py`, Architect Agent | NO |
| FinProxy project | `finproxy` | FinProxy ADRs, tech stack, architecture decisions, domain knowledge | Architect Agent, PO Agent | YES → client |
| FinProxy sub-scope | `finproxy__feature_specs` | BDD scenarios, feature specs | Feature spec seeding | YES → client |
| GCSE Tutor project | `gcse-tutor` | Tutor ADRs, subject config, pedagogy decisions | Future | YES (if needed) |

**Agent query pattern:** `group_ids=["appmilla-fleet", "{project_id}"]`
- Fleet patterns available to all projects (compounding value)
- Project-specific context isolated per project
- Client export only includes project-scoped data

---

## Getting Started — First Session Commands

### Session 0: Seed Graphiti fleet knowledge (guardkit repo)
```bash
cd ~/Projects/appmilla_github/guardkit
# Create and run the fleet seeding script
python scripts/graphiti-seed-fleet.py
# Verify: search for a seeded decision
# Via Graphiti MCP in Claude Desktop: "search for resolved decision D4 NATS"
```

### Session 1: Merge addendums (Claude Code, any repo)
```bash
# In nats-core
cd ~/Projects/appmilla_github/nats-core
# Open Claude Code, paste merge instructions from addendum, execute

# In nats-infrastructure
cd ~/Projects/appmilla_github/nats-infrastructure
# Same process
```

### Session 2: Build nats-core (Claude Code)
```bash
cd ~/Projects/appmilla_github/nats-core
# Ensure guardkit init has been run
guardkit feature-spec  # generates BDD scenarios from merged spec
guardkit feature-plan  # decomposes into tasks
# Review and approve
guardkit feature-build FEAT-XXX  # AutoBuild
```

### Session 3: Build architect-agent (Claude Code)
```bash
cd ~/Projects/appmilla_github/architect-agent
guardkit init  # from weighted-evaluation template
# Then manual build or /feature-spec → /feature-plan
# Test standalone:
PYTHONPATH=src uv run python agent.py --docs ~/Projects/appmilla_github/finproxy-docs --scope "Phase 1"
```

### Session 4: Build architect-agent-mcp (Claude Code)
```bash
cd ~/Projects/appmilla_github/architect-agent-mcp
# Build MCP server (thin wrapper)
# Add to Claude Desktop MCP config:
# ~/.config/claude/claude_desktop_config.json
```

### Session 5: Run on FinProxy (Claude Desktop)
```
# In Claude Desktop with architect-agent MCP connected:
"Run the architect agent on the FinProxy docs, scope to Phase 1 MoneyHub integration"
# Review output → /system-arch → /system-design → build
```

---

## Source Documents Referenced

| Document | Location |
|----------|----------|
| Gap analysis | `forge/docs/research/ideas/conversation-starter-gap-analysis.md` |
| Fleet master index | `forge/docs/research/ideas/fleet-master-index.md` |
| Big picture vision | `forge/docs/research/ideas/big-picture-vision-and-durability.md` |
| Architect agent vision | `specialist-agent/docs/research/ideas/architect-agent-vision.md` |
| Skills & RAG decisions | `specialist-agent/docs/research/ideas/skills-and-knowledge-rag-decisions.md` |
| Skills & RAG conversation starter | `specialist-agent/docs/research/ideas/conversation-starter-skills-and-knowledge-rag.md` |
| Architecture knowledge library | `specialist-agent/docs/research/ideas/architecture-knowledge-library.md` |
| Book extraction pipeline | `specialist-agent/docs/research/ideas/architecture-book-extraction-pipeline.md` |
| Fine-tuned architect strategy | `specialist-agent/docs/research/ideas/fine-tuned-architect-agent-strategy.md` |
| Three-layer benchmark | `specialist-agent/docs/research/ideas/three-layer-architecture-benchmark.md` |
| Assumption defence & checkpoints | `specialist-agent/docs/research/ideas/assumption-defence-and-checkpoints.md` |
| C4 diagram index | `specialist-agent/docs/research/ideas/c4-diagram-index.md` |
| Critical review | `specialist-agent/docs/research/ideas/critical-review-5-april-2026.md` |
| Phase 0 build plan | `specialist-agent/docs/research/ideas/phase-0-build-plan.md` |
| FinProxy foundations | `finproxy-docs/FinProxy-LPA-Foundations-and-AI-Vision.md` |
| FinProxy architecture | `finproxy-docs/FinProxy-Architecture-Exploration.md` |
| FinProxy tech stack | `finproxy-docs/FinProxy-Tech-Stack.md` |
| Structured uncertainty | Project knowledge: `structured-uncertainty-handling.md` |
| Jarvis vision | `jarvis/docs/research/ideas/jarvis-vision.md` |
| nats-core system spec | `nats-core/docs/design/specs/nats-core-system-spec.md` |
| CAN bus ADR | `nats-core/docs/design/decisions/ADR-004-dynamic-fleet-registration.md` |
| Claude Code primitives | `YouTube Channel/insights/12 Primitives of Agent Architecture...md` |
| DeepAgents / Gemini 3 | `YouTube Channel/insights/Gemini 3 First Look...md` |
| Graphiti fleet + export spec | `guardkit/docs/research/graphiti-refinement/FEAT-GR-007-fleet-knowledge-and-export.md` |
| Graphiti refinement features | `guardkit/docs/research/graphiti-refinement/README.md` (FEAT-GR-000 through GR-007) |
| Graphiti backup script | `guardkit/scripts/graphiti-backup.sh` |
| Embedding model script | `guardkit/scripts/vllm-embed.sh` (nomic-embed-text-v1.5, 768-dim) |
| Graphiti LLM script | `guardkit/scripts/vllm-graphiti.sh` (Qwen2.5-14B-Instruct-FP8) |

---

*Created: 4 April 2026 | Architect Agent + FinProxy build planning session*
*Updated: 5 April 2026 | Graphiti decisions: fleet knowledge, export/import, embedding confirmation*
*SUPERSEDED: 12 April 2026 | See specialist-agent phase build plans and lpa-platform/docs/buildplan.md*
