# Forge Ideas Docs Overhaul — Conversation Starter

## For: Updating all Forge research/ideas docs to reflect current project state
## Date: 12 April 2026
## Status: Ready for overhaul session
## Repo: `guardkit/forge` (`docs/research/ideas/`)
## Context: Multiple docs are 2-6 weeks out of date. The project has evolved significantly since the fleet-master-index and gap analysis were last written.

---

## Purpose

Guide an overhaul of the Forge ideas docs. These documents serve as the project's strategic memory — the fleet-master-index is the single source of truth for what exists, where it lives, and how it connects. If these docs are stale, new sessions start from wrong assumptions.

This conversation starter captures everything that's changed, identifies what each doc needs, and introduces two new patterns (build plans and history docs) that should be formalised.

---

## What's Changed Since Last Update

### Repo-Level Changes

| Change | Old State | New State | Impact on Forge Docs |
|--------|-----------|-----------|---------------------|
| `architect-agent` renamed to `specialist-agent` | Separate architect-agent repo | `guardkit/specialist-agent` with ADR-ARCH-010 | Fleet index, gap analysis, build sequence all reference wrong repo name |
| `ideation-agent` archived | Separate ideation-agent repo | Three ideas extracted to `specialist-agent/docs/research/ideas/ideation-role-extracted-ideas.md`. Repo can be archived. | Fleet index still lists it as active repo |
| `product-owner-agent` absorbed | Separate product-owner-agent repo | Role within specialist-agent unified harness | Fleet index still lists separate repo. `forge-pipeline-orchestrator-refresh.md` already captures this. |
| `architect-agent-mcp` superseded | Separate MCP adapter repo | MCP built into `specialist-agent serve --role X --transport stdio` (FEAT-014) | Fleet index still lists as active adapter repo |
| `lpa-platform` created | Did not exist | New repo for FinProxy .NET platform. Build plan + command history written. | Fleet index doesn't know about it |
| `agentic-dataset-factory` mature | Early stage | Production run (~2,500 examples), Docling validated, first fine-tune complete | Fleet index has minimal entry |

### Architecture-Level Changes

| Change | Old State | New State | Source Doc |
|--------|-----------|-----------|-----------|
| Unified harness pattern | Three separate agent repos | One codebase, many roles via `role.yaml` config (ADR-ARCH-008, 009) | `specialist-agent/docs/research/ideas/unified-agent-harness-conversation-starter.md` |
| Three-layer architecture | Two-layer (fine-tuning + RAG) | Layer 1: behaviour (fine-tuned weights), Layer 2: knowledge (per-role Graphiti), Layer 3: context (project docs) | `specialist-agent/docs/research/ideas/fine-tuned-architect-agent-strategy.md` |
| Phase G (Graphiti runtime) | Graphiti for seeding only | Per-role learning via `role:{role_id}` group IDs. Query tool + write-back + metrics. | `specialist-agent/docs/research/ideas/phaseG-scope.md` + `phaseG-build-plan.md` |
| Phase 1C (domain fidelity) | Not identified | 5 regressions: SOURCE_COLLAPSE, DOMAIN_DILUTION. Two new detection patterns. | `specialist-agent/docs/research/ideas/phase1c-domain-fidelity-scope.md` + `build-plan.md` |
| MCP deployment architecture | Separate repo per role | One binary, role-specific MCP servers, AgentManifest as source of truth | `specialist-agent/docs/research/ideas/mcp-deployment-architecture.md` |
| AWS Bedrock hosting | GB10 only | Bedrock Custom Model Import for production. ~$1.50-3.00/run. Scales to zero. | `specialist-agent/docs/research/ideas/unified-agent-harness-conversation-starter.md` (Bedrock section) |
| Landscape research | Not done | Devin, MetaGPT, ChatDev, CrewAI, LangGraph analysed. Five differentiators identified. | `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` |
| DDD Southwest positioning | Vague | Talk narrative, demo sequence, Solow Paradox framing | `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` |

### Validation Milestones

| Milestone | Date | Result |
|-----------|------|--------|
| Phase 0 FinProxy run | March 2026 | 0.75 score, 3 iterations, 93s, 1006 tests |
| Phase 1 FinProxy run | April 2026 | 0.93 score, 2 iterations, 162s |
| First fine-tune (GCSE tutor) | March 2026 | Gemma 4 31B, 1,736 examples, loss 2.45→0.50, 2h 5min |
| nats-core library | April 2026 | 98% test coverage, 6 features, all implemented |
| Docling pipeline | March 2026 | Standard + VLM modes validated on GB10 |

### New Resolved Decisions (D23+)

| # | Decision | Resolution |
|---|----------|-----------|
| D23 | Unified harness | One codebase, many roles. architect + product-owner + ideation. `specialist-agent` repo. (ADR-ARCH-008) |
| D24 | Role config pattern | YAML/markdown for values, Python code for structure. (ADR-ARCH-009) |
| D25 | Repo rename strategy | Two-stage: repo first (done), package rename in Phase 2. (ADR-ARCH-010) |
| D26 | Fine-tuning teaches behaviour, not facts | Two-layer: fine-tune for taste/judgment, RAG for knowledge. Independently updatable. |
| D27 | Graphiti for facts/relationships, not document storage | Validated limitation. doc_reader for content, Graphiti for entities and decisions. |
| D28 | Skills layer dropped | Superseded by fine-tuning strategy. System prompt carries patterns + criteria. |
| D29 | Three deployments | Architect (fine-tuned Bedrock), Product Owner (fine-tuned Bedrock), Ideation (base model). |
| D30 | GCSE tutor deploys to Bedrock first | Validates import pipeline, frees GB10 for development. |
| D31 | MCP adapter built into specialist-agent | `architect-agent-mcp` repo superseded. `specialist-agent serve --role X --transport stdio`. |
| D32 | Per-role knowledge compounding | `role:{role_id}` group IDs in Graphiti. Three-scope query: project → role → fleet. |

---

## Document-by-Document Update Plan

### 1. `fleet-master-index.md` — MAJOR REWRITE

**Current state:** Lists 8 agent repos (many now archived/absorbed), 11 build phases (wrong sequence), decisions D1-D22 (missing D23-D32).

**Required changes:**
- **Repository Map:** Remove `ideation-agent`, `product-owner-agent`, `architect-agent-mcp` as separate repos. Add `specialist-agent` (unified harness). Add `lpa-platform`. Add `agentic-dataset-factory` with current state.
- **Agent Fleet Summary:** Collapse to 3 specialist agent roles + Jarvis + GuardKit Factory + YouTube Planner. Remove "Template" column (all use the weighted-evaluation template now). Add "Model" column (fine-tuned vs base).
- **Resolved Decisions:** Add D23-D32.
- **Build Sequence:** Replace 11-phase sequence with current phase structure:
  ```
  specialist-agent:  Phase 0 ✅ → 1 ✅ → 1C → 1B → G → 2 → 3 → F
  nats-core:         ✅ Implemented (98% coverage)
  nats-infrastructure: ✅ Configured
  forge:             Blocked on specialist-agent Phase 3 + nats infra
  lpa-platform:      Blocked on dotnet exemplar + template-create
  ```
- **Templates:** Add status for `dotnet-fastendpoints` template (planned from exemplar). Mark orchestrator template review as pending.
- **Hardware Topology:** Update port allocations if changed. Note vLLM model changes (Qwen3-Coder-Next, Gemma 4 31B).

**Source of truth docs to reference:**
- `specialist-agent/docs/research/ideas/architect-agent-vision.md` (updated 12 April 2026)
- `specialist-agent/docs/architecture/ARCHITECTURE.md`
- `nats-core/docs/design/specs/nats-core-system-spec.md`
- `lpa-platform/docs/buildplan.md`

### 2. `big-picture-vision-and-durability.md` — MODERATE UPDATE

**Current state:** Mostly still valid (strategic vision, learning goals, durability analysis). The "Three Goals" framing is timeless.

**Required changes:**
- **Pipeline diagram:** Update to show specialist-agent (not separate repos).
- **War stories section:** Add Phase 0/1 FinProxy results, first fine-tune success, domain fidelity regressions as new war stories.
- **Proof points:** Update with current validation milestones.
- **"What's at risk" section:** Review whether Bedrock hosting changes the risk profile.
- **DDD Southwest reference:** Update with landscape positioning and talk narrative.

### 3. `conversation-starter-gap-analysis.md` — LIGHT UPDATE

**Current state:** Good structural overview of the problem statement and project context. Some details outdated.

**Required changes:**
- Update repo references (specialist-agent not architect-agent).
- Add fine-tuning and Graphiti runtime to the capability summary.
- Update the "what gaps still need addressing" section.

### 4. `forge-pipeline-orchestrator-refresh.md` — ALREADY CURRENT (v3, 11 April 2026)

**Current state:** Recently updated. Already reflects unified harness, nats-core implementation, NATS infrastructure readiness.

**Required changes:** Minimal — verify consistency with latest specialist-agent phase changes.

### 5. `architect-agent-finproxy-build-plan.md` — SUPERSEDED

**Current state:** Original build plan for the architect-agent before Phase 0. Now superseded by the phase-specific build plans in `specialist-agent/docs/research/ideas/`.

**Action:** Add a "SUPERSEDED" header pointing to the specialist-agent phase build plans. Don't delete — it's historical evidence of the build approach.

---

## New Patterns to Formalise

### Pattern 1: Build Plans (`build_plan.md` / `docs/buildplan.md`)

**What it is:** A document per phase or per repo that captures the full GuardKit command sequence with all `--context` flags. Includes prerequisites, expected outputs per step, and the feature dependency graph.

**Where it's proven:**
- `specialist-agent/docs/research/ideas/phase1-build-plan.md` — Phase 1 output quality
- `specialist-agent/docs/research/ideas/phase1b-unified-harness-build-plan.md` — Phase 1B unified harness
- `specialist-agent/docs/research/ideas/phase1c-domain-fidelity-build-plan.md` — Phase 1C domain fidelity
- `specialist-agent/docs/research/ideas/phaseG-build-plan.md` — Phase G Graphiti runtime
- `specialist-agent/docs/research/ideas/phase2-build-plan.md` — Phase 2 web search/DDD
- `specialist-agent/docs/research/ideas/phase3-build-plan.md` — Phase 3 NATS/MCP
- `specialist-agent/docs/research/ideas/phaseF-build-plan.md` — Phase F fine-tuning
- `lpa-platform/docs/buildplan.md` — FinProxy LPA platform (full command sequence)

**Why it matters:** The build plan is the bridge between scope docs (what to build) and the actual GuardKit commands (how to build). Without it, each session rediscovers the `--context` flags, prerequisite ordering, and validation steps. With it, Claude Code can be handed the build plan directly and execute the sequence.

**Structure:**
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

**Recommendation:** Add a "Build Plan Pattern" section to the fleet-master-index or create a separate `build-plan-pattern.md` doc that codifies this.

### Pattern 2: Command History (`command-history.md`)

**What it is:** A running log at the repo root that captures every GuardKit command run, its output, and any decisions made during the session. Pre-command setup documented at the top.

**Where it's proven:**
- `nats-core/command-history.md` — full /system-arch → /system-design → /feature-spec × 6 → build sequence
- `specialist-agent/command_history.md` — Phase 0 through Phase 1 commands
- `lpa-platform/command-history.md` — pre-command setup, planned sequence (not yet executed)

**Why it matters:** The command history is evidence of what was actually run, in what order, with what results. It's the ground truth that conversation starters and build plans are derived from.

### Pattern 3: Feature Spec History (`feature-spec-FEAT-XXX-history.md`)

**What it is:** A per-feature log at the repo root that captures the full `/feature-spec` session — Rich's responses to each acceptance group, assumptions resolved, and final outcome.

**Where it's proven:**
- `specialist-agent/feature-spec-FEAT-001-history.md` through `FEAT-007-history.md`
- `specialist-agent/feature-spec-domain-fidelity-history.md`
- `specialist-agent/feature-plan-fidelity-history.md`
- `specialist-agent/system-arch-history.md`
- `specialist-agent/system-design-history.md`
- `specialist-agent/system-plan-history.md`
- `nats-core/system-arch-history.md`

**Why it matters:** These documents reveal a critical pattern — **Rich almost always accepts defaults.** This validates that the GuardKit command prompts and defaults are well-calibrated. It also provides training signal for future automation: if the human accepts 95% of defaults, the system can learn when NOT to ask.

**Observable pattern from history docs:**
- Rich accepts default acceptance groups: almost always "A" (accept)
- Rich accepts default edge case expansions: almost always "Y"
- Rich accepts assumptions at face value: review required only for high-risk
- Rare interventions: only when the spec misses something domain-specific

---

## Suggested Overhaul Approach

### Session 1: Fleet Master Index Rewrite
1. Read current fleet-master-index.md
2. Read `specialist-agent/docs/research/ideas/architect-agent-vision.md` (updated vision)
3. Read this conversation starter
4. Rewrite the fleet-master-index from scratch, incorporating:
   - Updated repo map (specialist-agent, lpa-platform, archived repos)
   - Updated agent fleet summary (3 specialist roles, not 8 separate agents)
   - Updated decisions (D23-D32)
   - Updated build sequence (per-repo phase structure)
   - Build plan pattern documentation
   - History doc pattern documentation
5. Write the updated fleet-master-index.md

### Session 2: Supporting Docs Update
1. Update big-picture-vision-and-durability.md (war stories, proof points, pipeline diagram)
2. Update conversation-starter-gap-analysis.md (repo references, capabilities)
3. Add "SUPERSEDED" header to architect-agent-finproxy-build-plan.md
4. Verify forge-pipeline-orchestrator-refresh.md consistency

---

## Source Documents for the Overhaul Session

| Document | Path | What It Provides |
|----------|------|-----------------|
| **Updated vision** | `specialist-agent/docs/research/ideas/architect-agent-vision.md` | Current project state, three-layer architecture, phase sequence, three roles |
| **Unified harness** | `specialist-agent/docs/research/ideas/unified-agent-harness-conversation-starter.md` | One codebase many roles, GOAL.md pattern, Bedrock hosting |
| **MCP deployment** | `specialist-agent/docs/research/ideas/mcp-deployment-architecture.md` | One binary, role-specific MCP servers |
| **Phase G scope** | `specialist-agent/docs/research/ideas/phaseG-scope.md` | Per-role knowledge compounding |
| **Landscape research** | `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` | Devin, MetaGPT, ChatDev positioning, DDD Southwest framing |
| **Phase 1C scope** | `specialist-agent/docs/research/ideas/phase1c-domain-fidelity-scope.md` | Domain fidelity regressions and fixes |
| **LPA build plan** | `lpa-platform/docs/buildplan.md` | FinProxy platform command sequence |
| **nats-core spec** | `nats-core/docs/design/specs/nats-core-system-spec.md` | 6 features, 98% coverage |
| **This doc** | `forge/docs/research/ideas/forge-ideas-overhaul-conversation-starter.md` | What's changed, what each doc needs |

---

## Do-Not-Reopen

1. All decisions D1-D22 from the existing fleet-master-index remain valid
2. The "Three Goals" framing from big-picture-vision-and-durability.md is timeless
3. The forge-pipeline-orchestrator-refresh.md was recently updated (v3, 11 April) — don't redo

---

*Conversation starter: 12 April 2026*
*"Stale docs are worse than no docs — they teach wrong assumptions."*
