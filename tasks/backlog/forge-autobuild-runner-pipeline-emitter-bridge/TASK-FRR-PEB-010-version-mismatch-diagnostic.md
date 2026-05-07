---
id: TASK-FRR-PEB-010
title: langgraph-runner version-mismatch diagnostic at startup (fail-fast)
status: in_review
created: 2026-05-06 00:00:00+00:00
updated: 2026-05-06 00:00:00+00:00
priority: high
task_type: refactor
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 4
implementation_mode: task-work
complexity: 4
estimated_minutes: 60
dependencies:
- TASK-FRR-PEB-002
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- version-skew-diagnostic
- sdk-volatility-mitigation
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-PEBR
  base_branch: main
  started_at: '2026-05-07T10:35:23.020407'
  last_updated: '2026-05-07T10:49:19.366920'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `the stack-specific Phase-3 specialist` (Implementation)\n- Not all acceptance\
      \ criteria met:\n  \u2022 AC-1: A new `src/forge/lifecycle_bridge/version_check.py`\
      \ declares a\n  \u2022 AC-2: At `LifecycleBridge` initialisation (before `recover_in_flight`),\n\
      \  \u2022 AC-3: On out-of-range version, the bridge raises\n  \u2022 AC-4: The\
      \ diagnostic is also printed to stderr (in addition to\n  \u2022 AC-5: On in-range\
      \ version, startup proceeds silently (no INFO log\n  (1 more)"
    timestamp: '2026-05-07T10:35:23.020407'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-05-07T10:44:09.558150'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: langgraph-runner version-mismatch diagnostic at startup (fail-fast)

## TL;DR

Mitigate the dominant Option C risk (SDK schema drift across `langgraph-api`
versions) by checking the running sidecar's version at daemon startup and
**failing the daemon with a clear diagnostic** if the version is outside
the bridge's declared support range. Surfaces version skew **loudly** at
startup rather than silently emitting malformed envelopes at runtime.

## Locks BDD scenarios

- @edge-case @regression `A langgraph-runner version mismatch is
  detected at forge startup and fails the daemon with a diagnostic`
  (ASSUM-010)

## Acceptance criteria

- AC-1: A new `src/forge/lifecycle_bridge/version_check.py` declares a
  `LANGGRAPH_API_SUPPORTED_RANGE = ">=0.8.5,<0.9"` (or the actual
  current range — confirm during implementation by checking
  `pyproject.toml` and the running sidecar's `/version` endpoint).
- AC-2: At `LifecycleBridge` initialisation (before `recover_in_flight`),
  the bridge calls the sidecar's `/version` (or equivalent SDK-exposed
  metadata endpoint) and compares against the declared range using
  `packaging.specifiers.SpecifierSet`.
- AC-3: On out-of-range version, the bridge raises
  `LangGraphVersionMismatchError` with message naming both the
  expected range and the observed version. The error propagates to
  daemon startup and **fails the daemon** (the daemon never finishes
  booting).
- AC-4: The diagnostic is also printed to stderr (in addition to
  raising) so the operator sees it without needing logs:
  `langgraph-runner version skew: expected {range}, observed {version}.
  Bridge cannot start safely.`
- AC-5: On in-range version, startup proceeds silently (no INFO log
  is enough — verbose-mode INFO is acceptable but default is silent).
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- In-range version → daemon starts cleanly (stub `/version` returns
  e.g. `0.8.7`).
- Out-of-range version → daemon fails with diagnostic; stderr contains
  expected and observed versions.
- Sidecar unreachable at startup → version check uses a 5s timeout;
  on timeout, retry policy falls back to T8's reconnect rather than
  failing the daemon (so a slow-starting sidecar doesn't kill forge).

## Files to Create

- `src/forge/lifecycle_bridge/version_check.py`
- `tests/forge/lifecycle_bridge/test_version_check.py`

## Files to Modify

- `src/forge/lifecycle_bridge/bridge.py`
- `pyproject.toml`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/version_check.py` (new);
  `src/forge/lifecycle_bridge/bridge.py` (call check in init);
  `pyproject.toml` (declared range source-of-truth — keep in sync
  with the constant in `version_check.py`).
- Use `packaging.specifiers` for range comparison (already a
  transitive dep via `setuptools`); add to `pyproject.toml`
  `dependencies` if not present.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_version_check.py -x -v
ruff check src/forge/lifecycle_bridge/version_check.py
```
