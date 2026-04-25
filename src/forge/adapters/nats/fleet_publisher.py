"""Forge fleet self-registration, heartbeat publishing, and deregistration.

Implements the lifecycle described in
``docs/design/contracts/API-nats-fleet-lifecycle.md §2``:

- :func:`register_on_boot` — publishes :data:`FORGE_MANIFEST` to the
  ``fleet.register`` topic and stores it in the ``agent-registry`` KV bucket
  via :meth:`nats_core.client.NATSClient.register_agent`.
- :func:`heartbeat_loop` — long-running coroutine that publishes an
  :class:`~nats_core.events.AgentHeartbeatPayload` every
  ``interval_seconds`` until ``cancel_event`` is set. Each iteration
  catches and logs transient publish failures so the loop never exits on
  a recoverable error (TASK-NFI-004 AC-005). The loop is **independent of
  registry reachability**: a temporarily unreachable ``agent-registry``
  KV bucket does not stop heartbeats (Group E ``@integration`` scenario).
- :func:`deregister` — best-effort graceful deregistration. Idempotent:
  may be called more than once without raising.

Status, queue depth, and active-task count are read from an injected
:class:`StatusProvider`. Time is read through a :class:`Clock` protocol
so tests can advance time without wall-clock sleeps (AC-002).

SIGTERM wiring is the responsibility of the application entrypoint; this
module only exposes the three coroutines and the supporting protocols.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from nats_core.events import AgentHeartbeatPayload

from forge.config.models import DEFAULT_HEARTBEAT_INTERVAL_SECONDS
from forge.fleet.manifest import FORGE_MANIFEST

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats_core.client import NATSClient

logger = logging.getLogger(__name__)

#: Identifier stamped on every heartbeat payload Forge publishes; mirrors
#: ``FORGE_MANIFEST.agent_id`` and is exported as a constant so tests
#: assert against a single source of truth.
AGENT_ID: str = "forge"

#: Status literal type alias — kept aligned with
#: :class:`nats_core.events.AgentHeartbeatPayload.status`.
AgentStatus = Literal["ready", "busy", "degraded", "draining"]

__all__ = [
    "AGENT_ID",
    "AgentStatus",
    "Clock",
    "MonotonicClock",
    "StatusProvider",
    "build_heartbeat_payload",
    "deregister",
    "heartbeat_loop",
    "register_on_boot",
]


# ---------------------------------------------------------------------------
# Injection protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class StatusProvider(Protocol):
    """Read-only handle exposing Forge's runtime status to the heartbeat loop.

    The pipeline state machine (TASK-NFI-007) is the production
    implementation; tests pass an inert fake. Methods are synchronous
    snapshots — never await inside ``get_*`` since they are called from the
    heartbeat loop's hot path.
    """

    def get_current_status(self) -> AgentStatus:
        """Return the current operational status."""
        ...

    def get_active_tasks(self) -> int:
        """Return the count of in-flight builds (``0`` or ``1`` for Forge — ADR-SP-012)."""
        ...

    def get_queue_depth(self) -> int:
        """Return the count of build-queued events waiting to be picked up."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time abstraction so the heartbeat loop has no wall-clock dependency in tests.

    Implementations must be safe to use from a single asyncio task — the
    loop reads :meth:`monotonic` between sleeps and awaits :meth:`sleep`
    sequentially.
    """

    def monotonic(self) -> float:
        """Return a strictly non-decreasing seconds-since-epoch reading."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the current task for ``seconds`` of clock time."""
        ...


class MonotonicClock:
    """Default :class:`Clock` backed by ``time.monotonic`` and ``asyncio.sleep``.

    Production callers do not need to instantiate this directly; the
    heartbeat loop falls back to ``MonotonicClock()`` when no clock is
    injected.
    """

    def monotonic(self) -> float:
        """Delegate to :func:`time.monotonic`."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Delegate to :func:`asyncio.sleep`."""
        await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Payload builder (pure — no I/O)
# ---------------------------------------------------------------------------


def build_heartbeat_payload(
    *,
    status_provider: StatusProvider,
    started_at_monotonic: float,
    clock: Clock,
) -> AgentHeartbeatPayload:
    """Snapshot the current runtime state into an :class:`AgentHeartbeatPayload`.

    This helper is exported so the loop logic and the test suite can both
    construct payloads through the same code path, keeping the
    ``active_tasks`` / ``queue_depth`` / ``uptime_seconds`` derivation
    auditable in one place.

    Args:
        status_provider: Source of current status, queue depth, and active
            task count. Each of the three accessor methods is invoked
            exactly once per call.
        started_at_monotonic: Monotonic-clock time when the loop started.
            Subtracted from the current monotonic reading to derive
            ``uptime_seconds``.
        clock: Clock used to read the current monotonic time. Tests inject
            a fake so the resulting ``uptime_seconds`` is deterministic.

    Returns:
        A freshly-constructed :class:`AgentHeartbeatPayload` ready to pass
        to :meth:`nats_core.client.NATSClient.heartbeat`.
    """
    # Guard against a non-monotonic clock reading. The protocol promises
    # monotonic, but if a test substitutes a misbehaving fake we'd rather
    # publish ``uptime_seconds=0`` than negative.
    raw_uptime = clock.monotonic() - started_at_monotonic
    uptime_seconds = max(0, int(raw_uptime))

    return AgentHeartbeatPayload(
        agent_id=AGENT_ID,
        status=status_provider.get_current_status(),
        queue_depth=status_provider.get_queue_depth(),
        active_tasks=status_provider.get_active_tasks(),
        uptime_seconds=uptime_seconds,
    )


# ---------------------------------------------------------------------------
# Public lifecycle coroutines
# ---------------------------------------------------------------------------


async def register_on_boot(nats_client: NATSClient | Any) -> None:
    """Publish :data:`FORGE_MANIFEST` to ``fleet.register`` and KV-store it.

    Thin pass-through to :meth:`nats_core.client.NATSClient.register_agent`.
    Kept as a named coroutine (rather than inlined at the call site) so the
    application entrypoint has a single, search-grep-friendly registration
    seam (TASK-NFI-004 AC-001).

    Args:
        nats_client: Connected NATS client exposing ``register_agent``.

    Raises:
        Exception: Whatever the underlying client raises on transport
            failure. The boot sequence treats registration failure as
            fatal — heartbeats can survive a flaky KV, but if the boot
            publish itself fails the supervisor should see the error.
    """
    await nats_client.register_agent(FORGE_MANIFEST)


async def deregister(
    nats_client: NATSClient | Any, reason: str = "shutdown"
) -> None:
    """Publish a graceful deregistration. Idempotent — safe to call twice.

    The underlying :meth:`nats_core.client.NATSClient.deregister_agent`
    already swallows ``KeyError`` from the KV delete, so the second call's
    KV-delete step is a no-op. We additionally wrap the publish step in a
    try/except so a transient bus error during shutdown does not
    short-circuit the rest of the SIGTERM handler — graceful teardown
    must always reach the heartbeat-task cancel that follows this call.

    Args:
        nats_client: Connected NATS client exposing ``deregister_agent``.
        reason: Human-readable reason embedded in the deregistration
            event payload. Defaults to ``"shutdown"``.
    """
    try:
        await nats_client.deregister_agent(AGENT_ID, reason=reason)
    except Exception as exc:  # noqa: BLE001 — best-effort, never raise
        # AC-004 — calling twice does not raise. Logging at WARNING so
        # operators still see transient failures during shutdown.
        logger.warning(
            "fleet deregister(reason=%r) failed; treating as idempotent no-op: %s",
            reason,
            exc,
        )


async def heartbeat_loop(
    nats_client: NATSClient | Any,
    cancel_event: asyncio.Event,
    *,
    status_provider: StatusProvider,
    interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    clock: Clock | None = None,
) -> None:
    """Publish an :class:`AgentHeartbeatPayload` every ``interval_seconds``.

    Loop semantics (TASK-NFI-004 AC-002, AC-005, AC-006, AC-007):

    - One publish per tick using a freshly-snapshotted payload.
    - Transport-level errors from ``nats_client.heartbeat`` are caught and
      logged at WARNING; the loop then sleeps and retries on the next tick
      (AC-005).
    - The loop never reads the registry — registry unreachability cannot
      stop heartbeats (AC-006, Group E ``@integration``).
    - The loop exits cleanly when ``cancel_event`` is set, whether the
      event fires between ticks or mid-sleep (AC-007). The currently
      pending sleep is cancelled so shutdown does not stall for up to
      ``interval_seconds``.

    Args:
        nats_client: Connected NATS client exposing ``heartbeat``.
        cancel_event: Set by the supervisor (typically the SIGTERM
            handler) to signal the loop to exit.
        status_provider: Synchronous snapshot source for status, queue
            depth, and active task count.
        interval_seconds: Cadence between publishes (default
            :data:`forge.config.models.DEFAULT_HEARTBEAT_INTERVAL_SECONDS`,
            which is anchored to ASSUM-001 = 30s).
        clock: Optional :class:`Clock` injection. Defaults to a
            :class:`MonotonicClock`. Tests pass a fake so the loop is
            wall-clock independent.
    """
    active_clock: Clock = clock if clock is not None else MonotonicClock()
    started_at = active_clock.monotonic()

    logger.info(
        "fleet heartbeat loop starting agent_id=%s interval_seconds=%s",
        AGENT_ID,
        interval_seconds,
    )

    while not cancel_event.is_set():
        # --- Publish step --------------------------------------------------
        try:
            payload = build_heartbeat_payload(
                status_provider=status_provider,
                started_at_monotonic=started_at,
                clock=active_clock,
            )
            await nats_client.heartbeat(payload)
        except Exception as exc:  # noqa: BLE001 — never exit the loop on publish error
            # Registry unreachability and transient bus errors both land
            # here; AC-005 / AC-006 require the loop to keep running.
            logger.warning(
                "fleet heartbeat publish failed (loop continues): %s", exc
            )

        if cancel_event.is_set():
            break

        # --- Sleep step ----------------------------------------------------
        # Race the inter-tick sleep against the cancel event so SIGTERM
        # does not stall for up to one full interval.
        cancelled = await _sleep_or_cancel(
            cancel_event=cancel_event,
            clock=active_clock,
            seconds=float(interval_seconds),
        )
        if cancelled:
            break

    logger.info("fleet heartbeat loop exited cleanly agent_id=%s", AGENT_ID)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _sleep_or_cancel(
    *,
    cancel_event: asyncio.Event,
    clock: Clock,
    seconds: float,
) -> bool:
    """Wait either ``seconds`` of clock time or until ``cancel_event`` fires.

    Returns ``True`` if cancellation interrupted the sleep, ``False`` if
    the sleep completed normally. The pending task is always cancelled
    and awaited so no orphan tasks linger after this function returns —
    important for SIGTERM teardown determinism.

    Args:
        cancel_event: Event to race against the sleep.
        clock: Clock providing the awaitable sleep primitive.
        seconds: Sleep duration to race.

    Returns:
        ``True`` if ``cancel_event`` is set on return, ``False`` otherwise.
    """
    sleep_task: asyncio.Task[None] = asyncio.create_task(clock.sleep(seconds))
    cancel_task: asyncio.Task[bool] = asyncio.create_task(cancel_event.wait())

    try:
        _done, pending = await asyncio.wait(
            {sleep_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        # Outer task was cancelled (e.g. supervisor cancelled the
        # heartbeat task directly). Tidy up the children before
        # propagating so we do not leak background coroutines.
        sleep_task.cancel()
        cancel_task.cancel()
        raise

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Expected — we just cancelled it.
            pass
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            logger.debug(
                "pending task raised during _sleep_or_cancel cleanup: %s", exc
            )

    return cancel_event.is_set()
