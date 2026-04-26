---
complexity: 7
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-GCI-003
- TASK-GCI-004
feature_id: FEAT-FORGE-005
id: TASK-GCI-008
implementation_mode: task-work
parent_review: TASK-REV-GCI0
priority: high
status: design_approved
tags:
- guardkit
- adapter
- subprocess
- timeout
- cancellation
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement forge.adapters.guardkit.run() subprocess wrapper
updated: 2026-04-25 00:00:00+00:00
wave: 3
---

# Task: Implement forge.adapters.guardkit.run() subprocess wrapper

## Description

Build the central subprocess wrapper that every `guardkit_*` tool wrapper
calls. It assembles the command line, invokes DeepAgents `execute`, enforces
the 600-second default timeout (ASSUM-001), captures the 4 KB stdout tail
(ASSUM-003), enforces worktree-confined `cwd`, and folds the outcome through
`parse_guardkit_output()` (TASK-GCI-004) into a `GuardKitResult`.

This is the **boundary** where adapter exceptions become structured errors —
`run()` itself never raises (ADR-ARCH-025).

Per `docs/design/contracts/API-subprocess.md` §3.1 (invocation shape), §3.2
(progress stream), §3.4 (result schema), §6 (return-value contract).

## Implementation

```python
# src/forge/adapters/guardkit/run.py
from pathlib import Path
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.context_resolver import resolve_context_flags  # TASK-GCI-003
from forge.adapters.guardkit.parser import parse_guardkit_output            # TASK-GCI-004


async def run(
    *,
    subcommand: str,
    args: list[str],
    repo_path: Path,
    read_allowlist: list[Path],
    timeout_seconds: int = 600,        # ASSUM-001
    with_nats_streaming: bool = True,
    extra_context_paths: list[str] | None = None,   # ASSUM-005 (retry merge)
) -> GuardKitResult:
    """Single subprocess entry point for every GuardKit subcommand.

    Composes context_resolver + execute + parser. Enforces timeout, captures
    stdout-tail, never raises past the adapter boundary.
    """
```

## Acceptance Criteria

- [ ] `run()` in `src/forge/adapters/guardkit/run.py`
- [ ] Composes `[guardkit, subcommand, *args, *context_flags, --nats?]` and
      passes the list (never a shell string) to DeepAgents `execute`
- [ ] `cwd = repo_path` is always inside the build worktree allowlist —
      DeepAgents enforces, but `run()` adds a defence-in-depth check that
      `repo_path` is absolute and non-symlinked-to-outside before the call
      (Scenario "A subprocess targeting a working directory outside the
      allowlist is refused")
- [ ] Calls `resolve_context_flags(repo_path, subcommand, read_allowlist)`
      and prepends the resolver's flags; resolver warnings are surfaced on
      `GuardKitResult.warnings`
- [ ] Graphiti subcommands (`graphiti add-context`, `graphiti query`) skip
      the resolver entirely — `run()` detects the `subcommand` prefix and
      omits the resolver call (Scenario "Graphiti GuardKit subcommands skip
      context-manifest resolution entirely")
- [ ] `extra_context_paths` (when provided) are merged with manifest-derived
      paths for the current call only — not persisted (Scenario "A failed
      invocation can be retried with additional explicit context";
      ASSUM-005)
- [ ] `with_nats_streaming=True` appends `--nats` to the command line; the
      NATS subscriber (TASK-GCI-005) is wired separately by the caller
- [ ] On timeout: terminate the subprocess and return
      `GuardKitResult(status="timeout", …)` (Scenarios "A subprocess that
      exceeds the timeout is reported as timed-out" / "A silent stalled
      subprocess is terminated by the configured timeout"); the process
      handle is released before returning
- [ ] On cancellation (caller cancels the surrounding asyncio task):
      terminate the subprocess cleanly, do not surface partial artefacts as
      completed work (Scenario "A cancelled build terminates its in-flight
      subprocess cleanly")
- [ ] On non-zero exit: returns `GuardKitResult(status="failed", …)` with
      `stderr` and `exit_code` populated (Scenario "A non-zero exit is
      reported as a failure with the subprocess error output")
- [ ] On binary outside the shell allowlist (DeepAgents-enforced): the
      `execute` call's exception is caught and converted into
      `GuardKitResult(status="failed", warnings=[…permissions_refused…])`
      (Scenario "A subprocess whose binary is not in the shell allowlist is
      refused")
- [ ] Function body wrapped in `try/except Exception as exc:` — never
      raises (ADR-ARCH-025; Scenario "An unexpected error inside a wrapper
      is returned as a structured error, not raised")
- [ ] Two parallel `run()` calls within the same build worktree do not
      share mutable state — each receives its own `GuardKitResult` (ASSUM-006,
      Scenario "Parallel GuardKit invocations in the same build do not
      corrupt each other's results")
- [ ] Boundary unit tests around the timeout: 599s success, 600s exact,
      601s timeout (Scenario "A subprocess that finishes within the timeout
      is reported as successful" with examples 1, 300, 599)
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Use `asyncio.wait_for(execute(...), timeout=timeout_seconds)` for the
  timeout cap. On `asyncio.TimeoutError`, ensure the underlying process is
  terminated (DeepAgents `execute` handles this if the timeout flows
  through its API; otherwise wrap in a helper that sends SIGTERM then
  SIGKILL after a 5-second grace)
- For cancellation: catch `asyncio.CancelledError` in a `finally` block,
  terminate the subprocess if still running, **re-raise** the
  `CancelledError` so the surrounding async context unwinds correctly. This
  is the **one** exception to "never raises" — cancellation propagation is
  required for correct asyncio shutdown
- The Graphiti subcommand detector should match the prefix
  (`subcommand.startswith("graphiti ")` or split on whitespace), keyed off
  whatever shape the tool wrappers actually pass in (TASK-GCI-010
  decides — sync the contract there)
- Build-id and worktree path come from the caller (the tool wrapper); this
  function does not consult any global state — the resolver and the run
  must remain stateless (ASSUM-007)
- Wrap the whole body in `try/except Exception as exc: return GuardKitResult(...)` —
  do not propagate exceptions; return a "failed" result with a
  `wrapper_internal_error` warning carrying `type(exc).__name__: str(exc)`

## Seam Tests

This task is the integration seam between context resolver, output parser,
and DeepAgents execute. Add `@pytest.mark.seam` tests with stubbed
`execute` covering: timeout boundary (599/600/601s), cancellation
mid-run, parallel invocations isolated, Graphiti bypass, retry with
extra context. Tag with
`@pytest.mark.integration_contract("guardkit_subprocess_contract")`.