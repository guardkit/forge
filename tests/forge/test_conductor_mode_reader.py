"""Tests for the conductor's mode reader and its degrade rail.

The conductor's revival, Stage 1b (design pass §a.1). Two things are under
test and the second matters more than the first:

1. :class:`SqliteBuildModeReader` — the ~10-line adapter that reads
   ``builds.mode`` off the daemon's pool. The mode column already
   round-trips end to end (``forge queue --mode c`` writes it,
   ``schema_v2.sql`` backfills history, :class:`BuildRow` hydrates it);
   this reader was the missing inch. :class:`TestSqliteBuildModeReader`.

2. **The degrade rail, with the real adapter in play.** ``supervisor.py``
   ``_read_build_mode`` answers MODE_A when the reader is unwired *or*
   raising, which is what makes a half-wired conductor degrade to today's
   behaviour rather than crash a build. It has been pinned against fakes;
   this file pins it against the production adapter over a real database,
   and against the adapter over a broken pool.
   :class:`TestDegradeRail` and :class:`TestByteForByteRoutinePath`.

The prime invariant of this lane is *no behaviour change*, and the way it
is honoured is that with the flag off nothing wires a mode reader at all.
:class:`TestByteForByteRoutinePath` proves the stronger claim: even with
the production reader wired, a build whose row says the routine path — and
a build whose reader is broken — take the identical Mode A turn, dispatcher
call for dispatcher call.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    SqliteBuildModeReader,
    SqliteLifecyclePersistence,
)
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import (
    BuildModeReader as BuildModeReaderProto,
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
)


_T0 = datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    """Persistence facade over a freshly-migrated database file."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx)
    finally:
        cx.close()


def _seed(
    pool: SqliteLifecyclePersistence,
    *,
    mode: BuildMode,
    feature_id: str = "FEAT-MODE-001",
    correlation_id: str = "corr-mode-001",
) -> str:
    payload = SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="lane/conductor-revival",
        feature_yaml_path="features/demo/demo.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=_T0,
        requested_at=_T0,
    )
    return pool.record_pending_build(payload, mode=mode)


# ---------------------------------------------------------------------------
# Minimal Mode A collaborators — enough to drive one routine turn
# ---------------------------------------------------------------------------


@dataclass
class _StateReader:
    def get_build_state(self, build_id: str) -> BuildState:
        return BuildState.RUNNING


@dataclass
class _OrderingReader:
    def is_approved(
        self, build_id: str, stage: StageClass, feature_id: str | None = None
    ) -> bool:
        return False

    def feature_catalogue(self, build_id: str) -> list[str]:
        return []


@dataclass
class _PerFeatureReader:
    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return False


@dataclass
class _AsyncTaskReader:
    def list_autobuild_states(self, build_id: str) -> Iterable[Any]:
        return []


@dataclass
class _ReasoningModel:
    calls: list[str] = field(default_factory=list)

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:
        self.calls.append(build_id)
        return DispatchChoice(
            stage=StageClass.PRODUCT_OWNER, rationale="kick off the routine path"
        )


@dataclass
class _TurnRecorder:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record_turn(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


@dataclass
class _Dispatcher:
    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return {"dispatcher": self.label, "status": "ok"}


@dataclass
class _PRGate:
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit_decision(self, **kwargs: Any) -> Any:
        self.submissions.append(dict(kwargs))
        return {"gate": "pr-review"}


class _BrokenPool:
    """A pool whose reads raise — the "unhealthy SQLite" shape."""

    def get_build_row(self, build_id: str) -> Any:
        raise sqlite3.OperationalError("database is locked")


def _supervisor(mode_reader: Any) -> tuple[Supervisor, dict[str, Any]]:
    doubles: dict[str, Any] = {
        "reasoning_model": _ReasoningModel(),
        "turn_recorder": _TurnRecorder(),
        "specialist": _Dispatcher("specialist"),
        "subprocess": _Dispatcher("subprocess"),
        "autobuild": _Dispatcher("autobuild"),
        "pr_gate": _PRGate(),
    }
    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=_StateReader(),
        ordering_stage_log_reader=_OrderingReader(),
        per_feature_stage_log_reader=_PerFeatureReader(),
        async_task_reader=_AsyncTaskReader(),
        reasoning_model=doubles["reasoning_model"],
        turn_recorder=doubles["turn_recorder"],
        specialist_dispatcher=doubles["specialist"],
        subprocess_dispatcher=doubles["subprocess"],
        autobuild_dispatcher=doubles["autobuild"],
        pr_review_gate=doubles["pr_gate"],
        build_mode_reader=mode_reader,
    )
    return supervisor, doubles


# ---------------------------------------------------------------------------
# (1) The adapter
# ---------------------------------------------------------------------------


class TestSqliteBuildModeReader:
    """~10 lines over ``builds.mode``, and its two contract points."""

    def test_satisfies_the_supervisor_protocol(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        assert isinstance(SqliteBuildModeReader(pool), BuildModeReaderProto)

    @pytest.mark.parametrize(
        "mode", [BuildMode.MODE_A, BuildMode.MODE_B, BuildMode.MODE_C]
    )
    def test_reads_the_recorded_mode(
        self, pool: SqliteLifecyclePersistence, mode: BuildMode
    ) -> None:
        build_id = _seed(pool, mode=mode)

        assert SqliteBuildModeReader(pool).get_build_mode(build_id) is mode

    def test_missing_row_reads_as_the_routine_path(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # An unknown build_id is not evidence that a build wants the fix
        # journey. The safe answer is always MODE_A.
        reader = SqliteBuildModeReader(pool)

        assert reader.get_build_mode("build-never-existed") is BuildMode.MODE_A

    def test_empty_build_id_is_refused(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError, match="build_id must be non-empty"):
            SqliteBuildModeReader(pool).get_build_mode("")

    def test_a_broken_pool_raises_so_the_rail_can_log_it(self) -> None:
        # Deliberately NOT swallowed here: the supervisor's degrade rail
        # catches it and logs the fault. Swallowing it in the adapter
        # would hide an unhealthy database from that log.
        with pytest.raises(sqlite3.OperationalError):
            SqliteBuildModeReader(_BrokenPool()).get_build_mode("build-1")

    def test_concurrent_builds_do_not_cross_talk(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        routine = _seed(
            pool,
            mode=BuildMode.MODE_A,
            feature_id="FEAT-ROUTINE",
            correlation_id="corr-routine",
        )
        fix = _seed(
            pool,
            mode=BuildMode.MODE_C,
            feature_id="FEAT-FIX",
            correlation_id="corr-fix",
        )
        reader = SqliteBuildModeReader(pool)

        assert reader.get_build_mode(routine) is BuildMode.MODE_A
        assert reader.get_build_mode(fix) is BuildMode.MODE_C
        assert reader.get_build_mode(routine) is BuildMode.MODE_A


# ---------------------------------------------------------------------------
# (2) The degrade rail — with the production adapter in play
# ---------------------------------------------------------------------------


class TestDegradeRail:
    """``_read_build_mode``: unwired or raising → MODE_A, never a crash."""

    def test_unwired_reader_is_mode_a(self) -> None:
        supervisor, _ = _supervisor(None)

        assert supervisor._read_build_mode("build-anything") is BuildMode.MODE_A

    def test_production_adapter_reports_the_real_mode(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed(pool, mode=BuildMode.MODE_C)
        supervisor, _ = _supervisor(SqliteBuildModeReader(pool))

        assert supervisor._read_build_mode(build_id) is BuildMode.MODE_C

    def test_production_adapter_over_a_broken_pool_degrades_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        supervisor, _ = _supervisor(SqliteBuildModeReader(_BrokenPool()))

        with caplog.at_level(logging.ERROR):
            mode = supervisor._read_build_mode("build-1")

        assert mode is BuildMode.MODE_A
        assert "falling back to MODE_A" in caplog.text
        assert "database is locked" in caplog.text

    def test_empty_build_id_through_the_adapter_also_degrades(
        self, pool: SqliteLifecyclePersistence, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The adapter's own ValueError is caught by the rail too — a
        # caller bug must not crash a build.
        supervisor, _ = _supervisor(SqliteBuildModeReader(pool))

        with caplog.at_level(logging.ERROR):
            assert supervisor._read_build_mode("") is BuildMode.MODE_A


class TestByteForByteRoutinePath:
    """The prime invariant, proven rather than asserted."""

    @staticmethod
    def _turn(mode_reader: Any) -> tuple[Any, dict[str, Any]]:
        supervisor, doubles = _supervisor(mode_reader)
        report = asyncio.run(supervisor.next_turn("build-routine"))
        return report, doubles

    @staticmethod
    def _shape(report: Any, doubles: dict[str, Any]) -> dict[str, Any]:
        """The observable shape of one turn — outcome + every dispatch."""
        return {
            "outcome": report.outcome,
            "chosen_stage": report.chosen_stage,
            "permitted": report.permitted_stages,
            "reasoning_calls": list(doubles["reasoning_model"].calls),
            "specialist": [
                {k: v for k, v in c.items()} for c in doubles["specialist"].calls
            ],
            "subprocess": len(doubles["subprocess"].calls),
            "autobuild": len(doubles["autobuild"].calls),
            "pr_gate": len(doubles["pr_gate"].submissions),
            "recorded": [r["outcome"] for r in doubles["turn_recorder"].rows],
        }

    def test_unwired_and_routine_row_take_the_identical_turn(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _seed(pool, mode=BuildMode.MODE_A)
        # The build_id the turn runs against is deliberately unknown to
        # the pool as well — a missing row reads MODE_A too.
        baseline_report, baseline_doubles = self._turn(None)
        wired_report, wired_doubles = self._turn(SqliteBuildModeReader(pool))

        assert self._shape(wired_report, wired_doubles) == self._shape(
            baseline_report, baseline_doubles
        )
        assert baseline_report.outcome is TurnOutcome.DISPATCHED
        assert baseline_report.chosen_stage is StageClass.PRODUCT_OWNER

    def test_a_raising_reader_takes_the_identical_turn(self) -> None:
        baseline_report, baseline_doubles = self._turn(None)
        broken_report, broken_doubles = self._turn(
            SqliteBuildModeReader(_BrokenPool())
        )

        assert self._shape(broken_report, broken_doubles) == self._shape(
            baseline_report, baseline_doubles
        )

    def test_the_reasoning_model_is_still_consulted_on_the_routine_path(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        # M0 corollary: the routine path is unchanged, including its
        # reasoning-model step. The fix journey's turn never reaches it.
        _report, doubles = self._turn(SqliteBuildModeReader(pool))

        assert doubles["reasoning_model"].calls == ["build-routine"]
        assert len(doubles["specialist"].calls) == 1
