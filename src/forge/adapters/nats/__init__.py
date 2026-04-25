"""NATS adapter package — publishers and consumers for the fleet/pipeline streams.

Re-exports the canonical entrypoints so callers can import from the
package root without knowing the module layout:

- :class:`PipelinePublisher` / :class:`PublishFailure` (lifecycle stream).
- :func:`register_on_boot`, :func:`heartbeat_loop`, :func:`deregister`
  plus the :class:`StatusProvider` and :class:`Clock` injection
  protocols (fleet self-registration).
"""

from forge.adapters.nats.fleet_publisher import (
    AGENT_ID,
    Clock,
    MonotonicClock,
    StatusProvider,
    deregister,
    heartbeat_loop,
    register_on_boot,
)
from forge.adapters.nats.pipeline_publisher import (
    PipelinePublisher,
    PublishFailure,
)

__all__ = [
    "AGENT_ID",
    "Clock",
    "MonotonicClock",
    "PipelinePublisher",
    "PublishFailure",
    "StatusProvider",
    "deregister",
    "heartbeat_loop",
    "register_on_boot",
]
