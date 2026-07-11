---
complexity: 4
conductor_workspace: wave2-forward-context-builder
dependencies:
- TASK-FW10-001
estimated_minutes: 60
feature_id: FEAT-FORGE-010
id: TASK-FW10-003
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- factory
- forward-context
- allowlist
task_type: feature
title: ForwardContextBuilder production factory bound to SQLite reader and worktree
  allowlist
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-003 — `ForwardContextBuilder` production factory

## Why

`ForwardContextBuilder` is one of `dispatch_autobuild_async`'s four
required collaborators. It exists as a class in
`src/forge/pipeline/forward_context_builder.py` (the gap doc's grep
showed only one docstring example) but has no production constructor.
This task adds the factory that binds the builder to a SQLite reader
and the worktree allowlist read from `forge_config`.

## Files to create / modify

- `src/forge/cli/_serve_deps_forward_context.py` (NEW):
  - `def build_forward_context_builder(sqlite_pool, forge_config) -> ForwardContextBuilder`
  - Returns a builder configured with:
    - The SQLite reader for prior stage outputs / forward propagation.
    - `forge_config.allowed_worktree_paths` (or equivalent — confirm
      the field name when implementing) as the allowlist applied to
      every worktree path the builder returns.
- `tests/cli/test_serve_deps_forward_context.py` (NEW):
  - Factory returns a builder that round-trips a request against a
    fixture SQLite database.
  - Allowlist enforcement: a worktree path inside the allowlist is
    accepted; one outside is rejected before the builder returns the
    context.

## Acceptance criteria

- [ ] `build_forward_context_builder` accepts `(sqlite_pool, forge_config)`
      and returns a `ForwardContextBuilder` Protocol-conforming object.
- [ ] The returned builder honours `forge_config`'s worktree allowlist.
      A path outside the allowlist is rejected before the builder
      returns; the rejection raises an exception that callers can
      translate into a `build-failed` envelope (delegated to
      TASK-FW10-009).
- [ ] Unit tests cover the happy path (allowed worktree → context
      returned) and the rejected path (disallowed worktree → exception).
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- Confirm the exact name of the allowlist field on `ForgeConfig`
  during implementation (source of truth: `src/forge/config/models.py`).
- `ForwardContextBuilder` exposes a Protocol surface in
  `src/forge/pipeline/dispatchers/autobuild_async.py`. Match that
  surface; do not add new methods.
- This factory does **not** import from `_serve_deps.py` (composition
  is TASK-FW10-007's job). Keeps the five Wave 2 tasks free of
  cross-merge conflicts.

## Coach validation

- `pytest tests/cli/test_serve_deps_forward_context.py -x`.
- `pytest tests/forge -x` (smoke gate 2).
- Lint: project-configured ruff/format.

## References

- [`src/forge/pipeline/forward_context_builder.py`](../../../src/forge/pipeline/forward_context_builder.py) (the class to construct)
- [`src/forge/pipeline/dispatchers/autobuild_async.py`](../../../src/forge/pipeline/dispatchers/autobuild_async.py) (the Protocol surface)
- IMPLEMENTATION-GUIDE.md §4 contract: `ForwardContextBuilder`