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
import json
import shlex
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
    guardkit/study-tutor: /srv/checkouts/study-tutor
    # Namespace aliases: builds are queued with repo=<checkout-folder>/<name>.
    checkouts/study-tutor: /srv/checkouts/study-tutor
    guardkit/api_test: /srv/checkouts/api_test
    checkouts/api_test: /srv/checkouts/api_test
"""

SORTED_DISTINCT = [
    "/srv/checkouts/api_test",
    "/srv/checkouts/study-tutor",
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


def _script_env(
    tmp_path: Path,
    config: Path | str,
    *,
    image: str | None = "forge:test-only",
    home: Path | None = None,
) -> dict[str, str]:
    """Environment for a script run: fixture config, stub settings file, and a
    fake ``docker`` first on ``PATH`` that records any call it receives.

    ``image`` is what ``FORGE_IMAGE`` is set to; ``None`` leaves it unset,
    which is the refusal case (the script has no default image — 2026-09-24).
    ``home`` overrides ``$HOME`` so the two state binds can be shown to follow
    it rather than being written into the script."""
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
    env.pop("FORGE_IMAGE", None)
    if image is not None:
        env["FORGE_IMAGE"] = image
    if home is not None:
        env["HOME"] = str(home)
    return env


def _run_script(
    tmp_path: Path,
    config: Path | str,
    *,
    image: str | None = "forge:test-only",
    home: Path | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RECREATE_SCRIPT)],
        cwd=tmp_path,
        env=_script_env(tmp_path, config, image=image, home=home),
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
    # the two state binds are untouched — same shape, under this account's home
    home = os.environ["HOME"]
    assert f" -v {home}/forge-state:/var/forge:rw" in result.stdout
    assert (
        f" -v {home}/forge-prod-state/.forge:/home/forge/.forge:rw" in result.stdout
    )


def test_the_state_binds_follow_the_home_directory_not_a_written_out_path(tmp_path):
    """No machine's home path is written into the script (2026-09-24).

    Run it with a different ``$HOME`` and the two state binds move with it.
    Before this change they named one person's home directory outright, which
    is the same defect as the image carrying a machine's paths.
    """
    config = _write_config(tmp_path)
    elsewhere = tmp_path / "somebody-else"
    elsewhere.mkdir()

    result = _run_script(tmp_path, config, home=elsewhere)

    assert result.returncode == 0, result.stderr
    assert f" -v {elsewhere}/forge-state:/var/forge:rw" in result.stdout
    assert (
        f" -v {elsewhere}/forge-prod-state/.forge:/home/forge/.forge:rw"
        in result.stdout
    )
    # And the home directory the suite itself runs under appears nowhere in
    # the command, which is what "no written-out home path" means.
    assert f" -v {os.environ['HOME']}/forge-state:" not in result.stdout


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


def test_the_script_refuses_when_no_image_is_named(tmp_path):
    """There is no default image any more (2026-09-24).

    It used to default to ``forge:latest``, which is not what forge-prod runs:
    on 24 September the container was running a tagged build from 19 September
    while ``forge:latest`` was ten days older, so a run with nothing set would
    have quietly downgraded production. The refusal comes before anything is
    removed or started, and before the map is even read.
    """
    config = _write_config(tmp_path)

    result = _run_script(tmp_path, config, image=None)

    assert result.returncode == 1
    assert "FORGE_IMAGE is not set" in result.stderr
    assert "forge:latest" not in result.stdout
    # It printed the two candidates rather than choosing between them.
    assert "Running now:" in result.stderr
    assert "Release named:" in result.stderr
    assert "FORGE_IMAGE=<image> bash ops/forge-prod-recreate.sh" in result.stderr
    # And nothing was started: the only docker call allowed here is a read.
    calls = (tmp_path / "docker-was-called")
    if calls.exists():
        for line in calls.read_text(encoding="utf-8").splitlines():
            assert line.startswith("inspect") or line.startswith("image inspect"), line


def test_the_refusal_names_the_release_the_manifest_carries(tmp_path):
    """The operator is told what the repository's own release manifest names."""
    config = _write_config(tmp_path)
    manifest = REPO_ROOT / "release" / "manifest.yaml"
    name = ""
    version = ""
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("image_name:") and not name:
            name = line.split(":", 1)[1].strip()
        if line.startswith("version:") and not version:
            version = line.split(":", 1)[1].strip()

    result = _run_script(tmp_path, config, image=None)

    assert result.returncode == 1
    assert name and version
    assert f"{name}:{version}" in result.stderr


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


def test_recreate_preserves_configured_paths_as_single_literal_arguments(tmp_path):
    repo = tmp_path / "checkouts with spaces" / "repo 'quoted' $(false)"
    config = _write_config(tmp_path, "permissions:\n  filesystem:\n    allowlist: [/tmp]\nplanning:\n  target_repo_paths:\n    team/repo: " + json.dumps(str(repo)) + "\n")
    env = _script_env(tmp_path, config)
    state = tmp_path / "state 'quoted' $(false)"
    home_state = tmp_path / "home state"
    env.update(FORGE_STATE_DIR=str(state), FORGE_PROD_HOME_STATE=str(home_state))
    result = subprocess.run(["bash", str(RECREATE_SCRIPT)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    # Execute the printed command through the shell sops uses, but only a fake
    # Docker can run. This catches both word splitting and shell substitution.
    argv_file = tmp_path / "argv.json"
    docker = tmp_path / "bin" / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      "open(os.environ['ARGV_FILE'], 'w').write(json.dumps(sys.argv[1:]))\n")
    env["ARGV_FILE"] = str(argv_file)
    invoked = subprocess.run(["sh", "-c", result.stdout], env=env, cwd=tmp_path,
                             capture_output=True, text=True, timeout=30)
    assert invoked.returncode == 0, invoked.stderr
    args = json.loads(argv_file.read_text())
    assert args == shlex.split(result.stdout)[1:]
    binds = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "-v"]
    assert binds == [f"{repo}:{repo}:rw", f"{state}:/var/forge:rw",
                     f"{home_state}:/home/forge/.forge:rw"]
