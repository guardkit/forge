"""Runbook executor components (FEAT-RBX).

Provides the step-type registry, handler protocol, and executor dispatch loop
for the Software Factory's runbook automation.
"""

from .registry import StepHandler, StepOutcome, StepTypeRegistry

__all__ = [
    "StepHandler",
    "StepOutcome",
    "StepTypeRegistry",
]
