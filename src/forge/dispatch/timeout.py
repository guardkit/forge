"""Timeout coordinator for one dispatch attempt — TASK-SAD-004.

Enforces the local **hard cut-off** (default 900 seconds — ASSUM-003)
for one dispatch attempt and the **unsubscribe-on-timeout** cleanup.
After the hard timeout fires, any reply that arrives later is silently
dropped: the dispatch is already "failed" from the reasoning loop's
perspective and a late reply must not retroactively change the outcome.

Implements three BDD scenarios:

* ``B.just-inside-local-timeout`` — reply arriving 1 tick before the
  cut-off is accepted.
* ``B.just-outside-local-timeout`` — reply arriving 1 tick after the
  cut-off returns ``None`` and the late payload never reaches the
  gating layer.
* ``D.unsubscribe-on-timeout`` — the registry's ``bindings`` map no
  longer contains the correlation key after the timeout fires.

Design notes
------------

The timeout coordinator does **not** own the subscription. The
:class:`~forge.dispatch.correlation.CorrelationRegistry` does. This
separation lets us prove the unsubscribe-on-timeout invariant by
inspecting only the registry's state in tests.

Late-reply suppression is enforced by ``registry.release()`` rather
than by any timer in this coordinator — the registry is the single
source of truth for "is this binding still accepting replies".

A separate **specialist-side advisory** timeout (600s — ASSUM-002) is
the specialist's concern, not Forge's. Forge enforces only the hard
900s ceiling here. Do not conflate the two.

Clock semantics
---------------

The actual cut-off uses :func:`asyncio.timeout` (Python 3.11+) so
cancellation propagates correctly under task cancellation. The
injected :class:`~forge.discovery.protocol.Clock` is used to capture
the *start-of-wait* timestamp for diagnostic logging and audit — it
is intentionally **not** used as the timer source. ``Clock.now()``
returns a timezone-aware UTC ``datetime`` and is freezable in tests
via a ``FakeClock`` double; ``asyncio.timeout`` is driven by the
asyncio event loop. Tests requiring deterministic boundary behaviour
configure short real timeouts and arrange the binding's reply Future
either side of the cut-off.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from forge.discovery.protocol import Clock

if TYPE_CHECKING:  # pragma: no cover - import only for static type checking
    # The concrete CorrelationRegistry / CorrelationBinding live in
    # ``forge.dispatch.correlation`` (TASK-SAD-003). They are imported
    # only for type checking so this module can be exercised
    # independently of TASK-SAD-003 with structurally-typed test
    # doubles that satisfy :class:`_RegistryLike` / :class:`_BindingLike`.
    from forge.dispatch.correlation import (  # noqa: F401
        CorrelationBinding,
        CorrelationRegistry,
    )


logger = logging.getLogger(__name__)


# Hard local cut-off for one dispatch attempt, in seconds. ASSUM-003.
DEFAULT_TIMEOUT_SECONDS: float = 900.0

# Interim "still waiting" diagnostic point, in seconds. Purely an
# OBSERVABILITY seam (M8): a real PO/architect run takes minutes of LLM
# time, so a silent dispatch used to look identical to a hung one until
# the 900s cut-off fired. At this interim we emit a pointed warn naming
# the awaited subject / correlation / agent so an on-call reader can tell
# "slow but alive" from "nothing coming". This does NOT shorten the hard
# cut-off — the wait continues to the full ``DEFAULT_TIMEOUT_SECONDS``.
INTERIM_WARN_SECONDS: float = 60.0

# Diagnostic mirror of the reply subject the registry is awaiting on:
# nats-core ``Topics.Agents.RESULT`` == the adapter's
# ``RESULT_SUBJECT_TEMPLATE`` (``agents.result.{agent_id}``, 3-token, D2).
# Held here as a LOCAL format string — used only to compose human log
# lines — so the pure dispatch domain stays free of transport imports
# (the same layering discipline :mod:`forge.dispatch.correlation` keeps).
_RESULT_SUBJECT_FMT: str = "agents.result.{agent_id}"


@runtime_checkable
class _BindingLike(Protocol):
    """Structural surface of a correlation binding the coordinator touches.

    The coordinator reads ``correlation_key`` and ``matched_agent_id``
    for the pointed no-reply diagnostics (M8) — the awaited reply subject
    is derived from the agent id. Both the production
    :class:`~forge.dispatch.correlation.CorrelationBinding` and any
    test double satisfy this protocol structurally.
    """

    correlation_key: str
    matched_agent_id: str


@runtime_checkable
class _RegistryLike(Protocol):
    """Structural surface of the correlation registry the coordinator uses.

    The coordinator depends on exactly two methods:

    * ``wait_for_reply(binding)`` — returns the payload dict when an
      authentic reply arrives, or never returns if no reply ever
      comes (the coordinator imposes the hard cut-off externally).
    * ``release(binding)`` — idempotently tears down the binding's
      subscription so late replies are silently dropped.
    """

    async def wait_for_reply(self, binding: Any) -> Optional[dict]:
        ...

    def release(self, binding: Any) -> None:
        ...


class TimeoutCoordinator:
    """Wrap a per-binding ``wait_for_reply`` with a hard timeout.

    Delegates subscription release to
    :meth:`CorrelationRegistry.release`. Uses an injected
    :class:`Clock` for the start-of-wait timestamp so audit lines are
    deterministic in tests.

    The timer itself is :func:`asyncio.timeout` so cancellation
    propagates correctly when the surrounding task is cancelled
    (re-raises :class:`asyncio.CancelledError` after release).
    """

    def __init__(
        self,
        registry: _RegistryLike,
        clock: Clock,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        interim_warn_seconds: float = INTERIM_WARN_SECONDS,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError(
                "default_timeout_seconds must be strictly positive, "
                f"got {default_timeout_seconds!r}",
            )
        if interim_warn_seconds <= 0:
            raise ValueError(
                "interim_warn_seconds must be strictly positive, "
                f"got {interim_warn_seconds!r}",
            )
        self._registry = registry
        self._clock = clock
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._interim_warn_seconds = float(interim_warn_seconds)

    @property
    def default_timeout_seconds(self) -> float:
        """Default hard cut-off in seconds (ASSUM-003 → 900.0)."""
        return self._default_timeout_seconds

    @property
    def interim_warn_seconds(self) -> float:
        """Interim no-reply diagnostic point in seconds (M8 → 60.0)."""
        return self._interim_warn_seconds

    async def wait_with_timeout(
        self,
        binding: _BindingLike,
        timeout_seconds: Optional[float] = None,
    ) -> Optional[dict]:
        """Wait for the authentic reply or until the hard timeout fires.

        Returns the payload dict on success, ``None`` on timeout. The
        binding is **always** released before this method returns —
        on success, on timeout, and on cancellation — so the
        registry's ``bindings`` map no longer contains the
        correlation key once control leaves the coordinator.

        Late replies are suppressed by ``registry.release()`` rather
        than by any timer in this coordinator. The registry is the
        single source of truth for "is this binding still accepting
        replies".

        Args:
            binding: The active :class:`CorrelationBinding` to wait on.
            timeout_seconds: Optional per-call override of the default
                hard timeout. ``None`` (the default) uses
                :attr:`default_timeout_seconds`.

        Returns:
            The reply payload on success, or ``None`` on timeout.

        Raises:
            ValueError: If ``timeout_seconds`` is supplied and is not
                strictly positive.
        """
        effective: float = (
            self._default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if effective <= 0:
            raise ValueError(
                "timeout_seconds must be strictly positive, "
                f"got {timeout_seconds!r}",
            )

        # Capture start-of-wait timestamp via the injected Clock so the
        # audit log line is deterministic against FakeClock. We do not
        # use the Clock as the timer source — asyncio.timeout owns that.
        started_at: datetime = self._clock.now()

        # Awaited reply subject, derived from the agent id on the binding
        # (M8 diagnostics only — see ``_RESULT_SUBJECT_FMT``).
        awaited_subject: str = _RESULT_SUBJECT_FMT.format(
            agent_id=binding.matched_agent_id,
        )

        # Split the single hard budget into an interim leg + a remainder
        # leg PURELY so we can emit a pointed "still waiting" warn at the
        # interim without shortening the cut-off: interim + remainder ==
        # effective, and the reply is awaited continuously across both.
        # No ``asyncio.sleep`` is used — each leg is bounded by its own
        # ``asyncio.timeout`` so cancellation semantics are unchanged
        # (AC-008). When the interim would meet or exceed the whole
        # budget (short per-call timeouts), there is no interim leg and
        # the behaviour is identical to a single ``asyncio.timeout``.
        interim: float = min(self._interim_warn_seconds, effective)
        remainder: float = effective - interim

        try:
            # --- interim leg: wait up to the interim for a reply. ---
            try:
                async with asyncio.timeout(interim):
                    payload = await self._registry.wait_for_reply(binding)
                if payload is not None:
                    return payload
                # ``None`` WITHOUT TimeoutError: the registry absorbed the
                # leg's cancellation (CorrelationRegistry.wait_for_reply
                # swallows CancelledError as its release-during-wait
                # semantic) or had no future to wait on. ``None`` is never
                # an authentic reply payload — treat it exactly like an
                # expired interim leg, or the whole budget silently
                # collapses to the interim (observed live 2026-07-11:
                # dispatch.local_timeout at 60s on a healthy in-flight
                # specialist run).
            except TimeoutError:
                # asyncio.timeout raises TimeoutError (Python 3.11+; the
                # older asyncio.TimeoutError is now an alias). No reply
                # arrived within the interim leg — fall through.
                pass

            # --- remainder leg: only when the interim is strictly less
            # than the whole budget. Emit the pointed "still waiting"
            # warn, then keep awaiting the SAME reply for the remainder
            # so the 900s hard cut-off is preserved, not shortened. ---
            if remainder > 0:
                logger.warning(
                    "wait_with_timeout: no reply after %.3fs — still "
                    "awaiting (subject=%s, correlation_key=%s, agent_id=%s, "
                    "elapsed_seconds=%.3f, hard_cutoff_seconds=%.3f, "
                    "started_at=%s)",
                    interim,
                    awaited_subject,
                    binding.correlation_key,
                    binding.matched_agent_id,
                    interim,
                    effective,
                    started_at.isoformat(),
                )
                try:
                    async with asyncio.timeout(remainder):
                        payload = await self._registry.wait_for_reply(binding)
                    if payload is not None:
                        return payload
                    # Same ``None``-is-not-a-reply rule as the interim leg.
                except TimeoutError:
                    pass

            # --- hard cut-off fired with no reply (interim leg alone
            # when there was no remainder, or interim + remainder). The
            # degrade log NAMES the subject + correlation it was awaiting
            # so the timeout is never a generic "nothing happened". ---
            logger.warning(
                "wait_with_timeout: hard cut-off fired — no reply "
                "(subject=%s, correlation_key=%s, agent_id=%s, "
                "timeout_seconds=%.3f, elapsed_seconds=%.3f, "
                "started_at=%s)",
                awaited_subject,
                binding.correlation_key,
                binding.matched_agent_id,
                effective,
                effective,
                started_at.isoformat(),
            )
            return None
        finally:
            # Release on success AND on timeout AND on cancellation.
            # This is the unsubscribe-on-timeout invariant: the
            # registry's bindings map no longer contains this
            # correlation key once control leaves the coordinator,
            # which is the property D.unsubscribe-on-timeout asserts.
            self._registry.release(binding)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "INTERIM_WARN_SECONDS",
    "TimeoutCoordinator",
]
