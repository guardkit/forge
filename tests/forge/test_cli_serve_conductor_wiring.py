"""Tests for the conductor's production wiring in ``forge.cli.serve``.

The conductor's revival, Stage 1b (design pass §a.1 / §a.3). Two pieces:

* :func:`build_supervisor` grows the mode kwargs. Until now it wired
  thirteen routine-path collaborators and **zero** mode fields — the
  conductor was unreachable from production by construction. The kwargs
  are pass-through with ``None`` defaults, so every existing caller
  composes byte-for-byte unchanged (the ``supervisor.py`` TASK-MBC8-008
  backwards-compat invariant) — :class:`TestBuildSupervisorModeKwargs`.

* :func:`build_conductor_mode_kwargs` builds the production values,
  gated on the ``conductor.enabled`` flag. **Flag off (the default) hands
  back nothing**, which leaves every mode field ``None`` and the tree
  byte-for-byte today's routine path — :class:`TestFlagGate`. Flag on
  wires the mode reader, the stateless planner, the ``stage_log``
  projection, and the terminal handler paired with a real commit probe
  — :class:`TestActivatedWiring`.

Nothing here activates anything: there is still no ``next_turn`` driver
(design pass §a.2, Stage 1c), so these tests exercise the wiring's shape
and its default-off honesty, not a live loop. No network, no broker, no
git process — SQLite lives in ``tmp_path`` and the probe's executor is
faked.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import build_conductor_mode_kwargs, build_supervisor
from forge.config.models import (
    ConductorConfig,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    SqliteBuildModeReader,
    SqliteLifecyclePersistence,
)
from forge.pipeline.mode_c_history_reader import SqliteModeCHistoryReader
from forge.pipeline.mode_c_planner import ModeCCyclePlanner
from forge.pipeline.supervisor import (
    BuildModeReader as BuildModeReaderProto,
    ModeCHistoryReader as ModeCHistoryReaderProto,
    Supervisor,
)
from forge.pipeline.terminal_handlers.mode_c import evaluate_terminal


_T0 = datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC)

#: The six mode fields Stage 1b threads through ``build_supervisor``.
_MODE_KWARGS = (
    "build_mode_reader",
    "mode_c_planner",
    "mode_c_history_reader",
    "mode_c_terminal_handler",
    "mode_c_commit_probe",
    "fix_task_context_builder",
)


@pytest.fixture()
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx)
    finally:
        cx.close()


def _collaborators() -> dict[str, Any]:
    """The routine-path collaborators ``build_supervisor`` requires."""
    return {
        "forward_context_builder": MagicMock(name="forward_context_builder"),
        "async_task_starter": MagicMock(name="async_task_starter"),
        "stage_log_recorder": MagicMock(name="stage_log_recorder"),
        "state_channel": MagicMock(name="state_channel"),
        "lifecycle_emitter": MagicMock(name="lifecycle_emitter"),
        "async_subagent_middleware": MagicMock(tools=[]),
        "ordering_guard": MagicMock(name="ordering_guard"),
        "per_feature_sequencer": MagicMock(name="per_feature_sequencer"),
        "constitutional_guard": MagicMock(name="constitutional_guard"),
        "state_reader": MagicMock(name="state_reader"),
        "ordering_stage_log_reader": MagicMock(name="ordering_stage_log_reader"),
        "per_feature_stage_log_reader": MagicMock(name="per_feature_stage_log_reader"),
        "async_task_reader": MagicMock(name="async_task_reader"),
        "reasoning_model": MagicMock(name="reasoning_model"),
        "turn_recorder": MagicMock(name="turn_recorder"),
        "specialist_dispatcher": AsyncMock(name="specialist_dispatcher"),
        "subprocess_dispatcher": AsyncMock(name="subprocess_dispatcher"),
        "pr_review_gate": MagicMock(name="pr_review_gate"),
    }


def _permissions() -> PermissionsConfig:
    return PermissionsConfig(filesystem=FilesystemPermissions(allowlist=[]))


def _config(*, enabled: bool) -> ForgeConfig:
    return ForgeConfig(
        permissions=_permissions(),
        conductor=ConductorConfig(enabled=enabled),
    )


def _seed_fix_build(pool: SqliteLifecyclePersistence) -> str:
    payload = SimpleNamespace(
        feature_id="FEAT-FIX-001",
        repo="guardkit/forge",
        branch="lane/fix-journey",
        feature_yaml_path="tasks/backlog/TASK-DEMO-001.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id="corr-fix-001",
        parent_request_id=None,
        queued_at=_T0,
        requested_at=_T0,
    )
    return pool.record_pending_build(payload, mode=BuildMode.MODE_C)


# ---------------------------------------------------------------------------


class TestBuildSupervisorModeKwargs:
    """The factory can now pass mode fields — and defaults to not."""

    def test_the_six_mode_kwargs_exist(self) -> None:
        params = inspect.signature(build_supervisor).parameters

        for name in _MODE_KWARGS:
            assert name in params, f"build_supervisor must accept {name!r}"
            assert params[name].default is None, (
                f"{name!r} must default to None so existing callers compose "
                "unchanged"
            )

    def test_mode_b_fields_are_not_offered(self) -> None:
        # The full journey was retired as a production destination
        # (design pass §e); its fields stay None by the dataclass default.
        params = inspect.signature(build_supervisor).parameters

        assert not [p for p in params if p.startswith("mode_b")]

    def test_existing_callers_compose_unchanged(self) -> None:
        sup = build_supervisor(**_collaborators())

        assert isinstance(sup, Supervisor)
        for name in _MODE_KWARGS:
            assert getattr(sup, name) is None, (
                f"{name!r} must be None when not passed — the unwired mode "
                "reader is the degrade rail that keeps every build routine"
            )
        assert sup.mode_b_planner is None
        assert sup.mode_b_history_reader is None
        assert sup.mode_b_post_autobuild is None

    def test_each_mode_kwarg_reaches_its_field_by_identity(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        mode_reader = SqliteBuildModeReader(pool)
        planner = ModeCCyclePlanner()
        history_reader = SqliteModeCHistoryReader(pool)

        async def probe(build: Any) -> Any:  # pragma: no cover - identity only
            raise AssertionError("not called")

        def context_builder(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        sup = build_supervisor(
            build_mode_reader=mode_reader,
            mode_c_planner=planner,
            mode_c_history_reader=history_reader,
            mode_c_terminal_handler=evaluate_terminal,
            mode_c_commit_probe=probe,
            fix_task_context_builder=context_builder,
            **_collaborators(),
        )

        assert sup.build_mode_reader is mode_reader
        assert sup.mode_c_planner is planner
        assert sup.mode_c_history_reader is history_reader
        assert sup.mode_c_terminal_handler is evaluate_terminal
        assert sup.mode_c_commit_probe is probe
        assert sup.fix_task_context_builder is context_builder


# ---------------------------------------------------------------------------


class TestFlagGate:
    """``conductor.enabled`` is the switch, and it defaults OFF."""

    def test_the_flag_defaults_off(self) -> None:
        config = ForgeConfig(permissions=_permissions())

        assert config.conductor.enabled is False

    def test_flag_off_wires_nothing(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        assert (
            build_conductor_mode_kwargs(pool=pool, config=_config(enabled=False))
            == {}
        )

    @pytest.mark.parametrize(
        "config",
        [None, SimpleNamespace(), SimpleNamespace(conductor=None), "not a config"],
    )
    def test_unreadable_config_wires_nothing(
        self, pool: SqliteLifecyclePersistence, config: Any
    ) -> None:
        # A misread config leaves the conductor inert — the safe direction.
        assert build_conductor_mode_kwargs(pool=pool, config=config) == {}

    def test_flag_off_leaves_a_supervisor_byte_for_byte_routine(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        off = build_supervisor(
            **build_conductor_mode_kwargs(pool=pool, config=_config(enabled=False)),
            **_collaborators(),
        )
        never_wired = build_supervisor(**_collaborators())

        for name in _MODE_KWARGS:
            assert getattr(off, name) is None
            assert getattr(off, name) == getattr(never_wired, name)
        assert off._read_build_mode("build-anything") is BuildMode.MODE_A

    def test_flag_off_ignores_a_recorded_fix_journey_row(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # Even with a MODE_C row in the database, an off conductor means
        # the supervisor never learns about it.
        build_id = _seed_fix_build(pool)
        sup = build_supervisor(
            **build_conductor_mode_kwargs(pool=pool, config=_config(enabled=False)),
            **_collaborators(),
        )

        assert sup._read_build_mode(build_id) is BuildMode.MODE_A


# ---------------------------------------------------------------------------


class TestActivatedWiring:
    """Flag on: the four collaborators the fix journey needs."""

    def test_wires_the_mode_reader_planner_history_and_handler_pair(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        kwargs = build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True))

        assert set(kwargs) == {
            "build_mode_reader",
            "mode_c_planner",
            "mode_c_history_reader",
            "mode_c_terminal_handler",
            "mode_c_commit_probe",
        }
        assert isinstance(kwargs["build_mode_reader"], BuildModeReaderProto)
        assert isinstance(kwargs["mode_c_planner"], ModeCCyclePlanner)
        assert isinstance(kwargs["mode_c_history_reader"], ModeCHistoryReaderProto)
        assert kwargs["mode_c_terminal_handler"] is evaluate_terminal
        assert callable(kwargs["mode_c_commit_probe"])

    def test_the_terminal_handler_is_explicit_not_the_implicit_fallback(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # The supervisor falls back to ``evaluate_terminal`` when the field
        # is None — but then it has no commit probe, and the handler raises
        # on the branch that matters. Wiring the pair explicitly is the
        # whole point (design pass §a.3).
        kwargs = build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True))

        assert kwargs["mode_c_terminal_handler"] is not None
        assert kwargs["mode_c_commit_probe"] is not None

    def test_fix_task_context_builder_is_deliberately_not_wired(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # The failure-pack-reading adapter is design pass §b.2, a separate
        # item; the supervisor already guards its call site against None.
        kwargs = build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True))

        assert "fix_task_context_builder" not in kwargs

    def test_the_mode_reader_reads_a_real_fix_journey_row(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_fix_build(pool)
        sup = build_supervisor(
            **build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True)),
            **_collaborators(),
        )

        assert sup._read_build_mode(build_id) is BuildMode.MODE_C
        # And an unknown build still reads routine.
        assert sup._read_build_mode("build-unknown") is BuildMode.MODE_A

    def test_the_history_reader_projects_this_pool(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_fix_build(pool)
        kwargs = build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True))

        history = kwargs["mode_c_history_reader"].get_mode_c_history(build_id)

        assert history == ()

    def test_the_commit_probe_fails_loudly_without_a_worktree(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # A queued build has no worktree_path yet; the probe must say so
        # rather than answer "no commits".
        from forge.lifecycle.persistence import Build
        from forge.lifecycle.state_machine import BuildState

        build_id = _seed_fix_build(pool)
        kwargs = build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True))

        result = asyncio.run(
            kwargs["mode_c_commit_probe"](
                Build(
                    build_id=build_id,
                    status=BuildState.QUEUED,
                    mode=BuildMode.MODE_C,
                )
            )
        )

        assert result.failed is True
        assert result.has_commits is False
        assert "worktree_path" in (result.error or "")

    def test_base_branch_and_allowlist_are_forwarded(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        seen: list[str] = []

        class _Allow:
            def is_allowed(self, build_id: str, path: str) -> bool:
                seen.append(path)
                return True

        kwargs = build_conductor_mode_kwargs(
            pool=pool,
            config=_config(enabled=True),
            base_branch="release/2026-07",
            worktree_allowlist=_Allow(),
        )

        assert callable(kwargs["mode_c_commit_probe"])
        # The forwarding is proven by the probe's own suite; here we only
        # assert the factory accepts and returns a probe for them.
        assert inspect.iscoroutinefunction(kwargs["mode_c_commit_probe"])

    def test_the_activated_supervisor_composes(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        sup = build_supervisor(
            **build_conductor_mode_kwargs(pool=pool, config=_config(enabled=True)),
            **_collaborators(),
        )

        assert isinstance(sup, Supervisor)
        assert sup.build_mode_reader is not None
        assert sup.mode_c_planner is not None
        assert sup.mode_c_history_reader is not None
        assert sup.mode_c_terminal_handler is evaluate_terminal
        assert sup.mode_c_commit_probe is not None
        # The retired full journey stays unwired, flag or no flag.
        assert sup.mode_b_planner is None
        assert sup.mode_b_history_reader is None
        assert sup.mode_b_post_autobuild is None
