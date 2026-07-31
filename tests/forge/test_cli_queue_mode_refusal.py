"""``forge queue`` refuses modes nothing will drive (revival Stage 1a).

Design pass risk h.8, "supersession half-retirement":

    ``forge queue --mode b`` (and ``--mode c``, pre-flag) queues a build no
    production driver will ever drive — a silently-stuck row. Stage 1:
    queue-time refusal for unactivated modes, with a plain-language
    message.

The defect this kills is not a crash; it is a *lie*. The CLI printed
"Queued FEAT-X (build pending)", exited zero, and left a PENDING row that
nothing on the estate would ever pick up. Nobody finds that out until
somebody reads the queue by hand.

So the assertion that matters most in this module is the negative one:
**no build row was written and nothing was published**, on every refusal
path. Exit code and message are the operator's side of it; the empty
persistence log is the defect's side.

Zero broker contact: the publisher seam is monkey-patched, and a refused
attempt must not reach it anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import queue as cli_queue
from forge.config.conductor import CONDUCTOR_FLAG_PATH
from forge.lifecycle.modes import BuildMode


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingPersistence:
    """Records every call so a refusal can be proved to have made none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows: list[tuple[Any, BuildMode | str | None]] = []

    def exists_active_build(self, feature_id: str) -> bool:
        self.calls.append(("exists_active_build", feature_id))
        return False

    def queue_build(
        self,
        payload: Any,
        *,
        mode: BuildMode | str | None = None,
        profile: str | None = None,
    ) -> str:
        self.calls.append(("queue_build", payload))
        self.rows.append((payload, mode))
        return "build-1"

    def record_pending_build(self, payload: Any) -> str:
        self.calls.append(("record_pending_build", payload))
        self.rows.append((payload, getattr(payload, "mode", None)))
        return "build-1"


@pytest.fixture
def persistence(monkeypatch: pytest.MonkeyPatch) -> _RecordingPersistence:
    fake = _RecordingPersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    return fake


@pytest.fixture
def published(
    monkeypatch: pytest.MonkeyPatch, persistence: _RecordingPersistence
) -> list[tuple[str, bytes]]:
    captured: list[tuple[str, bytes]] = []

    def _capture(subject: str, body: bytes) -> None:
        captured.append((subject, body))

    monkeypatch.setattr(cli_queue, "publish", _capture)
    return captured


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    return repo


@pytest.fixture
def feature_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "feature.yaml"
    path.write_text("name: example\n", encoding="utf-8")
    return path


@pytest.fixture
def fix_task_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "fix-task.yaml"
    path.write_text(
        "name: example-fix\nparent_feature: FEAT-FIX007\n", encoding="utf-8"
    )
    return path


def _write_config(
    tmp_path: Path, repo_dir: Path, *, conductor: bool | None, name: str
) -> Path:
    body: dict[str, Any] = {
        "queue": {"repo_allowlist": [str(repo_dir)]},
        "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
    }
    if conductor is not None:
        body["conductor"] = {"enabled": conductor}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


@pytest.fixture
def config_no_section(tmp_path: Path, repo_dir: Path) -> Path:
    """A forge.yaml that has never heard of the conductor — today's estate."""
    return _write_config(tmp_path, repo_dir, conductor=None, name="forge.yaml")


@pytest.fixture
def config_off(tmp_path: Path, repo_dir: Path) -> Path:
    return _write_config(tmp_path, repo_dir, conductor=False, name="forge-off.yaml")


@pytest.fixture
def config_on(tmp_path: Path, repo_dir: Path) -> Path:
    return _write_config(tmp_path, repo_dir, conductor=True, name="forge-on.yaml")


def _queue(
    config_path: Path,
    *,
    positional: str,
    repo_dir: Path,
    feature_yaml: Path,
    mode: str | None,
    extra: list[str] | None = None,
):
    from forge.cli.main import main

    argv = [
        "--config",
        str(config_path),
        "queue",
        positional,
        "--repo",
        str(repo_dir),
        "--feature-yaml",
        str(feature_yaml),
    ]
    if mode is not None:
        argv += ["--mode", mode]
    argv += extra or []
    return CliRunner().invoke(main, argv)


def _assert_nothing_written(
    persistence: _RecordingPersistence, published: list
) -> None:
    """The whole point of the refusal, in one helper."""
    assert persistence.rows == [], "a refused queue attempt wrote a build row"
    assert persistence.calls == [], "a refused queue attempt touched persistence"
    assert published == [], "a refused queue attempt published to the bus"


# ---------------------------------------------------------------------------
# The retired full journey
# ---------------------------------------------------------------------------


class TestFullJourneyRefused:
    """``--mode b`` is retired — refused always, flag or no flag."""

    @pytest.mark.parametrize("flag", ["b", "B"])
    def test_refused_in_every_spelling(
        self,
        flag: str,
        config_no_section: Path,
        repo_dir: Path,
        feature_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        result = _queue(
            config_no_section,
            positional="FEAT-MODEB1",
            repo_dir=repo_dir,
            feature_yaml=feature_yaml,
            mode=flag,
        )
        assert result.exit_code != 0
        _assert_nothing_written(persistence, published)

    def test_refused_even_with_the_conductor_switched_on(
        self,
        config_on: Path,
        repo_dir: Path,
        feature_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """Retirement is not gating.

        The full journey was superseded by the spec-writer chain, not
        parked pending activation — turning the conductor on must not
        resurrect it.
        """
        result = _queue(
            config_on,
            positional="FEAT-MODEB1",
            repo_dir=repo_dir,
            feature_yaml=feature_yaml,
            mode="b",
        )
        assert result.exit_code != 0
        _assert_nothing_written(persistence, published)

    def test_message_points_at_the_mechanism_that_replaced_it(
        self,
        config_no_section: Path,
        repo_dir: Path,
        feature_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """An operator must leave knowing what to do instead."""
        result = _queue(
            config_no_section,
            positional="FEAT-MODEB1",
            repo_dir=repo_dir,
            feature_yaml=feature_yaml,
            mode="b",
        )
        assert "retired" in result.output
        assert "spec-writer chain" in result.output
        assert "Nothing was queued" in result.output


# ---------------------------------------------------------------------------
# The fix journey, gated on the flag
# ---------------------------------------------------------------------------


class TestFixJourneyGatedOnTheConductor:
    @pytest.mark.parametrize(
        "config_fixture", ["config_no_section", "config_off"]
    )
    def test_refused_while_the_conductor_is_off(
        self,
        config_fixture: str,
        request: pytest.FixtureRequest,
        repo_dir: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """Absent section and explicit false must behave identically."""
        config_path = request.getfixturevalue(config_fixture)
        result = _queue(
            config_path,
            positional="TASK-FIX007",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="c",
        )
        assert result.exit_code != 0
        _assert_nothing_written(persistence, published)

    def test_message_names_the_conductor_and_the_switch(
        self,
        config_off: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        result = _queue(
            config_off,
            positional="TASK-FIX007",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="c",
        )
        assert "conductor" in result.output
        assert CONDUCTOR_FLAG_PATH in result.output
        assert "Nothing was queued" in result.output

    def test_queues_once_the_conductor_is_switched_on(
        self,
        config_on: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """The gate opens — the refusal is a gate, not a removal."""
        result = _queue(
            config_on,
            positional="TASK-FIX007",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="c",
        )
        assert result.exit_code == 0, result.output
        assert len(persistence.rows) == 1
        _, mode = persistence.rows[0]
        assert mode is BuildMode.MODE_C
        assert len(published) == 1

    def test_refusal_beats_a_capped_profile_echo(
        self,
        config_off: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """The refusal fires before *every* side effect, output included.

        Budget-profile resolution runs early and echoes to the terminal;
        a refused build must not narrate a budget it will never spend.
        """
        result = _queue(
            config_off,
            positional="TASK-FIX007",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="c",
            extra=["--profile", "unattended"],
        )
        assert result.exit_code != 0
        assert "budget profile" not in result.output
        _assert_nothing_written(persistence, published)


# ---------------------------------------------------------------------------
# The routine build is untouched
# ---------------------------------------------------------------------------


class TestRoutineBuildUntouched:
    """The lane's prime invariant, at the queue surface."""

    @pytest.mark.parametrize("mode", [None, "a", "A"])
    def test_routine_build_queues_normally(
        self,
        mode: str | None,
        config_no_section: Path,
        repo_dir: Path,
        feature_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        """Including the default (no ``--mode`` at all) — the shipping path."""
        result = _queue(
            config_no_section,
            positional="FEAT-ROUTINE",
            repo_dir=repo_dir,
            feature_yaml=feature_yaml,
            mode=mode,
        )
        assert result.exit_code == 0, result.output
        assert len(persistence.rows) == 1
        _, persisted_mode = persistence.rows[0]
        assert persisted_mode is BuildMode.MODE_A
        assert len(published) == 1

    def test_routine_build_unaffected_by_the_flag_being_on(
        self,
        config_on: Path,
        repo_dir: Path,
        feature_yaml: Path,
        persistence: _RecordingPersistence,
        published: list,
    ) -> None:
        result = _queue(
            config_on,
            positional="FEAT-ROUTINE",
            repo_dir=repo_dir,
            feature_yaml=feature_yaml,
            mode="a",
        )
        assert result.exit_code == 0, result.output
        assert len(persistence.rows) == 1


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestModeHelpSpeaksPlainNames:
    """User surfaces speak human — and say which modes actually work."""

    @pytest.fixture
    def help_text(self) -> str:
        from forge.cli.main import main

        result = CliRunner().invoke(main, ["queue", "--help"])
        assert result.exit_code == 0, result.output
        # Click wraps help text; normalise before substring checks.
        normalised = " ".join(result.output.split())
        return normalised.replace("- ", "-")

    def test_help_leads_with_plain_names(self, help_text: str) -> None:
        assert "the routine build" in help_text
        assert "the fix journey" in help_text
        assert "the full journey" in help_text

    def test_help_says_which_modes_are_refused(self, help_text: str) -> None:
        """Nobody should discover a mode is dead by running it."""
        assert "RETIRED" in help_text
        assert "REFUSED" in help_text

    def test_help_names_the_switch_for_the_fix_journey(
        self, help_text: str
    ) -> None:
        assert CONDUCTOR_FLAG_PATH in help_text

    def test_help_still_carries_the_chain_shapes(self, help_text: str) -> None:
        """Plain names lead; the stage order stays for the curious."""
        for token in (
            "FEAT-FORGE-008",
            "product-owner",
            "pull-request-review",
            "/feature-spec",
            "/task-review",
            "/task-work",
        ):
            assert token in help_text

    def test_help_names_the_merge_ready_checkpoint(
        self, help_text: str
    ) -> None:
        """The delivery primitive, in the words Rich uses for it."""
        assert "merge-ready checkpoint" in help_text
