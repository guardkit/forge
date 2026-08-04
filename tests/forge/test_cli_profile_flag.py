"""Tests for the ``forge queue --profile`` CLI surface (FEAT-UBS-002).

Coverage:

* Unknown profile is rejected before any side effect (UsageError / exit 2).
* A known unattended profile echoes the resolved caps + the enforcement-pending
  NOTE, carries ``profile='unattended'`` to the persistence write, and enqueues
  successfully.
* The attended default is silent (no caps banner) and carries ``profile=None``.
* An attended override against a capped default is now honoured via carriage —
  it persists ``profile='attended'`` and emits no deferral warning.

Mirrors the ``click.testing.CliRunner`` + fake-persistence harness in
``tests.forge.test_cli_mode_flag`` so the suite runs without a NATS broker.

TASK-UBS-002-integration §2(a): ``--profile`` now travels to the daemon on the
``builds.profile`` row, so the skeleton's "not yet plumbed" assertions are
replaced by carriage checks + the narrower "enforcement loop not yet activated"
note (the serve-side wiring now exists; only the Mode-C driver is dormant).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import queue as cli_queue
from forge.cli.main import main
from forge.lifecycle.modes import BuildMode


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
def config_path(tmp_path: Path, repo_dir: Path) -> Path:
    """A forge.yaml whose budget block defines attended + unattended."""
    doc = {
        "queue": {"repo_allowlist": [str(repo_dir)]},
        "budget": {
            "default_profile": "attended",
            "profiles": {
                "attended": {},
                "unattended": {
                    "max_review_cycles": 2,
                    "max_build_wallclock_seconds": 5400,
                },
            },
        },
        "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
    }
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


class _FakePersistence:
    """Minimal in-memory persistence: records queued builds, no NATS/db."""

    def __init__(self) -> None:
        self.records: list[Any] = []
        self.profiles: list[str | None] = []

    def exists_active_build(self, feature_id: str) -> bool:  # noqa: ARG002
        return False

    def queue_build(
        self,
        payload: Any,
        *,
        mode: BuildMode | str | None = None,  # noqa: ARG002
        profile: str | None = None,
    ) -> str:
        self.records.append(payload)
        self.profiles.append(profile)
        return f"build-{payload.feature_id}"


@pytest.fixture
def fake_persistence(monkeypatch: pytest.MonkeyPatch) -> _FakePersistence:
    fake = _FakePersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    monkeypatch.setattr(cli_queue, "publish", lambda subject, body: None)
    return fake


def _invoke(config_path: Path, repo_dir: Path, feature_yaml: Path, *profile_args: str):
    runner = CliRunner()
    return runner.invoke(
        main,
        [
            "--config",
            str(config_path),
            "queue",
            "FEAT-PROF",
            "--repo",
            str(repo_dir),
            "--feature-yaml",
            str(feature_yaml),
            *profile_args,
        ],
    )


class TestUnknownProfileRejected:
    """AC: an unknown --profile fails fast, before persistence/publish."""

    def test_unknown_profile_exit_2(
        self, config_path: Path, repo_dir: Path, feature_yaml: Path
    ) -> None:
        # No fake_persistence fixture: the rejection must happen before any
        # persistence call, so the real make_persistence is never reached.
        result = _invoke(config_path, repo_dir, feature_yaml, "--profile", "ghost")
        assert result.exit_code == 2, result.output
        assert "unknown budget profile" in result.output
        assert "unattended" in result.output  # lists known profiles


class TestKnownProfileEchoesCaps:
    """AC-04: a capped profile echoes its caps + the enforcement-pending NOTE
    and carries the profile name to the persistence write."""

    def test_unattended_echoes_caps_and_note(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        fake_persistence: _FakePersistence,
    ) -> None:
        result = _invoke(config_path, repo_dir, feature_yaml, "--profile", "unattended")
        assert result.exit_code == 0, result.output
        assert "budget profile 'unattended'" in result.output
        assert "max_review_cycles=2" in result.output
        # The profile travels to the daemon and the serve-side wiring now
        # exists; the residual gap surfaced is the (out-of-scope) enforcement
        # loop activation, not carriage or wiring.
        assert "not yet activated" in result.output
        assert len(fake_persistence.records) == 1
        # AC-04 carriage: the selected profile reaches the persistence write.
        assert fake_persistence.profiles == ["unattended"]


class TestAttendedDefaultSilent:
    """AC: the attended default enqueues with no caps banner (ASSUM-010) and
    carries profile=None (→ the daemon applies default_profile)."""

    def test_no_profile_is_silent_and_enqueues(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        fake_persistence: _FakePersistence,
    ) -> None:
        result = _invoke(config_path, repo_dir, feature_yaml)
        assert result.exit_code == 0, result.output
        assert "budget profile" not in result.output
        assert "not yet activated" not in result.output
        assert len(fake_persistence.records) == 1
        # No --profile → NULL carriage → daemon resolves default_profile.
        assert fake_persistence.profiles == [None]


class TestAttendedOverrideAgainstCappedDefault:
    """AC-04: --profile attended against a capped default is now HONOURED via
    carriage — it persists profile='attended' (caps off) and emits no deferral
    warning, because the override truly reaches the daemon row now."""

    def test_attended_override_carries_and_no_warning(
        self,
        tmp_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        fake_persistence: _FakePersistence,
    ) -> None:
        # Capped default; operator explicitly asks for caps OFF (attended). The
        # daemon now resolves the requested attended profile from the build row
        # rather than falling back to the capped default — no mismatch to warn.
        doc = {
            "queue": {"repo_allowlist": [str(repo_dir)]},
            "budget": {
                "default_profile": "unattended",
                "profiles": {
                    "attended": {},
                    "unattended": {"max_review_cycles": 2},
                },
            },
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
        }
        cfg = tmp_path / "forge.yaml"
        cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")
        result = _invoke(cfg, repo_dir, feature_yaml, "--profile", "attended")
        assert result.exit_code == 0, result.output
        # attended has no caps -> no caps banner
        assert "budget profile" not in result.output
        # attended = caps off -> the enforcement-pending note (caps-only) is silent
        assert "not yet activated" not in result.output
        # AC-04 carriage: the override is persisted so the daemon honours it.
        assert fake_persistence.profiles == ["attended"]
        assert len(fake_persistence.records) == 1


class TestLegBudgetsAreVisibleAtQueueTime:
    """A knob the operator cannot see is a knob they cannot trust.

    The leg budgets are deliberately NOT caps (``caps_enabled`` stays
    False for a profile that only turns them), so the banner is gated on
    "anything set" rather than on ``caps_enabled`` — otherwise the one
    surface that echoes a profile would go silent for exactly the profile
    the experiment round is built to use.
    """

    def test_a_leg_only_profile_still_echoes_what_it_turns(
        self,
        tmp_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        fake_persistence: _FakePersistence,
    ) -> None:
        doc = {
            "queue": {"repo_allowlist": [str(repo_dir)]},
            "budget": {
                "default_profile": "attended",
                "profiles": {
                    "attended": {},
                    "experiment": {
                        "leg_max_turns": 4,
                        "leg_sdk_timeout_seconds": 300,
                        "leg_budget_seconds": 900,
                    },
                },
            },
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
        }
        cfg = tmp_path / "forge.yaml"
        cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")

        result = _invoke(cfg, repo_dir, feature_yaml, "--profile", "experiment")

        assert result.exit_code == 0, result.output
        assert "budget profile 'experiment'" in result.output
        assert "leg_max_turns=4" in result.output
        assert "leg_sdk_timeout_seconds=300" in result.output
        assert "leg_budget_seconds=900" in result.output
        # Leg budgets are not caps, so the cap-enforcement NOTE stays silent.
        assert "not yet activated" not in result.output
        assert fake_persistence.profiles == ["experiment"]

    def test_a_profile_that_turns_nothing_is_still_silent(
        self,
        config_path: Path,
        repo_dir: Path,
        feature_yaml: Path,
        fake_persistence: _FakePersistence,
    ) -> None:
        """The gate change moves no existing line."""
        result = _invoke(config_path, repo_dir, feature_yaml, "--profile", "attended")

        assert result.exit_code == 0, result.output
        assert "budget profile" not in result.output
