"""A routine build names its seat — and an unnamed seat is today, exactly.

Found 2026-09-11, while working out how to point a routine build at a
different coder for a controlled comparison. Forge named a model for
exactly one thing: the fix journey's legs, from ``conductor.seat``, which
ride the command line as ``--model <seat>``. A routine dispatch carried no
``--model`` at all, so the build system's own command line fell back to its
default — the literal string ``claude-sonnet-4-5-20250929``, a frontier
vendor's model NAME, which reaches a local model only because the estate's
proxy carries a wildcard row mapping ``claude-*`` to the workhorse seat.
Nothing was mis-served. But which model writes this factory's code was
decided by a line in a proxy's configuration file rather than by the
factory, and Rich's ruling on the day it was found was to fix it now
rather than be bitten later.

This module pins the three things that ruling turns into:

* a named routine seat rides the dispatch as ``--model <seat>``, in the
  place the fix journey's seat rides — appended after everything the argv
  already carried;
* **no seat named is today, byte for byte** — no flag, no empty flag,
  nothing reordered — so every deployed ``forge.yaml`` keeps working; and
* the fix journey is untouched: a routine seat never rides a fix-journey
  stage, whose seat comes from ``conductor.seat`` through the conductor's
  own adapter.

Every assertion here reads the WHOLE argv, element by element, rather than
looking a flag up in it: the property under test is not "the seat is
present somewhere" but "nothing else moved".

The doubles are the sibling modules' — one definition of "what a fake
runner looks like" for this dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.dispatchers.subprocess import (
    StageDispatchStatus,
    dispatch_subprocess_stage,
)
from forge.pipeline.forward_context_builder import ForwardContextBuilder
from forge.pipeline.mode_c_planner import FixTaskRef
from forge.pipeline.stage_taxonomy import StageClass

from tests.forge.pipeline.dispatchers.test_subprocess import (
    FakeStageLogReader,
    FakeStageLogWriter,
    FakeSubprocessRunner,
    FakeWorktreeAllowlist,
    _seed_approved,
)
from tests.forge.pipeline.dispatchers.test_subprocess_mode_c_argv import (
    FixJourneyRunner,
)


BUILD_ID = "build-FEAT-SEAT-20260911"
CORRELATION_ID = "corr-routine-seat-001"
WORKTREE_ROOT = "/work/build-FEAT-SEAT-20260911"
SEAT = "qwen3-coder-30b"


@pytest.fixture
def allowlist() -> FakeWorktreeAllowlist:
    return FakeWorktreeAllowlist(roots_by_build={BUILD_ID: WORKTREE_ROOT})


@pytest.fixture
def reader() -> FakeStageLogReader:
    return FakeStageLogReader()


@pytest.fixture
def builder(
    reader: FakeStageLogReader, allowlist: FakeWorktreeAllowlist
) -> ForwardContextBuilder:
    return ForwardContextBuilder(reader, allowlist)


@pytest.fixture
def writer() -> FakeStageLogWriter:
    return FakeStageLogWriter()


@pytest.fixture
def runner() -> FakeSubprocessRunner:
    return FakeSubprocessRunner()


async def _dispatch(
    *,
    stage: StageClass,
    builder: ForwardContextBuilder,
    allowlist: FakeWorktreeAllowlist,
    writer: FakeStageLogWriter,
    runner: FakeSubprocessRunner,
    **kwargs: Any,
):
    return await dispatch_subprocess_stage(
        stage,
        BUILD_ID,
        correlation_id=CORRELATION_ID,
        repo_path=Path(WORKTREE_ROOT),
        read_allowlist=[Path(WORKTREE_ROOT)],
        forward_context_builder=builder,
        worktree_allowlist=allowlist,
        stage_log_writer=writer,
        subprocess_runner=runner,
        **kwargs,
    )


def _seed_the_planning_chain(reader: FakeStageLogReader) -> None:
    """Approved rows enough that a planning stage carries real context.

    A seat that only rode an EMPTY argv would prove nothing about where it
    lands, so the per-feature cases below carry a ``--context`` pair and
    the assertions read the whole line.
    """
    _seed_approved(
        reader, build_id=BUILD_ID, stage=StageClass.PRODUCT_OWNER, text="charter"
    )
    _seed_approved(
        reader,
        build_id=BUILD_ID,
        stage=StageClass.ARCHITECT,
        text="architect output",
    )
    _seed_approved(
        reader,
        build_id=BUILD_ID,
        stage=StageClass.SYSTEM_ARCH,
        paths=(f"{WORKTREE_ROOT}/arch.md",),
    )
    _seed_approved(
        reader,
        build_id=BUILD_ID,
        stage=StageClass.SYSTEM_DESIGN,
        text="feature catalogue entry",
    )


#: Today's argv for a ``feature-spec`` dispatch of this build, with the
#: planning chain seeded — written out in full, because "today, byte for
#: byte" is the contract and a literal is the only honest way to state it.
TODAYS_FEATURE_SPEC_ARGV = [
    "--build-id",
    BUILD_ID,
    "--correlation-id",
    CORRELATION_ID,
    "--feature-id",
    "FEAT-1",
    "--context",
    "feature catalogue entry",
]

#: Today's argv for a ``system-arch`` dispatch — the shortest routine
#: shape there is: the two identifiers and nothing else.
TODAYS_SYSTEM_ARCH_ARGV = [
    "--build-id",
    BUILD_ID,
    "--correlation-id",
    CORRELATION_ID,
]


# ---------------------------------------------------------------------------
# Unnamed is today, exactly
# ---------------------------------------------------------------------------


class TestUnnamedIsTodayExactly:
    """No routine seat configured changes NO byte of the command line.

    This is the half that protects every ``forge.yaml`` already deployed:
    the lever is opt-in, so a factory that names no routine seat must
    dispatch exactly as it did before the lever existed, and the build
    system's own CLI default must still be what applies.
    """

    @pytest.mark.asyncio
    async def test_no_seat_leaves_the_feature_spec_argv_exactly_as_it_is(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        _seed_the_planning_chain(reader)

        result = await _dispatch(
            stage=StageClass.FEATURE_SPEC,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-1",
        )

        assert result.status is StageDispatchStatus.SUCCESS
        assert runner.calls[0]["args"] == TODAYS_FEATURE_SPEC_ARGV
        assert "--model" not in runner.calls[0]["args"]

    @pytest.mark.asyncio
    async def test_no_seat_leaves_the_shortest_routine_argv_exactly_as_it_is(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        await _dispatch(
            stage=StageClass.SYSTEM_ARCH,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
        )

        assert runner.calls[0]["args"] == TODAYS_SYSTEM_ARCH_ARGV

    @pytest.mark.asyncio
    async def test_an_explicit_none_is_the_same_as_not_passing_one(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        _seed_the_planning_chain(reader)

        await _dispatch(
            stage=StageClass.FEATURE_SPEC,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-1",
            routine_seat=None,
        )

        assert runner.calls[0]["args"] == TODAYS_FEATURE_SPEC_ARGV

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    @pytest.mark.asyncio
    async def test_a_blank_seat_reads_as_absent_not_as_an_empty_token(
        self,
        blank: str,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """``--model ''`` would be a command line naming a nothing.

        Config load strips a blank seat to absent; the dispatcher strips
        again so a value that arrived some other way can never put an
        empty token on the command line.
        """
        _seed_the_planning_chain(reader)

        await _dispatch(
            stage=StageClass.FEATURE_SPEC,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-1",
            routine_seat=blank,
        )

        assert runner.calls[0]["args"] == TODAYS_FEATURE_SPEC_ARGV


# ---------------------------------------------------------------------------
# A named seat rides the dispatch
# ---------------------------------------------------------------------------


class TestANamedSeatRidesTheDispatch:
    """The factory names the model that writes its code."""

    @pytest.mark.asyncio
    async def test_the_seat_appends_exactly_two_tokens_and_moves_nothing(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        _seed_the_planning_chain(reader)

        result = await _dispatch(
            stage=StageClass.FEATURE_SPEC,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-1",
            routine_seat=SEAT,
        )

        assert result.status is StageDispatchStatus.SUCCESS
        assert runner.calls[0]["args"] == TODAYS_FEATURE_SPEC_ARGV + [
            "--model",
            SEAT,
        ]

    @pytest.mark.parametrize(
        ("stage", "subcommand", "feature_id"),
        [
            (StageClass.SYSTEM_ARCH, "system-arch", None),
            (StageClass.SYSTEM_DESIGN, "system-design", None),
            (StageClass.FEATURE_SPEC, "feature-spec", "FEAT-1"),
            (StageClass.FEATURE_PLAN, "feature-plan", "FEAT-1"),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_routine_stage_carries_the_seat(
        self,
        stage: StageClass,
        subcommand: str,
        feature_id: str | None,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """All four planning stages, each against its own baseline.

        The baseline is taken from a second dispatch of the SAME stage
        with no seat, so the comparison is this stage's real argv rather
        than a literal copied from another one.
        """
        _seed_the_planning_chain(reader)
        baseline_runner = FakeSubprocessRunner()

        await _dispatch(
            stage=stage,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=baseline_runner,
            feature_id=feature_id,
        )
        await _dispatch(
            stage=stage,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id=feature_id,
            routine_seat=SEAT,
        )

        baseline = baseline_runner.calls[0]["args"]
        assert runner.calls[0]["args"] == baseline + ["--model", SEAT]
        assert runner.calls[0]["subcommand"] == subcommand

    @pytest.mark.asyncio
    async def test_a_seat_with_whitespace_around_it_rides_stripped(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """``--model ' qwen3 '`` would name a different seat on the wire."""
        await _dispatch(
            stage=StageClass.SYSTEM_ARCH,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            routine_seat=f"  {SEAT}  ",
        )

        assert runner.calls[0]["args"] == TODAYS_SYSTEM_ARCH_ARGV + [
            "--model",
            SEAT,
        ]

    @pytest.mark.asyncio
    async def test_the_seat_comes_before_a_callers_own_extra_args(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """The same order the fix journey's seat keeps: seat first.

        A caller threading its own flags still gets them last, so the two
        token positions the seat occupies are stable however the call is
        made.
        """
        await _dispatch(
            stage=StageClass.SYSTEM_ARCH,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            routine_seat=SEAT,
            extra_args=["--retry"],
        )

        assert runner.calls[0]["args"] == TODAYS_SYSTEM_ARCH_ARGV + [
            "--model",
            SEAT,
            "--retry",
        ]

    @pytest.mark.asyncio
    async def test_the_seat_changes_nothing_else_about_the_dispatch(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """Same subcommand, same cwd, same allowlist, same context paths.

        The seat is two tokens on a command line and nothing else — in
        particular it must not disturb the resolved ``--context`` paths,
        which are the other half of what a stage is given.
        """
        _seed_the_planning_chain(reader)
        baseline_runner = FakeSubprocessRunner()

        await _dispatch(
            stage=StageClass.SYSTEM_DESIGN,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=baseline_runner,
        )
        await _dispatch(
            stage=StageClass.SYSTEM_DESIGN,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            routine_seat=SEAT,
        )

        baseline = dict(baseline_runner.calls[0])
        seated = dict(runner.calls[0])
        assert seated.pop("args") == baseline.pop("args") + ["--model", SEAT]
        assert seated == baseline


# ---------------------------------------------------------------------------
# The fix journey is untouched
# ---------------------------------------------------------------------------


class TestTheFixJourneyIsUntouched:
    """Its seat, its argv and its flags stay exactly as they are.

    The fix journey's legs take their seat from ``conductor.seat``,
    appended by the conductor's own adapter as ``extra_args``. A routine
    seat must never reach them: two ``--model`` pairs on one command line
    would be two answers to one question, and the pin that the fix
    journey's seat stays first and appends exactly two tokens would stop
    reading true.
    """

    @pytest.fixture
    def fix_runner(self) -> FixJourneyRunner:
        return FixJourneyRunner()

    @pytest.mark.asyncio
    async def test_a_review_dispatch_is_the_same_with_or_without_a_routine_seat(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        fix_runner: FixJourneyRunner,
    ) -> None:
        baseline_runner = FixJourneyRunner()

        await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=baseline_runner,
            task_id="TASK-FIX007",
        )
        result = await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=fix_runner,
            task_id="TASK-FIX007",
            routine_seat=SEAT,
        )

        assert result.status is StageDispatchStatus.SUCCESS
        assert fix_runner.calls[0]["args"] == [
            "--build-id",
            BUILD_ID,
            "--correlation-id",
            CORRELATION_ID,
            "--task-id",
            "TASK-FIX007",
        ]
        assert fix_runner.calls[0]["args"] == baseline_runner.calls[0]["args"]

    @pytest.mark.asyncio
    async def test_a_work_dispatch_keeps_the_conductors_seat_and_budgets(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        fix_runner: FixJourneyRunner,
    ) -> None:
        """The conductor's own extras arrive as they always did.

        ``extra_args`` here is the shape the conductor adapter builds —
        the seat first, then the leg budgets. A routine seat configured at
        the same time must add nothing to it.
        """
        conductors_extras = [
            "--model",
            "gpt-oss-120b",
            "--max-turns",
            "3",
            "--sdk-timeout",
            "420",
        ]

        await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=fix_runner,
            fix_task=FixTaskRef(
                fix_task_id="TASK-FIX007-A", review_history_index=0
            ),
            routine_seat=SEAT,
            extra_args=conductors_extras,
        )

        argv = fix_runner.calls[0]["args"]
        assert argv == [
            "--build-id",
            BUILD_ID,
            "--correlation-id",
            CORRELATION_ID,
            "--task-id",
            "TASK-FIX007-A",
        ] + conductors_extras
        assert argv.count("--model") == 1
        assert SEAT not in argv
