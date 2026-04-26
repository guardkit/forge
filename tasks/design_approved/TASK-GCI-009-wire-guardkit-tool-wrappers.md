---
complexity: 6
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-GCI-008
- TASK-GCI-005
feature_id: FEAT-FORGE-005
id: TASK-GCI-009
implementation_mode: task-work
parent_review: TASK-REV-GCI0
priority: high
status: design_approved
tags:
- guardkit
- tools
- langchain
- decorator
- error-contract
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Wire 9 guardkit_* @tool wrappers (system/feature/task/autobuild)
updated: 2026-04-25 00:00:00+00:00
wave: 4
---

# Task: Wire 9 guardkit_* @tool wrappers (system/feature/task/autobuild)

## Description

Build the nine `@tool(parse_docstring=True)` async wrappers in
`src/forge/tools/guardkit.py` — one per non-Graphiti GuardKit subcommand.
Each is a thin wrapper that calls `forge.adapters.guardkit.run()`
(TASK-GCI-008), composes a NATS progress subscription via
`subscribe_progress()` (TASK-GCI-005) when streaming is enabled, and
returns a JSON string. None of these tools may raise (ADR-ARCH-025).

Per `docs/design/contracts/API-tool-layer.md` §6 (GuardKit subcommand
tools, table at §6.1) and §2 (universal error contract).

## Tools to wrap

| Tool | Wraps | Parameters |
|---|---|---|
| `guardkit_system_arch` | `system-arch` | repo, feature_id, scope |
| `guardkit_system_design` | `system-design` | repo, focus, protocols |
| `guardkit_system_plan` | `system-plan` | repo, feature_description |
| `guardkit_feature_spec` | `feature-spec` | repo, feature_description, context_paths |
| `guardkit_feature_plan` | `feature-plan` | repo, feature_id |
| `guardkit_task_review` | `task-review` | repo, task_id |
| `guardkit_task_work` | `task-work` | repo, task_id |
| `guardkit_task_complete` | `task-complete` | repo, task_id |
| `guardkit_autobuild` | `autobuild` | repo, feature_id |

## Implementation

```python
# src/forge/tools/guardkit.py
from langchain.tools import tool
from forge.adapters.guardkit.run import run as guardkit_run
from forge.adapters.guardkit.progress_subscriber import subscribe_progress, ProgressSink


@tool(parse_docstring=True)
async def guardkit_feature_spec(
    repo: str,
    feature_description: str,
    context_paths: list[str] | None = None,
) -> str:
    """Run `guardkit feature-spec` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_description: One-line description for the /feature-spec session.
        context_paths: Optional explicit --context overrides. When None, the
            context-manifest resolver picks them automatically.

    Returns:
        JSON: {"status":"success|failed|timeout","artefacts":[...],
        "coach_score":...,"duration_secs":...,"stderr":"...","warnings":[...]}.
    """
    try:
        # ... compose run() call, optional progress subscription, return result.model_dump_json()
    except Exception as exc:
        return f'{{"status":"error","error":"{type(exc).__name__}: {exc}"}}'
```

## Acceptance Criteria

- [ ] All nine wrappers in `src/forge/tools/guardkit.py`, each decorated with
      `@tool(parse_docstring=True)`
- [ ] Every wrapper returns a `str` (JSON-encoded `GuardKitResult` on
      success/failed/timeout, or `{"status":"error","error":"..."}` on
      internal exception per ADR-ARCH-025) — never raises (Scenarios "A
      failing GuardKit subprocess is reported as a structured error, not an
      exception" / "An unexpected error inside a wrapper is returned as a
      structured error, not raised")
- [ ] Function body of each wrapper is wrapped in
      `try/except Exception as exc:` returning the JSON error string
- [ ] Each wrapper calls `forge.adapters.guardkit.run(subcommand=…, …)`
      with the right `subcommand` literal — no `format` / `f-string`
      command construction
- [ ] Each wrapper composes the NATS progress subscriber for telemetry
      (Scenario "GuardKit progress is streamed on the bus while the
      subprocess is still running"); a missing subscriber must not fail the
      call (Scenario "The authoritative result still returns when progress
      streaming is unavailable")
- [ ] Each wrapper logs via `structlog` with `tool_name`, `duration_ms`,
      `status` (per API-tool-layer.md §2)
- [ ] Tools that take `context_paths` (only `guardkit_feature_spec` per
      §6.1) thread them through to `run(extra_context_paths=…)` for the
      explicit-context retry case (Scenario "A failed invocation can be
      retried with additional explicit context")
- [ ] On a successful call, the returned JSON string contains `artefacts`
      (Scenario "A GuardKit subcommand completes successfully and its
      artefacts are captured"), `duration_secs`, and `coach_score` when
      GuardKit produced one
- [ ] PR creation is **not** in this file — that lives in a separate
      `version_control` tool layer that wraps TASK-GCI-007 (out of scope
      here; the BDD scenario "Forge opens a pull request for the build
      through the version-control adapter" is covered by the gh adapter
      task and BDD test wiring)
- [ ] Unit tests with a fake `run()`: each tool returns the right JSON
      shape on success/failed/timeout/exception; each tool's docstring
      parses correctly through `@tool(parse_docstring=True)` (verifiable by
      inspecting `tool.args_schema`)
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Follow the `langchain-tool-decorator-specialist` rule — every tool wraps
  body in try/except, returns string, never raises. The docstring is the
  description; the `Args:` block is the schema source
- Use `asyncio.gather()` to spawn the progress subscriber concurrent with
  the synchronous `run()` call — the subscription's lifetime is the
  duration of the run
- Avoid duplication via a small helper inside the module (e.g.
  `_invoke(subcommand, repo, args, extra_context=None)`) that the nine
  wrappers delegate to — keeps each `@tool` body focused on
  parameter→args translation
- The `repo` parameter is a `str` per the contract (LangChain tools prefer
  primitive parameter types); convert to `Path` inside the helper

## Seam Tests

`@pytest.mark.seam` tests should validate the JSON contract: every wrapper
returns parseable JSON with the documented keys; the error path returns the
ADR-ARCH-025 shape verbatim; the progress subscriber is invoked but its
unavailability does not affect the wrapper's return. Tag with
`@pytest.mark.integration_contract("guardkit_tool_layer_contract")`.