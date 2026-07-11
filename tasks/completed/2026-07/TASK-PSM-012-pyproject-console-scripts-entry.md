---
id: TASK-PSM-012
title: pyproject.toml `console_scripts` entry for forge CLI
task_type: scaffolding
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 5
implementation_mode: direct
complexity: 2
estimated_minutes: 30
status: completed
dependencies:
- TASK-PSM-008
- TASK-PSM-009
- TASK-PSM-010
- TASK-PSM-011
tags:
- scaffolding
- pyproject
- packaging
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T14:12:59.302745'
  last_updated: '2026-04-27T14:19:10.895380'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T14:12:59.302745'
    player_summary: 'Two surgical changes implement TASK-PSM-012: (1) added [project.scripts]
      block to pyproject.toml with `forge = "forge.cli.main:main"` so `pip install
      -e .` writes a `forge` shim onto $PATH; (2) registered the previously-orphaned
      `status_cmd` from `forge.cli.status` on the top-level Click group in `src/forge/cli/main.py`
      so all five subcommands (queue, status, history, cancel, skip) appear under
      `forge --help`. The status command was already implemented by TASK-PSM-009 but
      had never been wired in'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-012: feature-rollup completed"
---

# Task: pyproject.toml `console_scripts` entry for forge CLI

## Description

Add the Click entry point to [pyproject.toml](../../../pyproject.toml) so
`pip install` (or `pip install -e .`) makes the `forge` binary available
on PATH:

```toml
[project.scripts]
forge = "forge.cli.main:main"
```

After this is wired:

- `forge --help` lists subcommands (queue, status, history, cancel, skip)
- `forge queue --help` shows the queue subcommand options
- `pyinstrument` / `cProfile` show the binary launching cleanly with no
  import-time NATS connection (read commands MUST start without touching
  the bus)

## Acceptance Criteria

- [ ] `pyproject.toml` contains `[project.scripts]` section with the
      `forge = "forge.cli.main:main"` entry
- [ ] `pip install -e .` (or the project's equivalent) succeeds
- [ ] `forge --help` runs and exits 0; output lists all five subcommands
- [ ] `forge queue --help`, `forge status --help`, `forge history --help`,
      `forge cancel --help`, `forge skip --help` all exit 0
- [ ] Smoke test: `forge status` against an empty SQLite DB returns an
      empty table (no NATS connection attempted — verified by tracing)
- [ ] `setup.py` / `setup.cfg` are NOT used (this is a `pyproject.toml`
      project per the existing convention)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This is the smallest task in the feature but the most user-visible — it's
what makes the CLI exist as a binary. Sequence after Wave 4 finishes so
all five subcommands are wireable.

## Coach Validation

- `pyproject.toml` has the `[project.scripts]` block
- `forge` is on PATH after `pip install -e .`
- `forge --help` exits 0 and shows all five subcommands
- No NATS connection during `forge --help` startup
