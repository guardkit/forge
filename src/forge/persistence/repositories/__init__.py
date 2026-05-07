"""Repository facades over the :mod:`forge.persistence` substrate."""

from __future__ import annotations

from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
    BridgeRegistryNotFoundError,
)

__all__ = [
    "BridgeRegistry",
    "BridgeRegistryEntry",
    "BridgeRegistryNotFoundError",
]
