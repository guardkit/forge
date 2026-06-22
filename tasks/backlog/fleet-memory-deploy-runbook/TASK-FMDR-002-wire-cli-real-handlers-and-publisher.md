---
id: TASK-FMDR-002
title: Wire `forge runbook run` to the real step handlers and real NATS publisher
status: in_progress
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: feature
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 1
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies: []
tags:
- forge-output-loop
- forge-cli
- runbook-executor
- nats-publisher
consumer_context:
- task: TASK-FMDR-001
  consumes: RUNBOOK_STEP_PARAMS
  framework: forge.executor.shell_steps subprocess handlers (deploy_compose / run_smoke_tests)
  driver: docker compose via fleet-memory deploy.sh / smoke.sh
  format_note: step.params must provide cwd, script, env_file keys; env_file is a
    path only, never its contents
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 0
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FMDR
  base_branch: main
  started_at: '2026-06-22T22:07:21.085990'
  last_updated: '2026-06-22T22:07:21.085992'
  turns: []
---

# TASK-FMDR-002 — Wire `forge runbook run` to real handlers + real publisher

## Summary

`src/forge/cli/runbook.py` today constructs an **empty `StepTypeRegistry()`** and a
**`_NoOpNATSClient`** (lines 196–199, "full wiring will come in integration"). This is
that integration. Make `forge runbook run` dispatch through the real FEAT-SSH handlers
and publish the real lifecycle events.

FEAT-SSH is **merged on `main`**: `register_shell_handlers`, `deploy_compose`, and
`run_smoke_tests` live in `src/forge/executor/shell_steps.py`. Import and use them — do
**not** re-implement handlers.

## Acceptance Criteria

- [ ] In `run_cmd`, the registry is populated by
      `register_shell_handlers(registry)` (from `forge.executor.shell_steps`) so each
      step is dispatched to its registered handler; the run reports the runbook
      completed successfully (A3).
- [ ] The `_NoOpNATSClient` is replaced by a real NATS client opened via the established
      pattern (`nats.connect(servers=...)`, `FORGE_NATS_URL` env var, default
      `nats://127.0.0.1:4222` — see `src/forge/cli/queue.py:265`,
      `src/forge/cli/_serve_daemon.py:163`). Lifecycle events are published in order:
      runbook-started → step-started → step-result → … → runbook-complete (D6).
- [ ] Publishing is **best-effort**: if no broker is reachable the run still completes
      (the executor already wraps every publish in `_safe_publish`). A `--no-events`
      escape hatch (or equivalent) keeps the CLI usable on hosts with no broker.
- [ ] The database password / DSN never appears in the persisted step results or the
      published lifecycle events — verified by a planted-secret assertion (C3). (Scrub
      happens in FEAT-SSH's `scrub_process_output`; this task asserts the boundary
      holds end-to-end through the CLI.)
- [ ] Existing `tests/forge/test_cli_runbook.py` still passes (persist-before-execute
      ordering, error handling, duplicate refusal).
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Coach Validation

- `pytest tests/forge/test_cli_runbook.py -v`
- `pytest tests/forge -k "runbook and (handler or publisher or events)" -v`
- Lint/format the changed files.

## Implementation Notes

- Keep the DI seams (`_build_repository`, `_build_executor`) — inject the registry and
  publisher so tests can substitute a capturing fake NATS client to assert event order.
- Reuse `RunbookPublisher` (`src/forge/adapters/nats/runbook_publisher.py`) — it already
  owns the five lifecycle methods; you are only swapping the client it wraps.
- Do not change the executor's publish semantics; only the CLI's wiring changes.

## Seam Tests

The following seam test validates the integration contract with the producer task
(TASK-FMDR-001). Implement it to verify the boundary before integration.

```python
"""Seam test: verify RUNBOOK_STEP_PARAMS contract from TASK-FMDR-001."""
import json
from pathlib import Path

import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("RUNBOOK_STEP_PARAMS")
def test_runbook_step_params_format():
    """Verify the exemplar's step params match what the shell handlers read.

    Contract: step.params must provide cwd, script, env_file keys (env_file a
    path only). Producer: TASK-FMDR-001.
    """
    data = json.loads(
        Path("forge/runbooks/RUNBOOK-fleet-memory-nas.json").read_text(encoding="utf-8")
    )
    assert data["steps"], "runbook must have steps"
    for step in data["steps"]:
        params = step["params"]
        assert {"cwd", "script", "env_file"} <= params.keys(), (
            f"step {step['step_type']} missing required params: {params}"
        )
        # env_file is a path only — never an inlined secret or connection string.
        assert "password" not in params["env_file"].lower()
        assert "://" not in params["env_file"]
```
