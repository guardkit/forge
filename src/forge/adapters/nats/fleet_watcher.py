"""Live NATS fleet watcher dispatching events to a :class:`FleetEventSink`.

Owns the live-subscription side of Forge's fleet plane described in
``docs/design/contracts/API-nats-fleet-lifecycle.md §3`` and
``docs/design/models/DM-discovery.md §4``.

The watcher subscribes to two channels and dispatches every event to a
single :class:`FleetEventSink` (typically the
:class:`forge.discovery.cache.DiscoveryCache`):

* ``fleet.register`` / ``fleet.deregister`` — observed via
  ``nats_client.watch_fleet`` (NATS JetStream KV ``agent-registry``).
  PUT events deliver an :class:`~nats_core.manifest.AgentManifest` →
  :meth:`FleetEventSink.upsert_agent`. DELETE / PURGE events deliver a
  ``None`` manifest → :meth:`FleetEventSink.remove_agent`.
* ``fleet.heartbeat.>`` — observed via ``nats_client.subscribe`` (the
  envelope-aware fleet topic). Each :class:`MessageEnvelope` is
  validated as :class:`~nats_core.events.AgentHeartbeatPayload` and
  routed to :meth:`FleetEventSink.update_heartbeat` with a
  ``status_changed`` flag computed from the cache's current view.

The watcher never mutates the cache directly — every cache mutation
goes via the sink so the sink's ``asyncio.Lock`` is the single
serialisation point (DM-discovery §4).

A companion :func:`stale_sweeper` background task flips agents whose
``last_heartbeat_at`` age exceeds the configured
``stale_heartbeat_seconds`` to ``status="degraded"`` so the resolver
excludes them from primary selection.

Robustness guarantees
---------------------

* Malformed heartbeat payloads are logged at WARN and dropped — the
  watcher continues processing subsequent valid events
  (AC-002 / Group C @negative).
* :func:`watch` survives transient ``nats_client`` errors with a
  reconnect loop bounded only by ``asyncio.CancelledError``
  (which propagates so callers own shutdown).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Literal, Protocol, runtime_checkable

from nats_core.envelope import MessageEnvelope
from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import AgentManifest
from nats_core.topics import Topics
from pydantic import ValidationError

from forge.discovery.models import DiscoveryCacheEntry
from forge.discovery.protocol import Clock, FleetEventSink

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (importable so tests can shrink them to keep runs fast).
# ---------------------------------------------------------------------------

#: Default sleep between reconnect attempts after a transient watch_fleet error.
DEFAULT_RECONNECT_BACKOFF_SECONDS: float = 1.0

#: Default cadence for :func:`stale_sweeper`. Independent of
#: ``stale_heartbeat_seconds`` — a 10s sweep with a 90s threshold means an
#: agent is marked degraded within roughly 90–100 seconds of its last
#: heartbeat (ASSUM-002).
DEFAULT_SWEEP_INTERVAL_SECONDS: int = 10

#: Status value used when synthesising a heartbeat for the stale sweeper.
#: Annotated as ``Literal["degraded"]`` so :class:`AgentHeartbeatPayload`
#: accepts it without a cast — its ``status`` field is a typed Literal.
DEGRADED_STATUS: Literal["degraded"] = "degraded"


# ---------------------------------------------------------------------------
# Reader protocol — companion to FleetEventSink for reads
# ---------------------------------------------------------------------------


@runtime_checkable
class SnapshotReader(Protocol):
    """Read-side companion to :class:`FleetEventSink`.

    The watcher needs the cache's *current* heartbeat status to compute
    ``status_changed`` correctly across stale_sweeper-induced flips
    (which the watcher cannot otherwise observe). The reader exposes
    only the snapshot — it never widens the mutation surface.

    :class:`forge.discovery.cache.DiscoveryCache` already implements
    this protocol; tests typically pass the same instance for both
    ``sink`` and ``reader``.
    """

    async def snapshot(self) -> dict[str, DiscoveryCacheEntry]:
        """Return a shallow copy of the current cache state."""
        ...


@runtime_checkable
class _NATSClientLike(Protocol):
    """Minimal ``nats_core.client.NATSClient`` slice the watcher depends on."""

    async def watch_fleet(
        self,
        callback: Callable[[str, AgentManifest | None], Awaitable[None]],
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Watcher class — handlers exposed as methods so tests can drive them directly
# ---------------------------------------------------------------------------


class FleetWatcher:
    """Translate NATS fleet events into :class:`FleetEventSink` calls.

    The class exists primarily so the per-event handlers
    (:meth:`on_fleet_change`, :meth:`on_heartbeat`) can be exercised by
    unit tests without needing to spin a real subscribe / watch_fleet
    loop. The :func:`watch` module-level function wires this class to a
    real ``nats_client`` for production.

    Args:
        sink: The :class:`FleetEventSink` receiving validated events.
        status_reader: Optional :class:`SnapshotReader` used to compare
            a new heartbeat status against the cache's current value.
            When omitted, the watcher falls back to a process-local map
            of last-seen statuses; that map cannot observe
            stale_sweeper-induced flips, so production callers should
            always pass the same :class:`forge.discovery.cache.DiscoveryCache`
            instance for both ``sink`` and ``status_reader``.
    """

    def __init__(
        self,
        sink: FleetEventSink,
        *,
        status_reader: SnapshotReader | None = None,
    ) -> None:
        self._sink = sink
        self._reader = status_reader
        # Process-local fallback for status_changed when no reader is wired.
        self._last_status: dict[str, str] = {}

    # ------------------------------------------------------------------
    # NATS callback handlers
    # ------------------------------------------------------------------

    async def on_fleet_change(
        self,
        agent_id: str,
        manifest: AgentManifest | None,
    ) -> None:
        """Dispatch a single ``watch_fleet`` event to the sink.

        ``manifest is None`` indicates the KV entry was deleted or
        purged — that is the ``fleet.deregister`` path. A non-``None``
        manifest is treated as a (possibly version-bumped)
        re-registration; :meth:`FleetEventSink.upsert_agent` is
        idempotent at the cache layer so repeated registers cannot
        introduce duplicate entries (AC-004).
        """

        try:
            if manifest is None:
                await self._sink.remove_agent(agent_id)
                self._last_status.pop(agent_id, None)
                logger.debug("fleet_watcher.deregister agent_id=%s", agent_id)
                return

            await self._sink.upsert_agent(manifest)
            # The cache resets the snapshot to ``status="ready"`` on
            # upsert; mirror that here so the in-memory fallback stays
            # in sync when no reader is wired.
            self._last_status[manifest.agent_id] = "ready"
            logger.debug(
                "fleet_watcher.register agent_id=%s version=%s",
                manifest.agent_id,
                manifest.version,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — translate to log+continue
            # The watcher must not die on a single bad event. Log and
            # let the surrounding subscribe loop deliver the next one.
            logger.warning(
                "fleet_watcher: dispatch error agent_id=%s manifest_present=%s "
                "error=%s",
                agent_id,
                manifest is not None,
                exc,
            )

    async def on_heartbeat(self, envelope: MessageEnvelope) -> None:
        """Validate ``envelope.payload`` and dispatch to the sink.

        The envelope itself is already deserialised by ``NATSClient.subscribe``
        — we only need to coerce the inner payload dict to
        :class:`AgentHeartbeatPayload`. Validation failures are logged
        and dropped so the watcher continues processing subsequent
        valid heartbeats (AC-002).
        """

        try:
            hb = AgentHeartbeatPayload.model_validate(envelope.payload)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "fleet_watcher: invalid heartbeat payload dropped (%s); "
                "envelope source_id=%s",
                exc,
                getattr(envelope, "source_id", "<unknown>"),
            )
            return

        prev_status = await self._previous_status(hb.agent_id)
        status_changed = prev_status != hb.status
        # Update the in-memory mirror unconditionally so the fallback
        # path tracks the latest value even if the sink's update fails.
        self._last_status[hb.agent_id] = hb.status

        try:
            await self._sink.update_heartbeat(hb.agent_id, hb, status_changed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — translate to log+continue
            logger.warning(
                "fleet_watcher: update_heartbeat failed agent_id=%s error=%s",
                hb.agent_id,
                exc,
            )

    async def _previous_status(self, agent_id: str) -> str | None:
        """Return the status the cache currently records for ``agent_id``.

        Falls back to the in-memory mirror if no :class:`SnapshotReader`
        is wired or the read raises. Returning ``None`` for unknown
        agents means the very first heartbeat after registration is
        always treated as a status change — matching the behaviour
        humans expect when the cache transitions from "no observation"
        to "first observation".
        """

        if self._reader is None:
            return self._last_status.get(agent_id)

        try:
            snapshot = await self._reader.snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log and fall back
            logger.warning(
                "fleet_watcher: snapshot read failed agent_id=%s error=%s",
                agent_id,
                exc,
            )
            return self._last_status.get(agent_id)

        entry = snapshot.get(agent_id)
        if entry is None:
            return None
        return entry.last_heartbeat_status

    # ------------------------------------------------------------------
    # Long-running run loop (production wiring)
    # ------------------------------------------------------------------

    async def run(
        self,
        nats_client: _NATSClientLike,
        *,
        reconnect_backoff_seconds: float = DEFAULT_RECONNECT_BACKOFF_SECONDS,
    ) -> None:
        """Bind subscriptions and block until cancelled.

        Survives transient ``nats_client`` errors by sleeping
        ``reconnect_backoff_seconds`` between attempts. Re-subscribing
        causes ``watch_fleet`` to replay current KV entries, so the
        sink receives a fresh manifest for every registered agent on
        each reconnect (idempotent — :meth:`FleetEventSink.upsert_agent`
        replaces the cache entry).
        """

        while True:
            try:
                await nats_client.subscribe(
                    Topics.Fleet.HEARTBEAT_ALL,
                    self.on_heartbeat,
                )
                await nats_client.watch_fleet(callback=self.on_fleet_change)
                # watch_fleet returning normally means the upstream
                # iterator drained — re-enter the loop so we re-subscribe.
                logger.info(
                    "fleet_watcher: watch_fleet returned; resubscribing",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any error
                logger.warning(
                    "fleet_watcher: transient error %s; reconnecting in %.2fs",
                    exc,
                    reconnect_backoff_seconds,
                )
            await asyncio.sleep(reconnect_backoff_seconds)


# ---------------------------------------------------------------------------
# Module-level entrypoints — match the task signatures
# ---------------------------------------------------------------------------


async def watch(
    nats_client: _NATSClientLike,
    sink: FleetEventSink,
    *,
    status_reader: SnapshotReader | None = None,
    reconnect_backoff_seconds: float = DEFAULT_RECONNECT_BACKOFF_SECONDS,
) -> None:
    """Subscribe to fleet events and dispatch to ``sink`` until cancelled.

    Thin wrapper around :class:`FleetWatcher` to honour the task brief's
    function signature ``watch(nats_client, sink: FleetEventSink)``.

    Args:
        nats_client: NATS client exposing ``watch_fleet`` (KV) and
            ``subscribe`` (envelope-aware) coroutines.
        sink: The :class:`FleetEventSink` receiving validated events.
        status_reader: Optional cache-snapshot reader. See
            :class:`FleetWatcher`.
        reconnect_backoff_seconds: Sleep between reconnect attempts.
    """

    watcher = FleetWatcher(sink, status_reader=status_reader)
    await watcher.run(
        nats_client,
        reconnect_backoff_seconds=reconnect_backoff_seconds,
    )


# ---------------------------------------------------------------------------
# Stale sweeper
# ---------------------------------------------------------------------------


async def run_one_sweep(
    sink: FleetEventSink,
    reader: SnapshotReader,
    clock: Clock,
    stale_heartbeat_seconds: int,
) -> int:
    """Mark every stale agent as degraded — single pass.

    Extracted from :func:`stale_sweeper`'s loop so unit tests can drive
    one deterministic sweep without dealing with ``asyncio.sleep``.

    Args:
        sink: Mutation surface — receives synthesised
            :meth:`FleetEventSink.update_heartbeat` calls.
        reader: Read surface — supplies the snapshot of current entries.
        clock: Time source. The sweeper subtracts
            ``entry.last_heartbeat_at`` from ``clock.now()`` to compute
            heartbeat age.
        stale_heartbeat_seconds: Age threshold (seconds). An entry is
            marked degraded only when ``age > threshold`` (strict
            inequality — matches the contract wording "older than").

    Returns:
        The number of entries flipped to ``degraded`` in this pass.
    """

    now = clock.now()
    snapshot = await reader.snapshot()
    flipped = 0

    for agent_id, entry in snapshot.items():
        if entry.last_heartbeat_status == DEGRADED_STATUS:
            # Already degraded — nothing to do. Skipping prevents the
            # sweeper from continuously refreshing last_heartbeat_at.
            continue

        age = (now - entry.last_heartbeat_at).total_seconds()
        if age <= stale_heartbeat_seconds:
            continue

        # Synthesise a heartbeat payload that preserves the last-known
        # queue depth and active task count — only ``status`` flips.
        # ``uptime_seconds`` is required by the schema; the cache does
        # not retain a prior value so we publish 0 as an opaque
        # "unknown" token. Downstream consumers read status, not uptime.
        synthetic = AgentHeartbeatPayload(
            agent_id=agent_id,
            status=DEGRADED_STATUS,
            queue_depth=entry.last_queue_depth,
            active_tasks=entry.last_active_tasks,
            uptime_seconds=0,
        )

        try:
            await sink.update_heartbeat(agent_id, synthetic, status_changed=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep sweeping other agents
            logger.warning(
                "stale_sweeper: failed to flip agent_id=%s to degraded: %s",
                agent_id,
                exc,
            )
            continue

        flipped += 1
        logger.info(
            "stale_sweeper: marked agent_id=%s degraded (age=%.1fs threshold=%ds)",
            agent_id,
            age,
            stale_heartbeat_seconds,
        )

    return flipped


async def stale_sweeper(
    sink: FleetEventSink,
    reader: SnapshotReader,
    clock: Clock,
    stale_heartbeat_seconds: int,
    *,
    interval_s: int = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Background task — periodically mark stale agents as degraded.

    Loops :func:`run_one_sweep` separated by ``interval_s`` seconds.
    ``CancelledError`` propagates so the caller can stop the task with
    ``task.cancel()``; any other exception is logged and the loop
    continues so a transient sink/reader fault cannot kill the sweeper.

    Args:
        sink: Mutation surface (typically the
            :class:`forge.discovery.cache.DiscoveryCache`).
        reader: Read surface (the same cache instance in production).
        clock: Time source — injected so tests advance time
            deterministically (e.g. with a ``FakeClock`` double).
        stale_heartbeat_seconds: Age threshold from
            :class:`forge.config.models.FleetConfig`.
        interval_s: Sweep cadence in seconds. Default
            :data:`DEFAULT_SWEEP_INTERVAL_SECONDS`.
    """

    while True:
        try:
            await run_one_sweep(sink, reader, clock, stale_heartbeat_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the sweeper alive
            logger.warning(
                "stale_sweeper: unexpected sweep error: %s; continuing",
                exc,
            )

        await asyncio.sleep(interval_s)


__all__ = [
    "DEFAULT_RECONNECT_BACKOFF_SECONDS",
    "DEFAULT_SWEEP_INTERVAL_SECONDS",
    "DEGRADED_STATUS",
    "FleetWatcher",
    "SnapshotReader",
    "run_one_sweep",
    "stale_sweeper",
    "watch",
]
