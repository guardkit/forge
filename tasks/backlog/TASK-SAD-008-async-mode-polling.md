---
id: TASK-SAD-008
title: "Async-mode polling: run-identifier handling, status-tool convergence"
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 4
implementation_mode: task-work
complexity: 4
dependencies: [TASK-SAD-006]
tags: [dispatch, async-mode, polling, run-identifier]
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Async-mode polling — run-identifier handling, status-tool convergence

## Description

Some specialist capabilities are long-running; the specialist's first reply
carries a `run_identifier` rather than a final result. Forge polls the
capability's status tool until a final outcome arrives. The polled status
tool is itself a regular dispatch — so polling reuses the orchestrator
rather than implementing a parallel code path.

The sync-reply path and the async-mode path **converge at the
`DispatchOutcome` level**: both produce the same outcome shape, so the
reasoning loop sees one contract.

Implements scenario D.async-mode-polling.

## Interface

```python
# src/forge/dispatch/async_polling.py
import asyncio
from forge.dispatch.orchestrator import DispatchOrchestrator
from forge.dispatch.models import DispatchOutcome, AsyncPending, SyncResult
from forge.discovery.protocol import Clock


class AsyncPollingCoordinator:
    """Convert AsyncPending → SyncResult / DispatchError by polling."""

    def __init__(
        self,
        orchestrator: DispatchOrchestrator,
        clock: Clock,
        poll_interval_seconds: float = 5.0,
        max_total_seconds: float = 900.0,  # ASSUM-003 ceiling applies here too
    ) -> None: ...

    async def converge(
        self,
        pending: AsyncPending,
        *,
        status_capability: str = "status",
    ) -> DispatchOutcome:
        """Poll until a final outcome arrives or the ceiling is hit.

        Each poll is a regular dispatch via the orchestrator (so it goes
        through the full subscribe-before-publish / exactly-once pipeline).
        The status tool's reply is parsed by the existing reply parser
        (TASK-SAD-005).

        Returns SyncResult on completion or DispatchError on
        ceiling-exceeded / repeated-pending.
        """
        ...
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/async_polling.py` defines `AsyncPollingCoordinator`
      with `converge()`.
- [ ] Each poll dispatches via the orchestrator — does NOT bypass
      subscribe-before-publish or any other invariant.
- [ ] Polling honours the same hard 900s ceiling as sync dispatch
      (ASSUM-003); cumulative time is tracked via the injected `Clock`.
- [ ] Test (D.async-mode-polling): initial reply with `run_identifier` →
      `AsyncPending`; subsequent status-tool reply with `coach_score` →
      `SyncResult`. The two are linked via the same `resolution_id`.
- [ ] Test (status-tool repeated pending): if the status tool keeps
      returning `AsyncPending`, polling continues until the ceiling, then
      emits `DispatchError(error_explanation="async_polling_ceiling_exceeded")`.
- [ ] Test (status-tool error): if the status tool replies with an
      error, the polling loop emits the `DispatchError` and stops.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Implementation Notes

- The `converge()` method is the single entry from the reasoning loop into
  the polling path. The pipeline_consumer's dispatch callback returns the
  `DispatchOutcome` from `orchestrator.dispatch()`; if it is `AsyncPending`,
  the consumer (or reasoning loop) calls `converge()`.
- `poll_interval_seconds` is constant; do NOT add adaptive backoff. The
  spec is silent on poll cadence and the simplest behaviour is the
  least-surprising one. If a future requirement demands backoff, add it
  then.
- Reuse `Clock` from `forge.discovery.protocol` for deterministic tests.
- The status capability name (`"status"` by default) is per-specialist
  convention. Keep it overridable for future specialists with different
  conventions.
