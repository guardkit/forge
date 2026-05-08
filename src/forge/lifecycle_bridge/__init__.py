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
from forge.lifecycle_bridge.coexistence import TerminalPublishLedger
from forge.lifecycle_bridge.run_state_source import (
    RUN_STATUS_TERMINAL,
    RunStateFetcher,
    RunStateSnapshot,
    langgraph_run_state_fetcher,
)
from forge.lifecycle_bridge.stream_source import langgraph_stream_source
from forge.lifecycle_bridge.translation import StreamEventTranslator
from forge.lifecycle_bridge.wireup import (
    LifecycleBridgeWireup,
    StreamSource,
)

__all__ = [
    "AckHandle",
    "BuildContext",
    "LifecycleBridge",
    "LifecycleBridgeWireup",
    "RUN_STATUS_TERMINAL",
    "RunStateFetcher",
    "RunStateSnapshot",
    "StreamEventTranslator",
    "StreamSource",
    "TerminalPublishLedger",
    "langgraph_run_state_fetcher",
    "langgraph_stream_source",
]
