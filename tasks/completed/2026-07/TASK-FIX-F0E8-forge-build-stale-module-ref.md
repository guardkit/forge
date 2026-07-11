---
id: TASK-FIX-F0E8
title: Fix `forge.build` stale module ref in `tests/unit/test_git_operations.py`
status: completed
created: 2026-04-29T11:35:00Z
updated: 2026-04-29T12:35:00Z
previous_state: in_progress
state_transition_reason: "Path 1 (importorskip) applied; collection error eliminated; awaiting human review"
priority: low
tags: [fix, test-cleanup, stale-ref, F0E4-followup, importorskip]
complexity: 1
task_type: fix
decision_required: false
parent_review: TASK-REV-F0E4
scoping_source: .claude/reviews/TASK-REV-F0E4-report.md §5.3
estimated_effort: 5-10 minutes
chosen_path: "1 (importorskip)"  # path 2 = move-out-of-collection; path 3 = build TASK-IC-010
fix_summary: |
  TASK-IC-010 turns out to be design_approved-but-not-yet-implemented
  (not a stale-ref deletion as F0E4 §5.3 framed it). The test file
  predates the implementation. Added `pytest.importorskip("forge.build.git_operations", ...)`
  at the top of tests/unit/test_git_operations.py so collection skips cleanly
  until TASK-IC-010 ships. When that lands, delete the importorskip block
  to re-enable the test scaffolding.
files_changed:
  - tests/unit/test_git_operations.py  # +6 lines (3 comment + 4-line importorskip pytest call)
test_results:
  status: passed
  coverage: not_applicable  # MINIMAL workflow; no coverage requirement
  last_run: 2026-04-29T12:35:00Z
  evidence: |
    .venv/bin/python -m pytest --co -q tests/unit/test_git_operations.py
      → "no tests collected in 0.02s" (clean skip; previously: 1 collection error)
    .venv/bin/python -m pytest --co -q tests/unit/
      → "289 tests collected in 0.05s" (zero ModuleNotFoundError; previously: 289 + 1 error)
reconciled: "2026-07-11 WS3-S8 sweep — TASK-FIX-F0E8: ['git: ee2b555 fix(tests): skip-on-import test_git_operations.py until TASK-IC-010 ships (TASK-FIX-F0E8)']"
---

# Task: Fix `forge.build` stale module ref in `tests/unit/test_git_operations.py`

## Description

Surfaced as a side-effect of TASK-REV-F0E4's empirical Python 3.14 install:
collecting `tests/unit/test_git_operations.py` produces

```
E   ModuleNotFoundError: No module named 'forge.build'
```

`forge.build` does not exist in `src/forge/`. Likely a stale module-path
reference left over from a refactor.

This is **pre-existing** (not introduced by TASK-REV-F0E4),
**orthogonal to LangChain pinning** and `nats-core`, and isolated to a
single test file. Filed separately so the cleanup is small and reversible.

## Acceptance Criteria

- [ ] Diagnose: read `tests/unit/test_git_operations.py` and identify which
      symbol(s) are imported from `forge.build`.
- [ ] Decide:
  - If the symbol genuinely moved during a refactor — update the import
    to the current location.
  - If the symbol no longer exists (the test was orphaned by a deletion)
    — delete the test file (or the affected test class) and note the
    deletion in the commit message.
  - If the test is testing functionality that *should* exist but was
    accidentally removed — bigger problem; escalate (do NOT just delete
    in that case).
- [ ] After the fix: `.venv/bin/python -m pytest --co -q tests/unit/test_git_operations.py`
      collects with **zero** errors. (If the file is deleted, this AC becomes
      "the file is gone and full-suite collection no longer references it.")
- [ ] Commit references `TASK-FIX-F0E8` and `TASK-REV-F0E4` in the message body.

## Out of scope

- The LangChain 1.x pin alignment (that's TASK-LCP-001 inside the FEAT-F0EP feature folder at `tasks/backlog/langchain-1x-pin-alignment/`).
- Fixing `nats-core` import (that's TASK-FIX-F0E6).
- Fixing `pytest-asyncio` / dev-deps install (that's TASK-FIX-F0E7).
- Any broader cleanup of stale module references in other test files —
  if more are found, file them per-finding rather than bundling.
- Adding new test coverage for git operations — out of scope for a
  cleanup task.

## Source Material

- **Empirical observation**: [`.claude/reviews/TASK-REV-F0E4-report.md`](../../.claude/reviews/TASK-REV-F0E4-report.md) §5.3
- **Pytest log**: [`docs/history/portfolio-py314-rebaseline-pytest.log`](../../docs/history/portfolio-py314-rebaseline-pytest.log)
- **The file being fixed**: `tests/unit/test_git_operations.py`
- **Module that doesn't exist**: `src/forge/build/` (verify with `ls src/forge/`)
