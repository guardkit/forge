"""Tests for ``forge.pipeline.terminal_handlers.mode_c`` (TASK-MBC8-007).

Covers Mode C terminal-handler scenarios from Group D, G, L, N:

Group D — terminal routing:
  * Initial /task-review empty → CLEAN_REVIEW_NO_FIXES (AC-002).
  * Follow-up clean review with no commits → CLEAN_REVIEW_NO_COMMITS
    (AC-003).
  * Follow-up clean review with commits → PR_REVIEW (AC-004).
  * /task-review hard-stop → FAILED (AC-005).

Group G — per-fix-task artefact attribution (AC-007).
Group L — fix-task lineage on TASK_WORK entries (AC-008).
Group N — session outcome reflects mode terminal (AC-009).

The handler is async; tests use ``pytest.mark.asyncio`` via the
event-loop policy. Project pytest config does not mandate
``pytest-asyncio`` — to keep this test file self-contained and
zero-dependency, we drive coroutines through ``asyncio.run`` instead
of relying on the marker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_planner import StageEntry
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.terminal_handlers import mode_c as terminal_mode_c
from forge.pipeline.terminal_handlers.mode_c import (
    CommitProbeResult,
    ModeCTerminal,
    ModeCTerminalDecision,
    build_session_outcome_payload,
    build_task_work_attribution,
    evaluate_terminal,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build() -> Build:
    """Return a Mode C ``Build`` value object with status RUNNING."""
    return Build(
        build_id="build-FEAT-MBC8-20260427000000",
        status=BuildState.RUNNING,
        mode=BuildMode.MODE_C,
    )


def _review_entry(
    *,
    fix_tasks: tuple[str, ...] = (),
    status: str = "approved",
    hard_stop: bool = False,
    failure_reason: str | None = None,
) -> StageEntry:
    """Construct a ``StageEntry`` representing one ``/task-review``."""
    return StageEntry(
        stage_class=StageClass.TASK_REVIEW,
        status=status,
        fix_tasks=fix_tasks,
        fix_task_id=None,
        hard_stop=hard_stop,
        failure_reason=failure_reason,
    )


def _work_entry(*, fix_task_id: str, status: str = "approved") -> StageEntry:
    """Construct a ``StageEntry`` representing one ``/task-work``."""
    return StageEntry(
        stage_class=StageClass.TASK_WORK,
        status=status,
        fix_tasks=(),
        fix_task_id=fix_task_id,
        hard_stop=False,
    )


def _make_probe(
    *,
    count: int = 0,
    failed: bool = False,
    error: str | None = None,
) -> Callable[[Build], Awaitable[CommitProbeResult]]:
    """Build an async fake ``commit_probe`` returning a fixed result."""

    async def _probe(build: Build) -> CommitProbeResult:  # noqa: ARG001
        return CommitProbeResult(count=count, failed=failed, error=error)

    return _probe


def _run(coro: Awaitable[ModeCTerminalDecision]) -> ModeCTerminalDecision:
    """Drive a coroutine to completion via ``asyncio.run``."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-001 — module structure
# ---------------------------------------------------------------------------


class TestModuleStructure:
    """AC-001 — module exposes the required surface."""

    def test_module_dotted_path(self) -> None:
        assert terminal_mode_c.__name__ == "forge.pipeline.terminal_handlers.mode_c"

    def test_module_exports_required_symbols(self) -> None:
        required = {
            "ModeCTerminal",
            "ModeCTerminalDecision",
            "CommitProbe",
            "CommitProbeResult",
            "evaluate_terminal",
            "build_task_work_attribution",
            "build_session_outcome_payload",
        }
        assert required.issubset(set(terminal_mode_c.__all__))

    def test_terminal_enum_has_four_required_variants(self) -> None:
        members = {member.name for member in ModeCTerminal}
        assert members == {
            "CLEAN_REVIEW_NO_FIXES",
            "CLEAN_REVIEW_NO_COMMITS",
            "PR_REVIEW",
            "FAILED",
        }

    def test_terminal_enum_is_strenum(self) -> None:
        # StrEnum members compare equal to their string values, so
        # they serialise straight into JSON without coercion.
        assert ModeCTerminal.PR_REVIEW == "pr-review"
        assert ModeCTerminal.FAILED == "failed"


# ---------------------------------------------------------------------------
# AC-002 — CLEAN_REVIEW_NO_FIXES (initial empty review)
# ---------------------------------------------------------------------------


class TestCleanReviewNoFixes:
    """AC-002 — initial /task-review returns no fix tasks."""

    def test_initial_empty_review_returns_clean_review_no_fixes(self) -> None:
        history = [_review_entry(fix_tasks=())]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.outcome == ModeCTerminal.CLEAN_REVIEW_NO_FIXES

    def test_initial_empty_review_records_canonical_rationale(self) -> None:
        history = [_review_entry(fix_tasks=())]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.rationale == "mode-c-task-review-empty"

    def test_initial_empty_review_has_no_commits(self) -> None:
        history = [_review_entry(fix_tasks=())]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.has_commits is False

    def test_initial_empty_review_does_not_call_commit_probe(self) -> None:
        """Initial-empty path must NOT shell out — there is no
        worktree state worth probing."""
        history = [_review_entry(fix_tasks=())]
        called = {"count": 0}

        async def _probe(build: Build) -> CommitProbeResult:  # noqa: ARG001
            called["count"] += 1
            return CommitProbeResult(count=99)

        _ = _run(evaluate_terminal(_build(), history, commit_probe=_probe))
        assert called["count"] == 0


# ---------------------------------------------------------------------------
# AC-003 — CLEAN_REVIEW_NO_COMMITS (follow-up clean review, no commits)
# ---------------------------------------------------------------------------


class TestCleanReviewNoCommits:
    """AC-003 — follow-up review clean but worktree has zero commits."""

    def test_followup_clean_review_no_commits(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=0))
        )
        assert decision.outcome == ModeCTerminal.CLEAN_REVIEW_NO_COMMITS

    def test_followup_clean_review_no_commits_rationale(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=0))
        )
        assert decision.rationale == "mode-c-no-commits"

    def test_followup_clean_review_no_commits_has_commits_false(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=0))
        )
        assert decision.has_commits is False


# ---------------------------------------------------------------------------
# AC-004 — PR_REVIEW (commits present)
# ---------------------------------------------------------------------------


class TestPrReview:
    """AC-004 — at least one approved /task-work and worktree has commits."""

    def test_followup_clean_review_with_commits_routes_to_pr_review(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=3))
        )
        assert decision.outcome == ModeCTerminal.PR_REVIEW
        assert decision.has_commits is True

    def test_pr_review_rationale_records_commits_present(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=1))
        )
        assert decision.rationale == "mode-c-commits-present"

    def test_pr_review_carries_no_pull_request_url_from_handler(self) -> None:
        """The handler does not know the PR URL — that is set later by
        the PR-creation adapter. Field is structurally None here so
        AC-009's "no PR url on CLEAN_REVIEW_*" cannot leak through."""
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=2))
        )
        assert decision.pull_request_url is None


# ---------------------------------------------------------------------------
# AC-005 — FAILED paths
# ---------------------------------------------------------------------------


class TestFailedPaths:
    """AC-005 — hard-stop / rejected review / all work failed / probe error."""

    def test_review_hard_stop_returns_failed(self) -> None:
        history = [_review_entry(fix_tasks=("FIX-1",), status="failed", hard_stop=True)]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-task-review-hard-stop"
        assert decision.failure_reason is not None

    def test_review_rejected_without_hard_stop_returns_failed(self) -> None:
        history = [_review_entry(fix_tasks=("FIX-1",), status="rejected")]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-task-review-rejected"

    def test_a_failed_review_LEG_is_named_a_tooling_fault_not_a_rejection(
        self,
    ) -> None:
        """Leg-result honesty, 2026-08-03.

        ``rejected`` is a gate's verdict on a review that RAN. ``failed``
        is the leg falling over — the twin of the all-work-failed terminal
        — and calling it "rejected" sent a diagnoser hunting a review
        verdict that was never reached.
        """
        history = [_review_entry(fix_tasks=("FIX-1",), status="failed")]

        decision = _run(evaluate_terminal(_build(), history))

        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == terminal_mode_c.RATIONALE_FAILED_REVIEW_LEG
        assert decision.rationale != "mode-c-task-review-rejected"

    def test_the_failed_legs_own_words_ride_the_terminal(self) -> None:
        """A stop that cannot say what stopped it is half a stop."""
        banner = (
            "REFUSED (Phase 0, ad-hoc task creation): the review leg is "
            "id-form only and no task file exists for TASK-TST1FIX1"
        )
        history = [
            _review_entry(fix_tasks=(), status="failed", failure_reason=banner)
        ]

        decision = _run(evaluate_terminal(_build(), history))

        assert decision.failure_reason is not None
        assert banner in decision.failure_reason

    def test_a_rejection_with_no_recorded_reason_reads_as_it_always_did(
        self,
    ) -> None:
        history = [_review_entry(fix_tasks=("FIX-1",), status="rejected")]

        decision = _run(evaluate_terminal(_build(), history))

        assert decision.failure_reason == "/task-review rejected"

    def test_all_task_work_failed_returns_failed(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1", "FIX-2")),
            _work_entry(fix_task_id="FIX-1", status="failed"),
            _work_entry(fix_task_id="FIX-2", status="failed"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=0))
        )
        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-all-task-work-failed"
        assert decision.has_commits is False

    def test_commit_probe_failure_returns_failed_not_clean(self) -> None:
        """Per the TASK-MBC8-007 implementation note: a failed commit
        probe is FAILED, NOT silently demoted to CLEAN_REVIEW."""
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(
                _build(),
                history,
                commit_probe=_make_probe(
                    count=0, failed=True, error="git: not a worktree"
                ),
            )
        )
        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-commit-check-failed"
        assert decision.failure_reason == "git: not a worktree"

    def test_no_review_in_history_returns_failed_defensively(self) -> None:
        """Defensive: the handler is invoked on a build that never
        reached its initial /task-review."""
        history: list[StageEntry] = []
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.outcome == ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-no-task-review-recorded"


# ---------------------------------------------------------------------------
# AC-006 — has_commits flag drives planner routing
# ---------------------------------------------------------------------------


class TestHasCommitsFlag:
    """AC-006 — has_commits on the decision matches outcome semantics."""

    def test_pr_review_decision_has_commits_true(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=5))
        )
        assert decision.has_commits is True

    def test_clean_review_no_commits_decision_has_commits_false(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        decision = _run(
            evaluate_terminal(_build(), history, commit_probe=_make_probe(count=0))
        )
        assert decision.has_commits is False

    def test_clean_review_no_fixes_has_commits_false(self) -> None:
        history = [_review_entry(fix_tasks=())]
        decision = _run(evaluate_terminal(_build(), history))
        assert decision.has_commits is False


# ---------------------------------------------------------------------------
# AC-007 / AC-008 — per-fix-task attribution + lineage (Group G + L)
# ---------------------------------------------------------------------------


class TestTaskWorkAttribution:
    """AC-007 (Group G) + AC-008 (Group L) — attribution helpers."""

    def test_attribution_records_fix_task_id_and_review_entry_id(self) -> None:
        details = build_task_work_attribution(
            fix_task_id="FIX-42",
            originating_review_entry_id="entry-uuid-aaa",
            artefact_paths=("/build/x/file.py",),
        )
        assert details["fix_task_id"] == "FIX-42"
        assert details["originating_review_entry_id"] == "entry-uuid-aaa"

    def test_attribution_records_artefact_paths_for_this_fix_task(self) -> None:
        details = build_task_work_attribution(
            fix_task_id="FIX-1",
            originating_review_entry_id="entry-uuid-aaa",
            artefact_paths=(
                "/build/x/a.py",
                "/build/x/b.py",
            ),
        )
        assert details["artefact_paths"] == [
            "/build/x/a.py",
            "/build/x/b.py",
        ]

    def test_attribution_filters_artefacts_to_owning_fix_task(self) -> None:
        """Group G — no artefact path attributed to more than one fix task.

        When the supervisor passes an authoritative
        ``fix_task_artefact_index``, the helper rejects paths that
        belong to sibling fix tasks.
        """
        index = {
            "FIX-1": frozenset({"/build/x/a.py"}),
            "FIX-2": frozenset({"/build/x/b.py"}),
        }
        details = build_task_work_attribution(
            fix_task_id="FIX-1",
            originating_review_entry_id="entry-uuid-aaa",
            artefact_paths=(
                "/build/x/a.py",
                "/build/x/b.py",  # belongs to FIX-2 — must be filtered.
                "/build/x/c.py",  # not in any fix task — must be filtered.
            ),
            fix_task_artefact_index=index,
        )
        assert details["artefact_paths"] == ["/build/x/a.py"]

    def test_attribution_returns_list_not_tuple(self) -> None:
        """Pydantic v2 ``details`` columns expect plain lists for JSON."""
        details = build_task_work_attribution(
            fix_task_id="FIX-1",
            originating_review_entry_id="entry-uuid-aaa",
            artefact_paths=("/build/x/a.py",),
        )
        assert isinstance(details["artefact_paths"], list)


# ---------------------------------------------------------------------------
# AC-009 — session-outcome payload (Group N)
# ---------------------------------------------------------------------------


class TestSessionOutcomePayload:
    """AC-009 — CLEAN_REVIEW_* outcomes carry no pull_request_url and no
    PR-review gate decision."""

    def test_clean_review_no_fixes_payload_omits_pull_request_url(self) -> None:
        decision = ModeCTerminalDecision(
            outcome=ModeCTerminal.CLEAN_REVIEW_NO_FIXES,
            has_commits=False,
            rationale="mode-c-task-review-empty",
        )
        payload = build_session_outcome_payload(decision)
        assert "pull_request_url" not in payload
        assert payload["outcome"] == "clean-review-no-fixes"
        assert payload["has_commits"] is False

    def test_clean_review_no_commits_payload_omits_pull_request_url(
        self,
    ) -> None:
        decision = ModeCTerminalDecision(
            outcome=ModeCTerminal.CLEAN_REVIEW_NO_COMMITS,
            has_commits=False,
            rationale="mode-c-no-commits",
        )
        payload = build_session_outcome_payload(decision)
        assert "pull_request_url" not in payload
        assert payload["outcome"] == "clean-review-no-commits"

    def test_pr_review_payload_includes_pull_request_url_when_set(self) -> None:
        decision = ModeCTerminalDecision(
            outcome=ModeCTerminal.PR_REVIEW,
            has_commits=True,
            rationale="mode-c-commits-present",
            pull_request_url="https://github.com/x/y/pull/42",
        )
        payload = build_session_outcome_payload(decision)
        assert payload["pull_request_url"] == "https://github.com/x/y/pull/42"

    def test_failed_payload_includes_failure_reason(self) -> None:
        decision = ModeCTerminalDecision(
            outcome=ModeCTerminal.FAILED,
            has_commits=False,
            rationale="mode-c-task-review-hard-stop",
            failure_reason="reviewer hard-stopped: stack mismatch",
        )
        payload = build_session_outcome_payload(decision)
        assert payload["failure_reason"] == "reviewer hard-stopped: stack mismatch"
        # Failed outcomes must NOT carry pull_request_url either.
        assert "pull_request_url" not in payload


# ---------------------------------------------------------------------------
# AC-005 — defensive guard: commit_probe required for the no-commits branch
# ---------------------------------------------------------------------------


class TestProbeWiringGuard:
    """Defensive: the handler refuses to silently route without a probe."""

    def test_missing_probe_raises_for_no_commits_branch(self) -> None:
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1", status="approved"),
            _review_entry(fix_tasks=()),
        ]
        with pytest.raises(RuntimeError, match="commit_probe is required"):
            _run(evaluate_terminal(_build(), history, commit_probe=None))

    def test_missing_probe_ok_for_no_fixes_branch(self) -> None:
        """The initial-empty branch never reaches the probe call site,
        so omitting the probe must NOT raise."""
        history = [_review_entry(fix_tasks=())]
        decision = _run(evaluate_terminal(_build(), history, commit_probe=None))
        assert decision.outcome == ModeCTerminal.CLEAN_REVIEW_NO_FIXES

    def test_missing_probe_ok_for_failed_review_branch(self) -> None:
        history = [
            _review_entry(fix_tasks=(), status="failed", hard_stop=True),
        ]
        decision = _run(evaluate_terminal(_build(), history, commit_probe=None))
        assert decision.outcome == ModeCTerminal.FAILED
