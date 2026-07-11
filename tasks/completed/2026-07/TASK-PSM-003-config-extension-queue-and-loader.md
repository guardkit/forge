---
id: TASK-PSM-003
title: "Config extension \u2014 QueueConfig and load_config()"
task_type: declarative
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 1
implementation_mode: direct
complexity: 3
estimated_minutes: 45
status: completed
dependencies: []
tags:
- lifecycle
- config
- pydantic
- declarative
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T12:54:50.524785'
  last_updated: '2026-04-27T12:59:27.587442'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T12:54:50.524785'
    player_summary: 'Added QueueConfig (Pydantic v2, extra=''forbid'') to src/forge/config/models.py
      with the four required fields and ge=1 validators on all integer fields, then
      wired queue: QueueConfig = Field(default_factory=QueueConfig) into ForgeConfig.
      Created src/forge/config/loader.py exposing load_config(path: Path) -> ForgeConfig
      that uses yaml.safe_load on the file''s UTF-8 contents (defaulting an empty
      document to {}) and passes the result straight to ForgeConfig.model_validate,
      allowing pydantic.Validation'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-003: feature-rollup completed"
---

# Task: Config extension — QueueConfig and load_config()

## Description

Extend `src/forge/config/models.py` (per gap-context §2.1, MUST be in place,
not a parallel module) with:

1. A new `QueueConfig` Pydantic v2 sub-model:
   - `default_max_turns: int = Field(5, ge=1)` — minimum 1 (ASSUM-001)
   - `default_sdk_timeout_seconds: int = 1800`
   - `default_history_limit: int = 50`
   - `repo_allowlist: list[Path] = []` — paths matched by `forge queue --repo`
2. A `queue: QueueConfig = QueueConfig()` field on the `ForgeConfig` root
   model.
3. A new `src/forge/config/loader.py` module exporting
   `load_config(path: Path) -> ForgeConfig` — reads YAML, validates via
   Pydantic, returns the parsed root model.

The Pydantic validator on `default_max_turns` (the `ge=1` constraint) gives
the Group B "turn budget < 1 rejected" scenario its rejection branch
automatically — no extra code in the CLI is required for that scenario.

## Acceptance Criteria

- [ ] `QueueConfig` exists in `forge/config/models.py` with the four fields
      and Pydantic v2 `Field` validators
- [ ] `ForgeConfig` has `queue: QueueConfig = QueueConfig()` (default factory
      so loading a `forge.yaml` without a `queue:` block works)
- [ ] `load_config(path: Path) -> ForgeConfig` reads YAML via `yaml.safe_load`
      and validates via Pydantic `model_validate`
- [ ] `load_config` raises `pydantic.ValidationError` (not a wrapped exception)
      on malformed config so callers can catch and format errors
- [ ] Boundary test: `default_max_turns: 0` in YAML raises `ValidationError`
- [ ] Boundary test: `default_max_turns: 1` accepted (ASSUM-001 minimum)
- [ ] Boundary test: missing `queue:` block — `ForgeConfig.queue` populated
      from defaults
- [ ] Boundary test: `repo_allowlist: ["/home/rich/Projects"]` parses to
      `list[Path]`
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
# In forge/config/models.py (extend the existing module — do NOT add
# a parallel forge.config.QueueConfig)
from pathlib import Path
from pydantic import BaseModel, Field


class QueueConfig(BaseModel):
    default_max_turns: int = Field(5, ge=1)
    default_sdk_timeout_seconds: int = Field(1800, ge=1)
    default_history_limit: int = Field(50, ge=1)
    repo_allowlist: list[Path] = Field(default_factory=list)


class ForgeConfig(BaseModel):
    # ... existing fields ...
    queue: QueueConfig = Field(default_factory=QueueConfig)
```

```python
# forge/config/loader.py (new file)
from pathlib import Path

import yaml

from forge.config.models import ForgeConfig


def load_config(path: Path) -> ForgeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ForgeConfig.model_validate(raw)
```

## Producer of integration contracts

- **CONFIG_LOADER** — consumed by TASK-PSM-008, TASK-PSM-009, TASK-PSM-010,
  TASK-PSM-011. See IMPLEMENTATION-GUIDE.md §4.

## Coach Validation

- `QueueConfig` is defined in `forge/config/models.py`, NOT a new package
- `load_config` lives in `forge/config/loader.py`
- Pydantic v2 idioms used (`Field(...)`, `model_validate`)
- `default_max_turns >= 1` enforced via `Field(ge=1)`
- Lint/format checks pass
