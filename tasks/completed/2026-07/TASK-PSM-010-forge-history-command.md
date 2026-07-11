---
id: TASK-PSM-010
title: '`forge history` command (--feature, --limit, --since, --format)'
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 4
implementation_mode: direct
complexity: 4
estimated_minutes: 60
status: completed
dependencies:
- TASK-PSM-005
consumer_context:
- task: TASK-PSM-003
  consumes: CONFIG_LOADER
  framework: Pydantic v2
  driver: YAML + Pydantic
  format_note: ForgeConfig.queue.default_history_limit (default 50) used when --limit
    is not specified
- task: TASK-PSM-005
  consumes: PERSISTENCE_PROTOCOLS
  framework: Python typing.Protocol (runtime_checkable)
  driver: dependency injection via constructor
  format_note: read_history(limit, feature_id) returns BuildRow list; read_only_connect()
    (mode=ro URI)
tags:
- cli
- forge-history
- click
- read-path
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:46:01.322331'
  last_updated: '2026-04-27T13:53:43.316564'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:46:01.322331'
    player_summary: "Implemented forge.cli.history per API-cli.md \xA75. The module\
      \ exposes a Click command (history_cmd) plus a plain run_history() entry point\
      \ so unit tests drive the rendering path without invoking Click. Three renderers\
      \ (table/json/md), --feature filter, --since ISO-date filter (with bare-date\
      \ promotion to midnight UTC), --limit (defaults to ForgeConfig.queue.default_history_limit).\
      \ Reads SQLite via forge.adapters.sqlite.read_only_connect (mode=ro URI). Stage_log\
      \ expansion happens for json + md form"
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-010: feature-rollup completed"
---

# Task: `forge history` command

## Description

Create `src/forge/cli/history.py` per
[`API-cli.md §5`](../../../docs/design/contracts/API-cli.md).

Behaviour:

- **`forge history`** (no args) — last 50 builds (default from
  `ForgeConfig.queue.default_history_limit`), table format, newest-first.
- **`--feature FEAT-XXX`** — all builds for that feature with
  `stage_log` expanded.
- **`--limit N`** — clamp results to N (capped at e.g. 1000 by the
  persistence layer to prevent unbounded queries).
- **`--since 2026-04-20`** — filter to builds queued on or after the
  given ISO date.
- **`--format table|json|md`** — table is default; `md` emits the
  markdown structure shown in `API-cli.md §5.3`; `json` is suitable for
  piping.

Same import discipline as `forge status` — **MUST NOT** import from
`forge.adapters.nats.*`.

## Acceptance Criteria

- [ ] `forge history` returns at most 50 entries by default (Group B
      "History with no arguments returns the 50 most recent")
- [ ] `forge history --limit 1` returns at most 1 entry (Group B
      Scenario Outline)
- [ ] `forge history --limit 50` on 60 prior builds returns 50 (Group B)
- [ ] `forge history` on 10 prior builds returns 10 (does not pad)
- [ ] `forge history --feature FEAT-XXX` returns all builds for that
      feature; matches Group A "Viewing history for a specific feature"
- [ ] `forge history --since 2026-04-20` filters by `queued_at >= date`
- [ ] `forge history --format md` emits the markdown structure from
      `API-cli.md §5.3` (per-build heading, started/finished, stages list)
- [ ] `forge history --format json` emits a JSON array
- [ ] **Import check**: zero NATS imports in `cli/history.py`
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

- The markdown format renders nicely in Claude / VSCode; the example in
  `API-cli.md §5.3` is the spec.
- `--limit` defaults to `config.queue.default_history_limit` (which is
  itself defaulted to 50 in `QueueConfig`).
- Date parsing for `--since`: use `datetime.fromisoformat(s + "T00:00:00+00:00")`
  to allow bare-date input.

## Coach Validation

- `cli/history.py` exists with `history_cmd`
- No NATS imports
- Default limit comes from config, not hardcoded
- Three output formats supported (table, json, md)
- Lint/format pass
