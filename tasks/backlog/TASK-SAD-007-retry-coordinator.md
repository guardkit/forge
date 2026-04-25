---
id: TASK-SAD-007
title: "Retry coordinator: fresh correlation, additional context, sibling resolution record"
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 3
implementation_mode: task-work
complexity: 5
dependencies: [TASK-SAD-001, TASK-SAD-006]
tags: [dispatch, retry, reasoning-model-driven, sibling-record]
consumer_context:
  - task: TASK-SAD-001
    consumes: CapabilityResolution
    framework: "Pydantic v2"
    driver: "in-memory + persisted via TASK-SAD-002"
    format_note: "Reads the prior attempt's resolution_id and writes a sibling record with retry_of=<prev_resolution_id>. Append-only retry_of field from TASK-SAD-001."
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Retry coordinator — fresh correlation, additional context, sibling resolution record

## Description

Implement the retry path for soft failures (specialist error, malformed
envelope, timeout). The retry policy is **reasoning-model-driven**: there is
no fixed max-retry at the dispatch layer (ASSUM-005). The reasoning loop
decides when to retry and what additional context to add. The retry
coordinator's job is to execute that decision correctly:

1. Generate a **fresh correlation** (no reuse — distinct from the original
   attempt's correlation key).
2. **Append the additional context** to the dispatch parameters.
3. Persist a **sibling resolution record** linked via `retry_of=<prev_resolution_id>`
   (do NOT overwrite the original record).
4. Increment `attempt_no`.

Implements scenario A.retry-with-additional-context.

## Interface

```python
# src/forge/dispatch/retry.py
from forge.dispatch.orchestrator import DispatchOrchestrator
from forge.dispatch.persistence import DispatchParameter
from forge.dispatch.models import DispatchOutcome


class RetryCoordinator:
    """Reasoning-model-driven retry. No fixed max-retry at this layer.

    Each retry creates a sibling CapabilityResolution record linked via
    retry_of=<prev_resolution_id>. The reasoning loop chooses whether to
    invoke retry and what additional context to attach.
    """

    def __init__(self, orchestrator: DispatchOrchestrator) -> None: ...

    async def retry_with_context(
        self,
        *,
        previous_outcome: DispatchOutcome,
        capability: str,
        original_parameters: list[DispatchParameter],
        additional_context: list[DispatchParameter],
    ) -> DispatchOutcome:
        """Re-dispatch with a fresh correlation and additional context.

        - previous_outcome supplies resolution_id (used as retry_of) and
          attempt_no (used to compute the new attempt's attempt_no).
        - The orchestrator generates a fresh correlation key (it does not
          reuse the prior one).
        - The sibling resolution record is created via the orchestrator's
          normal persist step.
        """
        next_attempt_no = previous_outcome.attempt_no + 1
        retry_of_id = previous_outcome.resolution_id
        merged_params = original_parameters + additional_context
        return await self.orchestrator.dispatch(
            capability=capability,
            parameters=merged_params,
            attempt_no=next_attempt_no,
            retry_of=retry_of_id,
        )
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/retry.py` defines `RetryCoordinator` with
      `retry_with_context()`.
- [ ] No fixed max-retry count at this layer. The retry coordinator does
      NOT count attempts and does NOT refuse to retry. Document this in
      the module docstring.
- [ ] Test (A.retry-with-additional-context): retry call's resulting
      `DispatchOutcome` carries `attempt_no = previous_outcome.attempt_no
      + 1` and the new resolution record's `retry_of` field equals the
      previous resolution's `resolution_id`.
- [ ] Test (fresh correlation): the new dispatch attempt's correlation
      key is distinct from the previous attempt's. Verify via the
      registry's binding map.
- [ ] Test (sibling not overwrite): after retry, BOTH resolution records
      exist in persistence — the original AND the sibling. The original
      is unchanged.
- [ ] Test (additional context propagation): parameters list passed to
      `orchestrator.dispatch()` is `original + additional_context` in
      order; original parameters are not mutated.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Tests

```python
"""Seam test: verify CapabilityResolution sibling-record contract."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("CapabilityResolution")
async def test_retry_creates_sibling_record_not_overwrite(
    orchestrator, retry_coordinator, db_writer
):
    """Verify retry preserves the original record and creates a sibling.

    Contract: CapabilityResolution.retry_of from TASK-SAD-001.
    """
    first = await orchestrator.dispatch(capability="review", parameters=[])
    rows_before = db_writer.row_count()
    await retry_coordinator.retry_with_context(
        previous_outcome=first,
        capability="review",
        original_parameters=[],
        additional_context=[],
    )
    rows_after = db_writer.row_count()
    assert rows_after == rows_before + 1
    siblings = db_writer.find_by_retry_of(first.resolution_id)
    assert len(siblings) == 1
```

## Implementation Notes

- Do NOT add retry-policy logic here (no exponential backoff, no jitter,
  no rate limit). The reasoning loop owns retry policy. This module
  executes the decision; it does not make the decision.
- Do NOT mutate the original `parameters` list. Use list concatenation
  to produce the new parameters list.
- The `attempt_no` increment and `retry_of` linkage are the only
  semantically-significant transformations this coordinator applies.
- Edge case: if the reasoning loop calls retry on a `Degraded` outcome,
  the orchestrator will re-resolve and may produce a different `Degraded`
  (e.g., a new specialist may have joined). This is the correct behaviour
  per D.cache-freshness-on-join — do not short-circuit.
