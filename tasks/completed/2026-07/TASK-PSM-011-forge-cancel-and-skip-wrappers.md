---
id: TASK-PSM-011
title: '`forge cancel` and `forge skip` thin wrappers'
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 4
implementation_mode: direct
complexity: 3
estimated_minutes: 45
status: completed
dependencies:
- TASK-PSM-005
consumer_context:
- task: TASK-PSM-003
  consumes: CONFIG_LOADER
  framework: Pydantic v2
  driver: YAML + Pydantic
  format_note: ForgeConfig used for connection parameters; cancel/skip route through
    CliSteeringHandler which is already shipped
- task: TASK-PSM-005
  consumes: PERSISTENCE_PROTOCOLS
  framework: Python typing.Protocol (runtime_checkable)
  driver: dependency injection via constructor
  format_note: Wraps SqliteBuildSnapshotReader, SqliteBuildCanceller, SqliteBuildResumer,
    SqliteStageSkipRecorder, SqlitePauseRejectResolver instances and passes them to
    CliSteeringHandler
- task: TASK-PSM-004
  consumes: STATE_TRANSITION_API
  framework: Python module import
  driver: in-process call
  format_note: "CliSteeringHandler internally invokes state_machine.transition() via\
    \ the Sqlite* persistence implementations \u2014 CLI command itself never composes\
    \ Transition objects"
tags:
- cli
- forge-cancel
- forge-skip
- click
- cli-steering
autobuild_state:
  current_turn: 2
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:46:01.314242'
  last_updated: '2026-04-27T14:04:39.809518'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Quality gates reported as passed but no tests were executed (tests_passed=0,
      coverage=null). Player may not have run tests.'
    timestamp: '2026-04-27T13:46:01.314242'
    player_summary: 'Direct mode SDK invocation completed (git-detected: 36 modified,
      15 created)'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:58:57.932598'
    player_summary: "Created src/forge/cli/cancel.py (55 lines) and src/forge/cli/skip.py\
      \ (50 lines) \u2014 both are intentionally thin Click wrappers that resolve\
      \ the operator-supplied identifier via SqliteLifecyclePersistence.find_active_or_recent\
      \ and delegate to CliSteeringHandler.handle_cancel/handle_skip with responder=os.getlogin().\
      \ Extracted the runtime wiring (CliRuntime + build_cli_runtime) into a new src/forge/cli/runtime.py\
      \ to avoid a circular import between cli/main.py and the cancel/skip subcommand\
      \ modules. "
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-011: feature-rollup completed"
---

# Task: `forge cancel` and `forge skip` thin wrappers

## Description

Create `src/forge/cli/cancel.py` and `src/forge/cli/skip.py`. Both are
**thin wrappers** that delegate to the already-shipped
`CliSteeringHandler` from `forge.pipeline.cli_steering` (FEAT-FORGE-007
territory — do NOT redesign cancel/skip semantics).

Per `API-cli.md §6` and `§7`:

### `forge cancel <feature_id|build_id> [--reason "text"]`

1. Resolve to a `build_id` via `persistence.find_active_or_recent(feature_id)`
2. If not found: exit non-zero with "no active or recent build" message
   (Group C)
3. Call `CliSteeringHandler.handle_cancel(build_id, reason, responder=os.getlogin())`
4. The handler decides:
   - If build is PAUSED → synthesises ApprovalResponsePayload(reject) via
     `synthetic_response_injector` (FEAT-FORGE-002 territory; already shipped)
   - If build is RUNNING → calls `cancel_async_task` + transitions to
     CANCELLED via state_machine
5. Print confirmation, exit 0

### `forge skip <feature_id> [--reason "text"]`

1. Resolve build_id same as cancel
2. Call `CliSteeringHandler.handle_skip(build_id, reason, responder=os.getlogin())`
3. If status != PAUSED on a `FLAG_FOR_REVIEW` gate, the handler returns
   `SkipStatus.REFUSED` → CLI exits non-zero with "skip not allowed unless
   paused" (Group C)
4. Otherwise the handler synthesises ApprovalResponsePayload(override) and
   the build resumes; CLI prints confirmation and exits 0

This task implements:

- Group C "Skip on non-paused refused" (handler already enforces this)
- Group C "Cancel of unknown feature → not-found"
- Group D "Cancel paused → synthetic reject" (handler already does this)
- Group D "Skip flagged-stage → resume running" (handler already does this)
- Group E "Cancelling operator recorded distinctly" — the wrapper passes
  `responder=os.getlogin()`; handler stores it on the resolution

## Acceptance Criteria

- [ ] `cli/cancel.py` exports `cancel_cmd` (Click command)
- [ ] `cli/skip.py` exports `skip_cmd` (Click command)
- [ ] Both register with `cli/main.py`
- [ ] `forge cancel` resolves feature_id → build_id via
      `persistence.find_active_or_recent`; exits non-zero with NOT_FOUND
      message if no match (Group C)
- [ ] `forge skip` exits non-zero with REFUSED message when handler
      returns `SkipStatus.REFUSED` (Group C "skip on non-paused")
- [ ] Both pass `responder=os.getlogin()` to the handler — Group E
      "cancelling operator recorded distinctly" passes
- [ ] Both wrappers are < 60 lines each (this task is intentionally thin
      — the handler does the work)
- [ ] BDD scenarios for Group C (cancel-unknown, skip-non-paused) and
      Group D (cancel-paused, skip-flagged-stage) pass
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
# src/forge/cli/cancel.py
import os
import sys
import click

from forge.pipeline.cli_steering import CliSteeringHandler, CancelStatus
from forge.lifecycle.persistence import SqliteLifecyclePersistence


@click.command("cancel")
@click.argument("identifier")
@click.option("--reason", default="cli cancel", help="Cancellation reason recorded on the build")
@click.pass_obj
def cancel_cmd(config, identifier: str, reason: str) -> None:
    persistence = SqliteLifecyclePersistence.from_config(config)
    build = persistence.find_active_or_recent(identifier)
    if build is None:
        click.echo(f"No active or recent build for: {identifier}", err=True)
        sys.exit(2)

    handler = CliSteeringHandler.from_config(config)
    result = handler.handle_cancel(
        build_id=build.build_id,
        reason=reason,
        responder=os.getlogin(),
    )
    click.echo(f"Cancelled {build.build_id}: {result.summary()}")
```

`forge skip` follows the same pattern.

## Coach Validation

- `cancel.py` and `skip.py` both exist as thin wrappers (< 60 lines each)
- Both delegate to `CliSteeringHandler` — DO NOT reimplement cancel/skip
  semantics
- Both pass `responder=os.getlogin()` — Group E audit-trail invariant
- `find_active_or_recent` used for resolution (handles the not-found case)
- Lint/format pass
