"""Mode C cycle planner — review-then-work iteration with cyclic terminator.

This is the only stage planner in the codebase that dispatches the same
stage class (``/task-work``) repeatedly within a single build. Mode C runs
a ``/task-review`` to identify fix tasks, then dispatches one ``/task-work``
per fix task in sequence, and finally schedules a follow-up ``/task-review``.
Termination is reviewer-driven (FEAT-FORGE-008 ASSUM-010): a follow-up
review that returns no further fix tasks ends the cycle. There is no
numeric iteration cap.

A plan is one of exactly three things — dispatch, terminal, or **wait**
(:class:`ModeCWait`). The wait variant was made explicit by the
conductor's revival (Stage 1a, design pass §h.1): an in-flight
``/task-work`` previously fell through to "all fix tasks completed" and
scheduled a premature follow-up review. It now yields a wait.

Two terminal outcomes are possible (ASSUM-005, ASSUM-007, ASSUM-017):

* :attr:`ModeCTerminal.CLEAN_REVIEW` — a review (initial or follow-up)
  emitted no fix tasks and no commits were produced.
* :attr:`ModeCTerminal.FAILED` — the most recent ``/task-review`` was
  hard-stopped or rejected, **or every work leg of the current review
  cycle failed** (ASSUM-008 as narrowed by Rich, 2026-08-02 — see
  :data:`_TERMINAL_STATUSES`). A *single* failed fix task still does not
  terminate the build: it is isolated to its own fix task per ASSUM-008
  and the planner returns the next fix task in line. The 100%-failed case
  also sets :attr:`ModeCPlan.total_work_failure` — the typed signal the
  supervisor branches on, because that terminal fires while the cycle's own
  review is still the latest one and the terminal handler has no branch for
  that shape.

When a follow-up review is clean and the build has produced commits, the
planner advances to :attr:`StageClass.PULL_REQUEST_REVIEW` instead of
terminating. The commit detection itself lives outside the planner: it
reads a ``has_commits`` flag set by TASK-MBC8-007's terminal handler.

The planner is **stateless**. Every call inspects ``history`` and the
``has_commits`` flag; cyclic behaviour emerges from the planner deciding
the same ``next_stage = TASK_WORK`` repeatedly until the most-recent
review's fix-task list is exhausted.

Each ``next_fix_task`` decision returns a :class:`FixTaskRef` carrying the
fix-task identifier and a back-reference (``review_history_index``) to the
originating ``/task-review`` entry — the audit anchor required by Group L
data-integrity scenarios.

References:
    - FEAT-FORGE-008 ASSUM-004 — Mode C chain shape.
    - FEAT-FORGE-008 ASSUM-005 — PR review when fixes change the branch.
    - FEAT-FORGE-008 ASSUM-007 — clean initial review terminates without
      dispatching ``/task-work``.
    - FEAT-FORGE-008 ASSUM-008 — failure isolation (failed ``/task-work``
      does not auto-cancel sibling fix tasks), NARROWED 2026-08-02: the
      isolation rule stops at 100% — a cycle whose every work leg failed
      terminates FAILED instead of scheduling a follow-up review.
    - FEAT-FORGE-008 ASSUM-010 — termination is reviewer-driven; no
      numeric iteration cap.
    - FEAT-FORGE-008 ASSUM-017 — clean follow-up review with no commits
      terminates the build.
    - TASK-MBC8-004 — this task brief.
    - TASK-MBC8-007 — owner of the ``has_commits`` flag.
    - TASK-MAG7-008 — ``dispatch_subprocess_stage`` produces the typed
      fix-task list consumed here as ``StageEntry.fix_tasks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence

from forge.lifecycle.persistence import Build
from forge.pipeline.mode_chains_data import MODE_C_CHAIN
from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
    "FixTaskLookup",
    "FixTaskRef",
    "ModeCCyclePlanner",
    "ModeCPlan",
    "ModeCTerminal",
    "ModeCWait",
    "StageEntry",
    "plan_next_stage",
]


# ---------------------------------------------------------------------------
# Status vocabulary — locally documented so callers know what to record
# ---------------------------------------------------------------------------


#: Status string indicating a stage entry was approved by its gate. The
#: only status that allows downstream dispatch in Mode C.
_STATUS_APPROVED: str = "approved"

#: Status string indicating the work leg itself failed. Kept separate from
#: :data:`_TERMINAL_STATUSES` because the 100%-failure rule below reads it
#: STRICTLY: ``rejected`` is a gate's verdict on real work and ``cancelled``
#: is a human's, and neither is evidence that the tooling is broken.
_STATUS_FAILED: str = "failed"

#: Status strings indicating a stage entry has reached a terminal outcome
#: (positive or negative). For ``/task-work`` the planner treats every
#: terminal status as "this fix task's slot is complete — advance" so
#: ASSUM-008 isolation is honoured (a failed fix task does not block its
#: siblings).
#:
#: **ASSUM-008, NARROWED — Rich's word, 2026-08-02.** The isolation rule
#: above is right per fix task and wrong at 100%. Because a FAILED work leg
#: closes its fix-task slot exactly like an approved one, a cycle in which
#: every single leg failed read as "all fix tasks completed" and the planner
#: scheduled a follow-up review — 42 times on the runaway ledger, with
#: 158/158 legs failed. Total work failure is a TOOLING FAULT, not a fix
#: outcome, and it is indistinguishable from total success only because
#: nothing was looking. So:
#:
#: * every fix task of the cycle terminal AND every terminal work row
#:   strictly ``"failed"`` → terminal FAILED, naming the class and the ids
#:   (:meth:`ModeCCyclePlanner._total_work_failure`);
#: * ANY ``approved`` / ``rejected`` / ``cancelled`` in the mix → today's
#:   behaviour, unchanged. The isolate-ONE-failure rule is untouched.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {_STATUS_APPROVED, _STATUS_FAILED, "rejected", "cancelled"}
)

#: Status strings on a ``/task-review`` entry that terminate the whole
#: Mode C build. Hard-stop is captured as a separate flag on
#: :class:`StageEntry` because the gate vocabulary distinguishes
#: ``hard_stop`` from a generic ``reject``.
_REVIEW_FAILURE_STATUSES: frozenset[str] = frozenset({"failed", "rejected"})


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ModeCTerminal(StrEnum):
    """Mode C terminal outcomes.

    Only set when the planner's ``next_stage`` is ``None``. The enum is a
    :class:`StrEnum` so the string values appear directly in stage history
    rationales without coercion.

    Members:
        CLEAN_REVIEW: A ``/task-review`` returned no fix tasks and no
            commits were produced. The build is "done" — nothing to fix,
            nothing to push.
        FAILED: A ``/task-review`` was hard-stopped or rejected, or every
            work leg of the current review cycle failed (ASSUM-008 as
            narrowed 2026-08-02). The build cannot proceed.
    """

    CLEAN_REVIEW = "clean-review"
    FAILED = "failed"


class ModeCWait(StrEnum):
    """Why the planner is waiting rather than dispatching or terminating.

    The third plan variant, made explicit by the conductor's revival
    (Stage 1a, design pass §h.1). Before it, "wait" and "terminate" were
    both encoded as ``next_stage=None`` and told apart only by whether
    ``terminal`` happened to be set — and the in-flight fix-task case did
    not even reach that: it fell through to "all fix tasks completed" and
    scheduled a *premature follow-up review* while a ``/task-work`` was
    still running. The code comment at that site admitted the encoding was
    wrong-if-reached. It is now right-if-reached: an in-flight fix task
    yields :attr:`FIX_TASK_IN_FLIGHT`, never a dispatch.

    A waiting plan means: nothing to do this tick; the build is neither
    advanced nor finished; re-plan when the in-flight stage resolves. The
    driver loop must **not** run the terminal handler on a waiting plan —
    the handler reads history structurally and would classify a mid-cycle
    build as a terminal outcome.

    Members:
        REVIEW_AWAITING_APPROVAL: The most recent ``/task-review`` has not
            reached ``approved`` (still pending / running). Dispatching
            ``/task-work`` before the review is approved would breach the
            Group B prerequisite invariant.
        FIX_TASK_IN_FLIGHT: A ``/task-work`` is still running for a fix
            task in the current review's list. The next fix task's slot
            cannot be judged open or closed until it resolves.
    """

    REVIEW_AWAITING_APPROVAL = "review-awaiting-approval"
    FIX_TASK_IN_FLIGHT = "fix-task-in-flight"


@dataclass(frozen=True, slots=True)
class FixTaskLookup:
    """Result of the "which fix task is next?" walk — three outcomes, typed.

    The helper used to answer with ``str | None``, which collapsed two
    genuinely different answers ("nothing left to do — schedule the
    follow-up review" and "the next one is still running — wait") into the
    same ``None``. The caller could only act on one of them, so it acted
    on the wrong one for the other (design pass risk h.1). Three named
    outcomes cannot be misread.

    Exactly one of the three is true on every instance:

    * ``fix_task_id`` is set — dispatch ``/task-work`` for it.
    * ``in_flight_id`` is set — wait; that fix task is still running.
    * both are ``None`` — every fix task in the current review's list has
      reached a terminal status; schedule the follow-up review.
    """

    fix_task_id: str | None = None
    in_flight_id: str | None = None

    @property
    def is_wait(self) -> bool:
        """``True`` iff a fix task is still in flight."""
        return self.in_flight_id is not None

    @property
    def is_exhausted(self) -> bool:
        """``True`` iff every fix task has reached a terminal status."""
        return self.fix_task_id is None and self.in_flight_id is None


@dataclass(frozen=True, slots=True)
class FixTaskRef:
    """Reference to a fix task identified by a specific ``/task-review`` entry.

    The ``review_history_index`` back-reference is the audit anchor that
    Group L lineage scenarios depend on: every dispatched ``/task-work``
    can be traced back to the exact review that emitted its fix-task
    identifier, even when later cycles emit the same identifier again.

    Attributes:
        fix_task_id: The fix-task identifier emitted by the review.
        review_history_index: Index into the planner's ``history`` argument
            of the ``/task-review`` entry that emitted this fix task.
        review_stage_label: Stage label of the originating review entry.
            Defaults to ``"task-review"`` — the canonical stage label.
            Carried explicitly so audit logs do not need to re-resolve it.
    """

    fix_task_id: str
    review_history_index: int
    review_stage_label: str = "task-review"


@dataclass(frozen=True, slots=True)
class StageEntry:
    """Planner-domain view of one recorded stage outcome.

    The planner does not consume :class:`forge.lifecycle.persistence.StageLogEntry`
    directly — that type is shaped by SQLite persistence concerns
    (``threshold_applied``, ``coach_score``, …) that the planner does not
    need. ``StageEntry`` is the minimal projection the planner reads: the
    stage class, its terminal status, and the per-stage payload (fix-task
    list for ``/task-review``; fix-task identifier for ``/task-work``).

    Adapters in TASK-MBC8-008 (Supervisor wiring) project the persisted
    ``StageLogEntry`` into this shape; tests construct it directly.

    Attributes:
        stage_class: The :class:`StageClass` of this entry.
        status: One of ``"approved"``, ``"failed"``, ``"rejected"``,
            ``"cancelled"``, or a non-terminal status (``"pending"``,
            ``"running"``). Only ``"approved"`` allows downstream dispatch.
        fix_tasks: For ``/task-review`` entries, the typed list of fix-task
            identifiers emitted by the reviewer. Empty tuple for entries
            that are not ``/task-review`` or for clean reviews.
        fix_task_id: For ``/task-work`` entries, the identifier of the fix
            task this dispatch worked on. ``None`` for entries that are
            not ``/task-work``.
        finding_anchors: For ``/task-review`` entries, the location
            identities of what the review found (LI stage-2 §5) —
            ``<file>|<severity>`` per finding. **Three-valued on purpose:**
            ``None`` means the row states nothing about anchors (a legacy
            row, or one whose key was unreadable), an empty tuple means the
            review reported no findings, and a populated tuple is the
            review's report. The planner does not read this field — the
            conductor's review-cycle no-progress stop does — but it is
            projected here because ``StageEntry`` is the one shape the
            history is read through.
        hard_stop: Whether the gate decision was a hard-stop. A hard-stop
            on ``/task-review`` terminates the build with FAILED regardless
            of the ``status`` string (gate vocabularies vary).
        failure_reason: The row's OWN account of why it failed — the
            dispatch rationale, which for a subprocess leg carries the
            leg's banner and output tail verbatim. ``None`` on every row
            that did not fail, and on legacy rows that recorded nothing.
            Leg-result honesty (2026-08-03): the terminal that stops a
            journey has to be able to SAY what stopped it, and by the time
            the handler runs, the leg's own words survive only here.
            Neither the planner nor the handler branches on this field's
            content — it is carried, never parsed.
    """

    stage_class: StageClass
    status: str
    fix_tasks: tuple[str, ...] = field(default=())
    fix_task_id: str | None = None
    finding_anchors: tuple[str, ...] | None = None
    hard_stop: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ModeCPlan:
    """The planner's decision for one Mode C step.

    Exactly one of the following is true on every plan:

    * ``next_stage`` is set and the supervisor dispatches that stage.
      ``terminal`` is ``None``. ``next_fix_task`` is set when
      ``next_stage == TASK_WORK``.
    * ``next_stage`` is ``None`` and ``terminal`` is set. The build has
      reached a terminal outcome — :attr:`ModeCTerminal.CLEAN_REVIEW` or
      :attr:`ModeCTerminal.FAILED`.
    * Both ``next_stage`` and ``terminal`` are ``None`` and ``wait`` is
      set — the planner is waiting on an in-flight prerequisite (the most
      recent review is not yet approved, or a ``/task-work`` is still
      running). The supervisor records the wait and retries on the next
      tick; it must NOT run the terminal handler on a waiting plan.

    ``wait`` is the discriminator to branch on — checking
    ``next_stage is None`` alone cannot tell a wait from a terminal.

    Attributes:
        permitted_stages: Frozenset of stage classes that are dispatchable
            under Mode C. Always equal to ``frozenset(MODE_C_CHAIN)``;
            published per-plan so callers can scope the dispatch switch
            without re-importing the chain data module.
        next_stage: The stage class to dispatch next, or ``None``.
        next_fix_task: When ``next_stage == TASK_WORK``, the
            :class:`FixTaskRef` carrying the fix-task identifier and back-
            reference to the originating ``/task-review``. ``None``
            otherwise.
        terminal: A :class:`ModeCTerminal` outcome when the build is done,
            otherwise ``None``.
        wait: A :class:`ModeCWait` reason when the planner is waiting on
            an in-flight prerequisite, otherwise ``None``. Set iff both
            ``next_stage`` and ``terminal`` are ``None``.
        rationale: A short human-readable string explaining the decision.
            The supervisor logs this against the build's stage history.
        total_work_failure: The failed fix-task ids, in dispatch order, when
            this terminal is the ASSUM-008 narrowing's 100%-failed-cycle
            ruling (Rich's word, 2026-08-02); ``None`` on every other plan.
            **This is a typed discriminator, not decoration.** The terminal
            handler classifies a cycle by the ``/task-work`` rows that ran
            *before* the latest review, and this terminal fires while that
            review is still the current one with its fix-task list intact —
            a shape the handler has no branch for, and whose defensive
            branch accuses its caller of a wiring bug. The supervisor reads
            this field to know the ruling is already made, so the rule is
            stated once, here, and never re-derived downstream.
    """

    permitted_stages: frozenset[StageClass]
    next_stage: StageClass | None
    next_fix_task: FixTaskRef | None = None
    terminal: ModeCTerminal | None = None
    wait: ModeCWait | None = None
    rationale: str = ""
    total_work_failure: tuple[str, ...] | None = None

    @property
    def is_waiting(self) -> bool:
        """``True`` iff this plan is a wait — neither dispatch nor terminal.

        The single predicate callers branch on. Kept as a property (rather
        than leaving callers to test ``wait is not None``) so the driver
        loop reads as ``if plan.is_waiting:`` and cannot accidentally
        re-derive the old, wrong ``next_stage is None`` test.
        """
        return self.wait is not None


# Frozenset of Mode C dispatchable stages; built once at import time so
# every plan can share the reference (frozensets are hashable + immutable).
_MODE_C_PERMITTED: frozenset[StageClass] = frozenset(MODE_C_CHAIN)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class ModeCCyclePlanner:
    """Stateless Mode C cycle planner.

    Single public method :meth:`plan_next_stage` consumes the build's
    recorded history and returns a :class:`ModeCPlan`. Repeated calls with
    the same history return equivalent plans — there is no internal state.

    Cyclic behaviour emerges from the planner repeatedly returning
    ``next_stage = TASK_WORK`` until the most recent review's fix-task
    list is exhausted, then scheduling a follow-up ``/task-review``.
    Termination is reviewer-driven — a follow-up review with no fix tasks
    ends the cycle.
    """

    def plan_next_stage(
        self,
        build: Build,
        history: Sequence[StageEntry],
        *,
        has_commits: bool = False,
    ) -> ModeCPlan:
        """Decide the next Mode C stage given the build and its history.

        Args:
            build: The build value object. Used today only for inclusion
                in returned rationales; the decision logic is purely
                structural over ``history``.
            history: The build's recorded stage entries in dispatch order.
                Mode C entries (``/task-review`` and ``/task-work``) are
                interleaved as the cycle runs.
            has_commits: Whether the build has produced commits against
                the working branch. Set by TASK-MBC8-007's terminal
                handler. Drives the choice between
                :attr:`ModeCTerminal.CLEAN_REVIEW` (no commits) and
                :attr:`StageClass.PULL_REQUEST_REVIEW` (commits) on a
                follow-up clean review.

        Returns:
            A :class:`ModeCPlan` describing the next decision.
        """
        del build  # build identity is not part of the planning decision
        permitted = _MODE_C_PERMITTED

        # Empty history → dispatch the initial /task-review.
        if not history:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=StageClass.TASK_REVIEW,
                rationale="initial review — empty history",
            )

        # Locate the most recent /task-review entry. Mode C always opens
        # with one; if for any reason the history contains no review, we
        # treat that as "dispatch a review" — the recovery-friendly choice.
        latest_review_idx = self._latest_review_index(history)
        if latest_review_idx is None:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=StageClass.TASK_REVIEW,
                rationale="no /task-review in history — dispatching initial review",
            )

        latest_review = history[latest_review_idx]

        # /task-review hard-stop or reject → terminal FAILED. AC-007 plus
        # the Group C "reject decision before PR terminates the build"
        # scenario both flow through this branch.
        if latest_review.hard_stop or latest_review.status in _REVIEW_FAILURE_STATUSES:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=None,
                terminal=ModeCTerminal.FAILED,
                rationale=(
                    "hard-stop on /task-review"
                    if latest_review.hard_stop
                    else (
                        f"/task-review {latest_review.status} — terminal FAILED"
                        # The leg's own words, when it left any. This
                        # rationale is the FALLBACK the supervisor records
                        # if the terminal handler cannot be reached, so it
                        # must be able to say what stopped the journey too.
                        + (
                            f" ({latest_review.failure_reason})"
                            if latest_review.failure_reason
                            else ""
                        )
                    )
                ),
            )

        # /task-review still pending or running → wait. AC: ``/task-work``
        # does not dispatch before the review is approved (Group B
        # prerequisite invariant).
        if latest_review.status != _STATUS_APPROVED:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=None,
                wait=ModeCWait.REVIEW_AWAITING_APPROVAL,
                rationale=(
                    "/task-review awaiting approval "
                    f"(status={latest_review.status!r})"
                ),
            )

        # Approved review — fan out work or terminate based on fix-task list.
        fix_tasks = latest_review.fix_tasks
        if not fix_tasks:
            return self._decide_clean_review(
                history=history,
                latest_review_idx=latest_review_idx,
                has_commits=has_commits,
                permitted=permitted,
            )

        # Find the next fix task that has not yet reached a terminal
        # status under this review's work iteration. Three outcomes,
        # each typed on the lookup (design pass §h.1) — the "still
        # running" case must NOT fall through to the follow-up review.
        lookup = self._next_undispatched_fix_task(
            history=history,
            latest_review_idx=latest_review_idx,
            fix_tasks=fix_tasks,
        )

        if lookup.fix_task_id is not None:
            ref = FixTaskRef(
                fix_task_id=lookup.fix_task_id,
                review_history_index=latest_review_idx,
            )
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=StageClass.TASK_WORK,
                next_fix_task=ref,
                rationale=(
                    f"dispatch /task-work for fix task {lookup.fix_task_id!r}"
                ),
            )

        if lookup.is_wait:
            # A /task-work is still running for this fix task. Wait —
            # scheduling the follow-up review here would review a
            # half-finished cycle and (worse) put a second stage in
            # flight for the same build.
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=None,
                wait=ModeCWait.FIX_TASK_IN_FLIGHT,
                rationale=(
                    f"/task-work still in flight for fix task "
                    f"{lookup.in_flight_id!r} — waiting"
                ),
            )

        # Every fix task reached a terminal status. Before reading that as
        # "the cycle did its work", ask the ASSUM-008 narrowing's question:
        # did ANY leg actually run? A cycle whose every leg failed is a
        # broken tool reporting completeness, and scheduling a follow-up
        # review over it burns another cycle to rediscover the same
        # findings (the runaway's 42 repetitions).
        all_failed = self._total_work_failure(
            history=history,
            latest_review_idx=latest_review_idx,
            fix_tasks=fix_tasks,
        )
        if all_failed is not None:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=None,
                terminal=ModeCTerminal.FAILED,
                rationale=(
                    "every work leg in this cycle failed — a tooling fault, "
                    "not a fix outcome; no follow-up review is scheduled "
                    f"(fix tasks: {', '.join(all_failed)})"
                ),
                total_work_failure=all_failed,
            )

        # All fix tasks reached terminal status and at least one leg was
        # something other than a bare failure — schedule a follow-up
        # /task-review per ASSUM-010 (no numeric cap).
        return ModeCPlan(
            permitted_stages=permitted,
            next_stage=StageClass.TASK_REVIEW,
            rationale="all fix tasks completed — scheduling follow-up review",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _latest_review_index(
        history: Sequence[StageEntry],
    ) -> int | None:
        """Return the index of the most recent ``/task-review`` entry.

        Returns ``None`` if the history contains no review entries — that
        case is recoverable (the planner dispatches an initial review).
        """
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].stage_class == StageClass.TASK_REVIEW:
                return idx
        return None

    @staticmethod
    def _next_undispatched_fix_task(
        *,
        history: Sequence[StageEntry],
        latest_review_idx: int,
        fix_tasks: tuple[str, ...],
    ) -> FixTaskLookup:
        """Return the first fix task whose ``/task-work`` slot is open.

        Walks ``fix_tasks`` in declaration order and returns the first
        identifier that has not reached a terminal status under the
        current review iteration. A fix task is considered "complete"
        (slot closed) when a ``/task-work`` entry recorded *after*
        ``latest_review_idx`` references it with a status in
        :data:`_TERMINAL_STATUSES` — including ``"failed"``. ASSUM-008
        ("failure isolated to its fix task") means a failed slot still
        unblocks dispatch of the *next* fix task.

        Returns a :class:`FixTaskLookup` rather than ``str | None``: the
        "still running" answer and the "nothing left" answer are
        different instructions to the caller and must not share an
        encoding (design pass §h.1 — the named in-code defect this
        replaces returned ``None`` for both, so an in-flight fix task
        scheduled a premature follow-up review).
        """
        # Collect terminal-status work entries for the current review
        # iteration only — earlier iterations may have re-emitted the
        # same fix-task identifier and we must not confuse the lineage.
        completed_ids: set[str] = set()
        in_flight_ids: set[str] = set()
        for entry in history[latest_review_idx + 1 :]:
            if entry.stage_class != StageClass.TASK_WORK:
                continue
            if entry.fix_task_id is None:
                # Defensive: a /task-work entry with no fix_task_id is an
                # invariant violation upstream. Skip it rather than
                # crash; the missing fix task will be re-dispatched on
                # the next planning tick.
                continue
            if entry.status in _TERMINAL_STATUSES:
                completed_ids.add(entry.fix_task_id)
            else:
                in_flight_ids.add(entry.fix_task_id)

        for fix_task_id in fix_tasks:
            if fix_task_id in completed_ids:
                continue
            if fix_task_id in in_flight_ids:
                # A prior ``/task-work`` is still running for this fix
                # task. This is the WAIT answer, and it is its own
                # variant — not a ``None`` shared with "nothing left to
                # do". The caller turns it into a waiting plan; it never
                # reaches the follow-up-review branch.
                return FixTaskLookup(in_flight_id=fix_task_id)
            return FixTaskLookup(fix_task_id=fix_task_id)

        # Every fix task in this review's list has a terminal
        # ``/task-work`` slot — the cycle's fan-out is exhausted.
        return FixTaskLookup()

    @staticmethod
    def _total_work_failure(
        *,
        history: Sequence[StageEntry],
        latest_review_idx: int,
        fix_tasks: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        """Fix-task ids of a 100%-failed cycle, or ``None``.

        The ASSUM-008 narrowing (Rich's word, 2026-08-02). Called only from
        the exhausted branch of the fix-task walk, so "every fix task of
        this cycle is terminal" is already established; this answers the
        second half — **was every one of those terminal rows strictly a
        failure?**

        Returns:
            The failed fix-task ids in dispatch order, de-duplicated, when
            the cycle recorded at least one terminal ``/task-work`` row and
            EVERY terminal ``/task-work`` row in it carries the status
            ``"failed"``. ``None`` otherwise — and ``None`` means "keep
            today's behaviour", which is the answer for:

            * any ``approved`` in the mix (some work landed);
            * any ``rejected`` or ``cancelled`` in the mix (a gate's or a
              human's verdict on work that RAN — the strictness is the
              whole point, because a tooling fault is what the rule
              claims and a rejection is not one);
            * a cycle with no terminal work rows at all (nothing to
              accuse — an empty review's clean-review branch handles it
              before the walk ever reaches here).

        Args:
            history: The build's stage entries in dispatch order.
            latest_review_idx: Index of the review whose cycle is judged.
            fix_tasks: That review's fix-task list. **Rows whose
                ``fix_task_id`` is not in it are not this cycle's evidence
                and are skipped** — the same window
                :meth:`_next_undispatched_fix_task` uses when it decides the
                fan-out is exhausted, so the two cannot disagree about which
                rows exist. Without the filter a foreign or stale row got
                named in the operator-facing rationale as one of this
                cycle's fix tasks.

        In-flight rows are ignored rather than treated as counter-evidence:
        the caller cannot reach this method while one exists (the walk
        returns a WAIT first), and skipping them keeps the rule readable as
        "of the legs that finished, every one failed".
        """
        wanted = frozenset(fix_tasks)
        failed: list[str] = []
        for entry in history[latest_review_idx + 1 :]:
            if entry.stage_class != StageClass.TASK_WORK:
                continue
            if entry.fix_task_id is None:
                # Same defensive skip the fix-task walk makes: an
                # unattributable row is an upstream invariant violation and
                # is not evidence either way.
                continue
            if entry.fix_task_id not in wanted:
                continue
            if entry.status not in _TERMINAL_STATUSES:
                continue
            if entry.status != _STATUS_FAILED:
                return None
            if entry.fix_task_id not in failed:
                failed.append(entry.fix_task_id)

        # An empty list is the "no terminal work rows in this cycle" answer:
        # every row that could have set it also appends to it (a terminal row
        # is either strictly failed and appended, or returns above), so the
        # list IS the saw-a-terminal-row flag. A separate boolean would have
        # been a second statement of the same fact.
        if not failed:
            return None
        return tuple(failed)

    @staticmethod
    def _decide_clean_review(
        *,
        history: Sequence[StageEntry],
        latest_review_idx: int,
        has_commits: bool,
        permitted: frozenset[StageClass],
    ) -> ModeCPlan:
        """Resolve a clean (empty fix-task) review into a terminal or PR plan.

        Logic per AC-005 / ASSUM-005 / ASSUM-007 / ASSUM-017:

        * Initial clean review (no preceding ``/task-work``) → terminal
          ``CLEAN_REVIEW``. There is no PR review even if some other
          process has produced commits — Mode C only opens a PR when the
          build itself produced fixes through ``/task-work`` (ASSUM-005).
        * Follow-up clean review with no commits → terminal
          ``CLEAN_REVIEW`` (ASSUM-017).
        * Follow-up clean review with commits → advance to
          ``PULL_REQUEST_REVIEW`` (ASSUM-005).
        """
        # Detect whether any /task-work has run prior to this review.
        # Initial review = no prior /task-work in history.
        had_prior_work = any(
            entry.stage_class == StageClass.TASK_WORK
            for entry in history[:latest_review_idx]
        )

        if not had_prior_work:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=None,
                terminal=ModeCTerminal.CLEAN_REVIEW,
                rationale="initial /task-review returned no fix tasks",
            )

        if has_commits:
            return ModeCPlan(
                permitted_stages=permitted,
                next_stage=StageClass.PULL_REQUEST_REVIEW,
                rationale=(
                    "follow-up /task-review clean — fixes produced commits, "
                    "advancing to pull-request review"
                ),
            )

        return ModeCPlan(
            permitted_stages=permitted,
            next_stage=None,
            terminal=ModeCTerminal.CLEAN_REVIEW,
            rationale=(
                "follow-up /task-review clean — no commits, terminal clean review"
            ),
        )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def plan_next_stage(
    build: Build,
    history: Sequence[StageEntry],
    *,
    has_commits: bool = False,
) -> ModeCPlan:
    """Module-level convenience wrapper around :class:`ModeCCyclePlanner`.

    The class is stateless so the singleton wrapper is safe; callers that
    prefer a function form (mirroring ``MODE_C_PREREQUISITES`` and other
    declarative module-level surfaces in :mod:`forge.pipeline`) can use
    this without instantiating the class.
    """
    return ModeCCyclePlanner().plan_next_stage(build, history, has_commits=has_commits)
