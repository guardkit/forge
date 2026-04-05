---
id: TASK-768F
title: Update fleet master index for April 2026 decisions
status: completed
created: 2026-04-04T00:00:00Z
updated: 2026-04-05T00:00:00Z
completed: 2026-04-05T00:00:00Z
priority: high
tags: [documentation, fleet, architecture, architect-agent]
complexity: 4
task_type: documentation
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Update fleet master index to reflect April 2026 ideation session decisions

## Description

Edit `docs/research/ideas/fleet-master-index.md` to incorporate all decisions from the April 2026 ideation session. This includes adding the new `architect-agent-mcp` adapter repo, updating existing repo map entries, adding fleet-wide decisions D14-D21, updating the build sequence to bring Phase 8 forward, and adding new related documents.

## Changes Required

### 1. Add `architect-agent-mcp` to the Repository Map

In the INFRASTRUCTURE section of the repo map (after `nats-infrastructure`), add a new ADAPTERS section:
```
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
```

### 2. Update the Infrastructure Components table

Add a new section heading "Adapter Components" after the Infrastructure Components table and add:

| Component | Repo | Type | Purpose |
|-----------|------|------|---------|
| **Architect Agent MCP** | `architect-agent-mcp` | MCP server | Claude Desktop access to Architect Agent. Derives tools from AgentManifest. Zero business logic — pure adapter. |

### 3. Update the nats-core repo map entry

Update the nats-core entry in the Repository Map to add the contracts directory and ADR-004:
```
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
```

Note: change "5 features" to "6 features" in the comment — Feature 6 (Fleet Registration) was merged from the addendum.

### 4. Update the architect-agent repo map entry

Update to include the contracts directory:
```
├── architect-agent           ← Product docs → system architecture → /system-arch input
│   └── docs/
│       ├── design/contracts/
│       │   └── architect-agent-manifest.md    ← Specific manifest: 3 intents, 5 tools, 6 eval criteria
│       └── research/ideas/
│           └── architect-agent-vision.md      ← Architecture generation agent vision
```

### 5. Add new fleet-wide decisions D14-D21

Add these to the Resolved Decisions table (after the existing D13):

| D14 | Containerisation | Phase 2 — containers for lifecycle, concurrency, fleet scaling |
| D15 | Agent discovery | Dynamic CAN bus registration via NATS fleet.register (ADR-004) |
| D16 | Agent logic separate from adapters | Same core logic, different entry points (MCP now, NATS later). Adapter pattern. |
| D17 | AgentManifest as single source of truth | Both MCP tools and NATS registration derived from one manifest. Two-level registry: intents (routing) + tools (direct interaction). |
| D18 | Tool risk classification | Every tool declares read_only, mutating, or destructive from day one |
| D19 | Trust tiers | core (infrastructure), specialist (fleet), extension (future plugins) |
| D20 | Direct tool topics | `agents.{agent_id}.tools.{tool_name}` for agent-to-agent calls bypassing Jarvis |
| D21 | Graphiti for architectural memory | Working MCP setup and proven Python client retained for Architect Agent |

### 6. Update the Build Sequence — Phase 8 (Architect Agent)

Replace the existing Phase 8 content with:
```
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
```

Add a note at the top of the Build Sequence section:
```
**Priority change (April 2026):** Phase 8 (Architect Agent) has been brought forward
to run concurrently with Phases 1-2. FinProxy needs architecture work now, and the
Architect Agent is designed to work standalone (via MCP) before NATS infrastructure
exists. The AgentManifest contract ensures zero refactoring when NATS arrives later.
See `architect-agent-finproxy-build-plan.md` for the detailed build plan.
```

### 7. Add the build plan to Related Documents

Add to the Related Documents table at the bottom:

| Architect Agent + FinProxy Build Plan | guardkitfactory/docs/research/ideas/architect-agent-finproxy-build-plan.md | Master TODO, all decisions, build sequence, file references |
| Agent Manifest Contract | nats-core/docs/design/contracts/agent-manifest-contract.md | Shared schema for AgentManifest, ToolCapability, IntentCapability |

## Acceptance Criteria

- [x] `architect-agent-mcp` added to Repository Map under new ADAPTERS section
- [x] Adapter Components table added after Infrastructure Components table
- [x] nats-core repo map entry updated with contracts dir and ADR-004, "6 features"
- [x] architect-agent repo map entry updated with contracts directory
- [x] Decisions D14-D21 added to Resolved Decisions table
- [x] Phase 8 content replaced with updated version (brought forward)
- [x] Priority change note added at top of Build Sequence section
- [x] Two new entries added to Related Documents table
- [ ] Committed with message: "Update fleet master index: add architect-agent-mcp, new decisions D14-D21, bring forward Phase 8 for FinProxy"

## Test Requirements

- [x] Verify fleet-master-index.md renders correctly (valid markdown)
- [x] Verify all internal cross-references are consistent
- [x] Verify no existing content was accidentally removed

## Implementation Notes

Target file: `docs/research/ideas/fleet-master-index.md`
Commit message: "Update fleet master index: add architect-agent-mcp, new decisions D14-D21, bring forward Phase 8 for FinProxy"
