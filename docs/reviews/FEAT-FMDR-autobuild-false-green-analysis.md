# FEAT-FMDR AutoBuild Run — False-Green / Premature-Stall Analysis

**Date:** 2026-06-23
**Run:** `guardkit autobuild feature FEAT-FMDR` (SDK harness, DEBUG)
**Outcome reported by AutoBuild:** TASK-FMDR-001 `unrecoverable_stall` (3 turns); TASK-FMDR-002 incomplete (operator-stopped mid turn-3); waves 2–3 (003/004/005) never reached.
**Actual code state at stop:** ✅ Green — 34/34 tests pass in ~0.2s; deliverables for 001 and 002 complete.

## TL;DR

The AutoBuild reported failure for **harness/environment reasons, not code reasons.** The Player did converge the implementation to a passing state, but the Coach's validation harness repeatedly misread a *hanging test* (in early-turn versions of the suite) as an *"absent test signal"* and applied a "narrative false-green" override. Three such results in a row tripped the context-pollution guard, which killed TASK-FMDR-001 even though its code was, by then, green.

## Evidence

- Coach isolated runs logged `Isolated test execution timed out after 60s` with `tests_run=0 tests_failed=0`, then:
  `Reconciling quality_gates ... test-orchestrator status=failed (error=absent test signal ... timed out after 60s, tests_run=0) but quality_gates claimed ... tests_passing=True — overriding to NOT passed (narrative false-green).`
- TASK-FMDR-001 termination:
  `Context pollution detected: 3 consecutive test failures in turns [1, 2, 3]` →
  `Unrecoverable stall detected ... no passing checkpoint exists. Exiting loop early.`
- Running the **same** Coach suite directly against the final worktree:
  `pytest tests/forge/test_cli_runbook.py tests/forge/test_runbook_exemplar.py` → **34 passed in 0.18s.**
- NATS was **not** the cause: `127.0.0.1:4222` is open (GB10 JetStream, reachable locally), and `_connect_nats_best_effort()` uses `connect_timeout=2` with a `_NoOpNATSClient` fallback. Connect either succeeds in ms or fails in 2s — it cannot produce a 60s hang.

## Root cause

1. **No per-test timeout in the Coach's pytest invocation.** The isolated run is `pytest <files> -v --tb=short` with no `--timeout`. An early-turn test version that blocked (real connection/subprocess before mocks were added) consumed the entire 60s isolated budget and produced **no parseable result** (`tests_run=0`).
2. **`tests_run=0 + timeout` is classified as "absent test signal" → false-green override.** That conflates two very different situations: (a) *infra/SDK transport timeout* (genuinely no signal) and (b) *a specific test hung* (a real, attributable FAILURE). Case (b) should surface as a named failing test, not a generic "absent signal."
3. **The context-pollution guard counted those harness timeouts as real failures** and declared `unrecoverable_stall` with no passing checkpoint — terminating a task whose implementation was converging to green.

Contributing factor: this repo had **no default pytest timeout**, so nothing locally bounded a hanging test either.

## Repo-side fix attempt — tried and REVERTED

Initially added `addopts = "--timeout=30 --timeout-method=thread"` to
`pyproject.toml [tool.pytest.ini_options]`. **This was reverted** — it backfired:
a global `addopts` requires `pytest-timeout` to be importable by *every*
interpreter that runs pytest, and the AutoBuild worktree `.venv` does **not**
install it (only the system python had it, which is why the initial local
verification misleadingly passed). The result: every pytest invocation in the
worktree — the BDD runner *and* the Coach's independent run — failed with
`error: unrecognized arguments: --timeout=30 --timeout-method=thread`, producing
a *new* false-green on TASK-FMDR-003 turn 1.

**Lesson:** bounding test hangs is the **harness's** job (it owns the subprocess
that runs pytest), not a repo-wide pytest arg that couples the whole suite to a
plugin being present in every environment. The repo-side knob is left out; the
fix lives in guardkit `TASK-ABFIX-010`.

## Harness-side fix (filed)

Tracked in **guardkit `TASK-ABFIX-010`** (follow-up to FEAT-CD4C / TASK-ABFIX-005 parallel-wave Coach isolation):

1. Inject a per-test timeout into the Coach's isolated pytest command so a single hanging test yields a real FAILED result instead of consuming the whole budget with `tests_run=0`.
2. Distinguish *infra/SDK transport timeout* from *a test that hung*; only the former is "absent test signal."
3. Require evidence of genuine (non-timeout) failures before the context-pollution guard declares `unrecoverable_stall`, so a converging task isn't killed by harness timeouts.

## Process observations (non-blocking)

- TASK-FMDR-001 and -002 ran in the **same wave editing overlapping files** (`tests/forge/test_cli_runbook.py`, `src/forge/cli/runbook.py`) in one shared worktree, and the Coach ran the *combined* suite for both — so a hang from either failed both. Consider sequencing tasks that touch the same files.
- Repeated honesty flags (`claim_audit_unmodified`, should_fix) on both tasks — the deterministic honesty layer worked correctly; worth confirming the Player guidance on audit-file edits.
- Graphiti/FalkorDB `bound to a different event loop` errors recurred during Coach context loading. Non-fatal (warns and continues) but noisy; separate issue.

## Salvage decision

001/002 deliverables are green and being salvaged manually (review + mark complete). 003/004 (scenario suite, disposable-compose e2e) were never started and will be handled separately.
