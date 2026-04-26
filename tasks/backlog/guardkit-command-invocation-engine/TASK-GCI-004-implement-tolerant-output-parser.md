---
id: TASK-GCI-004
title: Implement parse_guardkit_output() tolerant parser
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
- TASK-GCI-001
tags:
- guardkit
- adapter
- parser
- tolerant
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-005
  base_branch: main
  started_at: '2026-04-26T08:56:02.878708'
  last_updated: '2026-04-26T09:30:05.557849'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Not all acceptance criteria\
      \ met:\n  \u2022 All modified files pass project-configured lint/format checks\
      \ with zero"
    timestamp: '2026-04-26T08:56:02.878708'
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
    timestamp: '2026-04-26T09:04:32.693860'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-04-26T09:09:37.757576'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement parse_guardkit_output() tolerant parser

## Description

Build the parser that turns raw subprocess `(stdout, stderr, exit_code,
duration)` into a `GuardKitResult`. The contract is **tolerant by design** —
unknown output shapes degrade to `status="success"` with empty `artefacts`
rather than failing the whole call (Scenario "An unknown GuardKit output shape
degrades to success with no artefacts").

Per `docs/design/contracts/API-subprocess.md` §3.4 (result schema) and §6
(return-value contract — never raises past the adapter boundary,
ADR-ARCH-025).

## Implementation

```python
# src/forge/adapters/guardkit/parser.py
from forge.adapters.guardkit.models import GuardKitResult


_STDOUT_TAIL_BYTES = 4096   # ASSUM-003


def parse_guardkit_output(
    *,
    subcommand: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    duration_secs: float,
    timed_out: bool = False,
) -> GuardKitResult:
    """Parse a GuardKit subprocess outcome into the canonical result shape.

    Tolerant: unknown stdout shapes still return status="success" with empty
    artefacts (the reasoning model decides whether the stage produced useful
    work). Never raises — internal exceptions are caught and folded into
    GuardKitResult.warnings.
    """
```

## Acceptance Criteria

- [ ] `parse_guardkit_output()` in `src/forge/adapters/guardkit/parser.py`
- [ ] `timed_out=True` → `status="timeout"`, regardless of `exit_code`
      (Scenario "A subprocess that exceeds the timeout is reported as
      timed-out")
- [ ] `timed_out=False, exit_code != 0` → `status="failed"`, `stderr`
      preserved (Scenario "A non-zero exit is reported as a failure with the
      subprocess error output")
- [ ] `timed_out=False, exit_code == 0, recognised shape` → `status="success"`,
      artefacts/coach_score/criterion_breakdown/detection_findings populated
- [ ] `timed_out=False, exit_code == 0, unrecognised shape` →
      `status="success"`, `artefacts=[]`, no exception raised (Scenario "An
      unknown GuardKit output shape degrades to success with no artefacts")
- [ ] `stdout_tail` is the **last** 4 KB of stdout when stdout is larger
      than 4 KB (Scenario "A large stdout is truncated to the most recent
      tail in the returned result"); preserves stdout verbatim when it is
      smaller (Scenario "A compact stdout is preserved verbatim in the
      returned result")
- [ ] Tail boundary is byte-based, not character-based (multi-byte UTF-8 is
      sliced safely with `errors="ignore"` on the decode of the leading
      remainder)
- [ ] Internal parse errors (malformed JSON, etc.) are caught and surfaced
      as `GuardKitWarning(code="parser_unrecognised_shape", …)`; never
      propagate as exceptions (Scenario "An unexpected error inside a wrapper
      is returned as a structured error, not raised")
- [ ] Unit tests cover: success-with-artefacts, success-empty,
      success-unknown-shape, failed-with-stderr, timeout, stdout < 4 KB,
      stdout >> 4 KB, multi-byte stdout tail, malformed JSON in stdout
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- The shape GuardKit emits is documented in the GuardKit project, not here.
  Look for: a `## Artefacts` section listing absolute paths, optional
  `coach_score: <float>` line, optional `## Coach Breakdown` table,
  optional `## Detection Findings` JSON block. If parsing the GuardKit
  prose grows complex, prefer a simple regex pass + JSON-block detection
  over a full parser
- The whole function body is wrapped in `try/except Exception as exc:` —
  on any internal error, return `GuardKitResult(status="success", …)`
  with a `parser_unrecognised_shape` warning describing the exception,
  matching the ADR-ARCH-025 contract that the parser **never raises**
- Slicing the tail: `stdout.encode("utf-8")[-4096:].decode("utf-8", errors="ignore")`
- Do **not** import the subprocess wrapper here — pure function on its
  inputs

## Seam Tests

This task is a consumer of the GuardKit subprocess output shape (an external
contract owned by the GuardKit project). Add a parametrised test capturing
the canonical shape from a real `guardkit --version` invocation if available
in the dev environment, plus golden-output fixtures for the documented
patterns above. Mark as `@pytest.mark.seam` with
`@pytest.mark.integration_contract("guardkit_output_shape")`.
