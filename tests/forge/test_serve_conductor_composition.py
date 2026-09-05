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

#: The fix journey's seat, config-as-code (conductor-activation design
#: pass §2). ``conductor.enabled: true`` with no seat REFUSES at config
#: load, so every switched-on config in this module names one.
SEAT = "qwen3-coder-30b"


def _config(
    *, conductor_on: bool, leg_budgets: dict[str, int] | None = None
) -> ForgeConfig:
    raw: dict[str, Any] = {
        "pipeline": {
            "build_queue_subject": "pipeline.build-queued.team-a",
            "approved_originators": ["terminal"],
        },
        "permissions": {"filesystem": {"allowlist": ["/work"]}},
    }
    if conductor_on:
        raw["conductor"] = {"enabled": True, "seat": SEAT}
    if leg_budgets is not None:
        # On the reserved ``attended`` profile deliberately: the build row
        # this module inserts requests no profile, so ``attended`` is what
        # it resolves — and leg budgets are NOT caps, so arming it with
        # them is legal where arming it with a cap is not (ASSUM-010).
        raw["budget"] = {"profiles": {"attended": dict(leg_budgets)}}
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


class TestTheLegSeatIsConfigAsCode:
    """Conductor-activation design pass §2 — the seat, and the env DELETED.

    LI stage-2 landed the seat as a stopgap operator env var and ledgered
    its own replacement: "When that field lands, this env read is DELETED,
    not kept beside it: two statements of one rule is a future lie." The
    field landed as ``conductor.seat``; these are the env thread's
    config-sourced equivalents, plus the pin that the env is gone.
    """

    def test_the_env_thread_is_gone_not_kept_beside_the_field(self) -> None:
        """The ledger comment's own law, enforced structurally.

        Two statements of one rule is a future lie — so the constant, the
        export and the read must all be absent, and a stale
        ``FORGE_CONDUCTOR_LEG_MODEL`` in an operator's environment must
        change NOTHING.

        Pinned by NAME, not by banning ``os.environ`` module-wide: this
        module has no business reading the seat from the environment ever
        again, but it may perfectly well grow an unrelated environment read
        one day, and a blanket ban would make that a test failure with
        nothing to say. The names are hunted in the parsed AST rather than
        in the raw text so the ledger COMMENT recording the deletion — the
        note that exists to stop anyone re-introducing it — is not itself
        read as a re-introduction.
        """
        import ast

        source = Path(serve_conductor_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        code_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        code_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }

        assert not hasattr(serve_conductor_mod, "CONDUCTOR_LEG_MODEL_ENV")
        assert "CONDUCTOR_LEG_MODEL_ENV" not in serve_conductor_mod.__all__
        assert "CONDUCTOR_LEG_MODEL_ENV" not in code_names
        assert "CONDUCTOR_LEG_MODEL_ENV" not in code_literals
        assert "FORGE_CONDUCTOR_LEG_MODEL" not in code_literals

    def test_a_stale_env_var_no_longer_seats_a_leg(
        self, pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deleted thread, driven — an operator's leftover export is inert."""
        monkeypatch.setenv("FORGE_CONDUCTOR_LEG_MODEL", "some-stale-seat")
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert "--model" not in runner.calls[0]["args"]

    def test_no_seat_means_the_argv_names_no_model_at_all(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The disabled-conductor argv, byte-identical to what it always was."""
        runner = _runner()
        supervisor = _supervisor_factory(
            pool, subprocess_runner=runner, leg_model=None
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert "--model" not in runner.calls[0]["args"]

    def test_the_config_seat_names_the_seat_on_both_legs(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """One seat serves both legs — today's reality, pinned."""
        config = _config(conductor_on=True)
        runner = _runner()
        supervisor = _supervisor_factory(
            pool,
            config=config,
            subprocess_runner=runner,
            leg_model=config.conductor.seat,
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)
        _drive(supervisor, StageClass.TASK_WORK)

        assert len(runner.calls) == 2
        for call in runner.calls:
            assert call["args"][-2:] == ["--model", SEAT]

    def test_a_blank_seat_is_read_as_unset_not_as_an_empty_seat(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """``--model ''`` would be worse than no flag: a named nothing.

        The config model already normalises blank to ``None``; the factory
        keeps the same posture so an injected blank cannot reach the argv.
        """
        runner = _runner()
        supervisor = _supervisor_factory(
            pool, subprocess_runner=runner, leg_model="   "
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert "--model" not in runner.calls[0]["args"]

    def test_the_production_composition_root_threads_the_config_seat(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The edited call site itself — not a re-statement of the wiring.

        This is the seam the whole field exists for: the daemon's own
        ``_compose_conductor_router`` must hand the factory
        ``config.conductor.seat``. Capture the factory's kwargs at the
        real production call.
        """
        captured: dict[str, Any] = {}
        original = serve_conductor_mod.build_conductor_supervisor_factory

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        serve_conductor_mod.build_conductor_supervisor_factory = _capture  # type: ignore[assignment]
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
            serve_conductor_mod.build_conductor_supervisor_factory = original  # type: ignore[assignment]

        assert captured["leg_model"] == SEAT

    def test_a_disabled_boot_threads_no_seat(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Flag off = no seat on the wire, so the argv cannot drift."""
        captured: dict[str, Any] = {}
        original = serve_conductor_mod.build_conductor_supervisor_factory

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        serve_conductor_mod.build_conductor_supervisor_factory = _capture  # type: ignore[assignment]
        try:
            serve_mod._compose_conductor_router(
                sqlite_pool=pool,
                forge_config=_config(conductor_on=False),
                lifecycle_emitter=None,
                gate_parts=None,
                gate_repository=None,
                gate_state_machine=None,
                clock=lambda: None,
            )
        finally:
            serve_conductor_mod.build_conductor_supervisor_factory = original  # type: ignore[assignment]

        assert captured["leg_model"] is None


class TestThePackReaderIsWired:
    """Conductor rewire rule 3 — the gap the design pass found on 5 September.

    ``cli/serve.py`` composed the conductor WITHOUT
    ``failure_pack_source_reader``, so a journey read the failure pack under
    its own build id, found nothing, and would have reviewed blind. These
    capture the two production call sites and prove a reader now reaches
    both — and that it answers with the failed build the repair names.
    """

    @staticmethod
    def _capture(module_attr: str) -> Any:
        return getattr(serve_conductor_mod, module_attr)

    def _compose(self, pool: SqliteLifecyclePersistence, attr: str) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        original = getattr(serve_conductor_mod, attr)

        def _capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        setattr(serve_conductor_mod, attr, _capture)
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
            setattr(serve_conductor_mod, attr, original)
        return captured

    def test_the_supervisor_factory_gets_a_pack_reader(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        captured = self._compose(pool, "build_conductor_supervisor_factory")

        assert captured["failure_pack_source_reader"] is not None

    def test_the_driver_deps_factory_gets_the_same_reader(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The failure pack a journey WRITES points back at the same build."""
        captured = self._compose(pool, "build_conductor_driver_deps_factory")

        assert captured["source_build_id_reader"] is not None

    def test_the_reader_names_the_failed_build_the_repair_came_from(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        captured = self._compose(pool, "build_conductor_supervisor_factory")
        pool.connection.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, "
            "queued_at, mode) VALUES ('build-repair-1', 'FEAT-44A8', "
            "'appmilla/api_test', 'main', 'f.yaml', 'QUEUED', 'cli', "
            "'fix-build-FEAT-44A8-20260904131328', '2026-09-05T00:00:00Z', "
            "'mode-c')"
        )
        pool.connection.commit()

        read = captured["failure_pack_source_reader"]

        assert read("build-repair-1") == "build-FEAT-44A8-20260904131328"


class TestTheLegBudgetsAreYamlKnobs:
    """The experiment round's knobs, wired at the composition site.

    Forge threaded NO leg budgets: the dispatcher's only extra argv was
    ``--model <seat>``, so the build system's hardcoded 2 turns / 420s /
    1620s governed production and moving them was an image-level change.
    These drive the COMPOSED production path — the argv that reaches the
    runner — rather than re-reading the schema.
    """

    def test_a_profile_with_no_leg_budgets_is_todays_argv_exactly(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Every profile deployed today resolves here."""
        runner = _runner()
        supervisor = _supervisor_factory(pool, subprocess_runner=runner)(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)
        _drive(supervisor, StageClass.TASK_WORK)

        for call in runner.calls:
            for flag in ("--max-turns", "--sdk-timeout", "--leg-budget"):
                assert flag not in call["args"]

    def test_the_profiles_leg_budgets_reach_the_legs_argv(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Both leg kinds are threaded — each with the flags it declares.

        ``--sdk-timeout`` is declared by BOTH ``guardkit task-review`` and
        ``guardkit task-work``; ``--max-turns`` and ``--leg-budget`` by the
        work leg alone, and Click exits 2 on an undeclared option before
        the command body runs, so threading them onto a review would kill
        the journey at its opening leg rather than shorten it.
        """
        runner = _runner()
        supervisor = _supervisor_factory(
            pool,
            subprocess_runner=runner,
            config=_config(
                conductor_on=True,
                leg_budgets={
                    "leg_max_turns": 4,
                    "leg_sdk_timeout_seconds": 300,
                    "leg_budget_seconds": 900,
                },
            ),
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)
        _drive(supervisor, StageClass.TASK_WORK)

        review, work = runner.calls[0]["args"], runner.calls[1]["args"]

        assert review[-2:] == ["--sdk-timeout", "300"]
        assert "--max-turns" not in review
        assert "--leg-budget" not in review
        assert work[-6:] == [
            "--max-turns",
            "4",
            "--sdk-timeout",
            "300",
            "--leg-budget",
            "900",
        ]

    def test_the_seat_and_the_budgets_ride_the_same_argv(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        runner = _runner()
        supervisor = _supervisor_factory(
            pool,
            subprocess_runner=runner,
            leg_model=SEAT,
            config=_config(
                conductor_on=True, leg_budgets={"leg_sdk_timeout_seconds": 300}
            ),
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert runner.calls[0]["args"][-4:] == [
            "--model",
            SEAT,
            "--sdk-timeout",
            "300",
        ]

    def test_the_legs_budgets_come_off_the_SAME_resolved_profile(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """One resolution of ``builds.profile``, not two.

        The caps the supervisor judges the build against and the budgets
        its legs are handed must be the same profile by construction. The
        factory reads the guards out of the budget kwargs it already
        built; substituting the builder proves it does not resolve a
        second time behind its own back.
        """
        from forge.config.models import BudgetGuards

        guards = BudgetGuards(leg_sdk_timeout_seconds=111)
        runner = _runner()
        supervisor = _supervisor_factory(
            pool,
            subprocess_runner=runner,
            budget_kwargs_builder=lambda **_kw: {
                "budget_guards": guards,
                "budget_profile_name": "substituted",
            },
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        assert supervisor.budget_guards is guards
        assert runner.calls[0]["args"][-2:] == ["--sdk-timeout", "111"]

    def test_a_budget_builder_that_supplies_no_guards_appends_nothing(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """``budget_kwargs_builder`` is injectable; absent guards are inert."""
        runner = _runner()
        supervisor = _supervisor_factory(
            pool, subprocess_runner=runner, budget_kwargs_builder=lambda **_kw: {}
        )(BUILD_ID)

        _drive(supervisor, StageClass.TASK_REVIEW)

        for flag in ("--max-turns", "--sdk-timeout", "--leg-budget"):
            assert flag not in runner.calls[0]["args"]


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

    def test_close_out_records_a_row(self, pool: SqliteLifecyclePersistence) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool, config=_config(conductor_on=True)
        )(BUILD_ID, supervisor=object())

        deps.close_out(build_id=BUILD_ID, report=_TurnReportDouble())

        labels = [r.stage_label for r in pool.read_stages(BUILD_ID)]
        assert "conductor-close-out" in labels


class _Decision:
    """Duck-typed stand-in for a ``ModeCTerminalDecision``."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _TurnReportDouble:
    """Duck-typed stand-in for a ``TurnReport`` at close-out time."""

    def __init__(
        self,
        *,
        outcome: str | None = None,
        rationale: str = "terminal",
        dispatch_result: object | None = None,
    ) -> None:
        self.outcome = outcome
        self.rationale = rationale
        self.dispatch_result = dispatch_result


class TestTheTerminalRowTransition:
    """The stuck row, 2026-08-03.

    The first production fix journey reached its terminal, logged "closed
    out" — and left ``builds.status = RUNNING`` with an empty ``error``,
    forever, because this seam recorded a ``stage_log`` row and stopped.
    The FTR lesson it cited ("terminal transitions are owned by the
    lifecycle bridge and the gate's own state machine") is true of the
    merge-card path and of nothing else the conductor reaches.
    """

    @staticmethod
    def _close_out(pool: SqliteLifecyclePersistence):
        return build_conductor_driver_deps_factory(
            pool=pool, config=_config(conductor_on=True)
        )(BUILD_ID, supervisor=object()).close_out

    def test_a_clean_review_terminal_completes_the_row(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        self._close_out(pool)(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="terminal",
                rationale="clean-review-no-fixes: mode-c-task-review-empty",
                dispatch_result=_Decision("clean-review-no-fixes"),
            ),
        )

        row = pool.get_build_row(BUILD_ID)
        assert row.status.value == "COMPLETE"
        assert row.completed_at is not None
        # ``forge status`` renders this column as the failure text; a
        # successful journey must not leave prose in it.
        assert not row.error

    def test_a_failed_terminal_fails_the_row_with_the_reason(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        self._close_out(pool)(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="terminal",
                rationale=(
                    "failed: mode-c-task-review-leg-failed (/task-review "
                    "failed: REFUSED (Phase 0, ad-hoc task creation))"
                ),
                dispatch_result=_Decision("failed"),
            ),
        )

        row = pool.get_build_row(BUILD_ID)
        assert row.status.value == "FAILED"
        assert "mode-c-task-review-leg-failed" in (row.error or "")
        assert "REFUSED (Phase 0" in (row.error or "")

    def test_a_terminal_with_an_unreadable_decision_still_leaves_running(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The supervisor's own handler-raised fallback reaches here.

        It carries no decision at all. Guessing COMPLETE would claim a
        delivery, so the row fails — and the reason names the gap.
        """
        self._close_out(pool)(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="terminal", rationale="MODE_C planner halted cycle"
            ),
        )

        row = pool.get_build_row(BUILD_ID)
        assert row.status.value == "FAILED"
        assert row.error

    def test_the_merge_card_path_is_left_to_its_own_writer(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The FTR lesson, kept exactly where it applies."""

        class _Card:
            card_published = True
            card_result = "RESUMED"

        self._close_out(pool)(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="dispatched",
                rationale="the merge card was published",
                dispatch_result=_Card(),
            ),
        )

        assert pool.get_build_row(BUILD_ID).status.value == "RUNNING"

    def test_an_already_terminal_row_is_left_alone(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Never resurrect, never overwrite: the one careful writer's rule."""
        close_out = self._close_out(pool)
        close_out(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="terminal",
                rationale="failed: mode-c-task-review-leg-failed",
                dispatch_result=_Decision("failed"),
            ),
        )
        first = pool.get_build_row(BUILD_ID)

        close_out(
            build_id=BUILD_ID,
            report=_TurnReportDouble(
                outcome="terminal",
                rationale="clean-review-no-fixes: mode-c-task-review-empty",
                dispatch_result=_Decision("clean-review-no-fixes"),
            ),
        )

        after = pool.get_build_row(BUILD_ID)
        assert after.status.value == "FAILED"
        assert after.error == first.error


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
        "conductor": {"enabled": True, "seat": SEAT},
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


# ---------------------------------------------------------------------------
# The routing law's close-side check (card Q8/A.2, second half) — the
# ``stamps_satisfied`` leg on the merge-ready checkpoint
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    import os
    import subprocess

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
    )
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env
    ).stdout


def _write_f4_envelope(
    worktree: Path, run_id: str, *, started: str, verdict: str = "pass", hurl_exit: int = 0
) -> Path:
    import json

    history = worktree / "qa" / "gates" / "history"
    history.mkdir(parents=True, exist_ok=True)
    out = history / f"{run_id}.json"
    out.write_text(
        json.dumps(
            {
                "format_version": "1.0",
                "run_id": run_id,
                "feature_id": "FEAT-G",
                "target_env": "local",
                "started": started,
                "finished": started,
                "preflight": {"checks": [], "instrument_ok": True},
                "gates": [
                    {"gate_id": "health", "exit_code": 0, "assertions": []},
                    {"gate_id": "hurl-twins", "exit_code": hurl_exit, "assertions": []},
                ],
                "verdict": verdict,
            }
        )
    )
    return out


@pytest.fixture
def stamped_repo(tmp_path: Path) -> "dict[str, Any]":
    """A fixture repo pair: a CANONICAL checkout carrying the stamped feature
    YAML (the plan of record) and a fix-branch WORKTREE that is a real git
    repo with one code commit at 10:00Z — plus a build row pointing at both.
    """
    canonical = tmp_path / "canonical"
    features = canonical / ".guardkit" / "features"
    features.mkdir(parents=True)
    (features / "FEAT-G.yaml").write_text(
        "id: FEAT-G\n"
        "name: sign-in\n"
        "routing_law: enforced\n"
        "feature_files:\n"
        "  - features/sign-in.feature\n"
        "scenarios:\n"
        "  'User signs in with valid credentials': hurl\n"
        "  'Rate limiter refuses the 6th attempt':\n"
        "    verifier: toolchain\n"
        "    test_ref: test_rate_limiter_refuses_sixth\n"
        "  'Owner reads the merge card in Slack': operator\n"
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", "fix/g")
    (worktree / "app.py").write_text("print('fix')\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-q", "-m", "the fix", when="2026-08-15T10:00:00+00:00")

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
    return {
        "pool": SqliteLifecyclePersistence(connection=cx),
        "canonical": canonical,
        "worktree": worktree,
    }


class TestTheRoutingLawCloseSide:
    """Card A.2 second half: stamped verifier did not run = ABSENT = no card.

    Every test here runs the REAL default leg (``make_stamps_leg()`` — real
    YAML reader, real envelope reader, real git) through the REAL reader and,
    for the checkpoint-level cases, the REAL ``MergeReadyCheckpointPublisher``.
    Only the declared toolchain command is faked (exit 0, subprocess-free).
    """

    @staticmethod
    def _reader(repo: "dict[str, Any]") -> Any:
        from forge.cli._serve_conductor import make_gates_green_reader

        def _green_suite(*, command: str, cwd: Any, timeout_seconds: int):
            return 0, f"`{command}` exited 0"

        return make_gates_green_reader(
            pool=repo["pool"],
            config=_gates_config({"org/target": str(repo["canonical"])}),
            declaration_loader=lambda _root: _Declaration("uv run pytest -q"),
            command_runner=_green_suite,
        )

    @staticmethod
    def _submit(repo: "dict[str, Any]", reader: Any, published: list) -> Any:
        from forge.cli._serve_conductor import make_merge_ready_checkpoint

        checkpoint = make_merge_ready_checkpoint(
            pool=repo["pool"],
            publish_card=lambda **kw: published.append(kw) or "RESUMED",
            gates_green_reader=reader,
            published_probe=lambda _bid: False,
        )
        return asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID, feature_id="FEAT-G", auto_approve=False, rationale="r"
            )
        )

    def test_a_green_suite_with_a_hurl_stamp_and_no_envelope_is_absent_no_card(
        self, stamped_repo: "dict[str, Any]"
    ) -> None:
        """THE close-side check: the suite is green, the card is still refused,
        and the detail names the scenario and the missing home in plain words."""
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.UNKNOWN
        assert report.failed_gates == (
            "routing law: hurl (scenario 'User signs in with valid credentials')",
        )
        assert "'User signs in with valid credentials'" in report.detail
        assert "`verifier: hurl`" in report.detail
        assert "no results envelope exists under" in report.detail
        assert "ABSENT is UNKNOWN, and UNKNOWN publishes no merge card" in report.detail
        # The operator scenario is LISTED even on a blocked close.
        assert "ATTENDED" in report.detail
        assert "'Owner reads the merge card in Slack'" in report.detail
        # And the suite's own green is stated so nobody goes looking for a red test.
        assert "The declared suite itself is green" in report.detail

        published: list[dict[str, Any]] = []
        decision = self._submit(stamped_repo, self._reader(stamped_repo), published)
        assert decision.card_published is False
        assert published == []
        assert "no card" in decision.rationale

    def test_a_stale_envelope_is_absent_no_card(self, stamped_repo: "dict[str, Any]") -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        _write_f4_envelope(
            stamped_repo["worktree"], "FEAT-G-local-stale", started="2026-08-15T09:00:00+00:00"
        )
        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.UNKNOWN
        assert "STALE" in report.detail
        assert "FEAT-G-local-stale" in report.detail

    def test_a_fresh_green_hurl_envelope_publishes_the_card_and_lists_attended(
        self, stamped_repo: "dict[str, Any]"
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        _write_f4_envelope(
            stamped_repo["worktree"], "FEAT-G-local-fresh", started="2026-08-15T10:00:01+00:00"
        )
        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.GREEN
        assert "all 3 stamped scenario(s)" in report.detail
        assert "hurl: 1" in report.detail and "toolchain: 1" in report.detail
        assert "FEAT-G-local-fresh" in report.detail
        # operator: satisfied by declaration, LOGGED attended in the card text
        assert "ATTENDED" in report.detail
        assert "'Owner reads the merge card in Slack'" in report.detail

        published: list[dict[str, Any]] = []
        decision = self._submit(stamped_repo, self._reader(stamped_repo), published)
        assert decision.card_published is True
        assert len(published) == 1
        assert published[0]["gates"].status is GateStatus.GREEN

    def test_a_fresh_envelope_whose_hurl_gate_failed_is_absent(
        self, stamped_repo: "dict[str, Any]"
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        _write_f4_envelope(
            stamped_repo["worktree"],
            "FEAT-G-local-red",
            started="2026-08-15T10:00:01+00:00",
            verdict="fail",
            hurl_exit=1,
        )
        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.UNKNOWN
        assert "verdict `fail`, not `pass`" in report.detail

    def test_no_stamps_is_not_enforced_and_the_report_is_untouched(
        self, stamped_repo: "dict[str, Any]"
    ) -> None:
        """Backward compatibility: a feature with no stamps reads exactly what
        the toolchain leg said — same status, same detail, no routing-law text."""
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        feature = stamped_repo["canonical"] / ".guardkit" / "features" / "FEAT-G.yaml"
        feature.write_text("id: FEAT-G\nname: sign-in\ntasks: []\n")
        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.GREEN
        assert report.detail == "`uv run pytest -q` exited 0"

    def test_no_feature_yaml_at_all_is_not_enforced(self, stamped_repo: "dict[str, Any]") -> None:
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        (stamped_repo["canonical"] / ".guardkit" / "features" / "FEAT-G.yaml").unlink()
        report = self._reader(stamped_repo)(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.GREEN
        assert report.detail == "`uv run pytest -q` exited 0"

    def test_a_red_suite_never_reaches_the_stamps_leg(self, stamped_repo: "dict[str, Any]") -> None:
        """Order of decision: a red suite is RED (fix loop), not UNKNOWN."""
        from forge.cli._serve_conductor import make_gates_green_reader
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        calls: list[str] = []

        def _leg(**kw: Any):
            calls.append("leg")
            raise AssertionError("must not be reached")

        reader = make_gates_green_reader(
            pool=stamped_repo["pool"],
            config=_gates_config({"org/target": str(stamped_repo["canonical"])}),
            declaration_loader=lambda _root: _Declaration("uv run pytest -q"),
            command_runner=lambda **_kw: (1, "exited 1"),
            stamps_leg=_leg,
        )
        report = reader(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.RED
        assert calls == []

    def test_a_raising_leg_is_unknown_never_green(self, stamped_repo: "dict[str, Any]") -> None:
        from forge.cli._serve_conductor import make_gates_green_reader
        from forge.pipeline.merge_ready_checkpoint import GateStatus

        def _leg(**kw: Any):
            raise RuntimeError("stamps reader on fire")

        reader = make_gates_green_reader(
            pool=stamped_repo["pool"],
            config=_gates_config({"org/target": str(stamped_repo["canonical"])}),
            declaration_loader=lambda _root: _Declaration("uv run pytest -q"),
            command_runner=lambda **_kw: (0, "exited 0"),
            stamps_leg=_leg,
        )
        report = reader(build_id=BUILD_ID, branch="fix/g")
        assert report.status is GateStatus.UNKNOWN
        assert "stamps reader on fire" in report.detail

    def test_the_leg_receives_the_canonical_root_the_worktree_and_the_branch(
        self, stamped_repo: "dict[str, Any]"
    ) -> None:
        from forge.cli._serve_conductor import make_gates_green_reader
        from forge.pipeline.routing_stamps import StampsStatus, StampsVerdict

        seen: list[dict[str, Any]] = []

        def _leg(**kw: Any):
            seen.append(kw)
            return StampsVerdict(status=StampsStatus.NOT_ENFORCED)

        reader = make_gates_green_reader(
            pool=stamped_repo["pool"],
            config=_gates_config({"org/target": str(stamped_repo["canonical"])}),
            declaration_loader=lambda _root: _Declaration("uv run pytest -q"),
            command_runner=lambda **_kw: (0, "exited 0"),
            stamps_leg=_leg,
        )
        reader(build_id=BUILD_ID, branch="fix/g")
        assert seen == [
            {
                "feature_id": "FEAT-G",
                "repo_root": str(stamped_repo["canonical"]),
                "worktree": str(stamped_repo["worktree"]),
                "branch": "fix/g",
                "toolchain_green": True,
            }
        ]
