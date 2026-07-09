"""Environment-reservation lease interface (WS2-B8, scope §4 / scope Q2).

The shared GB10 GPU corrupted 2 of 5 study-tutor acceptance attempts under a
concurrent workload (``RESULTS-study-tutor-p2-live-acceptance-2026-07-05.md``).
v1 gives deploy/live-gate runs a **reservation lease** so other fleet consumers
honour it, aligning with DF-002 (ledger-based resource governance).

**Scope Q2 is OPEN** — NATS KV lease vs a DF-002 ledger extension. B8 therefore
puts take/release **behind an interface** (:class:`ReservationLease`) so the
backend is swappable without touching the deploy stage. v1 ships the in-process
:class:`InProcessReservationLease` (correct within a single forge process — the
common case: one serve daemon). :class:`UnconfiguredReservationLease` raises
loudly if a profile *requests* a reservation but no real backend is wired
(the FEAT-DD4F "no silent no-op" discipline — a reservation that silently
does nothing is worse than none, because callers trust it).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "ReservationHandle",
    "ReservationLease",
    "ReservationError",
    "ReservationUnavailableError",
    "InProcessReservationLease",
    "UnconfiguredReservationLease",
]


class ReservationError(RuntimeError):
    """Base error for reservation-lease failures."""


class ReservationUnavailableError(ReservationError):
    """Raised when a resource is already held by another holder."""


@dataclass(frozen=True, slots=True)
class ReservationHandle:
    """An acquired lease. Passed back to :meth:`ReservationLease.release`.

    Attributes:
        resource: The reserved resource name (e.g. ``gb10-gpu``).
        holder: The holder identity (typically the correlation/deploy-run id).
    """

    resource: str
    holder: str


@runtime_checkable
class ReservationLease(Protocol):
    """Swappable reservation-lease backend (scope Q2).

    An implementation acquires an exclusive lease on a named resource and
    releases it. The deploy stage takes a lease before touching a reserved
    resource and releases it in a ``finally`` so a crash never wedges the
    resource forever (the backend is responsible for lease expiry).
    """

    def acquire(self, resource: str, *, holder: str) -> ReservationHandle:
        """Acquire an exclusive lease on ``resource`` for ``holder``.

        Raises:
            ReservationUnavailableError: If the resource is already held.
        """
        ...

    def release(self, handle: ReservationHandle) -> None:
        """Release a previously acquired lease. Idempotent."""
        ...


class InProcessReservationLease:
    """In-process reservation lease (v1 default backend).

    Correct within a single forge process (one serve daemon holding all deploy
    runs) — the common v1 case. Cross-process coordination (the real GB10-GPU
    lease shared with the WS4 82h run) is the ``kv`` backend, deferred to V1
    per scope Q2. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}

    def acquire(self, resource: str, *, holder: str) -> ReservationHandle:
        with self._lock:
            current = self._held.get(resource)
            if current is not None and current != holder:
                raise ReservationUnavailableError(
                    f"resource {resource!r} is already reserved by {current!r}"
                )
            self._held[resource] = holder
        logger.info("reservation acquired resource=%s holder=%s", resource, holder)
        return ReservationHandle(resource=resource, holder=holder)

    def release(self, handle: ReservationHandle) -> None:
        with self._lock:
            current = self._held.get(handle.resource)
            if current == handle.holder:
                del self._held[handle.resource]
                logger.info(
                    "reservation released resource=%s holder=%s",
                    handle.resource,
                    handle.holder,
                )
            # Idempotent: releasing a lease we do not hold is a no-op (but is
            # logged so a double-release or foreign-release is visible).
            elif current is None:
                logger.debug("reservation release: %s already free", handle.resource)
            else:
                logger.warning(
                    "reservation release: %s held by %s, not releasing on behalf of %s",
                    handle.resource,
                    current,
                    handle.holder,
                )


class UnconfiguredReservationLease:
    """A reservation backend that RAISES if used (FEAT-DD4F: no silent no-op).

    Wired when reservations are not configured. If a deploy profile *requests* a
    reservation but no backend was provided, acquiring must fail loudly rather
    than silently proceed unprotected — a reservation callers trust but that
    does nothing is a false green. The deploy stage catches this at the stage
    boundary and records an honest failed deploy, never a silent success.
    """

    def acquire(self, resource: str, *, holder: str) -> ReservationHandle:
        raise ReservationError(
            f"reservation requested for resource {resource!r} but no reservation "
            "backend is configured (deploy.reservation_backend). Refusing to "
            "proceed unprotected — configure a backend or remove the "
            "reservation from the deploy profile."
        )

    def release(self, handle: ReservationHandle) -> None:
        # Never acquired anything, so release is a no-op. Kept so the interface
        # is total (a caller in a finally block must not itself raise).
        return None
