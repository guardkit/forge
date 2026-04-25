"""Forge fleet integration package.

Exports the :data:`FORGE_MANIFEST` constant published to ``fleet.register``
on agent boot.
"""

from forge.fleet.manifest import FORGE_MANIFEST

__all__ = ["FORGE_MANIFEST"]
