"""Forge discovery domain — pure-domain capability resolution.

This package owns the in-memory fleet cache and the resolution
algorithm that maps a ``(tool_name, intent_pattern)`` request to a
fleet agent. It deliberately imports **no NATS transport types**:

* ``AgentManifest`` and ``AgentHeartbeatPayload`` are imported from
  :mod:`nats_core` because they are the published contract schemas —
  they are domain types, not transport.
* Transport-layer NATS APIs (the asyncio NATS client, top-level
  ``nats`` package, client classes) never appear in this package — the
  fleet watcher (a separate adapter, TASK-NFI-005) speaks NATS and
  forwards events through the :class:`FleetEventSink` protocol.

See ``docs/design/models/DM-discovery.md`` for the canonical design.
"""

from forge.discovery.cache import DiscoveryCache
from forge.discovery.models import (
    CapabilityResolution,
    DiscoveryCacheEntry,
    HeartbeatStatus,
    MatchSource,
    TrustTier,
)
from forge.discovery.protocol import Clock, FleetEventSink, SystemClock
from forge.discovery.resolve import resolve

__all__ = [
    "CapabilityResolution",
    "Clock",
    "DiscoveryCache",
    "DiscoveryCacheEntry",
    "FleetEventSink",
    "HeartbeatStatus",
    "MatchSource",
    "SystemClock",
    "TrustTier",
    "resolve",
]
