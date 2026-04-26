"""Forge GuardKit adapter package.

Re-exports the Pydantic v2 models for the GuardKit command invocation
engine (FEAT-FORGE-005):

- :class:`GuardKitResult` / :class:`GuardKitWarning` — canonical shape
  returned by every ``forge.adapters.guardkit.run()`` call (see
  :mod:`forge.adapters.guardkit.models` / TASK-GCI-001).
- :class:`GuardKitProgressEvent` — typed shape of a single
  ``pipeline.stage-complete.*`` NATS message consumed by the
  progress-stream subscriber (see
  :mod:`forge.adapters.guardkit.progress` / TASK-GCI-005).

Importing from ``forge.adapters.guardkit`` keeps call sites short and
decoupled from the internal module layout. The shim mirrors the
re-export pattern used in ``forge.config.__init__``.
"""

from .models import GuardKitResult, GuardKitWarning
from .progress import GuardKitProgressEvent

__all__ = [
    "GuardKitProgressEvent",
    "GuardKitResult",
    "GuardKitWarning",
]
