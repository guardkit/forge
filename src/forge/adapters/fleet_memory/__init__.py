"""Fleet-memory adapters — the gate's priors read over the fleet store.

Public surface: :class:`FleetMemoryPriorsConfig`,
:class:`FleetMemoryPriorsReader`, and the env-gated composition factory
:func:`build_priors_reader_from_env`. Constraints (loop affinity,
project scoping, activation ruling) live in :mod:`.priors`.
"""

from __future__ import annotations

from forge.adapters.fleet_memory.priors import (
    FleetMemoryPriorsConfig,
    FleetMemoryPriorsReader,
    build_priors_reader_from_env,
)

__all__ = [
    "FleetMemoryPriorsConfig",
    "FleetMemoryPriorsReader",
    "build_priors_reader_from_env",
]
