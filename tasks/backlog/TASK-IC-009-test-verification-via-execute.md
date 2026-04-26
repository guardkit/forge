---
id: TASK-IC-009
title: "Test verification via DeepAgents execute tool"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [testing, subprocess, deepagents]
complexity: 3
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 1
implementation_mode: direct
dependencies: []
estimated_minutes: 60
---

# Task: Test verification via DeepAgents execute tool

## Description

Implement the function that runs the configured test command (default:
`pytest`) inside the per-build ephemeral worktree via the DeepAgents
`execute` tool. Capture stdout, parse exit code, return a typed result dict
that downstream consumers (Coach, BDD steps) validate against.

Covers `@key-example test-verification`, `@negative negative-failing-tests-reported`,
and resolves ASSUM-003/ASSUM-004 by defining the result dict shape concretely.

## Module: `forge/build/test_verification.py`

```python
class TestVerificationResult(TypedDict):
    passed: bool
    pass_count: int
    fail_count: int
    failing_tests: list[str]
    output_tail: str
    duration_seconds: float

async def verify_tests(
    worktree_path: Path,
    test_command: str = "pytest",
    timeout_seconds: int = 600,
) -> TestVerificationResult:
    """Run test_command via the DeepAgents execute tool with worktree_path
    as cwd. Parse output. Return TestVerificationResult."""
```

## Acceptance Criteria

- [ ] Spawn subprocess via DeepAgents `execute` tool (NOT `subprocess.run()`
      directly — constitutional constraint per AGENTS.md)
- [ ] Working directory locked to `worktree_path` (validated against
      allowlist by execute tool, not by this module)
- [ ] Exit code 0 → `passed=True`; non-zero → `passed=False`
- [ ] Parse pytest summary line for `pass_count` / `fail_count` (e.g.
      `=== 12 passed, 3 failed in 4.2s ===`)
- [ ] Extract failing test identifiers (the `FAILED tests/...` lines)
- [ ] `output_tail` = last 4000 chars of captured stdout (configurable)
- [ ] `duration_seconds` parsed from pytest summary; fall back to wall-clock
      measurement
- [ ] Timeout (default 600s) respected; on timeout, returns `passed=False`
      with a synthetic `failing_tests=["__TIMEOUT__"]` marker
      (`@boundary subprocess-timeout`)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_verify_tests.py` — mocked execute tool returning
      various pytest output samples (all-pass, partial-fail, no-tests,
      collection-error)
- [ ] `tests/unit/test_verify_timeout.py` — simulated timeout produces
      synthetic timeout marker
- [ ] BDD step impl for `@key-example test-verification` and
      `@boundary subprocess-timeout` (TASK-IC-011)

## Implementation Notes

- The DeepAgents `execute` tool is the constitutional path; do NOT shell
  out via `subprocess.run()` even "just for tests" — the lesson from
  Graphiti `architecture_decisions` is that subprocess invocation of
  `claude`/`guardkit` was rejected; the same principle applies here.
- Pytest output parsing: use a regex on the summary line; if the regex
  doesn't match (older pytest, plugin format), fall back to `passed = exit_code == 0`
  with `pass_count = -1` / `fail_count = -1` to signal "exit code authoritative,
  counts unavailable" — document this fallback.
- The `test_command` is configurable so a project can use `pytest -x`,
  `pytest --cov`, etc. Validate it against the `git`/`gh`/`pytest` allowlist
  (the binary, first token, must be in the list).
