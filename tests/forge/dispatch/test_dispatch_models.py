"""Tests for ``forge.dispatch.models``.

Covers TASK-SAD-001 acceptance criteria:

* AC-001: Package re-exports.
* AC-002: All five Pydantic models exist with the documented fields.
* AC-003: ``DispatchOutcome`` is a discriminated union over the four
  variants (round-trips through ``model_dump`` / ``model_validate``).
* AC-004: ``CapabilityResolution.retry_of`` defaults to ``None`` and does
  not break existing FEAT-FORGE-002 construction.
* AC-005: Each variant survives ``model_dump()`` → ``model_validate()``
  via the discriminated union and round-trips back to its concrete class.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from forge import dispatch
from forge.discovery.models import CapabilityResolution
from forge.dispatch import (
    AsyncPending,
    CorrelationKey,
    Degraded,
    DispatchAttempt,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)


# A reusable TypeAdapter so each round-trip test goes through the
# discriminated union (the production consumption path), not the
# concrete class directly.
_OUTCOME_ADAPTER: TypeAdapter[DispatchOutcome] = TypeAdapter(DispatchOutcome)


class TestPackageReExports:
    """AC-001: package ``forge.dispatch`` re-exports the model surface."""

    def test_package_init_reexports_all_public_models(self) -> None:
        expected = {
            "AsyncPending",
            "CorrelationKey",
            "Degraded",
            "DispatchAttempt",
            "DispatchError",
            "DispatchOutcome",
            "SyncResult",
        }
        assert expected.issubset(set(dispatch.__all__))
        for name in expected:
            assert hasattr(dispatch, name), f"forge.dispatch missing {name}"

    def test_correlation_key_is_str_alias(self) -> None:
        # CorrelationKey is intentionally an opaque str alias; format
        # validation lives at the boundary (TASK-SAD-003), not here.
        assert CorrelationKey is str


class TestDispatchAttempt:
    """AC-002: DispatchAttempt schema."""

    def test_minimal_construction_defaults_attempt_no_to_one(self) -> None:
        attempt = DispatchAttempt(
            resolution_id="r1",
            correlation_key="0" * 32,
            matched_agent_id="agent-a",
        )
        assert attempt.attempt_no == 1
        assert attempt.retry_of is None

    def test_retry_of_carries_previous_resolution_id(self) -> None:
        attempt = DispatchAttempt(
            resolution_id="r2",
            correlation_key="a" * 32,
            matched_agent_id="agent-a",
            attempt_no=2,
            retry_of="r1",
        )
        assert attempt.retry_of == "r1"
        assert attempt.attempt_no == 2

    def test_attempt_no_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            DispatchAttempt(
                resolution_id="r1",
                correlation_key="0" * 32,
                matched_agent_id="agent-a",
                attempt_no=0,
            )

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DispatchAttempt(
                resolution_id="r1",
                correlation_key="0" * 32,
                matched_agent_id="agent-a",
                bogus="nope",
            )


class TestDispatchOutcomeRoundTrip:
    """AC-003 + AC-005: discriminated union round-trips for every variant."""

    def test_sync_result_round_trip_via_discriminated_union(self) -> None:
        outcome = SyncResult(
            resolution_id="r1",
            attempt_no=1,
            coach_score=0.875,
            criterion_breakdown={"clarity": 0.9},
            detection_findings=[{"rule": "x", "severity": "info"}],
        )
        dumped = outcome.model_dump()
        assert dumped["kind"] == "sync_result"

        restored = _OUTCOME_ADAPTER.validate_python(dumped)
        assert isinstance(restored, SyncResult)
        assert restored == outcome

    def test_async_pending_round_trip_via_discriminated_union(self) -> None:
        outcome = AsyncPending(
            resolution_id="r1",
            attempt_no=1,
            run_identifier="run-42",
        )
        restored = _OUTCOME_ADAPTER.validate_python(outcome.model_dump())
        assert isinstance(restored, AsyncPending)
        assert restored == outcome

    def test_degraded_round_trip_via_discriminated_union(self) -> None:
        outcome = Degraded(
            resolution_id="r1",
            attempt_no=2,
            reason="downstream service slow",
        )
        restored = _OUTCOME_ADAPTER.validate_python(outcome.model_dump())
        assert isinstance(restored, Degraded)
        assert restored == outcome

    def test_dispatch_error_round_trip_via_discriminated_union(self) -> None:
        outcome = DispatchError(
            resolution_id="r1",
            attempt_no=3,
            error_explanation="agent rejected: schema mismatch",
        )
        restored = _OUTCOME_ADAPTER.validate_python(outcome.model_dump())
        assert isinstance(restored, DispatchError)
        assert restored == outcome

    def test_discriminator_distinguishes_variants_with_same_payload_shape(
        self,
    ) -> None:
        # Degraded and DispatchError share resolution_id + attempt_no
        # plus a string field; the discriminator (``kind``) is the only
        # distinguishing axis at the union level.
        degraded = Degraded(
            resolution_id="r1", attempt_no=1, reason="slow",
        ).model_dump()
        error = DispatchError(
            resolution_id="r1", attempt_no=1, error_explanation="boom",
        ).model_dump()

        assert isinstance(_OUTCOME_ADAPTER.validate_python(degraded), Degraded)
        assert isinstance(_OUTCOME_ADAPTER.validate_python(error), DispatchError)

    def test_unknown_kind_is_rejected_by_discriminator(self) -> None:
        with pytest.raises(ValidationError):
            _OUTCOME_ADAPTER.validate_python(
                {"kind": "not_a_real_kind", "resolution_id": "r1", "attempt_no": 1},
            )

    def test_each_variant_kind_literal_is_fixed(self) -> None:
        # AC-002 / AC-003: the discriminator literal cannot be overridden.
        with pytest.raises(ValidationError):
            SyncResult(
                kind="async_pending",  # type: ignore[arg-type]
                resolution_id="r1",
                attempt_no=1,
            )


class TestSyncResultDefaults:
    """AC-002: mutable defaults are factory-built, not shared."""

    def test_default_factories_do_not_share_state(self) -> None:
        a = SyncResult(resolution_id="r1", attempt_no=1)
        b = SyncResult(resolution_id="r2", attempt_no=1)
        a.criterion_breakdown["only_in_a"] = 1.0
        a.detection_findings.append("only_in_a")
        assert "only_in_a" not in b.criterion_breakdown
        assert "only_in_a" not in b.detection_findings

    def test_coach_score_bounded_to_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            SyncResult(resolution_id="r1", attempt_no=1, coach_score=1.5)
        with pytest.raises(ValidationError):
            SyncResult(resolution_id="r1", attempt_no=1, coach_score=-0.1)


class TestCapabilityResolutionRetryOf:
    """AC-004: ``retry_of`` is append-only and defaults to None."""

    def _kwargs(self) -> dict[str, object]:
        return {
            "resolution_id": "r1",
            "build_id": "b1",
            "stage_label": "s1",
            "requested_tool": "t",
            "match_source": "tool_exact",
            "matched_agent_id": "agent-a",
            "resolved_at": datetime.now(UTC),
        }

    def test_existing_call_sites_compile_without_retry_of(self) -> None:
        # FEAT-FORGE-002 callers do not pass ``retry_of``. Construction
        # must still succeed and default to None.
        resolution = CapabilityResolution(**self._kwargs())
        assert resolution.retry_of is None

    def test_retry_of_carries_previous_resolution_id(self) -> None:
        resolution = CapabilityResolution(**self._kwargs(), retry_of="r-prev")
        assert resolution.retry_of == "r-prev"

    def test_retry_of_round_trips_through_model_dump(self) -> None:
        resolution = CapabilityResolution(**self._kwargs(), retry_of="r-prev")
        restored = CapabilityResolution.model_validate(resolution.model_dump())
        assert restored.retry_of == "r-prev"
        assert restored == resolution
