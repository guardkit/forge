"""Tests for ``forge.lifecycle.persistence`` (TASK-PSM-005).

Acceptance-criteria coverage map:

* AC-001: All seven Sqlite* classes pass ``isinstance`` checks against
  the runtime_checkable Protocols defined in ``pipeline/cli_steering.py``
  — :class:`TestProtocolImplementations`.
* AC-002: ``apply_transition`` is the ONLY public method that issues
  ``UPDATE builds SET status = ?`` (static-analysis check) —
  :class:`TestSingleStatusWriter`.
* AC-003: ``apply_transition`` rejects raw kwargs / non-Transition
  arguments — :class:`TestApplyTransitionSignature`.
* AC-004: ``mark_paused`` writes ``pending_approval_request_id``
  atomically with the state transition — :class:`TestMarkPausedAtomic`.
* AC-005: ``record_pending_build`` translates ``IntegrityError`` on the
  unique index into :class:`DuplicateBuildError` —
  :class:`TestRecordPendingBuildDuplicate`.
* AC-006: ``read_status`` returns active + last 5 terminal builds, sorted
  ``queued_at DESC`` — :class:`TestReadStatus`.
* AC-007: ``read_history`` clamps ``limit`` — :class:`TestReadHistory`.
* AC-008: ``exists_active_build`` returns True iff in active set —
  :class:`TestExistsActiveBuild`.
* AC-009: All write paths use ``BEGIN IMMEDIATE`` —
  :class:`TestBeginImmediate`.
* AC-010: All read paths use ``read_only_connect()`` —
  :class:`TestReadPathsUseReadOnlyConnect`.
* AC-011: Unit tests cover each Protocol method against a real in-memory
  SQLite database — every test class above uses ``apply_at_boot`` on a
  fresh in-memory db.

The Protocol implementations exercise their behaviours against an
in-memory SQLite database — no mocking of the storage layer.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.persistence import (
    ACTIVE_STATES,
    MAX_HISTORY_LIMIT,
    AsyncTaskCanceller,
    AsyncTaskUpdater,
    Build,
    BuildRow,
    BuildStatusView,
    DuplicateBuildError,
    SqliteBuildCanceller,
    SqliteBuildResumer,
    SqliteBuildSnapshotReader,
    SqliteLifecyclePersistence,
    SqlitePauseRejectResolver,
    SqliteStageLogReader,
    SqliteStageSkipRecorder,
    StageLogEntry,
)
from forge.lifecycle.state_machine import (
    BuildState,
    InvalidTransitionError,
    transition as compose_transition,
)
from forge.pipeline.cli_steering import (
    AsyncTaskCanceller as AsyncTaskCancellerProto,
    AsyncTaskUpdater as AsyncTaskUpdaterProto,
    BuildCanceller as BuildCancellerProto,
    BuildLifecycle,
    BuildResumer as BuildResumerProto,
    BuildSnapshotReader as BuildSnapshotReaderProto,
    PauseRejectResolver as PauseRejectResolverProto,
    StageSkipRecorder as StageSkipRecorderProto,
)
from forge.pipeline.stage_taxonomy import StageClass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str = "FEAT-TEST-001",
    correlation_id: str = "corr-001",
    queued_at: datetime | None = None,
    triggered_by: str = "cli",
    repo: str = "guardkit/forge",
    branch: str = "main",
    feature_yaml_path: str = "features/test/test.yaml",
) -> SimpleNamespace:
    """Construct a duck-typed BuildQueuedPayload for record_pending_build."""
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
    """Return a writer connection against a freshly-migrated db file."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    """Return a persistence facade bound to the migrated writer connection."""
    return SqliteLifecyclePersistence(connection=writer_db)


@pytest.fixture()
def seeded_build(
    persistence: SqliteLifecyclePersistence,
) -> tuple[str, SimpleNamespace]:
    """Seed one QUEUED build and return (build_id, payload)."""
    payload = _make_payload()
    build_id = persistence.record_pending_build(payload)
    return build_id, payload


# ---------------------------------------------------------------------------
# AC-001: Protocol implementations
# ---------------------------------------------------------------------------


class TestProtocolImplementations:
    """Each Sqlite* class must satisfy its runtime_checkable Protocol."""

    def test_snapshot_reader_satisfies_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        reader = SqliteBuildSnapshotReader(persistence)
        assert isinstance(reader, BuildSnapshotReaderProto)

    def test_build_canceller_satisfies_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        canceller = SqliteBuildCanceller(persistence)
        assert isinstance(canceller, BuildCancellerProto)

    def test_build_resumer_satisfies_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        resumer = SqliteBuildResumer(persistence)
        assert isinstance(resumer, BuildResumerProto)

    def test_stage_skip_recorder_satisfies_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        recorder = SqliteStageSkipRecorder(persistence)
        assert isinstance(recorder, StageSkipRecorderProto)

    def test_pause_reject_resolver_satisfies_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        resolver = SqlitePauseRejectResolver(persistence, lambda payload: None)
        assert isinstance(resolver, PauseRejectResolverProto)

    def test_async_task_canceller_satisfies_protocol(self) -> None:
        canceller = AsyncTaskCanceller(lambda task_id: None)
        assert isinstance(canceller, AsyncTaskCancellerProto)

    def test_async_task_updater_satisfies_protocol(self) -> None:
        updater = AsyncTaskUpdater(
            lambda task_id, *, append_pending_directive: None
        )
        assert isinstance(updater, AsyncTaskUpdaterProto)


# ---------------------------------------------------------------------------
# AC-002: single status writer
# ---------------------------------------------------------------------------


class TestSingleStatusWriter:
    """sc_001: only ``persistence.apply_transition`` writes status SQL."""

    def test_single_status_writer_in_src(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        src_root = repo_root / "src"
        pattern = re.compile(r"UPDATE\s+builds\s+SET\s+status", re.IGNORECASE)
        hits: list[Path] = []
        for path in src_root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(path)
        assert len(hits) == 1, (
            f"expected exactly one ``UPDATE builds SET status`` writer, "
            f"found {len(hits)}: {hits}"
        )
        assert "lifecycle/persistence.py" in hits[0].as_posix(), (
            f"the sole status-writer must live in lifecycle/persistence.py, "
            f"not {hits[0]}"
        )


# ---------------------------------------------------------------------------
# AC-003: apply_transition signature rejects raw kwargs
# ---------------------------------------------------------------------------


class TestApplyTransitionSignature:
    """``apply_transition`` accepts only :class:`Transition` value objects."""

    def test_signature_has_only_transition_param(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        import inspect

        sig = inspect.signature(persistence.apply_transition)
        # Only one declared parameter — and it's named "transition".
        params = list(sig.parameters)
        assert params == ["transition"], (
            f"apply_transition signature must be (transition); got {params}"
        )

    def test_apply_transition_rejects_non_transition(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(TypeError):
            persistence.apply_transition({"status": "RUNNING"})  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            persistence.apply_transition("RUNNING")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            persistence.apply_transition(None)  # type: ignore[arg-type]

    def test_apply_transition_advances_state_for_existing_build(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        t = compose_transition(
            Build(build_id=build_id, status=BuildState.QUEUED),
            BuildState.PREPARING,
        )
        persistence.apply_transition(t)
        row = persistence.connection.execute(
            "SELECT status, started_at FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "PREPARING"
        assert row["started_at"] is not None


# ---------------------------------------------------------------------------
# AC-004: mark_paused writes pending_approval_request_id atomically
# ---------------------------------------------------------------------------


class TestMarkPausedAtomic:
    """mark_paused writes status + pending_approval_request_id in one UPDATE."""

    def test_mark_paused_persists_request_id(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Drive the build into RUNNING (paused only valid from RUNNING).
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PREPARING),
                BuildState.RUNNING,
            )
        )

        persistence.mark_paused(build_id, "req-abc-123")

        row = persistence.connection.execute(
            "SELECT status, pending_approval_request_id FROM builds "
            "WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "PAUSED"
        assert row["pending_approval_request_id"] == "req-abc-123"

    def test_mark_paused_rejects_empty_args(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError):
            persistence.mark_paused("", "req-1")
        with pytest.raises(ValueError):
            persistence.mark_paused("build-x", "")

    def test_mark_paused_unknown_build_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(RuntimeError):
            persistence.mark_paused("build-does-not-exist", "req-1")

    def test_mark_paused_invalid_transition_raises(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        # QUEUED -> PAUSED is not in the table.
        build_id, _ = seeded_build
        with pytest.raises(InvalidTransitionError):
            persistence.mark_paused(build_id, "req-1")


# ---------------------------------------------------------------------------
# AC-005: record_pending_build → DuplicateBuildError
# ---------------------------------------------------------------------------


class TestRecordPendingBuildDuplicate:
    """Duplicate (feature_id, correlation_id) → DuplicateBuildError."""

    def test_first_record_succeeds(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload()
        build_id = persistence.record_pending_build(payload)
        assert build_id == derive_build_id(
            payload.feature_id, payload.queued_at
        )
        row = persistence.connection.execute(
            "SELECT status FROM builds WHERE build_id = ?", (build_id,)
        ).fetchone()
        assert row["status"] == "QUEUED"

    def test_duplicate_raises_domain_error(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        first = _make_payload(
            feature_id="FEAT-DUP-001",
            correlation_id="corr-dup-1",
            queued_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        )
        second = _make_payload(
            feature_id="FEAT-DUP-001",
            correlation_id="corr-dup-1",
            queued_at=datetime(2026, 4, 27, 13, 0, 0, tzinfo=UTC),
        )
        persistence.record_pending_build(first)

        with pytest.raises(DuplicateBuildError) as exc_info:
            persistence.record_pending_build(second)
        assert exc_info.value.feature_id == "FEAT-DUP-001"
        assert exc_info.value.correlation_id == "corr-dup-1"

    def test_duplicate_does_not_leave_partial_row(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        first = _make_payload(correlation_id="corr-x")
        second = _make_payload(correlation_id="corr-x")
        persistence.record_pending_build(first)
        with pytest.raises(DuplicateBuildError):
            persistence.record_pending_build(second)
        # Only one row exists for this (feature, corr).
        count = persistence.connection.execute(
            "SELECT COUNT(*) FROM builds WHERE feature_id = ? "
            "AND correlation_id = ?",
            (first.feature_id, "corr-x"),
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# AC-006: read_status — active + last 5 terminal, queued_at DESC
# ---------------------------------------------------------------------------


class TestReadStatus:
    """``read_status`` combines active + last 5 terminal builds."""

    def test_empty_db_returns_empty(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert persistence.read_status() == []

    def test_active_builds_listed_newest_first(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        ids: list[str] = []
        for i in range(3):
            payload = _make_payload(
                feature_id=f"FEAT-A-{i:03d}",
                correlation_id=f"corr-a-{i}",
                queued_at=base + timedelta(minutes=i),
            )
            ids.append(persistence.record_pending_build(payload))

        statuses = persistence.read_status()
        assert [s.build_id for s in statuses] == list(reversed(ids))
        assert all(isinstance(s, BuildStatusView) for s in statuses)
        for s in statuses:
            assert s.status in ACTIVE_STATES

    def test_terminal_builds_capped_at_five(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # Seed seven terminal builds.
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        for i in range(7):
            payload = _make_payload(
                feature_id=f"FEAT-T-{i:03d}",
                correlation_id=f"corr-t-{i}",
                queued_at=base + timedelta(minutes=i),
            )
            build_id = persistence.record_pending_build(payload)
            # QUEUED -> CANCELLED is in the table.
            persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=BuildState.QUEUED),
                    BuildState.CANCELLED,
                )
            )

        statuses = persistence.read_status()
        # Five terminal builds, no active.
        assert len(statuses) == 5
        # Newest five — feature ids 002..006 in reverse.
        feature_ids = [s.feature_id for s in statuses]
        assert feature_ids == [
            "FEAT-T-006",
            "FEAT-T-005",
            "FEAT-T-004",
            "FEAT-T-003",
            "FEAT-T-002",
        ]

    def test_active_and_terminal_combined_sorted_desc(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        # Three active, two terminal — active have older timestamps so
        # we can verify the global DESC sort interleaves correctly.
        active_payload = _make_payload(
            feature_id="FEAT-AC-001",
            correlation_id="corr-ac-1",
            queued_at=base + timedelta(minutes=1),
        )
        terminal_payload = _make_payload(
            feature_id="FEAT-TR-001",
            correlation_id="corr-tr-1",
            queued_at=base + timedelta(minutes=10),
        )
        active_build = persistence.record_pending_build(active_payload)
        terminal_build = persistence.record_pending_build(terminal_payload)
        persistence.apply_transition(
            compose_transition(
                Build(build_id=terminal_build, status=BuildState.QUEUED),
                BuildState.CANCELLED,
            )
        )

        statuses = persistence.read_status()
        assert [s.build_id for s in statuses] == [
            terminal_build,
            active_build,
        ]

    def test_feature_filter_narrows_results(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        a = _make_payload(feature_id="FEAT-X-1", correlation_id="cx1")
        b = _make_payload(feature_id="FEAT-X-2", correlation_id="cx2")
        persistence.record_pending_build(a)
        persistence.record_pending_build(b)
        statuses = persistence.read_status(feature_id="FEAT-X-1")
        assert len(statuses) == 1
        assert statuses[0].feature_id == "FEAT-X-1"


# ---------------------------------------------------------------------------
# AC-007: read_history clamps limit
# ---------------------------------------------------------------------------


class TestReadHistory:
    """``read_history`` clamps limit to MAX_HISTORY_LIMIT."""

    def test_default_limit_is_50(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        base = datetime(2026, 4, 27, tzinfo=UTC)
        # Create 60 builds; default limit returns 50.
        for i in range(60):
            payload = _make_payload(
                feature_id=f"FEAT-H-{i:03d}",
                correlation_id=f"corr-h-{i}",
                queued_at=base + timedelta(minutes=i),
            )
            persistence.record_pending_build(payload)
        history = persistence.read_history()
        assert len(history) == 50
        assert all(isinstance(r, BuildRow) for r in history)
        # Newest first.
        timestamps = [r.queued_at for r in history]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_limit_clamped_to_max(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # The MAX clamp is enforced regardless of how few rows exist.
        history = persistence.read_history(limit=10_000)
        # No rows seeded, so empty; the important check is it doesn't blow up.
        assert isinstance(history, list)
        # And the clamp is reflected on the limit constant.
        assert MAX_HISTORY_LIMIT == 1000

    def test_limit_zero_returns_empty(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        assert persistence.read_history(limit=0) == []
        assert persistence.read_history(limit=-5) == []

    def test_limit_must_be_int(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(TypeError):
            persistence.read_history(limit="50")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-008: exists_active_build
# ---------------------------------------------------------------------------


class TestExistsActiveBuild:
    """``exists_active_build`` is true iff a build is in an active state."""

    def test_no_builds_returns_false(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert persistence.exists_active_build("FEAT-NOPE-001") is False

    def test_queued_build_is_active(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        _, payload = seeded_build
        assert persistence.exists_active_build(payload.feature_id) is True

    @pytest.mark.parametrize(
        "terminal_state",
        [
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.CANCELLED,
        ],
    )
    def test_terminal_builds_are_not_active(
        self,
        persistence: SqliteLifecyclePersistence,
        terminal_state: BuildState,
    ) -> None:
        payload = _make_payload(
            feature_id=f"FEAT-TERM-{terminal_state.value}",
            correlation_id=f"corr-term-{terminal_state.value}",
        )
        build_id = persistence.record_pending_build(payload)
        # Drive into a terminal state via the legal transition path.
        if terminal_state is BuildState.CANCELLED:
            persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=BuildState.QUEUED),
                    BuildState.CANCELLED,
                )
            )
        else:
            # QUEUED -> PREPARING -> RUNNING -> FINALISING -> terminal.
            persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=BuildState.QUEUED),
                    BuildState.PREPARING,
                )
            )
            if terminal_state is BuildState.FAILED:
                persistence.apply_transition(
                    compose_transition(
                        Build(build_id=build_id, status=BuildState.PREPARING),
                        BuildState.FAILED,
                        error="test failure",
                    )
                )
            else:  # COMPLETE
                persistence.apply_transition(
                    compose_transition(
                        Build(build_id=build_id, status=BuildState.PREPARING),
                        BuildState.RUNNING,
                    )
                )
                persistence.apply_transition(
                    compose_transition(
                        Build(build_id=build_id, status=BuildState.RUNNING),
                        BuildState.FINALISING,
                    )
                )
                persistence.apply_transition(
                    compose_transition(
                        Build(build_id=build_id, status=BuildState.FINALISING),
                        BuildState.COMPLETE,
                    )
                )

        assert persistence.exists_active_build(payload.feature_id) is False

    def test_paused_build_is_active(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, payload = seeded_build
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PREPARING),
                BuildState.RUNNING,
            )
        )
        persistence.mark_paused(build_id, "req-paused-1")
        assert persistence.exists_active_build(payload.feature_id) is True


# ---------------------------------------------------------------------------
# AC-009: BEGIN IMMEDIATE on writes
# ---------------------------------------------------------------------------


class TestBeginImmediate:
    """All write paths use ``BEGIN IMMEDIATE``."""

    def test_persistence_module_uses_begin_immediate(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        text = (
            repo_root / "src" / "forge" / "lifecycle" / "persistence.py"
        ).read_text(encoding="utf-8")
        # The module should reference BEGIN IMMEDIATE for its writes.
        assert "BEGIN IMMEDIATE" in text, (
            "lifecycle/persistence.py must use BEGIN IMMEDIATE for "
            "write transactions (review finding F7)"
        )


# ---------------------------------------------------------------------------
# AC-010: read paths use read_only_connect()
# ---------------------------------------------------------------------------


class TestReadPathsUseReadOnlyConnect:
    """All read paths use ``read_only_connect()`` from the connect module."""

    def test_persistence_module_imports_read_only_connect(self) -> None:
        from forge.lifecycle import persistence

        # The module must import the canonical ro helper.
        assert hasattr(persistence, "read_only_connect")

    def test_reads_against_real_file_open_a_ro_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.lifecycle import persistence as persistence_module

        # Point at a real file db so the ro path is exercised.
        db_path = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db_path)
        migrations.apply_at_boot(cx)
        try:
            facade = SqliteLifecyclePersistence(connection=cx)

            calls: list[Path] = []
            real_ro = persistence_module.read_only_connect

            def spy(path: Path) -> sqlite3.Connection:
                calls.append(path)
                return real_ro(path)

            monkeypatch.setattr(
                persistence_module, "read_only_connect", spy
            )

            # Any read path counts.
            facade.read_status()
            facade.read_history(limit=5)
            facade.read_stages("build-does-not-exist")
            facade.exists_active_build("FEAT-NOPE-001")
            facade.find_active_or_recent("FEAT-NOPE-001")

            assert calls, (
                "expected read paths to invoke read_only_connect; "
                "no calls observed"
            )
            # All ro connections must target the same db file.
            assert all(p == db_path for p in calls)
        finally:
            cx.close()


# ---------------------------------------------------------------------------
# Protocol method behaviours
# ---------------------------------------------------------------------------


class TestSqliteBuildSnapshotReader:
    """Behavioural tests for the snapshot reader."""

    def test_unknown_build_yields_terminal(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        reader = SqliteBuildSnapshotReader(persistence)
        snapshot = reader.get_snapshot("build-unknown")
        assert snapshot.lifecycle is BuildLifecycle.TERMINAL

    def test_queued_yields_other_running(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        reader = SqliteBuildSnapshotReader(persistence)
        snapshot = reader.get_snapshot(build_id)
        assert snapshot.lifecycle is BuildLifecycle.OTHER_RUNNING

    def test_paused_yields_paused_at_gate_with_stage(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Drive to PAUSED.
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PREPARING),
                BuildState.RUNNING,
            )
        )
        # Append a GATED stage_log row so the reader can find the stage.
        now = datetime.now(UTC)
        persistence.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=StageClass.PULL_REQUEST_REVIEW.value,
                target_kind="local_tool",
                target_identifier="pr-review-gate",
                status="GATED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={},
            )
        )
        persistence.mark_paused(build_id, "req-pause-1")

        reader = SqliteBuildSnapshotReader(persistence)
        snapshot = reader.get_snapshot(build_id)
        assert snapshot.lifecycle is BuildLifecycle.PAUSED_AT_GATE
        assert snapshot.paused_stage is StageClass.PULL_REQUEST_REVIEW

    def test_terminal_status_yields_terminal(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.CANCELLED,
            )
        )
        reader = SqliteBuildSnapshotReader(persistence)
        snapshot = reader.get_snapshot(build_id)
        assert snapshot.lifecycle is BuildLifecycle.TERMINAL

    def test_empty_build_id_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        reader = SqliteBuildSnapshotReader(persistence)
        with pytest.raises(ValueError):
            reader.get_snapshot("")


class TestSqliteBuildCanceller:
    """``mark_cancelled`` routes through ``apply_transition``."""

    def test_cancel_marks_build_terminal(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        canceller = SqliteBuildCanceller(persistence)
        canceller.mark_cancelled(build_id, "operator-requested")
        row = persistence.connection.execute(
            "SELECT status, error, completed_at FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "CANCELLED"
        assert row["error"] == "operator-requested"
        assert row["completed_at"] is not None

    def test_cancel_terminal_build_is_noop(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Drive to terminal.
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.CANCELLED,
            )
        )
        canceller = SqliteBuildCanceller(persistence)
        # Second cancel must be a no-op (no exception, no resurrection).
        canceller.mark_cancelled(build_id, "redundant-cancel")
        row = persistence.connection.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "CANCELLED"

    def test_cancel_unknown_build_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        canceller = SqliteBuildCanceller(persistence)
        with pytest.raises(RuntimeError):
            canceller.mark_cancelled("build-missing", "rationale")


class TestSqliteBuildResumer:
    """``resume_after_skip`` re-enters RUNNING from PAUSED."""

    def test_resume_from_paused(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Drive to PAUSED.
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PREPARING),
                BuildState.RUNNING,
            )
        )
        persistence.mark_paused(build_id, "req-resume-1")
        resumer = SqliteBuildResumer(persistence)
        resumer.resume_after_skip(build_id, StageClass.SYSTEM_DESIGN)
        row = persistence.connection.execute(
            "SELECT status, pending_approval_request_id FROM builds "
            "WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "RUNNING"
        # Resume clears the pending request id.
        assert row["pending_approval_request_id"] is None

    def test_resume_non_paused_is_noop(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        resumer = SqliteBuildResumer(persistence)
        # No exception; the build remains QUEUED.
        resumer.resume_after_skip(build_id, StageClass.FEATURE_PLAN)
        row = persistence.connection.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        assert row["status"] == "QUEUED"


class TestSqliteStageSkipRecorder:
    """Two methods → two distinct stage_log rows."""

    def test_record_skipped_appends_row(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        recorder = SqliteStageSkipRecorder(persistence)
        recorder.record_skipped(
            build_id, StageClass.SYSTEM_DESIGN, "operator skipped"
        )
        rows = persistence.read_stages(build_id)
        assert len(rows) == 1
        assert rows[0].status == "SKIPPED"
        assert rows[0].details["rationale"] == "operator skipped"

    def test_record_skip_refused_appends_gated_row(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        recorder = SqliteStageSkipRecorder(persistence)
        recorder.record_skip_refused(
            build_id, StageClass.PULL_REQUEST_REVIEW, "constitutional veto"
        )
        rows = persistence.read_stages(build_id)
        assert len(rows) == 1
        assert rows[0].status == "GATED"
        assert rows[0].gate_mode == "HARD_STOP"
        assert rows[0].details["refused"] is True


class TestSqliteStageLogReader:
    """Stage-log reader returns chronologically ordered entries."""

    def test_read_stages_returns_chronological(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Append two entries in chronological order.
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        for i, stage in enumerate([StageClass.SYSTEM_ARCH, StageClass.FEATURE_PLAN]):
            persistence.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label=stage.value,
                    target_kind="local_tool",
                    target_identifier="t",
                    status="PASSED",
                    started_at=base + timedelta(seconds=i),
                    completed_at=base + timedelta(seconds=i + 1),
                    duration_secs=1.0,
                    details={"n": i},
                )
            )
        reader = SqliteStageLogReader(persistence)
        rows = reader.read_stages(build_id)
        assert [r.stage_label for r in rows] == [
            StageClass.SYSTEM_ARCH.value,
            StageClass.FEATURE_PLAN.value,
        ]


class TestSqlitePauseRejectResolver:
    """Synthesises ApprovalResponsePayload(reject) and forwards it."""

    def test_resolve_as_reject_invokes_injector(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        # Drive to PAUSED with a known request id.
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PREPARING),
                BuildState.RUNNING,
            )
        )
        persistence.mark_paused(build_id, "req-rej-1")

        captured: list[Any] = []

        def fake_injector(payload: Any) -> str:
            captured.append(payload)
            return "ok"

        resolver = SqlitePauseRejectResolver(persistence, fake_injector)
        result = resolver.resolve_as_reject(
            build_id=build_id,
            stage=StageClass.PULL_REQUEST_REVIEW,
            feature_id="FEAT-TEST-001",
            rationale="cli-cancel",
        )
        assert result == "ok"
        assert len(captured) == 1
        synthetic = captured[0]
        assert synthetic["request_id"] == "req-rej-1"
        assert synthetic["decision"] == "reject"
        assert synthetic["decided_by"] == "cli-cancel"
        assert synthetic["notes"] == "cli-cancel"
        assert synthetic["_metadata"]["build_id"] == build_id

    def test_resolve_without_pending_request_raises(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, _ = seeded_build
        resolver = SqlitePauseRejectResolver(persistence, lambda p: None)
        with pytest.raises(RuntimeError):
            resolver.resolve_as_reject(
                build_id=build_id,
                stage=StageClass.PULL_REQUEST_REVIEW,
                feature_id="FEAT-TEST-001",
                rationale="rationale",
            )

    def test_resolve_unknown_build_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        resolver = SqlitePauseRejectResolver(persistence, lambda p: None)
        with pytest.raises(RuntimeError):
            resolver.resolve_as_reject(
                build_id="build-missing",
                stage=StageClass.PULL_REQUEST_REVIEW,
                feature_id=None,
                rationale="x",
            )


class TestAsyncTaskPassthroughs:
    """AsyncTaskCanceller / AsyncTaskUpdater forward to injected callables."""

    def test_canceller_forwards_task_id(self) -> None:
        captured: list[str] = []
        canceller = AsyncTaskCanceller(lambda task_id: captured.append(task_id))
        canceller.cancel_async_task("task-42")
        assert captured == ["task-42"]

    def test_canceller_rejects_empty_task_id(self) -> None:
        canceller = AsyncTaskCanceller(lambda task_id: None)
        with pytest.raises(ValueError):
            canceller.cancel_async_task("")

    def test_updater_forwards_directive(self) -> None:
        captured: dict[str, Any] = {}

        def fake_update(task_id: str, *, append_pending_directive: str) -> None:
            captured["task_id"] = task_id
            captured["directive"] = append_pending_directive

        updater = AsyncTaskUpdater(fake_update)
        updater.update_async_task(
            "task-13", append_pending_directive="please-stop"
        )
        assert captured == {"task_id": "task-13", "directive": "please-stop"}

    def test_updater_rejects_empty_args(self) -> None:
        updater = AsyncTaskUpdater(lambda *a, **k: None)
        with pytest.raises(ValueError):
            updater.update_async_task("", append_pending_directive="x")
        with pytest.raises(ValueError):
            updater.update_async_task("t", append_pending_directive="")


# ---------------------------------------------------------------------------
# find_active_or_recent
# ---------------------------------------------------------------------------


class TestFindActiveOrRecent:
    """``find_active_or_recent`` returns active first, else most recent."""

    def test_returns_active_build(
        self,
        persistence: SqliteLifecyclePersistence,
        seeded_build: tuple[str, SimpleNamespace],
    ) -> None:
        build_id, payload = seeded_build
        result = persistence.find_active_or_recent(payload.feature_id)
        assert result is not None
        assert result.build_id == build_id
        assert result.status is BuildState.QUEUED

    def test_falls_back_to_terminal_when_none_active(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        payload = _make_payload(
            feature_id="FEAT-ONLY-TERM",
            correlation_id="corr-only-1",
        )
        build_id = persistence.record_pending_build(payload)
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.CANCELLED,
            )
        )
        result = persistence.find_active_or_recent("FEAT-ONLY-TERM")
        assert result is not None
        assert result.build_id == build_id
        assert result.status is BuildState.CANCELLED

    def test_unknown_feature_returns_none(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert persistence.find_active_or_recent("FEAT-NEVER") is None

    def test_empty_feature_id_raises(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError):
            persistence.find_active_or_recent("")


# ---------------------------------------------------------------------------
# TIMEOUT TRUTH — ``builds.terminal_class`` (schema_v9)
# ---------------------------------------------------------------------------


class TestRecordTerminalClass:
    """The durable answer to "WHICH of the five deaths was this?".

    ``builds.status`` spells all five ``FAILED``; this column is the machine-
    readable difference, and it never touches ``status``.
    """

    def test_a_classified_build_reads_back(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        build_id, _payload = seeded_build
        persistence.record_terminal_class(build_id, "timeout-wedge")
        assert persistence.read_terminal_class(build_id) == "timeout-wedge"

    def test_an_unclassified_build_reads_back_NULL(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        """The common case: every ordinary failure and every historical row."""
        build_id, _payload = seeded_build
        assert persistence.read_terminal_class(build_id) is None

    def test_first_write_wins(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        """A redelivery or replay re-presents the SAME verdict; a later write
        could only be a duplicate or a degradation."""
        build_id, _payload = seeded_build
        persistence.record_terminal_class(build_id, "timeout-in-band")
        persistence.record_terminal_class(build_id, "timeout-wall-clock")
        assert persistence.read_terminal_class(build_id) == "timeout-in-band"

    def test_it_never_touches_status(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        """THE HONESTY LAW: a recorder records, it never mints a state."""
        build_id, _payload = seeded_build
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.PREPARING,
            )
        )
        persistence.record_terminal_class(build_id, "timeout-budget-cap")
        assert persistence.read_status()[0].status is BuildState.PREPARING

    def test_an_unknown_build_is_a_quiet_noop(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        persistence.record_terminal_class("build-does-not-exist", "timeout-wedge")
        assert persistence.read_terminal_class("build-does-not-exist") is None

    def test_empty_arguments_are_refused(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        build_id, _payload = seeded_build
        with pytest.raises(ValueError):
            persistence.record_terminal_class("", "timeout-wedge")
        with pytest.raises(ValueError):
            persistence.record_terminal_class(build_id, "")
        with pytest.raises(ValueError):
            persistence.read_terminal_class("")

    def test_it_reaches_the_status_projection(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        build_id, _payload = seeded_build
        persistence.record_terminal_class(build_id, "timeout-wedge")
        view = persistence.read_status()[0]
        assert view.terminal_class == "timeout-wedge"

    def test_the_projection_says_None_when_unclassified(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        assert persistence.read_status()[0].terminal_class is None

    def test_it_reaches_the_full_build_row(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        build_id, _payload = seeded_build
        persistence.record_terminal_class(build_id, "timeout-in-band")
        rows = persistence.read_history(limit=5)
        assert rows[0].terminal_class == "timeout-in-band"


class TestPositionalFallbackOrdering:
    """THE LANDMINE this file's own comment records.

    ``_row_to_build_row``'s tuple fallback maps ``SELECT *`` BY POSITION. v8
    was nearly shifted onto ``last_coach_score``'s value by an omission in
    exactly this tuple; v9's ``terminal_class`` is appended for the same
    reason. This test reads a real row both ways and demands they agree — so
    any future ALTER TABLE that forgets the tuple fails here rather than
    silently returning the WRONG COLUMN'S value.
    """

    def test_a_positional_row_hydrates_identically_to_a_named_row(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        from forge.lifecycle.persistence import _row_to_build_row

        build_id, _payload = seeded_build
        persistence.record_terminal_class(build_id, "timeout-wall-clock")
        persistence.record_budget_breach(build_id, "wall_clock: 10s > 5s")
        persistence.record_last_coach_score(build_id, 0.75)

        with persistence._reader() as cx:
            named = cx.execute(
                "SELECT * FROM builds WHERE build_id = ?", (build_id,)
            ).fetchone()
        by_name = _row_to_build_row(named)
        by_position = _row_to_build_row(tuple(named))

        assert by_position == by_name
        assert by_position.terminal_class == "timeout-wall-clock"
        assert by_position.task_id is None

    def test_the_fallback_tuple_covers_every_builds_column(
        self, persistence: SqliteLifecyclePersistence, seeded_build
    ) -> None:
        """A structural guard: the tuple must be the table, in table order.

        Read the literal ``keys = (...)`` tuple out of the function's own
        source and compare it to ``PRAGMA table_info`` — the exact order
        SQLite returns for ``SELECT *``. Any future ALTER TABLE that forgets
        this tuple fails HERE, loudly, instead of silently reading one column
        into another's field.
        """
        import ast
        import inspect
        import textwrap

        from forge.lifecycle import persistence as persistence_module

        with persistence._reader() as cx:
            columns = [row[1] for row in cx.execute("PRAGMA table_info(builds)")]

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(persistence_module._row_to_build_row))
        )
        keys: list[str] | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "keys" for t in node.targets
                )
                and isinstance(node.value, ast.Tuple)
            ):
                keys = [ast.literal_eval(elt) for elt in node.value.elts]
                break

        assert keys is not None, "_row_to_build_row no longer has a `keys` tuple"
        assert keys == columns, (
            "the positional fallback tuple has drifted from the builds table: "
            f"tuple={keys!r} vs table={columns!r}. A SELECT * read would take "
            "the WRONG column's value."
        )
