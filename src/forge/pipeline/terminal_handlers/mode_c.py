"""Mode C terminal-state handler — empty review, no commits, PR review (TASK-MBC8-007).

Mode C has three terminal-routing outcomes after the cycle planner
exhausts its fix-task fan-out (FEAT-FORGE-008 ASSUM-005, ASSUM-007,
ASSUM-017):

* :attr:`ModeCTerminal.CLEAN_REVIEW_NO_FIXES` — the *initial*
  ``/task-review`` returned an empty fix-task list. No ``/task-work``
  was dispatched and the build completes immediately.
* :attr:`ModeCTerminal.CLEAN_REVIEW_NO_COMMITS` — every dispatched
  ``/task-work`` ran to a terminal status and at least one was
  approved, but the build's worktree shows zero commits between the
  build's base and HEAD. The constitutional PR-review gate has nothing
  to fire on, so the build completes with no PR creation attempt.
* :attr:`ModeCTerminal.PR_REVIEW` — at least one ``/task-work`` was
  approved and the worktree carries one or more commits. Routing
  proceeds to :attr:`forge.pipeline.stage_taxonomy.StageClass.PULL_REQUEST_REVIEW`.
* :attr:`ModeCTerminal.FAILED` — either the most recent ``/task-review``
  was hard-stopped/rejected, or every dispatched ``/task-work`` ended
  in a failed terminal lifecycle, or the commit-count probe itself
  failed (``"mode-c-commit-check-failed"``). A probe failure is **not**
  silently demoted to a clean review — the build is recorded as
  failed and the operator gets the underlying error in the rationale.

This module is the **single decision point** for Mode C terminal
routing. The planner (``forge.pipeline.mode_c_planner``) handles the
mid-cycle fan-out; this handler handles the cycle's exit.

The handler also exposes two small helpers for the supervisor wiring:

* :func:`build_task_work_attribution` — builds the per-fix-task
  ``stage_log`` ``details`` payload so each ``TASK_WORK`` entry carries
  ``fix_task_id``, ``originating_review_entry_id``, and an
  ``artefact_paths`` list filtered to only paths produced by *that* fix
  task (Group G "no artefact path attributed to more than one fix
  task" + Group L lineage).
* :func:`build_session_outcome_payload` — builds the
  :class:`forge.memory.models.SessionOutcome` payload for the
  decision; CLEAN_REVIEW_* outcomes carry no ``pull_request_url`` and
  no PR-review gate decision (Group N "session outcome reflects mode
  terminal").

References:
    - FEAT-FORGE-008 ASSUM-005 — PR review when fixes change the
      branch.
    - FEAT-FORGE-008 ASSUM-007 — clean initial review terminates
      without dispatching ``/task-work``.
    - FEAT-FORGE-008 ASSUM-008 — failure isolation (failed
      ``/task-work`` does not auto-cancel siblings).
    - FEAT-FORGE-008 ASSUM-017 — clean follow-up review with no
      commits terminates the build.
    - TASK-MBC8-004 — ``ModeCCyclePlanner`` consumes ``has_commits``
      from this handler's decision.
    - TASK-MBC8-007 — this task brief.
    - FEAT-FORGE-005 — worktree allowlist (path-resolution surface
      reused by the production commit-probe wiring).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.lifecycle.persistence import Build
from forge.pipeline.mode_c_planner import StageEntry
from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
    "CommitProbe",
    "CommitProbeResult",
    "ModeCTerminal",
    "ModeCTerminalDecision",
    "RATIONALE_FAILED_ALL_WORK_FAILED",
    "build_session_outcome_payload",
    "build_task_work_attribution",
    "evaluate_terminal",
]

logger = logging.getLogger("forge.pipeline.terminal_handlers.mode_c")


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


#: Status string indicating an approved gate decision.
_STATUS_APPROVED: str = "approved"

#: Statuses that count as a *failed* terminal outcome on a single stage
#: entry. ``cancelled`` is treated as failed for terminal-routing
#: purposes — the operator-cancelled fix task does not count as a
#: success that justifies a PR.
_FAILED_STATUSES: frozenset[str] = frozenset({"failed", "rejected", "cancelled"})

#: Statuses on a ``/task-review`` entry that terminate the whole Mode C
#: build with FAILED. Hard-stop is captured separately on
#: :class:`StageEntry` because the gate vocabulary distinguishes
#: ``hard_stop`` from a generic ``rejected``.
_REVIEW_FAILURE_STATUSES: frozenset[str] = frozenset({"failed", "rejected"})


# ---------------------------------------------------------------------------
# Stage-log rationale strings
# ---------------------------------------------------------------------------


#: Rationale string recorded on the terminal stage entry when the
#: initial ``/task-review`` returned no fix tasks (AC-002).
_RATIONALE_NO_FIXES: str = "mode-c-task-review-empty"

#: Rationale string recorded when every dispatched ``/task-work`` ran
#: but produced no commits in the worktree (AC-003).
_RATIONALE_NO_COMMITS: str = "mode-c-no-commits"

#: Rationale string recorded when the build advances to PR review
#: because the fix-task loop produced at least one commit (AC-004).
_RATIONALE_PR_REVIEW: str = "mode-c-commits-present"

#: Rationale string recorded on a ``/task-review`` hard-stop (AC-005).
_RATIONALE_FAILED_HARD_STOP: str = "mode-c-task-review-hard-stop"

#: Rationale string recorded on a ``/task-review`` reject/failure
#: terminal status that is **not** a hard-stop (AC-005).
_RATIONALE_FAILED_REVIEW_REJECTED: str = "mode-c-task-review-rejected"

#: Rationale string recorded when every dispatched ``/task-work``
#: ended in a failed terminal lifecycle (AC-005).
#:
#: PUBLIC because the supervisor records the very same word for the
#: planner-side 100%-failed-cycle terminal (ASSUM-008 as narrowed, Rich's
#: word 2026-08-02), which stops the journey one turn EARLIER than this
#: handler's branch 5a can see. Two constants spelling the same operator
#: sentence would be one edit away from a lie, so there is one.
RATIONALE_FAILED_ALL_WORK_FAILED: str = "mode-c-all-task-work-failed"

#: Rationale string recorded when the ``git rev-list base..HEAD --count``
#: probe itself failed. Defence in depth: the probe error is treated
#: as build-level FAILED, not silently demoted to a clean review (per
#: the implementation note on TASK-MBC8-007).
_RATIONALE_FAILED_COMMIT_CHECK: str = "mode-c-commit-check-failed"

#: Rationale string when the handler is invoked against a build that
#: never produced a ``/task-review`` entry — defensive only; in
#: production the supervisor never reaches the terminal handler before
#: the cycle's first review row is committed to ``stage_log``.
_RATIONALE_FAILED_NO_REVIEW: str = "mode-c-no-task-review-recorded"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ModeCTerminal(StrEnum):
    """Terminal-handler outcomes for a Mode C build (AC-001).

    Distinct from :class:`forge.pipeline.mode_c_planner.ModeCTerminal`
    which collapses CLEAN_REVIEW_NO_FIXES and CLEAN_REVIEW_NO_COMMITS
    into a single ``CLEAN_REVIEW`` value: the planner only needs to
    know "stop the cycle, no PR" but the *terminal handler* needs to
    distinguish the two clean cases so the recorded ``stage_log``
    rationale (and the corresponding session-outcome payload)
    accurately reflects which clean path was taken.

    ``StrEnum`` so the values appear as literal strings in the
    ``stage_log.details`` JSON without coercion.

    Members:
        CLEAN_REVIEW_NO_FIXES: The initial ``/task-review`` returned
            an empty fix-task list. No ``/task-work`` was dispatched.
        CLEAN_REVIEW_NO_COMMITS: Every dispatched ``/task-work``
            reached a terminal status and at least one was approved,
            but ``git rev-list base..HEAD --count`` returned zero.
        PR_REVIEW: At least one approved ``/task-work`` exists and the
            worktree carries one or more commits — route to PR review.
        FAILED: A ``/task-review`` was hard-stopped or rejected, or
            every ``/task-work`` failed, or the commit probe failed.
    """

    CLEAN_REVIEW_NO_FIXES = "clean-review-no-fixes"
    CLEAN_REVIEW_NO_COMMITS = "clean-review-no-commits"
    PR_REVIEW = "pr-review"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommitProbeResult:
    """Outcome of the ``git rev-list base..HEAD --count`` probe.

    Attributes:
        count: The integer commit count reported by ``git rev-list
            --count``. Zero means the worktree's HEAD matches the
            build's base — there is nothing to push. ``failed=True``
            sets ``count=0`` defensively but ``count`` should not be
            consumed when ``failed`` is set.
        failed: Whether the probe itself failed (non-zero exit code,
            spawn error, allowlist violation). A failed probe is
            recorded as :attr:`ModeCTerminal.FAILED` with rationale
            ``"mode-c-commit-check-failed"``; it is NOT silently
            demoted to a clean review (TASK-MBC8-007 implementation
            note).
        error: Human-readable error string captured from the probe's
            stderr (or the exception type+message if the call raised).
            Threaded into :attr:`ModeCTerminalDecision.failure_reason`
            so operators can debug the underlying git failure without
            re-running the build.
    """

    count: int
    failed: bool = False
    error: str | None = None

    @property
    def has_commits(self) -> bool:
        """Return ``True`` iff the probe succeeded and reported >0 commits."""
        return not self.failed and self.count > 0


CommitProbe = Callable[[Build], Awaitable[CommitProbeResult]]
"""Async callable that returns the worktree's commit count.

Production wiring (FEAT-FORGE-005 worktree allowlist + the git
adapter) injects a real implementation; tests inject a fake. The
contract is:

* Resolve the build's worktree path through the worktree allowlist
  (do NOT invent a new path resolver — see TASK-MBC8-007 implementation
  note).
* Run ``git rev-list <base>..HEAD --count`` against that path.
* Return the integer count on success, or
  ``CommitProbeResult(count=0, failed=True, error=...)`` on any
  failure (non-zero exit, spawn error, allowlist denial).

The callable is async because it shells out to ``git``; the rest of
this module is sync and the await is the single I/O suspension
point.
"""


@dataclass(frozen=True, slots=True)
class ModeCTerminalDecision:
    """The handler's decision for one Mode C build.

    Attributes:
        outcome: The :class:`ModeCTerminal` variant.
        has_commits: Whether the build's worktree carries one or more
            commits. Threaded onto :class:`forge.pipeline.BuildContext`
            by the supervisor (AC-006) so
            :class:`forge.pipeline.mode_c_planner.ModeCCyclePlanner`
            can route the follow-up review branch.
        rationale: Short canonical rationale string recorded on the
            terminal ``stage_log`` entry. One of the module-level
            ``_RATIONALE_*`` constants.
        pull_request_url: Always ``None`` from this handler — the URL
            is set later by the PR-creation adapter when ``outcome ==
            PR_REVIEW``. Carried as a field so
            :func:`build_session_outcome_payload` can omit it
            structurally rather than via in-band ``None`` checks
            scattered across call sites (AC-009).
        failure_reason: Human-readable explanation when ``outcome ==
            FAILED``. Carries the originating hard-stop rationale or
            the probe's stderr verbatim. ``None`` for non-FAILED
            outcomes.
    """

    outcome: ModeCTerminal
    has_commits: bool
    rationale: str
    pull_request_url: str | None = None
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def evaluate_terminal(
    build: Build,
    history: Sequence[StageEntry],
    *,
    commit_probe: CommitProbe | None = None,
) -> ModeCTerminalDecision:
    """Evaluate the terminal state of a Mode C build (AC-001 — AC-005).

    The function inspects ``history`` to classify the most recent
    review and the work entries that ran under it, then optionally
    awaits ``commit_probe`` to derive ``has_commits`` for the two
    paths that need it (CLEAN_REVIEW_NO_COMMITS vs PR_REVIEW).

    Decision tree (each branch is mutually exclusive):

    1. No ``/task-review`` row in history → FAILED (defensive — should
       not happen in production; see ``_RATIONALE_FAILED_NO_REVIEW``).
    2. Latest ``/task-review`` is hard-stopped → FAILED with
       ``_RATIONALE_FAILED_HARD_STOP``.
    3. Latest ``/task-review`` is rejected/failed (no hard-stop flag)
       → FAILED with ``_RATIONALE_FAILED_REVIEW_REJECTED``.
    4. Latest ``/task-review`` approved with empty fix-task list and
       no prior ``/task-work`` → CLEAN_REVIEW_NO_FIXES (AC-002).
    5. Latest ``/task-review`` approved with prior ``/task-work``
       entries:

       a. If every prior ``/task-work`` ended in failure → FAILED
          with ``RATIONALE_FAILED_ALL_WORK_FAILED`` (AC-005 part 2).
       b. Otherwise probe the worktree via ``commit_probe``:

          * Probe failed → FAILED with
            ``_RATIONALE_FAILED_COMMIT_CHECK``.
          * Probe count == 0 → CLEAN_REVIEW_NO_COMMITS (AC-003).
          * Probe count > 0 → PR_REVIEW (AC-004).

    Args:
        build: The Mode C build value object — passed through to
            ``commit_probe`` so the probe can resolve the worktree
            path. Not otherwise inspected here; the decision is
            structural over ``history``.
        history: The build's recorded stage entries in dispatch order.
        commit_probe: Async callable that returns the worktree's
            commit count. Required for the no-commits / PR-review
            split (decision-tree branch 5b). When ``None`` and the
            decision tree reaches branch 5b, the handler raises
            ``RuntimeError`` — the supervisor must always wire a
            probe. Earlier branches do not call the probe and remain
            valid with ``commit_probe=None`` (this is what makes
            FAILED-only and CLEAN_REVIEW_NO_FIXES tests trivial).

    Returns:
        :class:`ModeCTerminalDecision` carrying the outcome variant,
        the ``has_commits`` flag, the rationale string for the
        ``stage_log`` entry, and (on FAILED) a human-readable
        ``failure_reason``.

    Raises:
        RuntimeError: ``commit_probe`` is ``None`` but the decision
            tree reached the commit-count branch. This is a wiring
            bug — the supervisor MUST inject a probe before
            invoking the handler in production.
    """
    # ``build`` is threaded straight through to ``commit_probe`` for
    # worktree-path resolution; the structural decision tree below
    # reads only ``history``.

    # 1 — no review in history (defensive)
    latest_review_idx = _latest_review_index(history)
    if latest_review_idx is None:
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_NO_REVIEW,
            failure_reason="no /task-review entry in history",
        )

    latest_review = history[latest_review_idx]

    # 2 — hard-stop on the review terminates the whole build with
    # FAILED. Captured ahead of the rejected-status branch because the
    # gate vocabulary attaches ``hard_stop=True`` independently of the
    # status string (TASK-MBC8-004 same logic).
    if latest_review.hard_stop:
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_HARD_STOP,
            failure_reason=(
                f"/task-review hard-stop (status={latest_review.status!r})"
            ),
        )

    # 3 — rejected / failed review without hard-stop flag.
    if latest_review.status in _REVIEW_FAILURE_STATUSES:
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_REVIEW_REJECTED,
            failure_reason=f"/task-review {latest_review.status}",
        )

    # The review is approved (non-terminal statuses like "pending" /
    # "running" should never reach the terminal handler — those are
    # the planner's wait branch — but for defensive robustness we
    # treat any non-approved-non-failed status as FAILED rather than
    # crashing).
    if latest_review.status != _STATUS_APPROVED:
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_REVIEW_REJECTED,
            failure_reason=(
                f"unexpected /task-review status {latest_review.status!r} "
                "at terminal handler"
            ),
        )

    # 4 — approved review with empty fix-task list. Distinguish
    # initial-empty (no prior /task-work) from follow-up-clean
    # (prior /task-work present).
    fix_tasks = latest_review.fix_tasks
    work_before = [
        e for e in history[:latest_review_idx] if e.stage_class == StageClass.TASK_WORK
    ]

    if not work_before and not fix_tasks:
        # AC-002: initial review returned no fix tasks. The build is
        # clean by definition — the constitutional guard never
        # dispatched any work that could have produced commits.
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.CLEAN_REVIEW_NO_FIXES,
            has_commits=False,
            rationale=_RATIONALE_NO_FIXES,
        )

    # 5 — follow-up clean review (or non-empty fix-tasks edge case).
    # Inspect the prior /task-work entries.
    if fix_tasks:
        # Defensive: the terminal handler was invoked while the
        # cycle is still mid-flight. Treat as FAILED rather than
        # making an unsafe routing decision; the supervisor is the
        # caller responsible for never invoking us before the cycle
        # has exhausted its fix-task list.
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_REVIEW_REJECTED,
            failure_reason=(
                "evaluate_terminal called mid-cycle: latest /task-review "
                f"still has {len(fix_tasks)} fix task(s) outstanding"
            ),
        )

    # 5a — every dispatched /task-work failed. The build cannot
    # produce commits and must not advance to PR review even if the
    # branch happens to carry pre-existing commits from another
    # process (ASSUM-005: PR review only when the *build itself*
    # produced fixes).
    approved_count = sum(1 for e in work_before if e.status == _STATUS_APPROVED)
    failed_count = sum(1 for e in work_before if e.status in _FAILED_STATUSES)
    if approved_count == 0 and failed_count == len(work_before):
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=RATIONALE_FAILED_ALL_WORK_FAILED,
            failure_reason=(
                f"every dispatched /task-work failed "
                f"({failed_count} of {len(work_before)})"
            ),
        )

    # 5b — at least one approved /task-work; probe the worktree to
    # split CLEAN_REVIEW_NO_COMMITS vs PR_REVIEW.
    if commit_probe is None:
        raise RuntimeError(
            "evaluate_terminal: commit_probe is required for the "
            "CLEAN_REVIEW_NO_COMMITS / PR_REVIEW decision (no-fixes and "
            "FAILED branches do not need it). Wire one through the "
            "supervisor before invoking this handler in production."
        )

    probe_result = await commit_probe(build)
    if probe_result.failed:
        # Probe itself failed — defence in depth. Per the TASK-MBC8-007
        # implementation note, this is FAILED, NOT silently CLEAN_REVIEW.
        logger.warning(
            "mode_c_terminal_commit_probe_failed",
            extra={
                "build_id": build.build_id,
                "error": probe_result.error,
            },
        )
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale=_RATIONALE_FAILED_COMMIT_CHECK,
            failure_reason=probe_result.error or "git rev-list probe failed",
        )

    if probe_result.has_commits:
        return ModeCTerminalDecision(
            outcome=ModeCTerminal.PR_REVIEW,
            has_commits=True,
            rationale=_RATIONALE_PR_REVIEW,
        )

    return ModeCTerminalDecision(
        outcome=ModeCTerminal.CLEAN_REVIEW_NO_COMMITS,
        has_commits=False,
        rationale=_RATIONALE_NO_COMMITS,
    )


# ---------------------------------------------------------------------------
# Per-fix-task attribution (AC-007, AC-008) and session-outcome payload
# (AC-009) — small helpers consumed by the supervisor wiring.
# ---------------------------------------------------------------------------


def build_task_work_attribution(
    *,
    fix_task_id: str,
    originating_review_entry_id: str,
    artefact_paths: Sequence[str],
    fix_task_artefact_index: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """Build the ``stage_log.details`` payload for a ``TASK_WORK`` entry.

    Implements AC-007 (per-fix-task artefact attribution) and AC-008
    (fix-task lineage).

    Group G's invariant — "no artefact path attributed to more than
    one fix task" — is enforced when ``fix_task_artefact_index`` is
    provided: each candidate path is admitted only if it appears in
    the index entry for *this* fix task. Without the index, the
    function trusts the caller — the index is the supervisor's
    primary defence against accidental cross-attribution under
    failure-isolation (ASSUM-008) re-runs.

    Args:
        fix_task_id: Identifier of the fix task this ``TASK_WORK``
            entry was dispatched for. Threaded onto the persisted
            row so Group L lineage scenarios can resolve "which fix
            task produced this artefact".
        originating_review_entry_id: ``stage_log.entry_id`` of the
            ``/task-review`` row that emitted this fix-task
            identifier. The audit anchor required by Group L's
            data-integrity scenario.
        artefact_paths: Candidate artefact paths produced under this
            ``TASK_WORK`` dispatch. May include paths that belong to
            sibling fix tasks if the dispatcher accidentally widened
            its capture window — the index filter rejects those.
        fix_task_artefact_index: Optional mapping from
            ``fix_task_id`` to the frozen set of paths that belong
            to that fix task. Provided by the supervisor when
            cross-attribution prevention is required; tests omit it
            when exercising the basic shape.

    Returns:
        A ``dict`` suitable for ``StageLogEntry.details``. Always
        carries the three keys ``fix_task_id``,
        ``originating_review_entry_id``, and ``artefact_paths``.
        ``artefact_paths`` is a list (not a tuple) because Pydantic
        v2 + JSON-encoded ``details`` columns expect plain lists.
    """
    if fix_task_artefact_index is None:
        attributed = list(artefact_paths)
    else:
        allowed = fix_task_artefact_index.get(fix_task_id, frozenset())
        attributed = [path for path in artefact_paths if path in allowed]

    return {
        "fix_task_id": fix_task_id,
        "originating_review_entry_id": originating_review_entry_id,
        "artefact_paths": attributed,
    }


def build_session_outcome_payload(
    decision: ModeCTerminalDecision,
) -> dict[str, Any]:
    """Build the SessionOutcome ``details`` payload for a Mode C decision.

    Implements AC-009: CLEAN_REVIEW_* outcomes carry **no**
    ``pull_request_url`` and **no** PR-review gate decision. PR_REVIEW
    outcomes do not carry the URL from this handler either — the URL
    is set later by the PR-creation adapter — but PR_REVIEW outcomes
    are the only ones for which ``pull_request_url`` is even a valid
    key on the payload.

    Args:
        decision: The terminal decision returned by
            :func:`evaluate_terminal`.

    Returns:
        A ``dict`` with the keys downstream
        :func:`forge.memory.session_outcome.write_session_outcome`
        consumes. Specifically:

        * ``outcome`` — the :class:`ModeCTerminal` enum's string
          value.
        * ``rationale`` — the canonical rationale string.
        * ``has_commits`` — the boolean flag (mirrors
          ``decision.has_commits``).
        * ``failure_reason`` — included only when set on the
          decision (FAILED outcomes).
        * ``pull_request_url`` — included only for PR_REVIEW
          outcomes when the field is non-None on the decision; the
          AC-009 invariant is enforced by **structural omission**
          rather than an in-band ``None``.
    """
    payload: dict[str, Any] = {
        "outcome": decision.outcome.value,
        "rationale": decision.rationale,
        "has_commits": decision.has_commits,
    }
    if decision.failure_reason is not None:
        payload["failure_reason"] = decision.failure_reason
    if (
        decision.outcome == ModeCTerminal.PR_REVIEW
        and decision.pull_request_url is not None
    ):
        payload["pull_request_url"] = decision.pull_request_url
    return payload


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _latest_review_index(history: Sequence[StageEntry]) -> int | None:
    """Return the index of the most recent ``/task-review`` entry.

    Returns ``None`` when the history contains no review row.
    """
    for idx in range(len(history) - 1, -1, -1):
        if history[idx].stage_class == StageClass.TASK_REVIEW:
            return idx
    return None
