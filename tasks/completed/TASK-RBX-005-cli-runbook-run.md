---
id: TASK-RBX-005
title: 'CLI: forge runbook run <path>'
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: feature
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 4
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies:
- TASK-RBX-004
consumer_context:
- task: TASK-RSP-003
  consumes: persistence_repo_surface
  framework: click command -> RunbookRepository.create_runbook then RunbookExecutor.run
  driver: sqlite3 (STRICT), forge.adapters.sqlite
  format_note: the JSON file is read, persisted via create_runbook (durable home for
    results + pointer), THEN executed (ASSUM-007); duplicate create is refused by
    the repo
tags:
- forge
- runbook
- executor
- cli
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-22T08:48:09.883452'
  last_updated: '2026-06-22T09:00:11.004961'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T08:48:09.883452'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# CLI: forge runbook run <path>

## TL;DR

Wire the executor to the command line. `forge runbook run <path-to-runbook-json>`
reads the JSON, **persists** it via `create_runbook` (so results + pointer have
a durable home — ASSUM-007), then runs it through `RunbookExecutor` and reports
the outcome. Attaches as a sibling of `queue` / `status` / `history` / `cancel`
/ `skip` on the existing `forge.cli.main:main` Click group.

## Scope

New module `src/forge/cli/runbook.py`; one line added to
`src/forge/cli/main.py`.

- **`runbook_cmd`** — a `click.Group(name="runbook")` exposing a `run`
  subcommand (group so `forge runbook <verb>` can grow later).
- **`forge runbook run <path>`**:
  - Read the file at `<path>`. If it does not exist → report clearly that the
    runbook file could not be found; **execute nothing**; non-zero exit
    (Negative "missing runbook file").
  - Parse JSON into a `Runbook`. If contents are not a valid runbook → report
    clearly that the file is invalid; **execute nothing**; non-zero exit
    (Negative "invalid runbook file").
  - `create_runbook(...)` to persist (durable home). A duplicate is refused by
    the repo (surface that as a clear message, not a traceback).
  - Build `RunbookExecutor(repo, registry, publisher)` and `run(...)`.
  - Report the `RunResult`: "runbook completed" on success; the escalation
    reason + stopped index on stop/pause.
- Register the command in `main.py`:
  `main.add_command(_runbook.runbook_cmd)` (import `from forge.cli import
  runbook as _runbook`).
- The registry + publisher are assembled at the CLI boundary (dependency
  injection point), mirroring how `forge queue` builds its runtime.

## Acceptance Criteria

- [ ] `forge runbook run <path>` loads a runbook file, executes its steps in
      sequence order, and reports completion (Key Example "Running a runbook
      from the command line").
- [ ] The runbook is **persisted** (`create_runbook`) before execution so the
      pointer + results survive (ASSUM-007); a `forge runbook run` of the same
      file twice does not double-create (repo refuses; clear message).
- [ ] A missing file path reports "runbook file could not be found" and
      executes nothing; non-zero exit (Negative).
- [ ] An invalid runbook file reports "runbook file is invalid" and executes no
      steps; non-zero exit (Negative).
- [ ] `forge runbook run --help` renders; the command appears under
      `forge runbook` (sibling of queue/status/history/cancel/skip).
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.
- [ ] Tests added to `tests/forge/test_cli_runbook.py` using Click's
      `CliRunner` + a `tmp_path` runbook file, written **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/test_cli_runbook.py -q
python -m pytest tests/forge/test_cli_main.py -q
python -m pytest tests/forge/test_cli_runbook.py -q -m seam
```

## §4 Seam Tests

Validates the `persistence_repo_surface` contract — the CLI persists *then*
executes (ASSUM-007), not the reverse.

```python
"""Seam test: verify persist-then-execute ordering (TASK-RSP-003 / ASSUM-007)."""
import pytest
from click.testing import CliRunner


@pytest.mark.seam
@pytest.mark.integration_contract("persistence_repo_surface")
def test_cli_persists_before_executing(tmp_path, monkeypatch) -> None:
    """`forge runbook run` calls create_runbook before the executor runs.

    Contract: the runbook must have a durable home (create_runbook) before any
    step executes, so results + pointer survive a crash mid-run.
    Producer: TASK-RSP-003
    """
    calls: list[str] = []
    # monkeypatch RunbookRepository.create_runbook -> calls.append("create")
    # monkeypatch RunbookExecutor.run -> calls.append("run")
    # write a valid runbook json to tmp_path, invoke the CLI, then:
    # assert calls == ["create", "run"]
    pytest.skip("wire spies over create_runbook + executor.run once built")
```

## Implementation Notes

- Keep file/JSON validation errors as clean operator messages (Click
  `echo` + non-zero exit), never raw tracebacks — the two negative scenarios
  assert on the *message intent*, not a Python exception class.
- The cancel/skip wrappers show the established pattern for a thin subcommand
  that does not need `ctx.obj` config; follow it.
- Do not duplicate execution logic here — the CLI is a thin adapter over
  `RunbookExecutor` (TASK-RBX-004).
