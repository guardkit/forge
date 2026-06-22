---
id: TASK-RBX-008
title: "Harden executor↔persistence result contract (reconcile model + real-repo seam test)"
status: backlog
created: 2026-06-22T00:00:00Z
updated: 2026-06-22T00:00:00Z
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

- [ ] A handler's structured result round-trips through persistence without the
      `captured_output`-as-JSON stopgap (or a documented decision to keep it).
- [ ] A real-repo (non-fake) seam test exercises `RunbookExecutor.run` against
      `RunbookRepository` + SQLite and asserts status + result persistence.
- [ ] The seam test would fail if `update_step_status(result=…)` were dropped or
      if `try_claim_step_for_execution` were missing from the repo.
- [ ] All modified files pass project-configured lint/format checks.

## Notes

- Reference: `src/forge/executor/executor.py` (`_build_step_result`, the
  passed/failed/awaiting persist paths), `src/forge/persistence/repositories/runbook.py`
  (`update_step_status`, `_encode_result`, `StepResult`).
- Relates to [[autobuild-cannot-edit-sibling-repos]] only tangentially; the core
  lesson here is "integration-test cross-feature seams against the real
  collaborator, not a fake."
