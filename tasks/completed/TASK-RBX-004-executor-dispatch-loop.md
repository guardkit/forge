---
id: TASK-RBX-004
title: Executor dispatch loop (core)
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: feature
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 3
implementation_mode: task-work
complexity: 7
estimated_minutes: 120
dependencies:
- TASK-RBX-001
- TASK-RBX-003
consumer_context:
- task: TASK-RBX-001
  consumes: handler_outcome
  framework: in-process StepTypeRegistry.resolve -> StepHandler(step) -> StepOutcome
  driver: forge.executor.registry
  format_note: resolve() may return None (escalate, never crash); a handler that raises
    is contained and mapped to StepStatus.failed
- task: TASK-RSP-004
  consumes: terminal_pointer
  framework: forge.persistence.repositories.runbook.RunbookRepository.advance
  driver: sqlite3 (STRICT), BEGIN IMMEDIATE
  format_note: advance() must allow current_step_index to reach == step_count (terminal);
    R1 reconciliation. Refuses only > count.
- task: TASK-RSP-004
  consumes: persistence_repo_surface
  framework: RunbookRepository.load_runbook / update_step_status / advance
  driver: sqlite3 (STRICT), BEGIN IMMEDIATE
  format_note: result is persisted via update_step_status(..., result=) BEFORE advance();
    on resume a step already 'passed' at the pointer is advanced without re-running
tags:
- forge
- runbook
- executor
- core
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-22T08:31:17.048221'
  last_updated: '2026-06-22T08:48:07.895524'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T08:31:17.048221'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Executor dispatch loop (core)

## TL;DR

The heart of the feature. `RunbookExecutor.run(runbook_id)` loads a persisted
runbook, walks steps from `current_step_index`, dispatches each to its
registered handler, persists **result before advancing**, and announces the
lifecycle. Stops on the first failure / unknown handler / approval gate and
escalates; a later run resumes at the stopped step without restarting.

## Scope

New module `src/forge/executor/executor.py`.

- **`RunbookExecutor(repository, registry, publisher)`** — composes the
  FEAT-RSP `RunbookRepository`, the `StepTypeRegistry` (TASK-RBX-001), and the
  `RunbookPublisher` (TASK-RBX-003). No other collaborators.
- **`run(runbook_id, *, correlation_id) -> RunResult`**:
  1. `load_runbook`. **Refuse** an empty runbook (`step_count == 0`) before any
     event — nothing to execute (**ASSUM-006**).
  2. If `current_step_index == step_count` → **already complete** no-op: no
     handler runs, no `runbook-started`/`runbook-complete`; report "already
     complete" (**ASSUM-005**).
  3. Announce `runbook-started` once.
  4. Loop from `current_step_index` to `step_count - 1`:
     - **Recovery shortcut:** if the step at the pointer is already `passed`
       (crash after result-commit, before advance), `advance()` without
       re-running (idempotent — Data-Integrity scenarios).
     - Announce `step-started`.
     - `handler = registry.resolve(step.step_type)`. If `None` →
       `update_step_status(failed?)` **no** (do not mark passed); announce
       `escalated(reason=unknown_handler)`; **stop** (ASSUM-002).
     - Run the handler. A raised exception is **contained**: map to
       `StepOutcome(status=failed)`, never propagate (ASSUM-008; executor
       "stops cleanly rather than crash").
     - Map outcome:
       - `passed` → `update_step_status(passed, result)` **then** `advance()`;
         announce `step-result(success)`; continue.
       - `failed` → `update_step_status(failed, result)`; announce
         `step-result(failure)` + `escalated(reason=step_failed)`; **stop**
         (pointer rests on the failed step — ASSUM-001).
       - `awaiting_approval` → `update_step_status(awaiting_approval, result)`;
         announce `escalated(reason=awaiting_approval)`; **pause/stop** (later
         steps do not run — ASSUM-003).
  5. When the loop completes (pointer reaches `step_count`), announce
     `runbook-complete` once.
- **Ordering invariant (data integrity):** `update_step_status(result)` commits
  **before** `advance()`. A crash in the gap leaves the pointer on a `passed`
  step → the recovery shortcut handles it on the next run; no step is ever
  advanced past without its result persisted.
- **Publish failures never roll back:** wrap each `publisher.*` call so
  `PublishFailure` is caught + logged and the run continues / persists
  regardless (ASSUM-009-exec).
- Add a small `RunResult` (overall `status`, `stopped_at_index | None`,
  `reason | None`) for the CLI to report.

`StepStatus`, `Step`, `Runbook` come from FEAT-RSP. Do not redefine.

## Acceptance Criteria

- [ ] Running a 3-step runbook runs each handler exactly once, in sequence,
      and completes (Key Example "runs each step in sequence to completion").
- [ ] A completed step is recorded `passed` and its result persisted (Key
      Example "status and result persisted").
- [ ] The resume pointer advances past each completed step and comes to rest at
      `step_count` (Key Example "resume pointer advances"; R1 terminal marker).
- [ ] Lifecycle order is `runbook-started` (×1), then `step-started` →
      `step-result` per step in order, then `runbook-complete` (×1) (Key
      Example "announces the lifecycle").
- [ ] A single-step runbook runs it once and completes (Boundary).
- [ ] A runbook resumed on its final step runs only that step; earlier handlers
      do not re-run (Boundary "resumed at its final step").
- [ ] An already-complete runbook (`pointer == count`) runs no handler and is
      reported already complete; no lifecycle events (Boundary + ASSUM-005).
- [ ] An empty runbook is refused before execution; no lifecycle events
      (Boundary/Negative + ASSUM-006).
- [ ] A step whose `step_type` has no handler stops the run, announces
      `escalated`, and is not recorded `passed` (Negative + ASSUM-002).
- [ ] A failing step stops the run, is recorded `failed`, later handlers do not
      run, the run stops (Edge "failing step stops the run").
- [ ] After a failure, re-running resumes at the failed step (now succeeding)
      without re-running earlier steps (Edge "re-running resumes at the failed
      step").
- [ ] A failing step escalates and `runbook-complete` is **not** announced
      (Edge).
- [ ] The pointer rests on the failed step after a failure; re-run resumes
      there (Edge "pointer rests on the failed step").
- [ ] A run interrupted after a step committed resumes at the next step (Edge
      "interrupted after a step completes").
- [ ] A handler that raises is contained: step recorded `failed`, `escalated`
      announced, later handlers do not run, executor does not crash (Security
      "handler that raises is contained").
- [ ] A step resolving to `awaiting_approval` pauses the run, records the step
      `awaiting_approval`, announces `escalated`, later handlers do not run
      (Integration "step that requires approval"; ASSUM-003).
- [ ] `step-result` reports each step's actual outcome (success vs failure)
      (Edge "step-result announcement reports the outcome").
- [ ] A failing event stream does not roll back persisted progress; every step
      still runs and persists and the runbook still completes (Edge "failure to
      announce events"; ASSUM-009-exec).
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.
- [ ] Unit tests use in-memory fake handlers (no subprocess, no broker) and are
      added under `tests/forge/executor/test_executor.py`, written
      **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/executor/test_executor.py -q
python -m pytest tests/forge/executor/test_executor.py -q -m seam
```

## §4 Seam Tests

Validates the `terminal_pointer` contract from TASK-RSP-004 (R1 reconciliation)
and the result-before-advance ordering.

```python
"""Seam test: verify terminal_pointer contract + write ordering (TASK-RSP-004)."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("terminal_pointer")
def test_advance_reaches_terminal_position(tmp_path) -> None:
    """advance() must rest the pointer at current_step_index == step_count.

    Contract: R1 — the terminal position one past the last step is allowed;
    it is the runbook's completion marker. Advancing beyond count is refused.
    Producer: TASK-RSP-004 (amended)
    """
    from forge.persistence.repositories.runbook import RunbookRepository
    # Build a single-step persisted runbook, advance once -> pointer == 1 == count.
    repo = RunbookRepository(tmp_path / "forge.db")
    # ... create_runbook(one step), advance(...) ...
    # rb = repo.load_runbook(runbook_id, correlation_id="c")
    # assert rb.current_step_index == rb.step_count  # terminal marker
    pytest.skip("wire against the amended FEAT-RSP advance() once built")


@pytest.mark.seam
@pytest.mark.integration_contract("persistence_repo_surface")
def test_result_committed_before_pointer_advances() -> None:
    """The executor calls update_step_status(result=) before advance().

    Contract: a crash in the gap must leave the pointer on a 'passed' step,
    never advanced past a step whose result is unpersisted.
    Producer: TASK-RSP-004
    """
    # Spy repository records call order; assert update_step_status precedes
    # advance for each passed step.
    pytest.skip("implement with a call-order spy over the repository surface")
```

## Implementation Notes

- The recovery shortcut (advance-without-run for an already-`passed` step at the
  pointer) is what makes resume idempotent across a crash in the
  commit→advance gap; without it, a resumed run would re-execute a completed
  step's handler.
- Escalation is announced for all three triggers (`unknown_handler`,
  `step_failed`, `awaiting_approval`) but the persisted effect differs:
  `failed` is written for failure/raise; the step keeps its prior status for an
  unknown handler (it was never run).
- Keep the executor **sync or async** consistent with the repository surface;
  the publisher is async — if the executor is sync, drive publishes via the
  same loop the rest of forge uses (see `pipeline` callers of
  `PipelinePublisher` for the established pattern).
