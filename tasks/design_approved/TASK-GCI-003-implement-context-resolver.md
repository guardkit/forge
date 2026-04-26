---
complexity: 6
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-GCI-001
feature_id: FEAT-FORGE-005
id: TASK-GCI-003
implementation_mode: task-work
parent_review: TASK-REV-GCI0
priority: high
status: design_approved
tags:
- guardkit
- adapter
- context-manifest
- resolver
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement context_resolver.resolve_context_flags() (DDR-005)
updated: 2026-04-25 00:00:00+00:00
wave: 2
---

# Task: Implement context_resolver.resolve_context_flags() (DDR-005)

## Description

Build the resolver that reads `.guardkit/context-manifest.yaml` from a target
repo, follows dependency references up to a depth-2 cap, filters documents by
the per-subcommand allowed-category table, prepends `internal_docs.always_include`,
omits documents outside the filesystem read allowlist, and returns the ordered
`--context <path>` argument list for a GuardKit invocation.

This is the central value prop of FEAT-FORGE-005 — every GuardKit subcommand
invocation goes through this function (except Graphiti subcommands, which
bypass it entirely per Scenario "Graphiti GuardKit subcommands skip
context-manifest resolution entirely").

Per `docs/design/decisions/DDR-005-cli-context-manifest-resolution.md` and
`docs/design/contracts/API-subprocess.md` §3.3.

## Implementation

```python
# src/forge/adapters/guardkit/context_resolver.py
from pathlib import Path
from forge.adapters.guardkit.models import GuardKitWarning


_COMMAND_CATEGORY_FILTER: dict[str, set[str]] = {
    "system-arch":    {"architecture", "decisions"},
    "system-design":  {"specs", "decisions", "contracts", "architecture"},
    "system-plan":    {"architecture", "decisions", "specs"},
    "feature-spec":   {"specs", "contracts", "source", "decisions"},   # ASSUM-004
    "feature-plan":   {"specs", "decisions", "architecture"},
    "task-review":    {"contracts", "source"},
    "task-work":      {"contracts", "source"},
    "task-complete":  {"contracts", "source"},
    "autobuild":      {"contracts", "source"},
    # Graphiti subcommands intentionally absent — caller must skip resolution.
}

_DEPTH_CAP = 2  # ASSUM-002


class ResolvedContext:
    """Output of resolve_context_flags()."""
    flags: list[str]                       # ["--context", "/abs/path/a.md", "--context", "/abs/path/b.md"]
    paths: list[str]                       # absolute paths in flag order
    warnings: list[GuardKitWarning]


def resolve_context_flags(
    repo_path: Path,
    subcommand: str,
    read_allowlist: list[Path],
) -> ResolvedContext: ...
```

## Acceptance Criteria

- [ ] `resolve_context_flags()` in `src/forge/adapters/guardkit/context_resolver.py`
      returns a `ResolvedContext` with `flags`, `paths`, `warnings`
- [ ] `_COMMAND_CATEGORY_FILTER` matches DDR-005 verbatim (9 entries — Graphiti
      subcommands are intentionally absent)
- [ ] Missing manifest → returns empty `flags`, single
      `GuardKitWarning(code="context_manifest_missing", …)`, never raises
      (Scenario "A missing context manifest degrades gracefully to no context flags")
- [ ] `internal_docs.always_include` paths are prepended to the flag list
      regardless of category filter (Scenario "Context flags are assembled
      automatically from the manifest for the invoked subcommand")
- [ ] Dependency chase follows manifests up to depth 2 then stops with
      `context_manifest_cycle_detected` warning (Scenarios "Context resolution
      follows dependency references up to the depth cap" / "stops at the depth
      cap and warns")
- [ ] Cycle detection: a manifest already visited in the current chain is not
      re-visited (Scenario "A circular dependency chain is detected and resolved
      safely")
- [ ] Documents whose resolved absolute path is outside `read_allowlist` are
      omitted with a structured warning naming the omitted path (Scenario
      "Context documents that fall outside the read allowlist are omitted with
      a warning")
- [ ] Documents whose path resolves outside the repo root are omitted with a
      structured warning (Scenario "A context manifest entry that would escape
      the repository root is rejected")
- [ ] Resolution is **stateless** — two concurrent calls against the same
      `repo_path` produce independent `ResolvedContext` values, no module-level
      cache (ASSUM-007, Scenario "Two concurrent builds against the same
      repository resolve context independently")
- [ ] Order is stable: `always_include` first, then categories in
      `_COMMAND_CATEGORY_FILTER` insertion order, then by manifest declaration
      order within each category
- [ ] Symlinks are followed before allowlist check
- [ ] Exhaustive unit tests: missing manifest, depth-1 chase, depth-2 chase,
      depth-3 stop, two-node cycle, allowlist-omitted doc, escaping-path doc,
      Graphiti-bypass-by-caller (resolver raises `KeyError` on Graphiti
      subcommand keys — caller must skip resolution entirely, not call this
      function)
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Use `pathlib.Path.resolve(strict=False)` to canonicalise (handles symlinks)
- The depth cap is on **manifest hops**, not document count. depth-0 = the
  origin repo's manifest; depth-1 = one hop into a sibling repo's manifest;
  depth-2 = two hops (the cap)
- For the "Graphiti subcommands skip resolution" scenario, the cleanest seam
  is: caller (TASK-GCI-010) decides to skip. This function should raise
  `KeyError` if asked to resolve a Graphiti subcommand — that's a programmer
  error, not a runtime degradation. Document this in the docstring
- Read allowlist paths are passed in (do **not** import `forge.config` here —
  let the caller wire it; keeps the resolver pure for testability)
- Return `ResolvedContext` as a `@dataclass(frozen=True)` or NamedTuple
  (lighter-weight than Pydantic for an internal value object)