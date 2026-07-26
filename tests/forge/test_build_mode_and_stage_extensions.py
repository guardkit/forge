"""Tests for TASK-MBC8-001 (BuildMode + StageClass extensions).

Acceptance-criteria coverage map:

* AC-001 :class:`TestBuildModeEnum` — ``BuildMode`` exists with the
  three required ``mode-a`` / ``mode-b`` / ``mode-c`` members.
* AC-002 :class:`TestStageClassExtensions` — ``StageClass`` gains
  ``TASK_REVIEW`` / ``TASK_WORK`` at the end of the enum so the Mode A
  iteration prefix is preserved.
* AC-003 :class:`TestStagePrerequisitesTaskWork` — ``TASK_WORK ←
  TASK_REVIEW`` is the only new prerequisite row.
* AC-004 :class:`TestPerFixTaskStages` — ``PER_FIX_TASK_STAGES``
  contains exactly ``TASK_WORK``; ``PER_FEATURE_STAGES`` is unchanged.
* AC-005 :class:`TestBuildAndBuildRowMode` — ``Build`` and ``BuildRow``
  expose ``mode: BuildMode`` defaulting to MODE_A.
* AC-006 :class:`TestSqliteMigrationAddsModeColumn` — the v2 migration
  adds ``mode TEXT NOT NULL DEFAULT 'mode-a'`` to ``builds`` and
  backfills historical rows to ``mode-a``.
* AC-007 :class:`TestQueueBuildAcceptsMode` — ``queue_build`` accepts
  ``mode: BuildMode`` and writes it; ``BuildStatusView`` exposes
  ``mode``.

The tests exercise the persistence layer against a real in-memory
SQLite database with the migrations applied — no mocking of the
storage layer.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    Build,
    BuildRow,
    BuildStatusView,
    SqliteLifecyclePersistence,
)
from forge.pipeline.stage_taxonomy import (
    PER_FEATURE_STAGES,
    PER_FIX_TASK_STAGES,
    STAGE_PREREQUISITES,
    StageClass,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str = "FEAT-MBC8-001",
    correlation_id: str = "corr-mbc8-001",
    queued_at: datetime | None = None,
    triggered_by: str = "cli",
    repo: str = "guardkit/forge",
    branch: str = "main",
    feature_yaml_path: str = "features/example/feature.yaml",
) -> SimpleNamespace:
    if queued_at is None:
        queued_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    return SimpleNamespace(
        feature_id=feature_id,
        repo=repo,
        branch=branch,
        feature_yaml_path=feature_yaml_path,
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by=triggered_by,
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=queued_at,
        requested_at=queued_at,
    )


@pytest.fixture()
def writer_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db)


# ---------------------------------------------------------------------------
# AC-001: BuildMode enum
# ---------------------------------------------------------------------------


class TestBuildModeEnum:
    """``BuildMode`` exposes the three required modes."""

    def test_build_mode_has_three_members(self) -> None:
        assert {m.name for m in BuildMode} == {"MODE_A", "MODE_B", "MODE_C"}

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (BuildMode.MODE_A, "mode-a"),
            (BuildMode.MODE_B, "mode-b"),
            (BuildMode.MODE_C, "mode-c"),
        ],
    )
    def test_build_mode_string_values(
        self, member: BuildMode, value: str
    ) -> None:
        assert member.value == value
        assert member == value  # StrEnum equality with raw str

    def test_build_mode_round_trips_through_str(self) -> None:
        for member in BuildMode:
            assert BuildMode(str(member)) is member


# ---------------------------------------------------------------------------
# AC-002: StageClass gains TASK_REVIEW + TASK_WORK at the end
# ---------------------------------------------------------------------------


class TestStageClassExtensions:
    """``StageClass`` is extended with the two Mode C members."""

    def test_task_review_and_task_work_exist(self) -> None:
        assert StageClass.TASK_REVIEW.value == "task-review"
        assert StageClass.TASK_WORK.value == "task-work"

    def test_task_review_and_task_work_appended_after_leading_eight(self) -> None:
        names = [m.name for m in StageClass]
        # The Mode C extensions follow the leading eight (positions 9-10). WS2-B8
        # appended the two output-side stages (DEPLOY, LIVE_GATE) after them, so
        # they are no longer the final members — but they keep their positions
        # relative to the leading eight (the StageOrderingGuard invariant).
        assert names[8:10] == ["TASK_REVIEW", "TASK_WORK"]
        assert names[-2:] == ["DEPLOY", "LIVE_GATE"]

    def test_mode_a_iteration_prefix_unchanged(self) -> None:
        names = [m.name for m in StageClass]
        assert names[:8] == [
            "PRODUCT_OWNER",
            "ARCHITECT",
            "SYSTEM_ARCH",
            "SYSTEM_DESIGN",
            "FEATURE_SPEC",
            "FEATURE_PLAN",
            "AUTOBUILD",
            "PULL_REQUEST_REVIEW",
        ]


# ---------------------------------------------------------------------------
# AC-003: STAGE_PREREQUISITES adds TASK_WORK ← TASK_REVIEW
# ---------------------------------------------------------------------------


class TestStagePrerequisitesTaskWork:
    """A single new prerequisite row links TASK_WORK to TASK_REVIEW."""

    def test_task_work_has_task_review_prerequisite(self) -> None:
        assert STAGE_PREREQUISITES[StageClass.TASK_WORK] == [
            StageClass.TASK_REVIEW
        ]

    def test_task_review_has_no_prerequisite_entry(self) -> None:
        # TASK_REVIEW is the entry stage of the Mode C cycle and must
        # not appear as a prerequisite key (mirrors PRODUCT_OWNER).
        assert StageClass.TASK_REVIEW not in STAGE_PREREQUISITES

    def test_only_one_new_row_added(self) -> None:
        # The seven Mode A keys plus TASK_WORK gives eight keys; WS2-B8 adds
        # the two output-side rows (DEPLOY ← PULL_REQUEST_REVIEW,
        # LIVE_GATE ← DEPLOY) for ten total. The two new rows are filtered out
        # of the reasoning-loop permitted set (POST_REVIEW_STAGES), so Mode
        # A/B/C dispatch is unchanged despite the additional prerequisite rows.
        assert len(STAGE_PREREQUISITES) == 10
        assert STAGE_PREREQUISITES[StageClass.DEPLOY] == [
            StageClass.PULL_REQUEST_REVIEW
        ]
        assert STAGE_PREREQUISITES[StageClass.LIVE_GATE] == [StageClass.DEPLOY]


# ---------------------------------------------------------------------------
# AC-004: PER_FIX_TASK_STAGES + PER_FEATURE_STAGES unchanged
# ---------------------------------------------------------------------------


class TestPerFixTaskStages:
    """``PER_FIX_TASK_STAGES`` exposes the per-fix-task fan-out tag."""

    def test_per_fix_task_stages_is_frozenset(self) -> None:
        assert isinstance(PER_FIX_TASK_STAGES, frozenset)

    def test_per_fix_task_stages_contains_only_task_work(self) -> None:
        assert PER_FIX_TASK_STAGES == frozenset({StageClass.TASK_WORK})

    def test_task_review_is_not_per_fix_task(self) -> None:
        # TASK_REVIEW runs once per Mode C cycle, not per fix task.
        assert StageClass.TASK_REVIEW not in PER_FIX_TASK_STAGES

    def test_per_feature_stages_unchanged(self) -> None:
        assert PER_FEATURE_STAGES == frozenset(
            {
                StageClass.FEATURE_SPEC,
                StageClass.FEATURE_PLAN,
                StageClass.AUTOBUILD,
                StageClass.PULL_REQUEST_REVIEW,
            }
        )


# ---------------------------------------------------------------------------
# AC-005: Build / BuildRow expose mode (default MODE_A)
# ---------------------------------------------------------------------------


class TestBuildAndBuildRowMode:
    """``Build`` and ``BuildRow`` both expose ``mode: BuildMode``."""

    def test_build_default_mode_is_mode_a(self) -> None:
        from forge.lifecycle.state_machine import BuildState

        b = Build(build_id="build-x", status=BuildState.QUEUED)
        assert b.mode is BuildMode.MODE_A

    def test_build_accepts_explicit_mode(self) -> None:
        from forge.lifecycle.state_machine import BuildState

        b = Build(
            build_id="build-x",
            status=BuildState.QUEUED,
            mode=BuildMode.MODE_C,
        )
        assert b.mode is BuildMode.MODE_C

    def test_build_row_default_mode_is_mode_a(self) -> None:
        from forge.lifecycle.state_machine import BuildState

        row = BuildRow(
            build_id="build-x",
            feature_id="FEAT-X",
            repo="r",
            branch="b",
            feature_yaml_path="features/x/x.yaml",
            status=BuildState.QUEUED,
            triggered_by="cli",
            correlation_id="corr-x",
            queued_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        )
        assert row.mode is BuildMode.MODE_A

    def test_build_row_accepts_explicit_mode(self) -> None:
        from forge.lifecycle.state_machine import BuildState

        row = BuildRow(
            build_id="build-x",
            feature_id="FEAT-X",
            repo="r",
            branch="b",
            feature_yaml_path="features/x/x.yaml",
            status=BuildState.QUEUED,
            triggered_by="cli",
            correlation_id="corr-x",
            queued_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
            mode=BuildMode.MODE_B,
        )
        assert row.mode is BuildMode.MODE_B


# ---------------------------------------------------------------------------
# AC-006: SQLite migration adds the mode column with mode-a default
# ---------------------------------------------------------------------------


class TestSqliteMigrationAddsModeColumn:
    """v2 migration adds ``mode TEXT NOT NULL DEFAULT 'mode-a'``."""

    def test_builds_has_mode_column_after_migration(
        self, writer_db: sqlite3.Connection
    ) -> None:
        cols = {
            row[1]: row
            for row in writer_db.execute("PRAGMA table_info(builds);")
        }
        assert "mode" in cols, f"expected ``mode`` column; got {list(cols)}"

    def test_mode_column_has_mode_a_default(
        self, writer_db: sqlite3.Connection
    ) -> None:
        cols = {
            row[1]: row
            for row in writer_db.execute("PRAGMA table_info(builds);")
        }
        info = cols["mode"]
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
        assert info[2].upper() == "TEXT"
        assert info[3] == 1, "mode column must be NOT NULL"
        assert info[4] is not None and "mode-a" in str(info[4])

    def test_schema_version_two_recorded(
        self, writer_db: sqlite3.Connection
    ) -> None:
        rows = writer_db.execute(
            "SELECT version FROM schema_version ORDER BY version ASC;"
        ).fetchall()
        versions = [r[0] for r in rows]
        assert 1 in versions and 2 in versions

    def test_migration_is_idempotent(
        self, writer_db: sqlite3.Connection
    ) -> None:
        # Re-running apply_at_boot must be a no-op.
        starting_version = migrations.apply_at_boot(writer_db)
        again = migrations.apply_at_boot(writer_db)
        assert starting_version == again

    def test_legacy_v1_db_backfills_to_mode_a(self, tmp_path: Path) -> None:
        # Stand up a v1-only database (no schema_v2.sql), seed a row,
        # then run the full migration and assert the row backfilled.
        db_path = tmp_path / "legacy.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            v1_sql = (
                Path(__file__).resolve().parents[2]
                / "src"
                / "forge"
                / "lifecycle"
                / "schema.sql"
            ).read_text(encoding="utf-8")
            cx.executescript(v1_sql)
            cx.execute(
                """
                INSERT INTO builds (
                    build_id, feature_id, repo, branch, feature_yaml_path,
                    status, triggered_by, correlation_id, queued_at
                ) VALUES (?, ?, ?, ?, ?, 'QUEUED', 'cli', ?, ?)
                """,
                (
                    "build-legacy-001",
                    "FEAT-LEGACY",
                    "r",
                    "b",
                    "features/legacy/legacy.yaml",
                    "corr-legacy",
                    datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC).isoformat(),
                ),
            )
            cx.commit()

            migrations.apply_at_boot(cx)

            row = cx.execute(
                "SELECT mode FROM builds WHERE build_id = ?",
                ("build-legacy-001",),
            ).fetchone()
            assert row is not None
            assert row[0] == "mode-a"
        finally:
            cx.close()


# ---------------------------------------------------------------------------
# AC-007: queue_build accepts mode + BuildStatusView exposes mode
# ---------------------------------------------------------------------------


class TestQueueBuildAcceptsMode:
    """``queue_build(mode=...)`` writes the column and ``BuildStatusView``
    surfaces it."""

    def test_queue_build_defaults_to_mode_a(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload()
        build_id = persistence.queue_build(payload)
        row = persistence.connection.execute(
            "SELECT mode FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row[0] == "mode-a"

    def test_queue_build_writes_mode_b(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload(
            feature_id="FEAT-MBC8-002", correlation_id="corr-002"
        )
        build_id = persistence.queue_build(payload, mode=BuildMode.MODE_B)
        row = persistence.connection.execute(
            "SELECT mode FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row[0] == "mode-b"

    def test_queue_build_accepts_string_alias(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload(
            feature_id="FEAT-MBC8-003", correlation_id="corr-003"
        )
        build_id = persistence.queue_build(payload, mode="mode-c")
        row = persistence.connection.execute(
            "SELECT mode FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row[0] == "mode-c"

    def test_status_view_exposes_mode(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload(
            feature_id="FEAT-MBC8-004", correlation_id="corr-004"
        )
        build_id = persistence.queue_build(payload, mode=BuildMode.MODE_C)

        statuses = persistence.read_status(feature_id="FEAT-MBC8-004")
        match = next(s for s in statuses if s.build_id == build_id)
        assert isinstance(match, BuildStatusView)
        assert match.mode is BuildMode.MODE_C

    def test_history_round_trips_mode(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload(
            feature_id="FEAT-MBC8-005", correlation_id="corr-005"
        )
        build_id = persistence.queue_build(payload, mode=BuildMode.MODE_B)

        history = persistence.read_history(feature_id="FEAT-MBC8-005")
        assert len(history) == 1
        assert history[0].build_id == build_id
        assert history[0].mode is BuildMode.MODE_B

    def test_record_pending_build_reads_payload_mode(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # Backwards-compat: callers that don't pass the kwarg but
        # supply ``payload.mode`` should still get the mode persisted.
        payload = _make_payload(
            feature_id="FEAT-MBC8-006", correlation_id="corr-006"
        )
        payload.mode = BuildMode.MODE_C
        build_id = persistence.record_pending_build(payload)
        row = persistence.connection.execute(
            "SELECT mode FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row[0] == "mode-c"


# ---------------------------------------------------------------------------
# Coach-score store — record_last_coach_score / read_last_coach_score
# (schema_v6; the durable sink feeding the min_coach_score budget floor)
# ---------------------------------------------------------------------------


class TestLastCoachScoreStore:
    """The write/read pair over the additive ``builds.last_coach_score`` column."""

    def _queued(
        self, persistence: SqliteLifecyclePersistence, cid: str
    ) -> str:
        payload = _make_payload(feature_id=f"FEAT-{cid}", correlation_id=cid)
        return persistence.record_pending_build(payload)

    def test_read_defaults_to_none_for_fresh_row(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = self._queued(persistence, "corr-cs-1")
        assert persistence.read_last_coach_score(build_id) is None

    def test_read_none_for_unknown_build(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert persistence.read_last_coach_score("build-PHANTOM") is None

    def test_record_then_read_round_trips(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = self._queued(persistence, "corr-cs-2")
        persistence.record_last_coach_score(build_id, 0.8)
        assert persistence.read_last_coach_score(build_id) == pytest.approx(0.8)

    def test_record_is_last_wins(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = self._queued(persistence, "corr-cs-3")
        persistence.record_last_coach_score(build_id, 1.0)
        persistence.record_last_coach_score(build_id, 0.0)
        assert persistence.read_last_coach_score(build_id) == pytest.approx(0.0)

    def test_record_leaves_status_untouched(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = self._queued(persistence, "corr-cs-4")
        persistence.record_last_coach_score(build_id, 0.5)
        row = persistence.connection.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row[0] == "QUEUED"

    def test_record_missing_row_is_quiet_noop(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # No row for this build_id — the UPDATE matches zero rows and does
        # not raise (module norm for concurrent-race writes).
        persistence.record_last_coach_score("build-PHANTOM", 0.9)
        assert persistence.read_last_coach_score("build-PHANTOM") is None

    def test_read_is_usable_as_budget_reader_di(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # The bound method must satisfy the Supervisor's
        # ``budget_coach_score_reader: Callable[[str], float | None]`` DI.
        reader: "Callable[[str], float | None]" = persistence.read_last_coach_score
        build_id = self._queued(persistence, "corr-cs-5")
        persistence.record_last_coach_score(build_id, 0.42)
        assert reader(build_id) == pytest.approx(0.42)

    def test_record_empty_build_id_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError):
            persistence.record_last_coach_score("", 1.0)

    def test_read_empty_build_id_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError):
            persistence.read_last_coach_score("")
