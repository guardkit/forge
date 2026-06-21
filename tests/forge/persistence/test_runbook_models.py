"""Tests for ``forge.persistence.repositories.runbook_models`` (TASK-RSP-001).

Acceptance-criteria coverage map:

* AC-1: ``forge.persistence.repositories.runbook_models`` exists and exports
  ``StepStatus``, ``StepResult``, ``Step``, ``Runbook``, ``RunbookValidationError``
  — :class:`TestImports`.
* AC-2: ``StepStatus`` has exactly the five members
  ``pending/running/passed/failed/awaiting_approval`` —
  :class:`TestStepStatusEnum`.
* AC-3: Constructing a ``Runbook`` with an empty ``steps`` tuple raises
  ``RunbookValidationError`` (ASSUM-002) — :class:`TestRunbookValidation`.
* AC-4: Constructing a ``Runbook`` with ``current_step_index`` outside
  ``[0, len(steps)]`` raises ``RunbookValidationError`` (ASSUM-004, R1) —
  :class:`TestRunbookValidation`.
* AC-5: Constructing a ``Step`` with an empty ``step_type`` raises
  ``RunbookValidationError`` (ASSUM-005) — :class:`TestStepValidation`.
* AC-6: A freshly constructed three-step ``Runbook`` with
  ``current_step_index=0`` and every step ``StepStatus.pending`` is valid
  and equality-comparable — :class:`TestRunbookConstruction`.
* AC-7: ``Step.result`` defaults to ``None``; a ``StepResult`` round-trips
  its fields via dataclass equality — :class:`TestStepResultRoundTrip`.
* AC-8: Models are immutable: attempting to set an attribute raises
  (``frozen=True``) — :class:`TestImmutability`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge.persistence.repositories.runbook_models import (
    Runbook,
    RunbookValidationError,
    Step,
    StepResult,
    StepStatus,
)


# ---------------------------------------------------------------------------
# AC-1: Imports
# ---------------------------------------------------------------------------


class TestImports:
    """Verify all expected names are exported from runbook_models."""

    def test_all_names_importable(self) -> None:
        """All required names are importable from runbook_models."""
        from forge.persistence.repositories.runbook_models import (
            Runbook,
            RunbookValidationError,
            Step,
            StepResult,
            StepStatus,
        )

        assert StepStatus is not None
        assert StepResult is not None
        assert Step is not None
        assert Runbook is not None
        assert RunbookValidationError is not None


# ---------------------------------------------------------------------------
# AC-2: StepStatus enum membership
# ---------------------------------------------------------------------------


class TestStepStatusEnum:
    """Verify StepStatus has exactly the five expected members."""

    def test_step_status_has_five_members(self) -> None:
        """StepStatus has exactly pending/running/passed/failed/awaiting_approval."""
        expected = {"pending", "running", "passed", "failed", "awaiting_approval"}
        actual = {status.value for status in StepStatus}
        assert actual == expected, f"Expected {expected}, got {actual}"

    def test_step_status_members_are_strings(self) -> None:
        """All StepStatus members have string values."""
        for status in StepStatus:
            assert isinstance(status.value, str)


# ---------------------------------------------------------------------------
# AC-3, AC-4: Runbook validation
# ---------------------------------------------------------------------------


class TestRunbookValidation:
    """Verify Runbook __post_init__ validation (ASSUM-002, ASSUM-004)."""

    def test_empty_steps_raises_validation_error(self) -> None:
        """Constructing a Runbook with empty steps raises RunbookValidationError."""
        with pytest.raises(RunbookValidationError) as exc_info:
            Runbook(
                runbook_id="rb-001",
                target="test-target",
                steps=(),
                current_step_index=0,
                status=StepStatus.pending,
                created_at=datetime.now(UTC),
            )
        assert "at least one step" in str(exc_info.value).lower()

    def test_current_step_index_negative_raises_validation_error(self) -> None:
        """Constructing a Runbook with negative current_step_index raises."""
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        with pytest.raises(RunbookValidationError) as exc_info:
            Runbook(
                runbook_id="rb-001",
                target="test-target",
                steps=(step,),
                current_step_index=-1,
                status=StepStatus.pending,
                created_at=datetime.now(UTC),
            )
        assert "current_step_index" in str(exc_info.value).lower()

    def test_current_step_index_beyond_terminal_raises_validation_error(self) -> None:
        """Constructing a Runbook with current_step_index > len(steps) raises.

        R1 (reconciled with FEAT-RBX): the terminal position len(steps) is
        valid; only an index strictly beyond it is rejected. For a one-step
        runbook the valid range is [0, 1]; index 2 is beyond terminal.
        """
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        with pytest.raises(RunbookValidationError) as exc_info:
            Runbook(
                runbook_id="rb-001",
                target="test-target",
                steps=(step,),
                current_step_index=2,  # one step → valid [0, 1]; 2 is beyond terminal
                status=StepStatus.pending,
                created_at=datetime.now(UTC),
            )
        assert "current_step_index" in str(exc_info.value).lower()

    def test_current_step_index_at_terminal_position_is_valid(self) -> None:
        """current_step_index == len(steps) (the completion marker) is valid.

        R1 (reconciled with FEAT-RBX): the resume pointer may rest one past the
        last step to mark the runbook complete.
        """
        steps = tuple(
            Step(
                step_type=f"step{i}",
                params={},
                status=StepStatus.passed,
                sequence_index=i,
            )
            for i in range(3)
        )
        runbook = Runbook(
            runbook_id="rb-terminal",
            target="test-target",
            steps=steps,
            current_step_index=3,  # == len(steps): terminal/complete
            status=StepStatus.passed,
            created_at=datetime.now(UTC),
        )
        assert runbook.current_step_index == len(runbook.steps)


# ---------------------------------------------------------------------------
# AC-5: Step validation
# ---------------------------------------------------------------------------


class TestStepValidation:
    """Verify Step __post_init__ validation (ASSUM-005)."""

    def test_empty_step_type_raises_validation_error(self) -> None:
        """Constructing a Step with empty step_type raises RunbookValidationError."""
        with pytest.raises(RunbookValidationError) as exc_info:
            Step(
                step_type="",
                params={},
                status=StepStatus.pending,
                sequence_index=0,
            )
        assert "step_type" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# AC-6: Runbook construction and equality
# ---------------------------------------------------------------------------


class TestRunbookConstruction:
    """Verify valid Runbook construction and equality."""

    def test_three_step_runbook_is_valid_and_comparable(self) -> None:
        """A three-step Runbook with current_step_index=0 is valid and comparable."""
        now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
        step1 = Step(
            step_type="init",
            params={"param": "value1"},
            status=StepStatus.pending,
            sequence_index=0,
        )
        step2 = Step(
            step_type="process",
            params={"param": "value2"},
            status=StepStatus.pending,
            sequence_index=1,
        )
        step3 = Step(
            step_type="finalize",
            params={"param": "value3"},
            status=StepStatus.pending,
            sequence_index=2,
        )

        runbook = Runbook(
            runbook_id="rb-001",
            target="test-target",
            steps=(step1, step2, step3),
            current_step_index=0,
            status=StepStatus.pending,
            created_at=now,
        )

        assert runbook.runbook_id == "rb-001"
        assert runbook.target == "test-target"
        assert len(runbook.steps) == 3
        assert runbook.current_step_index == 0
        assert runbook.status == StepStatus.pending
        assert runbook.created_at == now

        # Test equality (frozen dataclass)
        runbook2 = Runbook(
            runbook_id="rb-001",
            target="test-target",
            steps=(step1, step2, step3),
            current_step_index=0,
            status=StepStatus.pending,
            created_at=now,
        )
        assert runbook == runbook2


# ---------------------------------------------------------------------------
# AC-7: StepResult round-trip
# ---------------------------------------------------------------------------


class TestStepResultRoundTrip:
    """Verify Step.result defaults to None and StepResult round-trips."""

    def test_step_result_defaults_to_none(self) -> None:
        """Step.result defaults to None when not provided."""
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        assert step.result is None

    def test_step_result_can_be_set(self) -> None:
        """Step.result can be set to a StepResult instance."""
        started = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2026, 6, 21, 12, 1, 0, tzinfo=UTC)
        result = StepResult(
            exit_code=0,
            captured_output="test output",
            started_at=started,
            completed_at=completed,
        )
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.passed,
            sequence_index=0,
            result=result,
        )
        assert step.result == result

    def test_step_result_round_trips_via_equality(self) -> None:
        """StepResult round-trips its fields via dataclass equality."""
        started = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2026, 6, 21, 12, 1, 0, tzinfo=UTC)
        result1 = StepResult(
            exit_code=0,
            captured_output="test output",
            started_at=started,
            completed_at=completed,
        )
        result2 = StepResult(
            exit_code=0,
            captured_output="test output",
            started_at=started,
            completed_at=completed,
        )
        assert result1 == result2
        assert result1.exit_code == 0
        assert result1.captured_output == "test output"
        assert result1.started_at == started
        assert result1.completed_at == completed


# ---------------------------------------------------------------------------
# AC-8: Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """Verify models are immutable (frozen=True)."""

    def test_runbook_is_frozen(self) -> None:
        """Attempting to set an attribute on a Runbook raises."""
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = Runbook(
            runbook_id="rb-001",
            target="test-target",
            steps=(step,),
            current_step_index=0,
            status=StepStatus.pending,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(AttributeError):
            runbook.status = StepStatus.running  # type: ignore[misc]

    def test_step_is_frozen(self) -> None:
        """Attempting to set an attribute on a Step raises."""
        step = Step(
            step_type="test",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        with pytest.raises(AttributeError):
            step.status = StepStatus.running  # type: ignore[misc]

    def test_step_result_is_frozen(self) -> None:
        """Attempting to set an attribute on a StepResult raises."""
        result = StepResult(
            exit_code=0,
            captured_output="test output",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        with pytest.raises(AttributeError):
            result.exit_code = 1  # type: ignore[misc]
