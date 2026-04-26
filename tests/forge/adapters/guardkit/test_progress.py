"""Declarative-schema tests for ``forge.adapters.guardkit.progress``.

Covers TASK-GCI-002 acceptance criteria:

- AC-002: GuardKitProgressEvent lives in
  ``src/forge/adapters/guardkit/progress.py``.
- AC-004: All optional fields explicitly default to ``None``.
- AC-005: ``forge.adapters.guardkit`` re-exports the model.
- AC-007: ``model_dump_json()`` round-trips through
  ``model_validate_json()``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.adapters.guardkit import GuardKitProgressEvent as ReexportedEvent
from forge.adapters.guardkit.progress import GuardKitProgressEvent


class TestGuardKitProgressEventDefinition:
    """AC-002 / AC-004 / AC-005 — schema shape."""

    def test_module_path(self) -> None:
        # AC-002: model is sourced from the canonical path.
        assert GuardKitProgressEvent.__module__ == "forge.adapters.guardkit.progress"

    def test_optional_fields_default_to_none(self) -> None:
        # AC-004: every optional field is explicitly None by default.
        for name in ("coach_score", "artefact"):
            assert (
                GuardKitProgressEvent.model_fields[name].default is None
            ), name

    def test_required_fields_have_no_default(self) -> None:
        for name in ("build_id", "subcommand", "stage_label", "seq", "timestamp"):
            assert GuardKitProgressEvent.model_fields[name].is_required(), name

    def test_timestamp_is_string_not_datetime(self) -> None:
        # The implementation note says ``timestamp`` is an ISO 8601 ``str``,
        # not a ``datetime`` field — match the nats-core convention.
        annotation = GuardKitProgressEvent.model_fields["timestamp"].annotation
        assert annotation is str

    def test_seq_is_int(self) -> None:
        annotation = GuardKitProgressEvent.model_fields["seq"].annotation
        assert annotation is int

    def test_reexported_from_package_init(self) -> None:
        # AC-005: re-export shim works.
        assert ReexportedEvent is GuardKitProgressEvent


class TestGuardKitProgressEventBehaviour:
    """Construction + validation + JSON round-trip."""

    def test_minimal_construction(self) -> None:
        event = GuardKitProgressEvent(
            build_id="b-1",
            subcommand="/feature-spec",
            stage_label="discovery",
            seq=1,
            timestamp="2026-04-26T08:30:00+00:00",
        )
        assert event.coach_score is None
        assert event.artefact is None

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GuardKitProgressEvent(  # type: ignore[call-arg]
                build_id="b-1",
                subcommand="/feature-spec",
                stage_label="discovery",
                seq=1,
                # missing timestamp
            )

    def test_json_round_trip_minimal(self) -> None:
        # AC-007: minimal instance round-trips.
        original = GuardKitProgressEvent(
            build_id="b-9",
            subcommand="autobuild",
            stage_label="player_turn_1",
            seq=7,
            timestamp="2026-04-26T08:38:00+00:00",
        )
        rebuilt = GuardKitProgressEvent.model_validate_json(
            original.model_dump_json()
        )
        assert rebuilt == original

    def test_json_round_trip_full(self) -> None:
        # AC-007: instance with optionals populated round-trips.
        original = GuardKitProgressEvent(
            build_id="b-10",
            subcommand="/task-review",
            stage_label="coach_eval",
            seq=42,
            coach_score=0.91,
            artefact="reports/task-review/TASK-X.md",
            timestamp="2026-04-26T08:39:00+00:00",
        )
        rebuilt = GuardKitProgressEvent.model_validate_json(
            original.model_dump_json()
        )
        assert rebuilt == original
        assert rebuilt.coach_score == pytest.approx(0.91)
        assert rebuilt.artefact == "reports/task-review/TASK-X.md"
