---
id: TASK-NFI-001
title: 'Extend forge.yaml config: fleet + pipeline + permissions sections'
task_type: declarative
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 1
implementation_mode: direct
complexity: 3
dependencies: []
tags:
- config
- pydantic
- declarative
- nats
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
  started_at: '2026-04-25T13:56:04.217327'
  last_updated: '2026-04-25T13:56:33.972524'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T13:56:04.217327'
    player_summary: 'Direct mode SDK invocation completed (git-detected: 0 modified,
      16 created)'
    player_success: true
    coach_success: true
---

# Task: Extend forge.yaml config: fleet + pipeline + permissions sections

## Description

Add the `fleet`, `pipeline`, and `permissions` sections to the existing
`ForgeConfig` Pydantic schema. These sections drive the heartbeat cadence, the
stale-heartbeat threshold, the build-queue subject, and the filesystem
allowlist used by the pipeline consumer to refuse out-of-scope builds.

Implements the declarative surface referenced by assumptions ASSUM-001
(heartbeat 30s), ASSUM-002 (stale 90s), ASSUM-005 (progress ≥ 60s), and
ASSUM-004 (intent confidence 0.7).

## Schema additions

```python
class FleetConfig(BaseModel):
    heartbeat_interval_seconds: int = 30        # ASSUM-001
    stale_heartbeat_seconds: int = 90           # ASSUM-002
    cache_ttl_seconds: int = 30                 # ASSUM-003
    intent_min_confidence: float = 0.7          # ASSUM-004

class PipelineConfig(BaseModel):
    progress_interval_seconds: int = 60         # ASSUM-005
    build_queue_subject: str = "pipeline.build-queued.>"
    approved_originators: list[str] = [
        "terminal", "voice-reachy", "telegram", "slack", "dashboard", "cli-wrapper"
    ]

class FilesystemPermissions(BaseModel):
    allowlist: list[Path]                       # Absolute paths only

class PermissionsConfig(BaseModel):
    filesystem: FilesystemPermissions

class ForgeConfig(BaseModel):
    fleet: FleetConfig = FleetConfig()
    pipeline: PipelineConfig = PipelineConfig()
    permissions: PermissionsConfig
```

## Acceptance Criteria

- [ ] `FleetConfig`, `PipelineConfig`, `PermissionsConfig`, `FilesystemPermissions`
      Pydantic models added to `src/forge/config/models.py`
- [ ] Defaults match ASSUM-001..005 exactly (30/90/30/0.7/60)
- [ ] `ForgeConfig.fleet` and `ForgeConfig.pipeline` are optional with defaults
- [ ] `ForgeConfig.permissions.filesystem.allowlist` is required (no default — must be explicit)
- [ ] `FilesystemPermissions.allowlist` rejects relative paths (Pydantic validator)
- [ ] Round-trip test: YAML → `ForgeConfig.model_validate` → back to dict preserves field values
- [ ] Missing `permissions.filesystem.allowlist` raises `ValidationError` with a clear message

## Seam Note

This task is a **producer** for two Integration Contracts (§4 in IMPLEMENTATION-GUIDE.md):
- `ForgeConfig.fleet` → consumers TASK-NFI-004, TASK-NFI-005
- `ForgeConfig.permissions.filesystem.allowlist` → consumer TASK-NFI-007

Consumers add their own seam tests.

## Implementation Notes

- Pydantic v2 `BaseModel` + `Field(default=...)` idiom
- Use `Path` type for allowlist entries; validator rejects non-absolute
- Do NOT load the YAML here — just the model. YAML loader already exists in `forge.config.loader`
