---
id: TASK-PSM-008
title: CLI scaffold and `forge queue` command
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 4
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
status: completed
dependencies:
- TASK-PSM-001
- TASK-PSM-003
- TASK-PSM-005
consumer_context:
- task: TASK-PSM-001
  consumes: IDENTIFIER_VALIDATION
  framework: urllib.parse + re (stdlib)
  driver: stdlib
  format_note: "validate_feature_id(s) returns the validated string or raises InvalidIdentifierError.\
    \ Call it BEFORE any SQLite write or NATS publish in the queue command \u2014\
    \ fail fast on traversal/encoded-traversal/null-byte attacks"
- task: TASK-PSM-003
  consumes: CONFIG_LOADER
  framework: Pydantic v2
  driver: YAML + Pydantic
  format_note: load_config(path) returns ForgeConfig with .queue.default_max_turns/default_sdk_timeout_seconds/default_history_limit/repo_allowlist;
    CLI flags (--max-turns, --timeout) override defaults at the build-row write site
- task: TASK-PSM-005
  consumes: PERSISTENCE_PROTOCOLS
  framework: Python typing.Protocol (runtime_checkable)
  driver: dependency injection via constructor
  format_note: SqliteLifecyclePersistence.record_pending_build(payload) raises DuplicateBuildError
    on UNIQUE(feature_id, correlation_id) violation; exists_active_build(feature_id)
    checks Group C in-flight duplicate
- task: TASK-PSM-004
  consumes: STATE_TRANSITION_API
  framework: Python module import
  driver: in-process call
  format_note: "queue command does NOT directly transition state \u2014 it only writes\
    \ the QUEUED row via persistence.record_pending_build(); state transitions begin\
    \ in TASK-PSM-007 (recovery) and the supervisor (within-build, owned by 002-007)"
tags:
- cli
- forge-queue
- click
- write-then-publish
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:46:01.323109'
  last_updated: '2026-04-27T14:12:56.842980'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:46:01.323109'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-008: feature-rollup completed"
---

# Task: CLI scaffold and `forge queue` command

## Description

Create the `src/forge/cli/` package with the Click entry point and the
`forge queue` command. This is the first user-facing CLI; per
[`API-cli.md`](../../../docs/design/contracts/API-cli.md) it is short-lived
(runs, reads/writes, exits).

Files:

- `src/forge/cli/__init__.py` — empty package marker
- `src/forge/cli/main.py` — Click `@click.group()` entry point, dispatches to
  subcommands; loads `forge.yaml` via `forge.config.load_config`
- `src/forge/cli/queue.py` — `forge queue` subcommand

`forge queue` behaviour (`API-cli.md §3.3`):

1. Parse args: `feature_id` (positional), `--repo`, `--branch`,
   `--feature-yaml`, `--max-turns`, `--timeout`, `--correlation-id`
2. **Validate feature_id** via `lifecycle.identifiers.validate_feature_id()`
   — exit code 4 on `InvalidIdentifierError` (NEW exit code; `API-cli.md`
   currently lists 0/1/2/3, add 4 for identifier validation)
3. **Allowlist check** — `--repo` path must match an entry in
   `ForgeConfig.queue.repo_allowlist` (exit code 2 on miss)
4. Merge defaults: CLI args override `ForgeConfig.queue.default_*`
5. Build `BuildQueuedPayload(triggered_by="cli", originating_user=os.getlogin(), queued_at=now)`
6. Wrap in `MessageEnvelope(source_id="forge-cli", event_type=BUILD_QUEUED)`
7. **Active-duplicate check** — `persistence.exists_active_build(feature_id)`
   → exit code 3 if True (Group C "active in-flight duplicate")
8. **Write SQLite row** via `persistence.record_pending_build(payload)`
9. **THEN publish to NATS** via `pipeline_publisher.publish()`
10. **On NATS publish failure** — do NOT roll back the SQLite row; print
    `Queued FEAT-XXX (build pending) but pipeline NOT NOTIFIED — publish failed: <reason>`
    to stderr; exit code 1 (Group G + Group H)
11. On success: print
    `Queued FEAT-XXX (build pending) correlation_id=<uuid>` to stdout;
    exit 0

This task implements concerns **sc_002** (write-then-publish failure) and
**sc_003** (identifier validation) at the CLI boundary.

## Acceptance Criteria

- [ ] `forge.cli.main:main` is a Click group importable as
      `forge.cli.main:main` (the `pyproject.toml` entry in TASK-PSM-012
      will reference this)
- [ ] `forge.cli.main` loads `forge.yaml` once and passes the parsed
      `ForgeConfig` via `click.Context.obj` to subcommands
- [ ] `forge queue` validates `feature_id` BEFORE any SQLite write or NATS
      publish; on `InvalidIdentifierError`, exits 4 with a clear message
      and no side effects
- [ ] `forge queue` rejects `--repo` paths not matching
      `ForgeConfig.queue.repo_allowlist` with exit code 2 (Group C
      "path-allowlist refused")
- [ ] `forge queue` merges CLI args over config defaults: e.g.
      `--max-turns 7` overrides `default_max_turns: 5`
- [ ] `forge queue` writes the SQLite row BEFORE attempting NATS publish
      (verifiable by inspecting source order)
- [ ] On NATS publish failure: SQLite row remains, exit code 1, stderr
      message includes "publish failed" and identifies messaging-layer
      (Group H "messaging unreachable")
- [ ] On duplicate `(feature_id, correlation_id)` (caught at SQLite layer):
      exit code 3 with "duplicate build" message (Group B)
- [ ] BDD scenario test cases for queue command exit codes are passable
- [ ] No imports from `forge.adapters.nats` in `cli/status.py` /
      `cli/history.py` (this task scaffolds the package — verify the
      import discipline upfront)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
# src/forge/cli/main.py
import click
from forge.config.loader import load_config
from forge.cli import queue, status, history, cancel, skip


@click.group()
@click.option("--config", default=None, type=click.Path(exists=True), help="Override forge.yaml path")
@click.pass_context
def main(ctx: click.Context, config: str | None) -> None:
    ctx.obj = load_config(config) if config else load_config_default()


main.add_command(queue.queue_cmd)
main.add_command(status.status_cmd)
main.add_command(history.history_cmd)
main.add_command(cancel.cancel_cmd)
main.add_command(skip.skip_cmd)
```

```python
# src/forge/cli/queue.py — sketch only
@click.command("queue")
@click.argument("feature_id")
@click.option("--repo", required=True, type=click.Path(exists=True))
# ... other options ...
@click.pass_obj
def queue_cmd(config, feature_id, repo, ...):
    try:
        feature_id = validate_feature_id(feature_id)
    except InvalidIdentifierError as e:
        raise click.ClickException(f"Invalid feature_id: {e.reason}") from e
        # Click maps ClickException → exit code 1 by default; we'll need
        # to use sys.exit(4) or a custom CliException class for 2/3/4

    if not _path_in_allowlist(repo, config.queue.repo_allowlist):
        sys.exit(2)
    if persistence.exists_active_build(feature_id):
        sys.exit(3)

    payload = build_payload(...)
    persistence.record_pending_build(payload)  # write FIRST
    try:
        publisher.publish(payload)              # then publish
    except nats.errors.Error as e:
        click.echo(f"Queued {feature_id} but pipeline NOT NOTIFIED: {e}", err=True)
        sys.exit(1)

    click.echo(f"Queued {feature_id} (build pending) correlation_id={payload.correlation_id}")
```

## Coach Validation

- `forge.cli.main:main` is the Click group; subcommands registered
- `forge queue` calls `validate_feature_id` BEFORE any side effect
- Write-before-publish ordering visible in source
- `cli/status.py` and `cli/history.py` (created in PSM-009 / PSM-010)
  contain zero imports from `forge.adapters.nats.*`
- Exit codes 0/1/2/3/4 all reachable
- Lint/format pass
