"""Mid-run budget-breach DETECTION for the lifecycle-bridge observer.

FEAT-UBS-002 stage 2 (DETECT). This module is the honest middle of the
Option-B budget lane: the ``forge serve`` daemon watches a running build's
``stage-complete`` envelopes and, when a budget cap is breached, RECORDS the
breach and ESCALATES a risk=high approval — but it **never** pauses, cancels,
or rewrites ``builds.status``. A mid-run hard stop is not achievable on this
path (the ``runs.cancel`` seam is dark: no caller, no SDK client on the
production bridge, and the guardkit proc handle lives in the sidecar
coroutine). So the daemon reports only what it can honestly effect — a durable
record (``schema_v7.builds.budget_breach``) and an escalation the operator
acts on. The run continues to its own bounded end (stage 1's runner wall-clock
self-bound); the F6 terminal contracts stand byte-identical.

Design boundaries
-----------------

* :func:`~forge.pipeline.budget_guard.evaluate_budget` is reused verbatim as
  the pure verdict core (first-breach-wins). This module owns only the
  *observation* wiring: per-observer state, the review-cycle count, resolving
  the guards once, and the record + publish + log side effects on the first
  breach.
* The collaborators are injected callables so this module stays dependency-
  light and trivially testable. Production composes them from the serve-side
  reuse helpers (:func:`forge.cli.serve.resolve_budget_for_build`,
  :func:`~forge.cli.serve.make_budget_started_at_reader`,
  :func:`~forge.cli.serve.budget_wall_clock`) via
  :func:`build_budget_breach_observer`.
* Every side effect is guarded by BOTH a durable first-write-wins
  (``record_budget_breach`` writes only ``WHERE budget_breach IS NULL``) and an
  in-observer ``breached`` flag, so a re-fire on a later stage-complete neither
  overwrites the record nor publishes a second approval.

Honesty of the review-cycle count
----------------------------------

:attr:`BudgetObserverSession.review_cycles` counts ``stage-complete`` envelopes
in memory for the life of the observer. It **resets on bridge restart** (a
fresh process starts a fresh observer): documented plainly, not hidden. The
wall-clock and coach-score caps do not depend on this count, so a restart never
weakens them — only the ``max_review_cycles`` cap loses its running tally, the
honest cost of a stateless observer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from forge.config.models import BudgetGuards
from forge.pipeline.budget_guard import (
    BuildBudgetMetrics,
    build_budget_breach_approval_payload,
    evaluate_budget,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BudgetObserverSession",
    "BudgetBreachObserver",
    "build_budget_breach_observer",
]


#: ``async (payload, approval_subject) -> None`` — the daemon's re-emit shape
#: (:data:`forge.adapters.nats.pipeline_consumer.PublishApprovalRequestFn`).
PublishApprovalRequestFn = Callable[[Any, str], Awaitable[None]]


@dataclass
class BudgetObserverSession:
    """Per-observer (per-build) mutable budget-detection state.

    One instance per observer task, created at observer start and discarded
    when the observer exits. Its counters live only in memory — see the
    module docstring on the ``review_cycles`` reset-on-restart honesty.

    Attributes:
        guards: The resolved :class:`BudgetGuards` for the build, or ``None``
            until first resolved.
        profile_name: The profile the guards came from (for the escalation).
        resolved: Whether :attr:`guards` has been resolved yet (resolve-once).
        disabled: ``True`` once resolution finds a caps-off (attended) profile
            — the session then short-circuits every subsequent event.
        review_cycles: Count of ``stage-complete`` envelopes observed so far.
        breached: ``True`` once the first breach has been recorded + escalated
            (in-observer half of first-breach-wins).
    """

    guards: BudgetGuards | None = None
    profile_name: str = ""
    resolved: bool = False
    disabled: bool = False
    review_cycles: int = 0
    breached: bool = False


class BudgetBreachObserver:
    """Evaluates a running build's budget after each ``stage-complete``.

    Constructed once per ``forge serve`` daemon and shared across observers;
    per-build state lives in the :class:`BudgetObserverSession` the observer
    creates via :meth:`new_session`. All collaborators are injected so the
    class carries no persistence or NATS import weight.

    Args:
        resolve_budget: ``(build_id) -> (guards, profile_name)``. Reused
            production impl: ``lambda bid: resolve_budget_for_build(pool,
            config, bid)``.
        elapsed_seconds: ``(build_id) -> float`` wall-clock consumed by the
            build; fail-open ``0.0`` when unmeasurable (never a false breach).
        read_coach_score: ``(build_id) -> float | None`` durable coach-score
            fallback (``builds.last_coach_score``) when the envelope carries
            none.
        record_breach: ``(build_id, detail) -> None`` first-write-wins persist
            (``SqliteLifecyclePersistence.record_budget_breach``).
        publish_approval_request: :data:`PublishApprovalRequestFn` — the
            daemon's approval re-emit.
        approval_subject_for: ``(build_id) -> str`` resolves the approval
            subject for the escalation.
        clock: ``() -> datetime`` for the breach-record timestamp.
    """

    def __init__(
        self,
        *,
        resolve_budget: Callable[[str], "tuple[BudgetGuards, str]"],
        elapsed_seconds: Callable[[str], float],
        read_coach_score: Callable[[str], "float | None"],
        record_breach: Callable[[str, str], None],
        publish_approval_request: PublishApprovalRequestFn,
        approval_subject_for: Callable[[str], str],
        clock: Callable[[], datetime],
    ) -> None:
        self._resolve_budget = resolve_budget
        self._elapsed_seconds = elapsed_seconds
        self._read_coach_score = read_coach_score
        self._record_breach = record_breach
        self._publish_approval_request = publish_approval_request
        self._approval_subject_for = approval_subject_for
        self._clock = clock

    def new_session(self) -> BudgetObserverSession:
        """Return fresh per-observer state (one per build observer)."""
        return BudgetObserverSession()

    async def observe_stage_complete(
        self,
        session: BudgetObserverSession,
        *,
        build_id: str,
        feature_id: str,
        coach_score: float | None,
    ) -> None:
        """Evaluate the budget after one ``stage-complete`` was published.

        Called by the observer AFTER :meth:`LifecycleBridgeWireup._publish_event`
        returns for a ``StageCompletePayload``. On the first breach it records
        + escalates; it never pauses / cancels / writes ``builds.status``. The
        caller wraps this in its own exception guard (a budget bug must never
        break the lifecycle stream), but the control flow here also degrades
        gracefully.

        Args:
            session: The observer's per-build state.
            build_id: The build the ``stage-complete`` belongs to.
            feature_id: The build's feature (for the escalation payload).
            coach_score: The envelope's coach score (fresher than the durable
                column; falls back to :attr:`read_coach_score` when ``None``).
        """
        # Caps-off (attended) or already-escalated → strict no-op. The
        # in-observer ``breached`` flag is the first half of first-breach-wins;
        # ``disabled`` short-circuits an attended build after the one-time
        # resolution so it never records, publishes, or reads further.
        if session.disabled or session.breached:
            return

        # Resolve the guards ONCE per observer (resolve-once). A caps-off
        # profile disables the session for good — no further work, honouring
        # the attended byte-equivalence law.
        if not session.resolved:
            guards, profile_name = self._resolve_budget(build_id)
            session.guards = guards
            session.profile_name = profile_name
            session.resolved = True
            if not guards.caps_enabled:
                session.disabled = True
                return

        assert session.guards is not None  # resolved above; caps_enabled True

        # Count this stage-complete as a review cycle (cycles-done semantics
        # match evaluate_budget's ``>=`` cap check).
        session.review_cycles += 1

        last_coach_score = (
            coach_score
            if coach_score is not None
            else self._read_coach_score(build_id)
        )
        metrics = BuildBudgetMetrics(
            review_cycles=session.review_cycles,
            elapsed_wallclock_seconds=self._elapsed_seconds(build_id),
            # Tokens stay unmeasured on this path (ADR-ARCH-033) — the cap is
            # inert until a real value flows.
            tokens_used=None,
            last_coach_score=last_coach_score,
        )
        verdict = evaluate_budget(session.guards, metrics)
        if verdict.ok:
            return

        # FIRST breach for this observer. Flip the in-observer flag before any
        # side effect so a re-entrant event can never double-escalate.
        session.breached = True

        detail = (
            f"{verdict.breached_cap}: {verdict.detail} @ "
            f"{self._clock().isoformat()}"
        )
        # Durable first-write-wins (never overwrites an earlier breach).
        self._record_breach(build_id, detail)

        # Deterministic per-breach request_id, matching the supervisor's
        # convention (``budget-{build_id}-{review_cycles}``) so a re-publish is
        # idempotent for responders that key off request_id.
        request_id = f"budget-{build_id}-{session.review_cycles}"
        payload = build_budget_breach_approval_payload(
            request_id=request_id,
            build_id=build_id,
            feature_id=feature_id,
            profile_name=session.profile_name,
            verdict=verdict,
            metrics=metrics,
        )
        subject = self._approval_subject_for(build_id)
        await self._publish_approval_request(payload, subject)

        # ONE loud log — the operator's mid-run probe. NO status change: the
        # run continues to its own bounded end (honesty law of this lane).
        logger.warning(
            "budget_observer: budget breach DETECTED for build_id=%s "
            "feature_id=%s profile=%s cap=%s (%s); recorded + escalated a "
            "risk=high approval on %s — the run is NOT paused/cancelled (mid-"
            "run hard stop is unavailable; it bounds itself) (UBS-002)",
            build_id,
            feature_id,
            session.profile_name,
            verdict.breached_cap,
            verdict.detail,
            subject,
        )


def build_budget_breach_observer(
    *,
    pool: Any,
    config: Any,
    publish_approval_request: PublishApprovalRequestFn,
    project: str | None = None,
) -> BudgetBreachObserver:
    """Compose the production :class:`BudgetBreachObserver` from serve reuse.

    Wires the injected callables from the already-built FEAT-UBS-002 serve
    helpers (do-not-redesign): the profile resolver, the wall-clock reader, the
    coach-score fallback, the first-write-wins recorder, and the approval
    subject resolver.

    The serve-side imports are lazy so importing this module (and the
    lifecycle-bridge wireup that references it) never pulls the heavy
    ``forge.cli.serve`` graph.

    Args:
        pool: The shared :class:`SqliteLifecyclePersistence` facade.
        config: The validated :class:`ForgeConfig` (source of ``budget``).
        publish_approval_request: The daemon's approval re-emit
            (:data:`PublishApprovalRequestFn`).
        project: Optional project scope for the approval subject; ``None``
            (the fleet-wide default) matches ``make_budget_pause``'s subject.

    Returns:
        A :class:`BudgetBreachObserver` ready to hand to
        :class:`~forge.lifecycle_bridge.wireup.LifecycleBridgeWireup`.
    """
    from forge.adapters.nats.approval_publisher import (
        AGENT_ID as _APPROVAL_AGENT_ID,
    )
    from forge.adapters.nats.approval_publisher import APPROVAL_SUBJECT_TEMPLATE
    from forge.cli.serve import (
        budget_wall_clock,
        make_budget_started_at_reader,
        resolve_budget_for_build,
    )
    from nats_core.topics import Topics

    started_at_reader = make_budget_started_at_reader(pool)

    def _elapsed_seconds(build_id: str) -> float:
        started = started_at_reader(build_id)
        if started is None:
            return 0.0
        return max(0.0, (budget_wall_clock() - started).total_seconds())

    def _approval_subject_for(build_id: str) -> str:
        subject = Topics.resolve(
            APPROVAL_SUBJECT_TEMPLATE,
            agent_id=_APPROVAL_AGENT_ID,
            task_id=build_id,
        )
        if project is not None:
            subject = Topics.for_project(project, subject)
        return subject

    return BudgetBreachObserver(
        resolve_budget=lambda build_id: resolve_budget_for_build(
            pool, config, build_id
        ),
        elapsed_seconds=_elapsed_seconds,
        read_coach_score=pool.read_last_coach_score,
        record_breach=pool.record_budget_breach,
        publish_approval_request=publish_approval_request,
        approval_subject_for=_approval_subject_for,
        clock=budget_wall_clock,
    )
