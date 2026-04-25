---
id: TASK-NFI-002
title: Define FORGE_MANIFEST constant builder
task_type: declarative
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 1
implementation_mode: direct
complexity: 2
dependencies: []
tags:
- manifest
- nats-core
- declarative
- fleet
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-24T20:23:09.480837'
  last_updated: '2026-04-24T20:26:44.745561'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-24T20:23:09.480837'
    player_summary: "Created src/forge/fleet/ subpackage with a pure declarative manifest\
      \ module. FORGE_MANIFEST is a module-level nats_core.manifest.AgentManifest\
      \ constructed verbatim from docs/design/contracts/API-nats-fleet-lifecycle.md\
      \ \xA72.1: agent_id='forge', name='Forge', version='0.1.0', template='deepagents-pipeline-orchestrator',\
      \ trust_tier='core', status='ready', max_concurrent=1 (ADR-SP-012). Three IntentCapability\
      \ entries (build.* / pipeline.* / feature.*) and five ToolCapability entries\
      \ (forge_greenfield"
    player_success: true
    coach_success: true
---

# Task: Define FORGE_MANIFEST constant builder

## Description

Create `src/forge/fleet/manifest.py` exporting `FORGE_MANIFEST` — the
`nats_core.manifest.AgentManifest` describing Forge's intents, tools,
trust tier, and permissions. This is the payload published on startup
to `fleet.register` and stored in the `agent-registry` KV bucket.

The exact manifest content is specified in
`docs/design/contracts/API-nats-fleet-lifecycle.md §2.1` — copy it
verbatim. This is a declarative constant, not a runtime-computed value.

## Acceptance Criteria

- [ ] `src/forge/fleet/manifest.py` exports a module-level `FORGE_MANIFEST` constant
- [ ] Type is `nats_core.manifest.AgentManifest` (imported, not redeclared)
- [ ] `agent_id == "forge"`, `trust_tier == "core"`, `max_concurrent == 1`
- [ ] Three `IntentCapability` entries (build.* / pipeline.* / feature.*) match §2.1 verbatim
- [ ] Five `ToolCapability` entries (forge_greenfield, forge_feature, forge_review_fix, forge_status, forge_cancel) match §2.1 verbatim
- [ ] `required_permissions` matches §2.1 verbatim
- [ ] **Secret-free**: `FORGE_MANIFEST.model_dump_json()` contains none of `"api_key"`, `"token"`, `"password"`, `"secret"`, `"credential"` (case-insensitive) — asserted by unit test
- [ ] Import path `from forge.fleet.manifest import FORGE_MANIFEST` resolves

## Seam Note

This task is a **producer** for Integration Contract FORGE_MANIFEST (§4):
- Consumer: TASK-NFI-004 (fleet_publisher.register_on_boot)
- Format: `nats_core.manifest.AgentManifest` (immutable module-level constant)

## Implementation Notes

- Pure declarative — no runtime I/O, no env reads
- Version string `"0.1.0"` sourced from `forge.__version__` or hardcoded if not yet available
- The secret-free unit test is cheap; include it here rather than deferring to TASK-NFI-010
