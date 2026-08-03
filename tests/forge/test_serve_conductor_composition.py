"""The conductor's PRODUCTION composition (Stage 2, shakeout items 3 and 4).

Stage 1 left two honest gaps and said so in its own logs:

* ``build_conductor_router`` was called with no ``supervisor_factory``, so
  it stayed ``None`` — inert even with the flag ON;
* ``ConductorDriverDeps`` fell back to all-``None`` seams, so the first
  non-terminal turn hit a wait it could not perform and died
  ``WAIT_EXPIRED`` with no receipts.

Network-free by construction: the ONLY seam that would touch NATS
(``subscribe_resume``) arrives injected.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import serve as serve_mod
from forge.cli import _serve_conductor as serve_conductor_mod
from forge.cli._conductor_outcome import DECLINED, TakenTerminal
from forge.cli._serve_conductor import (
    CONDUCTOR_LEG_MODEL_ENV,
    CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS,
    CONDUCTOR_STAGE_TIMEOUT_SECONDS,
    CONDUCTOR_WORK_STAGE_TIMEOUT_SECONDS,
    _ModeAOnlySeam,
    build_conductor_driver_deps_factory,
    build_conductor_supervisor_factory,
    make_conductor_wait_window_reader,
)
from forge.pipeline.mode_c_planner import FixTaskRef
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-FIX007-20260731"


def _config(*, conductor_on: bool) -> ForgeConfig:
    raw: dict[str, Any] = {
        "pipeline": {
            "build_queue_subject": "pipeline.build-queued.team-a",
            "approved_originators": ["terminal"],
        },
        "permissions": {"filesystem": {"allowlist": ["/work"]}},
    }
    if conductor_on:
        raw["conductor"] = {"enabled": True}
    return ForgeConfig.model_validate(raw)


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-FIX007', "
        "'r', 'fix/x', 'f.yaml', 'RUNNING', 'cli', 'corr-1', "
        "'2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', '/work/b', 'mode-c', "
        "'TASK-FIX007')",
        (BUILD_ID,),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _supervisor_factory(pool: SqliteLifecyclePersistence, **over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "pool": pool,
        "config": _config(conductor_on=True),
        "forward_context_builder": object(),
        "worktree_allowlist": object(),
        "read_allowlist": [Path("/work")],
        "subprocess_runner": object(),
    }
    kwargs.update(over)
    return build_conductor_supervisor_factory(**kwargs)


# ---------------------------------------------------------------------------
# Item 3 — the supervisor factory the router refused to invent
# ---------------------------------------------------------------------------


class TestSupervisorFactory:
    def test_it_composes_a_supervisor_with_the_mode_fields_filled(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = _supervisor_factory(pool)(BUILD_ID)

        assert supervisor.build_mode_reader is not None
        assert supervisor.mode_c_planner is not None
        assert supervisor.mode_c_history_reader is not None
        assert supervisor.mode_c_terminal_handler is not None
        assert supervisor.mode_c_commit_probe is not None

    def test_the_pr_review_gate_is_the_merge_ready_checkpoint(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import (
            MergeReadyCheckpointPublisher,
        )

        supervisor = _supervisor_factory(pool)(BUILD_ID)

        assert isinstance(supervisor.pr_review_gate, MergeReadyCheckpointPublisher)

    def test_each_build_gets_a_fresh_checkpoint_so_the_latch_is_per_journey(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        factory = _supervisor_factory(pool)
        assert factory(BUILD_ID).pr_review_gate is not factory(BUILD_ID).pr_review_gate

    def test_no_gates_reader_means_no_card_ever(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """§c.3 holding by construction, not by intention.

        The precondition is PROVEN green, never "not proven red". With no
        gate reader wired the checkpoint reads UNKNOWN, treats it as red,
        and publishes nothing — so a half-wired conductor cannot deliver.
        """
        published: list[Any] = []
        supervisor = _supervisor_factory(
            pool, publish_card=lambda **kw: published.append(kw) or "RESUMED"
        )(BUILD_ID)

        decision = asyncio.run(
            supervisor.pr_review_gate.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-FIX007",
                auto_approve=False,
                rationale="clean",
            )
        )

        assert decision.card_published is False
        assert published == []

    def test_budget_caps_are_resolved_per_build(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = _supervisor_factory(pool)(BUILD_ID)
        assert supervisor.budget_guards is not None
        assert supervisor.budget_profile_name == "attended"
        assert supervisor.budget_started_at_reader is not None

    def test_the_dispatcher_is_the_conductor_adapter(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = _supervisor_factory(pool)(BUILD_ID)
        assert (
            supervisor.subprocess_dispatcher.__name__
            == "conductor_subprocess_dispatcher"
        )


def _runner():
    """The recording runner both drive-tests below dispatch into."""
    from tests.forge.pipeline.dispatchers.test_subprocess import FakeSubprocessRunner

    return FakeSubprocessRunner()


def _drive(supervisor: Any, stage: StageClass) -> None:
    """Run ONE real fix-journey dispatch through the composed adapter."""
    kwargs: dict[str, Any] = {"stage": stage, "build_id": BUILD_ID}
    if stage is StageClass.TASK_WORK:
        kwargs["fix_task"] = FixTaskRef(
            fix_task_id="TASK-FIX007-A", review_history_index=0
        )
    asyncio.run(supervisor.subprocess_dispatcher(**kwargs))


class TestTheLegTripwires:
    """LI stage-2 §1 — two NAMED tripwires, wired at the composition site."""

    def test_the_two_constants_carry_the_ruled_numbers(self) -> None:
        assert CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS == 600
        assert CONDUCTOR_WORK_STAGE_TIMEOUT_SECONDS == 1800
        assert CONDUCTOR_STAGE_TIMEOUT_SECONDS == {
            StageClass.TASK_REVIEW: 600,
            StageClass.TASK_WORK: 1800,
        }

    def test_the_constants_state_their_own_law_where_they_live(self) -> None:
        """Rich's 2026-07-30 ruling, kept beside the numbers it governs.

        A bare number grows into a work budget the first time somebody
        reads it in a hurry. The words are the fence, so pin the words —
        including the ledgered destination that makes these two temporary.
        """
        source = Path(serve_conductor_mod.__file__).read_text(encoding="utf-8")

        assert 'tripwires for "the leg itself is broken"' in source
        assert "never work-limiters" in source
        assert "monitored-supervision path" in source

    def test_a_review_dispatch_gets_600_and_a_work_dispatch_1800(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The composed production path, driven — not the constants re-read."""
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)
        _drive(supervisor, StageClass.TASK_WORK)

        assert [call["timeout_seconds"] for call in runner.calls] == [600, 1800]


class TestTheLegSeatEnvThread:
    """LI stage-2 §3.4 — the operator's stopgap seat thread.

    Ledgered, not smuggled: the config-as-code field on ``ConductorConfig``
    is Rich's ruling at conductor activation. Until then this env var is
    the ONE way the pipeline can name the seat a leg runs on.
    """

    def test_no_env_means_the_argv_names_no_model_at_all(
        self, pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CONDUCTOR_LEG_MODEL_ENV, raising=False)
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert "--model" not in runner.calls[0]["args"]

    def test_the_env_names_the_seat_on_a_fix_journey_dispatch(
        self, pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONDUCTOR_LEG_MODEL_ENV, "qwen3-coder-30b")
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)
        _drive(supervisor, StageClass.TASK_WORK)

        for call in runner.calls:
            assert call["args"][-2:] == ["--model", "qwen3-coder-30b"]

    def test_a_blank_env_is_read_as_unset_not_as_an_empty_seat(
        self, pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--model ''`` would be worse than no flag: a named nothing."""
        monkeypatch.setenv(CONDUCTOR_LEG_MODEL_ENV, "   ")
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert "--model" not in runner.calls[0]["args"]


class TestTheM0Guard:
    """Design pass §g made structural, not aspirational.

    A fix-journey turn branches before every Mode A collaborator. If one is
    ever reached, that is a control-flow bug — and for ``reasoning_model``
    it is a frontier call on the path whose whole point is making none. So
    those seams raise instead of working.
    """

    def test_the_reasoning_model_seam_refuses_loudly(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = _supervisor_factory(pool)(BUILD_ID)

        assert isinstance(supervisor.reasoning_model, _ModeAOnlySeam)
        with pytest.raises(RuntimeError, match="reasoning_model"):
            supervisor.reasoning_model.choose_next_stage()

    def test_the_specialist_dispatcher_seam_refuses_loudly(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = _supervisor_factory(pool)(BUILD_ID)
        with pytest.raises(RuntimeError, match="specialist_dispatcher"):
            supervisor.specialist_dispatcher()


# ---------------------------------------------------------------------------
# Item 4 — the driver deps that were all None
# ---------------------------------------------------------------------------


class TestDriverDepsFactory:
    def test_every_seam_the_loop_needs_is_wired(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(conductor_on=True),
            subscriber_factory=lambda *_a, **_k: None,
        )(BUILD_ID, supervisor=object())

        assert deps.wait_window_reader is not None
        assert deps.subscribe_resume is not None
        assert deps.export_stage_receipts is not None
        assert deps.write_failure_pack is not None
        assert deps.close_out is not None
        assert deps.escalation_resolved is not None

    def test_no_subscriber_leaves_the_wait_seam_unwired_rather_than_spinning(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The honest degrade: stop loudly, never busy-wait."""
        deps = build_conductor_driver_deps_factory(
            pool=pool, config=_config(conductor_on=True), subscriber_factory=None
        )(BUILD_ID, supervisor=object())

        assert deps.subscribe_resume is None

    def test_the_receipts_exporter_takes_the_drivers_shape(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        """One shape, settled deliberately.

        The driver calls ``(*, build_id, report)`` because that is all a
        turn loop knows; the real exporter needs ``stage`` and
        ``worktree_path``. This adapter is where they meet.
        """
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(conductor_on=True),
            receipts_root=tmp_path / "receipts",
        )(BUILD_ID, supervisor=object())

        class _R:
            chosen_stage = StageClass.TASK_REVIEW
            rationale = "initial review"

        key = deps.export_stage_receipts(build_id=BUILD_ID, report=_R())

        assert key == "001-task-review"
        assert (
            tmp_path / "receipts" / BUILD_ID / "stages" / key / "turn-rationale.txt"
        ).read_text() == "initial review"

    def test_a_planning_tick_exports_nothing(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(conductor_on=True),
            receipts_root=tmp_path / "receipts",
        )(BUILD_ID, supervisor=object())

        class _R:
            chosen_stage = None
            rationale = "waiting"

        assert deps.export_stage_receipts(build_id=BUILD_ID, report=_R()) is None

    def test_the_failure_pack_writer_lands_a_manifest(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(conductor_on=True),
            receipts_root=tmp_path / "receipts",
        )(BUILD_ID, supervisor=object())

        path = deps.write_failure_pack(
            build_id=BUILD_ID,
            reason="the structured wait expired",
            outcome="wait-expired",
            stage_keys=("001-task-review",),
        )

        assert path is not None and Path(path).exists()

    def test_close_out_records_a_row_and_never_transitions_the_build(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The conductor records; it does not adjudicate (the FTR lesson)."""
        deps = build_conductor_driver_deps_factory(
            pool=pool, config=_config(conductor_on=True)
        )(BUILD_ID, supervisor=object())

        class _R:
            outcome = None
            rationale = "terminal"

        deps.close_out(build_id=BUILD_ID, report=_R())

        labels = [r.stage_label for r in pool.read_stages(BUILD_ID)]
        assert "conductor-close-out" in labels
        assert pool.get_build_row(BUILD_ID).status.value == "RUNNING"


class TestTheWaitWindow:
    def test_a_build_with_no_pending_approval_is_resolved(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        read = make_conductor_wait_window_reader(
            pool=pool, config=_config(conductor_on=True)
        )
        assert read(BUILD_ID).resolved is True

    def test_a_missing_row_is_resolved_rather_than_a_crash(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        read = make_conductor_wait_window_reader(
            pool=pool, config=_config(conductor_on=True)
        )
        assert read("build-does-not-exist").resolved is True

    def test_a_pending_approval_opens_the_configured_window(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        from datetime import datetime, timedelta, timezone

        pool.mark_paused(BUILD_ID, "req-1")
        anchor = pool.get_build_row(BUILD_ID).started_at
        config = _config(conductor_on=True)

        read = make_conductor_wait_window_reader(
            pool=pool, config=config, clock=lambda: anchor + timedelta(seconds=10)
        )
        window = read(BUILD_ID)
        assert window.phase == 1
        assert window.remaining_seconds == pytest.approx(
            config.approval.default_wait_seconds - 10
        )

        # Past the first window it escalates and asks for a re-emit — which
        # the driver performs only AFTER the waiter arms (arm-before-post).
        read2 = make_conductor_wait_window_reader(
            pool=pool,
            config=config,
            clock=lambda: anchor
            + timedelta(seconds=config.approval.default_wait_seconds + 5),
        )
        escalated = read2(BUILD_ID)
        assert escalated.phase == 2
        assert escalated.needs_republish is True

        # Past the ceiling the window is spent: a loud stop, not a spin.
        read3 = make_conductor_wait_window_reader(
            pool=pool,
            config=config,
            clock=lambda: anchor
            + timedelta(seconds=config.approval.max_wait_seconds + 1),
        )
        assert read3(BUILD_ID).remaining_seconds == 0.0
        assert read3(BUILD_ID).resolved is False

        _ = datetime.now(timezone.utc)  # keep the import honest


# ---------------------------------------------------------------------------
# THE FLAG-OFF REGRESSION PIN, at the composition level
# ---------------------------------------------------------------------------


class TestFlagOffIsALiteralPassThrough:
    """The flag-off pin, driven through the REAL production seam.

    This class used to hold a test that monkeypatched the composer, then
    asserted ``conductor_enabled(config) is False`` and that the recorder
    was empty — without ever invoking anything that could have called the
    composer. It passed on a tree where the composer ran unconditionally.
    A pin that cannot fail is not a pin (shadow-replay item 6).

    So both tests below actually call
    :func:`~forge.cli.serve.bind_production_dispatch_chain` and drive the
    closure it returns, which is the ONE code path where the daemon
    decides whether to compose a conductor. Fakes stand at the NATS,
    gating and consumer-deps edges, exactly as
    ``tests/cli/test_serve_planning_wiring.py`` does for the same closure
    — the suite stays network-free.
    """

    @staticmethod
    def _drive_boot(
        pool: SqliteLifecyclePersistence,
        *,
        conductor_on: bool,
        recorder: "list[dict[str, Any]]",
        tmp_path: Path,
    ) -> None:
        """Run the production dispatch-chain composition once."""
        from unittest.mock import MagicMock, patch

        class _FakeNats:
            async def subscribe(self, subject: str, callback: Any) -> Any:
                return MagicMock()

            async def publish(self, subject: str, body: bytes) -> None:
                return None

        def _record(**kwargs: Any) -> Any:
            recorder.append(kwargs)
            return None

        with (
            patch.object(serve_mod, "_compose_conductor_router", _record),
            patch("forge.cli._serve_deps_gating.bind_gate_parts") as gate_parts,
            patch("forge.cli._serve_deps.build_pipeline_consumer_deps") as deps,
            patch(
                "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
            ) as publisher,
        ):
            gate_parts.return_value = None
            deps.return_value = MagicMock()
            publisher.return_value = (MagicMock(), MagicMock())

            compose = serve_mod.bind_production_dispatch_chain(
                forge_config=_config(conductor_on=conductor_on),
                sqlite_pool=pool,
                db_path=tmp_path / "forge.db",
            )
            asyncio.run(compose(_FakeNats()))

    def test_flag_off_never_enters_the_composer(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        """OFF must not construct a single conductor object.

        Not "equivalent behaviour" — nothing built. The flag is checked
        BEFORE the composer is reached, so this asserts the composer is
        never entered, having actually run the boot path that would enter
        it.
        """
        entered: list[dict[str, Any]] = []

        self._drive_boot(
            pool, conductor_on=False, recorder=entered, tmp_path=tmp_path
        )

        assert entered == []

    def test_flag_on_enters_the_composer_exactly_once(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        """The control that makes the flag-off assertion mean something.

        Same boot, same fakes, flag flipped — the composer is entered, and
        the shared NATS client reaches it (item 3: without the client the
        resume subscription cannot be composed at all).
        """
        entered: list[dict[str, Any]] = []

        self._drive_boot(pool, conductor_on=True, recorder=entered, tmp_path=tmp_path)

        assert len(entered) == 1
        assert entered[0]["nats_client"] is not None
        assert entered[0]["sqlite_pool"] is pool

    def test_flag_off_router_is_none(self, pool: SqliteLifecyclePersistence) -> None:
        assert (
            serve_mod.build_conductor_router(
                pool=pool,
                config=_config(conductor_on=False),
                supervisor_factory=lambda _bid: object(),
            )
            is None
        )

    def test_flag_on_with_a_factory_yields_a_live_router(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        router = serve_mod.build_conductor_router(
            pool=pool,
            config=_config(conductor_on=True),
            supervisor_factory=lambda _bid: object(),
            driver_deps_factory=lambda _bid, _sup: None,
            spawn=lambda coro: coro.close(),
        )
        assert router is not None

    @pytest.mark.asyncio
    async def test_the_composed_router_speaks_the_vocabulary(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The PRODUCTION composition answers the widened contract.

        Activation design §3: the bool is REPLACED. The seeded row is
        mode-c with no budget profile, so it meets the cap law and comes
        back taken-and-terminal with the reason carried — never ``True``,
        which could not say whether a slot still had a journey behind it.
        """
        router = serve_mod.build_conductor_router(
            pool=pool,
            config=_config(conductor_on=True),
            supervisor_factory=lambda _bid: pytest.fail("supervisor built"),
            spawn=lambda coro: coro.close(),
        )
        assert router is not None

        outcome = await router(build_id=BUILD_ID)
        assert isinstance(outcome, TakenTerminal)
        assert outcome.reason
        assert outcome is not DECLINED

    @pytest.mark.asyncio
    async def test_the_composed_router_declines_a_routine_build(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The mutation guard: DECLINED still means the routine path."""
        pool.connection.execute(
            "UPDATE builds SET mode = 'mode-a' WHERE build_id = ?", (BUILD_ID,)
        )
        pool.connection.commit()
        router = serve_mod.build_conductor_router(
            pool=pool,
            config=_config(conductor_on=True),
            supervisor_factory=lambda _bid: pytest.fail("supervisor built"),
        )
        assert router is not None
        assert await router(build_id=BUILD_ID) is DECLINED

    def test_the_composer_wires_both_factories(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Item 3's whole point: the router is no longer factory-less."""
        captured: dict[str, Any] = {}

        original = serve_mod.build_conductor_router

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        serve_mod.build_conductor_router = _capture  # type: ignore[assignment]
        try:
            router = serve_mod._compose_conductor_router(
                sqlite_pool=pool,
                forge_config=_config(conductor_on=True),
                lifecycle_emitter=None,
                gate_parts=None,
                gate_repository=None,
                gate_state_machine=None,
                clock=lambda: None,
            )
        finally:
            serve_mod.build_conductor_router = original  # type: ignore[assignment]

        assert captured["supervisor_factory"] is not None
        assert captured["driver_deps_factory"] is not None
        assert router is not None


# ---------------------------------------------------------------------------
# Item 2 — gates_green_reader: a REAL bounded evaluation
# ---------------------------------------------------------------------------


class _Declaration:
    """Stands in for guardkit's ``ToolchainDeclaration`` (duck-typed)."""

    def __init__(self, test: str | None, test_timeout: int = 300) -> None:
        self.test = test
        self.test_timeout = test_timeout


def _gates_config(repo_paths: "dict[str, str] | None" = None) -> ForgeConfig:
    raw: dict[str, Any] = {
        "pipeline": {
            "build_queue_subject": "pipeline.build-queued.team-a",
            "approved_originators": ["terminal"],
        },
        "permissions": {"filesystem": {"allowlist": ["/work"]}},
        "conductor": {"enabled": True},
    }
    if repo_paths is not None:
        raw["planning"] = {"target_repo_paths": repo_paths}
    return ForgeConfig.model_validate(raw)


@pytest.fixture
def gates_pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    """A build row whose worktree really exists on disk."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "gates.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-G', "
        "'org/target', 'fix/g', 'f.yaml', 'RUNNING', 'cli', 'corr-g', "
        "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', ?, 'mode-c', "
        "'TASK-G')",
        (BUILD_ID, str(worktree)),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


class TestGatesGreenReader:
    """The exit-code-is-the-verdict law, and every honest degrade around it."""

    @staticmethod
    def _reader(
        pool: Any,
        *,
        declaration: Any,
        exit_code: "int | None" = 0,
        calls: "list[dict[str, Any]] | None" = None,
        repo_paths: "dict[str, str] | None" = None,
    ) -> Any:
        from forge.cli._serve_conductor import make_gates_green_reader

        def _run(*, command: str, cwd: Any, timeout_seconds: int):
            if calls is not None:
                calls.append(
                    {
                        "command": command,
                        "cwd": str(cwd),
                        "timeout_seconds": timeout_seconds,
                    }
                )
            return exit_code, f"`{command}` exited {exit_code}"

        return make_gates_green_reader(
            pool=pool,
            config=_gates_config(
                repo_paths
                if repo_paths is not None
                else {"org/target": "/canonical/target"}
            ),
            declaration_loader=lambda _root: declaration,
            command_runner=_run,
        )

    def test_exit_zero_is_green(self, gates_pool: Any) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(
            gates_pool, declaration=_Declaration("npm test"), exit_code=0
        )(build_id=BUILD_ID, branch="fix/g")

        assert report.status is GateStatus.GREEN

    def test_a_non_zero_exit_is_red_and_never_unknown(self, gates_pool: Any) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(
            gates_pool, declaration=_Declaration("npm test"), exit_code=1
        )(build_id=BUILD_ID, branch="fix/g")

        assert report.status is GateStatus.RED
        assert report.failed_gates  # a red gate names something

    def test_it_runs_the_declared_command_in_the_fix_worktree(
        self, gates_pool: Any, tmp_path: Path
    ) -> None:
        """Declared command, fix branch's worktree, the declaration's bound."""
        calls: list[dict[str, Any]] = []

        self._reader(
            gates_pool,
            declaration=_Declaration("uv run pytest -q", test_timeout=900),
            calls=calls,
        )(build_id=BUILD_ID, branch="fix/g")

        assert calls == [
            {
                "command": "uv run pytest -q",
                "cwd": str(tmp_path / "wt"),
                "timeout_seconds": 900,
            }
        ]

    def test_no_declaration_is_an_honest_unknown_and_runs_nothing(
        self, gates_pool: Any
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        calls: list[dict[str, Any]] = []
        report = self._reader(gates_pool, declaration=None, calls=calls)(
            build_id=BUILD_ID, branch="fix/g"
        )

        assert report.status is GateStatus.UNKNOWN
        assert report.is_green is False  # UNKNOWN is red-safe
        assert calls == []

    def test_a_declaration_with_no_test_command_is_unknown(
        self, gates_pool: Any
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(gates_pool, declaration=_Declaration(None))(
            build_id=BUILD_ID, branch="fix/g"
        )

        assert report.status is GateStatus.UNKNOWN

    def test_an_unmapped_repo_is_unknown_never_the_worktree(
        self, gates_pool: Any
    ) -> None:
        """The declaration is read from the CANONICAL repo, or not at all.

        Falling back to the worktree would let a fix journey that edited
        ``.guardkit/config.yaml`` green itself.
        """
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        calls: list[dict[str, Any]] = []
        report = self._reader(
            gates_pool,
            declaration=_Declaration("npm test"),
            calls=calls,
            repo_paths={},
        )(build_id=BUILD_ID, branch="fix/g")

        assert report.status is GateStatus.UNKNOWN
        assert calls == []

    def test_a_timeout_is_unknown_not_red_and_not_green(
        self, gates_pool: Any
    ) -> None:
        """We did not observe a verdict, so we do not report one."""
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(
            gates_pool, declaration=_Declaration("npm test"), exit_code=None
        )(build_id=BUILD_ID, branch="fix/g")

        assert report.status is GateStatus.UNKNOWN

    def test_a_raising_runner_is_unknown_never_green(self, gates_pool: Any) -> None:
        from forge.cli._serve_conductor import make_gates_green_reader
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        def _boom(**_kw: Any):
            raise RuntimeError("the runner fell over")

        read = make_gates_green_reader(
            pool=gates_pool,
            config=_gates_config({"org/target": "/canonical/target"}),
            declaration_loader=lambda _root: _Declaration("npm test"),
            command_runner=_boom,
        )

        assert read(build_id=BUILD_ID).status is GateStatus.UNKNOWN

    def test_a_missing_build_row_is_unknown(self, gates_pool: Any) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(gates_pool, declaration=_Declaration("npm test"))(
            build_id="build-does-not-exist"
        )

        assert report.status is GateStatus.UNKNOWN

    def test_the_checkpoint_publishes_on_a_green_reader_and_not_on_a_red_one(
        self, gates_pool: Any
    ) -> None:
        """End to end through the REAL checkpoint: the reader decides the card."""
        from forge.cli._serve_conductor import make_merge_ready_checkpoint

        published: list[dict[str, Any]] = []

        def _run_case(exit_code: int) -> Any:
            checkpoint = make_merge_ready_checkpoint(
                pool=gates_pool,
                publish_card=lambda **kw: published.append(kw) or "RESUMED",
                gates_green_reader=self._reader(
                    gates_pool,
                    declaration=_Declaration("npm test"),
                    exit_code=exit_code,
                ),
                published_probe=lambda _bid: False,
            )
            return asyncio.run(
                checkpoint.submit_decision(
                    build_id=BUILD_ID,
                    feature_id="FEAT-G",
                    auto_approve=False,
                    rationale="r",
                )
            )

        red = _run_case(1)
        assert red.card_published is False
        assert published == []

        green = _run_case(0)
        assert green.card_published is True
        assert len(published) == 1


# ---------------------------------------------------------------------------
# Item 3 — the resume seam, over the REAL subscriber surface
# ---------------------------------------------------------------------------


class _FakeSubscriber:
    """Records what the seam asked for; arms exactly as the real one does."""

    def __init__(self, armed: Any, response: Any = "APPROVED") -> None:
        self._armed = armed
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def await_response(
        self,
        build_id: str,
        *,
        stage_label: str,
        attempt_count: int = 0,
        timeout_seconds: int | None = None,
    ) -> Any:
        self.calls.append(
            {
                "build_id": build_id,
                "stage_label": stage_label,
                "attempt_count": attempt_count,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._armed is not None:
            self._armed.set()
        return self._response


class TestTheResumeSeam:
    def test_it_calls_the_REAL_subscriber_method(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The shape correction: ``await_response``, not ``wait_for_response``.

        The Stage-2 seam called a method no subscriber in this tree has.
        A ``SIGNATURE-BINDING`` fake would not have caught it either —
        only calling the seam does. This test binds the fake's own
        ``await_response`` signature against the REAL
        :class:`~forge.adapters.nats.approval_subscriber.ApprovalSubscriber`'s.
        """
        import inspect

        from forge.adapters.nats.approval_subscriber import ApprovalSubscriber
        from forge.cli._serve_conductor import make_conductor_subscribe_resume
        from forge.gating.identity import derive_request_id

        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label="the merge-ready checkpoint",
            attempt_count=2,
        )
        pool.mark_paused(BUILD_ID, request_id)

        made: list[_FakeSubscriber] = []

        def factory(expected_approver: Any, armed: Any) -> Any:
            sub = _FakeSubscriber(armed)
            made.append(sub)
            return sub

        seam = make_conductor_subscribe_resume(
            pool=pool, subscriber_factory=factory, expected_approver="rich"
        )
        armed = asyncio.Event()
        result = asyncio.run(
            seam(BUILD_ID, armed=armed, timeout_seconds=45)
        )

        assert result == "APPROVED"
        assert armed.is_set()
        call = made[0].calls[0]
        # The pair is READ BACK off the durable request_id, not invented.
        assert call["stage_label"] == "the merge-ready checkpoint"
        assert call["attempt_count"] == 2
        assert call["timeout_seconds"] == 45
        # And the call is one the real subscriber would accept.
        inspect.signature(ApprovalSubscriber.await_response).bind(
            object(), BUILD_ID, **{k: v for k, v in call.items() if k != "build_id"}
        )

    def test_no_pending_request_arms_and_answers_immediately(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Never leave the driver's arm-timeout to fire on a resolved wait."""
        from forge.cli._serve_conductor import make_conductor_subscribe_resume

        called: list[int] = []
        seam = make_conductor_subscribe_resume(
            pool=pool,
            subscriber_factory=lambda *_a: called.append(1),
        )
        armed = asyncio.Event()

        assert asyncio.run(seam(BUILD_ID, armed=armed, timeout_seconds=5)) is None
        assert armed.is_set()
        assert called == []

    def test_an_unparseable_legacy_id_still_waits(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        from forge.cli._serve_conductor import make_conductor_subscribe_resume

        pool.mark_paused(BUILD_ID, "a-legacy-id-with-no-structure")
        made: list[_FakeSubscriber] = []

        seam = make_conductor_subscribe_resume(
            pool=pool,
            subscriber_factory=lambda _a, armed: made.append(
                _FakeSubscriber(armed)
            )
            or made[-1],
        )
        armed = asyncio.Event()

        assert asyncio.run(seam(BUILD_ID, armed=armed, timeout_seconds=5))
        assert made[0].calls[0]["attempt_count"] == 0

    def test_an_unwired_factory_leaves_the_driver_seam_none(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The honest degrade: stop loudly, never spin-poll."""
        deps = build_conductor_driver_deps_factory(
            pool=pool, config=_config(conductor_on=True), subscriber_factory=None
        )(BUILD_ID, object())

        assert deps.subscribe_resume is None

    def test_a_wired_factory_fills_the_driver_seam(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(conductor_on=True),
            subscriber_factory=lambda _a, armed: _FakeSubscriber(armed),
        )(BUILD_ID, object())

        assert deps.subscribe_resume is not None

    def test_the_composition_wires_the_seam_when_a_client_is_present(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Item 3's composition half: the client reaches the deps factory."""
        captured: dict[str, Any] = {}
        original = serve_mod.build_conductor_router

        class _FakeNats:
            async def subscribe(self, subject: str, callback: Any) -> Any:
                return object()

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        serve_mod.build_conductor_router = _capture  # type: ignore[assignment]
        try:
            serve_mod._compose_conductor_router(
                sqlite_pool=pool,
                forge_config=_config(conductor_on=True),
                lifecycle_emitter=None,
                gate_parts=None,
                gate_repository=None,
                gate_state_machine=None,
                clock=lambda: None,
                nats_client=_FakeNats(),
            )
        finally:
            serve_mod.build_conductor_router = original  # type: ignore[assignment]

        deps = captured["driver_deps_factory"](BUILD_ID, object())
        assert deps.subscribe_resume is not None

    def test_the_composition_leaves_the_seam_unwired_without_a_client(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        captured: dict[str, Any] = {}
        original = serve_mod.build_conductor_router

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        serve_mod.build_conductor_router = _capture  # type: ignore[assignment]
        try:
            serve_mod._compose_conductor_router(
                sqlite_pool=pool,
                forge_config=_config(conductor_on=True),
                lifecycle_emitter=None,
                gate_parts=None,
                gate_repository=None,
                gate_state_machine=None,
                clock=lambda: None,
            )
        finally:
            serve_mod.build_conductor_router = original  # type: ignore[assignment]

        deps = captured["driver_deps_factory"](BUILD_ID, object())
        assert deps.subscribe_resume is None


# ---------------------------------------------------------------------------
# Item 5 — the one-card latch, restart-safe
# ---------------------------------------------------------------------------


class TestTheDurableOneCardLatch:
    @staticmethod
    def _checkpoint(pool: Any, published: list) -> Any:
        from forge.cli._serve_conductor import make_merge_ready_checkpoint

        return make_merge_ready_checkpoint(
            pool=pool,
            publish_card=lambda **kw: published.append(kw) or "RESUMED",
            gates_green_reader=lambda **_kw: True,
        )

    @staticmethod
    def _submit(checkpoint: Any) -> Any:
        return asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-FIX007",
                auto_approve=False,
                rationale="r",
            )
        )

    def test_a_fresh_publisher_over_a_carded_build_publishes_nothing(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """THE RESTART CASE. Two publisher instances, one card.

        Before the durable half, a daemon restart mid-journey built a
        fresh publisher with an empty in-memory latch and would card the
        same build again — two cards for one merge word.
        """
        from forge.pipeline.merge_ready_checkpoint import MergeCardOutcome

        published: list[dict[str, Any]] = []

        first = self._submit(self._checkpoint(pool, published))
        assert first.outcome is MergeCardOutcome.CARD_PUBLISHED

        # Simulate what the gate's own writer does on that publish: the
        # durable row bearing the merge card's target identifier.
        self._record_a_merge_card_row(pool)

        # A NEW publisher — exactly what a restarted daemon composes.
        second = self._submit(self._checkpoint(pool, published))

        assert second.outcome is MergeCardOutcome.ALREADY_CHECKPOINTED
        assert len(published) == 1

    def test_without_the_durable_row_a_fresh_publisher_would_re_card(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The control that makes the test above mean something.

        Same two instances, no durable row — the in-memory latch alone
        does not survive, which is precisely the hole item 5 named.
        """
        published: list[dict[str, Any]] = []

        self._submit(self._checkpoint(pool, published))
        self._submit(self._checkpoint(pool, published))

        assert len(published) == 2

    def test_a_raising_probe_never_wedges_a_journey_that_never_carded(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        from forge.cli._serve_conductor import make_merge_ready_checkpoint
        from forge.pipeline.merge_ready_checkpoint import MergeCardOutcome

        published: list[dict[str, Any]] = []

        def _boom(_build_id: str) -> bool:
            raise RuntimeError("the probe fell over")

        decision = self._submit(
            make_merge_ready_checkpoint(
                pool=pool,
                publish_card=lambda **kw: published.append(kw) or "RESUMED",
                gates_green_reader=lambda **_kw: True,
                published_probe=_boom,
            )
        )

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(published) == 1

    def test_the_probe_reads_the_gates_own_row(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        from forge.cli._serve_conductor import (
            make_conductor_merge_card_published_probe,
        )

        probe = make_conductor_merge_card_published_probe(pool=pool)
        assert probe(BUILD_ID) is False

        self._record_a_merge_card_row(pool)
        assert probe(BUILD_ID) is True

    def test_an_unrelated_gate_row_is_not_a_merge_card(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The pre-dispatch gate's card must not latch the merge card."""
        from datetime import datetime, timezone

        from forge.cli._serve_conductor import (
            make_conductor_merge_card_published_probe,
        )
        from forge.lifecycle.persistence import StageLogEntry

        now = datetime.now(timezone.utc)
        pool.record_stage(
            StageLogEntry(
                build_id=BUILD_ID,
                stage_label="autobuild",
                target_kind="subagent",
                target_identifier="autobuild_runner",
                status="GATED",
                gate_mode=None,
                coach_score=None,
                threshold_applied=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={},
            )
        )

        assert make_conductor_merge_card_published_probe(pool=pool)(BUILD_ID) is False

    @staticmethod
    def _record_a_merge_card_row(pool: Any) -> None:
        from datetime import datetime, timezone

        from forge.cli._serve_gate_activation import _MERGE_CARD_TARGET_IDENTIFIER
        from forge.lifecycle.persistence import StageLogEntry
        from forge.pipeline.merge_ready_checkpoint import (
            MERGE_READY_CHECKPOINT_LABEL,
        )

        now = datetime.now(timezone.utc)
        pool.record_stage(
            StageLogEntry(
                build_id=BUILD_ID,
                stage_label=MERGE_READY_CHECKPOINT_LABEL,
                target_kind="subagent",
                target_identifier=_MERGE_CARD_TARGET_IDENTIFIER,
                status="GATED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                coach_score=None,
                threshold_applied=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={"gate_pause": {"request_id": "r", "attempt_count": 0}},
            )
        )
