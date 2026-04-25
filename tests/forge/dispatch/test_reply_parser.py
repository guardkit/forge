"""Tests for ``forge.dispatch.reply_parser``.

Covers TASK-SAD-005 acceptance criteria:

* AC-001: ``parse_reply()`` is exposed on the package and returns a
  :data:`DispatchOutcome` (sum-type from TASK-SAD-001).
* AC (A.coach-output-top-vs-nested): Top-level Coach fields are
  preferred over nested fallback values.
* AC (A.coach-output-nested-fallback): Nested Coach fields are used
  when top-level values are absent.
* AC (C.missing-coach-score): Missing Coach score everywhere →
  ``SyncResult`` with ``coach_score=None`` (gating layer applies
  FLAG_FOR_REVIEW; the parser never fabricates a default).
* AC (C.malformed-reply-envelope): Missing required envelope fields →
  ``DispatchError`` with schema-validation explanation; Coach fields
  are NOT extracted, even if present.
* AC (C.specialist-error): ``error`` key → ``DispatchError`` with the
  specialist's explanation copied verbatim.
* AC (D.async-mode-polling-initial): ``run_identifier`` →
  ``AsyncPending`` carrying that identifier.
* Purity: parser does no I/O and does not log payload values.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from forge.dispatch import (
    AsyncPending,
    DispatchError,
    SyncResult,
)
from forge.dispatch.reply_parser import (
    SpecialistReplyEnvelope,
    _extract_coach_fields,
    parse_reply,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_RES_ID = "r-1"
_ATTEMPT = 1


def _well_formed(**overrides: Any) -> dict[str, Any]:
    """Return a minimally-valid envelope payload, optionally overridden."""

    base: dict[str, Any] = {"agent_id": "specialist-a"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# AC-001 — public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """AC-001: ``parse_reply()`` returns a ``DispatchOutcome`` sum-type."""

    def test_parse_reply_is_callable_and_typed(self) -> None:
        outcome = parse_reply(
            _well_formed(),
            resolution_id=_RES_ID,
            attempt_no=_ATTEMPT,
        )
        # Sum-type: parser only emits these three terminals (Degraded is
        # owned by the gating layer in FEAT-FORGE-004).
        assert isinstance(outcome, (SyncResult, AsyncPending, DispatchError))

    def test_parse_reply_propagates_resolution_id_and_attempt_no(self) -> None:
        outcome = parse_reply(
            _well_formed(),
            resolution_id="r-xyz",
            attempt_no=4,
        )
        assert outcome.resolution_id == "r-xyz"
        assert outcome.attempt_no == 4


# ---------------------------------------------------------------------------
# A.coach-output-top-vs-nested
# ---------------------------------------------------------------------------


class TestCoachOutputTopLevelPreferred:
    """A.coach-output-top-vs-nested: top-level wins over nested."""

    def test_top_level_score_wins_over_nested_score(self) -> None:
        payload = _well_formed(
            coach_score=0.91,
            criterion_breakdown={"clarity": 0.9},
            detection_findings=[{"rule": "top", "severity": "info"}],
            result={
                "coach_score": 0.42,
                "criterion_breakdown": {"clarity": 0.4},
                "detection_findings": [{"rule": "nested", "severity": "warn"}],
            },
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score == 0.91
        assert outcome.criterion_breakdown == {"clarity": 0.9}
        assert outcome.detection_findings == [
            {"rule": "top", "severity": "info"},
        ]

    def test_top_level_score_of_zero_still_wins_over_nested(self) -> None:
        # A literal 0.0 is a legitimate Coach score — it MUST NOT be
        # treated as missing/falsy and silently overridden by the nested
        # block. (Regression guard for the spec's `or` example.)
        payload = _well_formed(
            coach_score=0.0,
            result={"coach_score": 0.99},
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score == 0.0


# ---------------------------------------------------------------------------
# A.coach-output-nested-fallback
# ---------------------------------------------------------------------------


class TestCoachOutputNestedFallback:
    """A.coach-output-nested-fallback: nested used when top-level absent."""

    def test_nested_only_payload_falls_back_to_nested_values(self) -> None:
        payload = _well_formed(
            result={
                "coach_score": 0.73,
                "criterion_breakdown": {"clarity": 0.7},
                "detection_findings": [{"rule": "nested", "severity": "info"}],
            },
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score == 0.73
        assert outcome.criterion_breakdown == {"clarity": 0.7}
        assert outcome.detection_findings == [
            {"rule": "nested", "severity": "info"},
        ]

    def test_partial_top_level_falls_back_per_field(self) -> None:
        # Top-level supplies the score; nested supplies the breakdown.
        # Each field falls back independently.
        payload = _well_formed(
            coach_score=0.5,
            result={"criterion_breakdown": {"clarity": 0.5}},
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score == 0.5
        assert outcome.criterion_breakdown == {"clarity": 0.5}


# ---------------------------------------------------------------------------
# C.missing-coach-score
# ---------------------------------------------------------------------------


class TestMissingCoachScore:
    """C.missing-coach-score: parser never fabricates a default score."""

    def test_no_score_anywhere_yields_sync_result_with_none(self) -> None:
        payload = _well_formed()  # nothing but agent_id
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score is None
        assert outcome.criterion_breakdown == {}
        assert outcome.detection_findings == []

    def test_empty_nested_result_with_no_top_level_yields_none(self) -> None:
        payload = _well_formed(result={})
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score is None


# ---------------------------------------------------------------------------
# C.malformed-reply-envelope
# ---------------------------------------------------------------------------


class TestMalformedEnvelope:
    """C.malformed-reply-envelope: schema is the source of truth."""

    def test_missing_agent_id_yields_dispatch_error(self) -> None:
        # ``agent_id`` is the sole required envelope field; its absence
        # must produce a DispatchError, not a SyncResult.
        payload: dict[str, Any] = {"coach_score": 0.9}
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        assert "schema validation" in outcome.error_explanation.lower()

    def test_malformed_envelope_does_not_extract_coach_fields(self) -> None:
        # Even when Coach fields are present in a malformed payload,
        # the parser MUST NOT extract them — schema validation aborts
        # the pipeline before any extraction step runs.
        payload: dict[str, Any] = {
            # missing agent_id
            "coach_score": 0.99,
            "criterion_breakdown": {"clarity": 0.9},
            "detection_findings": [{"rule": "x"}],
        }
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        # The Coach values must NOT have been embedded into the error
        # explanation (no payload-value leakage).
        assert "0.99" not in outcome.error_explanation
        assert "clarity" not in outcome.error_explanation

    def test_schema_validation_runs_before_specialist_error_branch(
        self,
    ) -> None:
        # Order matters: a malformed payload that ALSO carries an
        # ``error`` key is a schema-validation DispatchError, NOT a
        # specialist-error DispatchError. The schema is the source of
        # truth (TASK-SAD-005 implementation note).
        payload: dict[str, Any] = {
            # missing agent_id
            "error": "specialist exploded",
        }
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        # The verbatim specialist message must NOT have been promoted.
        assert "specialist exploded" not in outcome.error_explanation
        assert "schema validation" in outcome.error_explanation.lower()

    def test_empty_agent_id_is_rejected(self) -> None:
        payload = {"agent_id": ""}
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        assert "schema validation" in outcome.error_explanation.lower()

    def test_coach_score_out_of_range_is_schema_error(self) -> None:
        # A score outside [0, 1] is a schema-validation failure too —
        # the parser must not pass a half-built SyncResult to the
        # gating layer.
        payload = _well_formed(coach_score=1.5)
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        assert "schema validation" in outcome.error_explanation.lower()


# ---------------------------------------------------------------------------
# C.specialist-error
# ---------------------------------------------------------------------------


class TestSpecialistError:
    """C.specialist-error: copy the explanation verbatim."""

    def test_error_key_yields_dispatch_error_verbatim(self) -> None:
        payload = _well_formed(error="upstream tool returned 500: Bad Gateway")
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        assert outcome.error_explanation == (
            "upstream tool returned 500: Bad Gateway"
        )

    def test_empty_string_error_does_not_trigger_specialist_error(
        self,
    ) -> None:
        # A zero-length / whitespace-only error string is not a real
        # specialist error — the parser proceeds to the next branch.
        payload = _well_formed(error="   ")
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)

    def test_error_takes_priority_over_run_identifier(self) -> None:
        # If both are present, ``error`` is reported — a specialist
        # cannot "succeed asynchronously and fail" simultaneously.
        payload = _well_formed(
            error="boom", run_identifier="run-123",
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, DispatchError)
        assert outcome.error_explanation == "boom"


# ---------------------------------------------------------------------------
# D.async-mode-polling-initial
# ---------------------------------------------------------------------------


class TestAsyncPendingBranch:
    """D.async-mode-polling-initial: run_identifier → AsyncPending."""

    def test_run_identifier_yields_async_pending(self) -> None:
        payload = _well_formed(run_identifier="run-42")
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, AsyncPending)
        assert outcome.run_identifier == "run-42"

    def test_run_identifier_supersedes_coach_fields(self) -> None:
        # A reply that declares it is async-pending MUST NOT also be
        # interpreted as a sync coach result, even if Coach fields are
        # incidentally present in the payload.
        payload = _well_formed(
            run_identifier="run-7",
            coach_score=0.95,
        )
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, AsyncPending)
        assert outcome.run_identifier == "run-7"

    def test_blank_run_identifier_falls_through_to_sync_result(self) -> None:
        payload = _well_formed(run_identifier="   ", coach_score=0.5)
        outcome = parse_reply(
            payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
        )
        assert isinstance(outcome, SyncResult)
        assert outcome.coach_score == 0.5


# ---------------------------------------------------------------------------
# Purity / observability
# ---------------------------------------------------------------------------


class TestParserIsPure:
    """The parser is pure: no payload values logged, only outcome kinds."""

    def test_only_outcome_kind_is_logged_no_payload_values(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sentinel_score = 0.6789  # unique, easy to grep
        sentinel_finding = "DETECTED-XYZ-9001"
        payload = _well_formed(
            coach_score=sentinel_score,
            detection_findings=[sentinel_finding],
        )
        with caplog.at_level(logging.DEBUG, logger="forge.dispatch.reply_parser"):
            parse_reply(
                payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
            )

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert str(sentinel_score) not in joined
        assert sentinel_finding not in joined
        # Outcome-kind tracing should still happen.
        assert "sync_result" in joined

    def test_validation_error_log_and_message_are_value_free(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sentinel_value = "SENSITIVE-API-KEY-9001"
        # Out-of-range coach_score embeds the offending value in the
        # raw ValidationError. The summariser must strip it.
        payload = _well_formed(coach_score=2.0, agent_id=sentinel_value)
        # agent_id is well-formed (non-empty), so this passes envelope
        # validation; the SyncResult build is what fails.
        with caplog.at_level(logging.DEBUG, logger="forge.dispatch.reply_parser"):
            outcome = parse_reply(
                payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT,
            )
        assert isinstance(outcome, DispatchError)
        assert sentinel_value not in outcome.error_explanation
        assert "2.0" not in outcome.error_explanation

    def test_parser_does_not_mutate_payload(self) -> None:
        payload = _well_formed(
            coach_score=0.5,
            result={"coach_score": 0.1},
            detection_findings=[],
        )
        snapshot = {k: v for k, v in payload.items()}
        nested_snapshot = dict(payload["result"])
        parse_reply(payload, resolution_id=_RES_ID, attempt_no=_ATTEMPT)
        assert payload == snapshot
        assert payload["result"] == nested_snapshot


# ---------------------------------------------------------------------------
# Helper-level checks
# ---------------------------------------------------------------------------


class TestExtractCoachFieldsHelper:
    """Direct unit tests of the extraction helper."""

    def test_returns_none_breakdown_findings_when_payload_empty(self) -> None:
        score, breakdown, findings = _extract_coach_fields({})
        assert score is None
        assert breakdown == {}
        assert findings == []

    def test_non_dict_nested_result_is_treated_as_empty(self) -> None:
        # Defensive: a malformed-but-parseable nested block (e.g. a
        # string) must not crash the parser — it falls back to empty.
        score, breakdown, findings = _extract_coach_fields(
            {"result": "not-a-dict"},
        )
        assert score is None
        assert breakdown == {}
        assert findings == []

    def test_envelope_model_requires_agent_id(self) -> None:
        from pydantic import ValidationError as PydValidationError

        with pytest.raises(PydValidationError):
            SpecialistReplyEnvelope.model_validate({})
