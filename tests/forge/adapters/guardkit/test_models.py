"""Unit tests for :mod:`forge.adapters.guardkit.models` (TASK-GCI-001).

Test classes mirror the acceptance criteria for the GuardKit result models:

- AC-001 — :class:`GuardKitResult` and :class:`GuardKitWarning` are defined
  in ``src/forge/adapters/guardkit/models.py``.
- AC-002 — :attr:`GuardKitResult.status` is a typed
  ``Literal["success", "failed", "timeout"]`` rather than an :class:`Enum`.
- AC-003 — ``artefacts`` and ``warnings`` use
  ``Field(default_factory=list)`` so independent instances do not share
  mutable defaults.
- AC-004 — ``coach_score``, ``criterion_breakdown``, ``detection_findings``
  and ``stderr`` are explicitly :data:`Optional` (defaulting to ``None``).
- AC-005 — :meth:`GuardKitResult.model_dump_json` round-trips through
  :meth:`GuardKitResult.model_validate_json` without data loss.
- AC-006 — The ``forge.adapters.guardkit`` package re-exports
  ``GuardKitResult`` and ``GuardKitWarning`` via its ``__init__`` shim.
"""

from __future__ import annotations

import inspect
import typing
from typing import Literal, get_type_hints

import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from forge.adapters.guardkit import models as guardkit_models
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning


def _make_minimal_result(**overrides: object) -> GuardKitResult:
    """Build a minimal valid :class:`GuardKitResult` for tests.

    Only the required fields are populated — overrides may extend or
    replace any field for a specific scenario.
    """
    base: dict[str, object] = {
        "status": "success",
        "subcommand": "context-resolve",
        "duration_secs": 1.25,
        "exit_code": 0,
    }
    base.update(overrides)
    return GuardKitResult(**base)  # type: ignore[arg-type]


class TestModelsDefinedInModelsModule:
    """AC-001 — Both result models are defined in the expected module."""

    def test_guardkit_result_is_defined_in_models_module(self) -> None:
        assert GuardKitResult.__module__ == "forge.adapters.guardkit.models"

    def test_guardkit_warning_is_defined_in_models_module(self) -> None:
        assert GuardKitWarning.__module__ == "forge.adapters.guardkit.models"

    def test_models_module_path_is_under_guardkit_adapter(self) -> None:
        module_file = inspect.getsourcefile(guardkit_models) or ""
        assert module_file.endswith("forge/adapters/guardkit/models.py")


class TestStatusFieldIsLiteralNotEnum:
    """AC-002 — ``status`` is a Literal, not an Enum."""

    def test_status_annotation_is_literal_with_three_members(self) -> None:
        hints = get_type_hints(GuardKitResult)
        status_hint = hints["status"]
        assert typing.get_origin(status_hint) is Literal
        assert set(typing.get_args(status_hint)) == {"success", "failed", "timeout"}

    def test_status_accepts_each_literal_value(self) -> None:
        for value in ("success", "failed", "timeout"):
            result = _make_minimal_result(status=value)
            assert result.status == value

    def test_status_rejects_unknown_string(self) -> None:
        with pytest.raises(ValidationError):
            _make_minimal_result(status="errored")


class TestListFieldsUseDefaultFactory:
    """AC-003 — list-typed fields use ``default_factory=list``."""

    @pytest.mark.parametrize("field_name", ["artefacts", "warnings"])
    def test_field_has_default_factory_list(self, field_name: str) -> None:
        field: FieldInfo = GuardKitResult.model_fields[field_name]
        assert field.default_factory is list

    def test_independent_instances_do_not_share_artefacts_list(self) -> None:
        first = _make_minimal_result()
        second = _make_minimal_result()
        first.artefacts.append("/tmp/foo.txt")
        assert second.artefacts == []

    def test_independent_instances_do_not_share_warnings_list(self) -> None:
        first = _make_minimal_result()
        second = _make_minimal_result()
        first.warnings.append(
            GuardKitWarning(code="x", message="m"),
        )
        assert second.warnings == []

    def test_warning_details_uses_default_factory_dict(self) -> None:
        field: FieldInfo = GuardKitWarning.model_fields["details"]
        assert field.default_factory is dict
        first = GuardKitWarning(code="x", message="m")
        second = GuardKitWarning(code="y", message="n")
        first.details["k"] = "v"
        assert second.details == {}


class TestOptionalFieldsAreNullable:
    """AC-004 — Optional fields are explicitly Optional (default ``None``)."""

    @pytest.mark.parametrize(
        "field_name",
        ["coach_score", "criterion_breakdown", "detection_findings", "stderr"],
    )
    def test_optional_field_defaults_to_none(self, field_name: str) -> None:
        result = _make_minimal_result()
        assert getattr(result, field_name) is None

    @pytest.mark.parametrize(
        "field_name",
        ["coach_score", "criterion_breakdown", "detection_findings", "stderr"],
    )
    def test_optional_field_accepts_none_explicitly(self, field_name: str) -> None:
        result = _make_minimal_result(**{field_name: None})
        assert getattr(result, field_name) is None

    def test_optional_field_annotations_include_none(self) -> None:
        hints = get_type_hints(GuardKitResult)
        optional_fields = (
            "coach_score",
            "criterion_breakdown",
            "detection_findings",
            "stderr",
        )
        for name in optional_fields:
            args = typing.get_args(hints[name])
            assert type(None) in args, (
                f"{name} must be Optional, got {hints[name]!r}"
            )


class TestRoundTripsThroughJSON:
    """AC-005 — ``model_dump_json`` round-trips through ``model_validate_json``."""

    def test_minimal_result_round_trips(self) -> None:
        original = _make_minimal_result()
        rebuilt = GuardKitResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_fully_populated_result_round_trips(self) -> None:
        original = GuardKitResult(
            status="failed",
            subcommand="quality-coach",
            artefacts=["/tmp/a.txt", "/tmp/b.json"],
            coach_score=0.42,
            criterion_breakdown={"clarity": 0.5, "completeness": 0.3},
            detection_findings=[{"rule": "R1", "severity": "high"}],
            duration_secs=12.75,
            stdout_tail="last 4 KB of output...",
            stderr="non-fatal complaint on stderr",
            exit_code=2,
            warnings=[
                GuardKitWarning(
                    code="context_manifest_missing",
                    message="manifest not found",
                    details={"path": "/no/such/file"},
                ),
            ],
        )
        rebuilt = GuardKitResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original
        assert rebuilt.warnings[0].details == {"path": "/no/such/file"}

    def test_round_trip_preserves_status_literal(self) -> None:
        for value in ("success", "failed", "timeout"):
            original = _make_minimal_result(status=value)
            rebuilt = GuardKitResult.model_validate_json(original.model_dump_json())
            assert rebuilt.status == value


class TestPackageReExportShim:
    """AC-006 — The package ``__init__`` re-exports the result models."""

    def test_package_reexports_result_model(self) -> None:
        from forge.adapters import guardkit

        assert guardkit.GuardKitResult is GuardKitResult

    def test_package_reexports_warning_model(self) -> None:
        from forge.adapters import guardkit

        assert guardkit.GuardKitWarning is GuardKitWarning

    def test_package_dunder_all_includes_both_models(self) -> None:
        from forge.adapters import guardkit

        # AC-006 requires the shim re-export ``GuardKitResult`` and
        # ``GuardKitWarning``; other models contributed by sibling tasks
        # (e.g. ``GuardKitProgressEvent`` from TASK-GCI-005) may also
        # appear, so we assert inclusion rather than exact equality.
        assert {"GuardKitResult", "GuardKitWarning"} <= set(guardkit.__all__)
