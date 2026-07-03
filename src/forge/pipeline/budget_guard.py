"""Unattended-mode budget guards for the build loop (FEAT-UBS-002).

This module is a **profile layered on top of Mode C**, never a rewrite of it.
FEAT-FORGE-008 ASSUM-010 keeps :class:`~forge.pipeline.mode_c_planner.ModeCCyclePlanner`
reviewer-driven with *no* numeric iteration cap; the supervisor consults this
module *separately* when a build runs under an unattended profile. When a cap
is breached the build is **paused and escalated** via an
``ApprovalRequestPayload`` (``risk_level="high"``) — never a silent stop, never
a silent continue (scope §4).

Design boundaries:

- The evaluation core (:func:`evaluate_budget`) is a **pure function** over
  :class:`BudgetGuards` (config) and :class:`BuildBudgetMetrics` (runtime
  numbers). It imports nothing from ``nats_core`` / the supervisor, so it is
  trivially unit-testable and importable even where the wire package is absent.
- Only :func:`build_budget_breach_approval_payload` touches ``nats_core`` — and
  it does so lazily, mirroring the rest of the codebase, so importing this
  module never pulls the wire dependency.

Coach-score floor is a **STUB** (ADR-ARCH-033): ``metrics.last_coach_score`` is
always ``None`` today because the runner does not yet populate it (the
coach-score gap — itself a UBS-002 prerequisite). The ``min_coach_score`` branch
is therefore inert; it is written so it activates automatically the moment a
real score flows, with no further change here.

Integration seam (deferred to the follow-up task — see
``tasks/backlog/unattended-build-service/TASK-UBS-002-integration.md``): the
supervisor's ``_next_turn_mode_c`` computes a :class:`BuildBudgetMetrics` from
the build's history + timing, calls :func:`evaluate_budget`, and on a breach
emits the approval payload + pauses instead of dispatching the next cycle. That
wiring, plus carrying the selected profile from ``forge queue --profile`` across
the queue→daemon boundary, needs a real Mode C run to validate and is out of
scope for this skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from forge.config.models import BudgetGuards

__all__ = [
    "BudgetVerdict",
    "BuildBudgetMetrics",
    "build_budget_breach_approval_details",
    "build_budget_breach_approval_payload",
    "count_review_cycles",
    "evaluate_budget",
]


#: ``risk_level`` published on a budget-breach approval request. Scope §4
#: fixes this at ``"high"`` — a budget breach always demands a human decision.
BUDGET_BREACH_RISK_LEVEL = "high"

#: Machine-readable ``reason`` tag on the escalation ``details`` dict, so
#: notification adapters can distinguish a budget breach from other pauses.
BUDGET_BREACH_REASON = "budget_guard_breach"


@dataclass(frozen=True, slots=True)
class BuildBudgetMetrics:
    """Runtime measurements a build is judged against.

    Attributes:
        review_cycles: Mode C ``/task-review`` cycles already dispatched for
            this build. Compared against ``max_review_cycles``.
        elapsed_wallclock_seconds: Wall-clock consumed by the build so far.
            Compared against ``max_build_wallclock_seconds``.
        tokens_used: Tokens consumed so far, or ``None`` when not measured
            (the token cap is then treated as unenforceable, not breached).
        last_coach_score: Most recent Coach score in ``[0, 1]``, or ``None``.
            Always ``None`` today (ADR-ARCH-033) — the ``min_coach_score``
            floor stays inert until the runner populates it.
    """

    review_cycles: int = 0
    elapsed_wallclock_seconds: float = 0.0
    tokens_used: int | None = None
    last_coach_score: float | None = None


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Outcome of a budget evaluation.

    ``ok=True`` → the build may proceed. ``ok=False`` → a cap is breached;
    ``breached_cap`` names the offending :class:`BudgetGuards` field and
    ``detail`` is a human-readable rationale for the escalation.
    """

    ok: bool
    breached_cap: str | None = None
    detail: str = ""

    @property
    def breached(self) -> bool:
        """Convenience inverse of :attr:`ok`."""
        return not self.ok


_OK_VERDICT = BudgetVerdict(ok=True)


def evaluate_budget(
    profile: BudgetGuards,
    metrics: BuildBudgetMetrics,
) -> BudgetVerdict:
    """Return a :class:`BudgetVerdict` for ``metrics`` under ``profile``.

    Pure and side-effect-free. Caps are checked in a stable order and the
    *first* breach wins (so the escalation names one concrete cause). A profile
    with all caps unset (the ``attended`` profile — ASSUM-010) can never breach:
    every branch is guarded on ``is not None`` and returns OK.

    Cap semantics use ``>=``: a breach fires when the measurement *reaches* the
    cap, i.e. the guard is consulted before the step that would exceed it (the
    supervisor asks "may I run another review cycle?" with
    ``review_cycles`` = cycles already done).
    """
    if (
        profile.max_review_cycles is not None
        and metrics.review_cycles >= profile.max_review_cycles
    ):
        return BudgetVerdict(
            ok=False,
            breached_cap="max_review_cycles",
            detail=(
                f"review cycles ({metrics.review_cycles}) reached cap "
                f"({profile.max_review_cycles})"
            ),
        )

    if (
        profile.max_build_wallclock_seconds is not None
        and metrics.elapsed_wallclock_seconds >= profile.max_build_wallclock_seconds
    ):
        return BudgetVerdict(
            ok=False,
            breached_cap="max_build_wallclock_seconds",
            detail=(
                f"wall-clock ({metrics.elapsed_wallclock_seconds:.0f}s) reached "
                f"cap ({profile.max_build_wallclock_seconds}s)"
            ),
        )

    if (
        profile.max_build_tokens is not None
        and metrics.tokens_used is not None
        and metrics.tokens_used >= profile.max_build_tokens
    ):
        return BudgetVerdict(
            ok=False,
            breached_cap="max_build_tokens",
            detail=(
                f"tokens ({metrics.tokens_used}) reached cap "
                f"({profile.max_build_tokens})"
            ),
        )

    # Coach-score floor — STUB (ADR-ARCH-033). Inert while last_coach_score is
    # None (its value today); activates automatically once the runner populates
    # the score. A score strictly below the floor is a breach.
    if (
        profile.min_coach_score is not None
        and metrics.last_coach_score is not None
        and metrics.last_coach_score < profile.min_coach_score
    ):
        return BudgetVerdict(
            ok=False,
            breached_cap="min_coach_score",
            detail=(
                f"coach score ({metrics.last_coach_score:.3f}) below floor "
                f"({profile.min_coach_score:.3f})"
            ),
        )

    return _OK_VERDICT


def count_review_cycles(
    history: Sequence[Any],
    *,
    is_review: Callable[[Any], bool],
) -> int:
    """Count the review entries in ``history`` (the Mode C cyclic step).

    Decoupled from :class:`~forge.pipeline.stage_taxonomy.StageClass` via the
    ``is_review`` predicate so this module has no import dependency on the stage
    taxonomy. The supervisor passes
    ``is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW``.
    """
    return sum(1 for entry in history if is_review(entry))


def build_budget_breach_approval_details(
    *,
    build_id: str,
    feature_id: str,
    profile_name: str,
    verdict: BudgetVerdict,
    metrics: BuildBudgetMetrics,
) -> dict[str, Any]:
    """Build the ``details`` dict for a budget-breach approval request (pure).

    Kept free of ``nats_core`` so it is fully unit-testable. The shape mirrors
    the other approval-details producers (single-owner of the dict per the
    approval-protocol convention).
    """
    return {
        "build_id": build_id,
        "feature_id": feature_id,
        "reason": BUDGET_BREACH_REASON,
        "profile": profile_name,
        "breached_cap": verdict.breached_cap,
        "detail": verdict.detail,
        "metrics": {
            "review_cycles": metrics.review_cycles,
            "elapsed_wallclock_seconds": metrics.elapsed_wallclock_seconds,
            "tokens_used": metrics.tokens_used,
            "last_coach_score": metrics.last_coach_score,
        },
        "resume_options": ["approve_continue", "reject_terminate"],
    }


def build_budget_breach_approval_payload(
    *,
    request_id: str,
    build_id: str,
    feature_id: str,
    profile_name: str,
    verdict: BudgetVerdict,
    metrics: BuildBudgetMetrics,
) -> Any:
    """Construct an ``ApprovalRequestPayload`` for a budget breach.

    Imports ``nats_core`` lazily (house pattern) so importing this module never
    pulls the wire dependency. ``risk_level`` is fixed at
    :data:`BUDGET_BREACH_RISK_LEVEL` per scope §4.

    Raises:
        ValueError: If called with a non-breach ``verdict`` — escalating a
            healthy build would be a logic error at the call site.
    """
    if verdict.ok:
        raise ValueError(
            "build_budget_breach_approval_payload called with a passing "
            "verdict; only breaches are escalated"
        )

    # Lazy imports — the wire package and the AGENT_ID owner both reach
    # ``nats_core`` at import time, which this module must not require eagerly.
    from forge.adapters.nats.approval_publisher import AGENT_ID
    from nats_core.events import ApprovalRequestPayload

    return ApprovalRequestPayload(
        request_id=request_id,
        agent_id=AGENT_ID,
        action_description=(
            f"Budget guard: {verdict.breached_cap} breached for build "
            f"{build_id} (feature={feature_id}, profile={profile_name}); "
            "pausing for approval"
        ),
        risk_level=BUDGET_BREACH_RISK_LEVEL,
        details=build_budget_breach_approval_details(
            build_id=build_id,
            feature_id=feature_id,
            profile_name=profile_name,
            verdict=verdict,
            metrics=metrics,
        ),
    )
