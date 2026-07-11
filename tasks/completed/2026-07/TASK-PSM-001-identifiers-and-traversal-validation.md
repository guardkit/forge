---
id: TASK-PSM-001
title: Identifiers and path-traversal validation
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 1
implementation_mode: direct
complexity: 3
estimated_minutes: 45
status: completed
dependencies: []
tags:
- lifecycle
- security
- identifiers
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T12:54:50.533690'
  last_updated: '2026-04-27T12:58:38.552700'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T12:54:50.533690'
    player_summary: "Created the `forge.lifecycle` package as the security boundary\
      \ for any string later interpolated into a worktree path or the `build_id` PRIMARY\
      \ KEY. `validate_feature_id` performs a double `urllib.parse.unquote` (catches\
      \ `%252F`-style double-encoded traversal), then enforces (in order): length\
      \ 1\u201364, no `\\x00` after decode, allowlist `[A-Za-z0-9_-]+`. Allowlist\
      \ failures are split into reason `traversal` (`..`, `/`, or `\\` after decode)\
      \ vs. `disallowed_char`. `InvalidIdentifierError` subclasses `V"
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-001: feature-rollup completed"
---

# Task: Identifiers and path-traversal validation

## Description

Create `src/forge/lifecycle/identifiers.py` with two helpers:

- `validate_feature_id(s: str) -> str` — decode-then-allowlist validator that
  catches `../`, URL-encoded variants (`%2F`, `%2E%2E`, double-encoded), null
  bytes, and any character outside the allowlist `[A-Za-z0-9_-]+`.
- `derive_build_id(feature_id: str, queued_at: datetime) -> str` — produces
  the canonical `build-{feature_id}-{YYYYMMDDHHMMSS}` form per
  `API-cli.md §3.3` and `API-sqlite-schema.md §2.1`.

This module is the security boundary for any identifier that is later
interpolated into a worktree path or the `build_id` PRIMARY KEY. It MUST
reject inputs robust to decode-bypass attacks.

Implements concern **sc_003** from the review: validation depth must catch
URL-encoded traversal sequences and null bytes.

## Acceptance Criteria

- [ ] `validate_feature_id("FEAT-FORGE-001")` returns `"FEAT-FORGE-001"`
- [ ] `validate_feature_id("../etc/passwd")` raises `InvalidIdentifierError`
- [ ] `validate_feature_id("%2E%2E%2Fetc")` raises `InvalidIdentifierError`
- [ ] `validate_feature_id("%252F")` raises `InvalidIdentifierError`
      (double-encoded `/`)
- [ ] `validate_feature_id("FEAT\x00")` raises `InvalidIdentifierError`
      (null byte after decode)
- [ ] `validate_feature_id("a" * 65)` raises `InvalidIdentifierError`
      (length cap 64)
- [ ] `validate_feature_id("")` raises `InvalidIdentifierError` (must be
      at least 1 character)
- [ ] `derive_build_id("FEAT-FORGE-001", datetime(2026, 4, 27, 12, 30, 45, tzinfo=UTC))`
      returns `"build-FEAT-FORGE-001-20260427123045"`
- [ ] `InvalidIdentifierError` is a subclass of `ValueError` with a
      structured `reason` attribute (one of: `traversal`, `null_byte`,
      `disallowed_char`, `length`)
- [ ] Unit-test coverage: every branch of the validator has at least one
      positive and one negative test case (≥ 95% line coverage on this
      module)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

```python
import re
from datetime import datetime
from urllib.parse import unquote

_ALLOWED = re.compile(r"[A-Za-z0-9_-]+")
_MAX_LEN = 64


class InvalidIdentifierError(ValueError):
    def __init__(self, value: str, reason: str) -> None:
        super().__init__(f"Invalid feature_id ({reason}): {value!r}")
        self.value = value
        self.reason = reason


def validate_feature_id(s: str) -> str:
    # Double-decode: catches %252F (%2F encoded again) → / → reject
    decoded = unquote(unquote(s))
    if not (1 <= len(decoded) <= _MAX_LEN):
        raise InvalidIdentifierError(s, "length")
    if "\x00" in decoded:
        raise InvalidIdentifierError(s, "null_byte")
    if not _ALLOWED.fullmatch(decoded):
        # Distinguish traversal from generic disallowed for better errors
        if ".." in decoded or "/" in decoded or "\\" in decoded:
            raise InvalidIdentifierError(s, "traversal")
        raise InvalidIdentifierError(s, "disallowed_char")
    return decoded


def derive_build_id(feature_id: str, queued_at: datetime) -> str:
    return f"build-{feature_id}-{queued_at.strftime('%Y%m%d%H%M%S')}"
```

## Coach Validation

- File exists at `src/forge/lifecycle/identifiers.py`
- Unit tests under `tests/unit/lifecycle/test_identifiers.py` exercise the
  full attack-vector matrix
- `lint/format` checks pass on the new module
- No external imports beyond stdlib (`re`, `datetime`, `urllib.parse`)
