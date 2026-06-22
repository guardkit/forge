"""Runbook executor components (FEAT-RBX).

Provides the step-type registry, handler protocol, and executor dispatch loop
for the Software Factory's runbook automation.
"""

from .registry import StepHandler, StepOutcome, StepTypeRegistry
from .shell_steps import deploy_compose, register_shell_handlers, run_smoke_tests

__all__ = [
    "StepHandler",
    "StepOutcome",
    "StepTypeRegistry",
    "deploy_compose",
    "run_smoke_tests",
    "register_shell_handlers",
]
