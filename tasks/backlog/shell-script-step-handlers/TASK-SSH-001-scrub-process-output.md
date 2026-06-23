---
id: TASK-SSH-001
title: scrub_process_output credential scrubber (DSN + password)
status: in_review
priority: high
task_type: feature
parent_review: TASK-REV-SSH1
parent_feature: FEAT-SSH
feature_slug: shell-script-step-handlers
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 50
dependencies: []
tags:
- forge
- runbook
- shell-step
- security
- redaction
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-SSH
  base_branch: main
  started_at: '2026-06-22T15:13:22.269724'
  last_updated: '2026-06-22T15:31:23.514984'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Independent test verification shows tests_passed: false, contradicting
      Player''s claim of tests_passed: true. However, the independent test raw_output
      shows only ''I''ll run the test command for you.'' with no actual pytest results,
      suggesting the test capture failed. Runtime parity shows exit_code: 0 (passed),
      creating conflicting signals.: Re-run independent test verification to obtain
      actual pytest output. The test command was ''pytest tests/forge/memory/test_scrub_process_output.py
      -v --tb=short'' which should produce detailed pytest output, not just a message.
      Verify whether tests actually failed or if this is a data collection issue.'
    timestamp: '2026-06-22T15:13:22.269724'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-06-22T15:23:37.715401'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-SSH-001 — `scrub_process_output` credential scrubber

## Context

The shell-script step handlers capture subprocess stdout/stderr that may contain
postgres connection strings and passwords (e.g. a deploy script echoing its env,
or `psql` printing a DSN on error). The existing
[`redact_credentials`](../../../src/forge/memory/redaction.py) covers only
GitHub tokens / bearer / hex and is scoped to Graphiti entity payloads — it has
**no** postgres-DSN or password coverage (see summary §"Key implementation note",
ASSUM-002/003/004).

This task adds a **sibling** scrubber `scrub_process_output` in the same module,
reusing its compiled-pattern + purity + idempotency contract. It is the single
credential-scrub site for all shell-step captured output (ASSUM-006).

## Scope

Add to `src/forge/memory/redaction.py`:

```python
def scrub_process_output(text: str) -> str: ...
```

Patterns (most-specific-first, idempotent):

| Pattern | Marker |
|---------|--------|
| `postgresql://…` and `postgres://…` DSNs (user:pass@host:port/db, optional query) | `***REDACTED-DSN***` |
| `password=<value>` (case-insensitive key) | `password=***REDACTED-PASSWORD***` |
| `PGPASSWORD=<value>` | `PGPASSWORD=***REDACTED-PASSWORD***` |

Order DSN-first so a DSN's embedded `:password@` is consumed by the DSN marker
before the bare `password=` pass runs.

## Acceptance Criteria

- [ ] `scrub_process_output(text: str) -> str` is added to
      `src/forge/memory/redaction.py` and exported via `__all__`.
- [ ] A `postgresql://user:pass@host:5432/db` DSN is replaced by
      `***REDACTED-DSN***`; the same holds for the `postgres://` scheme.
- [ ] `password=hunter2` → `password=***REDACTED-PASSWORD***` and
      `PGPASSWORD=hunter2` → `PGPASSWORD=***REDACTED-PASSWORD***`
      (key preserved, value redacted).
- [ ] The function is **idempotent**:
      `scrub_process_output(scrub_process_output(s)) == scrub_process_output(s)`
      for all `s` — output already containing a redaction marker is left
      unchanged (covers the `.feature` "already redacted" scenario).
- [ ] The function is **pure**: no I/O, no logging, no retention of the
      original value; non-`str` input raises `TypeError` (mirrors
      `redact_credentials`).
- [ ] Non-credential text (plain log lines, URLs that are not DSNs) is
      returned unchanged — no false positives on `http(s)://` URLs.
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Coach Validation

```bash
pytest tests/forge/memory/test_scrub_process_output.py -v
ruff check src/forge/memory/redaction.py
ruff format --check src/forge/memory/redaction.py
```

## Implementation Notes

- Mirror the existing module's structure: module-level compiled `re.Pattern`
  constants, fixed-string markers, ordered `.sub()` chain inside the function.
- Keep `redact_credentials` untouched — this is a sibling, not a rewrite. A
  future caller may compose both, but they remain independent functions.
- The DSN regex should tolerate optional `+driver` dialect suffixes
  (`postgresql+asyncpg://`) so async DSNs are also scrubbed.
