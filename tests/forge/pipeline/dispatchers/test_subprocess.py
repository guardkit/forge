"""Tests for ``forge.pipeline.dispatchers.subprocess`` (TASK-MAG7-008).

Validates :func:`dispatch_subprocess_stage` — the thin composition layer
between FEAT-FORGE-005's GuardKit subprocess engine and TASK-MAG7-006's
:class:`ForwardContextBuilder`. The dispatcher must:

- Map each of the four subprocess stages (``SYSTEM_ARCH``, ``SYSTEM_DESIGN``,
  ``FEATURE_SPEC``, ``FEATURE_PLAN``) to its GuardKit slash-command
  subcommand and invoke the runner with the right argv.
- Thread ``correlation_id`` onto the subprocess command line.
- Record the dispatch outcome in ``stage_log`` with feature attribution.
- Convert subprocess hard-stops / non-zero exits / out-of-tree artefact
  paths into structured :class:`StageDispatchResult` failures rather than
  raising.

Both reader/writer Protocols are satisfied by in-memory test doubles so
the suite runs without SQLite, the FEAT-FORGE-005 allowlist subsystem,
or a real GuardKit binary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.pipeline.dispatchers.subprocess import (
    SUBPROCESS_STAGE_COMMANDS,
    StageDispatchResult,
    StageDispatchStatus,
    dispatch_subprocess_stage,
)
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ContextEntry,
    ForwardContextBuilder,
    StageLogReader,
    WorktreeAllowlist,
)
from forge.pipeline.stage_taxonomy import StageClass


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStageLogReader:
    """In-memory :class:`StageLogReader` for the forward-context builder."""

    entries: dict[
        tuple[str, StageClass, str | None], ApprovedStageEntry
    ] = field(default_factory=dict)

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:
        return self.entries.get((build_id, stage, feature_id))


@dataclass
class FakeWorktreeAllowlist:
    """Prefix-based :class:`WorktreeAllowlist`.

    Production wires the FEAT-FORGE-005 allowlist; the prefix model is
    sufficient because the production check answers the same yes/no
    question.
    """

    roots_by_build: dict[str, str] = field(default_factory=dict)

    def is_allowed(self, build_id: str, path: str) -> bool:
        root = self.roots_by_build.get(build_id)
        if root is None:
            return False
        return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass
class FakeStageLogWriter:
    """Captures :meth:`record_dispatch` calls so tests can inspect them."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_record: Exception | None = None

    def record_dispatch(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.raise_on_record is not None:
            raise self.raise_on_record


@dataclass
class FakeSubprocessRunner:
    """Records runner kwargs and returns a configured :class:`GuardKitResult`.

    By default returns a successful empty-artefacts result; tests
    override either the ``result`` attribute or ``raise_exc`` to drive
    failure paths.
    """

    result: GuardKitResult | None = None
    raise_exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.result is not None:
            return self.result
        return GuardKitResult(
            status="success",
            subcommand=kwargs.get("subcommand", "?"),
            artefacts=[],
            duration_secs=0.42,
            stdout_tail="",
            stderr=None,
            exit_code=0,
            warnings=[],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def build_id() -> str:
    return "build-FEAT-X-20260426"


@pytest.fixture
def correlation_id() -> str:
    return "corr-abc-123"


@pytest.fixture
def worktree_root() -> str:
    return "/work/build-FEAT-X-20260426"


@pytest.fixture
def repo_path(worktree_root: str) -> Path:
    return Path(worktree_root)


@pytest.fixture
def read_allowlist(worktree_root: str) -> list[Path]:
    return [Path(worktree_root)]


@pytest.fixture
def allowlist(
    worktree_root: str, build_id: str
) -> FakeWorktreeAllowlist:
    return FakeWorktreeAllowlist(roots_by_build={build_id: worktree_root})


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


def _seed_approved(
    reader: FakeStageLogReader,
    *,
    build_id: str,
    stage: StageClass,
    feature_id: str | None = None,
    text: str | None = None,
    paths: tuple[str, ...] = (),
) -> None:
    """Helper — seed a single approved row in the fake reader."""
    reader.entries[(build_id, stage, feature_id)] = ApprovedStageEntry(
        gate_decision="approved",
        artefact_paths=paths,
        artefact_text=text,
    )


# ---------------------------------------------------------------------------
# AC: stage → slash-command mapping
# ---------------------------------------------------------------------------


class TestStageCommandMapping:
    """AC: SYSTEM_ARCH/SYSTEM_DESIGN/FEATURE_SPEC/FEATURE_PLAN map correctly."""

    def test_subprocess_stage_commands_covers_exactly_six_stages(self) -> None:
        """The four planning stages plus the fix journey's two.

        The two fix-journey rows landed with the conductor's revival
        (Stage 1a, design pass §a.4). The set is asserted exhaustively so
        a stage added to :class:`StageClass` without a deliberate decision
        about whether it is a subprocess stage fails here rather than at
        the first dispatch.
        """
        assert set(SUBPROCESS_STAGE_COMMANDS) == {
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
            StageClass.TASK_REVIEW,
            StageClass.TASK_WORK,
        }

    def test_subprocess_stage_commands_have_expected_subcommand_tokens(
        self,
    ) -> None:
        assert SUBPROCESS_STAGE_COMMANDS[StageClass.SYSTEM_ARCH] == "system-arch"
        assert (
            SUBPROCESS_STAGE_COMMANDS[StageClass.SYSTEM_DESIGN]
            == "system-design"
        )
        assert (
            SUBPROCESS_STAGE_COMMANDS[StageClass.FEATURE_SPEC] == "feature-spec"
        )
        assert (
            SUBPROCESS_STAGE_COMMANDS[StageClass.FEATURE_PLAN] == "feature-plan"
        )

    @pytest.mark.parametrize(
        ("stage", "expected_subcommand", "feature_id"),
        [
            (StageClass.SYSTEM_ARCH, "system-arch", None),
            (StageClass.SYSTEM_DESIGN, "system-design", None),
            (StageClass.FEATURE_SPEC, "feature-spec", "FEAT-1"),
            (StageClass.FEATURE_PLAN, "feature-plan", "FEAT-1"),
        ],
    )
    @pytest.mark.asyncio
    async def test_dispatch_invokes_runner_with_mapped_subcommand(
        self,
        stage: StageClass,
        expected_subcommand: str,
        feature_id: str | None,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
        reader: FakeStageLogReader,
    ) -> None:
        # Seed enough approved-rows that build_for() returns *something*
        # for the per-feature stages — non-per-feature stages don't
        # require a producer because PRODUCT_OWNER → ARCHITECT chain
        # isn't in the subprocess set anyway.
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.PRODUCT_OWNER,
            text="charter",
        )
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.ARCHITECT,
            text="architect output",
        )
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.SYSTEM_ARCH,
            paths=(f"/work/{build_id}/arch.md",),
        )
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.SYSTEM_DESIGN,
            text="feature catalogue entry",
        )
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.FEATURE_SPEC,
            feature_id=feature_id,
            paths=(f"/work/{build_id}/spec-{feature_id}.md",),
        )

        result = await dispatch_subprocess_stage(
            stage,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
            feature_id=feature_id,
        )

        assert result.status is StageDispatchStatus.SUCCESS
        assert result.subcommand == expected_subcommand
        assert len(runner.calls) == 1
        assert runner.calls[0]["subcommand"] == expected_subcommand


# ---------------------------------------------------------------------------
# AC: ForwardContextBuilder integration
# ---------------------------------------------------------------------------


class TestForwardContextIntegration:
    """AC: --context flags are sourced from ForwardContextBuilder.build_for."""

    @pytest.mark.asyncio
    async def test_text_context_is_threaded_onto_argv_as_context_flag(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
        reader: FakeStageLogReader,
    ) -> None:
        # SYSTEM_ARCH consumes ARCHITECT (text). Seed approved architect
        # output so the builder emits a single text ContextEntry.
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.ARCHITECT,
            text="architect approved output",
        )

        await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        runner_args: list[str] = runner.calls[0]["args"]
        # The context value appears as a "--context" flag pair on argv.
        assert "--context" in runner_args
        idx = runner_args.index("--context")
        assert runner_args[idx + 1] == "architect approved output"

    @pytest.mark.asyncio
    async def test_path_context_is_threaded_via_extra_context_paths(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        worktree_root: str,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
        reader: FakeStageLogReader,
    ) -> None:
        # FEATURE_PLAN consumes FEATURE_SPEC (path-kind, per-feature).
        spec_path = f"{worktree_root}/spec-FEAT-1.md"
        _seed_approved(
            reader,
            build_id=build_id,
            stage=StageClass.FEATURE_SPEC,
            feature_id="FEAT-1",
            paths=(spec_path,),
        )

        await dispatch_subprocess_stage(
            StageClass.FEATURE_PLAN,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
            feature_id="FEAT-1",
        )

        # Path-kind context flows through extra_context_paths so the
        # GuardKit context resolver merges it with manifest entries.
        assert runner.calls[0]["extra_context_paths"] == [spec_path]


# ---------------------------------------------------------------------------
# AC: correlation_id threading
# ---------------------------------------------------------------------------


class TestCorrelationIdThreading:
    """AC: correlation_id appears on every subprocess envelope and stage_log."""

    @pytest.mark.asyncio
    async def test_correlation_id_appears_on_subprocess_argv(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        argv = runner.calls[0]["args"]
        assert "--correlation-id" in argv
        assert argv[argv.index("--correlation-id") + 1] == correlation_id

    @pytest.mark.asyncio
    async def test_correlation_id_recorded_on_stage_log_row(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert len(writer.calls) == 1
        assert writer.calls[0]["correlation_id"] == correlation_id


# ---------------------------------------------------------------------------
# AC: stage_log artefact paths attributed by feature_id
# ---------------------------------------------------------------------------


class TestStageLogArtefactRecording:
    """AC: artefact paths recorded with feature attribution (Group G)."""

    @pytest.mark.asyncio
    async def test_artefact_paths_recorded_in_stage_log(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        worktree_root: str,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        artefact_path = f"{worktree_root}/output.md"
        runner.result = GuardKitResult(
            status="success",
            subcommand="system-arch",
            artefacts=[artefact_path],
            duration_secs=1.5,
            exit_code=0,
        )

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.artefact_paths == (artefact_path,)
        assert writer.calls[0]["artefact_paths"] == (artefact_path,)
        assert writer.calls[0]["feature_id"] is None

    @pytest.mark.asyncio
    async def test_per_feature_artefact_paths_attributed_by_feature_id(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        worktree_root: str,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        spec_path = f"{worktree_root}/spec-FEAT-7.md"
        runner.result = GuardKitResult(
            status="success",
            subcommand="feature-spec",
            artefacts=[spec_path],
            duration_secs=2.0,
            exit_code=0,
        )

        result = await dispatch_subprocess_stage(
            StageClass.FEATURE_SPEC,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
            feature_id="FEAT-7",
        )

        assert result.feature_id == "FEAT-7"
        assert writer.calls[0]["feature_id"] == "FEAT-7"
        assert writer.calls[0]["artefact_paths"] == (spec_path,)


# ---------------------------------------------------------------------------
# AC: hard-stop / non-zero exit → FAILED with structured rationale
# ---------------------------------------------------------------------------


class TestSubprocessFailureMapping:
    """AC: hard-stop / non-zero exit converts to FAILED, never raises."""

    @pytest.mark.asyncio
    async def test_subprocess_hard_stop_converts_to_failed_result(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        runner.result = GuardKitResult(
            status="failed",
            subcommand="system-arch",
            artefacts=[],
            duration_secs=0.1,
            stdout_tail="",
            stderr="hard stop: invariant violation",
            exit_code=2,
            warnings=[
                GuardKitWarning(
                    code="hard_stop", message="invariant violation"
                )
            ],
        )

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.status is StageDispatchStatus.FAILED
        assert result.exit_code == 2
        assert "system-arch" in result.rationale
        assert "hard stop" in result.rationale.lower() or (
            "invariant violation" in result.rationale
        )

    @pytest.mark.asyncio
    async def test_runner_internal_exception_converts_to_failed(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        runner.raise_exc = RuntimeError("subprocess wrapper crashed")

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_DESIGN,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.status is StageDispatchStatus.FAILED
        assert "RuntimeError" in result.rationale
        assert "subprocess wrapper crashed" in result.rationale

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        runner.raise_exc = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await dispatch_subprocess_stage(
                StageClass.SYSTEM_ARCH,
                build_id,
                correlation_id=correlation_id,
                repo_path=repo_path,
                read_allowlist=read_allowlist,
                forward_context_builder=builder,
                worktree_allowlist=allowlist,
                stage_log_writer=writer,
                subprocess_runner=runner,
            )

    @pytest.mark.asyncio
    async def test_feature_spec_failure_records_feature_spec_rationale(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        # Group C @negative: /feature-spec failure must surface a
        # rationale the supervisor can pattern-match to halt that
        # feature's inner loop.
        runner.result = GuardKitResult(
            status="failed",
            subcommand="feature-spec",
            artefacts=[],
            duration_secs=0.5,
            stdout_tail="",
            stderr="invalid acceptance criteria block",
            exit_code=1,
            warnings=[],
        )

        result = await dispatch_subprocess_stage(
            StageClass.FEATURE_SPEC,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
            feature_id="FEAT-1",
        )

        assert result.status is StageDispatchStatus.FAILED
        # Failed-spec rationale must be flagged so the supervisor can
        # detect feature-spec failures specifically.
        assert "feature-spec" in result.rationale
        assert "invalid acceptance criteria block" in result.rationale

    @pytest.mark.asyncio
    async def test_timeout_status_converts_to_failed_result(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        runner.result = GuardKitResult(
            status="timeout",
            subcommand="system-design",
            artefacts=[],
            duration_secs=600.0,
            exit_code=-1,
            stderr="timed out",
        )

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_DESIGN,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.status is StageDispatchStatus.FAILED
        assert result.exit_code == -1


# ---------------------------------------------------------------------------
# AC: artefact path outside allowlist refused
# ---------------------------------------------------------------------------


class TestAllowlistEnforcement:
    """AC: Subprocess artefact paths outside worktree allowlist are refused."""

    @pytest.mark.asyncio
    async def test_artefact_path_outside_allowlist_refused(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        worktree_root: str,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        # Subprocess returns an artefact that resolves OUTSIDE the
        # worktree allowlist — a misbehaving GuardKit that bypassed its
        # own check. The dispatcher must refuse the dispatch.
        bogus = "/etc/passwd"
        runner.result = GuardKitResult(
            status="success",
            subcommand="system-arch",
            artefacts=[bogus],
            duration_secs=0.3,
            exit_code=0,
        )

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.status is StageDispatchStatus.FAILED
        assert bogus in result.rationale
        # Rejected paths are not surfaced on the result's artefact_paths.
        assert bogus not in result.artefact_paths

    @pytest.mark.asyncio
    async def test_partial_allowlist_rejection_surfaces_failed(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        worktree_root: str,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        good = f"{worktree_root}/ok.md"
        bad = "/tmp/escape.md"
        runner.result = GuardKitResult(
            status="success",
            subcommand="system-arch",
            artefacts=[good, bad],
            duration_secs=0.1,
            exit_code=0,
        )

        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        # Even one bad path forces the dispatch to FAILED — the operator
        # cannot trust an artefact set with an out-of-tree element.
        assert result.status is StageDispatchStatus.FAILED
        assert good in result.artefact_paths
        assert bad not in result.artefact_paths
        assert bad in result.rationale


# ---------------------------------------------------------------------------
# Misuse: per-feature stage without feature_id; unsupported stage
# ---------------------------------------------------------------------------


class TestProgrammaticGuards:
    """AC: misuse cases (wrong stage, missing feature_id) are surfaced cleanly."""

    @pytest.mark.asyncio
    async def test_unsupported_stage_raises_value_error(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        # PRODUCT_OWNER is a specialist stage, not a subprocess stage.
        # Calling the subprocess dispatcher with it is a programming
        # error, not a runtime condition — hence ValueError, not FAILED.
        with pytest.raises(ValueError, match="not a subprocess stage"):
            await dispatch_subprocess_stage(
                StageClass.PRODUCT_OWNER,
                build_id,
                correlation_id=correlation_id,
                repo_path=repo_path,
                read_allowlist=read_allowlist,
                forward_context_builder=builder,
                worktree_allowlist=allowlist,
                stage_log_writer=writer,
                subprocess_runner=runner,
            )

    @pytest.mark.asyncio
    async def test_per_feature_stage_without_feature_id_returns_failed(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        result = await dispatch_subprocess_stage(
            StageClass.FEATURE_SPEC,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
            feature_id=None,
        )

        assert result.status is StageDispatchStatus.FAILED
        assert "feature_id" in result.rationale
        # Runner was never called — we refused before dispatch.
        assert runner.calls == []
        # Stage_log row IS written for the refusal so the audit trail
        # captures the misuse.
        assert len(writer.calls) == 1
        assert writer.calls[0]["status"] is StageDispatchStatus.FAILED


# ---------------------------------------------------------------------------
# Robustness: writer raising must not break the dispatch contract
# ---------------------------------------------------------------------------


class TestWriterRobustness:
    """AC: dispatcher contract is "never raises past the boundary"."""

    @pytest.mark.asyncio
    async def test_writer_exception_does_not_propagate(
        self,
        build_id: str,
        correlation_id: str,
        repo_path: Path,
        read_allowlist: list[Path],
        builder: ForwardContextBuilder,
        allowlist: FakeWorktreeAllowlist,
        writer: FakeStageLogWriter,
        runner: FakeSubprocessRunner,
    ) -> None:
        writer.raise_on_record = RuntimeError("sqlite is sad today")

        # Subprocess succeeds; the writer crashes on record. The
        # dispatcher must still return the in-memory result, log the
        # row-loss, and not raise.
        result = await dispatch_subprocess_stage(
            StageClass.SYSTEM_ARCH,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=builder,
            worktree_allowlist=allowlist,
            stage_log_writer=writer,
            subprocess_runner=runner,
        )

        assert result.status is StageDispatchStatus.SUCCESS


# ---------------------------------------------------------------------------
# StageDispatchResult value-object semantics
# ---------------------------------------------------------------------------


class TestStageDispatchResultValueObject:
    """AC: StageDispatchResult is the structured return type."""

    def test_class_level_status_mirrors_match_enum(self) -> None:
        # The AC's `StageDispatchResult.FAILED` phrasing should resolve
        # without an extra StageDispatchStatus import.
        assert StageDispatchResult.FAILED is StageDispatchStatus.FAILED
        assert StageDispatchResult.SUCCESS is StageDispatchStatus.SUCCESS
        assert StageDispatchResult.DEGRADED is StageDispatchStatus.DEGRADED

    def test_result_is_frozen(self) -> None:
        result = StageDispatchResult(
            status=StageDispatchStatus.SUCCESS,
            stage=StageClass.SYSTEM_ARCH,
            build_id="b",
            feature_id=None,
            correlation_id="c",
            artefact_paths=(),
            rationale="ok",
            exit_code=0,
            duration_secs=0.0,
            subcommand="system-arch",
        )
        with pytest.raises((AttributeError, Exception)):
            # Frozen dataclass: assignment raises FrozenInstanceError.
            object.__setattr__  # noqa: B018 - reference to silence unused
            result.status = StageDispatchStatus.FAILED  # type: ignore[misc]
