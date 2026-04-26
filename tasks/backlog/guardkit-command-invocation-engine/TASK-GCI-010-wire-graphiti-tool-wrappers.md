---
id: TASK-GCI-010
title: Wire 2 Graphiti guardkit_* @tool wrappers (bypass resolver)
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-GCI0
feature_id: FEAT-FORGE-005
wave: 4
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-GCI-008
tags:
- guardkit
- graphiti
- tools
- langchain
- decorator
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 2
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T09:45:58.465787'
  last_updated: '2026-04-26T10:03:37.244569'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Not all acceptance criteria\
      \ met:\n  \u2022 All modified files pass project-configured lint/format checks\
      \ with zero"
    timestamp: '2026-04-26T09:45:58.465787'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-04-26T09:58:22.567975'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Wire 2 Graphiti guardkit_* @tool wrappers (bypass resolver)

## Description

Build the two `@tool(parse_docstring=True)` async wrappers for the GuardKit
Graphiti subcommands: `guardkit_graphiti_add_context` and
`guardkit_graphiti_query`. These differ from the other nine GuardKit tool
wrappers (TASK-GCI-009) in one critical way: **they bypass the
context-manifest resolver entirely** (Scenario "Graphiti GuardKit
subcommands skip context-manifest resolution entirely") because Graphiti
subcommands do not consume `--context` flags.

Per `docs/design/contracts/API-tool-layer.md` §6.1 (Tool list, last two
rows) and `docs/design/decisions/DDR-005-cli-context-manifest-resolution.md`
("GuardKit graphiti subcommands don't take --context; skip resolution
entirely.").

## Tools to wrap

| Tool | Wraps | Parameters |
|---|---|---|
| `guardkit_graphiti_add_context` | `graphiti add-context` | doc_path, group |
| `guardkit_graphiti_query` | `graphiti query` | query, group |

## Implementation

```python
# src/forge/tools/graphiti.py
from langchain.tools import tool
from forge.adapters.guardkit.run import run as guardkit_run


@tool(parse_docstring=True)
async def guardkit_graphiti_add_context(
    doc_path: str,
    group: str,
) -> str:
    """Run `guardkit graphiti add-context` to seed a doc into the knowledge graph.

    Skips context-manifest resolution — Graphiti subcommands do not consume
    --context flags.

    Args:
        doc_path: Absolute path to the markdown document to add.
        group: Graphiti group_id (e.g. "guardkit__feature_specs",
            "architecture_decisions").

    Returns:
        JSON: {"status":"success|failed|timeout", "duration_secs":..., "stderr":...}.
    """
    try:
        # ... call guardkit_run(subcommand="graphiti add-context", ...) — bypasses resolver
    except Exception as exc:
        return f'{{"status":"error","error":"{type(exc).__name__}: {exc}"}}'
```

## Acceptance Criteria

- [ ] Both wrappers in `src/forge/tools/graphiti.py`, each decorated with
      `@tool(parse_docstring=True)`
- [ ] Both call `forge.adapters.guardkit.run()` with a `subcommand`
      starting with `"graphiti "` so the resolver is **not** invoked
      (Scenario "Graphiti GuardKit subcommands skip context-manifest
      resolution entirely")
- [ ] No `--context` flags appear in the assembled command line for either
      wrapper, even when a manifest exists in the target repo
- [ ] Both return a JSON string; on internal exception, return
      `{"status":"error","error":"..."}` per ADR-ARCH-025 — never raises
- [ ] Function body wrapped in `try/except Exception as exc:`
- [ ] Each wrapper logs via `structlog` with `tool_name`, `duration_ms`,
      `status`
- [ ] Unit tests: each wrapper returns the right JSON shape on
      success/failed/timeout/exception; verify (via fake `run()`) that no
      `--context` flag is added regardless of the target repo's manifest
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- The `subcommand` value passed to `run()` must be exactly the prefix that
  `run()`'s Graphiti detector looks for (TASK-GCI-008 owns that detector).
  Sync the convention: `subcommand="graphiti add-context"` and
  `subcommand="graphiti query"` (a single-string subcommand with a space)
  is the simplest contract — `run()` then splits on space when assembling
  argv (`["guardkit", "graphiti", "add-context", ...]`)
- Do **not** import the context resolver here — these tools are
  resolver-blind by design. If `run()` ever calls the resolver for a
  Graphiti subcommand, that's a `run()` bug (TASK-GCI-008 has the
  test that asserts the resolver is **not** called for these subcommands)
- Working directory: `cwd = "/tmp"` or any worktree path is fine — Graphiti
  subcommands operate on Graphiti server state, not files in the repo. If
  `run()` requires a `repo_path` argument, pass the build's worktree (so
  the call still satisfies worktree-confinement)

## Seam Tests

`@pytest.mark.seam` tests should validate that the assembled command line
contains no `--context` token regardless of manifest presence. Tag with
`@pytest.mark.integration_contract("graphiti_tool_layer_contract")`.
