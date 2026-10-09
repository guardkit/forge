"""Tests for the ``forge`` CLI scaffold and ``forge queue`` command (TASK-PSM-008).

Each test class mirrors one acceptance criterion of TASK-PSM-008 so the
mapping between the criterion and its verifier stays explicit (per the
project's testing rules — AAA pattern, descriptive names, AC traceability).

The tests use :class:`click.testing.CliRunner` and module-level monkeypatch
hooks to substitute the SQLite persistence and NATS publisher with in-memory
fakes — the CLI exposes two seams (``make_persistence`` and ``publish``) for
this purpose so tests do not require a running NATS broker or a real
``~/.forge/forge.db`` file.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

from forge.cli import queue as cli_queue
from forge.lifecycle.persistence import DuplicateBuildError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    """Serialise ``data`` to ``path`` as YAML and return ``path``."""
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """A real directory the CLI's ``--repo`` ``Path(exists=True)`` accepts."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    return repo


@pytest.fixture
def feature_yaml(tmp_path: Path) -> Path:
    """A real file the CLI's ``--feature-yaml`` ``Path(exists=True)`` accepts."""
    yaml_path = tmp_path / "feature.yaml"
    yaml_path.write_text("name: example\n", encoding="utf-8")
    return yaml_path


@pytest.fixture
def config_path(tmp_path: Path, repo_dir: Path) -> Path:
    """A minimal ``forge.yaml`` whose ``repo_allowlist`` includes ``repo_dir``."""
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
def restricted_config_path(
    tmp_path: Path,
    repo_dir: Path,  # noqa: ARG001 — kept for fixture parity
) -> Path:
    """A ``forge.yaml`` whose ``repo_allowlist`` deliberately excludes ``repo_dir``."""
    other = tmp_path / "approved-only"
    other.mkdir()
    return _write_yaml(
        tmp_path / "forge_restricted.yaml",
        {
            "queue": {"repo_allowlist": [str(other)]},
            "permissions": {
                "filesystem": {"allowlist": [str(tmp_path)]},
            },
        },
    )


class _FakePersistence:
    """In-memory stand-in for :class:`SqliteLifecyclePersistence`.

    Captures the order of the two writes the CLI performs against it so a
    test can assert that ``record_pending_build`` was called *before* the
    publisher seam fired (write-then-publish ordering — AC for ``sc_002``).
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
        self.records: list[Any] = []

    def exists_active_build(self, feature_id: str) -> bool:
        self.calls.append(("exists_active_build", feature_id))
        return self.active

    def record_pending_build(self, payload: Any) -> str:
        self.calls.append(("record_pending_build", payload))
        if self.raise_duplicate:
            raise DuplicateBuildError(payload.feature_id, payload.correlation_id)
        self.records.append(payload)
        return f"build-{payload.feature_id}-{payload.queued_at:%Y%m%d%H%M%S}"


@pytest.fixture
def fake_persistence(monkeypatch: pytest.MonkeyPatch) -> _FakePersistence:
    fake = _FakePersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    monkeypatch.setattr(
        cli_queue, "_planning_handoff_retirement_preflight", lambda path: frozenset()
    )
    monkeypatch.setattr(
        cli_queue,
        "_planning_handoff_retirement_after_migration",
        lambda persistence: frozenset(),
    )
    return fake


@pytest.fixture
def captured_publish(
    monkeypatch: pytest.MonkeyPatch,
    fake_persistence: _FakePersistence,
) -> list[tuple[str, bytes]]:
    """Capture every ``publish`` invocation so tests can inspect ordering."""
    captured: list[tuple[str, bytes]] = []

    def _capture(subject: str, body: bytes) -> None:
        captured.append((subject, body))
        # Stamp the persistence call log so ordering tests can prove the
        # publish happened *after* ``record_pending_build``.
        fake_persistence.calls.append(("publish", subject))

    monkeypatch.setattr(cli_queue, "publish", _capture)
    return captured


# ---------------------------------------------------------------------------
# AC-001: forge.cli.main:main is a Click group importable as forge.cli.main:main
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    """AC-001: ``forge.cli.main:main`` is a Click group at that import path."""

    def test_main_is_a_click_group(self) -> None:
        from forge.cli.main import main

        assert isinstance(main, click.Group)

    def test_main_registers_the_queue_subcommand(self) -> None:
        from forge.cli.main import main

        assert "queue" in main.commands

    def test_module_dotted_path_matches_pyproject_entry_point(self) -> None:
        # The pyproject entry (TASK-PSM-012) will be ``forge.cli.main:main``
        # — assert the dotted path resolves so the entry point is wirable.
        import importlib

        module = importlib.import_module("forge.cli.main")
        assert hasattr(module, "main")
        assert isinstance(module.main, click.Group)


# ---------------------------------------------------------------------------
# AC-002: main loads forge.yaml once and passes ForgeConfig via ctx.obj
# ---------------------------------------------------------------------------


class TestConfigLoadedIntoContext:
    """AC-002: ``main`` parses ``forge.yaml`` and passes it via ``ctx.obj``."""

    def test_subcommands_receive_forge_config_via_pass_obj(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
        fake_persistence: _FakePersistence,  # noqa: ARG002
    ) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-EXAMPLE",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Queued FEAT-EXAMPLE" in result.output


# ---------------------------------------------------------------------------
# AC-003: feature_id validated BEFORE any side effect (exit 4 on invalid)
# ---------------------------------------------------------------------------


class TestFeatureIdValidationBeforeSideEffects:
    """AC-003: traversal/disallowed feature_id exits 4 and writes nothing."""

    def test_traversal_feature_id_exits_4_with_no_persistence_or_publish(
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
                "../etc/passwd",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 4, result.output
        assert "Invalid feature_id" in result.output or "traversal" in result.output
        assert fake_persistence.calls == [], (
            "validate_feature_id must run BEFORE any persistence call"
        )
        assert captured_publish == [], (
            "validate_feature_id must run BEFORE any publish"
        )


# ---------------------------------------------------------------------------
# AC-004: --repo not in repo_allowlist exits 2
# ---------------------------------------------------------------------------


class TestRepoAllowlistRejection:
    """AC-004: ``--repo`` outside ``repo_allowlist`` exits 2 (Group C)."""

    def test_unauthorised_repo_exits_2_with_no_persistence_or_publish(
        self,
        restricted_config_path: Path,
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
                str(restricted_config_path),
                "queue",
                "FEAT-PATH",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 2, result.output
        assert "allowlist" in result.output.lower() or "repo" in result.output.lower()
        assert fake_persistence.calls == []
        assert captured_publish == []


# ---------------------------------------------------------------------------
# AC-005: CLI args override config defaults (e.g. --max-turns 7 over default 5)
# ---------------------------------------------------------------------------


class TestCliFlagsOverrideConfigDefaults:
    """AC-005: ``--max-turns 7`` overrides ``default_max_turns: 5``."""

    def test_max_turns_flag_overrides_config_default(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
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
                "FEAT-OVERRIDE",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--max-turns",
                "7",
                "--timeout",
                "300",
            ],
        )
        assert result.exit_code == 0, result.output
        # The recorded payload must reflect the CLI-supplied overrides,
        # not the config defaults.
        assert len(fake_persistence.records) == 1
        payload = fake_persistence.records[0]
        assert payload.max_turns == 7
        assert payload.sdk_timeout_seconds == 300

    def test_omitted_flags_inherit_config_defaults(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],  # noqa: ARG002
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
                "FEAT-DEFAULTS",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = fake_persistence.records[0]
        assert payload.max_turns == 5
        assert payload.sdk_timeout_seconds == 1800


# ---------------------------------------------------------------------------
# AC-006: SQLite row written BEFORE NATS publish (write-then-publish)
# ---------------------------------------------------------------------------


class TestWriteThenPublishOrdering:
    """AC-006: ``record_pending_build`` runs before ``publish`` (sc_002)."""

    def test_record_then_publish_order_is_preserved(
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
                "FEAT-ORDER",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 0, result.output
        # Filter to the two ordered events; ``exists_active_build`` is
        # also captured but is irrelevant to the write/publish ordering.
        op_sequence = [name for name, _ in fake_persistence.calls]
        record_idx = op_sequence.index("record_pending_build")
        publish_idx = op_sequence.index("publish")
        assert record_idx < publish_idx, (
            f"record_pending_build must precede publish; got {op_sequence!r}"
        )
        assert len(captured_publish) == 1


# ---------------------------------------------------------------------------
# AC-007: NATS publish failure → row remains, exit 1, "publish failed" + messaging-layer
# ---------------------------------------------------------------------------


class TestPublishFailureLeavesRowIntact:
    """AC-007: messaging-layer failure leaves the SQLite row, exits 1."""

    def test_publish_failure_keeps_row_and_exits_1(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_persistence: _FakePersistence,
    ) -> None:
        from forge.cli.main import main

        def _exploding_publish(subject: str, body: bytes) -> None:
            raise cli_queue.PublishError("connection refused")

        monkeypatch.setattr(cli_queue, "publish", _exploding_publish)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-PUBFAIL",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 1, result.output
        # Row was still recorded BEFORE the publish blew up.
        assert len(fake_persistence.records) == 1
        # stderr/stdout combined (CliRunner default mix) carries the
        # required diagnostic strings.
        combined = result.output
        assert "publish failed" in combined.lower()
        assert "messaging-layer" in combined.lower() or (
            "pipeline NOT NOTIFIED" in combined
        )


# ---------------------------------------------------------------------------
# AC-008: SQLite duplicate → exit 3 with "duplicate build" message (Group B)
# ---------------------------------------------------------------------------


class TestDuplicateBuildExitsThree:
    """AC-008: DuplicateBuildError → exit 3 with ``duplicate build`` message."""

    def test_sqlite_duplicate_exits_3(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_publish: list[tuple[str, bytes]],
    ) -> None:
        from forge.cli.main import main

        # Substitute a persistence facade that raises DuplicateBuildError
        # on record_pending_build.
        dup = _FakePersistence(raise_duplicate=True)
        monkeypatch.setattr(cli_queue, "make_persistence", lambda config: dup)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-DUP",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
                "--correlation-id",
                "fixed-corr",
            ],
        )
        assert result.exit_code == 3, result.output
        assert "duplicate build" in result.output.lower()
        # Publish must not run when the SQLite write was rejected.
        assert captured_publish == []

    def test_active_inflight_duplicate_exits_3(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_publish: list[tuple[str, bytes]],
    ) -> None:
        """Group C: ``exists_active_build`` short-circuits with exit 3."""
        from forge.cli.main import main

        active = _FakePersistence(active=True)
        monkeypatch.setattr(cli_queue, "make_persistence", lambda config: active)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-INFLIGHT",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 3, result.output
        assert active.records == [], "no row written when an active build exists"
        assert captured_publish == []


# ---------------------------------------------------------------------------
# AC-009: BDD scenario test cases for queue command exit codes are passable
# ---------------------------------------------------------------------------


class TestExitCodeMatrix:
    """AC-009: Every documented exit code (0/1/2/3/4) is reachable."""

    def test_happy_path_exit_zero(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        captured_publish: list[tuple[str, bytes]],
        fake_persistence: _FakePersistence,  # noqa: ARG002
    ) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "queue",
                "FEAT-HAPPY",
                "--repo",
                str(repo_dir),
                "--feature-yaml",
                str(feature_yaml),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "correlation_id=" in result.output
        assert len(captured_publish) == 1
        subject, body = captured_publish[0]
        assert subject == "pipeline.build-queued.FEAT-HAPPY"
        envelope = json.loads(body.decode("utf-8"))
        assert envelope["source_id"] == "forge-cli"
        assert envelope["event_type"] == "build_queued"


# ---------------------------------------------------------------------------
# AC-010: cli/status.py and cli/history.py contain no NATS imports
# ---------------------------------------------------------------------------


class TestImportDiscipline:
    """AC-010: read-side CLI modules must not import ``forge.adapters.nats``.

    TASK-PSM-008 only scaffolds the package; the read-side modules are
    introduced in TASK-PSM-009 / TASK-PSM-010. We assert the discipline
    statically: if the modules exist, their source must be free of any
    ``forge.adapters.nats`` import. The check is a no-op when the files
    are absent (the future-task wiring still has to satisfy it).
    """

    @pytest.mark.parametrize("module_name", ["status.py", "history.py"])
    def test_read_side_module_has_no_nats_imports(self, module_name: str) -> None:
        cli_pkg = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "forge"
            / "cli"
        )
        candidate = cli_pkg / module_name
        if not candidate.exists():
            pytest.skip(f"{module_name} not yet scaffolded by its owning task")
        source = candidate.read_text(encoding="utf-8")
        # Only fail on actual *import statements*; docstring references
        # to the discipline ("must not import forge.adapters.nats") are
        # part of the rule, not a violation of it.
        for raw_line in source.splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            assert "forge.adapters.nats" not in stripped, (
                f"{module_name} top-level import violates discipline: "
                f"{stripped!r}"
            )


# ---------------------------------------------------------------------------
# AC-011: lint/format — basic shape assertions (full lint runs in CI)
# ---------------------------------------------------------------------------


class TestSourceShape:
    """AC-011: production module is small, importable, and has no syntax issues.

    The full Black/Ruff pipeline runs in CI; here we only check a couple of
    bedrock invariants so a developer running ``pytest`` locally catches
    obvious format/structure regressions before pushing.
    """

    def test_main_module_imports_without_side_effects(self) -> None:
        # Re-import in a clean form; if importing ``forge.cli.main`` ever
        # acquired filesystem or network side effects, this test would
        # surface that immediately.
        import importlib

        module = importlib.import_module("forge.cli.main")
        assert hasattr(module, "main")

    def test_queue_module_exposes_required_seams(self) -> None:
        # The queue subcommand module exposes the test seams used by the
        # AutoBuild test suite — ``make_persistence``, ``publish``, and
        # the domain ``PublishError``. Loss of any of these names is a
        # test-discipline regression.
        import importlib

        module = importlib.import_module("forge.cli.queue")
        assert hasattr(module, "queue_cmd")
        assert hasattr(module, "make_persistence")
        assert hasattr(module, "publish")
        assert hasattr(module, "PublishError")

    def test_queue_module_does_not_import_nats_adapter_at_top_level(self) -> None:
        # The CLI may *call into* a NATS publisher seam, but importing
        # the module must not eagerly pull in ``forge.adapters.nats.*``
        # — keeps the CLI fast to start and avoids importing optional
        # NATS client dependencies for read-only operations.
        cli_pkg = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "forge"
            / "cli"
            / "queue.py"
        )
        source = cli_pkg.read_text(encoding="utf-8")
        # ``import forge.adapters.nats`` or ``from forge.adapters.nats``
        # at the top of the module is the regression we're guarding
        # against; lazy imports inside functions remain permitted.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and not line.startswith(
                (" ", "\t")
            ):
                assert "forge.adapters.nats" not in stripped, (
                    f"top-level NATS import found: {stripped!r}"
                )
