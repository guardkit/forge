"""Tests for ``forge queue --mode {a|b|c}`` CLI surface (TASK-MBC8-009).

Acceptance criteria coverage map (mirrors ``TASK-MBC8-009-cli-mode-flag.md``):

* AC-001 :class:`TestQueueModeFlag` — ``forge queue`` accepts ``--mode {a|b|c}``
  with default ``a`` and persists the mode via
  :meth:`SqliteLifecyclePersistence.queue_build`.
* AC-002 :class:`TestModeBSingleFeature` — ``--mode b`` requires exactly one
  feature identifier (ASSUM-006).
* AC-003 :class:`TestModeCSubject` — ``--mode c`` accepts a subject identifier
  and persists it via the existing feature-id column.
* AC-004 :class:`TestStatusDisplaysMode` — ``forge status`` displays the mode
  column with legacy rows defaulting to ``mode-a``.
* AC-005 :class:`TestHistoryFiltersByMode` — ``forge history --mode b`` and
  ``--mode c`` filter the history view.
* AC-006 :class:`TestPickNextPendingFifoRegardlessOfMode` — the queue picker
  returns builds in FIFO order regardless of mode.
* AC-007 :class:`TestConstitutionalGateNotBypassed` — constitutional gate
  cannot be bypassed by mode flag.
* AC-008 :class:`TestModeHelpTextReferencesChainShapes` — help text references
  the FEAT-FORGE-008 chain shapes verbatim.

Tests use ``click.testing.CliRunner`` and module-level monkeypatch hooks that
mirror :mod:`tests.forge.test_cli_main` — substituting the SQLite persistence
and NATS publisher with in-memory fakes so the suite runs without a NATS broker
or a real ``~/.forge/forge.db`` file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import history as cli_history
from forge.cli import queue as cli_queue
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    DuplicateBuildError,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.state_machine import BuildState, transition
from forge.pipeline.cli_steering import BuildLifecycle, SkipStatus
from forge.pipeline.constitutional_guard import ConstitutionalGuard, SkipVerdict
from forge.pipeline.stage_taxonomy import CONSTITUTIONAL_STAGES, StageClass


# ---------------------------------------------------------------------------
# Fixtures shared across this module
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    return repo


@pytest.fixture
def feature_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "feature.yaml"
    yaml_path.write_text("name: example\n", encoding="utf-8")
    return yaml_path


@pytest.fixture
def fix_task_yaml(tmp_path: Path) -> Path:
    """Fix-task YAML carrying a ``parent_feature`` pointer (TASK-F8-002).

    Mode C dispatches read this field to derive the parent ``feature_id``
    that rides on the wire alongside the per-fix-task ``task_id``.
    """
    yaml_path = tmp_path / "fix-task.yaml"
    yaml_path.write_text(
        "name: example-fix\nparent_feature: FEAT-FIX007\n",
        encoding="utf-8",
    )
    return yaml_path


@pytest.fixture
def config_path(tmp_path: Path, repo_dir: Path) -> Path:
    return _write_yaml(
        tmp_path / "forge.yaml",
        {
            "queue": {
                "default_max_turns": 5,
                "default_sdk_timeout_seconds": 1800,
                "default_history_limit": 50,
                "repo_allowlist": [str(repo_dir)],
            },
            "permissions": {
                "filesystem": {"allowlist": [str(tmp_path)]},
            },
        },
    )


@pytest.fixture
def conductor_config_path(tmp_path: Path, repo_dir: Path) -> Path:
    """``config_path`` with the conductor switched ON *and* a capped default.

    The fix journey (``--mode c``) is refused at queue time while the
    conductor is off (revival design pass §h.8) — a build nothing drives
    must never become a queue row. Since the stage-2 cap law (§4) it is
    refused a second way: a fix journey whose resolved budget profile
    carries no review-cycle cap does not open either, so the default
    profile here is the capped ``fix-journey`` one rather than the
    reserved (uncapped) ``attended``.

    The Mode C *identifier* tests below are about what rides on the wire,
    not about either gate, so they run against a config that clears both;
    the refusals themselves are covered in
    ``tests/forge/test_cli_queue_mode_refusal.py`` and
    ``tests/forge/test_mode_c_cap_law.py``.
    """
    return _write_yaml(
        tmp_path / "forge-conductor.yaml",
        {
            "queue": {
                "default_max_turns": 5,
                "default_sdk_timeout_seconds": 1800,
                "default_history_limit": 50,
                "repo_allowlist": [str(repo_dir)],
            },
            "conductor": {"enabled": True},
            "budget": {"default_profile": "fix-journey"},
            "permissions": {
                "filesystem": {"allowlist": [str(tmp_path)]},
            },
        },
    )


class _FakePersistence:
    """In-memory persistence stand-in that records ``queue_build`` calls.

    Mirrors :class:`tests.forge.test_cli_main._FakePersistence` but
    surfaces the explicit ``mode=`` kwarg so the AC tests can verify
    that the CLI plumbed the right :class:`BuildMode` through.
    """

    def __init__(
        self,
        *,
        active: bool = False,
        raise_duplicate: bool = False,
    ) -> None:
        self.active = active
        self.raise_duplicate = raise_duplicate
        self.calls: list[tuple[str, Any]] = []
        self.records: list[tuple[Any, BuildMode | None]] = []
        self.queue_build_kwargs: list[dict[str, Any]] = []

    def exists_active_build(self, feature_id: str) -> bool:
        self.calls.append(("exists_active_build", feature_id))
        return self.active

    def queue_build(
        self,
        payload: Any,
        *,
        mode: BuildMode | str | None = None,
        profile: str | None = None,
    ) -> str:
        # ``profile`` mirrors the production signature
        # (``SqliteLifecyclePersistence.queue_build``), which grew the
        # kwarg with the budget-guard integration. The double had drifted
        # behind it, so every CLI test that reached persistence died on a
        # TypeError rather than on its own assertion — a pre-existing
        # red unrelated to the mode surface. Kept in step here.
        self.calls.append(("queue_build", payload))
        self.queue_build_kwargs.append({"mode": mode, "profile": profile})
        if self.raise_duplicate:
            raise DuplicateBuildError(payload.feature_id, payload.correlation_id)
        resolved = (
            mode
            if isinstance(mode, BuildMode)
            else BuildMode(mode) if mode else BuildMode.MODE_A
        )
        self.records.append((payload, resolved))
        return f"build-{payload.feature_id}-{payload.queued_at:%Y%m%d%H%M%S}"

    # The CLI prefers ``queue_build`` but keeps a fallback path through
    # ``record_pending_build`` for older fakes — keep parity here so a
    # regression of the CLI's ``hasattr`` check would still surface.
    def record_pending_build(self, payload: Any) -> str:
        self.calls.append(("record_pending_build", payload))
        if self.raise_duplicate:
            raise DuplicateBuildError(payload.feature_id, payload.correlation_id)
        sniffed = getattr(payload, "mode", None)
        resolved = (
            BuildMode(sniffed) if sniffed else BuildMode.MODE_A
        )
        self.records.append((payload, resolved))
        return f"build-{payload.feature_id}-{payload.queued_at:%Y%m%d%H%M%S}"


@pytest.fixture
def fake_persistence(monkeypatch: pytest.MonkeyPatch) -> _FakePersistence:
    fake = _FakePersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    return fake


@pytest.fixture
def captured_publish(
    monkeypatch: pytest.MonkeyPatch,
    fake_persistence: _FakePersistence,
) -> list[tuple[str, bytes]]:
    captured: list[tuple[str, bytes]] = []

    def _capture(subject: str, body: bytes) -> None:
        captured.append((subject, body))
        fake_persistence.calls.append(("publish", subject))

    monkeypatch.setattr(cli_queue, "publish", _capture)
    return captured


# ---------------------------------------------------------------------------
# AC-001: forge queue --mode {a|b|c} default a
# ---------------------------------------------------------------------------


class TestQueueModeFlag:
    """``forge queue`` accepts ``--mode {a|b|c}`` and persists the mode."""

    @pytest.mark.parametrize(
        "flag,expected",
        [
            ("a", BuildMode.MODE_A),
            # Case-insensitive — Click's case_sensitive=False on the choice.
            ("A", BuildMode.MODE_A),
            # ``b``/``B`` used to live here. The full journey (Mode B) was
            # retired as a production destination on 2026-07-31 — it is
            # superseded by the spec-writer chain — so it can no longer
            # reach a persisted row at all. Its refusal is asserted in
            # ``TestModeBRetired`` below and in
            # ``tests/forge/test_cli_queue_mode_refusal.py``.
        ],
    )
    def test_explicit_mode_flag_maps_to_build_mode(
        self,
        flag: str,
        expected: BuildMode,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
        fake_persistence: _FakePersistence,
    ) -> None:
        # Mode C requires a TASK-XXX positional + parent_feature YAML
        # (TASK-F8-002), so the c/C flag cases live in
        # ``TestModeCSubject`` below rather than this parametrize.
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-MODE",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--mode",
                flag,
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(fake_persistence.records) == 1
        payload, persisted_mode = fake_persistence.records[0]
        assert persisted_mode is expected
        assert f"mode={expected.value}" in result.output
        # TASK-F8-002 — Mode A/B publishers must NOT carry a task_id.
        assert payload.task_id is None
        assert payload.mode == expected.value

    def test_mode_flag_defaults_to_a_for_backwards_compatibility(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
        fake_persistence: _FakePersistence,
    ) -> None:
        """Omitting ``--mode`` defaults to Mode A so existing callers
        (test fixtures, scripts) keep working unchanged (AC text:
        "default `a` for backwards compatibility")."""
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-DEFAULT",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 0, result.output
        _, persisted_mode = fake_persistence.records[0]
        assert persisted_mode is BuildMode.MODE_A

    def test_unknown_mode_value_is_rejected_at_parse_time(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-BAD",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--mode",
                "z",
            ],
        )
        # Click's BadParameter exits 2 by default.
        assert result.exit_code != 0
        # Parse-time refusal: no persistence or publish side effects.
        assert fake_persistence.calls == []
        assert captured_publish == []


# ---------------------------------------------------------------------------
# AC-002: --mode b requires exactly one feature identifier (ASSUM-006)
# ---------------------------------------------------------------------------


class TestModeBSingleFeature:
    """``forge queue --mode b <FEAT-ID>`` requires exactly one feature."""

    def test_mode_b_with_one_feature_is_refused_as_retired(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        """The well-formed Mode B invocation is refused, not queued.

        This test used to assert a successful enqueue. Rich's 2026-07-31
        ruling retired the full journey as a production destination — no
        driver exists for it and none is planned — so a well-formed
        ``--mode b`` is now the *worst* case, not the happy one: it would
        write a row nothing ever picks up. It must exit nonzero having
        written nothing.
        """
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-MBC8B01",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--mode",
                "b",
            ],
        )
        assert result.exit_code != 0
        assert fake_persistence.records == []
        assert fake_persistence.calls == []
        assert captured_publish == []
        assert "spec-writer chain" in result.output

    def test_mode_b_with_two_features_is_rejected_at_parse_time(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        """ASSUM-006: single feature per Mode B build. The CLI parser
        rejects multi-feature input *before* any persistence call so a
        misuse never produces a partial SQLite write."""
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-AAA111",
                "FEAT-BBB222",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--mode",
                "b",
            ],
        )
        assert result.exit_code != 0
        # No side effects occurred.
        assert fake_persistence.calls == []
        assert captured_publish == []
        # Diagnostic mentions ASSUM-006 / mode-b discipline so the
        # operator immediately understands the refusal reason.
        combined = result.output.lower()
        assert "mode b" in combined or "--mode b" in combined or "assum-006" in combined


# ---------------------------------------------------------------------------
# AC-003: --mode c <SUBJECT-ID> persisted via feature-id column
# ---------------------------------------------------------------------------


class TestModeCSubject:
    """``forge queue --mode c`` carries TASK-XXX + parent FEAT- on the wire.

    TASK-F8-002 / F008-VAL-002 — Mode C is the only build shape where
    the fix-task is the dispatch target. The CLI now reads the parent
    ``feature_id`` from the fix-task YAML's ``parent_feature`` field
    rather than reusing the ``feature_id`` column, so the wire payload
    carries both identifiers in their canonical slots.
    """

    @pytest.mark.parametrize("flag", ["c", "C"])
    def test_mode_c_populates_task_id_and_parent_feature_on_payload(
        self,
        flag: str,
        conductor_config_path: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
        fake_persistence: _FakePersistence,
    ) -> None:
        """Mode C: positional TASK-XXX + parent_feature: FEAT-XXX → both fields populated."""
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(conductor_config_path),
                "queue",
                "TASK-FIX007",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(fix_task_yaml),
                "--mode",
                flag,
            ],
        )
        assert result.exit_code == 0, result.output
        payload, persisted_mode = fake_persistence.records[0]
        assert persisted_mode is BuildMode.MODE_C
        # Parent feature_id resolved from the fix-task YAML.
        assert payload.feature_id == "FEAT-FIX007"
        # task_id round-trips the positional argument.
        assert payload.task_id == "TASK-FIX007"
        # mode is now a first-class wire field.
        assert payload.mode == "mode-c"

    def test_mode_c_rejects_non_task_positional_at_cli_boundary(
        self,
        conductor_config_path: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        """Mode C with a FEAT-shaped positional fails before any side effect."""
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(conductor_config_path),
                "queue",
                "FEAT-FIX007",  # wrong shape for Mode C
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(fix_task_yaml),
                "--mode",
                "c",
            ],
        )
        assert result.exit_code == cli_queue.EXIT_INVALID_IDENTIFIER
        assert "Mode C requires positional argument to match" in result.output
        # No persistence or publish side effects.
        assert fake_persistence.calls == []
        assert captured_publish == []

    def test_mode_c_requires_parent_feature_in_fix_task_yaml(
        self,
        conductor_config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,  # the bare YAML without parent_feature
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        """Missing ``parent_feature`` field surfaces a UsageError."""
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(conductor_config_path),
                "queue",
                "TASK-FIX007",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),  # has no parent_feature key
                "--mode",
                "c",
            ],
        )
        assert result.exit_code != 0
        # Click's UsageError surfaces the canonical message.
        assert "parent_feature" in result.output
        # No persistence or publish side effects.
        assert fake_persistence.calls == []
        assert captured_publish == []

    def test_mode_c_rejects_non_feat_parent_in_fix_task_yaml(
        self,
        conductor_config_path: Path,
        repo_dir: Path,
        tmp_path: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,
    ) -> None:
        """parent_feature with traversal characters fails the security pipeline."""
        bad_yaml = tmp_path / "bad-fix-task.yaml"
        bad_yaml.write_text(
            "parent_feature: ../etc/passwd\n", encoding="utf-8"
        )
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(conductor_config_path),
                "queue",
                "TASK-FIX007",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(bad_yaml),
                "--mode",
                "c",
            ],
        )
        assert result.exit_code == cli_queue.EXIT_INVALID_IDENTIFIER
        assert "Invalid parent_feature" in result.output
        assert fake_persistence.calls == []
        assert captured_publish == []


# ---------------------------------------------------------------------------
# AC-004: forge status displays the mode column
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str,
    correlation_id: str,
    queued_at: datetime,
) -> SimpleNamespace:
    """Build a duck-typed BuildQueuedPayload accepted by queue_build."""
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path=f"features/{feature_id}/feature.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=queued_at,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Return a freshly-migrated forge.db path (v1 + v2 schema)."""
    path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.close()
    return path


@pytest.fixture()
def writer(db_path: Path):
    cx = sqlite_connect.connect_writer(db_path)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer: sqlite3.Connection, db_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer, db_path=db_path)


class TestStatusDisplaysMode:
    """``forge status`` surfaces the mode column."""

    def test_status_renders_mode_a_for_legacy_row_default(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        """Legacy rows that pre-date FEAT-FORGE-008 backfill to
        ``mode-a`` per the additive migration default in
        ``schema_v2.sql``."""
        from forge.cli.status import status_cmd

        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        persistence.queue_build(
            _make_payload(
                feature_id="FEAT-LEGACY",
                correlation_id="corr-legacy",
                queued_at=base,
            ),
        )

        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "FEAT-LEGACY" in result.output
        assert "mode-a" in result.output

    def test_status_renders_mode_b_and_mode_c_concurrent_builds(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        """Group F: operator can disambiguate concurrent builds."""
        from forge.cli.status import status_cmd

        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        persistence.queue_build(
            _make_payload(
                feature_id="FEAT-MB",
                correlation_id="corr-mb",
                queued_at=base,
            ),
            mode=BuildMode.MODE_B,
        )
        persistence.queue_build(
            _make_payload(
                feature_id="FEAT-MC001",
                correlation_id="corr-mc",
                queued_at=base + timedelta(minutes=1),
            ),
            mode=BuildMode.MODE_C,
        )

        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "mode-b" in result.output
        assert "mode-c" in result.output


# ---------------------------------------------------------------------------
# AC-005: forge history --mode b/c filters by mode
# ---------------------------------------------------------------------------


class TestHistoryFiltersByMode:
    """``forge history --mode {b|c}`` filters the history view by mode."""

    def _seed_one_per_mode(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        for index, (mode, feat) in enumerate(
            [
                (BuildMode.MODE_A, "FEAT-MODE-A"),
                (BuildMode.MODE_B, "FEAT-MODE-B"),
                (BuildMode.MODE_C, "FEAT-MODE-C"),
            ]
        ):
            persistence.queue_build(
                _make_payload(
                    feature_id=feat,
                    correlation_id=f"corr-{index}",
                    queued_at=base + timedelta(minutes=index),
                ),
                mode=mode,
            )

    def test_history_default_returns_every_mode(
        self,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        self._seed_one_per_mode(persistence)
        rendered = cli_history.run_history(
            db_path=db_path,
            config=None,
            feature_id=None,
            limit=None,
            since=None,
            output_format="json",
            mode=None,
        )
        import json as _json

        rows = _json.loads(rendered)
        assert {row["feature_id"] for row in rows} == {
            "FEAT-MODE-A",
            "FEAT-MODE-B",
            "FEAT-MODE-C",
        }

    @pytest.mark.parametrize(
        "flag,expected_feature",
        [
            ("b", "FEAT-MODE-B"),
            ("c", "FEAT-MODE-C"),
        ],
    )
    def test_history_mode_filter_returns_only_that_mode(
        self,
        flag: str,
        expected_feature: str,
        persistence: SqliteLifecyclePersistence,
        db_path: Path,
    ) -> None:
        self._seed_one_per_mode(persistence)
        rendered = cli_history.run_history(
            db_path=db_path,
            config=None,
            feature_id=None,
            limit=None,
            since=None,
            output_format="json",
            mode=flag,
        )
        import json as _json

        rows = _json.loads(rendered)
        assert len(rows) == 1
        assert rows[0]["feature_id"] == expected_feature
        assert rows[0]["mode"] == f"mode-{flag}"


# ---------------------------------------------------------------------------
# AC-006: queue picker FIFO regardless of mode
# ---------------------------------------------------------------------------


class TestPickNextPendingFifoRegardlessOfMode:
    """``SqliteLifecyclePersistence.pick_next_pending`` is FIFO across modes.

    Every queued build is its own lifecycle (ASSUM-016) — a Mode A build
    queued at ``T0`` is picked before a Mode B build queued at ``T1``
    even though the Mode B chain is shorter. There must be no
    mode-based priority anywhere in the picker.
    """

    def test_fifo_order_preserved_across_modes(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        # Queue Mode B first, then Mode A, then Mode C — verifies the
        # picker returns oldest queued_at regardless of mode.
        first_mb = persistence.queue_build(
            _make_payload(
                feature_id="FEAT-FIFO-001",
                correlation_id="corr-001",
                queued_at=base,
            ),
            mode=BuildMode.MODE_B,
        )
        persistence.queue_build(
            _make_payload(
                feature_id="FEAT-FIFO-002",
                correlation_id="corr-002",
                queued_at=base + timedelta(minutes=1),
            ),
            mode=BuildMode.MODE_A,
        )
        persistence.queue_build(
            _make_payload(
                feature_id="FEAT-FIFO-003",
                correlation_id="corr-003",
                queued_at=base + timedelta(minutes=2),
            ),
            mode=BuildMode.MODE_C,
        )

        # Fleet-wide picker (project=None) — should return the oldest
        # build regardless of mode.
        picked = persistence.pick_next_pending(project=None)
        assert picked is not None
        assert picked.build_id == first_mb
        assert picked.mode is BuildMode.MODE_B  # the FIFO winner happens to be B

    def test_pick_next_pending_returns_none_when_no_queued_builds(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert persistence.pick_next_pending(project=None) is None


# ---------------------------------------------------------------------------
# AC-007: constitutional gate cannot be bypassed by mode flag
# ---------------------------------------------------------------------------


class TestConstitutionalGateNotBypassed:
    """The constitutional guard refuses skip on PR review in EVERY mode.

    Group E "skip refused at PR review": no operator-supplied flag —
    including the new ``--mode`` flag — can bypass ADR-ARCH-026's
    constitutional veto on :attr:`StageClass.PULL_REQUEST_REVIEW`.
    """

    def test_pull_request_review_is_constitutional(self) -> None:
        # Sanity: PR review is in the constitutional set, otherwise the
        # rest of this scenario is meaningless.
        assert StageClass.PULL_REQUEST_REVIEW in CONSTITUTIONAL_STAGES

    @pytest.mark.parametrize(
        "mode",
        [BuildMode.MODE_A, BuildMode.MODE_B, BuildMode.MODE_C],
    )
    def test_skip_refused_at_pull_request_review_in_every_mode(
        self,
        mode: BuildMode,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """The ConstitutionalGuard ignores mode entirely — its veto is
        keyed off ``stage`` alone (see ConstitutionalGuard.veto_skip)."""
        # Seed a build in this mode just to mirror the production path
        # the supervisor takes when reaching PR review.
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        persistence.queue_build(
            _make_payload(
                feature_id=f"FEAT-CG-{mode.value}",
                correlation_id=f"corr-{mode.value}",
                queued_at=base,
            ),
            mode=mode,
        )

        guard = ConstitutionalGuard()
        decision = guard.veto_skip(StageClass.PULL_REQUEST_REVIEW)
        assert decision.verdict is SkipVerdict.REFUSED_CONSTITUTIONAL
        # The rationale cites ADR-ARCH-026 belt-and-braces verbatim.
        assert "ADR-ARCH-026" in decision.rationale
        assert "constitutional" in decision.rationale.lower()


# ---------------------------------------------------------------------------
# AC-008: help text references FEAT-FORGE-008 chain shapes
# ---------------------------------------------------------------------------


class TestModeHelpTextReferencesChainShapes:
    """``forge queue --help`` references FEAT-FORGE-008 chain shapes verbatim.

    Operators must not need to read source code to choose a mode — the
    chain shapes from ASSUM-001 / ASSUM-004 are reproduced literally in
    the help text.
    """

    def test_help_text_lists_three_modes_with_chain_descriptions(self) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(main, ["queue", "--help"])
        assert result.exit_code == 0, result.output
        # Click wraps long help text and may insert line breaks inside
        # hyphen-bearing tokens (``/feature-\nspec``). Normalise the
        # output before substring checks so the assertion is robust to
        # terminal width.
        normalised = " ".join(result.output.split())
        normalised = normalised.replace("- ", "-")  # un-break wrapped hyphens

        # FEAT-FORGE-008 is named explicitly so operators can grep.
        assert "FEAT-FORGE-008" in normalised
        # Mode A chain shape — the eight stages (or at minimum the
        # endpoints product-owner / pull-request-review).
        assert "product-owner" in normalised
        assert "pull-request-review" in normalised
        # Mode B chain shape — starts at /feature-spec, skips system-arch.
        assert "/feature-spec" in normalised
        # Mode C chain shape — pairs /task-review with /task-work.
        assert "/task-review" in normalised
        assert "/task-work" in normalised

    def test_resolve_mode_helper_round_trips_short_and_long_forms(self) -> None:
        # The helper is reused by both the queue and history surfaces;
        # round-trip exercise here so both surfaces inherit consistent
        # behaviour.
        assert cli_queue.resolve_mode("a") is BuildMode.MODE_A
        assert cli_queue.resolve_mode("B") is BuildMode.MODE_B
        assert cli_queue.resolve_mode("mode-c") is BuildMode.MODE_C
        # History's resolve_mode_flag accepts None for "no filter".
        assert cli_history.resolve_mode_flag(None) is None
        assert cli_history.resolve_mode_flag("") is None
        assert cli_history.resolve_mode_flag("a") is BuildMode.MODE_A
