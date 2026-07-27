"""Shared subscription state for the ``forge serve`` daemon (TASK-F009-001 / TASK-FW10-001).

The daemon (writer) and the healthz HTTP server (reader) need to agree on
two booleans — *is the JetStream subscription currently live?* and *has
the lifecycle dispatch chain been composed?* — to implement the readiness
probe mandated by Integration Contract B (``HEALTHZ_PORT = 8080``).

This module defines the boundary type: a small ``SubscriptionState``
dataclass with:

* ``live: bool`` — flipped by :mod:`forge.cli._serve_daemon` when the
  JetStream pull subscription is bound (writer) / lost (writer) /
  shutting down (writer).
* ``chain_ready: bool`` — flipped by :mod:`forge.cli.serve` exactly once,
  after the orchestrator dispatch chain has been composed and both
  ``reconcile_on_boot`` routines (lifecycle recovery + redelivery
  reconciliation) have completed (TASK-FW10-001 ASSUM-012).

Both fields are guarded by an ``asyncio.Lock`` on the writer side. Reads
return the cached boolean without taking the lock — Python's GIL
guarantees attribute reads are atomic, and a stale-by-one-tick read is
acceptable for a readiness probe (TASK-F009-001 AC-006).

Wave-2 tasks T3 (daemon), T4 (healthz), and TASK-FW10-001 (boot wiring)
import :class:`SubscriptionState` and share a single instance built by
``serve_cmd`` so the producer and consumer cannot drift.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class SubscriptionState:
    """Mutable readiness flags shared between daemon, healthz, and boot wiring.

    Attributes:
        live: ``True`` once the JetStream subscription is established and
            messages can be processed; ``False`` while the daemon is
            still bootstrapping (or has just lost its connection).
        chain_ready: ``True`` once the orchestrator dispatch chain has
            been composed and both ``reconcile_on_boot`` routines have
            completed. The healthz server reports unhealthy until this
            flips True (TASK-FW10-001 ASSUM-012). It is one-way: once
            True it stays True for the daemon's lifetime — chain
            composition is not re-run on broker reconnect.
        ack_slot: The last ack-slot health reading (FEAT-PAC), one of
            ``"healthy"`` / ``"held"`` / ``"phantom"`` / ``"unknown"`` /
            ``"absent"``. Written by the boot cure step and the runtime watchdog in
            :mod:`forge.cli.serve`; read by the healthz handler so the
            phantom-ack wedge is visible to a standing ``curl`` probe.
            Defaults to ``"unknown"`` until the first boot inspection
            runs. This flag is advisory-only: it never gates the
            readiness verdict (the wedge is alarm-only in v1).
    """

    live: bool = False
    chain_ready: bool = False
    ack_slot: str = "unknown"
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def set_live(self, value: bool) -> None:
        """Atomically update :attr:`live` under the internal lock.

        Use this from the daemon side (writer) — the lock guarantees that
        a concurrent writer never tears the boolean across a coroutine
        switch. The reader side may use plain attribute access.

        Args:
            value: New value for :attr:`live`.
        """
        async with self._lock:
            self.live = value

    async def set_chain_ready(self, value: bool) -> None:
        """Atomically update :attr:`chain_ready` under the internal lock.

        Called by ``_run_serve`` after ``reconcile_on_boot`` routines and
        chain composition complete (TASK-FW10-001). Should only ever be
        flipped to True — once the chain is composed it stays composed
        for the daemon's lifetime.

        Args:
            value: New value for :attr:`chain_ready`.
        """
        async with self._lock:
            self.chain_ready = value

    async def set_ack_slot(self, value: str) -> None:
        """Atomically update :attr:`ack_slot` under the internal lock.

        Called by the FEAT-PAC boot cure step (once, after the boot
        inspection/cure) and by the runtime watchdog (once per interval).
        Writers take the lock for consistency with the other setters;
        the healthz handler reads lock-free like the other flags.

        Args:
            value: New ack-slot status
                (``"healthy"`` / ``"held"`` / ``"phantom"`` / ``"unknown"`` /
                ``"absent"``).
        """
        async with self._lock:
            self.ack_slot = value

    def is_live(self) -> bool:
        """Return the current :attr:`live` value without taking the lock.

        Reads are safe without the lock because Python attribute reads
        are atomic under the GIL and the readiness semantics tolerate a
        single-tick staleness window.
        """
        return self.live

    def is_chain_ready(self) -> bool:
        """Return the current :attr:`chain_ready` value without the lock.

        Same lock-free read semantics as :meth:`is_live`. The healthz
        request handler reads this together with :meth:`is_live` to
        compute the readiness gate (TASK-FW10-001 ASSUM-012).
        """
        return self.chain_ready

    def is_healthy(self) -> bool:
        """Composite readiness gate: live AND chain_ready.

        The healthz server returns 200 iff this is True. Either flag
        being False produces a 503 response — the reason field in the
        body distinguishes the cause for operators.
        """
        return self.live and self.chain_ready


__all__ = ["SubscriptionState"]
