---
id: TASK-SAD-004
title: 'Timeout coordinator: hard cut-off, unsubscribe-on-timeout, late-reply suppression'
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-SAD-001
tags:
- dispatch
- timeout
- asyncio
- deterministic-clock
- les1
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-003
  base_branch: main
  started_at: '2026-04-25T16:36:39.052460'
  last_updated: '2026-04-25T16:48:53.951128'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T16:36:39.052460'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Timeout coordinator — hard cut-off, unsubscribe-on-timeout, late-reply suppression

## Description

Implement the local hard-timeout cut-off for one dispatch attempt (default
900 seconds — ASSUM-003) and the unsubscribe-on-timeout cleanup. After the
hard timeout fires, any reply that arrives later must be silently dropped —
the dispatch is already "failed" from the reasoning loop's perspective and a
late reply must not retroactively change the outcome.

Implements scenarios:
- B.just-inside-local-timeout (reply arrives just before timeout — accepted)
- B.just-outside-local-timeout (reply arrives just after timeout — ignored)
- D.unsubscribe-on-timeout (subscription is released)

## Interface

```python
# src/forge/dispatch/timeout.py
import asyncio
from typing import Optional

from forge.discovery.protocol import Clock
from forge.dispatch.correlation import CorrelationRegistry, CorrelationBinding
from forge.dispatch.models import DispatchError


class TimeoutCoordinator:
    """Wrap a per-binding wait_for_reply with a hard timeout.

    Delegates subscription release to CorrelationRegistry.release().
    Uses an injected Clock for deterministic boundary tests.
    """

    def __init__(
        self,
        registry: CorrelationRegistry,
        clock: Clock,
        default_timeout_seconds: float = 900.0,
    ) -> None:
        ...

    async def wait_with_timeout(
        self,
        binding: CorrelationBinding,
        timeout_seconds: Optional[float] = None,
    ) -> Optional[dict]:
        """Wait for the authentic reply or until the timeout fires.

        Returns the payload dict on success, None on timeout.
        Always releases the binding before returning (success OR timeout).

        Late replies are suppressed by registry.release() rather than by
        any timer in this coordinator — the registry is the single source
        of truth for "is this binding still accepting replies".
        """
        ...
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/timeout.py` defines `TimeoutCoordinator` with
      `wait_with_timeout`.
- [ ] Default timeout is 900 seconds (ASSUM-003); overridable per call.
- [ ] Uses injected `Clock` (already in `forge.discovery.protocol`) for
      deterministic boundary tests against `FakeClock`.
- [ ] `wait_with_timeout` calls `registry.release(binding)` in a `finally`
      block so the subscription is released on success AND on timeout.
- [ ] Test (B.just-inside-local-timeout): reply arriving 1 tick before the
      hard cut-off is accepted.
- [ ] Test (B.just-outside-local-timeout): reply arriving 1 tick after the
      hard cut-off returns None and the late payload is dropped without
      reaching the gating layer.
- [ ] Test (D.unsubscribe-on-timeout): after timeout, asserting that the
      registry's `bindings` map no longer contains the correlation key.
- [ ] No use of `asyncio.sleep()` for the timeout itself — use
      `asyncio.timeout()` (Python 3.11+) or `wait_for(..., timeout=...)`
      so cancellation is correct under task cancellation.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Implementation Notes

- The timeout coordinator does NOT own the subscription. `CorrelationRegistry`
  does. This separation lets us prove the unsubscribe-on-timeout invariant
  by inspecting only the registry's state in tests.
- `Clock.now()` is monotonically increasing in production and freezable in
  tests. Use it for the start-of-wait timestamp. Do not use
  `asyncio.get_event_loop().time()` — that bypasses the injected clock.
- A separate "specialist-side advisory" timeout (600s — ASSUM-002) is the
  specialist's concern, not Forge's. Forge enforces only the hard 900s
  ceiling. Document this in the module docstring so a future reader does
  not conflate the two.
