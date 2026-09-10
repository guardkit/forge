"""THE LAST CYCLE, DRIVEN — a fix journey that needs two rounds reaches its card.

The sibling of ``test_fix_journey_total_failure_drive``: the same real
pieces — the real turn loop, the real Supervisor, the real Mode C planner,
the real ``stage_log`` projection, the real budget guard, a real SQLite
database — with fakes at exactly two edges (the GuardKit subprocess and the
merge card's delivery).

What it reproduces is journey one, 2026-09-08. Under the ``fix-journey``
profile a build may run two review cycles. Journey one used both: the first
review's fixes were wrong, the second review found the real cause, and its
three work legs were all approved. The planner then did what it does after
work legs — asked for a follow-up review — the budget guard refused it at
the cap, no escalation could be published because nothing on this path
wires one, and the build sat RUNNING for ever one step short of the merge
card, with every fix approved by the coach and the oracle.

Two things are pinned here, and the first is the delta:

* at the cap, with every fix task done and work approved, the journey goes
  to the merge-ready checkpoint and the card is published;
* a cap breach that still happens — the same profile's wall-clock cap here
  — closes the build out FAILED with the reason, instead of leaving the row
  RUNNING with the queue's slot held.

Network-free: no NATS client, no broker URL, no port.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
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
from forge.pipeline.conductor_driver import ConductorRunOutcome, drive_fix_journey

BUILD_ID = "build-FEAT-CAP-20260908"
SOURCE_BUILD_ID = "build-FEAT-CAP-20260907"
TASK_ID = "TASK-CAP001"

#: Fix tasks per review cycle. Journey one's second cycle had three; two is
#: enough to show the cycle's fan-out being exhausted.
FIX_TASKS_PER_CYCLE = 2

#: The turn ceiling this rig drives under. Two full cycles of (1 review + 2
#: legs) plus the checkpoint turn is 7; the ceiling is set well above that
#: so "reached the card" is never an artefact of running out of turns.
TURN_CEILING = 20


def cycle_fix_tasks(cycle: int) -> tuple[str, ...]:
    """The fix-task ids review ``cycle`` mints (1-based)."""
    return tuple(
        f"TASK-CAP{cycle:03d}-{i:03d}" for i in range(1, FIX_TASKS_PER_CYCLE + 1)
    )


def _bank_a_failure_pack(receipts_root: Path) -> None:
    pack = receipts_root / SOURCE_BUILD_ID
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "failure-manifest.json").write_text(
        json.dumps(
            {
                "build_id": SOURCE_BUILD_ID,
                "feature_id": "FEAT-CAP",
                "correlation_id": "corr-cap-1",
                "reason": "gates red: the delete path returns a 503",
                "branch": "feat/FEAT-CAP",
                "failed_at": "2026-09-07T21:04:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class FakeGuardKitTwoGoodCycles:
    """Journey one's shape: two review cycles, every work leg approved.

    Each review mints a fresh set of fix tasks and reports fresh finding
    anchors — the second review found something the first had missed, which
    is why the journey needed the second cycle at all. Fresh anchors also
    keep the review-cycle no-progress rule (which has its own pins in
    ``test_conductor_driver``) out of a drive it is not about.

    ``work_status`` is the one knob: ``"success"`` is journey one, and
    ``"failed"`` is the same journey with nothing approved in its last
    cycle — the shape that still breaches the cap.
    """

    def __init__(self, worktree: Path, *, work_status: str = "success") -> None:
        self.worktree = worktree
        self.work_status = work_status
        self.calls: list[dict[str, Any]] = []
        self.reviews = 0

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
                    for t in cycle_fix_tasks(self.reviews)
                ],
                detection_findings=[
                    {
                        "file": f"src/cap/cycle{self.reviews:03d}/{t.lower()}.py",
                        "severity": "high",
                        "summary": f"the defect {t} names",
                    }
                    for t in cycle_fix_tasks(self.reviews)
                ],
                warnings=[],
            )
        if self.work_status == "success":
            return GuardKitResult(
                status="success",
                subcommand=subcommand,
                exit_code=0,
                stdout_tail="",
                stderr="",
                duration_secs=2.0,
                artefacts=[str(self.worktree / "src" / "cap" / "fixed.py")],
                warnings=[],
            )
        return GuardKitResult(
            status="failed",
            subcommand=subcommand,
            exit_code=2,
            stdout_tail="",
            stderr="AgentInvocationError: the harness refused the seat",
            duration_secs=0.4,
            artefacts=[],
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


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _make_a_real_journey_tree(worktree: Path, branch: str) -> None:
    """A real git tree on the journey's own branch.

    The specification fence reads what this branch changed against its base,
    and a recorded worktree that is not a git tree is a reading that did not
    happen — a refusal, not a pass. This rig is not about the fence, so it
    gets a real tree carrying no change at all: the fence reads it, finds
    nothing, and the rest of the journey is what decides.
    """
    for args in (
        ("init", "-b", branch),
        ("commit", "--allow-empty", "-m", "the branch the journey works on"),
    ):
        subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
            ["git", *args],
            cwd=worktree,
            check=True,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
        )


@pytest.fixture
def rig(tmp_path: Path):
    """The journey's real machinery over a real SQLite file.

    The build row carries ``profile='fix-journey'``, which is where the cap
    of two review cycles comes from — the same built-in profile production
    resolves for a repair build. Nothing wires a pause collaborator, which
    is also production on this path (the warning is logged at every journey
    start).
    """
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    (worktree / "src" / "cap").mkdir(parents=True)
    _make_a_real_journey_tree(worktree, "fix/FEAT-CAP")
    receipts_root = tmp_path / "receipts"
    _bank_a_failure_pack(receipts_root)

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    # Started NOW: the fix-journey profile also carries a wall-clock cap,
    # and a build row dated in the past breaches that one first — which
    # would stop the journey for a reason this rig is not about.
    started = datetime.now(timezone.utc).isoformat()
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id, profile) VALUES (?, "
        "'FEAT-CAP', 'r', 'fix/FEAT-CAP', ?, 'RUNNING', 'cli', 'corr-cap-2', "
        "?, ?, ?, 'mode-c', ?, 'fix-journey')",
        (
            BUILD_ID,
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
            self.cx = cx
            self.worktree = worktree
            self.receipts_root = receipts_root
            self.guardkit = FakeGuardKitTwoGoodCycles(worktree)

        def run(self, delivery: FakeCardDelivery):
            from forge.cli._serve_deps_forward_context import (
                ForgeConfigWorktreeAllowlist,
                build_stage_log_reader,
            )
            from forge.pipeline.forward_context_builder import ForwardContextBuilder

            config = _config()
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
                publish_card=delivery,
                gates_green_reader=lambda **_: True,
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
                deps_factory(BUILD_ID, supervisor), max_turns=TURN_CEILING
            )
            return asyncio.run(drive_fix_journey(BUILD_ID, deps))

    return _Rig()


class TestTheSecondCycleReachesTheCard:
    """Journey one's shape, driven: two cycles, then the merge card."""

    def test_the_journey_runs_two_cycles_and_then_checks_for_merge(
        self, rig
    ) -> None:
        rig.run(FakeCardDelivery())

        assert rig.guardkit.subcommands() == [
            "task-review",
            "task-work",
            "task-work",
            "task-review",
            "task-work",
            "task-work",
        ], (
            "a third task-review here is the ninth seam: the journey asking "
            "for a review the cap must refuse"
        )

    def test_the_card_is_published(self, rig) -> None:
        delivery = FakeCardDelivery()

        report = rig.run(delivery)

        assert len(delivery.publishes) == 1, delivery.publishes
        assert report.outcome is ConductorRunOutcome.DELIVERED, report.rationale

    def test_the_reason_says_why_it_went_to_the_checks(self, rig) -> None:
        delivery = FakeCardDelivery()

        rig.run(delivery)

        rationale = delivery.publishes[0]["rationale"]
        assert "every fix task is done and the review cycles are used up" in (
            rationale
        ), rationale

    def test_the_build_never_pauses_on_the_cap(self, rig) -> None:
        """The guard is armed and simply never has to refuse anything."""
        rig.run(FakeCardDelivery())

        rows = rig.pool.read_stages(BUILD_ID)
        turns = [r for r in rows if r.stage_label == "conductor-turn"]
        outcomes = [r.details.get("outcome") for r in turns]
        assert "paused_budget" not in outcomes, outcomes

    def test_the_build_row_is_not_left_running(self, rig) -> None:
        rig.run(FakeCardDelivery())

        row = rig.pool.get_build_row(BUILD_ID)
        # The card path's row is owned by the gate's own state machine, so
        # the journey's close-out declines to guess there. What matters is
        # that the journey ended at the card and said so.
        close_outs = [
            r
            for r in rig.pool.read_stages(BUILD_ID)
            if r.stage_label == "conductor-close-out"
        ]
        assert len(close_outs) == 1, close_outs
        assert row is not None


class TestACapBreachIsClosedOutNotLeftRunning:
    """The other half of seam nine: a breach nobody can answer.

    The wall-clock cap is the readiest of the fix-journey profile's caps to
    breach in a test — a build that started an hour ago is over its hour —
    and the shape it produces is exactly the one journey one hit: the guard
    refuses the dispatch, nothing is wired to publish an escalation card,
    and before this lane the build sat RUNNING for ever with the pipeline
    consumer's slot held.
    """

    def test_the_build_is_closed_out_failed_with_the_reason(self, rig) -> None:
        from forge.lifecycle.state_machine import BuildState

        rig.cx.execute(
            "UPDATE builds SET started_at = ? WHERE build_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(),
                BUILD_ID,
            ),
        )
        rig.cx.commit()

        report = rig.run(FakeCardDelivery())

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET, report.rationale
        assert "no one could be asked" in report.rationale
        row = rig.pool.get_build_row(BUILD_ID)
        assert row is not None
        assert row.status == BuildState.FAILED.value, row.status
        assert "no one could be asked" in (row.error or ""), row.error

    def test_the_journey_leaves_its_failure_pack(self, rig) -> None:
        rig.cx.execute(
            "UPDATE builds SET started_at = ? WHERE build_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(),
                BUILD_ID,
            ),
        )
        rig.cx.commit()

        report = rig.run(FakeCardDelivery())

        assert report.failure_pack is not None
        close_outs = [
            r
            for r in rig.pool.read_stages(BUILD_ID)
            if r.stage_label == "conductor-close-out"
        ]
        assert len(close_outs) == 1, close_outs
