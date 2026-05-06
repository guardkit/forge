---
id: TASK-FRR-PEB-014
title: "ASSUM-009 contract-lock test (no-op under Option C; insurance against option flip)"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: low
task_type: testing
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 5
implementation_mode: direct
complexity: 3
estimated_minutes: 30
dependencies:
  - TASK-FRR-PEB-004
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - assum-009-contract-lock
  - option-flip-insurance
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: ASSUM-009 contract-lock test (no-op under Option C; insurance against option flip)

## TL;DR

Lock the cross-process correlation-id mismatch contract should the option
choice ever flip from C to D/E. Under Option C (the ratified choice), the
bridge runs in-forge and reuses `BuildContext.correlation_id` directly —
the F010C AST guard verifies (statically) that no `_safe_publish_*` call
omits `correlation_id=`. So this scenario is a **no-op test** that
documents the contract and would catch any future regression that
introduces a path bypassing the AST guard.

This is cheap insurance: 3 complexity, 1 file, ~30 min. If a future
review flips the option to D/E, this test is upgraded to a real
cross-process validator (per scoping doc §Cross-cutting #4 line 797–799).

ASSUM-009 / Q5 commitment under Option C.

## Locks BDD scenarios

- @edge-case @regression `An in-sidecar emit carrying a correlation
  identifier that does not match the registered build is rejected`
  (ASSUM-009 — under Option C, this is a no-op contract lock)

## Acceptance criteria

- AC-1: A new test file
  `tests/forge/lifecycle_bridge/test_correlation_id_contract_lock.py`
  contains a single test that:
  1. Constructs a `BuildContext` with correlation-id `"A"`.
  2. Constructs a `StreamPart` event that the translator (T3) would
     normally accept.
  3. Asserts that `StreamEventTranslator.translate()` produces an
     envelope with `correlation_id == "A"` (sourced from the
     `BuildContext`, not from the event).
  4. Asserts that there is no code path in the bridge that would
     accept a correlation-id from the SSE event itself (the translator
     reads from `BuildContext` only).
- AC-2: A docstring at the top of the file explicitly notes:
  > Under the ratified Option C, the bridge runs in-forge and the
  > correlation-id source is `BuildContext`, not the SSE event. This
  > test locks that contract. If a future review flips the option to
  > D or E, this test must be upgraded to a real cross-process
  > validator that rejects in-receive emits whose correlation-id does
  > not match the registered build.
- AC-3: The test uses `inspect.getsource()` on
  `StreamEventTranslator.translate()` and asserts that no occurrence
  of `correlation_id=stream_part.` (or similar pattern reading from
  the event) appears in the source. This is a static-analysis
  invariant equivalent to the F010C AST guard, scoped to the
  translator.
- AC-4: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

This task **is** a test. The acceptance criteria are the test.

- Test passes under the current implementation (no path reads
  correlation-id from the SSE event).
- Test would fail if a future contributor added a fallback like
  `correlation_id = stream_part.event_data.get("correlation_id", context.correlation_id)`.

## Implementation notes

- Touchpoints:
  `tests/forge/lifecycle_bridge/test_correlation_id_contract_lock.py`
  (new, single file).
- This is `direct` mode + `testing` task_type — minimal scope; no
  design or architectural review needed.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_correlation_id_contract_lock.py -x -v
ruff check tests/forge/lifecycle_bridge/test_correlation_id_contract_lock.py
```
