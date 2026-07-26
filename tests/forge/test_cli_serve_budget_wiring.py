"""Tests for the FEAT-UBS-002 serve-side budget wiring (INERT until activation).

These cover the production budget values ``serve.py`` now builds for a future
Mode-C ``next_turn`` driver:

* :func:`forge.cli.serve.resolve_budget_for_build` — the FIRST production
  ``BudgetConfig.resolve``: ``builds.profile`` → caps, reaching the Supervisor.
* :func:`forge.cli.serve.make_budget_pause` — the ADR-ARCH-021-ordered pause
  collaborator (publish → mark_paused → emit_paused), plus its loud-degrade on
  an ``InvalidTransitionError`` race.
* The Supervisor's wall-clock cap (the first wall-clock-cap coverage): a breach
  pauses; a missing reader / clock is fail-open (0.0 elapsed → never a breach).

Nothing is *activated* by this lane — the Supervisor/Mode-C path has no
production caller — so these tests exercise the wiring's shape and honesty, not
a live enforcement loop. AAA throughout; ``tmp_path`` keeps SQLite test-local.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import (
    build_supervisor,
    make_budget_pause,
    make_budget_started_at_reader,
    resolve_budget_for_build,
)
from forge.config.models import BudgetConfig, BudgetGuards
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.state_machine import (
    BuildState,
    InvalidTransitionError,
)
from forge.lifecycle.state_machine import (
    transition as compose_transition,
)
from forge.pipeline.budget_guard import (
    BuildBudgetMetrics,
    BudgetVerdict,
    build_budget_breach_approval_payload,
)
from forge.pipeline.supervisor import Supervisor, TurnOutcome

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str = "FEAT-UBS-002",
    correlation_id: str = "corr-ubs-002",
) -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/example/feature.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        requested_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture()
def writer_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def pool(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db)


def _budget_config() -> BudgetConfig:
    """A config with the reserved ``attended`` (caps off) profile + a capped one."""
    return BudgetConfig(
        default_profile="attended",
        profiles={
            "attended": BudgetGuards(),
            "capped": BudgetGuards(
                max_review_cycles=3,
                max_build_wallclock_seconds=10,
            ),
        },
    )


def _seed_running_build(
    pool: SqliteLifecyclePersistence,
    *,
    profile: str | None,
) -> str:
    """Seed one build and drive it QUEUED → PREPARING → RUNNING (started_at set)."""
    build_id = pool.record_pending_build(_make_payload(), profile=profile)
    pool.apply_transition(
        compose_transition(
            Build(build_id=build_id, status=BuildState.QUEUED),
            BuildState.PREPARING,
        )
    )
    pool.apply_transition(
        compose_transition(
            Build(build_id=build_id, status=BuildState.PREPARING),
            BuildState.RUNNING,
        )
    )
    return build_id


def _build_supervisor_kwargs() -> dict[str, Any]:
    """Collaborators for :func:`build_supervisor` (it builds the autobuild
    dispatcher internally from the closure collaborators, so that one is
    omitted here)."""
    return {
        "ordering_guard": MagicMock(name="ordering_guard"),
        "per_feature_sequencer": MagicMock(name="per_feature_sequencer"),
        "constitutional_guard": MagicMock(name="constitutional_guard"),
        "state_reader": MagicMock(name="state_reader"),
        "ordering_stage_log_reader": MagicMock(name="ordering_stage_log_reader"),
        "per_feature_stage_log_reader": MagicMock(
            name="per_feature_stage_log_reader"
        ),
        "async_task_reader": MagicMock(name="async_task_reader"),
        "reasoning_model": MagicMock(name="reasoning_model"),
        "turn_recorder": MagicMock(name="turn_recorder"),
        "specialist_dispatcher": AsyncMock(name="specialist_dispatcher"),
        "subprocess_dispatcher": AsyncMock(name="subprocess_dispatcher"),
        "pr_review_gate": MagicMock(name="pr_review_gate"),
    }


def _supervisor_collaborators() -> dict[str, Any]:
    """Collaborators for a direct :class:`Supervisor` construction (includes
    the autobuild dispatcher)."""
    return {
        **_build_supervisor_kwargs(),
        "autobuild_dispatcher": AsyncMock(name="autobuild_dispatcher"),
    }


# ---------------------------------------------------------------------------
# (b) resolve_budget_for_build — the first production BudgetConfig.resolve
# ---------------------------------------------------------------------------


class TestResolveBudgetForBuild:
    """``resolve_budget_for_build`` maps ``builds.profile`` → caps, end to end."""

    def test_row_profile_resolves_and_reaches_the_supervisor(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_running_build(pool, profile="capped")
        config = SimpleNamespace(budget=_budget_config())

        guards, name = resolve_budget_for_build(pool, config, build_id)

        assert name == "capped"
        assert guards.caps_enabled is True
        assert guards.max_review_cycles == 3
        # End to end: the resolved guards reach the Supervisor by identity.
        sup = build_supervisor(
            forward_context_builder=MagicMock(name="forward_context_builder"),
            async_task_starter=MagicMock(name="async_task_starter"),
            stage_log_recorder=MagicMock(name="stage_log_recorder"),
            state_channel=MagicMock(name="state_channel"),
            lifecycle_emitter=MagicMock(name="lifecycle_emitter"),
            async_subagent_middleware=MagicMock(tools=[]),
            budget_guards=guards,
            budget_profile_name=name,
            **_build_supervisor_kwargs(),
        )
        assert sup.budget_guards is guards
        assert sup.budget_profile_name == "capped"

    def test_null_profile_resolves_to_attended_caps_off(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # No ``--profile`` was selected → the row carries a NULL profile.
        build_id = _seed_running_build(pool, profile=None)
        config = SimpleNamespace(budget=_budget_config())

        guards, name = resolve_budget_for_build(pool, config, build_id)

        assert name == "attended"
        assert guards.caps_enabled is False
        # End to end: an attended (caps-off) profile reaches the Supervisor,
        # so the guard is a strict no-op (ASSUM-010).
        sup = build_supervisor(
            forward_context_builder=MagicMock(name="forward_context_builder"),
            async_task_starter=MagicMock(name="async_task_starter"),
            stage_log_recorder=MagicMock(name="stage_log_recorder"),
            state_channel=MagicMock(name="state_channel"),
            lifecycle_emitter=MagicMock(name="lifecycle_emitter"),
            async_subagent_middleware=MagicMock(tools=[]),
            budget_guards=guards,
            budget_profile_name=name,
            **_build_supervisor_kwargs(),
        )
        assert sup.budget_guards.caps_enabled is False

    def test_missing_row_is_treated_as_null_profile(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        config = SimpleNamespace(budget=_budget_config())

        guards, name = resolve_budget_for_build(pool, config, "build-does-not-exist")

        assert name == "attended"
        assert guards.caps_enabled is False


# ---------------------------------------------------------------------------
# (c) make_budget_pause — ADR-ARCH-021 order + loud degrade on race
# ---------------------------------------------------------------------------


def _breach_payload_verdict_metrics(
    *, build_id: str, feature_id: str, review_cycles: int = 3
) -> tuple[Any, BudgetVerdict, BuildBudgetMetrics]:
    verdict = BudgetVerdict(
        ok=False,
        breached_cap="max_review_cycles",
        detail=f"review cycles ({review_cycles}) reached cap (3)",
    )
    metrics = BuildBudgetMetrics(
        review_cycles=review_cycles,
        elapsed_wallclock_seconds=0.0,
        last_coach_score=None,
    )
    # The Supervisor stamps this deterministic request_id on the payload.
    payload = build_budget_breach_approval_payload(
        request_id=f"budget-{build_id}-{review_cycles}",
        build_id=build_id,
        feature_id=feature_id,
        profile_name="capped",
        verdict=verdict,
        metrics=metrics,
    )
    return payload, verdict, metrics


class TestMakeBudgetPause:
    """The pause collaborator publishes → marks PAUSED → emits, in ADR order."""

    @pytest.mark.asyncio
    async def test_adr_order_publish_then_mark_paused_then_emit(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_running_build(pool, profile="capped")
        feature_id = "FEAT-UBS-002"
        payload, verdict, metrics = _breach_payload_verdict_metrics(
            build_id=build_id, feature_id=feature_id
        )

        order: list[str] = []
        emit_kwargs: dict[str, Any] = {}

        async def publish_approval_request(pl: Any, subject: str) -> None:
            order.append("publish")
            assert subject == f"agents.approval.forge.{build_id}"
            assert pl is payload

        async def emit_paused(ctx: Any, **kwargs: Any) -> None:
            order.append("emit_paused")
            emit_kwargs.update(kwargs)
            emit_kwargs["ctx"] = ctx

        emitter = SimpleNamespace(emit_paused=emit_paused)

        # Spy on the real mark_paused so we capture ORDER while keeping the
        # genuine SQLite side effect (row → PAUSED).
        real_mark_paused = pool.mark_paused

        def spy_mark_paused(bid: str, rid: str) -> None:
            order.append("mark_paused")
            real_mark_paused(bid, rid)

        pool.mark_paused = spy_mark_paused  # type: ignore[method-assign]

        budget_pause = make_budget_pause(pool, publish_approval_request, emitter)
        await budget_pause(
            build_id=build_id,
            feature_id=feature_id,
            payload=payload,
            verdict=verdict,
            metrics=metrics,
        )

        # ADR-ARCH-021: publish BEFORE mark_paused BEFORE emit_paused.
        assert order == ["publish", "mark_paused", "emit_paused"]
        # The SQLite row is PAUSED with the deterministic request_id.
        row = pool.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.PAUSED
        assert row.pending_approval_request_id == f"budget-{build_id}-3"
        # emit_paused carries the honest budget-pause envelope.
        assert emit_kwargs["approval_subject"] == f"agents.approval.forge.{build_id}"
        assert emit_kwargs["gate_mode"] == "MANDATORY_HUMAN_APPROVAL"
        assert emit_kwargs["rationale"] == verdict.detail
        assert emit_kwargs["coach_score"] is None
        # correlation_id is threaded off the persisted row.
        assert emit_kwargs["ctx"].correlation_id == "corr-ubs-002"

    @pytest.mark.asyncio
    async def test_invalid_transition_degrades_loudly_without_emit(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # A QUEUED build cannot transition to PAUSED (QUEUED → PAUSED is not a
        # legal edge), so mark_paused raises InvalidTransitionError — the race
        # shape the closure must degrade on.
        build_id = pool.record_pending_build(_make_payload(), profile="capped")
        feature_id = "FEAT-UBS-002"
        payload, verdict, metrics = _breach_payload_verdict_metrics(
            build_id=build_id, feature_id=feature_id
        )

        order: list[str] = []

        async def publish_approval_request(pl: Any, subject: str) -> None:
            order.append("publish")

        emit_paused = AsyncMock(name="emit_paused")
        emitter = SimpleNamespace(emit_paused=emit_paused)

        # Sanity: the underlying pool genuinely rejects this transition.
        with pytest.raises(InvalidTransitionError):
            pool.mark_paused(build_id, "probe")

        budget_pause = make_budget_pause(pool, publish_approval_request, emitter)
        # The closure must NOT crash — it degrades loudly and returns.
        await budget_pause(
            build_id=build_id,
            feature_id=feature_id,
            payload=payload,
            verdict=verdict,
            metrics=metrics,
        )

        # The approval was published (idempotent by request_id), but no
        # build-paused was emitted for a row that is not PAUSED.
        assert order == ["publish"]
        emit_paused.assert_not_awaited()
        # The row is untouched by the failed pause.
        row = pool.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.QUEUED

    @pytest.mark.asyncio
    async def test_second_fire_on_paused_row_degrades_not_double_emit(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # Idempotency division of labour: the closure itself does NOT guard
        # against a re-fire (the Supervisor's already-PAUSED short-circuit
        # ensures a paused build never reaches here). If it IS called again on
        # an already-PAUSED row, mark_paused raises InvalidTransitionError
        # (PAUSED → PAUSED is illegal), so the closure degrades loudly: it does
        # not emit a second build-paused and does not crash. (It would re-run
        # the publish — which is why the short-circuit, not the closure, owns
        # no-double-publish.)
        build_id = _seed_running_build(pool, profile="capped")
        feature_id = "FEAT-UBS-002"
        payload, verdict, metrics = _breach_payload_verdict_metrics(
            build_id=build_id, feature_id=feature_id
        )

        publish = AsyncMock(name="publish_approval_request")
        emit_paused = AsyncMock(name="emit_paused")
        emitter = SimpleNamespace(emit_paused=emit_paused)
        budget_pause = make_budget_pause(pool, publish, emitter)

        # First fire pauses the build.
        await budget_pause(
            build_id=build_id,
            feature_id=feature_id,
            payload=payload,
            verdict=verdict,
            metrics=metrics,
        )
        assert pool.get_build_row(build_id).status is BuildState.PAUSED
        assert emit_paused.await_count == 1

        # Second fire on the now-PAUSED row: mark_paused raises → degrade.
        await budget_pause(
            build_id=build_id,
            feature_id=feature_id,
            payload=payload,
            verdict=verdict,
            metrics=metrics,
        )
        # No SECOND build-paused emission (the closure guards its own emit).
        assert emit_paused.await_count == 1
        # Row is still PAUSED, unchanged.
        assert pool.get_build_row(build_id).status is BuildState.PAUSED


# ---------------------------------------------------------------------------
# (d) The first wall-clock-cap coverage — breach pauses; fail-open otherwise
# ---------------------------------------------------------------------------


def _wallclock_supervisor(
    *,
    guards: BudgetGuards | None,
    wall_clock: Any,
    started_at_reader: Any,
    budget_pause: Any = None,
) -> Supervisor:
    return Supervisor(
        budget_guards=guards,
        budget_profile_name="capped",
        budget_wall_clock=wall_clock,
        budget_started_at_reader=started_at_reader,
        budget_pause=budget_pause,
        **_supervisor_collaborators(),
    )


class TestWallClockCap:
    """First-ever wall-clock-cap coverage: breach pauses; missing wiring is
    fail-open (0.0 elapsed → never a false pause)."""

    def test_elapsed_seconds_measured_when_both_wired(self) -> None:
        now = datetime(2026, 4, 27, 13, 0, 0, tzinfo=UTC)
        started = now - timedelta(seconds=3600)
        sup = _wallclock_supervisor(
            guards=BudgetGuards(max_build_wallclock_seconds=10),
            wall_clock=lambda: now,
            started_at_reader=lambda _bid: started,
        )
        assert sup._budget_elapsed_seconds("build-x") == pytest.approx(3600.0)

    def test_elapsed_seconds_fail_open_when_reader_missing(self) -> None:
        # Wall clock wired but no started_at reader → unmeasurable → 0.0.
        sup = _wallclock_supervisor(
            guards=BudgetGuards(max_build_wallclock_seconds=10),
            wall_clock=lambda: datetime.now(UTC),
            started_at_reader=None,
        )
        assert sup._budget_elapsed_seconds("build-x") == 0.0

    def test_elapsed_seconds_fail_open_when_started_at_none(self) -> None:
        # Both wired but the row has no started_at yet → 0.0 (never a breach).
        sup = _wallclock_supervisor(
            guards=BudgetGuards(max_build_wallclock_seconds=10),
            wall_clock=lambda: datetime.now(UTC),
            started_at_reader=lambda _bid: None,
        )
        assert sup._budget_elapsed_seconds("build-x") == 0.0

    @pytest.mark.asyncio
    async def test_wallclock_breach_pauses(self) -> None:
        now = datetime(2026, 4, 27, 13, 0, 0, tzinfo=UTC)
        started = now - timedelta(seconds=3600)  # 1h elapsed >> 10s cap
        budget_pause = AsyncMock(name="budget_pause")
        sup = _wallclock_supervisor(
            guards=BudgetGuards(max_build_wallclock_seconds=10),
            wall_clock=lambda: now,
            started_at_reader=lambda _bid: started,
            budget_pause=budget_pause,
        )

        report = await sup._enforce_mode_c_budget(
            build_id="build-x",
            build_state=BuildState.RUNNING,
            history=[],
            permitted=frozenset(),
        )

        assert report is not None
        assert report.outcome is TurnOutcome.PAUSED_BUDGET
        budget_pause.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wallclock_fail_open_never_breaches(self) -> None:
        # The cap is armed, but the started_at reader is absent → elapsed 0.0 →
        # the guard passes and the build proceeds (no pause). This is the
        # fail-open truth asserted directly.
        budget_pause = AsyncMock(name="budget_pause")
        sup = _wallclock_supervisor(
            guards=BudgetGuards(max_build_wallclock_seconds=10),
            wall_clock=lambda: datetime.now(UTC),
            started_at_reader=None,
            budget_pause=budget_pause,
        )

        report = await sup._enforce_mode_c_budget(
            build_id="build-x",
            build_state=BuildState.RUNNING,
            history=[],
            permitted=frozenset(),
        )

        assert report is None
        budget_pause.assert_not_awaited()


# ---------------------------------------------------------------------------
# make_budget_started_at_reader — the production wall-clock start reader
# ---------------------------------------------------------------------------


class TestMakeBudgetStartedAtReader:
    """The reader returns ``builds.started_at`` (or None), never crashing."""

    def test_reads_started_at_of_running_build(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_running_build(pool, profile="capped")
        reader = make_budget_started_at_reader(pool)
        started = reader(build_id)
        assert isinstance(started, datetime)

    def test_missing_row_returns_none(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        reader = make_budget_started_at_reader(pool)
        assert reader("build-does-not-exist") is None
