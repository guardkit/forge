---
id: TASK-GCI-002
title: Define GitOpResult, PRResult, and progress event DTOs
task_type: declarative
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-GCI0
feature_id: FEAT-FORGE-005
wave: 1
implementation_mode: direct
complexity: 3
dependencies: []
tags:
- pydantic
- declarative
- schemas
- git
- gh
- nats
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T08:50:31.048967'
  last_updated: '2026-04-26T08:56:01.017816'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T08:50:31.048967'
    player_summary: 'Created the three Pydantic v2 DTOs exactly as specified in the
      task brief. (1) src/forge/adapters/git/models.py defines GitOpResult (status:
      Literal[''success'',''failed''], operation, optional sha/worktree_path/stderr
      defaulting to None, required exit_code) and PRResult (status: Literal[''success'',''failed''],
      optional pr_url/pr_number/error_code/stderr defaulting to None). The PRResult
      docstring AND the error_code Field description both call out ''missing_credentials''
      as a known value, anchored to the'
    player_success: true
    coach_success: true
---

# Task: Define GitOpResult, PRResult, and progress event DTOs

## Description

Add the Pydantic models the git/gh adapters and the NATS progress-stream
subscriber return. These are the shared shapes consumed by TASK-GCI-006
(git), TASK-GCI-007 (gh), TASK-GCI-005 (subscriber), and ultimately the tool
wrappers in TASK-GCI-009 / TASK-GCI-010.

Per `docs/design/contracts/API-subprocess.md` §4 (git/gh adapter return
contract — never raises past the adapter boundary, ADR-ARCH-025).

## Schema additions

```python
# src/forge/adapters/git/models.py
from typing import Literal
from pydantic import BaseModel, Field


class GitOpResult(BaseModel):
    status: Literal["success", "failed"]
    operation: str                     # "prepare_worktree" | "commit_all" | "push" | "cleanup_worktree"
    sha: str | None = None             # commit ops only
    worktree_path: str | None = None   # prepare_worktree returns this
    stderr: str | None = None
    exit_code: int


class PRResult(BaseModel):
    status: Literal["success", "failed"]
    pr_url: str | None = None
    pr_number: int | None = None
    error_code: str | None = None      # e.g. "missing_credentials"
    stderr: str | None = None
```

```python
# src/forge/adapters/guardkit/progress.py
from pydantic import BaseModel


class GuardKitProgressEvent(BaseModel):
    """Typed shape of a single pipeline.stage-complete.* NATS message,
    surfaced to `forge status` consumers and the live-progress view.

    Authoritative completion still flows through GuardKitResult; this is
    telemetry only — the missing/slow stream must never fail an invocation
    (Scenario "The authoritative result still returns when progress
    streaming is unavailable").
    """
    build_id: str
    subcommand: str
    stage_label: str
    seq: int                            # monotonic per-invocation
    coach_score: float | None = None
    artefact: str | None = None
    timestamp: str                      # ISO 8601
```

## Acceptance Criteria

- [ ] `GitOpResult` and `PRResult` in `src/forge/adapters/git/models.py`
- [ ] `GuardKitProgressEvent` in `src/forge/adapters/guardkit/progress.py`
- [ ] `status` fields are `Literal[...]` (no `Enum`)
- [ ] All optional fields explicitly default to `None`
- [ ] Re-export shims: `src/forge/adapters/git/__init__.py` re-exports
      `GitOpResult` and `PRResult`
- [ ] `PRResult.error_code` documents `"missing_credentials"` as a known value
      (Scenario "A pull-request creation without GitHub credentials returns a
      structured error")
- [ ] `model_dump_json()` round-trips through `model_validate_json()` for all
      three models
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Pydantic v2 — keep declarative, no validators or logic
- Re-export pattern matches `src/forge/config/__init__.py`
- `GuardKitProgressEvent.timestamp` is a `str` (ISO 8601), matching the
  nats-core convention used elsewhere in the project (no `datetime` field)
- Do **not** wire NATS, git, or gh subprocess here — only the schemas
