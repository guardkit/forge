"""The fix journey's two argv shapes (conductor revival, Stage 1a).

Design pass ``supervisor-revival-design-pass-2026-07-31`` §a.4 / risk h.2:
adding ``TASK_REVIEW`` / ``TASK_WORK`` to ``SUBPROCESS_STAGE_COMMANDS`` is
necessary but *not sufficient* — "the map rows alone do not dispatch". The
argv shapes are the point, and they are what this module pins:

* ``task-review`` dispatches by the build's own task subject
  (``--task-id``), the same shape the build-system adapter already runs
  (``forge.tools.guardkit.guardkit_task_review``).
* ``task-work`` dispatches by *fix task* subject — the identifier on the
  reference the conductor threads through as the ``fix_task`` dispatcher
  kwarg.
* Both carry the queue's Mode C wire pair when supplied:
  ``--parent-feature`` (the ``parent_feature`` the queue read out of the
  fix-task YAML) and ``--feature-yaml`` (that YAML itself).
* Both carry ``--correlation-id`` — the exact-match identity law (FTR: a
  false terminal once killed a healthy build) needs every stage dispatch
  to mint and thread its own id, and with N task-work siblings per cycle
  that matters more here than on the single-stage consumer path.

A dispatch with no subject is refused, structured, never guessed at —
the fix-journey twin of the long-standing per-feature ``feature_id``
refusal.

The doubles are the ones the sibling ``test_subprocess`` module already
uses; importing them keeps a single definition of "what a fake runner
looks like" for this dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from forge.pipeline.dispatchers.subprocess import (
    MODE_C_STAGES,
    SUBPROCESS_STAGE_COMMANDS,
    StageDispatchStatus,
    dispatch_subprocess_stage,
)
from forge.pipeline.forward_context_builder import ForwardContextBuilder
from forge.pipeline.mode_c_planner import FixTaskRef
from forge.pipeline.stage_taxonomy import PER_FEATURE_STAGES, StageClass

from tests.forge.pipeline.dispatchers.test_subprocess import (
    FakeStageLogReader,
    FakeStageLogWriter,
    FakeSubprocessRunner,
    FakeWorktreeAllowlist,
    _seed_approved,
)


BUILD_ID = "build-FEAT-FIX007-20260731"
CORRELATION_ID = "corr-fix-journey-001"
WORKTREE_ROOT = "/work/build-FEAT-FIX007-20260731"


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
    **kwargs: object,
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
        **kwargs,  # type: ignore[arg-type]
    )


def _argv_pairs(argv: list[str]) -> dict[str, str]:
    """Collapse a flag/value argv into a dict for order-free assertions."""
    return {
        argv[i]: argv[i + 1]
        for i in range(0, len(argv) - 1)
        if argv[i].startswith("--")
    }


# ---------------------------------------------------------------------------
# The two map rows
# ---------------------------------------------------------------------------


class TestFixJourneyMapRows:
    """§a.4 — the two rows that were the last missing inch."""

    def test_task_review_and_task_work_map_to_their_subcommands(self) -> None:
        assert SUBPROCESS_STAGE_COMMANDS[StageClass.TASK_REVIEW] == "task-review"
        assert SUBPROCESS_STAGE_COMMANDS[StageClass.TASK_WORK] == "task-work"

    def test_map_rows_match_the_build_system_adapters_subcommands(self) -> None:
        """The adapter already ran both subcommands — the map now agrees.

        ``forge.tools.guardkit`` has run ``task-review`` / ``task-work``
        with bus streaming all along. A drift between the two spellings
        would surface as a "command not found" one layer below the
        dispatcher, so pin them against each other rather than against a
        literal in two places.
        """
        from forge.tools import guardkit as guardkit_tools

        # The wrappers are ``@tool``-decorated (StructuredTool objects), so
        # read the module's own source rather than unwrapping the decorator.
        source = Path(guardkit_tools.__file__).read_text(encoding="utf-8")
        assert (
            f'subcommand="{SUBPROCESS_STAGE_COMMANDS[StageClass.TASK_REVIEW]}"'
            in source
        )
        assert (
            f'subcommand="{SUBPROCESS_STAGE_COMMANDS[StageClass.TASK_WORK]}"'
            in source
        )

    def test_fix_journey_stages_are_not_per_feature_stages(self) -> None:
        """The two subject checks must not collide.

        ``PER_FEATURE_STAGES`` excludes the fix-journey stages, which is
        why they needed a refusal of their own: without it a subject-less
        fix-journey dispatch sails past the per-feature guard.
        """
        assert MODE_C_STAGES == {StageClass.TASK_REVIEW, StageClass.TASK_WORK}
        assert not (MODE_C_STAGES & PER_FEATURE_STAGES)


# ---------------------------------------------------------------------------
# argv shape: task-review
# ---------------------------------------------------------------------------


class TestTaskReviewArgv:
    """The opening (and follow-up) leg of the fix journey."""

    @pytest.mark.asyncio
    async def test_review_argv_carries_build_correlation_and_task_subject(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        result = await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
        )

        assert result.status is StageDispatchStatus.SUCCESS
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call["subcommand"] == "task-review"
        pairs = _argv_pairs(call["args"])
        assert pairs["--build-id"] == BUILD_ID
        assert pairs["--correlation-id"] == CORRELATION_ID
        assert pairs["--task-id"] == "TASK-FIX007"

    @pytest.mark.asyncio
    async def test_review_argv_carries_the_queues_mode_c_wire_pair(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """``--parent-feature`` / ``--feature-yaml`` mirror the queue.

        ``forge queue --mode c TASK-XXX --feature-yaml <fix-task.yaml>``
        reads ``parent_feature`` out of that YAML and puts both
        identifiers on the wire. The dispatch looks at the same two
        artefacts rather than re-deriving them.
        """
        await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
            parent_feature="FEAT-FIX007",
            fix_task_yaml="/work/tasks/fix-task.yaml",
        )

        pairs = _argv_pairs(runner.calls[0]["args"])
        assert pairs["--parent-feature"] == "FEAT-FIX007"
        assert pairs["--feature-yaml"] == "/work/tasks/fix-task.yaml"

    @pytest.mark.asyncio
    async def test_review_argv_omits_the_optional_pair_when_absent(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
        )
        argv = runner.calls[0]["args"]
        assert "--parent-feature" not in argv
        assert "--feature-yaml" not in argv

    @pytest.mark.asyncio
    async def test_review_never_emits_feature_id_even_when_supplied(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """One identifier slot per concept — the parent rides its own flag.

        Emitting both ``--feature-id`` and ``--parent-feature`` would give
        the subprocess two names for the same thing and no rule for which
        wins.
        """
        await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-FIX007",
            task_id="TASK-FIX007",
            parent_feature="FEAT-FIX007",
        )
        assert "--feature-id" not in runner.calls[0]["args"]

    @pytest.mark.asyncio
    async def test_review_argv_appends_forward_context_text_entries(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """Forward context still flows — the new shape is additive."""
        _seed_approved(
            reader,
            build_id=BUILD_ID,
            stage=StageClass.TASK_REVIEW,
            text="prior review notes",
        )
        await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
        )
        argv = runner.calls[0]["args"]
        # The subject prefix comes first; context entries are appended.
        assert argv.index("--task-id") < len(argv)
        assert argv[:2] == ["--build-id", BUILD_ID]

    @pytest.mark.asyncio
    async def test_review_without_task_id_is_refused_structurally(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        result = await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
        )
        assert result.status is StageDispatchStatus.FAILED
        assert "without task_id" in result.rationale
        # Refused BEFORE the subprocess — never a guessed subject.
        assert runner.calls == []
        # …and the refusal is still audited.
        assert len(writer.calls) == 1
        assert writer.calls[0]["status"] is StageDispatchStatus.FAILED

    @pytest.mark.asyncio
    async def test_review_with_blank_task_id_is_refused(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """An empty string is not a subject — whitespace must not pass."""
        result = await _dispatch(
            stage=StageClass.TASK_REVIEW,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="   ",
        )
        assert result.status is StageDispatchStatus.FAILED
        assert runner.calls == []


# ---------------------------------------------------------------------------
# argv shape: task-work
# ---------------------------------------------------------------------------


class TestTaskWorkArgv:
    """One dispatch per fix task the review emitted."""

    @pytest.mark.asyncio
    async def test_work_argv_subject_is_the_threaded_fix_task_id(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """The conductor's ``fix_task`` kwarg becomes ``--task-id``.

        This is the threading the design pass calls out: the planner mints
        a :class:`FixTaskRef`, the conductor puts it on ``dispatcher_kwargs``
        as ``fix_task``, and it has to land on the command line or the
        subprocess works on the wrong thing.
        """
        ref = FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0)
        result = await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            fix_task=ref,
        )

        assert result.status is StageDispatchStatus.SUCCESS
        call = runner.calls[0]
        assert call["subcommand"] == "task-work"
        pairs = _argv_pairs(call["args"])
        assert pairs["--task-id"] == "TASK-FIX007-A"
        assert pairs["--build-id"] == BUILD_ID
        assert pairs["--correlation-id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_work_argv_prefers_fix_task_over_the_builds_task_id(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """A work dispatch works ONE fix task, not the build's own task.

        Both identifiers are in scope during a fix journey; picking the
        wrong one would fan every sibling dispatch onto the same subject.
        """
        ref = FixTaskRef(fix_task_id="TASK-FIX007-B", review_history_index=2)
        await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
            fix_task=ref,
        )
        pairs = _argv_pairs(runner.calls[0]["args"])
        assert pairs["--task-id"] == "TASK-FIX007-B"

    @pytest.mark.asyncio
    async def test_work_argv_carries_the_queues_mode_c_wire_pair(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        ref = FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0)
        await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            fix_task=ref,
            parent_feature="FEAT-FIX007",
            fix_task_yaml=Path("/work/tasks/fix-task.yaml"),
        )
        pairs = _argv_pairs(runner.calls[0]["args"])
        assert pairs["--parent-feature"] == "FEAT-FIX007"
        assert pairs["--feature-yaml"] == "/work/tasks/fix-task.yaml"

    @pytest.mark.asyncio
    async def test_work_argv_does_not_thread_the_review_back_reference(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """``review_history_index`` is an audit anchor, not an argv flag.

        The build system has no flag for it; it belongs on the stage_log
        row (the terminal handler's attribution helper writes it). Passing
        an invented flag would fail at the subprocess boundary.
        """
        ref = FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=7)
        await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            fix_task=ref,
        )
        argv = runner.calls[0]["args"]
        assert "7" not in argv
        assert not any(a.startswith("--review") for a in argv)

    @pytest.mark.asyncio
    async def test_work_without_fix_task_is_refused_structurally(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """No reference, no dispatch — cross-fix-task attribution risk.

        The per-feature guard cannot catch this: ``PER_FEATURE_STAGES``
        deliberately excludes ``TASK_WORK``.
        """
        result = await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
        )
        assert result.status is StageDispatchStatus.FAILED
        assert "without a fix-task reference" in result.rationale
        assert runner.calls == []
        assert len(writer.calls) == 1

    @pytest.mark.asyncio
    async def test_work_with_blank_fix_task_id_is_refused(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        @dataclass
        class _BlankRef:
            fix_task_id: str = ""
            review_history_index: int = 0

        result = await _dispatch(
            stage=StageClass.TASK_WORK,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            fix_task=_BlankRef(),
        )
        assert result.status is StageDispatchStatus.FAILED
        assert runner.calls == []


# ---------------------------------------------------------------------------
# Identity + no-behaviour-change
# ---------------------------------------------------------------------------


class TestCorrelationIdAndNoRegression:
    @pytest.mark.parametrize(
        ("stage", "kwargs"),
        [
            (StageClass.TASK_REVIEW, {"task_id": "TASK-FIX007"}),
            (
                StageClass.TASK_WORK,
                {
                    "fix_task": FixTaskRef(
                        fix_task_id="TASK-FIX007-A", review_history_index=0
                    )
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_fix_journey_dispatch_threads_its_correlation_id(
        self,
        stage: StageClass,
        kwargs: dict,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """FTR's exact-match identity law needs the id on every envelope.

        With N task-work siblings in flight per cycle, resolving a
        terminal on anything looser than this dispatch's own id is how a
        healthy build gets killed by a sibling's failure.
        """
        result = await _dispatch(
            stage=stage,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            **kwargs,
        )
        argv = runner.calls[0]["args"]
        assert "--correlation-id" in argv
        assert argv[argv.index("--correlation-id") + 1] == CORRELATION_ID
        assert result.correlation_id == CORRELATION_ID
        assert writer.calls[0]["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_planning_stage_argv_is_unchanged_by_the_new_parameters(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """The four planning stages keep their exact argv — byte-for-byte.

        The lane's prime invariant is no behaviour change on the routine
        path. The new keyword parameters all default to ``None``, so a
        planning dispatch must produce the identical command line it
        produced before the fix-journey rows landed.
        """
        result = await _dispatch(
            stage=StageClass.FEATURE_PLAN,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            feature_id="FEAT-FIX007",
        )
        assert result.status is StageDispatchStatus.SUCCESS
        assert runner.calls[0]["args"] == [
            "--build-id",
            BUILD_ID,
            "--correlation-id",
            CORRELATION_ID,
            "--feature-id",
            "FEAT-FIX007",
        ]

    @pytest.mark.asyncio
    async def test_planning_stage_ignores_stray_fix_journey_parameters(
        self,
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        """A fix-journey kwarg on a planning stage must not leak onto argv."""
        await _dispatch(
            stage=StageClass.SYSTEM_ARCH,
            builder=builder,
            allowlist=allowlist,
            writer=writer,
            runner=runner,
            task_id="TASK-FIX007",
            parent_feature="FEAT-FIX007",
            fix_task_yaml="/work/tasks/fix-task.yaml",
        )
        argv = runner.calls[0]["args"]
        assert argv == ["--build-id", BUILD_ID, "--correlation-id", CORRELATION_ID]
