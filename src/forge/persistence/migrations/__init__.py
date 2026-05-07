"""Schema migrations for the :mod:`forge.persistence` substrate.

Each module in this package exposes a top-level ``apply(connection)``
function that materialises (or upgrades) a single auxiliary table.
Migrations here are deliberately additive and idempotent — re-running
them against an already-migrated database is a no-op.

Boot wiring (TASK-FRR-PEB-004) will compose these helpers after
:func:`forge.lifecycle.migrations.apply_at_boot` so the auxiliary
tables share the same writer connection and transactional discipline
as the canonical ``builds`` / ``stage_log`` substrate.
"""

from __future__ import annotations

from forge.persistence.migrations import lifecycle_bridge_registry

__all__ = ["lifecycle_bridge_registry"]
