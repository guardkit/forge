---
id: TASK-IC-010
title: "Git/gh operations via DeepAgents execute tool"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [git, github, subprocess, deepagents, security]
complexity: 4
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 2
implementation_mode: task-work
dependencies: [TASK-IC-009]
estimated_minutes: 90
consumer_context:
  - task: TASK-IC-009
    consumes: execute_command_allowlist
    framework: "DeepAgents execute tool subprocess permissions layer"
    driver: "DeepAgents execute_command"
    format_note: "Allowlist is a single named constant ALLOWED_BINARIES = {'git', 'gh', 'pytest'}. Any addition requires ADR + allowlist-change review. Test verification (TASK-IC-009) and git/gh ops (this task) share the same allowlist constant."
---

# Task: Git/gh operations via DeepAgents execute tool

## Description

Implement the four git/`gh` operations needed for end-to-end build flow:
branch, commit, push, PR creation. All routed through the DeepAgents
`execute` tool with the `git`/`gh`/`pytest` binary allowlist (same constant
as TASK-IC-009). Working directory locked to per-build worktree.
Credentials sourced from environment variables only.

Covers `@key-example pr-opened`, `@integration integration-end-to-end-build`,
`@security security-env-only-credentials`, `@security security-working-directory-allowlist`,
`@negative negative-missing-credentials`, `@negative negative-disallowed-binary-refused`.

## Module: `forge/build/git_operations.py`

```python
ALLOWED_BINARIES = frozenset({"git", "gh", "pytest"})  # SHARED with TASK-IC-009

async def create_branch(worktree_path: Path, branch_name: str) -> None:
    """git checkout -b <branch_name>"""

async def commit_changes(worktree_path: Path, message: str) -> None:
    """git add -A && git commit -m <message>"""

async def push_branch(worktree_path: Path, branch_name: str) -> None:
    """git push -u origin <branch_name>"""

async def create_pull_request(
    worktree_path: Path,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """gh pr create --title --body --base; return PR URL"""
```

## Acceptance Criteria

- [ ] Single named constant `ALLOWED_BINARIES` defined here; `TASK-IC-009`
      imports it (single source of truth)
- [ ] All four functions invoke via DeepAgents `execute` tool (not direct
      `subprocess.run()`)
- [ ] Working directory parameter enforced for every call; the execute
      tool's working-directory allowlist validates `worktree_path` is under
      the configured builds directory (`@security security-working-directory-allowlist`)
- [ ] Credentials read from environment only (`GH_TOKEN`, `GITHUB_TOKEN`)
      — NEVER from `forge.yaml` (`@security security-env-only-credentials`)
- [ ] Missing credentials → `create_pull_request()` records reason on
      `SessionOutcome` and does not crash (`@negative negative-missing-credentials`)
- [ ] Attempted invocation of disallowed binary (e.g. `rm`, `curl`) raises
      a clear error from the execute tool layer
      (`@negative negative-disallowed-binary-refused`)
- [ ] `create_pull_request()` returns the PR URL (parsed from `gh pr create`
      stdout)
- [ ] All operations are awaitable so the build loop can interleave them
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_git_operations.py` — mocked execute tool; assert
      correct binary + args; assert worktree_path passed as cwd
- [ ] `tests/unit/test_credentials_env_only.py` — clearing `GH_TOKEN`
      causes `create_pull_request()` to record reason without raising
- [ ] `tests/unit/test_disallowed_binary.py` — invocation with binary not
      in `ALLOWED_BINARIES` raises clean error
- [ ] `tests/integration/test_end_to_end_build.py` — opt-in; against a
      throwaway test repo, runs branch → commit → push → PR end-to-end

## Seam Tests

The following seam test validates the integration contract with the producer task. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify execute_command_allowlist from TASK-IC-009."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("execute_command_allowlist")
def test_execute_command_allowlist_shared():
    """Verify the allowlist constant is shared between test verification
    and git/gh operations.

    Contract: ALLOWED_BINARIES = {'git', 'gh', 'pytest'} is defined once
    and imported by both TASK-IC-009 and TASK-IC-010.
    Producer: TASK-IC-009 (test verification) — but the constant lives in
    TASK-IC-010's module per the §4 contract; verifier imports it.
    """
    from forge.build.git_operations import ALLOWED_BINARIES

    assert ALLOWED_BINARIES == frozenset({"git", "gh", "pytest"}), \
        "Allowlist must contain exactly git/gh/pytest. Adding a binary " \
        "requires ADR + allowlist-change review."

    from forge.build.test_verification import _allowed_binaries_for_test
    assert _allowed_binaries_for_test() is ALLOWED_BINARIES, \
        "Test verification must reference the SAME allowlist constant, " \
        "not duplicate it. See §4 Integration Contract."
```

## Implementation Notes

- Risk 5 from review: allowlist drift. The `ALLOWED_BINARIES` constant is
  the single chokepoint; any PR adding a binary to it must trigger an
  allowlist-change review. Document this in the module docstring.
- For `gh pr create`, parse the PR URL from stdout — it's the last line.
- Don't shell-quote arguments yourself; the execute tool handles argv as
  a list.
- The "missing credentials" case: `gh pr create` will fail with a specific
  exit code or stderr pattern; catch it and record `cred_missing=True` on
  the `SessionOutcome`'s metadata. The build still completes.
