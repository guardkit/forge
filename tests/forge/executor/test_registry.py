"""Tests for step-type registry and handler protocol (TASK-RBX-001).

Test-first implementation following acceptance criteria:
- AC-001: resolve returns registered handler
- AC-002: resolve returns None for unregistered step_type
- AC-003: register enables dispatch for previously-unknown step_type
- AC-004: StepOutcome validates terminal status values only
- AC-005: StepHandler is a Protocol (structural typing)
- AC-006: pytest marks registered without warnings
"""

from __future__ import annotations

import pytest

from forge.executor.registry import StepHandler, StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook_models import Step, StepStatus


class TestStepOutcome:
    """AC-004: StepOutcome only admits terminal status values."""

    def test_accepts_passed_status(self) -> None:
        """Terminal status 'passed' is valid."""
        outcome = StepOutcome(status=StepStatus.passed, result=None)
        assert outcome.status == StepStatus.passed
        assert outcome.result is None

    def test_accepts_failed_status(self) -> None:
        """Terminal status 'failed' is valid."""
        outcome = StepOutcome(status=StepStatus.failed, result={"error": "timeout"})
        assert outcome.status == StepStatus.failed
        assert outcome.result == {"error": "timeout"}

    def test_accepts_awaiting_approval_status(self) -> None:
        """Terminal status 'awaiting_approval' is valid."""
        outcome = StepOutcome(status=StepStatus.awaiting_approval, result=None)
        assert outcome.status == StepStatus.awaiting_approval

    def test_rejects_pending_status(self) -> None:
        """Non-terminal status 'pending' raises ValueError."""
        with pytest.raises(ValueError, match="status must be one of"):
            StepOutcome(status=StepStatus.pending, result=None)

    def test_rejects_running_status(self) -> None:
        """Non-terminal status 'running' raises ValueError."""
        with pytest.raises(ValueError, match="status must be one of"):
            StepOutcome(status=StepStatus.running, result=None)

    def test_result_is_json_serializable(self) -> None:
        """Result is JSON-serializable dict or None."""
        import json

        outcome = StepOutcome(
            status=StepStatus.passed,
            result={"key": "value", "count": 42, "nested": {"flag": True}},
        )
        # Should not raise
        json.dumps(outcome.result)


class TestStepHandler:
    """AC-005: StepHandler is a Protocol with structural typing."""

    def test_handler_protocol_structural_typing(self) -> None:
        """A callable with matching signature satisfies StepHandler protocol."""

        # In-memory fake handler — no inheritance needed
        def fake_handler(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result=None)

        # Type checker should accept this
        handler: StepHandler = fake_handler

        # Runtime verification
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        outcome = handler(step)
        assert isinstance(outcome, StepOutcome)


class TestStepTypeRegistry:
    """AC-001, AC-002, AC-003: Registry resolution behavior."""

    def test_resolve_returns_registered_handler(self) -> None:
        """AC-001: resolve returns the handler registered for a step_type."""
        registry = StepTypeRegistry()

        def test_handler(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result=None)

        registry.register("shell", test_handler)
        resolved = registry.resolve("shell")

        assert resolved is test_handler

    def test_resolve_returns_none_for_unregistered_type(self) -> None:
        """AC-002: resolve returns None for unregistered step_type."""
        registry = StepTypeRegistry()
        resolved = registry.resolve("unknown")
        assert resolved is None

    def test_register_enables_dispatch_for_new_type(self) -> None:
        """AC-003: A new step_type becomes dispatchable via register only."""
        registry = StepTypeRegistry()

        # Initially unregistered
        assert registry.resolve("approval") is None

        # Register handler
        def approval_handler(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.awaiting_approval, result=None)

        registry.register("approval", approval_handler)

        # Now resolvable
        resolved = registry.resolve("approval")
        assert resolved is approval_handler

        # Can invoke it
        step = Step(
            step_type="approval",
            params={"approver": "alice"},
            status=StepStatus.pending,
            sequence_index=0,
        )
        outcome = resolved(step)
        assert outcome.status == StepStatus.awaiting_approval

    def test_multiple_handlers_coexist(self) -> None:
        """Registry holds multiple independent handlers."""
        registry = StepTypeRegistry()

        def shell_handler(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result={"exit_code": 0})

        def http_handler(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result={"status_code": 200})

        registry.register("shell", shell_handler)
        registry.register("http", http_handler)

        assert registry.resolve("shell") is shell_handler
        assert registry.resolve("http") is http_handler

    def test_overwrite_handler_for_same_type(self) -> None:
        """Registering same step_type twice replaces the handler."""
        registry = StepTypeRegistry()

        def handler_v1(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result={"version": 1})

        def handler_v2(step: Step) -> StepOutcome:
            return StepOutcome(status=StepStatus.passed, result={"version": 2})

        registry.register("shell", handler_v1)
        registry.register("shell", handler_v2)

        resolved = registry.resolve("shell")
        assert resolved is handler_v2


@pytest.mark.runbook_executor
@pytest.mark.smoke
def test_pytest_mark_registered() -> None:
    """AC-006: runbook_executor mark does not emit unknown-mark warning."""
    # This test exists to verify the mark is registered in pyproject.toml.
    # If the mark is unregistered, pytest will emit a warning at collection time.
    pass
