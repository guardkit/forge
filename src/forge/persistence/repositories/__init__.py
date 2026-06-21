"""Repository facades over the :mod:`forge.persistence` substrate."""

from __future__ import annotations

from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
    BridgeRegistryNotFoundError,
)
from forge.persistence.repositories.runbook import (
    RunbookAdvanceError,
    RunbookDuplicateError,
    RunbookNotFoundError,
    RunbookRepository,
    RunbookStepNotFoundError,
)
from forge.persistence.repositories.runbook_models import (
    Runbook,
    RunbookValidationError,
    Step,
    StepResult,
    StepStatus,
)

__all__ = [
    "BridgeRegistry",
    "BridgeRegistryEntry",
    "BridgeRegistryNotFoundError",
    "Runbook",
    "RunbookAdvanceError",
    "RunbookDuplicateError",
    "RunbookNotFoundError",
    "RunbookRepository",
    "RunbookStepNotFoundError",
    "RunbookValidationError",
    "Step",
    "StepResult",
    "StepStatus",
]
