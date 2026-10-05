"""``forge register-repo`` for the container set-up (design of 5 October 2026).

Every check of the design's register-repo acceptance list is here. Nothing in
this file reaches git, docker or sbx: the command's one seam,
``register_repo.run_command``, is replaced by :class:`FakeEstate`, which keeps
the settings volume, the sandbox's files and clones, and the GitHub side as
plain dictionaries and answers each command the way the live estate would.
The one exception is the drained read's own script, which is run for real
against a real (temporary) ledger and a bus address nothing listens on.

The settings fixtures carry comment lines on purpose: the live files'
comments are the record of why entries exist.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import register_repo
from forge.cli.main import main

LEAF = "bench-one"
KEY = f"guardkit/{LEAF}"
URL = f"https://github.com/guardkit/{LEAF}.git"
SANDBOX = "api-test-deploy"
VOLUME = "forge-estate_forge-settings"
CLONE = "/workspace/api_test"
SANDBOX_SETTINGS = f"{CLONE}/.guardkit/tmp/factory-runtime/forge.yaml"
CLONE_PATH = f"{CLONE}/.guardkit/tmp/factory-runtime/projects/{LEAF}"
COORDINATOR_PATH = f"/var/lib/forge/projects/{LEAF}"

COORDINATOR_YAML = """\
# THE COORDINATOR'S SETTINGS FILE (fixture). Every comment is load-bearing.
permissions:
  filesystem:
    # Only paths inside the container.
    allowlist:
      - /var/lib/forge-evidence
      - /var/lib/forge/projects/api_test
planning:
  enabled: true
  default_target_repo: guardkit/api_test
  # The coordinator's own copy of each project, by the key it is registered under.
  target_repo_paths:
    guardkit/api_test: /var/lib/forge/projects/api_test
    checkouts/api_test: /var/lib/forge/projects/api_test
  # One entry per project that has a sandbox.
  sandboxes:
    guardkit/api_test:
      name: api-test-deploy
      sidecar_url: "${FORGE_SANDBOX_SIDECAR_URL}"
      runner_url: "${FORGE_SANDBOX_RUNNER_URL}"
    # A note after the last sandbox.
deploy:
  enabled: false
  execution_surface: sidecar
  sidecar_url: "${FORGE_SANDBOX_SIDECAR_URL}"
publication:
  enabled: false
  publisher_url: "${FORGE_PUBLISHER_URL}"
  builds_may_run_inside_the_coordinator: false
"""

SANDBOX_YAML = f"""\
# THE SANDBOX'S OWN SETTINGS FILE (fixture): paths inside the sandbox.
permissions:
  filesystem:
    allowlist:
      - {CLONE}
planning:
  enabled: true
  target_repo_paths:
    # The helper resolves a project by this key.
    guardkit/api_test: {CLONE}
  sandboxes:
    guardkit/api_test:
      name: api-test-deploy
      sidecar_url: http://192.0.2.10:8925
      runner_url: http://192.0.2.10:8924
"""

PUBLISHER_JSON = {
    "credential_file": "/etc/forge-publisher/credential",
    "ledger": "/var/lib/forge/forge.db",
    "state_dir": "/var/lib/publisher/state",
    "host": "0.0.0.0",
    "port": 8711,
    "git_timeout_seconds": 180,
    "known_hosts_file": "/etc/forge-publisher/known_hosts",
    "projects": {
        "guardkit/api_test": {
            "source": "git://192.0.2.10:8918/api_test",
            "remote": "git@github.com:guardkit/api_test.git",
        }
    },
}

GOOD_CONFIG = """\
memory:
  project: bench_one
toolchain:
  test: pytest -q
autobuild:
  player:
    required_documents:
      - docs/rules.md
"""


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _no(code: int = 1, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", stderr)


class FakeEstate:
    """The live estate as three dictionaries and a project folder.

    ``volume``: file name -> text in the coordinator's settings volume.
    ``sandbox_files``: absolute path -> text inside the sandbox.
    ``clones``: absolute path -> origin URL of a clone inside the sandbox.
    ``project``: the files GitHub would hand a clone of the project.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.volume: dict[str, str] = {"forge.yaml": COORDINATOR_YAML}
        self.sandbox_files: dict[str, str] = {SANDBOX_SETTINGS: SANDBOX_YAML}
        #: File modes, by volume name or sandbox path, as ``stat -c %a`` says them.
        self.modes: dict[str, str] = {"forge.yaml": "640", SANDBOX_SETTINGS: "600"}
        self.clones: dict[str, str] = {}
        self.ignored = True
        self.sandbox_can_read = True
        self.host_can_read = True
        self.drained_answer: Any = None
        self.calls: list[list[str]] = []
        self.writes: list[str] = []
        self.project = tmp_path / "github" / LEAF
        self.project.mkdir(parents=True)
        self.write_project(".guardkit/config.yaml", GOOD_CONFIG)
        self.write_project("docs/rules.md", "# Rules\n")

    def write_project(self, relative: str, text: str) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def project_digest(self) -> dict[str, bytes]:
        return {
            str(p.relative_to(self.project)): (p.read_bytes() if p.is_file() and not p.is_symlink() else b"link")
            for p in sorted(self.project.rglob("*"))
        }

    # -- the seam ---------------------------------------------------------

    def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002 — the seam's name
        argv = list(argv)
        self.calls.append(argv)
        if argv[:4] == ["env", "GIT_TERMINAL_PROMPT=0", "git", "clone"]:
            if not self.host_can_read:
                return _no(128, "fatal: could not read Username for 'https://github.com'")
            dest = Path(argv[-1])
            shutil.copytree(self.project, dest, symlinks=True)
            return _ok()
        if argv[:2] == ["git", "-C"] and argv[3:] == ["rev-parse", "HEAD"]:
            return _ok("0123456789abcdef0123\n")
        if argv[:2] == ["docker", "run"]:
            return self._docker_run(argv)
        if argv[:2] == ["docker", "exec"]:
            if self.drained_answer is None:
                return _no(1, "Error response from daemon: No such container")
            if isinstance(self.drained_answer, subprocess.CompletedProcess):
                return self.drained_answer
            return _ok(json.dumps(self.drained_answer) + "\n")
        if argv[:2] == ["sbx", "exec"]:
            return self._sbx_exec(argv[2:], input)
        raise AssertionError(f"the fake estate was asked something it does not know: {argv}")

    def _docker_run(self, argv: list[str]):
        image_at = argv.index("alpine")
        command = argv[image_at + 1 :]
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert mounts[0].startswith(f"{VOLUME}:/s"), mounts
        if command[0] == "cat":
            name = command[1].removeprefix("/s/")
            return _ok(self.volume[name]) if name in self.volume else _no(1, "cat: no such file")
        if command[:2] == ["ls", "-1"]:
            return _ok("\n".join(sorted(self.volume)) + "\n")
        if command[:2] == ["sh", "-c"]:
            assert "--user" in argv and argv[argv.index("--user") + 1] == "1000:1000"
            assert not mounts[0].endswith(":ro")
            assert command[2] == 'umask 077 && cp /in "$1" && chmod "$(stat -c %a "$2")" "$1"'
            local = mounts[1].rsplit(":", 2)[0]
            name, like = command[4].removeprefix("/s/"), command[5].removeprefix("/s/")
            self.volume[name] = Path(local).read_bytes().decode("utf-8")
            self.modes[name] = self.modes[like]
            self.writes.append(f"volume:{name}")
            return _ok()
        raise AssertionError(f"unexpected docker run {argv}")

    def _sbx_exec(self, rest: list[str], input):
        interactive = False
        env: list[str] = []
        user = None
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag == "-i":
                interactive = True
            elif flag == "-u":
                user = rest.pop(0)
            elif flag == "-e":
                env.append(rest.pop(0))
            else:
                raise AssertionError(f"unexpected sbx flag {flag}")
        assert user == "1000", "every sandbox command runs as the sandbox user"
        assert rest[0] == SANDBOX
        command = rest[1:]
        if command[0] == "cat":
            path = command[1]
            return _ok(self.sandbox_files[path]) if path in self.sandbox_files else _no(1, "cat: no such file")
        if command[:2] == ["ls", "-1"]:
            folder = command[2].rstrip("/") + "/"
            names = sorted(p[len(folder):] for p in self.sandbox_files if p.startswith(folder) and "/" not in p[len(folder):])
            return _ok("\n".join(names) + "\n")
        if command[:2] == ["sh", "-c"]:
            assert interactive and input is not None
            assert command[2] == 'umask 077 && cat > "$1" && chmod "$(stat -c %a "$2")" "$1"'
            self.sandbox_files[command[4]] = input
            self.modes[command[4]] = self.modes[command[5]]
            self.writes.append(f"sandbox:{command[4]}")
            return _ok()
        if command[0] == "test":
            return _ok() if command[2] in self.clones else _no(1)
        if command[0] == "git":
            if command[1] == "ls-remote":
                assert "GIT_TERMINAL_PROMPT=0" in env
                return _ok("abc123\tHEAD\n") if self.sandbox_can_read else _no(
                    128, "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
                )
            if command[1] == "clone":
                self.clones[command[-1]] = command[-2]
                self.writes.append(f"clone:{command[-1]}")
                return _ok()
            if command[1] == "-C" and command[3] == "check-ignore":
                assert command[2] == CLONE
                return _ok() if self.ignored else _no(1)
            if command[1] == "-C" and command[3:] == ["remote", "get-url", "origin"]:
                return _ok(self.clones[command[2]] + "\n") if command[2] in self.clones else _no(2)
        raise AssertionError(f"unexpected sbx exec {command}")


@pytest.fixture
def estate(tmp_path, monkeypatch) -> FakeEstate:
    fake = FakeEstate(tmp_path)
    monkeypatch.setattr(register_repo, "run_command", fake)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_ESTATE_ENV_FILE", raising=False)
    return fake


@pytest.fixture
def publisher_file(tmp_path) -> Path:
    path = tmp_path / "publisher" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(PUBLISHER_JSON, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


GATEWAY = "192.0.2.10"


def _run(*args: str):
    extra = ["--gateway-address", GATEWAY] if "--publish" in args and "--estate-env-file" not in args else []
    return CliRunner().invoke(
        main,
        ["register-repo", KEY, "--github", URL, "--sandbox-settings", SANDBOX_SETTINGS, *args, *extra],
        catch_exceptions=False,
    )


def _lines(result) -> list[str]:
    return result.output.splitlines()


# ---------------------------------------------------------------------------
# --dry-run writes nothing; preparation writes only staged copies
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_anywhere(estate, publisher_file):
    before_project = estate.project_digest()
    before_publisher = publisher_file.read_bytes()

    result = _run("--dry-run", "--publish", "--publisher-settings", str(publisher_file))

    assert result.exit_code == 0, result.output
    assert estate.writes == []
    assert estate.volume == {"forge.yaml": COORDINATOR_YAML}
    assert estate.sandbox_files == {SANDBOX_SETTINGS: SANDBOX_YAML}
    assert estate.clones == {}
    assert sorted(os.listdir(publisher_file.parent)) == ["settings.json"]
    assert publisher_file.read_bytes() == before_publisher
    assert estate.project_digest() == before_project
    assert "would stage" in result.output and "would add" in result.output
    assert "What would make it live" in result.output


def test_preparation_writes_only_staged_copies_and_leaves_the_live_files_alone(estate, publisher_file):
    before_publisher = publisher_file.read_bytes()

    result = _run("--publish", "--publisher-settings", str(publisher_file))

    assert result.exit_code == 0, result.output
    assert estate.volume["forge.yaml"] == COORDINATOR_YAML
    assert estate.sandbox_files[SANDBOX_SETTINGS] == SANDBOX_YAML
    assert publisher_file.read_bytes() == before_publisher
    assert f"forge.yaml.{LEAF}-pending" in estate.volume
    assert f"{SANDBOX_SETTINGS}.{LEAF}-pending" in estate.sandbox_files
    assert (publisher_file.parent / f"settings.json.{LEAF}-pending").is_file()
    written = {w.split(":", 1)[1] for w in estate.writes if not w.startswith("clone:")}
    for name in written:
        assert "-pending" in name or ".bak-" in name, name
    assert estate.clones == {CLONE_PATH: URL}


def test_nothing_is_ever_written_into_the_project(estate):
    before = estate.project_digest()
    result = _run()
    assert result.exit_code == 0, result.output
    assert estate.project_digest() == before


# ---------------------------------------------------------------------------
# Both settings files: exactly three entries, re-parse, comments, backups
# ---------------------------------------------------------------------------


def _added(old: str, new: str) -> list[str]:
    remaining = list(new.split("\n"))
    for line in old.split("\n"):
        remaining.remove(line)
    return remaining


def test_the_coordinator_file_gains_exactly_the_three_entries(estate):
    result = _run()
    assert result.exit_code == 0, result.output
    staged = estate.volume[f"forge.yaml.{LEAF}-pending"]

    assert _added(COORDINATOR_YAML, staged) == [
        f"      - {COORDINATOR_PATH}",
        f"    {KEY}: {COORDINATOR_PATH}",
        f"    {KEY}:",
        "      name: api-test-deploy",
        '      sidecar_url: "${FORGE_SANDBOX_SIDECAR_URL}"',
        '      runner_url: "${FORGE_SANDBOX_RUNNER_URL}"',
    ]
    before, after = yaml.safe_load(COORDINATOR_YAML), yaml.safe_load(staged)
    assert after["permissions"]["filesystem"]["allowlist"] == [
        *before["permissions"]["filesystem"]["allowlist"], COORDINATOR_PATH
    ]
    assert after["planning"]["target_repo_paths"] == {
        **before["planning"]["target_repo_paths"], KEY: COORDINATOR_PATH
    }
    assert after["planning"]["sandboxes"][KEY] == before["planning"]["sandboxes"]["guardkit/api_test"]
    del after["permissions"]["filesystem"]["allowlist"][-1]
    del after["planning"]["target_repo_paths"][KEY]
    del after["planning"]["sandboxes"][KEY]
    assert after == before


def test_the_sandbox_file_gains_the_same_three_entries_with_its_own_paths(estate):
    result = _run()
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.sandbox_files[f"{SANDBOX_SETTINGS}.{LEAF}-pending"])

    assert staged["permissions"]["filesystem"]["allowlist"] == [CLONE, CLONE_PATH]
    assert staged["planning"]["target_repo_paths"][KEY] == CLONE_PATH
    assert staged["planning"]["sandboxes"][KEY] == {
        "name": "api-test-deploy",
        "sidecar_url": "http://192.0.2.10:8925",
        "runner_url": "http://192.0.2.10:8924",
    }
    assert len(_added(SANDBOX_YAML, estate.sandbox_files[f"{SANDBOX_SETTINGS}.{LEAF}-pending"])) == 6


def test_both_staged_files_re_parse_with_the_factory_s_loader(estate, tmp_path, monkeypatch):
    from forge.config.loader import load_config

    assert _run().exit_code == 0
    for name in ("FORGE_SANDBOX_SIDECAR_URL", "FORGE_SANDBOX_RUNNER_URL", "FORGE_PUBLISHER_URL"):
        monkeypatch.setenv(name, "http://10.0.0.1:1")
    for text in (
        estate.volume[f"forge.yaml.{LEAF}-pending"],
        estate.sandbox_files[f"{SANDBOX_SETTINGS}.{LEAF}-pending"],
    ):
        path = tmp_path / "check.yaml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        assert KEY in config.planning.sandboxes
        assert config.planning.sandboxes[KEY].name == "api-test-deploy"


def test_every_comment_survives_in_both_files(estate):
    assert _run().exit_code == 0
    for old, new in (
        (COORDINATOR_YAML, estate.volume[f"forge.yaml.{LEAF}-pending"]),
        (SANDBOX_YAML, estate.sandbox_files[f"{SANDBOX_SETTINGS}.{LEAF}-pending"]),
    ):
        comments = [line for line in old.split("\n") if line.strip().startswith("#")]
        assert comments
        assert [line for line in new.split("\n") if line.strip().startswith("#")] == comments


def test_each_file_has_a_dated_backup_of_the_live_text(estate, publisher_file):
    assert _run("--publish", "--publisher-settings", str(publisher_file)).exit_code == 0
    stamp = register_repo.date.today().strftime("%Y%m%d")
    backup = f"bak-{stamp}-pre-register-{LEAF}"
    assert estate.volume[f"forge.yaml.{backup}"] == COORDINATOR_YAML
    assert estate.sandbox_files[f"{SANDBOX_SETTINGS}.{backup}"] == SANDBOX_YAML
    copy = publisher_file.parent / f"settings.json.{backup}"
    assert copy.read_bytes() == publisher_file.read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o600
    assert (publisher_file.parent / f"settings.json.{LEAF}-pending").stat().st_mode & 0o777 == 0o600


def test_staged_and_backup_copies_keep_the_live_file_s_mode(estate):
    assert _run().exit_code == 0
    stamp = register_repo.date.today().strftime("%Y%m%d")
    for suffix in (f"{LEAF}-pending", f"bak-{stamp}-pre-register-{LEAF}"):
        assert estate.modes[f"forge.yaml.{suffix}"] == "640"
        assert estate.modes[f"{SANDBOX_SETTINGS}.{suffix}"] == "600"


def test_the_runner_hands_bytes_through_without_newline_translation():
    text = "a: 1\r\nb: 2\r\n# é\n"
    done = register_repo._run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        input=text,
    )
    assert done.returncode == 0
    assert done.stdout == text


def test_the_publisher_file_is_read_and_staged_byte_for_byte(estate, publisher_file):
    crlf = (json.dumps(PUBLISHER_JSON, indent=2) + "\n").replace("\n", "\r\n")
    publisher_file.write_bytes(crlf.encode())
    assert _run("--publish", "--publisher-settings", str(publisher_file)).exit_code == 0
    stamp = register_repo.date.today().strftime("%Y%m%d")
    backup = publisher_file.parent / f"settings.json.bak-{stamp}-pre-register-{LEAF}"
    assert backup.read_bytes() == crlf.encode()


def test_a_crlf_settings_file_keeps_its_bytes_in_the_backup_and_staged_copy(estate):
    estate.volume["forge.yaml"] = COORDINATOR_YAML.replace("\n", "\r\n")
    result = _run()
    assert result.exit_code == 0, result.output
    stamp = register_repo.date.today().strftime("%Y%m%d")
    assert estate.volume[f"forge.yaml.bak-{stamp}-pre-register-{LEAF}"] == estate.volume["forge.yaml"]
    staged = estate.volume[f"forge.yaml.{LEAF}-pending"]
    for line in estate.volume["forge.yaml"].split("\n"):
        assert line in staged.split("\n")


def test_re_running_changes_nothing(estate, publisher_file):
    args = ("--publish", "--publisher-settings", str(publisher_file))
    assert _run(*args).exit_code == 0
    volume, files, clones = dict(estate.volume), dict(estate.sandbox_files), dict(estate.clones)
    listing = sorted(os.listdir(publisher_file.parent))
    estate.writes.clear()

    second = _run(*args)

    assert second.exit_code == 0, second.output
    assert estate.writes == []
    assert (estate.volume, estate.sandbox_files, estate.clones) == (volume, files, clones)
    assert sorted(os.listdir(publisher_file.parent)) == listing
    assert "already staged" in second.output
    # The activation still names the backup the staged copy was made from.
    assert f"bak-{register_repo.date.today().strftime('%Y%m%d')}-pre-register-{LEAF}" in second.output


def test_once_live_a_re_run_stages_nothing_and_says_so(estate):
    assert _run().exit_code == 0
    estate.volume["forge.yaml"] = estate.volume[f"forge.yaml.{LEAF}-pending"]
    estate.sandbox_files[SANDBOX_SETTINGS] = estate.sandbox_files[f"{SANDBOX_SETTINGS}.{LEAF}-pending"]
    estate.writes.clear()

    result = _run()

    assert result.exit_code == 0, result.output
    assert estate.writes == []
    assert f"nothing is staged — {KEY} is already in the live settings" in result.output


def test_an_entry_that_already_says_something_else_is_refused_and_nothing_is_written(estate):
    estate.volume["forge.yaml"] = COORDINATOR_YAML.replace(
        "    checkouts/api_test:", f"    {KEY}: /var/lib/forge/projects/elsewhere\n    checkouts/api_test:"
    )
    result = _run()
    assert result.exit_code == 1
    assert "already has guardkit/bench-one -> /var/lib/forge/projects/elsewhere" in result.output
    assert estate.writes == []


def test_a_file_without_api_test_s_sandbox_entry_is_refused(estate):
    estate.sandbox_files[SANDBOX_SETTINGS] = SANDBOX_YAML.split("  sandboxes:")[0]
    result = _run()
    assert result.exit_code == 1
    assert "no complete planning.sandboxes entry for guardkit/api_test" in result.output
    assert estate.writes == []


def test_the_coordinator_folder_is_not_created_and_the_reason_is_said(estate):
    result = _run()
    assert result.exit_code == 0
    assert "coordinator-folder" in result.output
    assert "never reads" in result.output
    assert not any(w.startswith("volume:") and "projects" in w for w in estate.writes)


# ---------------------------------------------------------------------------
# --publish adds only the route
# ---------------------------------------------------------------------------


def test_publish_adds_only_the_route(estate, publisher_file):
    result = _run("--publish", "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    staged = json.loads((publisher_file.parent / f"settings.json.{LEAF}-pending").read_text())
    expected = json.loads(json.dumps(PUBLISHER_JSON))
    expected["projects"][KEY] = {
        "source": f"git://192.0.2.10:8918/api_test/.guardkit/tmp/factory-runtime/projects/{LEAF}",
        "remote": f"git@github.com:guardkit/{LEAF}.git",
    }
    assert staged == expected


def test_without_publish_the_publisher_is_not_touched_or_named(estate, publisher_file):
    before = publisher_file.read_bytes()
    result = _run()
    assert result.exit_code == 0
    assert publisher_file.read_bytes() == before
    assert sorted(os.listdir(publisher_file.parent)) == ["settings.json"]
    assert "--wait forge-publisher" in result.output  # started with the rest, before the coordinator
    assert "routes did not change" in result.output
    assert "grep 'publisher: settings'" not in result.output


def test_a_route_already_there_is_unchanged_and_the_publisher_is_not_in_the_sequence(estate, publisher_file):
    data = json.loads(publisher_file.read_text())
    data["projects"][KEY] = {
        "source": f"git://192.0.2.10:8918/api_test/.guardkit/tmp/factory-runtime/projects/{LEAF}",
        "remote": f"git@github.com:guardkit/{LEAF}.git",
    }
    publisher_file.write_text(json.dumps(data))
    result = _run("--publish", "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    assert sorted(os.listdir(publisher_file.parent)) == ["settings.json"]
    assert "grep 'publisher: settings'" not in result.output
    assert "routes did not change" in result.output


# ---------------------------------------------------------------------------
# The printed activation sequence
# ---------------------------------------------------------------------------


def test_the_sequence_closes_intake_before_the_drained_check_and_restarts_nothing(estate, publisher_file):
    result = _run("--publish", "--publisher-settings", str(publisher_file))
    lines = _lines(result)
    close = next(i for i, l in enumerate(lines) if "dc stop front-door bus-gateway gateway-watch" in l)
    check = next(i for i, l in enumerate(lines) if "forge register-repo --check-drained" in l)
    stop_all = next(i for i, l in enumerate(lines) if l.strip() == "dc stop")
    swap = next(i for i, l in enumerate(lines) if f"forge.yaml.{LEAF}-pending /s/forge.yaml" in l)
    supervisor = next(i for i, l in enumerate(lines) if "dc up -d sandbox-runner" in l)
    publisher = next(i for i, l in enumerate(lines) if "dc up -d --wait forge-publisher" in l)
    rest = next(i for i, l in enumerate(lines) if "dc config --services" in l)
    door = max(i for i, l in enumerate(lines) if "dc up -d front-door bus-gateway gateway-watch" in l)
    assert close < check < stop_all < swap < supervisor < publisher < rest < door
    # Every docker/sbx call the command itself made was a read or a staged write.
    for call in estate.calls:
        assert "compose" not in call and "restart" not in call and "stop" not in call


def test_the_swap_is_one_command_that_checks_all_three_files_before_copying_any(estate, publisher_file):
    lines = _lines(_run("--publish", "--publisher-settings", str(publisher_file)))
    start = next(i for i, l in enumerate(lines) if l.startswith("(d)")) + 1
    end = next(i for i, l in enumerate(lines) if l.startswith("(e)"))
    chain = lines[start:end]
    assert len(chain) == 6
    assert all(l.rstrip().endswith("\\") for l in chain[:-1]) and not chain[-1].rstrip().endswith("\\")
    assert all(l.lstrip().startswith("&&") for l in chain[1:])
    kinds = ["cmp" if " cmp " in f" {l} " else "cp" for l in chain]
    assert kinds == ["cmp", "cmp", "cmp", "cp", "cp", "cp"]


def test_the_drained_step_says_to_rely_on_the_exit_status(estate):
    output = _run().output
    assert "rely on the exit status" in output


def test_the_sequence_names_the_check_the_services_check_and_the_coordinator_log(estate):
    result = _run()
    assert "estate-check" in result.output and " services" in result.output
    assert f"autobuild dispatch: {KEY} has a sandbox" in result.output
    assert "curl -sf http://192.0.2.10:8925/healthz" in result.output


def test_the_sequence_uses_the_estate_s_own_files_when_named(estate, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sandbox_env = run_dir / "sandbox-bootstrap.env"
    sandbox_env.write_text(f"FORGE_IMAGE=forge:x\nFORGE_CONFIG_PATH={SANDBOX_SETTINGS}\n")
    estate_env = run_dir / "estate.env"
    estate_env.write_text(
        "# the estate\n"
        "COMPOSE_FILE=/srv/forge-abc/deploy/estate/compose.yaml:/srv/forge-abc/deploy/estate/compose.external-bus.yaml\n"
        f"SANDBOX_PROJECT_ENV_FILE={sandbox_env}\n"
        "FORGE_NATS_URL=nats://forge:not-printed@nats:4222\n"
    )
    result = CliRunner().invoke(
        main, ["register-repo", KEY, "--github", URL, "--estate-env-file", str(estate_env)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert f"--env-file {estate_env}" in result.output
    assert f". {run_dir / 'secrets.env'};" in result.output
    assert "/srv/forge-abc/deploy/estate/estate-check" in result.output
    assert "not-printed" not in result.output
    assert f"{SANDBOX_SETTINGS}.{LEAF}-pending" in estate.sandbox_files


# ---------------------------------------------------------------------------
# Activation step (b): the drained check
# ---------------------------------------------------------------------------


def _quiet() -> dict[str, Any]:
    return {
        "ledger": {
            "builds": {}, "interrupted": 0, "planning_runs": {}, "queue_waiting": 0,
            "queue_held": 0, "merges_live": 0, "merges_resting": 0,
            "deploy_locks_live": 0, "deploy_locks_expired": 0,
        },
        "consumers": {
            "forge-serve-planning": {"pending": 0, "ack_pending": 0},
            "forge-serve": {"pending": 0, "ack_pending": 0},
        },
    }


def _check(estate, answer):
    estate.drained_answer = answer
    return CliRunner().invoke(main, ["register-repo", "--check-drained"], catch_exceptions=False)


def test_a_quiet_factory_is_drained(estate):
    result = _check(estate, _quiet())
    assert result.exit_code == 0, result.output
    assert "DRAINED" in result.output and "NOT DRAINED" not in result.output
    asked = estate.calls[-1]
    assert asked[:3] == ["docker", "exec", "forge-estate-coordinator-1"]
    spec = json.loads(asked[-1])
    assert spec["consumers"] == ["forge-serve-planning", "forge-serve"]
    assert spec["stream"] == "PIPELINE"


def test_a_busy_factory_is_refused(estate):
    facts = _quiet()
    facts["ledger"]["builds"] = {"RUNNING": 1}
    facts["ledger"]["planning_runs"] = {"FEATURE_PLAN": 1}
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "1 build is active (RUNNING 1)" in result.output
    assert "1 planning run is not finished (FEATURE_PLAN 1)" in result.output
    assert "NOT DRAINED" in result.output


def test_a_queued_item_present_at_the_first_check_stops_it(estate):
    facts = _quiet()
    facts["ledger"]["queue_waiting"] = 1
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "1 item waits in the work queue" in result.output


def test_a_published_but_undelivered_request_stops_it(estate):
    facts = _quiet()
    facts["consumers"]["forge-serve-planning"] = {"pending": 1, "ack_pending": 0}
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "forge-serve-planning has 1 pending and 0 awaiting acknowledgement" in result.output


def test_a_prepared_request_awaiting_admission_before_its_row_exists_stops_it(estate):
    # The ledger shows nothing at all; the build consumer holds the request.
    facts = _quiet()
    facts["consumers"]["forge-serve"] = {"pending": 0, "ack_pending": 1}
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "forge-serve has 0 pending and 1 awaiting acknowledgement" in result.output


def test_an_unreadable_consumer_stops_it(estate):
    facts = _quiet()
    facts["consumers"]["forge-serve"] = {"error": "NotFoundError: consumer not found"}
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "the bus consumer forge-serve could not be read" in result.output


def test_a_missing_consumer_answer_stops_it(estate):
    facts = _quiet()
    del facts["consumers"]["forge-serve"]
    assert _check(estate, facts).exit_code == 1


def test_an_unreadable_ledger_or_coordinator_stops_it(estate):
    facts = _quiet()
    facts["ledger"] = {"error": "OperationalError: unable to open database file"}
    result = _check(estate, facts)
    assert result.exit_code == 1 and "the ledger could not be read" in result.output

    result = _check(estate, None)
    assert result.exit_code == 1 and "could not be asked" in result.output

    result = _check(estate, _ok("not json\n"))
    assert result.exit_code == 1 and "not the read's report" in result.output


class Ledger:
    """A real ledger: Forge's own migrations, and rows written through Forge's
    own stores wherever one exists, so the read is held to the schema the
    coordinator really has."""

    def __init__(self, path: Path) -> None:
        from forge.lifecycle.migrations import apply_at_boot

        self.path = path
        self.cx = sqlite3.connect(path)
        apply_at_boot(self.cx)
        self.cx.commit()
        self.now = datetime.now(timezone.utc)
        self._n = 0

    def build(self, status: str) -> str:
        self._n += 1
        build_id = f"build-FEAT-{self._n}"
        self.cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, status,"
            " triggered_by, correlation_id, queued_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (build_id, f"FEAT-{self._n}", "guardkit/api_test", "main", "f.yaml", status, "cli",
             build_id, self.now.isoformat()),
        )
        self.cx.commit()
        return build_id

    def plan(self, state: str) -> None:
        self._n += 1
        self.cx.execute(
            "INSERT INTO planning_runs (correlation_id, state, originating_user, expected_approver,"
            " request_text, triggered_by, queued_at) VALUES (?,?,?,?,?,?,?)",
            (f"plan-{self._n}", state, "U1", "U1", "a sentence", "jarvis", self.now.isoformat()),
        )
        self.cx.commit()

    def queue(self, *, after: int | None = None) -> int:
        self._n += 1
        cursor = self.cx.execute(
            "INSERT INTO work_queue (sentence, kind, status, rank, after_id, originating_user,"
            " correlation_id, queued_at) VALUES (?,?,?,?,?,?,?,?)",
            ("a sentence", "feature", "QUEUED", float(self._n), after, "U1", f"q-{self._n}",
             self.now.isoformat()),
        )
        self.cx.commit()
        return int(cursor.lastrowid)

    def merge(self, *, days_ago: float, result: str | None, release: bool) -> None:
        from forge.pipeline.publication_record import PublicationRecordStore

        build_id = self.build("COMPLETE")
        store = PublicationRecordStore(self.cx)
        then = self.now - timedelta(days=days_ago)
        grant = store.take_lease(build_id=build_id, holder="merge-press:1", now=then, repo="guardkit/api_test")
        assert grant is not None
        if result is not None or release:
            fields: dict[str, Any] = {}
            if result is not None:
                fields["result"] = result
            if release:
                fields.update(lease_holder=None, lease_expires_at=None)
            assert store.record(build_id=build_id, turn=grant.turn, now=then, **fields)
        self.cx.commit()

    def deploy_lock(self, *, days_ago: float) -> None:
        from forge.pipeline.deployment_lock import DeploymentLockStore

        build_id = self.build("COMPLETE")
        grant = DeploymentLockStore(self.cx).grant(
            target=f"guardkit/api_test:{build_id}", build_id=build_id, turn=1, holder="h",
            now=self.now - timedelta(days=days_ago),
        )
        assert grant is not None
        self.cx.commit()

    def read(self) -> dict[str, Any]:
        return _run_the_read_script(self.path)

    def judged(self) -> tuple[list[str], list[str]]:
        facts = {**self.read(), "consumers": QUIET_CONSUMERS}
        return register_repo.judge_drained(facts, CONSUMERS), register_repo.drained_notes(facts)


def _run_the_read_script(db: Path) -> dict[str, Any]:
    env = {
        **os.environ,
        "FORGE_DB_PATH": str(db),
        # Nothing listens here; the password must never come back out.
        "FORGE_NATS_URL": "nats://forge:s3cret-pass@127.0.0.1:9",
    }
    done = subprocess.run(
        [sys.executable, "-c", register_repo.DRAINED_READ_SCRIPT, json.dumps(register_repo.drained_read_spec())],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "s3cret-pass" not in done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


CONSUMERS = ["forge-serve-planning", "forge-serve"]
QUIET_CONSUMERS = _quiet()["consumers"]


def test_the_read_script_counts_a_real_ledger_and_says_an_unreachable_bus_is_unreadable(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.build("RUNNING")
    ledger.build("COMPLETE")
    ledger.plan("FEATURE_SPEC")
    first = ledger.queue()
    ledger.queue(after=first)
    ledger.merge(days_ago=0, result=None, release=False)
    ledger.deploy_lock(days_ago=0)

    facts = ledger.read()

    assert facts["ledger"] == {
        "builds": {"RUNNING": 1}, "interrupted": 0, "planning_runs": {"FEATURE_SPEC": 1},
        "queue_waiting": 1, "queue_held": 1, "merges_live": 1, "merges_resting": 0,
        "deploy_locks_live": 1, "deploy_locks_expired": 0,
    }
    assert set(facts["consumers"]) == {"forge-serve", "forge-serve-planning"}
    assert all("error" in row for row in facts["consumers"].values())
    assert len(register_repo.judge_drained(facts, CONSUMERS)) == 8


def test_an_empty_real_ledger_is_quiet(tmp_path):
    facts = Ledger(tmp_path / "forge.db").read()
    assert facts["ledger"] == _quiet()["ledger"]
    assert register_repo.judge_drained({**facts, "consumers": QUIET_CONSUMERS}, CONSUMERS) == []


def test_old_interrupted_builds_with_quiet_consumers_are_drained_and_said_for_information(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    for _ in range(28):
        ledger.build("INTERRUPTED")
    facts = ledger.read()
    assert facts["ledger"]["builds"] == {}
    assert facts["ledger"]["interrupted"] == 28
    reasons, notes = ledger.judged()
    assert reasons == []
    assert len(notes) == 1 and "28 builds are INTERRUPTED" in notes[0]


def test_twenty_eight_interrupted_builds_print_drained(estate):
    facts = _quiet()
    facts["ledger"]["interrupted"] = 28
    result = _check(estate, facts)
    assert result.exit_code == 0, result.output
    assert "28 builds are INTERRUPTED" in result.output


@pytest.mark.parametrize("state", ["QUEUED", "PREPARING", "RUNNING", "PAUSED", "FINALISING"])
def test_every_active_build_state_is_counted(tmp_path, state):
    from forge.planning.work_queue_loop import BUILD_ACTIVE_STATES

    assert state in BUILD_ACTIVE_STATES
    ledger = Ledger(tmp_path / "forge.db")
    ledger.build(state)
    assert ledger.read()["ledger"]["builds"] == {state: 1}


def test_a_stale_refused_merge_with_its_lease_put_down_is_drained_with_a_note(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.merge(days_ago=14, result="publication pending", release=True)
    reasons, notes = ledger.judged()
    assert reasons == []
    assert notes == [
        "for information: 1 merge rests short of 'merged into the remote and running' with nobody "
        "holding it (refused, publication off, or a press that stopped); a restart does not touch it"
    ]


def test_a_press_that_crashed_with_its_lease_expired_is_drained_with_a_note(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.merge(days_ago=10, result=None, release=False)
    reasons, notes = ledger.judged()
    assert reasons == []
    assert len(notes) == 1 and "1 merge rests" in notes[0]


def test_a_live_merge_lease_is_not_drained(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.merge(days_ago=0, result="publication pending", release=False)
    reasons, _ = ledger.judged()
    assert reasons == [
        "1 merge is in progress (a publication record's lease is held and has not expired)"
    ]


def test_a_finished_merge_is_neither_counted_nor_noted(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.merge(days_ago=3, result="merged into the remote and running", release=True)
    assert ledger.judged() == ([], [])


def test_a_crashed_deploy_lock_that_has_expired_is_drained_with_a_note(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.deploy_lock(days_ago=10)
    reasons, notes = ledger.judged()
    assert reasons == []
    assert notes == [
        "for information: 1 deployment lock was left by a build that stopped and has expired; "
        "the next deploy takes it over"
    ]


def test_a_live_deploy_lock_is_not_drained(tmp_path):
    ledger = Ledger(tmp_path / "forge.db")
    ledger.deploy_lock(days_ago=0)
    reasons, _ = ledger.judged()
    assert reasons == ["1 deploy is in progress (a deployment target's lock is held and has not expired)"]


def test_a_held_queue_item_says_wait_for_its_antecedent_or_withdraw_it(estate):
    facts = _quiet()
    facts["ledger"]["queue_held"] = 1
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "behind another item; wait for the item it waits on to finish, or withdraw it" in result.output


def test_a_missing_ledger_count_is_unreadable_not_zero(estate):
    facts = _quiet()
    del facts["ledger"]["deploy_locks_live"]
    result = _check(estate, facts)
    assert result.exit_code == 1
    assert "could not be counted" in result.output


# ---------------------------------------------------------------------------
# Step 1: the project is checked, and refused with the lines to add
# ---------------------------------------------------------------------------


def test_a_project_without_memory_project_is_refused_with_the_lines_to_add(estate):
    estate.write_project(".guardkit/config.yaml", GOOD_CONFIG.replace("memory:\n  project: bench_one\n", ""))
    result = _run()
    assert result.exit_code == 1
    assert "memory:\n    project: <a name of letters, digits and underscores>" in result.output
    assert estate.writes == []


def test_a_project_without_a_test_command_is_refused_with_the_lines_to_add(estate):
    estate.write_project(".guardkit/config.yaml", GOOD_CONFIG.replace("toolchain:\n  test: pytest -q\n", ""))
    result = _run()
    assert result.exit_code == 1
    assert "toolchain:\n    test: <the command that runs this project's tests>" in result.output
    assert estate.writes == []


def test_a_missing_declared_document_is_refused_by_name(estate):
    (estate.project / "docs" / "rules.md").unlink()
    result = _run()
    assert result.exit_code == 1
    assert "the declared document docs/rules.md" in result.output and "is not in the project" in result.output
    assert estate.writes == []


def test_a_declared_document_that_is_a_link_is_refused(estate):
    (estate.project / "docs" / "real.md").write_text("x")
    (estate.project / "docs" / "rules.md").unlink()
    (estate.project / "docs" / "rules.md").symlink_to("real.md")
    result = _run()
    assert result.exit_code == 1
    assert "docs/rules.md is a link" in result.output


def test_a_project_with_no_settings_file_names_every_gap_at_once(estate):
    (estate.project / ".guardkit" / "config.yaml").unlink()
    result = _run()
    assert result.exit_code == 1
    assert "memory:" in result.output and "toolchain:" in result.output
    assert estate.writes == []


def test_a_project_the_machine_cannot_clone_is_refused(estate):
    estate.host_can_read = False
    result = _run()
    assert result.exit_code == 1
    assert f"could not read {URL}" in result.output
    assert estate.writes == []


# ---------------------------------------------------------------------------
# Step 3: the factory's clone in the shared sandbox
# ---------------------------------------------------------------------------


def test_the_clone_is_made_as_the_sandbox_user_in_the_projects_folder(estate):
    result = _run()
    assert result.exit_code == 0, result.output
    assert estate.clones == {CLONE_PATH: URL}
    clone_call = next(c for c in estate.calls if c[:2] == ["sbx", "exec"] and "clone" in c)
    assert clone_call[clone_call.index("-u") + 1] == "1000"


def test_a_clone_with_a_different_origin_is_refused_and_left_alone(estate):
    estate.clones[CLONE_PATH] = "https://github.com/someone-else/bench-one.git"
    result = _run()
    assert result.exit_code == 1
    assert "cloned from https://github.com/someone-else/bench-one.git" in result.output
    assert estate.writes == []
    assert estate.clones == {CLONE_PATH: "https://github.com/someone-else/bench-one.git"}


def test_a_clone_with_the_same_origin_is_reused(estate):
    estate.clones[CLONE_PATH] = f"https://github.com/guardkit/{LEAF}"
    result = _run()
    assert result.exit_code == 0, result.output
    assert not any(w.startswith("clone:") for w in estate.writes)
    assert "is already a clone of" in result.output


def test_an_unreadable_private_repository_is_refused_in_plain_words(estate):
    estate.sandbox_can_read = False
    result = _run()
    assert result.exit_code == 1
    assert "the sandbox cannot read" in result.output
    assert "private one needs a read-only credential" in result.output
    assert "owner decision" in result.output
    assert estate.writes == []


def test_a_projects_folder_that_is_not_git_ignored_is_refused(estate):
    estate.ignored = False
    result = _run()
    assert result.exit_code == 1
    assert "is not git-ignored in the api_test clone" in result.output
    assert estate.writes == []


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, said",
    [
        (["register-repo", "bench-one", "--github", URL], "not a project name of the form org/name"),
        (["register-repo", KEY], "--github"),
        (["register-repo", KEY, "--github", "git@github.com:guardkit/bench-one.git"], "https://"),
        (["register-repo", KEY, "--github", "https://github.com/guardkit/other.git"], "names the repository 'other'"),
        (["register-repo", KEY, "--github", URL], "which file is the sandbox's own forge.yaml"),
        (["register-repo", KEY, "--github", URL, "--sandbox-settings", SANDBOX_SETTINGS, "--publish"],
         "--publish needs the publisher's settings file"),
        (["register-repo", KEY, "--github", URL, "--sandbox-settings", "/elsewhere/forge.yaml"], "--sandbox-clone"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_asked(estate, args, said):
    result = CliRunner().invoke(main, args, catch_exceptions=False)
    assert result.exit_code == 1
    assert said in result.output
    assert estate.calls == []


def test_json_reports_the_steps_and_the_activation(estate):
    result = _run("--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert {"step", "status", "detail"} == set(report["steps"][0])
    assert any("--check-drained" in line for line in report["activation"])


# ---------------------------------------------------------------------------
# The surgical YAML helpers (kept from the first register-repo)
# ---------------------------------------------------------------------------


def test_locate_walks_indentation_to_a_nested_key():
    lines = COORDINATOR_YAML.split("\n")
    block = register_repo.locate(lines, ("planning", "sandboxes", "guardkit/api_test"))
    assert block is not None
    assert lines[block.key_line].strip() == "guardkit/api_test:"
    assert register_repo.locate(lines, ("planning", "missing")) is None


def test_missing_levels_are_created_and_the_result_parses():
    lines = ["# a comment", "planning:", "  enabled: true"]
    register_repo.set_mapping_entry(lines, ("planning", "target_repo_paths"), KEY, "/x")
    register_repo.append_sequence_item(lines, ("permissions", "filesystem", "allowlist"), "/x")
    assert yaml.safe_load("\n".join(lines)) == {
        "planning": {"enabled": True, "target_repo_paths": {KEY: "/x"}},
        "permissions": {"filesystem": {"allowlist": ["/x"]}},
    }
    assert lines[0] == "# a comment"


def test_an_inline_value_is_refused_rather_than_guessed():
    lines = ["planning:", "  target_repo_paths: {a: b}"]
    with pytest.raises(register_repo.YamlEditRefused):
        register_repo.set_mapping_entry(lines, ("planning", "target_repo_paths"), KEY, "/x")


def test_a_scalar_that_would_read_back_differently_is_quoted():
    assert register_repo._scalar("/var/lib/forge/projects/x") == "/var/lib/forge/projects/x"
    assert register_repo._scalar("${FORGE_SANDBOX_SIDECAR_URL}") == '"${FORGE_SANDBOX_SIDECAR_URL}"'
    assert register_repo._scalar("http://192.0.2.10:8925") == '"http://192.0.2.10:8925"'
    assert register_repo._scalar("1.0") == '"1.0"'
    assert register_repo._scalar("true") == '"true"'


def test_the_publish_source_comes_from_the_estate_s_gateway_address(estate, tmp_path, publisher_file):
    estate_env = tmp_path / "estate.env"
    estate_env.write_text(
        f"FACTORY_GATEWAY_ADDRESS=192.0.2.77\nFORGE_PUBLISHER_SETTINGS_FILE={publisher_file}\n"
    )
    result = _run("--publish", "--estate-env-file", str(estate_env))
    assert result.exit_code == 0, result.output
    staged = json.loads((publisher_file.parent / f"settings.json.{LEAF}-pending").read_text())
    assert staged["projects"][KEY]["source"] == (
        f"git://192.0.2.77:8918/api_test/.guardkit/tmp/factory-runtime/projects/{LEAF}"
    )


def test_publish_without_a_gateway_address_is_refused(estate, publisher_file):
    result = CliRunner().invoke(
        main,
        ["register-repo", KEY, "--github", URL, "--sandbox-settings", SANDBOX_SETTINGS,
         "--publish", "--publisher-settings", str(publisher_file)],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--gateway-address" in result.output
    assert estate.calls == []


@pytest.mark.parametrize(
    "address, said",
    [
        ("https://someone:tok3n-value@github.com/guardkit/bench-one.git", "contains an @"),
        ("https://tok3n-value@github.com/guardkit/bench-one.git", "contains an @"),
        ("ssh://tok3n-value@github.com/guardkit/bench-one.git", "contains an @"),
        ("https://github.com/guardkit/bench-one.git?tok3n-value", "contains a ?"),
        ("https://github.com/guardkit/bench-one.git#tok3n-value", "contains a #"),
        ("https:/github.com/guardkit/tok3n-value/bench-one.git", "does not begin with https://"),
        ("https:///github.com/tok3n-value/bench-one.git", "does not begin with https://"),
        ("http://tok3n-value.example/guardkit/bench-one.git", "does not begin with https://"),
    ],
)
def test_a_bad_address_is_refused_without_echoing_it(estate, address, said):
    result = CliRunner().invoke(main, ["register-repo", KEY, "--github", address], catch_exceptions=False)
    assert result.exit_code == 1
    assert said in result.output
    assert "tok3n-value" not in result.output
    assert estate.calls == []
