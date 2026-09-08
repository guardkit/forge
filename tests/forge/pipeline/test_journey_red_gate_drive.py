"""THE RED GATE, DRIVEN — a journey the checks send back, and one they end.

The sibling of ``test_fix_journey_cap_reaches_the_card``: the same real
pieces — the real turn loop, the real Supervisor, the real Mode C planner,
the real ``stage_log`` projection, the real budget guard, a real SQLite
database, the real merge-ready checkpoint — with fakes at exactly three
edges (the GuardKit subprocess, the gate set, and the merge card's
delivery).

What it reproduces is attempt fifteen, 2026-09-08. The fix cycle ran, the
merge-ready checkpoint ran the repository's declared suite on the journey
worktree, and two tests failed. The checkpoint looped back onto itself: the
same suite four times in two minutes, then the nothing-changed rule, then
FAILED. Nothing had gone back into the fix cycle, because nothing durable
said the checks had ever run.

Two drives are pinned:

* **a cycle left** — the red checkpoint's verdict lands in the history, the
  next stage is a review handed the gate's evidence, its fix task is worked,
  and the second checkpoint (green this time) publishes the one card. Three
  review cycles under a cap of three: the initial review, the follow-up, and
  the gate-driven one.
* **no cycle left** — the same red checkpoint under a cap of two ends the
  journey: the build row FAILED with the failing tests in its reason, a
  failure pack written, and the queue's message released exactly once.

Network-free: no NATS client, no broker URL, no port.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    build_conductor_driver_deps_factory,
    build_conductor_supervisor_factory,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.conductor_driver import ConductorRunOutcome, drive_fix_journey
from forge.pipeline.fix_task_context_builder import (
    GATE_FAILURES_DOCUMENT_NAME,
    GATE_FAILURES_INSTRUCTION,
)
from forge.pipeline.merge_ready_checkpoint import GatesReport, GateStatus
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-RED-20260908172338"
SOURCE_BUILD_ID = "build-FEAT-RED-20260907055525"
TASK_ID = "TASK-RED001"
FEATURE_ID = "FEAT-RED"

#: The two tests attempt fifteen's declared suite really failed on.
FAILING = (
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_success",
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_selective",
)

#: Well above the turns this journey needs, so nothing here is an artefact of
#: running out of them.
TURN_CEILING = 30


# ---------------------------------------------------------------------------
# The fakes at the three edges
# ---------------------------------------------------------------------------


class FakeGuardKit:
    """A review that repeats itself over approved work, then fixes what it is told.

    The first two reviews report the SAME finding anchors with an approved
    work leg between them — attempt eight's shape, which K1 rules is
    "unverified by the review seat, not a journey standing still" and sends
    to the merge-ready checks. That is how this journey reaches the
    checkpoint on its second cycle rather than its third, which is what
    leaves a cycle for the gate-driven review to spend.

    Every later review reports a fresh finding and mints a fresh fix task.
    """

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.calls: list[dict[str, Any]] = []
        self.reviews = 0

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        subcommand = kwargs["subcommand"]
        if subcommand != "task-review":
            return GuardKitResult(
                status="success",
                subcommand=subcommand,
                exit_code=0,
                stdout_tail="",
                stderr="",
                duration_secs=2.0,
                artefacts=[str(self.worktree / "src" / "red" / "fixed.py")],
                warnings=[],
            )

        self.reviews += 1
        # Reviews one and two say the same thing; three onwards are fresh.
        anchor_cycle = 1 if self.reviews <= 2 else self.reviews
        fix_task = f"TASK-RED{self.reviews:03d}-001"
        return GuardKitResult(
            status="success",
            subcommand=subcommand,
            exit_code=0,
            stdout_tail="",
            stderr="",
            duration_secs=1.0,
            artefacts=[str(self.worktree / "tasks" / f"{fix_task}.yaml")],
            detection_findings=[
                {
                    "file": f"src/red/cycle{anchor_cycle:03d}.py",
                    "severity": "high",
                    "summary": "the delete path filters what it must not",
                }
            ],
            warnings=[],
        )

    def subcommands(self) -> list[str]:
        return [c["subcommand"] for c in self.calls]

    def review_context_paths(self) -> list[list[str]]:
        """The context paths every review leg was actually dispatched with.

        This is the leg's own view: what the dispatcher put on its argv after
        the worktree allowlist gated it. Asserting here rather than on the
        builder's return is the point — a document the leg never receives is
        not a document.
        """
        return [
            [str(p) for p in (c.get("extra_context_paths") or ())]
            for c in self.calls
            if c["subcommand"] == "task-review"
        ]


class FakeGateSet:
    """The declared suite: red the first time it is asked, then green.

    Shaped like the production reader's answer, including the sentence that
    carries the command it ran and the code it exited with
    (``DeclaredTestDetail``), so the row this journey writes is the row
    production writes.
    """

    def __init__(self, *, reds: int = 1) -> None:
        self.reds = reds
        self.reads = 0

    def __call__(self, **_kwargs: Any) -> GatesReport:
        self.reads += 1
        if self.reads > self.reds:
            return GatesReport(
                status=GateStatus.GREEN,
                detail="`uv run pytest -q` exited 0 in the journey worktree",
            )

        class _Detail(str):
            command = "uv run pytest -q"
            exit_code = 1
            failing_cases = FAILING
            evidence = (
                "the lines the test command wrote that begin with FAILED, "
                "FAIL, ERROR or not ok:\n"
                f"FAILED {FAILING[0]} - assert 404 == 200\n"
                f"FAILED {FAILING[1]} - assert 404 == 200"
            )

        detail = _Detail("`uv run pytest -q` exited 1 — 2 failed: …")
        return GatesReport(
            status=GateStatus.RED,
            failed_gates=(f"declared toolchain test — 2 failed: {FAILING[0]}",),
            detail=detail,
            evidence=detail.evidence,
        )


class FakeCardDelivery:
    def __init__(self) -> None:
        self.publishes: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.publishes.append(kwargs)
        return "RESUMED"


class CountingRelease:
    """The pipeline consumer's message, released once and only once."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _build_id: str) -> None:
        self.calls += 1


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


def _config(max_review_cycles: int) -> ForgeConfig:
    """The estate's config with the go-live review-cycle cap on the profile."""
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
            "budget": {
                "default_profile": "attended",
                "profiles": {
                    "attended": {},
                    "fix-journey": {
                        "max_review_cycles": max_review_cycles,
                        "max_build_wallclock_seconds": 28800,
                    },
                },
            },
        }
    )


def _bank_a_failure_pack(receipts_root: Path) -> None:
    pack = receipts_root / SOURCE_BUILD_ID
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "failure-manifest.json").write_text(
        json.dumps(
            {
                "build_id": SOURCE_BUILD_ID,
                "feature_id": FEATURE_ID,
                "correlation_id": "corr-red-1",
                "reason": "gates red: the delete path returns a 404",
                "branch": f"feat/{FEATURE_ID}",
                "failed_at": "2026-09-07T05:55:25+00:00",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def rig(tmp_path: Path):
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    (worktree / "src" / "red").mkdir(parents=True)
    receipts_root = tmp_path / "receipts"
    _bank_a_failure_pack(receipts_root)

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    started = datetime.now(timezone.utc).isoformat()
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id, profile) VALUES (?, ?, "
        "'api_test', 'fix/FEAT-RED', ?, 'RUNNING', 'cli', 'corr-red-2', ?, ?, "
        "?, 'mode-c', ?, 'fix-journey')",
        (
            BUILD_ID,
            FEATURE_ID,
            str(worktree / "tasks" / "fix-task.yaml"),
            started,
            started,
            str(worktree),
            TASK_ID,
        ),
    )
    cx.commit()
    pool = SqliteLifecyclePersistence(connection=cx)

    class _Rig:
        def __init__(self) -> None:
            self.pool = pool
            self.worktree = worktree
            self.receipts_root = receipts_root
            self.guardkit = FakeGuardKit(worktree)
            self.gates = FakeGateSet()
            self.delivery = FakeCardDelivery()
            self.release = CountingRelease()

        def run(self, *, max_review_cycles: int):
            from forge.cli._serve_deps_forward_context import (
                ForgeConfigWorktreeAllowlist,
                build_stage_log_reader,
            )
            from forge.pipeline.forward_context_builder import ForwardContextBuilder

            config = _config(max_review_cycles)
            allowlist = ForgeConfigWorktreeAllowlist(allowed_roots=(str(tmp_path),))
            forward_context_builder = ForwardContextBuilder(
                build_stage_log_reader(pool), allowlist
            )
            supervisor_factory = build_conductor_supervisor_factory(
                pool=pool,
                config=config,
                forward_context_builder=forward_context_builder,
                worktree_allowlist=allowlist,
                read_allowlist=[tmp_path],
                subprocess_runner=self.guardkit,
                publish_card=self.delivery,
                gates_green_reader=self.gates,
                receipts_root=receipts_root,
                failure_pack_source_reader=lambda _bid: SOURCE_BUILD_ID,
            )
            deps_factory = build_conductor_driver_deps_factory(
                pool=pool,
                config=config,
                receipts_root=receipts_root,
                source_build_id_reader=lambda _bid: SOURCE_BUILD_ID,
            )
            supervisor = supervisor_factory(BUILD_ID)
            deps = dataclasses.replace(
                deps_factory(BUILD_ID, supervisor),
                max_turns=TURN_CEILING,
                release_queue_message=self.release,
            )
            return asyncio.run(drive_fix_journey(BUILD_ID, deps))

        def checkpoint_rows(self) -> list[Any]:
            return [
                row
                for row in pool.read_stages(BUILD_ID)
                if row.stage_label == StageClass.PULL_REQUEST_REVIEW.value
            ]

        def journey_stages(self) -> list[str]:
            """The journey's own stage rows, in order — the history the planner reads."""
            wanted = {
                StageClass.TASK_REVIEW.value,
                StageClass.TASK_WORK.value,
                StageClass.PULL_REQUEST_REVIEW.value,
            }
            return [
                row.stage_label
                for row in pool.read_stages(BUILD_ID)
                if row.stage_label in wanted
            ]

    return _Rig()


# ---------------------------------------------------------------------------
# A cycle left: the failures go back into the fix cycle
# ---------------------------------------------------------------------------


class TestTheRedChecksSendTheJourneyBack:
    """Cap of three: the initial review, the follow-up, and the gate-driven one."""

    def test_the_red_checks_are_followed_by_a_review_never_by_themselves(
        self, rig
    ) -> None:
        """The seam itself. Before this the checkpoint ran four times over."""
        rig.run(max_review_cycles=3)

        assert rig.journey_stages() == [
            "task-review",
            "task-work",
            "task-review",
            "pull-request-review",
            "task-review",
            "task-work",
            "pull-request-review",
        ]

    def test_the_journey_reaches_its_card(self, rig) -> None:
        report = rig.run(max_review_cycles=3)

        assert report.outcome is ConductorRunOutcome.DELIVERED, report.rationale
        assert len(rig.delivery.publishes) == 1

    def test_the_checkpoint_ran_twice_and_wrote_both_verdicts_down(
        self, rig
    ) -> None:
        rig.run(max_review_cycles=3)

        rows = rig.checkpoint_rows()
        assert [row.status for row in rows] == ["FAILED", "PASSED"]
        assert rows[0].details["failing_tests"] == list(FAILING)
        assert rig.gates.reads == 2, "the suite runs once per checkpoint, no more"

    def test_the_gate_driven_review_is_handed_the_document(self, rig) -> None:
        rig.run(max_review_cycles=3)

        third = rig.guardkit.review_context_paths()[2]
        gate_docs = [p for p in third if p.endswith(GATE_FAILURES_DOCUMENT_NAME)]
        assert len(gate_docs) == 1, third
        text = Path(gate_docs[0]).read_text(encoding="utf-8")
        assert GATE_FAILURES_INSTRUCTION in text
        assert "uv run pytest -q" in text
        assert "Exit code: 1" in text
        for name in FAILING:
            assert name in text

    def test_it_is_added_beside_what_the_review_already_had(self, rig) -> None:
        """The approved work's own artefact is still on the leg's argv."""
        rig.run(max_review_cycles=3)

        third = rig.guardkit.review_context_paths()[2]
        assert [p for p in third if p.endswith("fixed.py")], third

    def test_the_earlier_reviews_were_handed_no_gates_document(self, rig) -> None:
        rig.run(max_review_cycles=3)

        for paths in rig.guardkit.review_context_paths()[:2]:
            assert not [p for p in paths if p.endswith(GATE_FAILURES_DOCUMENT_NAME)]

    def test_the_journey_is_never_stopped_by_the_nothing_changed_rule(
        self, rig
    ) -> None:
        report = rig.run(max_review_cycles=3)

        assert report.outcome is not ConductorRunOutcome.NOTHING_CHANGED
        assert report.failure_pack is None


# ---------------------------------------------------------------------------
# No cycle left: the journey ends, naming what stayed red
# ---------------------------------------------------------------------------


class TestTheRedChecksEndAJourneyWithNoCycleLeft:
    """Cap of two: the same red checkpoint, and nowhere left to loop back to."""

    def test_the_build_is_closed_out_failed_with_the_failing_tests(
        self, rig
    ) -> None:
        report = rig.run(max_review_cycles=2)

        assert report.outcome is ConductorRunOutcome.RED_GATE_STOP, report.rationale
        assert report.rationale.startswith(
            "the merge-ready checks stayed red and no review cycle is left"
        )
        assert FAILING[0] in report.rationale
        row = rig.pool.get_build_row(BUILD_ID)
        assert row is not None
        assert row.status == BuildState.FAILED.value
        assert "no review cycle is left" in (row.error or "")

    def test_no_card_is_published(self, rig) -> None:
        rig.run(max_review_cycles=2)

        assert rig.delivery.publishes == []

    def test_the_failure_pack_is_written(self, rig) -> None:
        report = rig.run(max_review_cycles=2)

        assert report.failure_pack is not None

    def test_the_queued_message_is_released_exactly_once(self, rig) -> None:
        rig.run(max_review_cycles=2)

        assert rig.release.calls == 1

    def test_the_red_verdict_is_still_written_into_the_history(self, rig) -> None:
        rig.run(max_review_cycles=2)

        rows = rig.checkpoint_rows()
        assert [row.status for row in rows] == ["FAILED"]

    def test_the_suite_is_run_once_not_four_times(self, rig) -> None:
        """Attempt fifteen ran it four times in two minutes."""
        rig.run(max_review_cycles=2)

        assert rig.gates.reads == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
