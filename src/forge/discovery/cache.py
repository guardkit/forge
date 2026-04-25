"""Async-locked in-memory cache of fleet :class:`AgentManifest` records.

:class:`DiscoveryCache` is the concrete :class:`FleetEventSink`
implementation. The NATS fleet watcher calls
:meth:`~DiscoveryCache.upsert_agent`,
:meth:`~DiscoveryCache.remove_agent`, and
:meth:`~DiscoveryCache.update_heartbeat`; the resolver
(:mod:`forge.discovery.resolve`) reads via
:meth:`~DiscoveryCache.snapshot`.

All mutations and the snapshot read are guarded by a single
``asyncio.Lock`` so concurrent fleet events from a watcher (which can
fan out via ``asyncio.gather``) do not produce torn reads — see
``test_concurrent_upsert_and_remove`` in the AC-006 / AC-COVER row.

This module imports **no NATS transport types**. ``AgentManifest`` and
``AgentHeartbeatPayload`` are domain schema types that happen to be
defined in the ``nats_core`` schema package because that is the
published-contract home — see DM-discovery §1 for the architectural
rationale.
"""

from __future__ import annotations

import asyncio
import logging

from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import AgentManifest

from forge.discovery.models import DiscoveryCacheEntry
from forge.discovery.protocol import Clock, SystemClock

logger = logging.getLogger(__name__)


class DiscoveryCache:
    """Async-safe in-memory cache implementing :class:`FleetEventSink`.

    The cache is keyed by ``agent_id`` and stores a
    :class:`DiscoveryCacheEntry` per fleet agent. Mutation paths
    (upsert, remove, update_heartbeat) are all serialised through a
    single ``asyncio.Lock`` so the watcher can spawn handlers via
    ``asyncio.gather`` without producing torn cache state.

    Args:
        clock: Time provider used for ``cached_at`` and
            ``last_heartbeat_at`` defaults. Defaults to
            :class:`SystemClock` — tests inject a fake.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._entries: dict[str, DiscoveryCacheEntry] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # FleetEventSink protocol implementation
    # ------------------------------------------------------------------

    async def upsert_agent(self, manifest: AgentManifest) -> None:
        """Insert or replace the cache entry for ``manifest.agent_id``.

        Resets the heartbeat snapshot to a fresh ``ready`` state with
        zero queue depth — the agent has just (re-)registered, so any
        previous queue figures are stale.

        Args:
            manifest: The newly published manifest.
        """
        now = self._clock.now()
        async with self._lock:
            self._entries[manifest.agent_id] = DiscoveryCacheEntry(
                manifest=manifest,
                last_heartbeat_at=now,
                last_heartbeat_status="ready",
                last_queue_depth=0,
                last_active_tasks=0,
                cached_at=now,
            )
        logger.debug(
            "discovery.cache.upsert agent_id=%s version=%s",
            manifest.agent_id,
            manifest.version,
        )

    async def remove_agent(self, agent_id: str) -> None:
        """Delete the cache entry for ``agent_id`` if present.

        A no-op when the agent is unknown — never raises ``KeyError``.

        Args:
            agent_id: Identifier of the departing agent.
        """
        async with self._lock:
            removed = self._entries.pop(agent_id, None)
        if removed is None:
            logger.debug(
                "discovery.cache.remove agent_id=%s status=unknown",
                agent_id,
            )
        else:
            logger.debug("discovery.cache.remove agent_id=%s", agent_id)

    async def update_heartbeat(
        self,
        agent_id: str,
        hb: AgentHeartbeatPayload,
        status_changed: bool,
    ) -> None:
        """Apply a heartbeat update to the cache entry for ``agent_id``.

        When ``status_changed`` is ``True`` the heartbeat status, queue
        depth, and active task count are written. When ``False`` only
        ``last_heartbeat_at`` is refreshed — that is the routine
        liveness probe and matches DM-discovery §4 routine row.

        Heartbeats for unknown agents are dropped with a debug log —
        they typically race a deregistration.

        Args:
            agent_id: Identifier of the heartbeating agent.
            hb: The decoded heartbeat payload.
            status_changed: ``True`` if the status field differs from
                the previously cached value.
        """
        now = self._clock.now()
        async with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                logger.debug(
                    "discovery.cache.heartbeat agent_id=%s status=unknown",
                    agent_id,
                )
                return
            if status_changed:
                self._entries[agent_id] = entry.model_copy(
                    update={
                        "last_heartbeat_at": now,
                        "last_heartbeat_status": hb.status,
                        "last_queue_depth": hb.queue_depth,
                        "last_active_tasks": hb.active_tasks,
                    },
                )
            else:
                self._entries[agent_id] = entry.model_copy(
                    update={"last_heartbeat_at": now},
                )

    # ------------------------------------------------------------------
    # Read-side helpers (used by the resolver)
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, DiscoveryCacheEntry]:
        """Return a shallow copy of the cache under the lock.

        Each :class:`DiscoveryCacheEntry` is a frozen pydantic record,
        so a shallow copy of the dict is enough — the resolver cannot
        mutate cache state through the snapshot.

        Returns:
            A ``dict[str, DiscoveryCacheEntry]`` mapping ``agent_id``
            to the latest cached entry.
        """
        async with self._lock:
            return dict(self._entries)

    def __len__(self) -> int:
        """Return the number of cached entries (lock-free, eventually consistent)."""
        return len(self._entries)

    def __contains__(self, agent_id: object) -> bool:
        """Return ``True`` if ``agent_id`` has a cached entry (lock-free)."""
        return agent_id in self._entries


__all__ = ["DiscoveryCache"]
