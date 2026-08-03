"""THE RUNAWAY, DRIVEN — ASSUM-008 as narrowed 2026-08-02.

The sibling of ``test_fix_journey_full_replay``: the same real pieces —
the real turn loop, the real Supervisor, the real Mode C planner, the real
``stage_log`` projection, the real terminal handler, a real SQLite database
— with fakes at exactly two edges (the GuardKit subprocess and the merge
card's delivery).

The fixture reproduces the runaway's shape, not a smaller cousin of it:
**every** ``task-work`` leg fails, and **every** ``task-review`` re-mints
the same defects under fresh ids (the fix-task id is prose+position
derived, so a re-run of the same review mints new ones — 88 distinct ids
for ~5 defects on the banked ledger). Because a FAILED work leg closes its
fix-task slot exactly like an approved one, the pre-narrowing planner reads
each cycle as "all fix tasks completed" and schedules another review, and
the journey only ever stops because a turn ceiling is in the way. That is
the 42-cycle loop, in a fixture.

The delta is MEASURED here, not asserted about: the same rig with the
narrowing neutered is driven in
``test_without_the_rule_this_fixture_runs_away`` and hits the turn cap
after four full cycles. With the rule, the journey stops on the leg that
failed — one review, two failed legs, and nothing more.

Turn ceiling: the rig pins ``max_turns`` at
:data:`TURN_CEILING` so the neutered drive terminates in a test-shaped
number of turns rather than the production default of 200.

Network-free: no NATS client, no broker URL, no port. The card's publisher
is a counting fake and the subprocess is a fake.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
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
from forge.pipeline.forward_context_builder import ForwardContextBuilder
from forge.pipeline.mode_c_planner import ModeCCyclePlanner

BUILD_ID = "build-FEAT-RUNAWAY-20260802"
SOURCE_BUILD_ID = "build-FEAT-RUNAWAY-20260801"
TASK_ID = "TASK-RUN001"

#: Fix tasks per review cycle. Two is the smallest number that still shows
#: "the cycle's fan-out is exhausted" as a distinct thing from "the one leg
#: failed"; the planner unit tests parametrize N over 1/2/3/5/12.
FIX_TASKS_PER_CYCLE = 2

#: The turn ceiling this rig drives under. Four full cycles of
#: (1 review + 2 legs) plus the turn that would open cycle five — small
#: enough to keep the neutered drive fast, large enough that reaching it is
#: unambiguously "went round again", not "stopped one turn late".
TURN_CEILING = 12


def cycle_fix_tasks(cycle: int) -> tuple[str, ...]:
    """The fix-task ids review ``cycle`` mints (1-based).

    Fresh ids every cycle, deliberately: the runaway's reviews did not
    repeat an identifier once, which is exactly why an id-based dedup would
    have aimed at the wrong code (stage-2 design §5).
    """
    return tuple(
        f"TASK-RUN{cycle:03d}-{i:03d}" for i in range(1, FIX_TASKS_PER_CYCLE + 1)
    )


#: The ids of the first cycle — the only cycle that runs once the rule is in.
FIX_TASKS = cycle_fix_tasks(1)


def _bank_a_failure_pack(receipts_root: Path) -> None:
    pack = receipts_root / SOURCE_BUILD_ID
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "failure-manifest.json").write_text(
        json.dumps(
            {
                "build_id": SOURCE_BUILD_ID,
                "feature_id": "FEAT-RUNAWAY",
                "correlation_id": "corr-runaway-1",
                "reason": "gates red: the runtime smoke never ran",
                "branch": "feat/FEAT-RUNAWAY",
                "failed_at": "2026-08-01T21:04:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class FakeGuardKitTheRunawayShape:
    """Every review re-mints; every work leg dies. The ledger's shape.

    ``task-review`` always succeeds and always emits a fresh fix-task
    artefact per defect — the reviewer keeps finding the same things
    because nothing ever got fixed. ``task-work`` always fails and
    produces NO artefacts: that is the honest shape of a tooling fault (the
    leg never got far enough to write anything), and it keeps the fixture
    from minting fresh fix tasks out of the failure itself.

    Nothing here is aware of the narrowing. The same object drives both the
    ruled path and the neutered one; only the planner differs.
    """

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
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


@pytest.fixture
def rig(tmp_path: Path):
    """The journey's real machinery over a real SQLite file.

    No git repository is created. The commit probe is never consulted on
    this journey — the terminal handler's all-work-failed branch and the
    planner's tooling-fault terminal both short-circuit before it — so a
    seeded repo would be scenery, and scenery in a fixture reads as a claim
    the drive does not make.
    """
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    receipts_root = tmp_path / "receipts"
    _bank_a_failure_pack(receipts_root)

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-RUNAWAY', "
        "'r', 'fix/FEAT-RUNAWAY', ?, 'RUNNING', 'cli', 'corr-runaway-2', "
        "'2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z', ?, 'mode-c', ?)",
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
            self.guardkit = FakeGuardKitTheRunawayShape(worktree)

        def run(self, delivery: FakeCardDelivery):
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


# ---------------------------------------------------------------------------
# The measured delta — the runaway, and the runaway stopped
# ---------------------------------------------------------------------------


class TestTheRunawayIsWhatThisFixtureDoes:
    """The claim the module docstring makes, as an assertion.

    Neuter the narrowing — one method, returning "this cycle is not a total
    failure" — and the SAME fixture does what the ledger recorded. This is
    the control the rest of the file is measured against; without it every
    assertion below is compatible with the rule doing nothing at all.
    """

    @staticmethod
    def _neuter(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ModeCCyclePlanner,
            "_total_work_failure",
            staticmethod(lambda **_kwargs: None),
        )

    def test_without_the_rule_this_fixture_runs_away(
        self, rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._neuter(monkeypatch)

        report = rig.run(FakeCardDelivery())

        assert report.outcome is ConductorRunOutcome.TURN_CAP, (
            "the pre-narrowing planner has no reason to stop this journey: "
            f"got {report.outcome} — {report.rationale}"
        )
        assert report.turns == TURN_CEILING
        # Four full cycles of review + 2 failed legs. Each review re-minted
        # the same defects under fresh ids and each cycle closed its slots
        # on nothing but failures.
        one_cycle = ["task-review", "task-work", "task-work"]
        assert rig.guardkit.subcommands() == one_cycle * 4
        assert rig.guardkit.reviews == 4

    def test_without_the_rule_no_review_ever_learns_anything(
        self, rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ids are fresh every cycle — why id-based dedup was the wrong aim.

        Four cycles, eight fix tasks, not one identifier repeated: the
        durable rows the planner reads carry a brand-new set each time.
        """
        self._neuter(monkeypatch)

        rig.run(FakeCardDelivery())

        settled = [
            r
            for r in rig.pool.read_stages(BUILD_ID)
            if r.stage_label == "task-work"
            and r.details.get("lifecycle_state") != "running"
        ]
        ids = [r.details["fix_task_id"] for r in settled]
        assert len(ids) == 8
        assert len(set(ids)) == 8, ids


class TestATotallyFailedCycleStopsAndPublishesNothing:
    def test_the_journey_stops_after_the_last_failed_leg(self, rig) -> None:
        """One review, N work legs, and then NOTHING. No cycle two."""
        rig.run(FakeCardDelivery())

        assert rig.guardkit.subcommands() == [
            "task-review",
            "task-work",
            "task-work",
        ], (
            "a second task-review here is the runaway: the cycle whose every "
            "leg failed asking the reviewer to find the same things again"
        )

    def test_the_loop_ends_on_a_terminal_not_a_cap_or_a_wait(self, rig) -> None:
        report = rig.run(FakeCardDelivery())

        assert report.outcome is ConductorRunOutcome.COMPLETED, report.rationale
        assert report.outcome is not ConductorRunOutcome.TURN_CAP
        assert report.outcome is not ConductorRunOutcome.WAIT_EXPIRED
        assert report.turns == 4  # review + 2 legs + the terminal turn

    def test_the_terminal_names_the_tooling_fault_in_the_operators_words(
        self, rig
    ) -> None:
        """The stop and the WORD, together.

        A diagnoser arriving at this build reads exactly one sentence. It
        has to carry the canonical constant every other all-work-failed
        terminal on this estate carries, the class in plain words, and the
        ids — and it must not accuse anyone of a wiring bug, because none
        happened: the supervisor is on its routine path.
        """
        report = rig.run(FakeCardDelivery())

        assert report.rationale.startswith("failed: mode-c-all-task-work-failed"), (
            report.rationale
        )
        assert "a tooling fault, not a fix outcome" in report.rationale
        for fix_task_id in FIX_TASKS:
            assert fix_task_id in report.rationale, report.rationale
        assert "mid-cycle" not in report.rationale, report.rationale
        assert "clean-review" not in report.rationale

    def test_nothing_is_published(self, rig) -> None:
        """No merge card, at all. The owner hears nothing about a tooling fault.

        Non-discriminating on purpose: the pre-narrowing planner publishes
        nothing here either (it runs to the turn cap). The assertion is
        kept because "a broken tool never reaches the owner" is an
        invariant of this journey, not evidence for the rule.
        """
        delivery = FakeCardDelivery()

        rig.run(delivery)

        assert delivery.publishes == []

    def test_the_journey_leaves_per_stage_receipts(self, rig) -> None:
        report = rig.run(FakeCardDelivery())

        stages_dir = rig.receipts_root / BUILD_ID / "stages"
        assert stages_dir.is_dir()
        assert len(report.stage_receipts) >= 3

    def test_the_journey_closes_out_durably_saying_how_it_ended(self, rig) -> None:
        rig.run(FakeCardDelivery())

        rows = rig.pool.read_stages(BUILD_ID)
        close_outs = [r for r in rows if r.stage_label == "conductor-close-out"]
        assert len(close_outs) == 1
        assert close_outs[0].details["outcome"] == "terminal"

    def test_both_durable_rows_carry_the_same_true_sentence(self, rig) -> None:
        """The three places the terminal is banked must agree.

        The run report, the ``conductor-turn`` row and the
        ``conductor-close-out`` row are all a diagnoser has once the daemon
        has moved on. A wrong word in any of them outlives the run.
        """
        report = rig.run(FakeCardDelivery())

        rows = rig.pool.read_stages(BUILD_ID)
        turn_rows = [r for r in rows if r.stage_label == "conductor-turn"]
        close_outs = [r for r in rows if r.stage_label == "conductor-close-out"]
        terminal_turn = turn_rows[-1]

        assert terminal_turn.details["outcome"] == "terminal"
        for banked in (
            terminal_turn.details["rationale"],
            close_outs[0].details["rationale"],
        ):
            assert banked == report.rationale
            assert "mid-cycle" not in banked

    def test_both_work_rows_are_recorded_failed_and_attributable(self, rig) -> None:
        """The evidence the rule reads, pinned at the durable layer.

        If these rows landed any other way — PASSED, or without a
        ``fix_task_id`` — the planner's history projection would say
        something else entirely and the drive above would pass for the
        wrong reason.
        """
        rig.run(FakeCardDelivery())

        work = [
            r for r in rig.pool.read_stages(BUILD_ID) if r.stage_label == "task-work"
        ]
        settled = [r for r in work if r.details.get("lifecycle_state") != "running"]
        assert [r.status for r in settled] == ["FAILED", "FAILED"]
        assert sorted(r.details["fix_task_id"] for r in settled) == sorted(FIX_TASKS)


# ---------------------------------------------------------------------------
# The seam this drive does NOT reach — pinned so it is visible, not lost
# ---------------------------------------------------------------------------


class TestTheSeamsBeyondThePlanner:
    """A named gap, held by a test rather than by a paragraph in a report.

    Asserting the current truth here means a future cure breaks THIS test
    loudly instead of leaving a stale claim somewhere else.
    """

    def test_GAP_a_terminal_writes_no_failure_pack(self, rig) -> None:
        """The driver writes packs on loud STOPS, never on a TERMINAL.

        ``ConductorTurnLoop`` calls ``write_failure_pack`` on ERROR /
        PAUSED_BUDGET / NOTHING_CHANGED / WAIT_EXPIRED / RED_GATE_STOP /
        TURN_CAP. A TERMINAL closes out and exports receipts instead —
        which is right for the CLEAN_REVIEW terminals ("no-commit
        terminals stay silent") and arguably wrong for this one, which is
        a tooling fault a diagnoser will come looking for.

        Pinned, not fixed: the pack decision belongs to whoever rules on
        the driver's terminal branch, and it is outside this lane's scope.
        """
        rig.run(FakeCardDelivery())

        assert not (rig.receipts_root / BUILD_ID / "failure-manifest.json").exists()
