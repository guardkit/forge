"""THE FULL-JOURNEY REPLAY — a banked-pack-shaped fixture, end to end.

Conductor revival Stage 2, exit criterion.

Every unit test in this lane pins one seam. This one drives the whole fix
journey through the REAL pieces — the real turn loop, the real Supervisor,
the real Mode C planner, the real ``stage_log`` projection, the real
``fix_tasks`` producer, the real merge-ready checkpoint, over a real SQLite
database — with fakes at exactly two edges: the GuardKit subprocess and the
merge card's delivery. That is the shape the individual pins cannot prove,
because every defect this lane fixed was a defect *between* two components
that each looked correct alone.

The journey::

    banked failure pack in
      → task-review          (records fix_tasks)
      → WAIT honoured        (turn-serial: no plan while a stage is in flight)
      → task-work × N        (one dispatch per fix task, attributable)
      → follow-up review     (clean)
      → merge-ready checkpoint on a fakes-GREEN gate
      → EXACTLY ONE card     (the h.5 audit: one card per journey, never more)
      → DELIVERED

…and the declined variant: same journey, owner says no, ZERO re-issue, and
the run report says ``declined`` rather than claiming a delivery.

**The REAL shadow replay on the real banked FEAT-DRF pack remains the
coordinator's attended act.** This is fakes-driven and network-free; it
proves the wiring, not the live behaviour of guardkit or the broker.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
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
from forge.pipeline.conductor_driver import (
    ConductorRunOutcome,
    drive_fix_journey,
)
from forge.pipeline.forward_context_builder import ForwardContextBuilder
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-DRF-20260731"
SOURCE_BUILD_ID = "build-FEAT-DRF-20260730"
TASK_ID = "TASK-DRF001"
FIX_TASKS = ("TASK-DRF001-001", "TASK-DRF001-002")


# ---------------------------------------------------------------------------
# The fixture: a banked-failure-pack-shaped receipts tree
# ---------------------------------------------------------------------------


def _bank_a_failure_pack(receipts_root: Path) -> None:
    """Write the shape a terminally-failed build leaves behind.

    Not the real FEAT-DRF pack — its shape. ``failure-manifest.json`` is
    the machine-readable index the fix journey's context builder reads, so
    the journey starts from the evidence the failed build left rather than
    from a reason string (design pass §b.2, the IN direction).
    """
    pack = receipts_root / SOURCE_BUILD_ID
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "failure-manifest.json").write_text(
        json.dumps(
            {
                "build_id": SOURCE_BUILD_ID,
                "feature_id": "FEAT-DRF",
                "correlation_id": "corr-drf-1",
                "reason": "gates red: 3 failing tests in the runtime smoke",
                "branch": "feat/FEAT-DRF",
                "failed_at": "2026-07-30T21:04:00+00:00",
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The two fakes — the subprocess, and the card's delivery
# ---------------------------------------------------------------------------


class FakeGuardKit:
    """Stands in for ``guardkit.run``.

    The first ``task-review`` emits two fix-task artefacts; each
    ``task-work`` emits its own artefact; the follow-up review emits none
    (a clean review). Those artefact paths are what the real ``fix_tasks``
    producer reads, so the fan-out under test is driven by the same signal
    production reads.
    """

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.calls: list[dict[str, Any]] = []
        self._reviews = 0

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        subcommand = kwargs["subcommand"]
        artefacts: list[str] = []
        if subcommand == "task-review":
            self._reviews += 1
            if self._reviews == 1:
                artefacts = [
                    str(self.worktree / "tasks" / f"{t}.yaml") for t in FIX_TASKS
                ]
        elif subcommand == "task-work":
            argv = kwargs["args"]
            subject = argv[argv.index("--task-id") + 1]
            artefacts = [str(self.worktree / "work" / f"{subject}.patch")]
        return GuardKitResult(
            status="success",
            subcommand=subcommand,
            exit_code=0,
            stdout_tail="",
            stderr="",
            duration_secs=1.0,
            artefacts=artefacts,
            warnings=[],
        )

    def subcommands(self) -> list[str]:
        return [c["subcommand"] for c in self.calls]

    def subjects(self) -> list[str]:
        out = []
        for call in self.calls:
            argv = call["args"]
            out.append(argv[argv.index("--task-id") + 1])
        return out


class FakeCardDelivery:
    """Stands in for the approve-click merge card's publisher.

    Counts publishes — the design pass's h.5 audit is literally "count
    cards published per replay; the correct number for a whole fix journey
    is ONE".
    """

    def __init__(self, verdict: str = "RESUMED") -> None:
        self.verdict = verdict
        self.publishes: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.publishes.append(kwargs)
        return self.verdict


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


def _config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/"]}},
            "conductor": {"enabled": True},
        }
    )


def _init_worktree_with_a_fix_commit(worktree: Path) -> None:
    """Make the worktree a real git repo carrying one commit off ``main``.

    The Mode C terminal handler probes ``main..HEAD`` to decide between
    "hand back a gates-green branch" and "ended quietly, nothing changed".
    A fix journey that produced a commit is the branch that reaches the
    merge-ready checkpoint, so the fixture has to actually have one. Local
    git only — no network.
    """
    run = lambda *a: subprocess.run(
        a, cwd=worktree, check=True, capture_output=True
    )
    run("git", "init", "--initial-branch=main")
    run("git", "config", "user.email", "replay@example.invalid")
    run("git", "config", "user.name", "replay")
    (worktree / "README.md").write_text("base\n")
    run("git", "add", "-A")
    run("git", "commit", "-m", "base")
    run("git", "checkout", "-b", "fix/FEAT-DRF")
    (worktree / "work" / "fix.txt").write_text("the fix\n")
    run("git", "add", "-A")
    run("git", "commit", "-m", "the fix")


@pytest.fixture
def rig(tmp_path: Path):
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    (worktree / "work").mkdir(parents=True)
    _init_worktree_with_a_fix_commit(worktree)
    receipts_root = tmp_path / "receipts"
    _bank_a_failure_pack(receipts_root)

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-DRF', 'r', "
        "'fix/FEAT-DRF', ?, 'RUNNING', 'cli', 'corr-fix-1', "
        "'2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', ?, 'mode-c', ?)",
        (
            BUILD_ID,
            str(worktree / "tasks" / "fix-task.yaml"),
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

        def run(self, delivery: FakeCardDelivery, *, gates_green: bool = True):
            from forge.cli._serve_deps_forward_context import (
                ForgeConfigWorktreeAllowlist,
                build_stage_log_reader,
            )

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
                gates_green_reader=lambda **_: gates_green,
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
            deps = deps_factory(BUILD_ID, supervisor)
            return asyncio.run(drive_fix_journey(BUILD_ID, deps))

    return _Rig()


# ---------------------------------------------------------------------------
# The happy journey
# ---------------------------------------------------------------------------


class TestTheWholeFixJourney:
    def test_it_walks_review_work_review_checkpoint_and_delivers(self, rig) -> None:
        delivery = FakeCardDelivery("RESUMED")

        report = rig.run(delivery)

        assert report.outcome is ConductorRunOutcome.DELIVERED, report.rationale
        assert rig.guardkit.subcommands() == [
            "task-review",
            "task-work",
            "task-work",
            "task-review",
        ]

    def test_each_fix_task_is_dispatched_exactly_once_against_its_own_subject(
        self, rig
    ) -> None:
        """The attribution key earning its keep, end to end.

        An unattributable ``task-work`` row reads as "never dispatched" and
        the same fix goes out twice.
        """
        rig.run(FakeCardDelivery())

        subjects = rig.guardkit.subjects()
        assert subjects == [TASK_ID, FIX_TASKS[0], FIX_TASKS[1], TASK_ID]

    def test_exactly_one_card_is_published(self, rig) -> None:
        """Design pass risk h.5's audit: the correct number is ONE."""
        delivery = FakeCardDelivery()

        rig.run(delivery)

        assert len(delivery.publishes) == 1

    def test_every_dispatch_carries_its_own_correlation_id(self, rig) -> None:
        """The FTR exact-match law, with siblings in the same journey."""
        rig.run(FakeCardDelivery())

        ids = [
            call["args"][call["args"].index("--correlation-id") + 1]
            for call in rig.guardkit.calls
        ]
        assert len(set(ids)) == len(ids)
        assert all(i.startswith("corr-fix-1:") for i in ids)

    def test_no_dispatch_carries_the_removed_parent_feature_flag(self, rig) -> None:
        rig.run(FakeCardDelivery())

        assert all("--parent-feature" not in c["args"] for c in rig.guardkit.calls)

    def test_the_journey_leaves_per_stage_receipts(self, rig) -> None:
        """Success leaves receipts too, not only failure (§b.2, OUT)."""
        report = rig.run(FakeCardDelivery())

        stages_dir = rig.receipts_root / BUILD_ID / "stages"
        assert stages_dir.is_dir()
        assert len(report.stage_receipts) >= 3
        assert sorted(p.name for p in stages_dir.iterdir())[0].startswith("001-")

    def test_the_fix_tasks_landed_on_the_review_row(self, rig) -> None:
        rig.run(FakeCardDelivery())

        reviews = [
            r for r in rig.pool.read_stages(BUILD_ID) if r.stage_label == "task-review"
        ]
        assert reviews[0].details["fix_tasks"] == list(FIX_TASKS)
        assert reviews[1].details["fix_tasks"] == []

    def test_the_work_dispatch_carries_the_failure_pack_forward(self, rig) -> None:
        """The journey starts from the evidence, not from a reason string."""
        rig.run(FakeCardDelivery())

        work_calls = [c for c in rig.guardkit.calls if c["subcommand"] == "task-work"]
        blob = "\n".join(work_calls[0]["args"])
        assert SOURCE_BUILD_ID in blob
        assert "failure_pack" in blob

    def test_the_journey_closes_out_durably(self, rig) -> None:
        rig.run(FakeCardDelivery())

        labels = [r.stage_label for r in rig.pool.read_stages(BUILD_ID)]
        assert "conductor-close-out" in labels
        assert "conductor-turn" in labels


# ---------------------------------------------------------------------------
# The declined variant
# ---------------------------------------------------------------------------


class TestTheDeclinedVariant:
    def test_a_declined_card_is_reported_declined_not_delivered(self, rig) -> None:
        """Item 7. Stopping was always right; the WORD was wrong."""
        report = rig.run(FakeCardDelivery("CANCELLED"))

        assert report.outcome is ConductorRunOutcome.DECLINED

    def test_a_declined_card_is_never_re_issued(self, rig) -> None:
        """Item 6. Zero re-issue: the owner said no once, and once is all."""
        delivery = FakeCardDelivery("CANCELLED")

        rig.run(delivery)

        assert len(delivery.publishes) == 1

    def test_an_expired_card_says_expired(self, rig) -> None:
        report = rig.run(FakeCardDelivery("TIMED_OUT"))

        assert report.outcome is ConductorRunOutcome.EXPIRED


# ---------------------------------------------------------------------------
# The red-gate variant — the hard precondition
# ---------------------------------------------------------------------------


class TestARedGateNeverPublishes:
    def test_a_red_gate_publishes_no_card_at_all(self, rig) -> None:
        """§c.3: fix loops run BEFORE the merge word. A red gate is not a card."""
        delivery = FakeCardDelivery()

        report = rig.run(delivery, gates_green=False)

        assert delivery.publishes == []
        assert report.outcome is not ConductorRunOutcome.DELIVERED

    def test_a_red_gate_journey_leaves_its_own_failure_pack(self, rig) -> None:
        rig.run(FakeCardDelivery(), gates_green=False)

        manifest = rig.receipts_root / BUILD_ID / "failure-manifest.json"
        assert manifest.exists()
        recorded = json.loads(manifest.read_text())
        assert recorded["pack_kind"]
        # The cross-pack pointer a diagnoser needs to read both halves.
        assert recorded["source_build_id"] == SOURCE_BUILD_ID


class TestTheTurnSerialLaw:
    def test_the_loop_never_plans_while_a_stage_is_in_flight(self, rig) -> None:
        """Risk h.1, observed rather than asserted about.

        The planner's in-flight sentinel mis-encodes "a fix task is
        running" as "all fix tasks completed". The belt is that a dispatch
        is AWAITED inside the turn, so the next plan cannot overlap it —
        which shows up as a strictly alternating dispatch/return sequence
        with no second dispatch of the same subject.
        """
        rig.run(FakeCardDelivery())

        subjects = rig.guardkit.subjects()
        work_subjects = [s for s in subjects if s in FIX_TASKS]
        assert sorted(work_subjects) == sorted(FIX_TASKS)
        assert len(work_subjects) == len(set(work_subjects))

    def test_a_second_loop_on_the_same_build_is_refused(self, rig) -> None:
        from forge.pipeline.conductor_driver import (
            ConductorDriverDeps,
            ConductorTurnLoop,
            TurnSerialViolation,
            _IN_FLIGHT,
        )

        _IN_FLIGHT.add(BUILD_ID)
        try:
            loop = ConductorTurnLoop(ConductorDriverDeps(supervisor=object()))
            with pytest.raises(TurnSerialViolation):
                asyncio.run(loop.drive(BUILD_ID))
        finally:
            _IN_FLIGHT.discard(BUILD_ID)
