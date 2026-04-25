"""Unit tests for ``forge.gating.models`` (TASK-CGCP-001).

Each test class maps to one acceptance criterion in
``tasks/backlog/TASK-CGCP-001-define-gating-module-structure.md``:

* ``TestGateModeEnum``                        — AC-001
* ``TestPriorReferenceModel``                 — AC-002
* ``TestDetectionFindingModel``               — AC-003
* ``TestGateDecisionFieldsAndInvariants``     — AC-004 / AC-005 / AC-006 / AC-007
* ``TestCalibrationAdjustmentModel``          — AC-008
* ``TestResponseKindEnum``                    — AC-009
* ``TestEvaluateGateStub``                    — AC-010
* ``TestModulePurity``                        — AC-011

AC-012 (lint/format) is enforced by the project's lint pipeline and is
not unit-testable.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.gating import (
    CalibrationAdjustment,
    ConstitutionalRule,
    DetectionFinding,
    GateDecision,
    GateMode,
    PriorReference,
    ResponseKind,
    evaluate_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decided_at() -> datetime:
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _minimal_decision_kwargs(**overrides: object) -> dict[str, object]:
    """Build the minimal-but-valid set of kwargs for :class:`GateDecision`.

    Defaults keep the §6 invariants satisfied. Tests override only the
    fields they care about.
    """
    base: dict[str, object] = {
        "build_id": "build-123",
        "stage_label": "review",
        "target_kind": "fleet_capability",
        "target_identifier": "review_specification",
        "mode": GateMode.AUTO_APPROVE,
        "rationale": "Score above prior-derived threshold.",
        "coach_score": 0.92,
        "criterion_breakdown": {"completeness": 0.95, "correctness": 0.9},
        "detection_findings": [],
        "evidence": [],
        "threshold_applied": 0.85,
        "auto_approve_override": False,
        "degraded_mode": False,
        "decided_at": _decided_at(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# AC-001 — GateMode enum
# ---------------------------------------------------------------------------


class TestGateModeEnum:
    """AC-001: GateMode enum with four members."""

    def test_gate_mode_has_exactly_four_members(self) -> None:
        assert {m.name for m in GateMode} == {
            "AUTO_APPROVE",
            "FLAG_FOR_REVIEW",
            "HARD_STOP",
            "MANDATORY_HUMAN_APPROVAL",
        }

    def test_gate_mode_values_match_names(self) -> None:
        # DM-gating §1 — values are the upper-case spellings.
        for member in GateMode:
            assert member.value == member.name

    def test_gate_mode_is_str_enum(self) -> None:
        assert isinstance(GateMode.AUTO_APPROVE, str)


# ---------------------------------------------------------------------------
# AC-002 — PriorReference
# ---------------------------------------------------------------------------


class TestPriorReferenceModel:
    """AC-002: PriorReference with entity_id, group_id (Literal), summary, relevance_score."""

    def test_prior_reference_with_all_fields_constructs(self) -> None:
        prior = PriorReference(
            entity_id="abc-123",
            group_id="forge_pipeline_history",
            summary="Prior build flagged for review at 0.78.",
            relevance_score=0.7,
        )
        assert prior.entity_id == "abc-123"
        assert prior.group_id == "forge_pipeline_history"
        assert prior.summary.startswith("Prior build")
        assert prior.relevance_score == 0.7

    def test_prior_reference_relevance_score_is_optional(self) -> None:
        prior = PriorReference(
            entity_id="abc-123",
            group_id="forge_calibration_history",
            summary="Calibration event.",
        )
        assert prior.relevance_score is None

    def test_prior_reference_rejects_unknown_group_id(self) -> None:
        with pytest.raises(ValidationError):
            PriorReference(
                entity_id="abc-123",
                group_id="not_a_real_group",  # type: ignore[arg-type]
                summary="…",
            )

    def test_prior_reference_relevance_score_must_be_in_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            PriorReference(
                entity_id="abc",
                group_id="forge_pipeline_history",
                summary="…",
                relevance_score=1.5,
            )


# ---------------------------------------------------------------------------
# AC-003 — DetectionFinding
# ---------------------------------------------------------------------------


class TestDetectionFindingModel:
    """AC-003: DetectionFinding with pattern, severity (Literal), evidence, criterion."""

    def test_detection_finding_with_all_fields_constructs(self) -> None:
        finding = DetectionFinding(
            pattern="PHANTOM",
            severity="high",
            evidence="No grounding for claim 'X' in build_plan.md:42.",
            criterion="completeness",
        )
        assert finding.pattern == "PHANTOM"
        assert finding.severity == "high"
        assert finding.criterion == "completeness"

    def test_detection_finding_criterion_is_optional(self) -> None:
        finding = DetectionFinding(
            pattern="UNGROUNDED",
            severity="low",
            evidence="…",
        )
        assert finding.criterion is None

    @pytest.mark.parametrize(
        "severity",
        ["low", "medium", "high", "critical"],
    )
    def test_detection_finding_accepts_valid_severities(self, severity: str) -> None:
        DetectionFinding(pattern="P", severity=severity, evidence="e")

    def test_detection_finding_rejects_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            DetectionFinding(
                pattern="P",
                severity="catastrophic",  # type: ignore[arg-type]
                evidence="e",
            )


# ---------------------------------------------------------------------------
# AC-004 / AC-005 / AC-006 / AC-007 — GateDecision and §6 invariants
# ---------------------------------------------------------------------------


class TestGateDecisionFieldsAndInvariants:
    """AC-004: full DM-gating §1 field set; AC-005..AC-007 enforce §6."""

    def test_gate_decision_with_minimal_valid_kwargs_constructs(self) -> None:
        decision = GateDecision(**_minimal_decision_kwargs())
        assert decision.mode is GateMode.AUTO_APPROVE
        # Defaults from DM-gating §1.
        assert decision.detection_findings == []
        assert decision.evidence == []
        assert decision.auto_approve_override is False
        assert decision.degraded_mode is False

    def test_gate_decision_exposes_every_dm_gating_field(self) -> None:
        # DM-gating §1 GateDecision schema. If a field is added to the
        # spec it should be added here so the test catches the omission.
        expected_fields = {
            "build_id",
            "stage_label",
            "target_kind",
            "target_identifier",
            "mode",
            "rationale",
            "coach_score",
            "criterion_breakdown",
            "detection_findings",
            "evidence",
            "threshold_applied",
            "auto_approve_override",
            "degraded_mode",
            "decided_at",
        }
        assert set(GateDecision.model_fields).issuperset(expected_fields)

    # ----- AC-005 — mandatory mode requires override flag OR no threshold ----

    def test_mandatory_mode_with_override_and_threshold_is_allowed(self) -> None:
        # auto_approve_override=True ⇒ allowed even with a threshold.
        decision = GateDecision(
            **_minimal_decision_kwargs(
                mode=GateMode.MANDATORY_HUMAN_APPROVAL,
                auto_approve_override=True,
                threshold_applied=0.85,
            ),
        )
        assert decision.mode is GateMode.MANDATORY_HUMAN_APPROVAL

    def test_mandatory_mode_with_no_threshold_is_allowed(self) -> None:
        # threshold_applied is None ⇒ allowed even without override flag.
        decision = GateDecision(
            **_minimal_decision_kwargs(
                mode=GateMode.MANDATORY_HUMAN_APPROVAL,
                auto_approve_override=False,
                threshold_applied=None,
            ),
        )
        assert decision.threshold_applied is None

    def test_mandatory_mode_with_threshold_and_no_override_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GateDecision(
                **_minimal_decision_kwargs(
                    mode=GateMode.MANDATORY_HUMAN_APPROVAL,
                    auto_approve_override=False,
                    threshold_applied=0.85,
                ),
            )
        assert "MANDATORY_HUMAN_APPROVAL" in str(exc_info.value)

    # ----- AC-006 — degraded mode (coach_score is None) cannot auto-approve --

    def test_degraded_mode_cannot_auto_approve(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GateDecision(
                **_minimal_decision_kwargs(
                    mode=GateMode.AUTO_APPROVE,
                    coach_score=None,
                    threshold_applied=None,
                ),
            )
        assert "coach_score is None" in str(exc_info.value)

    @pytest.mark.parametrize(
        "mode",
        [
            GateMode.FLAG_FOR_REVIEW,
            GateMode.HARD_STOP,
            GateMode.MANDATORY_HUMAN_APPROVAL,
        ],
    )
    def test_degraded_mode_accepts_non_auto_approve_modes(self, mode: GateMode) -> None:
        decision = GateDecision(
            **_minimal_decision_kwargs(
                mode=mode,
                coach_score=None,
                threshold_applied=None,  # keep AC-005 happy when MANDATORY
            ),
        )
        assert decision.coach_score is None

    # ----- AC-007 — criterion_breakdown values must be in [0.0, 1.0] ---------

    def test_criterion_breakdown_accepts_unit_interval_values(self) -> None:
        decision = GateDecision(
            **_minimal_decision_kwargs(
                criterion_breakdown={
                    "completeness": 0.0,
                    "correctness": 1.0,
                    "efficiency": 0.5,
                },
            ),
        )
        assert decision.criterion_breakdown["correctness"] == 1.0

    def test_criterion_breakdown_rejects_value_above_one(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GateDecision(
                **_minimal_decision_kwargs(
                    criterion_breakdown={"completeness": 1.5},
                ),
            )
        assert "criterion_breakdown" in str(exc_info.value)

    def test_criterion_breakdown_rejects_negative_value(self) -> None:
        with pytest.raises(ValidationError):
            GateDecision(
                **_minimal_decision_kwargs(
                    criterion_breakdown={"completeness": -0.1},
                ),
            )


# ---------------------------------------------------------------------------
# AC-008 — CalibrationAdjustment
# ---------------------------------------------------------------------------


class TestCalibrationAdjustmentModel:
    """AC-008: CalibrationAdjustment with all fields per DM-gating §1."""

    def test_calibration_adjustment_with_all_fields_constructs(self) -> None:
        adj = CalibrationAdjustment(
            adjustment_id="adj-001",
            target_capability="review_specification",
            project_scope="forge",
            observed_pattern="6 of 10 flag-for-reviews overridden at 0.78-0.82.",
            proposed_bias="Lower flag threshold for review_specification by 0.03.",
            approved_by_rich=True,
            approved_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
            expires_at=None,
            supersedes=None,
        )
        assert adj.target_capability == "review_specification"
        assert adj.approved_by_rich is True

    def test_calibration_adjustment_minimal_construction(self) -> None:
        adj = CalibrationAdjustment(
            adjustment_id="adj-002",
            target_capability="review_design",
            observed_pattern="…",
            proposed_bias="…",
        )
        # Defaults from DM-gating §1.
        assert adj.project_scope is None
        assert adj.approved_by_rich is False
        assert adj.approved_at is None
        assert adj.expires_at is None
        assert adj.supersedes is None

    def test_calibration_adjustment_exposes_every_dm_gating_field(self) -> None:
        expected = {
            "adjustment_id",
            "target_capability",
            "project_scope",
            "observed_pattern",
            "proposed_bias",
            "approved_by_rich",
            "approved_at",
            "expires_at",
            "supersedes",
        }
        assert set(CalibrationAdjustment.model_fields).issuperset(expected)


# ---------------------------------------------------------------------------
# AC-009 — ResponseKind enum
# ---------------------------------------------------------------------------


class TestResponseKindEnum:
    """AC-009: ResponseKind with five members per DM-gating §2."""

    def test_response_kind_has_exactly_five_members(self) -> None:
        assert {m.name for m in ResponseKind} == {
            "ACCEPT_ALL",
            "ACCEPT_WITH_EDIT",
            "REJECT",
            "DEFER",
            "CUSTOM",
        }

    def test_response_kind_is_str_enum(self) -> None:
        assert isinstance(ResponseKind.ACCEPT_ALL, str)


# ---------------------------------------------------------------------------
# AC-010 — evaluate_gate stub
# ---------------------------------------------------------------------------


class TestEvaluateGateStub:
    """AC-010: keyword-only signature; raises NotImplementedError."""

    def test_evaluate_gate_signature_is_keyword_only_with_expected_params(self) -> None:
        sig = inspect.signature(evaluate_gate)
        expected_params = [
            "target_kind",
            "target_identifier",
            "stage_label",
            "coach_score",
            "criterion_breakdown",
            "detection_findings",
            "retrieved_priors",
            "calibration_adjustments",
            "constitutional_rules",
        ]
        assert list(sig.parameters) == expected_params
        for param in sig.parameters.values():
            assert param.kind is inspect.Parameter.KEYWORD_ONLY

    def test_evaluate_gate_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            evaluate_gate(
                target_kind="local_tool",
                target_identifier="review_pr",
                stage_label="review",
                coach_score=None,
                criterion_breakdown={},
                detection_findings=[],
                retrieved_priors=[],
                calibration_adjustments=[],
                constitutional_rules=[],
            )

    def test_evaluate_gate_return_annotation_is_gate_decision(self) -> None:
        # ``from __future__ import annotations`` keeps annotations as
        # strings; resolve them through ``get_type_hints`` to compare to
        # the actual class object.
        import typing

        hints = typing.get_type_hints(evaluate_gate)
        assert hints["return"] is GateDecision

    def test_constitutional_rule_can_be_constructed(self) -> None:
        # Ensures the placeholder type used by the stub signature is real.
        rule = ConstitutionalRule(
            rule_id="PR_REVIEW_HUMAN_ONLY",
            description="PR review is ALWAYS human (ADR-ARCH-026).",
        )
        assert rule.rule_id == "PR_REVIEW_HUMAN_ONLY"


# ---------------------------------------------------------------------------
# AC-011 — module purity (no nats_core / nats-py / langgraph / forge.adapters.*)
# ---------------------------------------------------------------------------


_GATING_PKG = Path(__file__).resolve().parents[3] / "src" / "forge" / "gating"
_FORBIDDEN_ROOTS = ("nats_core", "nats", "langgraph", "forge.adapters")


def _iter_imports(source_path: Path) -> list[str]:
    """Return the full import-paths declared in ``source_path``."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                names.append(node.module)
    return names


class TestModulePurity:
    """AC-011: forge.gating imports nothing from forbidden modules."""

    def test_gating_models_imports_are_clean(self) -> None:
        models_path = _GATING_PKG / "models.py"
        for imported in _iter_imports(models_path):
            for forbidden in _FORBIDDEN_ROOTS:
                assert not imported.startswith(forbidden), (
                    f"forge.gating.models imports forbidden module: {imported!r}"
                )

    def test_gating_init_imports_are_clean(self) -> None:
        init_path = _GATING_PKG / "__init__.py"
        for imported in _iter_imports(init_path):
            for forbidden in _FORBIDDEN_ROOTS:
                assert not imported.startswith(forbidden), (
                    f"forge.gating.__init__ imports forbidden module: {imported!r}"
                )
