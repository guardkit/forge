"""Forge dispatch domain — declarative dispatch models.

This package owns the pure-domain pydantic models consumed by the
dispatch tool (TASK-SAD-002+) and produced from
:class:`~forge.discovery.models.CapabilityResolution`.

It deliberately imports **no transport types**: NATS, HTTP, langgraph
nodes, etc. live elsewhere. The dispatch domain only declares schemas.

See ``tasks/backlog/TASK-SAD-001-dispatch-package-skeleton.md`` for the
canonical schema contract and ``IMPLEMENTATION-GUIDE.md`` §4 for the
integration boundaries this package straddles.
"""

from forge.dispatch.models import (
    AsyncPending,
    CorrelationKey,
    Degraded,
    DispatchAttempt,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)

__all__ = [
    "AsyncPending",
    "CorrelationKey",
    "Degraded",
    "DispatchAttempt",
    "DispatchError",
    "DispatchOutcome",
    "SyncResult",
]
