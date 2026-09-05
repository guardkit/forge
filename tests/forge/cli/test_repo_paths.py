"""``forge repo-paths`` and the recreate script's derived binds.

The register-repo spec (2026-09-05), rule 9 and rule 10's second and third
items. Two things are under test:

* the subcommand prints each checkout path in ``planning.target_repo_paths``
  once, sorted, one per line — the map spells every repository twice, so the
  de-duplication is the point;
* ``ops/forge-prod-recreate.sh`` builds its ``-v`` flags from that output
  instead of carrying them by hand, refuses when it cannot read the map, and —
  under ``DRY_RUN=1`` — runs no docker command at all.

Nothing here reads the live estate: the config comes from ``FORGE_CONFIG``, the
settings-of-record file is a stub under ``tmp_path``, and a fake ``docker`` on
``PATH`` records any call so the "no docker ran" claim is checked, not assumed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from forge.cli.main import main

#: The forge checkout these tests run from (tests/forge/cli/ -> root).
REPO_ROOT = Path(__file__).resolve().parents[3]
RECREATE_SCRIPT = REPO_ROOT / "ops" / "forge-prod-recreate.sh"

#: The same shape as the live file: two key spellings per repository, comments
#: between the entries, one repository listed out of alphabetical order.
FIXTURE_CONFIG = """\
permissions:
  filesystem:
    allowlist:
    - /home/forge
planning:
  target_repo_paths:
    guardkit/study-tutor: /home/richardwoollcott/Projects/appmilla_github/study-tutor
    # Namespace aliases: builds are queued with repo=appmilla_github/<name>.
    appmilla_github/study-tutor: /home/richardwoollcott/Projects/appmilla_github/study-tutor
    guardkit/api_test: /home/richardwoollcott/Projects/appmilla_github/api_test
    appmilla_github/api_test: /home/richardwoollcott/Projects/appmilla_github/api_test
"""

SORTED_DISTINCT = [
    "/home/richardwoollcott/Projects/appmilla_github/api_test",
    "/home/richardwoollcott/Projects/appmilla_github/study-tutor",
]


def _write_config(tmp_path: Path, text: str = FIXTURE_CONFIG) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The subcommand
# ---------------------------------------------------------------------------


def test_the_paths_print_sorted_and_distinct_one_per_line(tmp_path):
    config = _write_config(tmp_path)

    result = CliRunner().invoke(main, ["repo-paths", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == SORTED_DISTINCT


def test_the_group_level_config_is_honoured_too(tmp_path):
    config = _write_config(tmp_path)

    result = CliRunner().invoke(main, ["--config", str(config), "repo-paths"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == SORTED_DISTINCT


def test_an_empty_map_prints_nothing_and_succeeds(tmp_path):
    config = _write_config(
        tmp_path,
        "permissions:\n  filesystem:\n    allowlist:\n    - /home/forge\n",
    )

    result = CliRunner().invoke(main, ["repo-paths", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert result.output == ""


# A config this command cannot read is answered with one plain sentence and a
# non-zero exit — never a traceback. The recreate script prints this command's
# stderr straight to a human who is standing over a container they are about to
# take down, so the three ways a config can be unreadable all read the same.


def test_a_missing_config_file_is_refused(tmp_path):
    result = CliRunner().invoke(
        main, ["repo-paths", "--config", str(tmp_path / "nowhere.yaml")]
    )

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "there is no file at" in result.output


def test_a_config_that_is_not_valid_yaml_is_refused_in_plain_english(tmp_path):
    config = _write_config(tmp_path, "planning:\n  target_repo_paths: [1, 2\n")

    result = CliRunner().invoke(main, ["repo-paths", "--config", str(config)])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "could not be read as a forge.yaml" in result.output


def test_a_config_that_fails_validation_is_refused_in_plain_english(tmp_path):
    config = _write_config(tmp_path, "planning:\n  target_repo_paths: not-a-mapping\n")

    result = CliRunner().invoke(main, ["repo-paths", "--config", str(config)])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "could not be read as a forge.yaml" in result.output


def test_without_any_config_it_says_so_in_plain_english(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./forge.yaml here

    result = CliRunner().invoke(main, ["repo-paths"])

    assert result.exit_code != 0
    assert "no forge.yaml to read" in result.output


# ---------------------------------------------------------------------------
# The recreate script's derived binds
# ---------------------------------------------------------------------------


def _script_env(tmp_path: Path, config: Path | str) -> dict[str, str]:
    """Environment for a script run: fixture config, stub settings file, and a
    fake ``docker`` first on ``PATH`` that records any call it receives."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    sentinel = tmp_path / "docker-was-called"
    for name in ("docker", "sops"):
        tool = fake_bin / name
        tool.write_text(
            f'#!/usr/bin/env bash\necho "$@" >> "{sentinel}"\nexit 0\n',
            encoding="utf-8",
        )
        tool.chmod(0o755)
    enc = tmp_path / "forge-prod.enc.env"
    enc.write_text("# stub settings of record\n", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        DRY_RUN="1",
        FORGE_CONFIG=str(config),
        FORGE_PROD_ENV_ENC=str(enc),
        PATH=f"{fake_bin}:{env['PATH']}",
    )
    return env


def _run_script(tmp_path: Path, config: Path | str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RECREATE_SCRIPT)],
        cwd=tmp_path,
        env=_script_env(tmp_path, config),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_dry_run_derives_one_bind_per_repository_path(tmp_path):
    config = _write_config(tmp_path)

    result = _run_script(tmp_path, config)

    assert result.returncode == 0, result.stderr
    for path in SORTED_DISTINCT:
        assert f" -v {path}:{path}:rw" in result.stdout
    # the two state binds are untouched
    assert " -v /home/richardwoollcott/forge-state:/var/forge:rw" in result.stdout
    assert (
        " -v /home/richardwoollcott/forge-prod-state/.forge:/home/forge/.forge:rw"
        in result.stdout
    )


def test_the_dry_run_runs_no_docker_command(tmp_path):
    config = _write_config(tmp_path)

    result = _run_script(tmp_path, config)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "docker-was-called").exists()
    assert result.stdout.startswith("docker run -d --name forge-prod")


def test_no_repository_is_bound_twice_when_the_map_spells_it_twice(tmp_path):
    config = _write_config(tmp_path)

    result = _run_script(tmp_path, config)

    assert result.returncode == 0, result.stderr
    binds = [part for part in result.stdout.split() if part.endswith(":rw")]
    assert len(binds) == len(set(binds))
    assert len(binds) == len(SORTED_DISTINCT) + 2  # + the two state binds


def test_the_script_refuses_when_the_repository_map_cannot_be_read(tmp_path):
    result = _run_script(tmp_path, tmp_path / "there-is-no-config-here.yaml")

    assert result.returncode == 1
    assert "refusing to recreate forge-prod" in result.stderr
    assert not (tmp_path / "docker-was-called").exists()


def test_the_map_read_leaves_the_lock_file_alone(tmp_path):
    """The read runs as ``uv run --frozen --no-sync``, and this is why.

    Reading the repository map happens seconds before ``docker rm -f`` takes
    forge-prod down. Without ``--frozen`` that read can rewrite ``uv.lock``;
    without ``--no-sync`` it re-installs the virtual environment and can reach
    the network to resolve dependencies. Neither belongs in front of a
    container's removal, and a rewritten lock file is a change nobody asked
    for, in a checkout somebody else may be sharing.
    """
    config = _write_config(tmp_path)

    result = _run_script(tmp_path, config)

    assert result.returncode == 0, result.stderr
    status = subprocess.run(
        ["git", "status", "--porcelain", "uv.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert status.returncode == 0, status.stderr
    assert status.stdout == ""


def test_the_map_read_asks_uv_not_to_touch_the_lock_or_the_venv():
    """The flags themselves, so a future edit cannot drop them by accident."""
    line = [
        line
        for line in RECREATE_SCRIPT.read_text(encoding="utf-8").splitlines()
        if "forge repo-paths" in line and line.startswith("REPO_PATHS=")
    ]
    assert len(line) == 1
    assert "--frozen" in line[0]
    assert "--no-sync" in line[0]


def test_the_script_refuses_when_the_map_names_no_checkouts(tmp_path):
    config = _write_config(
        tmp_path,
        "permissions:\n  filesystem:\n    allowlist:\n    - /home/forge\n",
    )

    result = _run_script(tmp_path, config)

    assert result.returncode == 1
    assert "names no checkouts" in result.stderr
    assert not (tmp_path / "docker-was-called").exists()
