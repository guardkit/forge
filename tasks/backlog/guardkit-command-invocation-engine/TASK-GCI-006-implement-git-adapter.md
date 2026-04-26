---
id: TASK-GCI-006
title: Implement forge.adapters.git (worktree, commit, push, cleanup)
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-GCI0
feature_id: FEAT-FORGE-005
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-GCI-002
tags:
- git
- adapter
- worktree
- subprocess
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T08:56:02.868521'
  last_updated: '2026-04-26T09:27:08.426150'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Not all acceptance criteria\
      \ met:\n  \u2022 All modified files pass project-configured lint/format checks\
      \ with zero"
    timestamp: '2026-04-26T08:56:02.868521'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: '- Advisory (non-blocking): task-work produced a report with 2 of 3
      expected agent invocations. Missing phases: 3 (Implementation). Consider invoking
      these agents via the Task tool to strengthen stack-specific quality:

      - Phase 3: `python-api-specialist` (Implementation)

      - BDD oracle: 1 scenario(s) failed during pytest-bdd execution. Implementation
      does not satisfy the Gherkin specification.'
    timestamp: '2026-04-26T09:09:04.144191'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-04-26T09:16:50.078194'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement forge.adapters.git (worktree, commit, push, cleanup)

## Description

Build the thin wrappers over DeepAgents `execute` for git operations:
`prepare_worktree`, `commit_all`, `push`, `cleanup_worktree`. All four return
`GitOpResult` (TASK-GCI-002) and never raise past the adapter boundary
(ADR-ARCH-025).

Worktree cleanup is **best-effort** — a failed cleanup must not block
terminal-state transitions (Scenario "A failed worktree cleanup is logged but
does not prevent build completion"). PR creation lives in TASK-GCI-007 to
keep gh credentials separate from git operations.

Per `docs/design/contracts/API-subprocess.md` §4 (git/gh adapters) and §5
(worktree lifecycle, ADR-ARCH-028).

## Implementation

```python
# src/forge/adapters/git/operations.py
from pathlib import Path
from forge.adapters.git.models import GitOpResult


async def prepare_worktree(build_id: str, repo: Path, branch: str) -> GitOpResult: ...
async def commit_all(worktree: Path, message: str) -> GitOpResult: ...      # GitOpResult.sha populated on success
async def push(worktree: Path, remote_branch: str) -> GitOpResult: ...
async def cleanup_worktree(build_id: str, worktree: Path) -> GitOpResult: ...
```

## Acceptance Criteria

- [ ] All four functions in `src/forge/adapters/git/operations.py`, returning
      `GitOpResult`
- [ ] Every call goes through DeepAgents `execute` (no `subprocess.run` /
      `os.system` direct usage — permissions are enforced by the framework)
- [ ] All calls run with `cwd` inside the build's worktree (Scenario
      "Subprocesses are executed inside the current build's worktree")
- [ ] `prepare_worktree` creates `/var/forge/builds/{build_id}/`, runs
      `git worktree add <path> <branch>`, returns `GitOpResult.worktree_path`
- [ ] `commit_all` runs `git add -A && git commit -m <msg>` and returns
      `GitOpResult.sha` (parsed from `git rev-parse HEAD` after the commit)
- [ ] `push` runs `git push origin <branch>` and converts non-zero exit into
      `status="failed"` with stderr preserved
- [ ] `cleanup_worktree` runs `git worktree remove <path> --force` and
      returns `status="failed"` on non-zero exit but **logs and continues**
      (Scenario "A failed worktree cleanup is logged but does not prevent
      build completion") — the caller (state machine) must treat this as a
      warning, not a blocker
- [ ] Each function is wrapped in `try/except Exception as exc:` returning
      `GitOpResult(status="failed", operation=..., stderr=f"{type(exc).__name__}: {exc}")`
      — never raises (ADR-ARCH-025)
- [ ] Arguments containing shell metacharacters are passed as separate list
      tokens to `execute`, never via shell-string concatenation (Scenario
      "Shell metacharacters in subprocess arguments are passed as literal
      tokens")
- [ ] Unit tests with a fake `execute`: success path for each op, non-zero
      exit, raised exception, shell-metacharacter argument passthrough
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Use the same DeepAgents `execute` import the rest of the project uses; do
  not introduce a second subprocess primitive
- `prepare_worktree` may fail if the path already exists — return
  `status="failed"` with a clear stderr; let the caller decide whether to
  reuse or escalate
- `commit_all` with no staged changes is a real case (e.g. autobuild made no
  edits) — return `status="failed"` with stderr containing git's "nothing to
  commit" output; the caller decides whether to treat as success or skip the
  push
- This task deliberately does **not** include PR creation — gh has its own
  credential surface (TASK-GCI-007)

## Seam Tests

Add `@pytest.mark.seam` integration tests that hit a real git binary against
a tmp_path fixture (no network). Validate worktree-add, commit, and the
cleanup-failure-doesn't-block contract. Tag with
`@pytest.mark.integration_contract("git_adapter_subprocess_contract")`.
