---
complexity: 4
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-GCI-002
feature_id: FEAT-FORGE-005
id: TASK-GCI-007
implementation_mode: task-work
parent_review: TASK-REV-GCI0
priority: high
status: design_approved
tags:
- gh
- github
- adapter
- pull-request
- subprocess
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement forge.adapters.gh (create_pr, missing-credential error)
updated: 2026-04-25 00:00:00+00:00
wave: 2
---

# Task: Implement forge.adapters.gh (create_pr, missing-credential error)

## Description

Build the thin wrapper over DeepAgents `execute` for `gh pr create`. Returns
`PRResult` (TASK-GCI-002) and converts a missing `GH_TOKEN` env var into a
structured error rather than letting `gh` exit with a confusing prompt
(Scenario "A pull-request creation without GitHub credentials returns a
structured error").

Per `docs/design/contracts/API-subprocess.md` §4 + §4.1 (gh authentication).

## Implementation

```python
# src/forge/adapters/gh/operations.py
import os
from pathlib import Path
from forge.adapters.git.models import PRResult


async def create_pr(
    worktree: Path,
    title: str,
    body: str,
    base: str = "main",
    draft: bool = False,
) -> PRResult: ...
```

## Acceptance Criteria

- [ ] `create_pr()` in `src/forge/adapters/gh/operations.py`, returning
      `PRResult`
- [ ] Pre-flight check: if `GH_TOKEN` is unset (or empty) in `os.environ`,
      return `PRResult(status="failed", error_code="missing_credentials",
      stderr="GH_TOKEN not set in environment")` **without invoking gh**
      (Scenario "A pull-request creation without GitHub credentials returns
      a structured error")
- [ ] Otherwise, invoke `gh pr create --title <t> --body <b> --base <base>`
      (plus `--draft` if requested) via DeepAgents `execute` with `cwd =
      worktree`
- [ ] On success, parse the PR URL from gh's stdout and populate
      `PRResult.pr_url` and `PRResult.pr_number` (parsed from the trailing
      slash component of the URL)
- [ ] On non-zero exit, `PRResult(status="failed", stderr=...)`, no
      exception
- [ ] Function body wrapped in `try/except Exception as exc:` returning a
      `failed` `PRResult` — never raises (ADR-ARCH-025)
- [ ] Arguments containing shell metacharacters (e.g. backticks in PR body)
      are passed as separate list tokens to `execute`, never via
      shell-string concatenation (Scenario "Shell metacharacters in
      subprocess arguments are passed as literal tokens")
- [ ] Unit tests: missing GH_TOKEN, success path with parsed URL, non-zero
      exit, raised exception, body containing backticks/dollar-signs
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- Do **not** read `GH_TOKEN` once at import time — re-check on every call
  (the env may legitimately change between builds in a long-running
  process)
- gh's PR-URL line is typically the last non-empty line of stdout, e.g.
  `https://github.com/owner/repo/pull/123`. Use a small regex
  (`r"https://github\.com/[^/]+/[^/]+/pull/(\d+)"`) over `stdout.strip()`
  rather than splitting by newlines
- Do not handle credential-store interactions; per API-subprocess.md §4.1,
  Forge does not manage GitHub credentials — `GH_TOKEN` is sourced from the
  env by Docker deployment

## Seam Tests

`@pytest.mark.seam` integration tests should mock `execute` rather than call
the real `gh` binary (avoids network + auth dependencies in CI). Validate
the missing-credential branch returns `error_code="missing_credentials"`
without ever invoking `execute`. Tag with
`@pytest.mark.integration_contract("gh_adapter_subprocess_contract")`.