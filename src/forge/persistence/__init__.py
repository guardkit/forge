"""Forge persistence layer (TASK-FRR-PEB-002 onwards).

This package hosts the schema migrations and repository facades that
extend the original :mod:`forge.lifecycle` substrate with auxiliary
tables (e.g. the lifecycle bridge in-flight registry).

The split between :mod:`forge.lifecycle.persistence` (the original
``builds`` / ``stage_log`` substrate) and :mod:`forge.persistence` is
deliberate — see TASK-FRR-PEB-002 §Implementation notes for the
rationale: the lifecycle bridge is a structural concern that lives
*beside* the build state machine rather than inside it, so its schema
and repositories belong in a sibling package.
"""

from __future__ import annotations

__all__: list[str] = []
