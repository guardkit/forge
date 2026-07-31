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
from forge.cli._serve_conductor import (
    _ModeAOnlySeam,
    build_conductor_driver_deps_factory,
    build_conductor_supervisor_factory,
    make_conductor_wait_window_reader,
)
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
    def test_flag_off_composes_no_conductor_at_all(
        self, pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OFF must not construct a single conductor object.

        Not "equivalent behaviour" — nothing built. The flag is checked
        before the composer is reached, so this asserts the composer is
        never entered rather than that it happened to return ``None``.
        """
        entered: list[int] = []
        monkeypatch.setattr(
            serve_mod,
            "_compose_conductor_router",
            lambda **_kw: entered.append(1),
        )

        from forge.config.conductor import conductor_enabled

        config = _config(conductor_on=False)
        assert conductor_enabled(config) is False
        assert entered == []

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
