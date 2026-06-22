---
id: TASK-RBX-008
title: "Harden executor↔persistence result contract (reconcile model + real-repo seam test)"
status: completed
created: 2026-06-22T00:00:00Z
updated: 2026-06-22T00:00:00Z
completed: 2026-06-22T00:00:00Z
completed_location: tasks/completed/TASK-RBX-008-harden-result-contract.md
priority: medium
task_type: refactor
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
complexity: 4
dependencies: []
tags:
  - forge
  - runbook
  - executor
  - tech-debt
---

# Harden executor↔persistence result contract

## Background

FEAT-RBX shipped with the executor **omitting** the step `result` on every
`update_step_status` call — results were announced on the event stream but never
persisted. Root causes:

1. **Model mismatch.** The handler's `StepOutcome.result` is a free-form
   JSON-serialisable `dict`, but the persistence `StepResult` is a strict
   dataclass (`exit_code`, `captured_output`, `started_at`, `completed_at`).
   The original author left `result=` off with a "type mismatch" comment.
2. **Fake-only testing.** The executor's unit tests run against an in-memory
   fake repo, so neither this omission **nor** the missing
   `try_claim_step_for_execution` method (the concurrency seam gap) was caught
   until the autobuild run failed and a human ran the real repo.

The immediate omission was fixed in `f26433c` with an adapter
(`RunbookExecutor._build_step_result`) that stuffs the handler dict into
`StepResult.captured_output` as JSON and derives `exit_code` from status. That
is a **stopgap**, not the clean model.

## Scope

- **Reconcile the result model** so a handler's structured result is a
  first-class persisted value rather than a JSON blob inside `captured_output`.
  Options to evaluate: (a) widen `Step.result` / `update_step_status` to accept
  a JSON `Mapping` end-to-end (round-tripped as-is), or (b) keep `StepResult`
  but give it a typed `payload: Mapping` field. Pick one; do not keep the
  captured_output-as-JSON stopgap long-term.
- **Add a real-repo seam test for the executor** (`-m seam`): run
  `RunbookExecutor` against a real `RunbookRepository` + tmp SQLite (not a fake)
  end-to-end, asserting status **and** result persist and round-trip. This is
  the regression guard that would have caught both the result omission and the
  missing claim method.

## Acceptance Criteria

- [x] A handler's structured result round-trips through persistence without the
      `captured_output`-as-JSON stopgap (or a documented decision to keep it).
- [x] A real-repo (non-fake) seam test exercises `RunbookExecutor.run` against
      `RunbookRepository` + SQLite and asserts status + result persistence.
- [x] The seam test would fail if `update_step_status(result=…)` were dropped or
      if `try_claim_step_for_execution` were missing from the repo.
- [x] All modified files pass project-configured lint/format checks.

## Resolution

Chose **option (b)**: `StepResult` gained a typed
`payload: Mapping[str, Any] | None = None` field. The handler's structured dict
is now a first-class persisted value, round-tripped verbatim through
`_encode_result`/`_decode_result` (legacy rows without the key decode to
`payload=None`). The executor's `_build_step_result` writes the dict to
`payload` with `captured_output=""` and a status-derived `exit_code`; the
`json.dumps`-into-`captured_output` stopgap (and the now-unused `json` import)
are gone. Executor-recorded `started_at`/`completed_at` timing metadata is
preserved.

Seam test: `tests/forge/executor/test_executor_seam.py` (`@pytest.mark.seam`,
`integration_contract("executor_result_contract")`) drives `RunbookExecutor.run`
against a real `RunbookRepository` + tmp SQLite and asserts status + payload
round-trip for passed/failed/multi-step runs, and that the step is observably
claimed (`running`) before dispatch — exercising the real
`try_claim_step_for_execution` seam.

Files: `src/forge/persistence/repositories/runbook_models.py`,
`src/forge/persistence/repositories/runbook.py`,
`src/forge/executor/executor.py`,
`tests/forge/executor/test_executor.py` (updated AC-002 assertion),
`tests/forge/executor/test_executor_seam.py` (new).

Verification: 167 passed, 1 skipped across the executor + persistence + runbook
BDD/CLI/fold suites; `ruff check` + `ruff format --check` clean on all five
files.

## Notes

- Reference: `src/forge/executor/executor.py` (`_build_step_result`, the
  passed/failed/awaiting persist paths), `src/forge/persistence/repositories/runbook.py`
  (`update_step_status`, `_encode_result`, `StepResult`).
- Relates to [[autobuild-cannot-edit-sibling-repos]] only tangentially; the core
  lesson here is "integration-test cross-feature seams against the real
  collaborator, not a fake."
