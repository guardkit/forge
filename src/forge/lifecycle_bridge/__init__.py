"""Forge lifecycle bridge (TASK-FRR-PEB-002).

The :class:`LifecycleBridge` owns the SSE connection lifecycle to the
``langgraph-runner`` sidecar. T2 stands up the structural foundation:
the public method surface plus the SQLite-backed in-flight registry.
SSE envelope translation arrives in T3, ``forge serve`` startup wiring
in T4, and crash-recovery in T9.
"""

from __future__ import annotations

from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    LifecycleBridge,
)

__all__ = [
    "AckHandle",
    "BuildContext",
    "LifecycleBridge",
]
