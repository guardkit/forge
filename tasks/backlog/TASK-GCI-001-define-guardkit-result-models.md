---
id: TASK-GCI-001
title: Define GuardKitResult and result Pydantic models
task_type: declarative
status: blocked
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
- guardkit
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T08:37:57.481292'
  last_updated: '2026-04-26T08:38:00.267890'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Not all acceptance criteria met:\n  \u2022 `GuardKitResult` and `GuardKitWarning`\
      \ defined in\n  \u2022 `status` is a `Literal[\"success\", \"failed\", \"timeout\"\
      ]` (no `Enum`)\n  \u2022 `artefacts`, `warnings` use `Field(default_factory=list)`\
      \ (no shared\n  \u2022 `coach_score`, `criterion_breakdown`, `detection_findings`,\
      \ `stderr`\n  \u2022 `model_dump_json()` round-trips through `model_validate_json()`\
      \ without\n  (2 more)"
    timestamp: '2026-04-26T08:37:57.481292'
    player_summary: '[RECOVERED via player_report] Original error: Unexpected error:
      Agent player received API error: authentication_failed'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Not all acceptance criteria met:\n  \u2022 `GuardKitResult` and `GuardKitWarning`\
      \ defined in\n  \u2022 `status` is a `Literal[\"success\", \"failed\", \"timeout\"\
      ]` (no `Enum`)\n  \u2022 `artefacts`, `warnings` use `Field(default_factory=list)`\
      \ (no shared\n  \u2022 `coach_score`, `criterion_breakdown`, `detection_findings`,\
      \ `stderr`\n  \u2022 `model_dump_json()` round-trips through `model_validate_json()`\
      \ without\n  (2 more)"
    timestamp: '2026-04-26T08:37:59.244352'
    player_summary: '[RECOVERED via player_report] Original error: Unexpected error:
      Agent player received API error: authentication_failed'
    player_success: true
    coach_success: true
  - turn: 3
    decision: feedback
    feedback: "- Not all acceptance criteria met:\n  \u2022 `GuardKitResult` and `GuardKitWarning`\
      \ defined in\n  \u2022 `status` is a `Literal[\"success\", \"failed\", \"timeout\"\
      ]` (no `Enum`)\n  \u2022 `artefacts`, `warnings` use `Field(default_factory=list)`\
      \ (no shared\n  \u2022 `coach_score`, `criterion_breakdown`, `detection_findings`,\
      \ `stderr`\n  \u2022 `model_dump_json()` round-trips through `model_validate_json()`\
      \ without\n  (2 more)"
    timestamp: '2026-04-26T08:37:59.765090'
    player_summary: '[RECOVERED via player_report] Original error: Unexpected error:
      Agent player received API error: authentication_failed'
    player_success: true
    coach_success: true
---

# Task: Define GuardKitResult and result Pydantic models

## Description

Add the `GuardKitResult` Pydantic model plus its supporting types as the
canonical shape every `forge.adapters.guardkit.run()` call returns. This is the
contract every downstream tool wrapper (TASK-GCI-009, TASK-GCI-010), the parser
(TASK-GCI-004), and the subprocess wrapper (TASK-GCI-008) consume.

Per `docs/design/contracts/API-subprocess.md` §3.4. Confirms ASSUM-003 (the
4 KB stdout-tail field) declaratively in the schema.

## Schema additions

```python
# src/forge/adapters/guardkit/models.py
from typing import Any, Literal
from pydantic import BaseModel, Field


class GuardKitWarning(BaseModel):
    code: str               # e.g. "context_manifest_missing", "context_manifest_cycle_detected"
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class GuardKitResult(BaseModel):
    status: Literal["success", "failed", "timeout"]
    subcommand: str
    artefacts: list[str] = Field(default_factory=list)         # absolute paths emitted by GuardKit
    coach_score: float | None = None
    criterion_breakdown: dict[str, float] | None = None
    detection_findings: list[dict[str, Any]] | None = None
    duration_secs: float
    stdout_tail: str = ""                                       # last 4 KB (ASSUM-003)
    stderr: str | None = None
    exit_code: int
    warnings: list[GuardKitWarning] = Field(default_factory=list)
```

## Acceptance Criteria

- [ ] `GuardKitResult` and `GuardKitWarning` defined in
      `src/forge/adapters/guardkit/models.py`
- [ ] `status` is a `Literal["success", "failed", "timeout"]` (no `Enum`)
- [ ] `artefacts`, `warnings` use `Field(default_factory=list)` (no shared
      mutable defaults)
- [ ] `coach_score`, `criterion_breakdown`, `detection_findings`, `stderr`
      are explicitly `Optional`
- [ ] `model_dump_json()` round-trips through `model_validate_json()` without
      data loss
- [ ] Re-export shim: `src/forge/adapters/guardkit/__init__.py` re-exports
      `GuardKitResult`, `GuardKitWarning`
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Pydantic v2 `BaseModel`
- Per `system-prompt-template-specialist` re-export pattern (matches existing
  `src/forge/config/__init__.py` shim)
- Keep `models.py` declarative — no logic, no validators beyond what Pydantic
  gives by default
- Do **not** create a parser, runner, or any I/O here — that is TASK-GCI-004
  and TASK-GCI-008
