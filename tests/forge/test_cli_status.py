"""Tests for ``forge.cli.status`` (TASK-PSM-009).

Acceptance-criteria coverage map:

* AC-001: ``forge status`` (no args) shows active builds + 5 most recent
  terminal — :class:`TestDefaultView`.
* AC-002: ``forge status FEAT-XXX`` filters to that feature only —
  :class:`TestFeatureFilter`.
* AC-003: ``forge status --watch`` polls every 2s, re-renders, exits on
  terminal — :class:`TestWatchMode`.
* AC-004: ``forge status --full`` includes the last 5 stage_log entries
  per build — :class:`TestFullView`.
* AC-005: ``forge status --json`` emits a JSON array; each row matches
  :class:`BuildStatusView` — :class:`TestJsonOutput`.
* AC-006: ``cli/status.py`` imports zero modules from
  ``forge.adapters.nats.*`` — :class:`TestNoNatsImports`.
* AC-007: BDD scenario, NATS unreachable + ``forge status`` succeeds —
  :class:`TestNatsUnreachable`.
* AC-008: status query during active write returns within reasonable
  bound — :class:`TestStatusResponsiveWhileWriterActive`.

The CLI surface is exercised via Click's :class:`CliRunner` against a
real in-memory-on-disk SQLite database created by the lifecycle
migrations module — no mocking of the storage layer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.status import (
    _FULL_STAGE_LIMIT,
    _RECENT_TERMINAL_LIMIT,
    _WATCH_INTERVAL_SECS,
    _all_terminal,
    _read_status_views,
    status_cmd,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    BuildStatusView,
    SqliteLifecyclePersistence,
    StageLogEntry,
)
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle.state_machine import transition as compose_transition
from forge.lifecycle.persistence import Build

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str,
    correlation_id: str,
    queued_at: datetime,
) -> SimpleNamespace:
    """Construct a duck-typed BuildQueuedPayload."""
    return SimpleNamespace(
        feature_id=feature_id,
        repo="appmilla/forge",
        branch="main",
        feature_yaml_path=f"features/{feature_id}/feature.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=queued_at,
        requested_at=queued_at,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create a freshly-migrated db file and return its path."""
    path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.close()
    return path


@pytest.fixture()
def writer(db_path: Path) -> sqlite3.Connection:
    """Re-open the writer connection for seeding test data."""
    cx = sqlite_connect.connect_writer(db_path)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer: sqlite3.Connection, db_path: Path
) -> SqliteLifecyclePersistence:
    """Return a persistence facade bound to the writer + db_path."""
    return SqliteLifecyclePersistence(connection=writer, db_path=db_path)


def _seed_build(
    persistence: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
    target_state: BuildState,
    queued_at: datetime,
) -> str:
    """Seed a build and drive it to ``target_state`` via the state machine."""
    payload = _make_payload(
        feature_id=feature_id,
        correlation_id=correlation_id,
        queued_at=queued_at,
    )
    build_id = persistence.record_pending_build(payload)
    state_path = {
        BuildState.QUEUED: [],
        BuildState.PREPARING: [BuildState.PREPARING],
        BuildState.RUNNING: [BuildState.PREPARING, BuildState.RUNNING],
        BuildState.PAUSED: [
            BuildState.PREPARING,
            BuildState.RUNNING,
            BuildState.PAUSED,
        ],
        BuildState.FINALISING: [
            BuildState.PREPARING,
            BuildState.RUNNING,
            BuildState.FINALISING,
        ],
        BuildState.COMPLETE: [
            BuildState.PREPARING,
            BuildState.RUNNING,
            BuildState.FINALISING,
            BuildState.COMPLETE,
        ],
        BuildState.FAILED: [
            BuildState.PREPARING,
            BuildState.RUNNING,
            BuildState.FINALISING,
            BuildState.FAILED,
        ],
        BuildState.CANCELLED: [BuildState.CANCELLED],
        BuildState.SKIPPED: [
            BuildState.PREPARING,
            BuildState.RUNNING,
            BuildState.SKIPPED,
        ],
    }
    current = BuildState.QUEUED
    for next_state in state_path[target_state]:
        kwargs: dict[str, Any] = {}
        if next_state in (
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        ):
            kwargs["completed_at"] = queued_at + timedelta(minutes=5)
        if next_state is BuildState.PAUSED:
            kwargs["pending_approval_request_id"] = "req-001"
        t = compose_transition(
            Build(build_id=build_id, status=current),
            next_state,
            **kwargs,
        )
        persistence.apply_transition(t)
        current = next_state
    return build_id


def _seed_stage_log(
    persistence: SqliteLifecyclePersistence,
    *,
    build_id: str,
    count: int,
    base_time: datetime,
) -> None:
    """Seed ``count`` stage_log rows for a build."""
    for i in range(count):
        started = base_time + timedelta(minutes=i)
        completed = started + timedelta(seconds=30)
        persistence.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=f"stage-{i:02d}",
                target_kind="local_tool",
                target_identifier=f"tool-{i}",
                status="PASSED",
                gate_mode=None,
                started_at=started,
                completed_at=completed,
                duration_secs=30.0,
                details={"index": i},
            )
        )


# ---------------------------------------------------------------------------
# AC-006: import discipline (static-analysis check)
# ---------------------------------------------------------------------------


class TestNoNatsImports:
    """``cli/status.py`` MUST NOT import any module from
    ``forge.adapters.nats.*``."""

    def test_no_nats_imports_in_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        src_path = repo_root / "src" / "forge" / "cli" / "status.py"
        text = src_path.read_text(encoding="utf-8")
        # Any import (top-level or function-local) of forge.adapters.nats.*
        # is a direct violation of review F6 / Group H.
        assert not re.search(
            r"\bfrom\s+forge\.adapters\.nats\b", text
        ), "forge.cli.status must not import forge.adapters.nats.*"
        assert not re.search(
            r"\bimport\s+forge\.adapters\.nats\b", text
        ), "forge.cli.status must not import forge.adapters.nats.*"

    def test_no_nats_in_module_imports_at_runtime(self) -> None:
        import forge.cli.status as status_mod

        seen = set(getattr(status_mod, "__dict__", {}).keys())
        for name in seen:
            value = getattr(status_mod, name, None)
            module_name = getattr(value, "__module__", "") or ""
            assert not module_name.startswith("forge.adapters.nats"), (
                f"forge.cli.status pulled in nats module via {name}: " f"{module_name}"
            )


# ---------------------------------------------------------------------------
# AC-001: default view — active + 5 recent terminal
# ---------------------------------------------------------------------------


class TestDefaultView:
    """Default ``forge status`` shows active builds + 5 recent terminal."""

    def test_default_view_includes_active_and_terminal(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        # 2 active
        _seed_build(
            persistence,
            feature_id="FEAT-A",
            correlation_id="corr-A",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        _seed_build(
            persistence,
            feature_id="FEAT-B",
            correlation_id="corr-B",
            target_state=BuildState.QUEUED,
            queued_at=base + timedelta(minutes=1),
        )
        # 7 terminal — only the most-recent 5 should be returned.
        for i in range(7):
            _seed_build(
                persistence,
                feature_id=f"FEAT-T{i}",
                correlation_id=f"corr-T{i}",
                target_state=BuildState.COMPLETE,
                queued_at=base - timedelta(hours=i + 1),
            )

        views = _read_status_views(db_path, feature_id=None)
        assert len(views) == 2 + _RECENT_TERMINAL_LIMIT
        # Sorted newest-first.
        assert views == sorted(views, key=lambda v: v.queued_at, reverse=True)

    def test_default_view_renders_table(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-RENDER",
            correlation_id="corr-R",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "FEAT-RENDER" in result.output
        assert "RUNNING" in result.output


# ---------------------------------------------------------------------------
# AC-002: feature filter
# ---------------------------------------------------------------------------


class TestFeatureFilter:
    """Positional ``feature_id`` filters to that feature, all builds."""

    def test_feature_filter_returns_only_matching(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-X",
            correlation_id="corr-X1",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        _seed_build(
            persistence,
            feature_id="FEAT-X",
            correlation_id="corr-X2",
            target_state=BuildState.COMPLETE,
            queued_at=base - timedelta(hours=1),
        )
        _seed_build(
            persistence,
            feature_id="FEAT-Y",
            correlation_id="corr-Y1",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        views = _read_status_views(db_path, feature_id="FEAT-X")
        assert len(views) == 2
        assert all(v.feature_id == "FEAT-X" for v in views)
        assert views[0].queued_at >= views[1].queued_at

    def test_feature_filter_via_cli(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-Q",
            correlation_id="corr-Q",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        _seed_build(
            persistence,
            feature_id="FEAT-Z",
            correlation_id="corr-Z",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["FEAT-Q", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "FEAT-Q" in result.output
        assert "FEAT-Z" not in result.output


# ---------------------------------------------------------------------------
# AC-005: --json output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    """``--json`` emits a JSON array; rows match ``BuildStatusView``."""

    def test_json_output_is_array_of_status_views(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-J",
            correlation_id="corr-J",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--json", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == 1
        # Each row must round-trip through BuildStatusView.
        for row in payload:
            view = BuildStatusView.model_validate(row)
            assert view.feature_id == "FEAT-J"
            assert view.status is BuildState.RUNNING

    def test_json_empty_db_returns_empty_array(
        self,
        db_path: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--json", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == []


# ---------------------------------------------------------------------------
# AC-004: --full clamps stage tail to 5
# ---------------------------------------------------------------------------


class TestFullView:
    """``--full`` includes the last 5 stage_log entries per build."""

    def test_full_view_caps_stage_detail_at_five(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        build_id = _seed_build(
            persistence,
            feature_id="FEAT-F",
            correlation_id="corr-F",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        # 8 stages — only the last 5 must appear.
        _seed_stage_log(persistence, build_id=build_id, count=8, base_time=base)

        runner = CliRunner()
        result = runner.invoke(
            status_cmd,
            ["--json", "--full", "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        stages = payload[0].get("stages")
        assert stages is not None, "--full --json must include 'stages' key"
        assert len(stages) == _FULL_STAGE_LIMIT
        # Should be the LAST 5 stages (indices 3..7).
        labels = [s["stage_label"] for s in stages]
        assert labels == [f"stage-{i:02d}" for i in range(8 - _FULL_STAGE_LIMIT, 8)]

    def test_full_view_with_fewer_than_five_stages(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        build_id = _seed_build(
            persistence,
            feature_id="FEAT-F2",
            correlation_id="corr-F2",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        _seed_stage_log(persistence, build_id=build_id, count=2, base_time=base)

        runner = CliRunner()
        result = runner.invoke(
            status_cmd,
            ["--json", "--full", "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        stages = payload[0]["stages"]
        assert len(stages) == 2


# ---------------------------------------------------------------------------
# AC-003: --watch mode
# ---------------------------------------------------------------------------


class TestWatchMode:
    """``--watch`` polls every 2s, re-renders, exits when all terminal."""

    def test_watch_interval_is_two_seconds(self) -> None:
        # AC: per ``API-cli.md §4.2`` the watch loop polls every 2s.
        assert _WATCH_INTERVAL_SECS == 2.0

    def test_all_terminal_helper_with_only_terminal_states(
        self,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        views = [
            BuildStatusView(
                build_id="b1",
                feature_id="FEAT-A",
                status=BuildState.COMPLETE,
                queued_at=base,
                completed_at=base + timedelta(minutes=5),
            ),
            BuildStatusView(
                build_id="b2",
                feature_id="FEAT-B",
                status=BuildState.FAILED,
                queued_at=base,
                completed_at=base + timedelta(minutes=5),
            ),
        ]
        assert _all_terminal(views) is True

    def test_all_terminal_helper_with_active_state(self) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        views = [
            BuildStatusView(
                build_id="b1",
                feature_id="FEAT-A",
                status=BuildState.RUNNING,
                queued_at=base,
            ),
        ]
        assert _all_terminal(views) is False

    def test_all_terminal_helper_empty_list(self) -> None:
        # An empty list must be considered terminal so the watch loop
        # exits cleanly when the queue drains.
        assert _all_terminal([]) is True

    def test_watch_mode_exits_when_only_terminal(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-W",
            correlation_id="corr-W",
            target_state=BuildState.COMPLETE,
            queued_at=base,
        )
        runner = CliRunner()
        result = runner.invoke(
            status_cmd,
            ["--watch", "--db-path", str(db_path)],
        )
        # Must not hang — terminal-only state means immediate exit.
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# AC-007: NATS unreachable + status succeeds
# ---------------------------------------------------------------------------


class TestNatsUnreachable:
    """Status command works without the messaging layer (Group H)."""

    def test_status_works_without_nats_module_imported(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate "NATS unreachable" by ensuring no nats module is even
        # loaded into sys.modules at import time.
        import sys

        # Block any future attempt to import forge.adapters.nats.*.
        class _ForbiddenFinder:
            def find_module(self, name: str, path: Any = None) -> Any:
                if name.startswith("forge.adapters.nats"):
                    raise ImportError(f"NATS adapters are unreachable: {name}")
                return None

        monkeypatch.setattr(sys, "meta_path", [_ForbiddenFinder()] + sys.meta_path)

        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-NATS-DOWN",
            correlation_id="corr-N",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )
        # Re-import status module — must succeed without NATS.
        # Force a clean re-read of the module.
        sys.modules.pop("forge.cli.status", None)
        import forge.cli.status as reimport

        runner = CliRunner()
        result = runner.invoke(reimport.status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "FEAT-NATS-DOWN" in result.output


# ---------------------------------------------------------------------------
# AC-008: status responsive while writer active
# ---------------------------------------------------------------------------


class TestStatusResponsiveWhileWriterActive:
    """A status query while a writer is mid-transaction returns promptly."""

    def test_read_status_returns_within_reasonable_bound(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        import time

        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        _seed_build(
            persistence,
            feature_id="FEAT-CONC",
            correlation_id="corr-C",
            target_state=BuildState.RUNNING,
            queued_at=base,
        )

        # Writer holds a BEGIN IMMEDIATE transaction; reader must still
        # complete because we open a fresh ``mode=ro`` URI handle and
        # WAL mode on the writer permits concurrent readers (DDR-003).
        writer_cx = persistence.connection
        writer_cx.execute("BEGIN IMMEDIATE;")
        try:
            start = time.monotonic()
            views = _read_status_views(db_path, feature_id=None)
            elapsed = time.monotonic() - start
        finally:
            writer_cx.execute("ROLLBACK;")

        assert len(views) == 1
        # Reasonable bound — well under the 5s busy-timeout default.
        assert elapsed < 2.0, (
            f"read_status took {elapsed:.2f}s while writer active; "
            "exceeded reasonable bound"
        )


# ---------------------------------------------------------------------------
# Helper: status_cmd is a Click command
# ---------------------------------------------------------------------------


class TestStatusCommandShape:
    """``status_cmd`` is a Click command exposing the four flags."""

    def test_status_cmd_is_click_command(self) -> None:
        import click

        assert isinstance(status_cmd, click.Command)
        assert status_cmd.name == "status"

    def test_help_lists_all_flags(self) -> None:
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--help"])
        assert result.exit_code == 0
        for flag in ("--watch", "--full", "--json", "--in-flight"):
            assert (
                flag in result.output
            ), f"--help output missing {flag}: {result.output!r}"


# ---------------------------------------------------------------------------
# TASK-FRR-PEB-012: ``forge status --in-flight`` surface
# ---------------------------------------------------------------------------


class TestInFlightSurface:
    """``forge status --in-flight`` queries the lifecycle bridge registry.

    Acceptance-criteria coverage:

    * AC-1: ``forge status --in-flight`` queries the registry.
    * AC-2: Output format matches the existing table style.
    * AC-3: Empty registry → ``No in-flight builds.``.
    * AC-4: Combines cleanly with ``--db-path``, ``--json`` and the
      positional ``feature_id`` filter; rejects ``--watch`` / ``--full``.
    * AC-5: Read-only — the surface MUST NOT mutate the registry.
    """

    @pytest.fixture()
    def bridge_registry(self, db_path: Path):
        """Apply the bridge migration and yield a writeable registry.

        Tests that need to seed the in-flight registry use this fixture
        so the table exists; tests that exercise the empty-table branch
        deliberately skip seeding rather than skipping the fixture.
        """
        from forge.persistence.migrations import (
            lifecycle_bridge_registry as bridge_migration,
        )
        from forge.persistence.repositories.bridge_registry import (
            BridgeRegistry,
        )

        cx = sqlite_connect.connect_writer(db_path)
        bridge_migration.apply(cx)
        registry = BridgeRegistry(connection=cx)
        try:
            yield registry
        finally:
            cx.close()

    def _seed_entry(
        self,
        registry,
        *,
        feature_id: str,
        thread_id: str = "thread-001",
        run_id: str = "run-001",
        correlation_id: str = "corr-001",
        attached_at: datetime | None = None,
        deadline_secs: int = 300,
        current_lifecycle: str = "running",
    ) -> None:
        from forge.persistence.repositories.bridge_registry import (
            BridgeRegistryEntry,
        )

        if attached_at is None:
            attached_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        deadline_at = attached_at + timedelta(seconds=deadline_secs)
        entry = BridgeRegistryEntry(
            feature_id=feature_id,
            thread_id=thread_id,
            run_id=run_id,
            correlation_id=correlation_id,
            ack_handle_token=f"ack-{feature_id}",
            deadline_at=deadline_at,
            attached_at=attached_at,
            current_lifecycle=current_lifecycle,
            updated_at=attached_at,
            last_event_id=None,
        )
        registry.record(entry, correlation_id=correlation_id)

    # ------------------------------------------------------------------
    # AC-1: queries the registry
    # ------------------------------------------------------------------

    def test_in_flight_returns_registry_rows(
        self, bridge_registry, db_path: Path
    ) -> None:
        self._seed_entry(
            bridge_registry,
            feature_id="FEAT-IF-1",
            thread_id="thread-IF-1",
            run_id="run-IF-1",
            correlation_id="corr-IF-1",
        )
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--in-flight", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "FEAT-IF-1" in result.output
        assert "thread-IF-1" in result.output
        assert "run-IF-1" in result.output

    # ------------------------------------------------------------------
    # AC-2: table style matches existing forge status output
    # ------------------------------------------------------------------

    def test_in_flight_renders_table_with_canonical_columns(
        self, bridge_registry, db_path: Path
    ) -> None:
        self._seed_entry(bridge_registry, feature_id="FEAT-COLS")
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--in-flight", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        for header in ("FEATURE", "LIFECYCLE", "THREAD", "RUN", "ATTACHED", "DEADLINE"):
            assert (
                header in result.output
            ), f"--in-flight table missing header {header!r}: {result.output!r}"
        # The view shares Rich-table styling with the default status
        # table — the title prefix is stable across both.
        assert "Forge in-flight builds" in result.output

    # ------------------------------------------------------------------
    # AC-3: empty registry → "No in-flight builds."
    # ------------------------------------------------------------------

    def test_in_flight_empty_registry_emits_canonical_line(
        self, bridge_registry, db_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--in-flight", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "No in-flight builds." in result.output

    def test_in_flight_no_bridge_table_renders_empty_message(
        self, db_path: Path
    ) -> None:
        # No bridge migration applied — table missing, treat as empty.
        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--in-flight", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "No in-flight builds." in result.output

    # ------------------------------------------------------------------
    # AC-4: combines cleanly with existing flags
    # ------------------------------------------------------------------

    def test_in_flight_with_json_emits_json_array(
        self, bridge_registry, db_path: Path
    ) -> None:
        self._seed_entry(
            bridge_registry,
            feature_id="FEAT-JSON",
            thread_id="thread-JSON",
            run_id="run-JSON",
            correlation_id="corr-JSON",
        )
        runner = CliRunner()
        result = runner.invoke(
            status_cmd,
            ["--in-flight", "--json", "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == 1
        row = payload[0]
        assert row["feature_id"] == "FEAT-JSON"
        assert row["thread_id"] == "thread-JSON"
        assert row["run_id"] == "run-JSON"
        assert row["correlation_id"] == "corr-JSON"
        # ack_handle_token is internal book-keeping — must not leak.
        assert "ack_handle_token" not in row

    def test_in_flight_empty_with_json_emits_empty_array(
        self, bridge_registry, db_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            status_cmd,
            ["--in-flight", "--json", "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == []

    def test_in_flight_with_feature_filter_returns_only_match(
        self, bridge_registry, db_path: Path
    ) -> None:
        self._seed_entry(bridge_registry, feature_id="FEAT-A")
        self._seed_entry(
            bridge_registry,
            feature_id="FEAT-B",
            attached_at=datetime(2026, 4, 27, 12, 1, 0, tzinfo=UTC),
        )
        runner = CliRunner()
        result = runner.invoke(
            status_cmd, ["--in-flight", "--db-path", str(db_path), "FEAT-A"]
        )
        assert result.exit_code == 0, result.output
        assert "FEAT-A" in result.output
        assert "FEAT-B" not in result.output

    def test_in_flight_with_watch_raises_usage_error(
        self, bridge_registry, db_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            status_cmd, ["--in-flight", "--watch", "--db-path", str(db_path)]
        )
        assert result.exit_code != 0
        assert "--in-flight" in result.output
        assert "--watch" in result.output

    def test_in_flight_with_full_raises_usage_error(
        self, bridge_registry, db_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            status_cmd, ["--in-flight", "--full", "--db-path", str(db_path)]
        )
        assert result.exit_code != 0
        assert "--in-flight" in result.output
        assert "--full" in result.output

    # ------------------------------------------------------------------
    # AC-5: read-only — registry is unchanged after the CLI surface runs.
    # ------------------------------------------------------------------

    def test_in_flight_does_not_mutate_registry(
        self, bridge_registry, db_path: Path
    ) -> None:
        self._seed_entry(bridge_registry, feature_id="FEAT-RO-1")
        self._seed_entry(
            bridge_registry,
            feature_id="FEAT-RO-2",
            attached_at=datetime(2026, 4, 27, 12, 5, 0, tzinfo=UTC),
        )
        before = bridge_registry.list_active(correlation_id="cli-test:before")

        runner = CliRunner()
        # Run multiple variants — table, json, filter — to be sure the
        # CLI does not write through any branch.
        for argv in (
            ["--in-flight", "--db-path", str(db_path)],
            ["--in-flight", "--json", "--db-path", str(db_path)],
            ["--in-flight", "--db-path", str(db_path), "FEAT-RO-1"],
        ):
            result = runner.invoke(status_cmd, argv)
            assert result.exit_code == 0, result.output

        # Re-read with a fresh registry against the same writer. The set
        # of feature ids and the recorded attachment metadata must be
        # byte-for-byte identical.
        after = bridge_registry.list_active(correlation_id="cli-test:after")
        assert [e.feature_id for e in after] == [e.feature_id for e in before]
        assert [e.thread_id for e in after] == [e.thread_id for e in before]
        assert [e.run_id for e in after] == [e.run_id for e in before]
        assert [e.attached_at for e in after] == [e.attached_at for e in before]

    def test_in_flight_uses_read_only_connection(
        self, bridge_registry, db_path: Path
    ) -> None:
        # Defence-in-depth: confirm the cli/status.py module routes
        # through ``read_only_connect``. The function-name check guards
        # against a future refactor that swapped the read path for a
        # writer connection.
        import forge.cli.status as status_mod

        assert hasattr(status_mod, "read_only_connect")
        # The helper function we added uses read_only_connect; assert
        # the import is present rather than a writer connection helper.
        src = Path(status_mod.__file__).read_text(encoding="utf-8")
        assert "_read_in_flight_entries" in src
        assert "read_only_connect" in src
        assert "connect_writer" not in src


# ---------------------------------------------------------------------------
# TIMEOUT TRUTH — the CLASS column (schema_v9)
# ---------------------------------------------------------------------------


class TestTerminalClassColumn:
    """``FAILED`` is one word for five deaths; CLASS is the difference.

    STATUS is untouched by every test here — that is the point. An operator
    reading the table can now tell "this ran out of time" from "this is
    broken" without opening the failure pack, and nothing that already reads
    STATUS sees anything change.
    """

    def _failed_build(
        self,
        persistence: SqliteLifecyclePersistence,
        *,
        feature_id: str = "FEAT-CLASS",
        terminal_class: str | None = None,
    ) -> str:
        build_id = _seed_build(
            persistence,
            feature_id=feature_id,
            correlation_id=f"corr-{feature_id}",
            target_state=BuildState.FAILED,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        if terminal_class is not None:
            persistence.record_terminal_class(build_id, terminal_class)
        return build_id

    def test_a_classified_build_renders_its_class(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        self._failed_build(persistence, terminal_class="timeout-wedge")
        result = CliRunner().invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "CLASS" in result.output
        assert "timeout-wedge" in result.output

    def test_the_STATUS_cell_still_reads_exactly_FAILED(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        """No new status value. Not now, not ever, on this lane."""
        self._failed_build(persistence, terminal_class="timeout-budget-cap")
        result = CliRunner().invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert BuildState.FAILED.value in result.output
        views = _read_status_views(db_path, feature_id=None)
        assert views[0].status is BuildState.FAILED

    def test_an_unclassified_build_renders_a_dash(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        """The byte-identity control: an ordinary failure looks like before."""
        self._failed_build(persistence)
        views = _read_status_views(db_path, feature_id=None)
        assert views[0].terminal_class is None
        result = CliRunner().invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "timeout-" not in result.output

    def test_a_healthy_build_is_never_classified(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        _seed_build(
            persistence,
            feature_id="FEAT-HEALTHY",
            correlation_id="corr-healthy",
            target_state=BuildState.RUNNING,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        views = _read_status_views(db_path, feature_id=None)
        assert views[0].terminal_class is None

    def test_the_class_reaches_json_output(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        self._failed_build(persistence, terminal_class="timeout-in-band")
        result = CliRunner().invoke(
            status_cmd, ["--json", "--db-path", str(db_path)]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 1
        view = BuildStatusView.model_validate(rows[0])
        assert view.terminal_class == "timeout-in-band"
        assert view.status is BuildState.FAILED

    def test_the_feature_filtered_projection_carries_it_too(
        self, persistence: SqliteLifecyclePersistence, db_path: Path
    ) -> None:
        """Both SELECT branches must name the column, not just the default one."""
        self._failed_build(
            persistence, feature_id="FEAT-FILT", terminal_class="timeout-wall-clock"
        )
        views = _read_status_views(db_path, feature_id="FEAT-FILT")
        assert len(views) == 1
        assert views[0].terminal_class == "timeout-wall-clock"

    def test_a_pre_v9_database_still_renders(
        self, tmp_path: Path
    ) -> None:
        """The upgrade window: the column may simply not exist yet.

        ``forge status`` must not crash against a database the daemon has not
        yet migrated — "not classified" is the honest read.
        """
        from forge.lifecycle import migrations as lifecycle_migrations

        legacy = tmp_path / "legacy.db"
        cx = sqlite_connect.connect_writer(legacy)
        original = lifecycle_migrations._MIGRATIONS
        lifecycle_migrations._MIGRATIONS = tuple(
            m for m in original if m[0] <= 8
        )
        try:
            lifecycle_migrations.apply_at_boot(cx)
        finally:
            lifecycle_migrations._MIGRATIONS = original
        persistence = SqliteLifecyclePersistence(connection=cx, db_path=legacy)
        _seed_build(
            persistence,
            feature_id="FEAT-LEGACY",
            correlation_id="corr-legacy",
            target_state=BuildState.FAILED,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        cx.close()

        result = CliRunner().invoke(status_cmd, ["--db-path", str(legacy)])
        assert result.exit_code == 0, result.output
        assert "FEAT-LEGACY" in result.output


# ---------------------------------------------------------------------------
# THE IN-FLIGHT STAGE ROW (design §h stage 1) — the STAGE cell speaks
# ---------------------------------------------------------------------------


class TestInFlightStageCell:
    """The STAGE cell was ``—`` for every running build.

    The one question an operator asks of a running build — what is it doing
    right now? — was the one the table could not answer. It is now filled from
    the build monitor's heartbeat, under three rules these tests hold to:
    absent reads exactly as before, stale never reads as live, and a terminal
    row never consults the file at all.
    """

    @pytest.fixture(autouse=True)
    def _fenced_receipts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge import receipts as forge_receipts

        monkeypatch.setenv(
            forge_receipts.RECEIPTS_DIR_ENV, str(tmp_path / "receipts")
        )
        monkeypatch.delenv("FORGE_BUILD_MONITOR_POLL_SECONDS", raising=False)

    def _running_build(
        self,
        persistence: SqliteLifecyclePersistence,
        *,
        feature_id: str = "FEAT-LIVE",
    ) -> str:
        return _seed_build(
            persistence,
            feature_id=feature_id,
            correlation_id=f"corr-{feature_id}",
            target_state=BuildState.RUNNING,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )

    @staticmethod
    def _seed_heartbeat(
        tmp_path: Path,
        build_id: str,
        *,
        age_seconds: float = 0.0,
        **overrides: Any,
    ) -> Path:
        from forge import receipts as forge_receipts

        payload: dict[str, Any] = {
            "build_id": build_id,
            "feature_id": "FEAT-LIVE",
            "updated_at": (
                datetime.now(UTC) - timedelta(seconds=age_seconds)
            ).isoformat(),
            "description": "task=TASK-LIVE-007 turn=4",
            "last_task_id": "TASK-LIVE-007",
            "last_turn": 4,
            "last_decision": "feedback",
            "last_wave": 2,
            "tasks_completed": 1,
            "tasks_failed": 0,
            "current_wave": 2,
            "window_seconds": 3120.0,
            "window_source": "wave-execution-banner",
        }
        payload.update(overrides)
        path = (
            tmp_path
            / "receipts"
            / build_id
            / forge_receipts.IN_FLIGHT_STATE_NAME
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # -- the cell renderer, as a unit -------------------------------------

    def test_the_cell_names_the_task_the_turn_and_the_wave(
        self, tmp_path: Path
    ) -> None:
        from forge.cli.status import _in_flight_stage_cell

        self._seed_heartbeat(tmp_path, "build-x")
        payload = json.loads(
            (tmp_path / "receipts" / "build-x" / "in-flight.json").read_text()
        )
        assert _in_flight_stage_cell(payload) == "TASK-LIVE-007 turn 4 wave 2"

    def test_a_build_that_has_not_named_a_task_reads_starting(self) -> None:
        from forge.cli.status import _in_flight_stage_cell

        cell = _in_flight_stage_cell(
            {"updated_at": datetime.now(UTC).isoformat(), "last_task_id": None}
        )
        assert cell == "starting"

    def test_a_stale_heartbeat_says_so_and_never_reads_as_live(self) -> None:
        """THE FENCE. A dead sidecar must not leave the table claiming a
        liveness nobody can see."""
        from forge.cli.status import _in_flight_stage_cell

        cell = _in_flight_stage_cell(
            {
                "updated_at": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                "last_task_id": "TASK-LIVE-007",
                "last_turn": 4,
            }
        )
        assert cell.endswith("(stale)")

    def test_an_unstamped_heartbeat_is_stale_by_definition(self) -> None:
        from forge.cli.status import _in_flight_is_stale

        assert _in_flight_is_stale({}) is True
        assert _in_flight_is_stale({"updated_at": "not-a-timestamp"}) is True
        assert _in_flight_is_stale({"updated_at": 1754500000}) is True

    def test_a_naive_timestamp_is_read_as_utc_not_rejected(self) -> None:
        """The writer always stamps a zone; a hand-edited file might not."""
        from forge.cli.status import _in_flight_is_stale

        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        assert _in_flight_is_stale({"updated_at": naive}) is False

    # -- the reader --------------------------------------------------------

    def test_an_absent_file_reads_as_absent(self, tmp_path: Path) -> None:
        from forge.cli.status import _read_in_flight

        assert _read_in_flight("build-nothing-here") is None

    def test_a_malformed_file_reads_as_absent_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """A status table must never fail over a decoration."""
        from forge.cli.status import _read_in_flight

        path = tmp_path / "receipts" / "build-broken" / "in-flight.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ this is not json", encoding="utf-8")
        assert _read_in_flight("build-broken") is None

    def test_a_json_scalar_reads_as_absent(self, tmp_path: Path) -> None:
        from forge.cli.status import _read_in_flight

        path = tmp_path / "receipts" / "build-scalar" / "in-flight.json"
        path.parent.mkdir(parents=True)
        path.write_text('"a string"', encoding="utf-8")
        assert _read_in_flight("build-scalar") is None

    # -- the table ---------------------------------------------------------

    def test_a_running_build_renders_its_live_stage(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        build_id = self._running_build(persistence)
        self._seed_heartbeat(tmp_path, build_id)
        result = CliRunner().invoke(
            status_cmd, ["--db-path", str(db_path)], terminal_width=200
        )
        assert result.exit_code == 0, result.output
        assert "TASK-LIVE-007" in result.output
        assert "turn 4" in result.output

    def test_with_no_heartbeat_the_cell_is_the_dash_it_always_was(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        """The byte-identity control for every build that predates this lane."""
        self._running_build(persistence)
        result = CliRunner().invoke(
            status_cmd, ["--db-path", str(db_path)], terminal_width=200
        )
        assert result.exit_code == 0, result.output
        assert "TASK-" not in result.output
        assert "—" in result.output

    def test_a_stale_heartbeat_renders_stale_in_the_table(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        build_id = self._running_build(persistence)
        self._seed_heartbeat(tmp_path, build_id, age_seconds=3600)
        result = CliRunner().invoke(
            status_cmd, ["--db-path", str(db_path)], terminal_width=200
        )
        assert result.exit_code == 0, result.output
        assert "stale" in result.output

    def test_a_terminal_row_never_consults_the_file(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        """A stray heartbeat must not resurrect a finished build's stage."""
        build_id = _seed_build(
            persistence,
            feature_id="FEAT-DONE",
            correlation_id="corr-done",
            target_state=BuildState.COMPLETE,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        self._seed_heartbeat(tmp_path, build_id)
        result = CliRunner().invoke(
            status_cmd, ["--db-path", str(db_path)], terminal_width=200
        )
        assert result.exit_code == 0, result.output
        assert "TASK-LIVE-007" not in result.output

    def test_a_completed_stage_log_entry_still_wins_under_full(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        """A recorded stage is a fact; a heartbeat must not overwrite one."""
        build_id = self._running_build(persistence)
        _seed_stage_log(
            persistence,
            build_id=build_id,
            count=1,
            base_time=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        self._seed_heartbeat(tmp_path, build_id)
        result = CliRunner().invoke(
            status_cmd, ["--full", "--db-path", str(db_path)], terminal_width=200
        )
        assert result.exit_code == 0, result.output
        assert "stage-00" in result.output
        assert "TASK-LIVE-007" not in result.output

    # -- --json ------------------------------------------------------------

    def test_json_carries_the_heartbeat_with_an_explicit_stale_flag(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        build_id = self._running_build(persistence)
        self._seed_heartbeat(tmp_path, build_id)
        result = CliRunner().invoke(
            status_cmd, ["--json", "--db-path", str(db_path)]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert rows[0]["in_flight"]["last_task_id"] == "TASK-LIVE-007"
        # An explicit flag, so a machine consumer never has to parse an
        # adjective out of a rendered string.
        assert rows[0]["in_flight"]["stale"] is False
        # ``in_flight`` sits BESIDE the model's own fields, exactly as
        # ``--full``'s ``stages`` key already does: strip it and the row is
        # still a whole BuildStatusView, unchanged in every field.
        row = {k: v for k, v in rows[0].items() if k != "in_flight"}
        assert BuildStatusView.model_validate(row).status is BuildState.RUNNING

    def test_json_is_byte_identical_when_there_is_no_heartbeat(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        """Strictly additive: no heartbeat, no key."""
        self._running_build(persistence)
        result = CliRunner().invoke(
            status_cmd, ["--json", "--db-path", str(db_path)]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert "in_flight" not in rows[0]

    def test_json_never_attaches_a_heartbeat_to_a_terminal_row(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        build_id = _seed_build(
            persistence,
            feature_id="FEAT-DONE-JSON",
            correlation_id="corr-done-json",
            target_state=BuildState.FAILED,
            queued_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
        )
        self._seed_heartbeat(tmp_path, build_id)
        result = CliRunner().invoke(
            status_cmd, ["--json", "--db-path", str(db_path)]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert "in_flight" not in rows[0]

    def test_the_staleness_fence_follows_the_configured_poll_cadence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who slows the monitor must not get spurious "stale"."""
        from forge.cli.status import _in_flight_is_stale

        payload = {
            "updated_at": (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        }
        monkeypatch.setenv("FORGE_BUILD_MONITOR_POLL_SECONDS", "60")
        assert _in_flight_is_stale(payload) is True
        monkeypatch.setenv("FORGE_BUILD_MONITOR_POLL_SECONDS", "600")
        assert _in_flight_is_stale(payload) is False
