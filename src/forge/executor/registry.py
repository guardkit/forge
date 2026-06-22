"""Step-type registry and handler protocol (TASK-RBX-001).

Provides the dispatch substrate for the runbook executor:

- :class:`StepHandler` — Protocol defining the handler call signature.
- :class:`StepOutcome` — Value object carrying step execution results.
- :class:`StepTypeRegistry` — Maps step_type keys to handler implementations.

The executor (TASK-RBX-004) holds **no** knowledge of step internals — it only
ever resolves a handler by its ``step_type`` key and invokes it.

Design invariants:

- **Open-closed**: A brand-new step type is supported purely by calling
  ``register(...)`` — no edit to the registry or executor needed.
- **No exceptions**: ``resolve`` returns ``None`` for unregistered types; the
  executor turns ``None`` into an escalation, **never** a crash (ASSUM-002).
- **Terminal status only**: ``StepOutcome`` only admits ``{passed, failed,
  awaiting_approval}`` per ASSUM-008. Constructing it with ``pending`` or
  ``running`` raises ``ValueError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from forge.persistence.repositories.runbook_models import Step, StepStatus

__all__ = [
    "StepHandler",
    "StepOutcome",
    "StepTypeRegistry",
]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Execution outcome returned by a step handler.

    Carries the terminal status (one of ``passed``, ``failed``,
    ``awaiting_approval``) and an optional JSON-serializable result dict.

    The executor (TASK-RBX-004) hands ``result`` verbatim to
    ``update_step_status(..., result=outcome.result)``, so it must be
    JSON-serializable.

    Attributes:
        status: Terminal execution status (ASSUM-008: one of passed, failed,
            awaiting_approval).
        result: JSON-serializable result dict (or None). Persisted verbatim.

    Raises:
        ValueError: If status is not one of the allowed terminal values.
    """

    status: StepStatus
    result: dict[str, Any] | None

    # ASSUM-008: Only terminal statuses are allowed in StepOutcome
    _TERMINAL_STATUSES = frozenset(
        {StepStatus.passed, StepStatus.failed, StepStatus.awaiting_approval}
    )

    def __post_init__(self) -> None:
        """Validate status is a terminal value (ASSUM-008)."""
        if self.status not in self._TERMINAL_STATUSES:
            raise ValueError(
                f"StepOutcome status must be one of {sorted(s.value for s in self._TERMINAL_STATUSES)}, "
                f"got {self.status.value!r}"
            )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class StepHandler(Protocol):
    """Protocol for step execution handlers.

    A handler is any callable that accepts a :class:`Step` and returns a
    :class:`StepOutcome`. The executor resolves handlers via
    :class:`StepTypeRegistry` and invokes them without inspecting internals.

    Structural typing (Protocol) means in-memory fakes can satisfy this
    interface without inheritance — no broker or subprocess required for
    unit tests.

    Call signature:
        (step: Step) -> StepOutcome
    """

    def __call__(self, step: Step) -> StepOutcome: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class StepTypeRegistry:
    """Maps step_type strings to handler implementations.

    The executor queries this registry to resolve a handler for a given
    step. If no handler is registered, ``resolve`` returns ``None`` (the
    executor escalates; it never crashes on unknown types — ASSUM-002).

    Open-closed principle: adding a new step type requires only calling
    ``register(...)`` — no edit to the registry or executor.

    Thread-safety: This implementation is **not** thread-safe. If concurrent
    registration is needed, wrap it in a lock or use a thread-safe dict.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._handlers: dict[str, StepHandler] = {}

    def register(self, step_type: str, handler: StepHandler) -> None:
        """Register a handler for a given step_type.

        If a handler is already registered for this step_type, it is replaced
        (last-write-wins).

        Args:
            step_type: The step type key (e.g., "shell", "http", "approval").
            handler: A callable satisfying the StepHandler protocol.
        """
        self._handlers[step_type] = handler

    def resolve(self, step_type: str) -> StepHandler | None:
        """Resolve the handler registered for a step_type.

        Args:
            step_type: The step type key to look up.

        Returns:
            The registered handler, or None if no handler is registered.
            The executor turns None into an escalation (ASSUM-002).
        """
        return self._handlers.get(step_type)
