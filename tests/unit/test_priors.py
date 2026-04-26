"""Unit tests for ``forge.memory.priors`` (TASK-IC-006).

Documentation level for this task is *minimal* (2 files total — one
source, one test), so the three originally-scoped test modules
(``test_priors_retrieval.py``, ``test_priors_prose.py``,
``test_priors_not_in_argv.py``) are consolidated here. Each
acceptance criterion is covered by one test class:

* :class:`TestRetrieveFourParallelQueries`     — AC: four parallel queries
                                                  via ``asyncio.gather`` (one
                                                  per category).
* :class:`TestRecencyHorizon`                  — AC: configurable recency
                                                  horizon (default 30 days)
                                                  is applied uniformly to
                                                  all four queries.
* :class:`TestApprovedAdjustmentsBoundary`     — AC: approved-adjustments
                                                  query filters
                                                  ``approved=True AND
                                                  expires_at > now()``
                                                  (``@boundary
                                                  boundary-expired-adjustments``).
* :class:`TestProseEmptySectionRepresentation` — AC: empty section renders
                                                  as the literal ``(none)``
                                                  marker, NEVER omitted
                                                  (``@edge-case
                                                  empty-priors-representation``).
* :class:`TestProseStableSectionOrder`         — AC: section order is stable.
* :class:`TestProseInjectionViaPlaceholder`    — AC: prose block injected
                                                  via ``{domain_prompt}``
                                                  placeholder.
* :class:`TestPriorsNotInArgv`                 — AC: priors are NEVER passed
                                                  as subprocess arguments
                                                  (``@edge-case @security
                                                  priors-as-argument-refusal``).
* :class:`TestSeamPriorsProseSchema`           — Seam test from TASK-IC-006:
                                                  ``priors_prose_injection_schema``
                                                  contract with TASK-IC-002.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

import forge.memory.priors as priors_module
from forge.memory.models import (
    CalibrationAdjustment,
    CalibrationEvent,
    OverrideEvent,
    SessionOutcome,
)
from forge.memory.priors import (
    CALIBRATION_HISTORY_GROUP,
    DOMAIN_PROMPT_PLACEHOLDER,
    EMPTY_MARKER,
    PIPELINE_HISTORY_GROUP,
    SECTION_ORDER,
    Priors,
    PriorsLeakError,
    assert_not_in_argv,
    inject_into_system_prompt,
    render_priors_prose,
    retrieve_priors,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class _BuildContext:
    """Minimal stand-in for ``forge.pipeline.BuildContext``.

    Avoids importing the heavy real BuildContext (which transitively
    pulls in nats_core).
    """

    feature_id: str = "FEAT-FORGE-006"
    build_id: str = "build-FEAT-FORGE-006-20260426120000"


def _ts(hour: int = 12, day: int = 26) -> datetime:
    return datetime(2026, 4, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_session_outcome() -> SessionOutcome:
    return SessionOutcome(
        entity_id=uuid4(),
        build_id="build-FEAT-FORGE-006-20260420120000",
        outcome="success",
        gate_decision_ids=[],
        closed_at=_ts(day=20),
    )


def _make_override() -> OverrideEvent:
    return OverrideEvent(
        entity_id=uuid4(),
        gate_decision_id=uuid4(),
        original_recommendation="block",
        operator_decision="proceed",
        operator_rationale="test scaffolding override",
        decided_at=_ts(day=22),
    )


def _make_adjustment(*, approved: bool, expires_in_days: int) -> CalibrationAdjustment:
    now = _ts()
    return CalibrationAdjustment(
        entity_id=uuid4(),
        parameter="confidence_threshold",
        old_value="0.7",
        new_value="0.75",
        approved=approved,
        proposed_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=expires_in_days),
    )


def _make_qa_event() -> CalibrationEvent:
    return CalibrationEvent(
        entity_id="src/forge/x.py:42-60:abc",
        source_file="src/forge/x.py",
        question="What is the threshold?",
        answer="0.7",
        captured_at=_ts(day=24),
    )


# ---------------------------------------------------------------------------
# AC: four parallel queries via asyncio.gather
# ---------------------------------------------------------------------------


class TestRetrieveFourParallelQueries:
    """Verify all four category queries are issued concurrently."""

    @pytest.mark.asyncio
    async def test_retrieve_priors_issues_exactly_four_queries(self) -> None:
        # Arrange — recording dispatcher.
        invocations: list[dict[str, Any]] = []

        async def recording_dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            invocations.append(dict(kwargs))
            return []

        # Act
        await retrieve_priors(
            _BuildContext(),
            query_fn=recording_dispatch,
            now=_ts(),
        )

        # Assert — exactly four calls, one per category.
        assert len(invocations) == 4
        groups = [inv["group_id"] for inv in invocations]
        entity_types = [inv["entity_type"] for inv in invocations]
        assert groups.count(PIPELINE_HISTORY_GROUP) == 3
        assert groups.count(CALIBRATION_HISTORY_GROUP) == 1
        assert set(entity_types) == {
            "SessionOutcome",
            "OverrideEvent",
            "CalibrationAdjustment",
            "CalibrationEvent",
        }

    @pytest.mark.asyncio
    async def test_retrieve_priors_runs_queries_concurrently(self) -> None:
        """The four queries should overlap in wall-clock time.

        Each fake query sleeps 50 ms. If gather schedules them concurrently
        the total elapsed time is ~50 ms; if they ran serially it would be
        ~200 ms. We assert the elapsed time is well under the serial floor.
        """
        per_query_sleep = 0.05

        async def slow_dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            await asyncio.sleep(per_query_sleep)
            return []

        loop = asyncio.get_event_loop()
        start = loop.time()
        await retrieve_priors(
            _BuildContext(),
            query_fn=slow_dispatch,
            now=_ts(),
        )
        elapsed = loop.time() - start

        # Concurrent: ~per_query_sleep. Serial floor: 4 * per_query_sleep.
        # Allow generous headroom for scheduler jitter.
        assert elapsed < 3 * per_query_sleep, (
            f"Queries appear serial: elapsed={elapsed:.3f}s "
            f"vs per-query={per_query_sleep:.3f}s "
            f"(serial floor would be {4 * per_query_sleep:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_retrieve_priors_returns_empty_priors_when_all_queries_empty(
        self,
    ) -> None:
        async def empty_dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            return []

        priors = await retrieve_priors(
            _BuildContext(),
            query_fn=empty_dispatch,
            now=_ts(),
        )

        assert priors.recent_similar_builds == []
        assert priors.recent_override_behaviour == []
        assert priors.approved_calibration_adjustments == []
        assert priors.qa_priors == []

    @pytest.mark.asyncio
    async def test_retrieve_priors_assembles_partial_results(self) -> None:
        """A category with rows AND a category with no rows must coexist."""
        outcome_payload = _make_session_outcome().model_dump(mode="json")
        qa_payload = _make_qa_event().model_dump(mode="json")

        async def partial_dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "SessionOutcome":
                return [outcome_payload]
            if kwargs["entity_type"] == "CalibrationEvent":
                return [qa_payload]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=partial_dispatch, now=_ts()
        )

        assert len(priors.recent_similar_builds) == 1
        assert priors.recent_override_behaviour == []
        assert priors.approved_calibration_adjustments == []
        assert len(priors.qa_priors) == 1

    @pytest.mark.asyncio
    async def test_retrieve_priors_tolerates_per_category_failure(self) -> None:
        """A failure in one category must not poison the other three."""

        async def failing_one_dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "OverrideEvent":
                raise RuntimeError("simulated backend error")
            if kwargs["entity_type"] == "SessionOutcome":
                return [_make_session_outcome().model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=failing_one_dispatch, now=_ts()
        )

        # The failed category degrades to an empty list; siblings survive.
        assert len(priors.recent_similar_builds) == 1
        assert priors.recent_override_behaviour == []


# ---------------------------------------------------------------------------
# AC: configurable recency horizon (default 30 days)
# ---------------------------------------------------------------------------


class TestRecencyHorizon:
    """The single recency-horizon value is applied uniformly."""

    @pytest.mark.asyncio
    async def test_default_horizon_is_thirty_days(self) -> None:
        recorded: list[datetime] = []

        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            recorded.append(kwargs["since"])
            return []

        now = _ts()
        await retrieve_priors(_BuildContext(), query_fn=dispatch, now=now)

        # All four queries share the same ``since`` boundary, set 30 days
        # before ``now``.
        assert len(recorded) == 4
        assert all(s == now - timedelta(days=30) for s in recorded)

    @pytest.mark.asyncio
    async def test_custom_horizon_applied_to_every_query(self) -> None:
        recorded: list[datetime] = []

        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            recorded.append(kwargs["since"])
            return []

        now = _ts()
        await retrieve_priors(
            _BuildContext(), query_fn=dispatch, now=now, horizon_days=7
        )
        assert all(s == now - timedelta(days=7) for s in recorded)

    @pytest.mark.asyncio
    async def test_invalid_horizon_raises_value_error(self) -> None:
        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            return []

        with pytest.raises(ValueError):
            await retrieve_priors(
                _BuildContext(), query_fn=dispatch, horizon_days=0
            )
        with pytest.raises(ValueError):
            await retrieve_priors(
                _BuildContext(), query_fn=dispatch, horizon_days=-1
            )


# ---------------------------------------------------------------------------
# AC: approved=True AND expires_at > now()
# ---------------------------------------------------------------------------


class TestApprovedAdjustmentsBoundary:
    """``@boundary boundary-expired-adjustments`` enforcement."""

    @pytest.mark.asyncio
    async def test_unapproved_adjustments_are_filtered_out(self) -> None:
        unapproved = _make_adjustment(approved=False, expires_in_days=10)
        approved = _make_adjustment(approved=True, expires_in_days=10)

        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [
                    unapproved.model_dump(mode="json"),
                    approved.model_dump(mode="json"),
                ]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=dispatch, now=_ts()
        )
        ids = {a.entity_id for a in priors.approved_calibration_adjustments}
        assert approved.entity_id in ids
        assert unapproved.entity_id not in ids

    @pytest.mark.asyncio
    async def test_expired_adjustments_are_filtered_out(self) -> None:
        expired = _make_adjustment(approved=True, expires_in_days=-1)
        live = _make_adjustment(approved=True, expires_in_days=10)

        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [
                    expired.model_dump(mode="json"),
                    live.model_dump(mode="json"),
                ]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=dispatch, now=_ts()
        )
        ids = {a.entity_id for a in priors.approved_calibration_adjustments}
        assert live.entity_id in ids
        assert expired.entity_id not in ids

    @pytest.mark.asyncio
    async def test_adjustment_at_exact_now_is_treated_as_expired(self) -> None:
        """``expires_at > now()`` is strict — equality counts as expired."""
        now = _ts()
        on_boundary = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="threshold",
            old_value="0.7",
            new_value="0.8",
            approved=True,
            proposed_at=now - timedelta(days=1),
            expires_at=now,  # exactly equal
        )

        async def dispatch(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [on_boundary.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=dispatch, now=now
        )
        assert priors.approved_calibration_adjustments == []


# ---------------------------------------------------------------------------
# AC: empty section renders as "(none)" — never omitted
# ---------------------------------------------------------------------------


class TestProseEmptySectionRepresentation:
    """``@edge-case empty-priors-representation`` enforcement."""

    def test_completely_empty_priors_renders_each_section(self) -> None:
        prose = render_priors_prose(Priors())
        for section in SECTION_ORDER:
            assert section in prose, f"Section {section} missing from prose"
        # Exactly one ``(none)`` per empty section.
        assert prose.count(EMPTY_MARKER) == 4

    def test_partial_priors_only_empty_sections_render_marker(self) -> None:
        priors = Priors(recent_similar_builds=[_make_session_outcome()])
        prose = render_priors_prose(priors)

        # Three empty sections → three markers.
        assert prose.count(EMPTY_MARKER) == 3
        # Populated section's items must appear.
        assert priors.recent_similar_builds[0].build_id in prose
        # Every section name appears regardless of population.
        for section in SECTION_ORDER:
            assert section in prose


# ---------------------------------------------------------------------------
# AC: section order is stable
# ---------------------------------------------------------------------------


class TestProseStableSectionOrder:
    def test_sections_appear_in_declared_order(self) -> None:
        prose = render_priors_prose(Priors())
        positions = [prose.index(name) for name in SECTION_ORDER]
        assert positions == sorted(positions), (
            "Sections rendered out of declared order: " f"{positions}"
        )

    def test_section_order_is_independent_of_population(self) -> None:
        full = Priors(
            recent_similar_builds=[_make_session_outcome()],
            recent_override_behaviour=[_make_override()],
            approved_calibration_adjustments=[
                _make_adjustment(approved=True, expires_in_days=5)
            ],
            qa_priors=[_make_qa_event()],
        )
        prose = render_priors_prose(full)
        positions = [prose.index(name) for name in SECTION_ORDER]
        assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# AC: prose injected via {domain_prompt} placeholder
# ---------------------------------------------------------------------------


class TestProseInjectionViaPlaceholder:
    def test_inject_substitutes_into_domain_prompt_placeholder(self) -> None:
        template = (
            "You are the reasoning model.\n\n"
            "Domain context:\n{domain_prompt}\n\nProceed."
        )
        priors = Priors(recent_similar_builds=[_make_session_outcome()])

        result = inject_into_system_prompt(template, priors)

        # Original markers are intact.
        assert "You are the reasoning model." in result
        assert "Proceed." in result
        # Placeholder is replaced (no curly-brace residue).
        assert "{domain_prompt}" not in result
        # Prose body is present, including the section names.
        for section in SECTION_ORDER:
            assert section in result

    def test_placeholder_constant_matches_pattern(self) -> None:
        """The constant exposed for callers must equal the literal name."""
        assert DOMAIN_PROMPT_PLACEHOLDER == "domain_prompt"

    def test_inject_supports_extra_format_kwargs(self) -> None:
        template = "Date: {date}\n{domain_prompt}\nEnd."
        result = inject_into_system_prompt(
            template, Priors(), date="2026-04-26"
        )
        assert "Date: 2026-04-26" in result
        assert EMPTY_MARKER in result


# ---------------------------------------------------------------------------
# AC: priors NEVER passed as subprocess arguments
# ---------------------------------------------------------------------------


class TestPriorsNotInArgv:
    """``@edge-case @security priors-as-argument-refusal`` enforcement."""

    def test_assert_not_in_argv_passes_when_argv_is_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["pytest", "-q"])
        # Should not raise.
        assert_not_in_argv("This is a long sensitive priors line.\n")

    def test_assert_not_in_argv_raises_when_argv_carries_priors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaked = "operator chose to override gate at 12:00 because reasons"
        monkeypatch.setattr(sys, "argv", ["pytest", f"--note={leaked}"])

        with pytest.raises(PriorsLeakError) as excinfo:
            assert_not_in_argv(leaked + "\n")

        assert "sys.argv" in str(excinfo.value)

    def test_render_priors_prose_invokes_argv_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``render_priors_prose`` defends-in-depth via the argv check."""
        called: list[str] = []

        def spy(text: str) -> None:
            called.append(text)

        monkeypatch.setattr(priors_module, "assert_not_in_argv", spy)
        prose = render_priors_prose(Priors())
        assert called == [prose]

    @pytest.mark.asyncio
    async def test_priors_text_does_not_appear_in_subprocess_argv(
        self,
    ) -> None:
        """The CLI fallback MUST NOT thread priors content through argv.

        We patch ``asyncio.create_subprocess_exec`` and assert that
        none of the captured argv elements contain the rendered prose
        of the priors that were just retrieved.
        """
        outcome = _make_session_outcome()

        async def dispatch_emitting_outcome(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["entity_type"] == "SessionOutcome":
                return [outcome.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _BuildContext(), query_fn=dispatch_emitting_outcome, now=_ts()
        )
        prose = render_priors_prose(priors)
        sensitive_chunks = [
            line for line in prose.splitlines() if line.startswith("- ")
        ]
        assert sensitive_chunks, "expected at least one populated prose line"

        captured_args: list[tuple[str, ...]] = []

        async def fake_create_subprocess_exec(*args: str, **kwargs: Any):
            captured_args.append(tuple(args))

            class _Proc:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return b"[]", b""

            return _Proc()

        with patch.object(
            asyncio, "create_subprocess_exec", fake_create_subprocess_exec
        ):
            # Force the CLI tier to run by faking availability.
            with patch.object(
                priors_module, "_mcp_backend_available", return_value=False
            ):
                with patch.object(
                    priors_module, "_cli_backend_available", return_value=True
                ):
                    await retrieve_priors(
                        _BuildContext(), now=_ts(), horizon_days=30
                    )

        # The CLI must have been invoked.
        assert captured_args
        # Every captured argv must NOT contain any prose chunk.
        for argv in captured_args:
            joined = " ".join(argv)
            for chunk in sensitive_chunks:
                assert chunk not in joined, (
                    f"Priors leak: argv {argv!r} carries prose chunk "
                    f"{chunk!r}"
                )

    def test_assert_ignores_section_headers_and_markers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section headers and ``(none)`` are non-sensitive."""
        monkeypatch.setattr(
            sys, "argv", ["pytest", "## recent_similar_builds", EMPTY_MARKER]
        )
        # Empty priors render header + marker only — must NOT raise even
        # though those tokens literally appear in argv.
        prose = render_priors_prose(Priors())
        assert "## recent_similar_builds" in prose
        # Re-running the explicit check on the same prose is also clean.
        assert_not_in_argv(prose)


# ---------------------------------------------------------------------------
# Seam test — priors_prose_injection_schema (TASK-IC-002 contract)
# ---------------------------------------------------------------------------


class TestSeamPriorsProseSchema:
    """Seam test from TASK-IC-006 task spec.

    Verifies the contract with TASK-IC-002 (Graphiti write side):
    four named sections, each empty section renders exactly one
    ``(none)`` marker.
    """

    @pytest.mark.seam
    def test_priors_prose_section_schema(self) -> None:
        empty = Priors(
            recent_similar_builds=[],
            recent_override_behaviour=[],
            approved_calibration_adjustments=[],
            qa_priors=[],
        )
        prose = render_priors_prose(empty)
        for section in [
            "recent_similar_builds",
            "recent_override_behaviour",
            "approved_calibration_adjustments",
            "qa_priors",
        ]:
            assert section in prose, f"Section {section} missing from prose"
        assert prose.count("(none)") == 4, (
            "Each empty section must render exactly one '(none)' marker"
        )
