"""Tests for the FEAT-UBS-002 budget-guard evaluation core.

Covers the pure evaluator, the coach-score STUB behaviour (ADR-ARCH-033), the
escalation details dict, and the ApprovalRequestPayload construction.
"""

from __future__ import annotations

import pytest

from forge.config.models import BudgetConfig, BudgetGuards
from forge.pipeline.budget_guard import (
    BUDGET_BREACH_REASON,
    BUDGET_BREACH_RISK_LEVEL,
    BuildBudgetMetrics,
    build_budget_breach_approval_details,
    build_budget_breach_approval_payload,
    count_review_cycles,
    evaluate_budget,
)

_UNATTENDED = BudgetConfig().resolve("unattended")
_ATTENDED = BudgetConfig().resolve("attended")


class TestAttendedNeverBreaches:
    """AC: a caps-off profile (ASSUM-010) never breaches, whatever the metrics."""

    def test_huge_metrics_still_ok(self) -> None:
        verdict = evaluate_budget(
            _ATTENDED,
            BuildBudgetMetrics(
                review_cycles=10_000,
                elapsed_wallclock_seconds=1e9,
                tokens_used=10**9,
                last_coach_score=0.0,
            ),
        )
        assert verdict.ok is True
        assert verdict.breached is False
        assert verdict.breached_cap is None


class TestCapBreaches:
    """AC: each cap fires with the right ``breached_cap`` name."""

    def test_review_cycle_cap(self) -> None:
        v = evaluate_budget(_UNATTENDED, BuildBudgetMetrics(review_cycles=2))
        assert v.breached and v.breached_cap == "max_review_cycles"

    def test_review_cycle_below_cap_ok(self) -> None:
        assert evaluate_budget(_UNATTENDED, BuildBudgetMetrics(review_cycles=1)).ok

    def test_wallclock_cap(self) -> None:
        v = evaluate_budget(
            _UNATTENDED, BuildBudgetMetrics(elapsed_wallclock_seconds=5400)
        )
        assert v.breached and v.breached_cap == "max_build_wallclock_seconds"

    def test_token_cap_only_when_measured(self) -> None:
        prof = BudgetGuards(max_build_tokens=100)
        # tokens_used=None → not measured → not a breach
        assert evaluate_budget(prof, BuildBudgetMetrics(tokens_used=None)).ok
        # measured and over → breach
        v = evaluate_budget(prof, BuildBudgetMetrics(tokens_used=100))
        assert v.breached and v.breached_cap == "max_build_tokens"

    def test_first_breach_wins_ordering(self) -> None:
        prof = BudgetGuards(max_review_cycles=1, max_build_wallclock_seconds=1)
        v = evaluate_budget(
            prof, BuildBudgetMetrics(review_cycles=5, elapsed_wallclock_seconds=999)
        )
        # cycle cap is checked first → it is the reported cause
        assert v.breached_cap == "max_review_cycles"


class TestCoachScoreStub:
    """AC (ADR-ARCH-033): the coach floor is inert until a score is present."""

    def test_inert_when_score_is_none(self) -> None:
        prof = BudgetGuards(min_coach_score=0.8)
        assert evaluate_budget(prof, BuildBudgetMetrics(last_coach_score=None)).ok

    def test_active_once_score_present_and_below_floor(self) -> None:
        prof = BudgetGuards(min_coach_score=0.8)
        v = evaluate_budget(prof, BuildBudgetMetrics(last_coach_score=0.5))
        assert v.breached and v.breached_cap == "min_coach_score"

    def test_score_at_or_above_floor_ok(self) -> None:
        prof = BudgetGuards(min_coach_score=0.8)
        assert evaluate_budget(prof, BuildBudgetMetrics(last_coach_score=0.8)).ok


class TestCountReviewCycles:
    """AC: the history helper counts via an injected predicate (decoupled)."""

    def test_counts_matching_entries(self) -> None:
        entries = [{"kind": "review"}, {"kind": "work"}, {"kind": "review"}]
        n = count_review_cycles(entries, is_review=lambda e: e["kind"] == "review")
        assert n == 2


class TestEscalationDetails:
    """AC: the details dict is well-shaped and carries the breach metadata."""

    def test_details_shape(self) -> None:
        v = evaluate_budget(_UNATTENDED, BuildBudgetMetrics(review_cycles=2))
        details = build_budget_breach_approval_details(
            build_id="B1",
            feature_id="FEAT-X",
            profile_name="unattended",
            verdict=v,
            metrics=BuildBudgetMetrics(review_cycles=2),
        )
        assert details["reason"] == BUDGET_BREACH_REASON
        assert details["breached_cap"] == "max_review_cycles"
        assert details["profile"] == "unattended"
        assert details["metrics"]["review_cycles"] == 2
        assert details["resume_options"] == ["approve_continue", "reject_terminate"]


class TestEscalationPayload:
    """AC: the approval payload is high-risk and refuses a passing verdict."""

    def test_payload_is_high_risk_budget_breach(self) -> None:
        v = evaluate_budget(_UNATTENDED, BuildBudgetMetrics(review_cycles=2))
        payload = build_budget_breach_approval_payload(
            request_id="req-1",
            build_id="B1",
            feature_id="FEAT-X",
            profile_name="unattended",
            verdict=v,
            metrics=BuildBudgetMetrics(review_cycles=2),
        )
        assert payload.risk_level == BUDGET_BREACH_RISK_LEVEL == "high"
        assert payload.agent_id == "forge"
        assert payload.details["reason"] == BUDGET_BREACH_REASON

    def test_passing_verdict_refused(self) -> None:
        ok_verdict = evaluate_budget(_ATTENDED, BuildBudgetMetrics())
        with pytest.raises(ValueError):
            build_budget_breach_approval_payload(
                request_id="r",
                build_id="b",
                feature_id="f",
                profile_name="attended",
                verdict=ok_verdict,
                metrics=BuildBudgetMetrics(),
            )
