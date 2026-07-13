"""Tests for ``forge cancel`` and ``forge skip`` thin wrappers (TASK-PSM-011).

Each test class mirrors one acceptance criterion of TASK-PSM-011 so the
mapping between the criterion and its verifier stays explicit.

* AC-001 — ``cli/cancel.py`` exports ``cancel_cmd``.
* AC-002 — ``cli/skip.py`` exports ``skip_cmd``.
* AC-003 — both register on ``cli/main.py:main``.
* AC-004 — ``forge cancel`` resolves identifier via
  ``find_active_or_recent`` and exits non-zero on miss.
* AC-005 — ``forge skip`` exits non-zero with REFUSED message when the
  handler returns ``SkipStatus.REFUSED_CONSTITUTIONAL`` (and when the
  build is not paused, which is the explicit Group C scenario).
* AC-006 — both pass ``responder=os.getlogin()`` to the handler so the
  Group E audit-trail invariant holds.
* AC-007 — both wrappers are < 60 source lines.

The tests inject a fake :class:`CliRuntime` via monkeypatch on
``forge.cli.runtime.build_cli_runtime`` so no real SQLite database is
needed — every collaborator the wrapper touches is an in-memory fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from forge.cli import _cancel_run as cancel_run_module
from forge.cli import cancel as cancel_module
from forge.cli import main as cli_main
from forge.cli import runtime as cli_runtime
from forge.cli import skip as skip_module
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.cli_steering import (
    BuildLifecycle,
    BuildSnapshot,
    CancelOutcome,
    CancelStatus,
    SkipOutcome,
    SkipStatus,
)
from forge.pipeline.constitutional_guard import (
    SkipDecision,
    SkipVerdict,
)
from forge.pipeline.stage_taxonomy import StageClass

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakePersistence:
    """In-memory stand-in for :class:`SqliteLifecyclePersistence`.

    Only ``find_active_or_recent`` is exercised by the wrappers — the
    field is a dict so tests can pre-seed it per-feature.
    """

    builds: dict[str, Build] = field(default_factory=dict)

    def find_active_or_recent(self, identifier: str) -> Build | None:
        # Match either by feature_id key or by build_id directly.
        if identifier in self.builds:
            return self.builds[identifier]
        for build in self.builds.values():
            if build.build_id == identifier:
                return build
        return None


@dataclass
class FakeSnapshotReader:
    """Returns a pre-canned :class:`BuildSnapshot` per build_id."""

    snapshots: dict[str, BuildSnapshot] = field(default_factory=dict)

    def get_snapshot(self, build_id: str) -> BuildSnapshot:
        return self.snapshots.get(
            build_id,
            BuildSnapshot(build_id=build_id, lifecycle=BuildLifecycle.TERMINAL),
        )


@dataclass
class FakeHandler:
    """Captures the arguments each CLI delegation passes through."""

    snapshot_reader: FakeSnapshotReader
    cancel_calls: list[dict[str, Any]] = field(default_factory=list)
    skip_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_outcome: CancelOutcome | None = None
    skip_outcome: SkipOutcome | None = None

    def handle_cancel(
        self, *, build_id: str, reason: str, responder: str
    ) -> CancelOutcome:
        self.cancel_calls.append(
            {"build_id": build_id, "reason": reason, "responder": responder}
        )
        if self.cancel_outcome is not None:
            return self.cancel_outcome
        return CancelOutcome(
            build_id=build_id,
            status=CancelStatus.CANCELLED_DIRECT,
            rationale=f"cancelled {build_id} reason={reason!r} by {responder!r}",
        )

    def handle_skip(
        self,
        *,
        build_id: str,
        stage: StageClass,
        reason: str,
        responder: str,
    ) -> SkipOutcome:
        self.skip_calls.append(
            {
                "build_id": build_id,
                "stage": stage,
                "reason": reason,
                "responder": responder,
            }
        )
        if self.skip_outcome is not None:
            return self.skip_outcome
        return SkipOutcome(
            build_id=build_id,
            stage=stage,
            status=SkipStatus.SKIPPED,
            rationale=(
                f"skipped {build_id} stage={stage.value} reason={reason!r} "
                f"by {responder!r}"
            ),
            guard_decision=SkipDecision(
                stage=stage,
                verdict=SkipVerdict.ALLOWED,
                rationale="permitted",
            ),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """An empty file the Click ``--db`` option's ``exists=True`` accepts."""
    target = tmp_path / "forge.db"
    target.write_bytes(b"")
    return target


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    persistence: FakePersistence,
    handler: FakeHandler,
) -> None:
    """Replace :func:`build_cli_runtime` with a closure returning fakes."""
    fake = cli_runtime.CliRuntime(  # type: ignore[arg-type]
        persistence=persistence,  # type: ignore[arg-type]
        cli_steering_handler=handler,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(cli_runtime, "build_cli_runtime", lambda *_a, **_kw: fake)
    # ``forge cancel`` orchestration lives in ``_cancel_run`` (O-02 extraction
    # keeps the wrapper thin, AC-007); ``forge skip`` still builds the runtime
    # in-module.
    monkeypatch.setattr(cancel_run_module, "build_cli_runtime", lambda *_a, **_kw: fake)
    monkeypatch.setattr(skip_module, "build_cli_runtime", lambda *_a, **_kw: fake)


# ---------------------------------------------------------------------------
# AC-001 / AC-002 / AC-003 — exports and registration
# ---------------------------------------------------------------------------


class TestWrapperExportsAndRegistration:
    """AC-001 / AC-002 / AC-003 — the wrappers exist and are wired to ``main``."""

    def test_cancel_module_exports_cancel_cmd_as_click_command(self) -> None:
        import click

        assert isinstance(cancel_module.cancel_cmd, click.Command)
        assert cancel_module.cancel_cmd.name == "cancel"

    def test_skip_module_exports_skip_cmd_as_click_command(self) -> None:
        import click

        assert isinstance(skip_module.skip_cmd, click.Command)
        assert skip_module.skip_cmd.name == "skip"

    def test_cancel_cmd_is_registered_on_main(self) -> None:
        assert "cancel" in cli_main.main.commands
        assert cli_main.main.commands["cancel"] is cancel_module.cancel_cmd

    def test_skip_cmd_is_registered_on_main(self) -> None:
        assert "skip" in cli_main.main.commands
        assert cli_main.main.commands["skip"] is skip_module.skip_cmd


# ---------------------------------------------------------------------------
# AC-004 — cancel resolves identifier and exits non-zero on miss
# ---------------------------------------------------------------------------


class TestCancelIdentifierResolution:
    """AC-004 — Group C "cancel of unknown feature → not-found"."""

    def test_cancel_of_unknown_feature_exits_non_zero_with_not_found(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        persistence = FakePersistence(builds={})
        handler = FakeHandler(snapshot_reader=FakeSnapshotReader())
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            cancel_module.cancel_cmd,
            ["FEAT-NOPE", "--db", str(db_path)],
        )
        assert result.exit_code != 0
        assert "no active or recent build" in result.stderr.lower()
        assert handler.cancel_calls == []

    def test_cancel_of_known_feature_resolves_to_build_id_and_calls_handler(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        persistence = FakePersistence(
            builds={
                "FEAT-A1B2": Build(
                    build_id="build-FEAT-A1B2-001",
                    status=BuildState.RUNNING,
                )
            }
        )
        handler = FakeHandler(snapshot_reader=FakeSnapshotReader())
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            cancel_module.cancel_cmd,
            ["FEAT-A1B2", "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.output + result.stderr
        assert len(handler.cancel_calls) == 1
        assert handler.cancel_calls[0]["build_id"] == "build-FEAT-A1B2-001"


# ---------------------------------------------------------------------------
# AC-005 — skip exits non-zero on REFUSED / non-paused
# ---------------------------------------------------------------------------


class TestSkipNonPausedRefused:
    """AC-005 — Group C "skip on non-paused build is refused"."""

    def test_skip_on_non_paused_build_exits_non_zero(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        build = Build(
            build_id="build-FEAT-A1B2-001",
            status=BuildState.RUNNING,
        )
        persistence = FakePersistence(builds={"FEAT-A1B2": build})
        snapshot_reader = FakeSnapshotReader(
            snapshots={
                build.build_id: BuildSnapshot(
                    build_id=build.build_id,
                    lifecycle=BuildLifecycle.OTHER_RUNNING,
                )
            }
        )
        handler = FakeHandler(snapshot_reader=snapshot_reader)
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            skip_module.skip_cmd,
            ["FEAT-A1B2", "--db", str(db_path)],
        )
        assert result.exit_code != 0
        assert "REFUSED" in result.stderr
        assert "skip not allowed unless paused" in result.stderr.lower()
        # The handler is NEVER invoked — the wrapper short-circuits.
        assert handler.skip_calls == []

    def test_skip_on_unknown_feature_exits_non_zero(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        persistence = FakePersistence(builds={})
        handler = FakeHandler(snapshot_reader=FakeSnapshotReader())
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            skip_module.skip_cmd,
            ["FEAT-MISS", "--db", str(db_path)],
        )
        assert result.exit_code != 0
        assert "no active or recent build" in result.stderr.lower()


# ---------------------------------------------------------------------------
# AC-006 — both record the resolved responder on the handler (O-02: the
# responder now resolves via --responder / $FORGE_RESPONDER / OS user, never a
# tty-dependent os.getlogin()).
# ---------------------------------------------------------------------------


class TestResponderPassedToHandler:
    """AC-006 — Group E "cancelling operator recorded distinctly"."""

    def test_cancel_passes_responder_flag_to_handler(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        persistence = FakePersistence(
            builds={
                "FEAT-A1B2": Build(
                    build_id="build-FEAT-A1B2-001",
                    status=BuildState.RUNNING,
                )
            }
        )
        handler = FakeHandler(snapshot_reader=FakeSnapshotReader())
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            cancel_module.cancel_cmd,
            [
                "FEAT-A1B2",
                "--reason",
                "CI cleanup",
                "--responder",
                "alice",
                "--db",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output + result.stderr
        assert handler.cancel_calls == [
            {
                "build_id": "build-FEAT-A1B2-001",
                "reason": "CI cleanup",
                "responder": "alice",
            }
        ]

    def test_cancel_resolves_responder_from_env_without_a_tty(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        """O-02: no flag, os.getlogin() raising → resolves from env, no crash."""
        persistence = FakePersistence(
            builds={
                "FEAT-A1B2": Build(
                    build_id="build-FEAT-A1B2-001",
                    status=BuildState.RUNNING,
                )
            }
        )
        handler = FakeHandler(snapshot_reader=FakeSnapshotReader())
        _patch_runtime(monkeypatch, persistence, handler)
        import os

        def _no_tty() -> str:
            raise OSError(6, "No such device or address")

        monkeypatch.setattr(os, "getlogin", _no_tty)
        monkeypatch.setenv("FORGE_RESPONDER", "U03QR8WKT29")
        runner = CliRunner()
        result = runner.invoke(
            cancel_module.cancel_cmd,
            ["FEAT-A1B2", "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.output + result.stderr
        assert handler.cancel_calls[0]["responder"] == "U03QR8WKT29"

    def test_skip_passes_responder_flag_to_handler(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        build = Build(
            build_id="build-FEAT-A1B2-001",
            status=BuildState.PAUSED,
        )
        persistence = FakePersistence(builds={"FEAT-A1B2": build})
        snapshot_reader = FakeSnapshotReader(
            snapshots={
                build.build_id: BuildSnapshot(
                    build_id=build.build_id,
                    lifecycle=BuildLifecycle.PAUSED_AT_GATE,
                    paused_stage=StageClass.AUTOBUILD,
                    paused_feature_id="FEAT-A1B2",
                )
            }
        )
        handler = FakeHandler(snapshot_reader=snapshot_reader)
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            skip_module.skip_cmd,
            [
                "FEAT-A1B2",
                "--reason",
                "approved by hand",
                "--responder",
                "bob",
                "--db",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output + result.stderr
        assert len(handler.skip_calls) == 1
        call = handler.skip_calls[0]
        assert call["build_id"] == build.build_id
        assert call["stage"] is StageClass.AUTOBUILD
        assert call["reason"] == "approved by hand"
        assert call["responder"] == "bob"


# ---------------------------------------------------------------------------
# AC-005 — skip on a constitutional stage surfaces a non-zero exit
# ---------------------------------------------------------------------------


class TestSkipConstitutionalRefusal:
    """AC-005 — when the handler returns REFUSED_CONSTITUTIONAL the wrapper exits non-zero."""

    def test_constitutional_refusal_exits_non_zero_with_refused_message(
        self, monkeypatch: pytest.MonkeyPatch, db_path: Path
    ) -> None:
        build = Build(
            build_id="build-FEAT-A1B2-002",
            status=BuildState.PAUSED,
        )
        persistence = FakePersistence(builds={"FEAT-A1B2": build})
        snapshot_reader = FakeSnapshotReader(
            snapshots={
                build.build_id: BuildSnapshot(
                    build_id=build.build_id,
                    lifecycle=BuildLifecycle.PAUSED_AT_GATE,
                    paused_stage=StageClass.PULL_REQUEST_REVIEW,
                    paused_feature_id="FEAT-A1B2",
                )
            }
        )
        # Pre-canned constitutional refusal so the wrapper hits the
        # non-zero branch.
        handler = FakeHandler(
            snapshot_reader=snapshot_reader,
            skip_outcome=SkipOutcome(
                build_id=build.build_id,
                stage=StageClass.PULL_REQUEST_REVIEW,
                status=SkipStatus.REFUSED_CONSTITUTIONAL,
                rationale="constitutional veto: pr-review",
                guard_decision=SkipDecision(
                    stage=StageClass.PULL_REQUEST_REVIEW,
                    verdict=SkipVerdict.REFUSED_CONSTITUTIONAL,
                    rationale="pr-review is constitutional",
                ),
            ),
        )
        _patch_runtime(monkeypatch, persistence, handler)
        runner = CliRunner()
        result = runner.invoke(
            skip_module.skip_cmd,
            ["FEAT-A1B2", "--db", str(db_path)],
        )
        assert result.exit_code != 0
        assert "REFUSED" in result.stderr
        assert "constitutional" in result.stderr.lower()


# ---------------------------------------------------------------------------
# AC-007 — wrappers are < 60 lines
# ---------------------------------------------------------------------------


class TestWrappersAreThin:
    """AC-007 — each wrapper file is < 60 lines (intentionally thin)."""

    def test_cancel_module_is_under_60_lines(self) -> None:
        path = Path(cancel_module.__file__)
        line_count = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        assert line_count < 60, f"cancel.py is {line_count} lines (must be < 60)"

    def test_skip_module_is_under_60_lines(self) -> None:
        path = Path(skip_module.__file__)
        line_count = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        assert line_count < 60, f"skip.py is {line_count} lines (must be < 60)"
