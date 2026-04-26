"""Unit tests for ``forge.memory.models`` (TASK-IC-001).

Each test class maps to one or more acceptance criteria from
``tasks/design_approved/TASK-IC-001-entity-models-and-redaction.md``:

* ``TestPydanticV2BaseModel``                 — AC-001 (all six entity models
                                                  defined with Pydantic v2
                                                  BaseModel)
* ``TestEntityIdSourcedFromSqliteDocstring``  — AC-002 (ASSUM-007 resolution
                                                  documented on every UUID
                                                  ``entity_id``)
* ``TestSessionOutcomeOrdering``              — AC-003 (ASSUM-008 resolution —
                                                  ``gate_decision_ids`` is
                                                  ordered ascending by
                                                  ``decided_at``)
* ``TestCalibrationEventDeterministicId``     — AC-004 (deterministic
                                                  ``CalibrationEvent.entity_id``
                                                  from ``(source_file,
                                                  line_range_hash)``)
* ``TestModelValidationRejectsBadInput``      — AC-007 (model validation
                                                  rejects empty/missing
                                                  required fields)
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

import forge.memory.models as models_module
from forge.memory.models import (
    CalibrationAdjustment,
    CalibrationEvent,
    CapabilityResolution,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ts(year: int = 2026, month: int = 4, day: int = 25, hour: int = 12) -> datetime:
    """Build a fixed UTC datetime for deterministic ordering tests."""
    return datetime(year, month, day, hour, tzinfo=UTC)


def _line_range_hash(source_file: str, start: int, end: int) -> str:
    """Mirror the deterministic-id construction documented in
    ``@data-integrity deterministic-qa-identity``: ``sha256(<file>:<start>-<end>)``.
    """
    payload = f"{source_file}:{start}-{end}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _calibration_event_id(source_file: str, start: int, end: int) -> str:
    return f"{source_file}#{_line_range_hash(source_file, start, end)}"


# ---------------------------------------------------------------------------
# AC-001 — all six entity models defined with Pydantic v2 BaseModel
# ---------------------------------------------------------------------------


class TestPydanticV2BaseModel:
    """AC-001: all six entity models are Pydantic v2 ``BaseModel`` subclasses."""

    @pytest.mark.parametrize(
        "model_cls",
        [
            GateDecision,
            CapabilityResolution,
            OverrideEvent,
            CalibrationAdjustment,
            SessionOutcome,
            CalibrationEvent,
        ],
    )
    def test_inherits_from_pydantic_basemodel(self, model_cls: type[BaseModel]) -> None:
        # Arrange / Act
        is_subclass = issubclass(model_cls, BaseModel)

        # Assert
        assert is_subclass, f"{model_cls.__name__} must subclass pydantic.BaseModel"

    @pytest.mark.parametrize(
        "model_cls",
        [
            GateDecision,
            CapabilityResolution,
            OverrideEvent,
            CalibrationAdjustment,
            SessionOutcome,
            CalibrationEvent,
        ],
    )
    def test_model_uses_pydantic_v2_config(self, model_cls: type[BaseModel]) -> None:
        # Pydantic v2 exposes ``model_config`` (a dict produced by ConfigDict);
        # v1 uses an inner ``Config`` class. We assert the v2 surface.

        # Arrange / Act
        config = getattr(model_cls, "model_config", None)

        # Assert
        assert config is not None, (
            f"{model_cls.__name__} is missing pydantic v2 ``model_config``"
        )
        # v2 also exposes ``model_fields`` on the class itself.
        assert hasattr(model_cls, "model_fields"), (
            f"{model_cls.__name__} does not expose v2 ``model_fields``"
        )

    def test_all_six_models_exported_from_module(self) -> None:
        # Arrange
        expected = {
            "GateDecision",
            "CapabilityResolution",
            "OverrideEvent",
            "CalibrationAdjustment",
            "SessionOutcome",
            "CalibrationEvent",
        }

        # Act
        exported = set(getattr(models_module, "__all__", []))

        # Assert
        missing = expected - exported
        assert not missing, f"models module is missing exports: {missing}"


# ---------------------------------------------------------------------------
# AC-002 — entity_id documented as sourced from SQLite (ASSUM-007 resolution)
# ---------------------------------------------------------------------------


class TestEntityIdSourcedFromSqliteDocstring:
    """AC-002: every UUID ``entity_id`` documents the SQLite-source rule."""

    @pytest.mark.parametrize(
        "model_cls",
        [
            GateDecision,
            CapabilityResolution,
            OverrideEvent,
            CalibrationAdjustment,
            SessionOutcome,
        ],
    )
    def test_entity_id_field_documents_sqlite_source(
        self, model_cls: type[BaseModel]
    ) -> None:
        # Arrange
        field = model_cls.model_fields["entity_id"]
        description = (field.description or "").lower()

        # Act / Assert — the canonical phrase MUST appear so reviewers and
        # downstream consumers can grep for it.
        assert "sqlite" in description, (
            f"{model_cls.__name__}.entity_id description does not "
            f"mention SQLite: {field.description!r}"
        )
        assert "never generated at write time" in description, (
            f"{model_cls.__name__}.entity_id description does not say "
            f"'never generated at write time': {field.description!r}"
        )

    def test_pipeline_history_entity_id_typed_as_uuid(self) -> None:
        # The five pipeline-history entities use UUID typing (per task notes
        # and ASSUM-007 resolution). CalibrationEvent is the deliberate
        # exception (str-typed, deterministic — see TestCalibrationEventDeterministicId).

        # Arrange
        pipeline_models = [
            GateDecision,
            CapabilityResolution,
            OverrideEvent,
            CalibrationAdjustment,
            SessionOutcome,
        ]

        for model_cls in pipeline_models:
            field = model_cls.model_fields["entity_id"]
            # Act
            annotation = field.annotation
            # Assert
            assert annotation is UUID, (
                f"{model_cls.__name__}.entity_id should be typed as UUID, "
                f"got {annotation!r}"
            )


# ---------------------------------------------------------------------------
# AC-003 — SessionOutcome.gate_decision_ids ordered ascending (ASSUM-008)
# ---------------------------------------------------------------------------


class TestSessionOutcomeOrdering:
    """AC-003: ``gate_decision_ids`` is documented as ordered ascending."""

    def test_gate_decision_ids_field_documents_ordering(self) -> None:
        # Arrange
        field = SessionOutcome.model_fields["gate_decision_ids"]
        description = (field.description or "").lower()

        # Act / Assert
        assert "ascending" in description, (
            "SessionOutcome.gate_decision_ids description must mention "
            f"'ascending': {field.description!r}"
        )
        assert "decided_at" in description, (
            "SessionOutcome.gate_decision_ids description must mention "
            f"'decided_at': {field.description!r}"
        )

    def test_session_outcome_accepts_sorted_gate_decision_ids(self) -> None:
        # Even though the model does not enforce ordering at the validator
        # level (callers do — see field docs), it MUST still accept a
        # well-ordered list and round-trip the UUIDs.

        # Arrange
        ids = [uuid4(), uuid4(), uuid4()]

        # Act
        outcome = SessionOutcome(
            entity_id=uuid4(),
            build_id="build-42",
            outcome="success",
            gate_decision_ids=ids,
            closed_at=_ts(),
        )

        # Assert
        assert outcome.gate_decision_ids == ids
        assert outcome.outcome == "success"

    def test_session_outcome_default_gate_decision_ids_is_empty_list(self) -> None:
        # Arrange / Act
        outcome = SessionOutcome(
            entity_id=uuid4(),
            build_id="build-42",
            outcome="aborted",
            closed_at=_ts(),
        )

        # Assert
        assert outcome.gate_decision_ids == []


# ---------------------------------------------------------------------------
# AC-004 — CalibrationEvent.entity_id deterministic from (source_file, line_range_hash)
# ---------------------------------------------------------------------------


class TestCalibrationEventDeterministicId:
    """AC-004: ``CalibrationEvent.entity_id`` is deterministic and idempotent."""

    def test_entity_id_field_is_str_typed(self) -> None:
        # Arrange
        field = CalibrationEvent.model_fields["entity_id"]

        # Act / Assert
        assert field.annotation is str, (
            "CalibrationEvent.entity_id must be str-typed (deterministic "
            "hash), not UUID. The five pipeline-history entities use UUID; "
            f"this one is the deliberate exception. Got {field.annotation!r}."
        )

    def test_entity_id_description_explains_determinism(self) -> None:
        # Arrange
        field = CalibrationEvent.model_fields["entity_id"]
        desc = (field.description or "").lower()

        # Act / Assert
        assert "deterministic" in desc, (
            f"CalibrationEvent.entity_id description must explain that "
            f"the id is deterministic: {field.description!r}"
        )
        assert "source_file" in desc, (
            f"CalibrationEvent.entity_id description must reference "
            f"'source_file': {field.description!r}"
        )
        assert "line_range" in desc.replace("-", "_"), (
            f"CalibrationEvent.entity_id description must reference the "
            f"line range hash: {field.description!r}"
        )

    def test_re_ingestion_of_same_source_yields_same_entity_id(self) -> None:
        # The model itself does not synthesise the id (per task: "callers
        # construct it"), but constructing two events with the same
        # deterministic id MUST produce identical ``entity_id`` values.

        # Arrange
        eid = _calibration_event_id("docs/calibration/log.md", 100, 120)
        common = {
            "source_file": "docs/calibration/log.md",
            "question": "What is the gate threshold?",
            "answer": "0.7 by default.",
            "captured_at": _ts(),
        }

        # Act — two ingestion passes over the same file/range.
        first = CalibrationEvent(entity_id=eid, **common)
        second = CalibrationEvent(entity_id=eid, **common)

        # Assert
        assert first.entity_id == second.entity_id, (
            "Re-ingestion of the same (source_file, line range) must yield "
            "the same CalibrationEvent.entity_id (idempotency invariant)."
        )

    def test_partial_flag_defaults_to_false(self) -> None:
        # Arrange
        eid = _calibration_event_id("docs/calibration/log.md", 1, 5)

        # Act
        event = CalibrationEvent(
            entity_id=eid,
            source_file="docs/calibration/log.md",
            question="Q?",
            answer="A.",
            captured_at=_ts(),
        )

        # Assert
        assert event.partial is False


# ---------------------------------------------------------------------------
# AC-007 — model validation rejects empty/missing required fields
# ---------------------------------------------------------------------------


class TestModelValidationRejectsBadInput:
    """AC-007: each model rejects empty/missing required fields with a
    Pydantic ``ValidationError`` rather than silently coercing.
    """

    def test_gate_decision_rejects_missing_entity_id(self) -> None:
        with pytest.raises(ValidationError):
            GateDecision(
                stage_name="planning",
                decided_at=_ts(),
                score=0.9,
                criterion_breakdown={"clarity": 0.9},
                rationale="ok",
            )

    def test_gate_decision_rejects_empty_stage_name(self) -> None:
        with pytest.raises(ValidationError):
            GateDecision(
                entity_id=uuid4(),
                stage_name="",
                decided_at=_ts(),
                score=0.9,
                criterion_breakdown={},
                rationale="ok",
            )

    def test_gate_decision_rejects_score_outside_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            GateDecision(
                entity_id=uuid4(),
                stage_name="planning",
                decided_at=_ts(),
                score=1.5,  # invalid — > 1.0
                criterion_breakdown={},
                rationale="ok",
            )

    def test_capability_resolution_rejects_empty_capability(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityResolution(
                entity_id=uuid4(),
                agent_id="agent-1",
                capability="",  # empty
                selected_at=_ts(),
                discovery_cache_version="v1",
            )

    def test_override_event_rejects_missing_gate_decision_id(self) -> None:
        with pytest.raises(ValidationError):
            OverrideEvent(
                entity_id=uuid4(),
                original_recommendation="auto-approve",
                operator_decision="reject",
                operator_rationale="not enough confidence",
                decided_at=_ts(),
            )

    def test_calibration_adjustment_rejects_missing_approved(self) -> None:
        with pytest.raises(ValidationError):
            CalibrationAdjustment(
                entity_id=uuid4(),
                parameter="threshold",
                old_value="0.7",
                new_value="0.8",
                # approved missing
                proposed_at=_ts(),
                expires_at=_ts() + timedelta(days=1),
            )

    def test_calibration_adjustment_supersedes_defaults_to_none(self) -> None:
        # Positive companion: when ``supersedes`` is omitted it defaults to
        # ``None`` (i.e. "first adjustment").
        adj = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="threshold",
            old_value="0.7",
            new_value="0.8",
            approved=False,
            proposed_at=_ts(),
            expires_at=_ts() + timedelta(days=1),
        )
        assert adj.supersedes is None

    def test_session_outcome_rejects_invalid_outcome_literal(self) -> None:
        with pytest.raises(ValidationError):
            SessionOutcome(
                entity_id=uuid4(),
                build_id="build-1",
                outcome="catastrophe",  # not a valid literal
                gate_decision_ids=[],
                closed_at=_ts(),
            )

    def test_session_outcome_rejects_empty_build_id(self) -> None:
        with pytest.raises(ValidationError):
            SessionOutcome(
                entity_id=uuid4(),
                build_id="",
                outcome="success",
                gate_decision_ids=[],
                closed_at=_ts(),
            )

    def test_calibration_event_rejects_empty_entity_id(self) -> None:
        with pytest.raises(ValidationError):
            CalibrationEvent(
                entity_id="",
                source_file="docs/log.md",
                question="Q?",
                answer="A.",
                captured_at=_ts(),
            )

    def test_calibration_event_rejects_empty_source_file(self) -> None:
        with pytest.raises(ValidationError):
            CalibrationEvent(
                entity_id=_calibration_event_id("docs/log.md", 1, 5),
                source_file="",
                question="Q?",
                answer="A.",
                captured_at=_ts(),
            )


# ---------------------------------------------------------------------------
# Module-level smoke checks
# ---------------------------------------------------------------------------


class TestModuleSmoke:
    """Quick smoke checks: docstring presence, public-symbol stability."""

    def test_module_has_docstring(self) -> None:
        assert (models_module.__doc__ or "").strip(), (
            "forge.memory.models must have a module docstring"
        )

    def test_module_does_not_import_io_layers(self) -> None:
        # Pure-schema modules must not transitively pull in network/db
        # layers. We parse the AST so the docstring's mention of forbidden
        # names (e.g. "no ``nats_core`` imports") doesn't trip the check.
        import ast as _ast

        tree = _ast.parse(inspect.getsource(models_module))
        forbidden = ("nats_core", "langgraph", "forge.adapters", "asyncpg", "sqlite3")
        imports: list[str] = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, _ast.ImportFrom):
                if node.module is not None:
                    imports.append(node.module)
        for needle in forbidden:
            for imported in imports:
                assert not imported.startswith(needle), (
                    f"forge.memory.models must not import {needle!r}; "
                    f"found import {imported!r}"
                )
