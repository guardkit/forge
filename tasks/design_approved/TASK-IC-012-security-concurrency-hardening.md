---
complexity: 4
created: 2026-04-25 14:36:00+00:00
dependencies:
- TASK-IC-011
estimated_minutes: 90
feature_id: FEAT-FORGE-006
id: TASK-IC-012
implementation_mode: task-work
parent_review: TASK-REV-IC8B
priority: high
status: design_approved
tags:
- security
- concurrency
- data-integrity
- hardening
task_type: testing
title: Security and concurrency scenario hardening
updated: 2026-04-25 14:36:00+00:00
wave: 6
---

# Task: Security and concurrency scenario hardening

## Description

Once the BDD suite from TASK-IC-011 is green, perform a focused hardening
pass on the eight high-leverage scenarios that defend against silent failure
modes:

- `@security security-working-directory-allowlist`
- `@security security-env-only-credentials`
- `@security secrets-appearing-in-rationale-text-are-redacted`
- `@security filesystem-read-allowlist`
- `@security priors-as-argument-refusal`
- `@negative negative-disallowed-binary-refused`
- `@concurrency split-brain-mirror-dedupe`
- `@concurrency recency-horizon-bound`
- `@data-integrity supersession-cycle-rejection`

For each, add explicit defence-in-depth tests that go beyond the BDD
binding: e.g. fuzz tests on the redaction regex set, race-condition tests
for split-brain dedupe, depth-cap stress tests for cycle detection.

## Module: `tests/hardening/`

```
tests/hardening/
├── test_redaction_fuzz.py            # property-based fuzz on redact_credentials()
├── test_subprocess_allowlist_fuzz.py # try every binary outside ALLOWED_BINARIES
├── test_working_dir_traversal.py     # ../../../etc/passwd attempts rejected
├── test_split_brain_race.py          # two concurrent writers, assert dedupe
├── test_recency_horizon_boundary.py  # at-the-boundary timestamps included/excluded
├── test_priors_no_argv_leak.py       # property test: no priors text in any subprocess argv
└── test_supersession_chain_stress.py # chains of length 9, 10, 11; cycle detection at depth 10
```

## Acceptance Criteria

- [ ] Hypothesis (or pytest-style param) fuzz tests on `redact_credentials()`
      with 1000+ random inputs assert no original credential text leaks to
      output (positive matches always redacted)
- [ ] Working-directory traversal attempts (`../`, absolute paths,
      symlinks) are rejected by the execute-tool allowlist check
- [ ] Disallowed-binary fuzz: a parameterised list of 50+ common binary
      names (`bash`, `sh`, `python`, `curl`, `wget`, `rm`, `cat`, ...) all
      raise the allowlist error
- [ ] Split-brain race test spawns two concurrent `write_session_outcome()`
      calls; assert exactly one entity exists post-write
- [ ] Recency horizon boundary: priors at exactly `horizon_days` are
      included; at `horizon_days + 1µs` are excluded (deterministic
      boundary)
- [ ] Property test: for any randomly-generated `Priors` object, no field
      content appears in `sys.argv` of any subprocess invocation during
      build
- [ ] Supersession chain of depth 10 succeeds; depth 11 raises clean error;
      cycle of any length raises immediately
- [ ] All hardening tests pass in CI as part of the standard test command
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `pytest tests/hardening/` passes
- [ ] Hardening tests integrated into CI; failure should block merge

## Implementation Notes

- Use `hypothesis` for fuzz tests (already a common dev dep; add to
  `pyproject.toml` `[project.optional-dependencies] test` if missing).
- The split-brain race test is fundamentally non-deterministic; use
  `asyncio.gather()` with two writers and assert on the post-state, not
  on the timing.
- The disallowed-binary fuzz list should include shell builtins and common
  utility binaries; treat the test as documenting "things that explicitly
  cannot run in the worktree" — useful as a security assertion in code
  review.
- This unit ships only tests; no production code changes. If a hardening
  test reveals a production bug, the fix lands in the responsible unit
  (1-10), not here.