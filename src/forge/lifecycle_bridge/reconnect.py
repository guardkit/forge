"""Reconnect policy for the SSE lifecycle bridge (TASK-FRR-PEB-008).

ASSUM-003 verified backoff schedule for the langgraph-runner SSE
observer task:

* Initial backoff: 1.0s
* Cap: 30.0s
* Exponential ×2 on each consecutive failure
* Reset to initial on successful reconnection
* No fixed maximum retry count — the loop terminates only on
  ``asyncio.CancelledError`` (operator cancel / daemon shutdown) or on
  the per-build deadline expiry (TASK-FRR-PEB-008 deadline timer in
  :mod:`forge.lifecycle_bridge.bridge`).

The reconnect schedule is deliberately identical to the JetStream
consumer's reconnect schedule in :mod:`forge.cli._serve_daemon`
(``RECONNECT_INITIAL_BACKOFF`` / ``RECONNECT_MAX_BACKOFF``) — the
operator-facing logs read symmetrically across both reconnect paths.

Tests monkey-patch :data:`RECONNECT_INITIAL_BACKOFF` and
:data:`RECONNECT_MAX_BACKOFF` to 0.05s for fast runs (precedent:
``tests/forge/test_cli_serve_daemon.py:364-367`` — see
TASK-FRR-PEB-008 AC-5).

The constants are read on every call to :meth:`ReconnectPolicy.next_backoff`
so a ``monkeypatch.setattr`` applied after the policy is constructed
takes effect immediately — no re-instantiation required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


__all__ = [
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "ReconnectPolicy",
]


#: Initial reconnect backoff after an SSE error (seconds). Reused
#: verbatim from :mod:`forge.cli._serve_daemon` per ASSUM-003.
RECONNECT_INITIAL_BACKOFF: float = 1.0

#: Cap on the exponential reconnect backoff (seconds). Reused verbatim
#: from :mod:`forge.cli._serve_daemon` per ASSUM-003.
RECONNECT_MAX_BACKOFF: float = 30.0


class ReconnectPolicy:
    """Exponential reconnect schedule with cap and reset semantics.

    Backoff sequence (with the default constants):
    ``1.0 → 2.0 → 4.0 → 8.0 → 16.0 → 30.0 → 30.0 → ...`` (capped).
    On a successful reconnect, :meth:`reset` returns the schedule to
    the initial value so the *next* outage starts again at
    :data:`RECONNECT_INITIAL_BACKOFF` rather than the capped ceiling.

    The schedule is keyed off the **module-level** constants. Tests
    that monkey-patch them — e.g.
    ``monkeypatch.setattr(reconnect, "RECONNECT_INITIAL_BACKOFF", 0.05)``
    — observe the patched values immediately without having to
    re-instantiate the policy.

    The class is intentionally not a context manager and not a thread-
    safe primitive: the SSE observer loop is single-task per build, so
    a plain mutable counter is the right shape.
    """

    def __init__(self) -> None:
        # ``None`` means "no failure yet — the next call returns the
        # current module-level :data:`RECONNECT_INITIAL_BACKOFF`". We
        # deliberately defer the read to call time so monkey-patches
        # applied between construction and first use are honoured.
        self._next: float | None = None

    @property
    def current_backoff(self) -> float:
        """Return the backoff the next :meth:`next_backoff` call will yield.

        Useful for log lines that want to surface the upcoming sleep
        without advancing the schedule.
        """
        if self._next is None:
            return RECONNECT_INITIAL_BACKOFF
        return self._next

    def next_backoff(self) -> float:
        """Return the current backoff and advance the schedule.

        Sequence with default constants:
        ``1.0 → 2.0 → 4.0 → 8.0 → 16.0 → 30.0 → 30.0 → ...``

        Once the schedule reaches :data:`RECONNECT_MAX_BACKOFF` further
        calls keep returning the cap (no fixed maximum retry count —
        AC-1 / ASSUM-003).
        """
        if self._next is None:
            current = RECONNECT_INITIAL_BACKOFF
        else:
            current = self._next
        # Advance for the *next* call. Capping with ``min`` means the
        # schedule plateaus at the cap rather than overflowing.
        self._next = min(current * 2.0, RECONNECT_MAX_BACKOFF)
        return current

    def reset(self) -> None:
        """Reset so the next :meth:`next_backoff` returns the initial value.

        Called on every successful reconnection — the next outage
        starts again from :data:`RECONNECT_INITIAL_BACKOFF` per
        ASSUM-003.
        """
        self._next = RECONNECT_INITIAL_BACKOFF

    async def sleep_then_advance(
        self,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> float:
        """Sleep the current backoff and advance the schedule.

        Returns the backoff that was actually slept so callers can log
        it. ``sleep_fn`` is a seam for tests that want to verify the
        sleep duration without actually waiting.
        """
        backoff = self.next_backoff()
        sleeper = sleep_fn if sleep_fn is not None else asyncio.sleep
        await sleeper(backoff)
        return backoff
