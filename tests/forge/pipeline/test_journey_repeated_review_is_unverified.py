"""A repeated review after approved work is unverified, not no progress.

Attempt eight, 2026-09-08 (build ``build-FEAT-39F6-20260908110853``). The
first review named three things — a migration file, a router file, a test
file. Five work legs then ran inside the sandbox and every one of them was
approved by the coach and the oracle, with commits on the fix branch that
fixed exactly those three things: the column was made timezone-aware, the
delete endpoint caught database errors and answered 503, and a Postgres
test for the soft delete was written. The follow-up review then reported
the same three findings again, word for word — same files, same lines, same
severities — off the tree that no longer had them.

The driver's review-cycle rule read that as "nothing changed" and stopped
the journey one step short of its merge-ready checks, with every fix in
place. The rule was right about the words and wrong about the meaning: a
review that repeats itself after approved work has not proved no progress,
it has failed to verify.

What this file pins:

* the shared reading of "the review said the same thing again", which the
  turn loop and the planner both use, so they cannot disagree;
* the loop does NOT stop when the cycle's work was approved — it writes the
  repeated findings down as unverified, on the turn and in the receipts,
  and carries on;
* the loop DOES stop, exactly as before, when it cannot get a durable "yes"
  — no approved work, a supervisor that cannot answer, a reader that
  raises;
* the planner's next stage after such a review is the merge-ready
  checkpoint, with a rationale a person can read;
* the whole thing driven end to end over a real SQLite ledger: two reviews
  that say the same thing, approved work between them, and a merge card at
  the end instead of a stopped journey.

The suite drives the coroutines through ``asyncio.run`` — this package's
conductor tests do not declare ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.guardkit.parser import parse_guardkit_output
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    build_conductor_driver_deps_factory,
    build_conductor_supervisor_factory,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.conductor_driver import (
    UNVERIFIED_AFTER_APPROVED_WORK,
    ConductorDriverDeps,
    ConductorRunOutcome,
    drive_fix_journey,
)
from forge.pipeline.dispatchers.subprocess import (
    StageDispatchResult,
    StageDispatchStatus,
)
from forge.pipeline.finding_anchors import repeated_anchors
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    StageEntry,
    approved_work_between_the_last_two_reviews,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "build-FEAT-39F6-20260908110853"


# ---------------------------------------------------------------------------
# Attempt eight's three findings, as the review reported them twice
# ---------------------------------------------------------------------------

#: The first review's three findings. The follow-up repeated all three.
FINDINGS = [
    {
        "pattern": "UNGROUNDED",
        "file": "migrations/0007_soft_delete.py",
        "line": 22,
        "severity": "critical",
        "evidence": "the deleted_at column is naive",
    },
    {
        "pattern": "PHANTOM",
        "file": "src/api/routes/items.py",
        "line": 140,
        "severity": "medium",
        "evidence": "the delete endpoint does not catch SQLAlchemyError",
    },
    {
        "pattern": "MISSING_TEST",
        "file": "tests/test_soft_delete.py",
        "line": 1,
        "severity": "medium",
        "evidence": "no Postgres integration test for the soft delete",
    },
]

#: The same three defects, retitled and with the lines drifted — which is
#: what the seat actually produced, and what the anchor is designed to see
#: through.
FINDINGS_AGAIN = [
    {
        "pattern": "SCOPE_CREEP",
        "file": "migrations/0007_soft_delete.py",
        "line": None,
        "severity": "critical",
        "evidence": "the same naive column, retitled",
    },
    {
        "pattern": "PHANTOM",
        "file": "src/api/routes/items.py:151",
        "severity": "medium",
        "evidence": "the delete endpoint still looks unguarded",
    },
    {
        "pattern": "MISSING_TEST",
        "file": "tests/test_soft_delete.py",
        "line": 4,
        "severity": "medium",
        "evidence": "still no Postgres integration test",
    },
]

ANCHORS = (
    "migrations/0007_soft_delete.py|critical",
    "src/api/routes/items.py|medium",
    "tests/test_soft_delete.py|medium",
)


# ---------------------------------------------------------------------------
# The shared reading
# ---------------------------------------------------------------------------


class TestOneReadingOfRepeatedItself:
    """Both readers ask the same function, so they cannot drift apart."""

    def test_every_anchor_named_again_is_a_repeat(self) -> None:
        assert repeated_anchors(ANCHORS, ANCHORS) == tuple(sorted(ANCHORS))

    def test_new_findings_on_top_are_still_a_repeat(self) -> None:
        assert repeated_anchors(
            ANCHORS, ANCHORS + ("src/db/session.py|high",)
        ) == tuple(sorted(ANCHORS))

    def test_one_finding_resolved_is_not_a_repeat(self) -> None:
        assert repeated_anchors(ANCHORS, ANCHORS[:2]) == ()

    def test_nothing_recorded_before_repeats_nothing(self) -> None:
        assert repeated_anchors(None, ANCHORS) == ()
        assert repeated_anchors((), ANCHORS) == ()

    def test_a_clean_review_after_findings_is_not_a_repeat(self) -> None:
        assert repeated_anchors(ANCHORS, ()) == ()

    def test_the_list_is_sorted_so_the_sentence_is_stable(self) -> None:
        jumbled = tuple(reversed(ANCHORS))
        assert repeated_anchors(jumbled, jumbled) == repeated_anchors(
            ANCHORS, ANCHORS
        )


# ---------------------------------------------------------------------------
# Did the cycle produce anything? — read off the durable rows
# ---------------------------------------------------------------------------


def _review(anchors: tuple[str, ...] | None, *, fix_tasks: tuple[str, ...]):
    return StageEntry(
        stage_class=StageClass.TASK_REVIEW,
        status="approved",
        fix_tasks=fix_tasks,
        finding_anchors=anchors,
    )


def _work(fix_task_id: str, status: str = "approved"):
    return StageEntry(
        stage_class=StageClass.TASK_WORK,
        status=status,
        fix_task_id=fix_task_id,
    )


class TestApprovedWorkBetweenTheLastTwoReviews:
    def test_an_approved_leg_between_them_is_a_yes(self) -> None:
        history = [
            _review(ANCHORS, fix_tasks=("TASK-A", "TASK-B")),
            _work("TASK-A"),
            _work("TASK-B", status="failed"),
            _review(ANCHORS, fix_tasks=("TASK-C",)),
        ]
        assert approved_work_between_the_last_two_reviews(history) is True

    def test_nothing_approved_between_them_is_a_no(self) -> None:
        history = [
            _review(ANCHORS, fix_tasks=("TASK-A",)),
            _work("TASK-A", status="rejected"),
            _review(ANCHORS, fix_tasks=("TASK-A",)),
        ]
        assert approved_work_between_the_last_two_reviews(history) is False

    def test_approved_work_before_the_earlier_review_does_not_count(
        self,
    ) -> None:
        """The window is the cycle, not the whole journey."""
        history = [
            _review(ANCHORS, fix_tasks=("TASK-A",)),
            _work("TASK-A"),
            _review(ANCHORS, fix_tasks=("TASK-B",)),
            _work("TASK-B", status="failed"),
            _review(ANCHORS, fix_tasks=("TASK-B",)),
        ]
        assert approved_work_between_the_last_two_reviews(history) is False

    def test_one_review_only_is_a_no(self) -> None:
        history = [_review(ANCHORS, fix_tasks=("TASK-A",)), _work("TASK-A")]
        assert approved_work_between_the_last_two_reviews(history) is False


# ---------------------------------------------------------------------------
# The planner's next stage after a repeated review
# ---------------------------------------------------------------------------


def _build():
    from forge.lifecycle.persistence import Build
    from forge.lifecycle.state_machine import BuildState
    from forge.pipeline.mode_chains_data import BuildMode

    return Build(build_id=BUILD_ID, status=BuildState.RUNNING, mode=BuildMode.MODE_C)


class TestThePlannerGoesToTheChecks:
    """(a) — the same route the review-cycle cap takes."""

    def test_a_repeat_after_approved_work_goes_to_the_merge_ready_checks(
        self,
    ) -> None:
        history = [
            _review(ANCHORS, fix_tasks=("TASK-FIX1", "TASK-FIX2")),
            _work("TASK-FIX1"),
            _work("TASK-FIX2"),
            _review(ANCHORS, fix_tasks=("TASK-FIX3",)),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.PULL_REQUEST_REVIEW
        assert plan.next_fix_task is None
        assert plan.terminal is None
        assert plan.wait is None

    def test_the_rationale_says_what_happened_in_plain_words(self) -> None:
        history = [
            _review(ANCHORS, fix_tasks=("TASK-FIX1",)),
            _work("TASK-FIX1"),
            _review(ANCHORS, fix_tasks=("TASK-FIX2",)),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert "the review repeated 3 findings" in plan.rationale
        assert (
            "the cycle's approved work addressed — going to the merge-ready "
            "checks, which decide" in plan.rationale
        )
        for anchor in ANCHORS:
            assert anchor in plan.rationale

    def test_no_approved_work_still_fans_the_new_cycle_out(self) -> None:
        """Nothing was fixed, so the planner behaves exactly as before."""
        history = [
            _review(ANCHORS, fix_tasks=("TASK-FIX1",)),
            _work("TASK-FIX1", status="failed"),
            _review(ANCHORS, fix_tasks=("TASK-FIX2",)),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "TASK-FIX2"

    def test_a_review_that_found_something_else_fans_out_as_before(self) -> None:
        history = [
            _review(ANCHORS, fix_tasks=("TASK-FIX1",)),
            _work("TASK-FIX1"),
            _review(("src/db/session.py|high",), fix_tasks=("TASK-FIX2",)),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_WORK

    def test_a_row_that_recorded_no_anchors_repeats_nothing(self) -> None:
        """Every review row written before the anchors existed."""
        history = [
            _review(None, fix_tasks=("TASK-FIX1",)),
            _work("TASK-FIX1"),
            _review(None, fix_tasks=("TASK-FIX2",)),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_WORK

    def test_the_first_review_of_a_journey_fans_out(self) -> None:
        history = [_review(ANCHORS, fix_tasks=("TASK-FIX1",))]

        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_WORK


# ---------------------------------------------------------------------------
# The turn loop: the stop that no longer fires, and the one that still does
# ---------------------------------------------------------------------------


@dataclass
class FakeSupervisor:
    """Scripted turn reports, with the one read the loop makes of it.

    ``approved_work`` is the supervisor's answer to "did anything in this
    cycle end approved?" — ``None`` stands for a supervisor that has no
    such reading at all, which is every double written before this change.
    """

    script: list[Any] = field(default_factory=list)
    approved_work: bool | None = None
    asked: list[str] = field(default_factory=list)
    raises: Exception | None = None
    budget_pause: Any | None = None

    async def next_turn(self, build_id: str) -> Any:
        if not self.script:
            return TurnReport(outcome=TurnOutcome.TERMINAL, build_id=build_id)
        return self.script.pop(0)

    def cycle_had_approved_work(self, build_id: str) -> bool | None:
        self.asked.append(build_id)
        if self.raises is not None:
            raise self.raises
        return self.approved_work


@dataclass
class SupervisorWithNoSuchReading:
    """A supervisor that has never heard the question — every older boot."""

    script: list[Any] = field(default_factory=list)
    budget_pause: Any | None = None

    async def next_turn(self, build_id: str) -> Any:
        if not self.script:
            return TurnReport(outcome=TurnOutcome.TERMINAL, build_id=build_id)
        return self.script.pop(0)


def _review_dispatch(findings: list[dict[str, Any]]) -> StageDispatchResult:
    """Real leg text → the real parser → a real dispatch result."""
    stdout = "\n".join(
        [
            "## Artefacts",
            "- /w/tasks/TASK-FEAT39F6FIX1.yaml",
            "",
            "## Detection Findings",
            "```json",
            json.dumps(findings, indent=2),
            "```",
        ]
    )
    parsed = parse_guardkit_output(
        subcommand="task-review",
        stdout=stdout,
        stderr="",
        exit_code=0,
        duration_secs=1.0,
    )
    return StageDispatchResult(
        status=StageDispatchStatus.SUCCESS,
        stage=StageClass.TASK_REVIEW,
        build_id=BUILD_ID,
        feature_id=None,
        correlation_id="corr-8",
        artefact_paths=tuple(parsed.artefacts),
        rationale="task-review completed",
        exit_code=0,
        duration_secs=1.0,
        subcommand="task-review",
        detection_findings=tuple(parsed.detection_findings or ()),
        detection_findings_reported=parsed.detection_findings is not None,
    )


def _review_turn(findings: list[dict[str, Any]], *, rationale: str) -> TurnReport:
    return TurnReport(
        outcome=TurnOutcome.DISPATCHED,
        build_id=BUILD_ID,
        chosen_stage=StageClass.TASK_REVIEW,
        rationale=rationale,
        dispatch_result=_review_dispatch(findings),
    )


def _work_turn(fix_task_id: str) -> TurnReport:
    return TurnReport(
        outcome=TurnOutcome.DISPATCHED,
        build_id=BUILD_ID,
        chosen_stage=StageClass.TASK_WORK,
        rationale=f"dispatch /task-work for fix task {fix_task_id!r}",
        dispatch_result=StageDispatchResult(
            status=StageDispatchStatus.SUCCESS,
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            feature_id=None,
            correlation_id="corr-8",
            artefact_paths=(),
            rationale="task-work completed",
            exit_code=0,
            duration_secs=1.0,
            subcommand="task-work",
        ),
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def _attempt_eight_script() -> list[Any]:
    """The five approved legs and the review that said it all again."""
    return [
        _review_turn(FINDINGS, rationale="the first review named the cause"),
        _work_turn("TASK-FEAT39F6FIX1"),
        _work_turn("TASK-FEAT39F6FIX2"),
        _work_turn("TASK-FEAT39F6FIX3"),
        _work_turn("TASK-FEAT39F6FIX4"),
        _work_turn("TASK-FEAT39F6FIX5"),
        _review_turn(FINDINGS_AGAIN, rationale="the follow-up review"),
        # Reached only if the loop carried on, which is the point.
        TurnReport(
            outcome=TurnOutcome.TERMINAL,
            build_id=BUILD_ID,
            rationale="the merge-ready checks ran",
        ),
    ]


def _deps(supervisor: FakeSupervisor, **overrides: Any) -> ConductorDriverDeps:
    base: dict[str, Any] = {"supervisor": supervisor, "sleep": _no_sleep}
    base.update(overrides)
    return ConductorDriverDeps(**base)


class TestTheLoopCarriesARepeatedReviewPast:
    """(a) — attempt eight's shape, with the cycle's work approved."""

    def test_the_journey_is_not_stopped(self) -> None:
        supervisor = FakeSupervisor(
            script=_attempt_eight_script(), approved_work=True
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 8
        assert supervisor.asked == [BUILD_ID]

    def test_the_repeated_findings_are_recorded_as_unverified(self) -> None:
        supervisor = FakeSupervisor(
            script=_attempt_eight_script(), approved_work=True
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.unverified_findings == ANCHORS

    def test_the_sentence_reaches_the_receipts(self) -> None:
        """The exporter writes the turn's rationale into its receipt folder."""
        rationales: list[str] = []

        def export(*, build_id: str, report: Any) -> str:
            rationales.append(getattr(report, "rationale", ""))
            return f"{len(rationales):03d}-stage"

        supervisor = FakeSupervisor(
            script=_attempt_eight_script(), approved_work=True
        )

        asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, export_stage_receipts=export)
            )
        )

        noted = [text for text in rationales if UNVERIFIED_AFTER_APPROVED_WORK in text]
        assert len(noted) == 1, "exactly the repeating review's receipt"
        assert "the follow-up review" in noted[0], "the turn's own words survive"
        for anchor in ANCHORS:
            assert anchor in noted[0]

    def test_no_failure_pack_is_written(self) -> None:
        packs: list[dict[str, Any]] = []

        def write_pack(**kwargs: Any) -> str:  # pragma: no cover - must not run
            packs.append(kwargs)
            return "/packs/fix.json"

        supervisor = FakeSupervisor(
            script=_attempt_eight_script(), approved_work=True
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, write_failure_pack=write_pack)
            )
        )

        assert packs == []
        assert report.failure_pack is None


class TestTheStopStillFiresWhenTheCycleProducedNothing:
    """The other half — and the only answer that lets a journey through."""

    def test_no_approved_work_stops_at_the_second_review(self) -> None:
        supervisor = FakeSupervisor(
            script=_attempt_eight_script(), approved_work=False
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 7, "the stop fires ON the repeating review"
        assert report.unverified_findings == ()

    def test_a_supervisor_that_cannot_be_asked_stops(self) -> None:
        """Every double written before this change, and every older boot."""
        supervisor = SupervisorWithNoSuchReading(script=_attempt_eight_script())
        assert not hasattr(supervisor, "cycle_had_approved_work")

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 7

    def test_a_reading_that_raises_stops(self) -> None:
        supervisor = FakeSupervisor(
            script=_attempt_eight_script(),
            raises=RuntimeError("the ledger is locked"),
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 7

    def test_a_review_that_went_silent_is_still_a_stop(self) -> None:
        """Fail-closed is unchanged: a missing block repeats nothing.

        A leg that stops stating what it found cannot show a single
        previously-named finding resolved, and approved work does not make
        its silence readable.
        """
        silent = TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            chosen_stage=StageClass.TASK_REVIEW,
            rationale="the follow-up review emitted no findings block",
            dispatch_result=dataclasses.replace(
                _review_dispatch(FINDINGS),
                detection_findings=(),
                detection_findings_reported=False,
            ),
        )
        supervisor = FakeSupervisor(
            script=[
                _review_turn(FINDINGS, rationale="the first review"),
                _work_turn("TASK-FEAT39F6FIX1"),
                silent,
            ],
            approved_work=True,
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert "no readable findings block" in report.rationale
        assert supervisor.asked == [], "the question is not even asked"


# ---------------------------------------------------------------------------
# End to end, over a real ledger
# ---------------------------------------------------------------------------


class FakeGuardKitThatRepeatsItself:
    """The seat of attempt eight: fixes the work, then repeats the review.

    Both reviews report the SAME three findings; the work legs all succeed,
    which is what an approved fix-journey leg is (``_serve_deps_stage_log``:
    "a stage that DISPATCHED AND SUCCEEDED"). Each review mints its own
    fix-task ids, exactly as the real seat does.
    """

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.calls: list[dict[str, Any]] = []
        self.reviews = 0

    def fix_tasks(self, cycle: int) -> tuple[str, ...]:
        return (f"TASK-RPT{cycle:03d}-001", f"TASK-RPT{cycle:03d}-002")

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        subcommand = kwargs["subcommand"]
        if subcommand == "task-review":
            self.reviews += 1
            return GuardKitResult(
                status="success",
                subcommand=subcommand,
                exit_code=0,
                stdout_tail="",
                stderr="",
                duration_secs=1.0,
                artefacts=[
                    str(self.worktree / "tasks" / f"{t}.yaml")
                    for t in self.fix_tasks(self.reviews)
                ],
                detection_findings=list(
                    FINDINGS if self.reviews == 1 else FINDINGS_AGAIN
                ),
                warnings=[],
            )
        return GuardKitResult(
            status="success",
            subcommand=subcommand,
            exit_code=0,
            stdout_tail="",
            stderr="",
            duration_secs=2.0,
            artefacts=[str(self.worktree / "src" / "fixed.py")],
            warnings=[],
        )

    def subcommands(self) -> list[str]:
        return [c["subcommand"] for c in self.calls]


class FakeCardDelivery:
    def __init__(self, verdict: str = "RESUMED") -> None:
        self.verdict = verdict
        self.publishes: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.publishes.append(kwargs)
        return self.verdict


def _config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        }
    )


@pytest.fixture
def rig(tmp_path: Path):
    """The journey's real machinery over a real SQLite file."""
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    (worktree / "src").mkdir(parents=True)
    receipts_root = tmp_path / "receipts"
    receipts_root.mkdir()

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    started = datetime.now(timezone.utc).isoformat()
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id, profile) VALUES (?, "
        "'FEAT-39F6', 'r', 'repair/FEAT-39F6', ?, 'RUNNING', 'cli', "
        "'corr-rpt', ?, ?, ?, 'mode-c', 'TASK-FEAT39F6FIX1', 'fix-journey')",
        (
            BUILD_ID,
            str(worktree / "tasks" / "fix-task.yaml"),
            started,
            started,
            str(worktree),
        ),
    )
    cx.commit()
    pool = SqliteLifecyclePersistence(connection=cx)

    class _Rig:
        def __init__(self) -> None:
            self.pool = pool
            self.cx = cx
            self.worktree = worktree
            self.guardkit = FakeGuardKitThatRepeatsItself(worktree)

        def run(self, delivery: FakeCardDelivery):
            from forge.cli._serve_deps_forward_context import (
                ForgeConfigWorktreeAllowlist,
                build_stage_log_reader,
            )
            from forge.pipeline.forward_context_builder import ForwardContextBuilder

            config = _config()
            allowlist = ForgeConfigWorktreeAllowlist(allowed_roots=(str(tmp_path),))
            supervisor_factory = build_conductor_supervisor_factory(
                pool=pool,
                config=config,
                forward_context_builder=ForwardContextBuilder(
                    build_stage_log_reader(pool), allowlist
                ),
                worktree_allowlist=allowlist,
                read_allowlist=[tmp_path],
                subprocess_runner=self.guardkit,
                publish_card=delivery,
                gates_green_reader=lambda **_: True,
                receipts_root=receipts_root,
            )
            deps_factory = build_conductor_driver_deps_factory(
                pool=pool, config=config, receipts_root=receipts_root
            )
            supervisor = supervisor_factory(BUILD_ID)
            deps = dataclasses.replace(
                deps_factory(BUILD_ID, supervisor), max_turns=20
            )
            return asyncio.run(drive_fix_journey(BUILD_ID, deps))

    return _Rig()


class TestDrivenEndToEnd:
    """Attempt eight, driven: the card at the end instead of a stopped row."""

    @pytest.fixture(autouse=True)
    def _drive(self, rig) -> None:
        self.rig = rig
        self.delivery = FakeCardDelivery()
        self.report = rig.run(self.delivery)

    def test_the_journey_reaches_the_merge_ready_checks(self) -> None:
        assert self.rig.guardkit.subcommands() == [
            "task-review",
            "task-work",
            "task-work",
            "task-review",
        ], "no third cycle of work — the checks decide instead"

    def test_the_card_is_published(self) -> None:
        assert len(self.delivery.publishes) == 1
        assert self.report.outcome is ConductorRunOutcome.DELIVERED

    def test_the_repeated_findings_are_named_as_unverified(self) -> None:
        assert self.report.unverified_findings == ANCHORS

    def test_the_record_says_why_it_went_to_the_checks(self) -> None:
        """The planner's own words, on the turn row a person reads."""
        rows = self.rig.cx.execute(
            "SELECT details_json FROM stage_log WHERE build_id = ? "
            "ORDER BY id",
            (BUILD_ID,),
        ).fetchall()
        text = " ".join(str(row["details_json"] or "") for row in rows)
        assert "the review repeated 3 findings" in text
        assert "going to the merge-ready checks, which decide" in text
