"""Protocols and default clock for the Forge discovery layer.

This module defines two ``typing.Protocol`` types used by the
discovery domain plus a default :class:`SystemClock`:

* :class:`Clock` — a single-method time provider so cache TTL and
  staleness logic can be exercised with a deterministic
  :class:`FakeClock` in tests (DM-discovery §4 / AC-CACHE-TTL).

* :class:`FleetEventSink` — the surface the NATS fleet watcher calls
  when fleet events arrive. Decouples the watcher (:mod:`forge.adapters.nats`)
  from the cache implementation so a test double or a no-op sink can
  stand in.

* :class:`SystemClock` — production :class:`Clock` implementation that
  reads ``datetime.now(UTC)``.

Producer/consumer pairing for the FleetEventSink protocol:

============  ========================================================
Producer      ``forge.discovery.cache.DiscoveryCache`` (this package)
Consumer      ``forge.adapters.nats.fleet_watcher`` (TASK-NFI-005)
============  ========================================================
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import AgentManifest


@runtime_checkable
class Clock(Protocol):
    """Time provider for cache TTL and staleness checks.

    Implementations must return a timezone-aware UTC datetime so all
    arithmetic in the cache is unambiguous. Tests inject a
    :class:`FakeClock`-style double so TTL behaviour is deterministic.
    """

    def now(self) -> datetime:
        """Return the current UTC time."""
        ...


class SystemClock:
    """Default :class:`Clock` implementation backed by ``datetime.now(UTC)``.

    Used by :class:`forge.discovery.cache.DiscoveryCache` when no
    explicit ``clock`` is injected. Tests should not use this — they
    should construct the cache with a deterministic fake.
    """

    def now(self) -> datetime:
        """Return ``datetime.now(UTC)``."""
        return datetime.now(UTC)


@runtime_checkable
class FleetEventSink(Protocol):
    """Surface the NATS fleet watcher calls when fleet events arrive.

    The watcher is the *consumer* of this protocol; the cache is the
    *producer*. Both sides must agree on these signatures before
    Wave 3 starts (see Seam Note in TASK-NFI-003).

    All three methods are coroutines because the production
    implementation guards mutations with an ``asyncio.Lock``.
    """

    async def upsert_agent(self, manifest: AgentManifest) -> None:
        """Insert or replace the cache entry for ``manifest.agent_id``.

        Called for ``fleet.register`` events — including version-bumped
        re-registrations. Resets the heartbeat snapshot to a fresh
        ``ready`` state.

        Args:
            manifest: The newly published manifest.
        """
        ...

    async def remove_agent(self, agent_id: str) -> None:
        """Delete the cache entry for ``agent_id``.

        Called for ``fleet.deregister`` events. A no-op if no entry
        exists for the agent — never raises ``KeyError``.

        Args:
            agent_id: Identifier of the departing agent.
        """
        ...

    async def update_heartbeat(
        self,
        agent_id: str,
        hb: AgentHeartbeatPayload,
        status_changed: bool,
    ) -> None:
        """Apply a heartbeat update to the cache entry for ``agent_id``.

        Called for ``fleet.heartbeat.{agent_id}`` events. When
        ``status_changed`` is ``True`` the entry's status, queue depth,
        and active task count are all updated; when ``False`` only
        ``last_heartbeat_at`` is refreshed (DM-discovery §4 routine vs
        status-change rows).

        Args:
            agent_id: Identifier of the heartbeating agent.
            hb: The decoded heartbeat payload.
            status_changed: ``True`` if the status field differs from
                the previously cached value.
        """
        ...


__all__ = ["Clock", "FleetEventSink", "SystemClock"]
