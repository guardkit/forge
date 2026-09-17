"""The sandbox that carries the factory — Part O, phase 1, lane L2.

Rich's rule (2026-09-07): nothing the factory runs on a repository runs on the
host. So the sandbox that deploys a repository also carries the factory's two
services for it — the deploy sidecar and the build runner — on the factory's
own clone of the repository. Rules 68 and 69 of the spec, and rule 72's
inventory.

Six halves are proven here:

* the deploy wrapper (``deploy/sandbox-deploy.sh``) creates such a sandbox
  with the clone, the read-only mounts of the factory's code, the read-write
  receipts root, the two service ports and the environment file, and starts
  both host units — and, without the new settings, is byte for byte what it
  was (the argv is pinned both ways);
* the bootstrap that runs inside the sandbox (``deploy/sandbox-runner.sh``)
  copies the factory's code out of the mounts, makes the venv once, installs
  once, proves the install, starts both services, starts a died one again,
  and never opens the ledger;
* the profile's six new settings load, are checked, and reach the deploy
  script's environment exactly when they are set;
* ``forge register-repo --deploy-port`` emits them with this repository's own
  ports and paths, and ships the bootstrap byte for byte;
* the host unit that holds the bootstrap open is shaped like the keeper, and
  its stop reaches inside the sandbox — proven by running its own ExecStop
  command line against a real process of this test's own making;
* the systemd README's inventory of the runner's ledger touches (rule 72)
  cites lines that exist and still say what is quoted.

No test here runs ``sbx``, ``systemctl``, ``uv`` or ``langgraph``: fakes placed
first on PATH stand in for all four, record what they were asked, and answer
as told. Git is real, in temporary repositories that stand in for the mounts.
No sandbox, venv, unit or service of the estate is touched.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

import forge.cli.deploy_templates as templates
from forge.cli import register_repo
from forge.deploy.profile import DeployProfileError, parse_deploy_profile
from forge.deploy.runbook_builder import build_deploy_runbook, sandbox_env

TEMPLATES = Path(templates.__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The five settings every sandbox has always had.
FIVE = (
    "SANDBOX_NAME",
    "SANDBOX_MEMORY",
    "SANDBOX_CPUS",
    "SANDBOX_PUBLISH",
    "SANDBOX_ALLOW_NETWORK",
)

#: The six that make the sandbox carry the factory.
SIX = (
    "SANDBOX_SIDECAR_PUBLISH",
    "SANDBOX_RUNNER_PUBLISH",
    "SANDBOX_ENV_FILE",
    "SANDBOX_FORGE_PATH",
    "SANDBOX_GUARDKIT_PATH",
    "SANDBOX_RECEIPTS_PATH",
)

#: The checkouts the wrapper mounts and the bootstrap installs from, in order.
FACTORY_CHECKOUTS = ("forge", "guardkit", "nats-core", "fleet-memory", "guardkitfactory")


# ---------------------------------------------------------------------------
# The fakes
# ---------------------------------------------------------------------------

FAKE_SBX = """#!/usr/bin/env bash
# A stand-in for Docker's `sbx`. Writes down every argument and answers as
# told; never touches a real sandbox or the daemon.
printf '%s\\n' "$*" >> "$SBX_LOG"
case "$1" in
  ls) printf '%s\\n' "${SBX_LS:-}" ;;
  policy)
    if [ "$2 $3" = "check network" ]; then
      target="${@: -1}"
      case ",${SBX_ALLOWED:-}," in
        *",${target},"*) exit 0 ;;
        *) exit 1 ;;
      esac
    fi
    ;;
  exec) exit "${SBX_EXEC_STATUS:-0}" ;;
esac
exit 0
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# A stand-in for systemctl. Writes down what it was asked and does nothing.
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
exit 0
"""

FAKE_UV = """#!/usr/bin/env bash
# A stand-in for uv. `uv venv DIR` makes DIR/bin/python out of the fake
# service program; `uv pip install --python PY ...` makes the two console
# scripts beside PY. Every call is written down. Nothing is installed.
# Each call also writes, to its own separate log so the call log stays exactly
# what it was, whether uv's "never download an interpreter" switch was in the
# environment it was handed.
printf '%s\\n' "uv $*" >> "$FAKE_LOG"
if [ -n "${UV_ENV_LOG:-}" ]; then
  if [ -n "${UV_PYTHON_DOWNLOADS+x}" ]; then downloads="$UV_PYTHON_DOWNLOADS"; else downloads=unset; fi
  printf '%s\\n' "uv $1 UV_PYTHON_DOWNLOADS=$downloads" >> "$UV_ENV_LOG"
fi
case "$1" in
  venv)
    dir="${@: -1}"
    mkdir -p "$dir/bin"
    cp "$FAKE_BIN/fake-service" "$dir/bin/python"
    chmod 755 "$dir/bin/python"
    ;;
  pip)
    if [ "${2:-}" = "install" ] && [ -n "${FAKE_BROKEN_RUNTIME:-}" ]; then
      rm -f "$FAKE_BROKEN_RUNTIME"
    fi
    py=""
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--python" ]; then py="$arg"; fi
      prev="$arg"
    done
    bindir="$(dirname "$py")"
    for tool in langgraph guardkit-py; do
      cp "$FAKE_BIN/fake-service" "$bindir/$tool"
      chmod 755 "$bindir/$tool"
    done
    ;;
esac
exit 0
"""

FAKE_SERVICE = """#!/usr/bin/env bash
# A stand-in for the venv's python, langgraph and guardkit-py. Writes down its
# name and arguments, then the names of the settings it can see (never a
# value), then stays up like a service — unless a marker in FAKE_DIE_ONCE_DIR
# tells it to exit once, which is how "a service died" is played.
me="$(basename "$0")"
if [ "$me" = "python" ] && [ "${1:-}" = "-c" ]; then
  case "${2:-}" in
    *"sys.version"*) exec /usr/bin/python3 "$@" ;;
  esac
fi
printf '%s\\n' "$me $*" >> "$FAKE_LOG"
if [ "$me" = "python" ] && [ "${1:-}" = "-c" ]; then
  case "${2:-}" in
    *"serve("*) ;;
    *)
      if [ -n "${FAKE_BROKEN_RUNTIME:-}" ] && [ -f "$FAKE_BROKEN_RUNTIME" ]; then exit 1; fi
      exit 0 ;;
  esac
fi
if [ -n "${FORGE_DB_PATH+x}" ]; then db=set; else db=unset; fi
printf '%s\\n' "env $me FORGE_DB_PATH=$db FORGE_GUARDKIT_PATH=${FORGE_GUARDKIT_PATH:-unset} FORGE_RECEIPTS_DIR=${FORGE_RECEIPTS_DIR:-unset} GUARDKIT_HARNESS=${GUARDKIT_HARNESS:-unset} SIDECAR_PORT=${FORGE_DEPLOY_SIDECAR_PORT:-unset} BIND=${SANDBOX_RUNNER_BIND:-unset} UV_PYTHON_DOWNLOADS=${UV_PYTHON_DOWNLOADS:-unset} PWD=$PWD" >> "$FAKE_LOG"
role="$me"
if [ "$me" = "python" ]; then role=sidecar; fi
if [ "$me" = "langgraph" ]; then role=runner; fi
if [ -n "${FAKE_DIE_ONCE_DIR:-}" ] && [ -f "$FAKE_DIE_ONCE_DIR/$role" ]; then
  rm -f "$FAKE_DIE_ONCE_DIR/$role"
  exit 1
fi
exec sleep 300
"""


def _write_fake(directory: Path, name: str, text: str) -> None:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The wrapper: deploy/sandbox-deploy.sh
# ---------------------------------------------------------------------------


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    """An estate folder with the five factory checkouts beside the repository."""
    root = tmp_path / "estate"
    for name in FACTORY_CHECKOUTS:
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture
def wrapper_repo(estate: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A checkout carrying the shipped wrapper, plus the fake sbx and systemctl."""
    repo = estate / "bench-one"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(TEMPLATES / "sandbox-deploy.sh", repo / "deploy" / "sandbox-deploy.sh")
    (repo / "deploy" / "sandbox-deploy.sh").chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_fake(fake_bin, "sbx", FAKE_SBX)
    _write_fake(fake_bin, "systemctl", FAKE_SYSTEMCTL)
    return repo, fake_bin


def _drive_wrapper(wrapper_repo, tmp_path: Path, **env: str):
    """Run the wrapper with the fakes first on PATH; return (result, sbx, systemctl)."""
    repo, fake_bin = wrapper_repo
    sbx_log = tmp_path / "sbx.log"
    systemctl_log = tmp_path / "systemctl.log"
    sbx_log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    run_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SBX_LOG": str(sbx_log),
        "SYSTEMCTL_LOG": str(systemctl_log),
        "SBX_LS": "",
        "SANDBOX_NAME": "bench-one-deploy",
        "SANDBOX_MEMORY": "6g",
        "SANDBOX_CPUS": "4",
        "SANDBOX_PUBLISH": "127.0.0.1:8911:8911,127.0.0.1:8912:8912",
        "SANDBOX_ALLOW_NETWORK": "pypi.org,*.debian.org",
    }
    for name in SIX:
        run_env.pop(name, None)
    run_env.update({k: str(v) for k, v in env.items()})
    result = subprocess.run(
        [str(repo / "deploy" / "sandbox-deploy.sh")],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
    )
    return result, _log_lines(sbx_log), _log_lines(systemctl_log)


def _factory_env(estate: Path, tmp_path: Path, *, env_file: bool = True) -> dict[str, str]:
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    env = {
        "SANDBOX_SIDECAR_PUBLISH": "127.0.0.1:8935:8125",
        "SANDBOX_RUNNER_PUBLISH": "127.0.0.1:8934:8124",
        "SANDBOX_RECEIPTS_PATH": str(receipts),
    }
    if env_file:
        rendered = tmp_path / "bench-one-deploy.env"
        rendered.write_text("FORGE_CONFIG_PATH=/somewhere/forge.yaml\n", encoding="utf-8")
        env["SANDBOX_ENV_FILE"] = str(rendered)
    return env


class TestTheWrapperWithoutTheNewSettings:
    def test_the_create_argv_is_byte_for_byte_what_it_was(self, wrapper_repo, tmp_path):
        repo, _ = wrapper_repo
        result, sbx, systemctl = _drive_wrapper(wrapper_repo, tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")]
        assert created == [
            f"create shell {repo} --name bench-one-deploy --memory 6g --cpus 4 "
            "--publish 127.0.0.1:8911:8911 --publish 127.0.0.1:8912:8912"
        ]
        assert systemctl == ["--user start forge-sandbox-keeper@bench-one-deploy"]

    def test_the_deploy_still_runs_inside_exactly_as_before(self, wrapper_repo, tmp_path):
        repo, _ = wrapper_repo
        result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert [line for line in sbx if line.startswith("exec ")] == [
            f"exec -w {repo} -e CANDIDATE -e PROMOTE -e REVERT -e CANDIDATE_DOWN "
            "-e CANDIDATE_PORT -e ROLLBACK_IMAGE_REF -e ENV_FILE "
            "bench-one-deploy deploy/deploy.sh"
        ]


class TestTheSandboxThatCarriesTheFactory:
    def test_it_is_created_with_the_clone_the_mounts_the_ports_and_the_env_file(
        self, wrapper_repo, estate, tmp_path
    ):
        repo, _ = wrapper_repo
        env = _factory_env(estate, tmp_path)
        result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, **env)

        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")]
        assert created == [
            f"create shell {repo} "
            f"{estate}/forge:ro {estate}/guardkit:ro {estate}/nats-core:ro "
            f"{estate}/fleet-memory:ro {estate}/guardkitfactory:ro "
            f"{env['SANDBOX_RECEIPTS_PATH']} "
            "--name bench-one-deploy --clone --memory 6g --cpus 4 "
            "--publish 127.0.0.1:8911:8911 --publish 127.0.0.1:8912:8912 "
            "-p 127.0.0.1:8935:8125 -p 127.0.0.1:8934:8124 "
            f"--env-file {env['SANDBOX_ENV_FILE']} "
            "--env SANDBOX_FORGE_PATH --env SANDBOX_GUARDKIT_PATH "
            "--env SANDBOX_RECEIPTS_PATH"
        ]

    def test_the_mounts_default_to_the_checkout_s_neighbours(
        self, wrapper_repo, estate, tmp_path
    ):
        # No SANDBOX_FORGE_PATH or SANDBOX_GUARDKIT_PATH given: the folders
        # beside the checkout are what is mounted.
        result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, **_factory_env(estate, tmp_path))

        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")][0]
        assert f" {estate}/forge:ro {estate}/guardkit:ro " in created

    def test_a_named_forge_checkout_is_mounted_from_where_it_is_named(
        self, wrapper_repo, tmp_path
    ):
        elsewhere = tmp_path / "elsewhere"
        for name in FACTORY_CHECKOUTS:
            (elsewhere / name).mkdir(parents=True)
        env = _factory_env(elsewhere, tmp_path, env_file=False)
        env["SANDBOX_FORGE_PATH"] = str(elsewhere / "forge")
        env["SANDBOX_GUARDKIT_PATH"] = str(elsewhere / "guardkit")
        result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, **env)

        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")][0]
        assert (
            f" {elsewhere}/forge:ro {elsewhere}/guardkit:ro {elsewhere}/nats-core:ro "
            in created
        )

    def test_without_an_env_file_none_is_passed(self, wrapper_repo, estate, tmp_path):
        result, sbx, _ = _drive_wrapper(
            wrapper_repo, tmp_path, **_factory_env(estate, tmp_path, env_file=False)
        )

        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")][0]
        assert "--env-file" not in created
        assert "--clone" in created
        assert "-p 127.0.0.1:8935:8125 -p 127.0.0.1:8934:8124" in created

    def test_both_host_units_are_started(self, wrapper_repo, estate, tmp_path):
        result, _, systemctl = _drive_wrapper(
            wrapper_repo, tmp_path, **_factory_env(estate, tmp_path)
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert systemctl == [
            "--user start forge-sandbox-keeper@bench-one-deploy",
            "--user start forge-sandbox-runner@bench-one-deploy",
        ]

    def test_an_existing_sandbox_is_not_created_again_but_both_units_still_start(
        self, wrapper_repo, estate, tmp_path
    ):
        result, sbx, systemctl = _drive_wrapper(
            wrapper_repo,
            tmp_path,
            SBX_LS="bench-one-deploy   running",
            **_factory_env(estate, tmp_path),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert [line for line in sbx if line.startswith("create ")] == []
        assert len(systemctl) == 2

    def test_the_deploy_inside_is_unchanged(self, wrapper_repo, estate, tmp_path):
        repo, _ = wrapper_repo
        result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, **_factory_env(estate, tmp_path))

        assert result.returncode == 0, result.stdout + result.stderr
        assert [line for line in sbx if line.startswith("exec ")] == [
            f"exec -w {repo} -e CANDIDATE -e PROMOTE -e REVERT -e CANDIDATE_DOWN "
            "-e CANDIDATE_PORT -e ROLLBACK_IMAGE_REF -e ENV_FILE "
            "bench-one-deploy deploy/deploy.sh"
        ]


class TestTheWrapperRefusesInOneSentence:
    @pytest.mark.parametrize("only", ["SANDBOX_SIDECAR_PUBLISH", "SANDBOX_RUNNER_PUBLISH"])
    def test_one_service_port_without_the_other(self, wrapper_repo, tmp_path, only):
        result, sbx, systemctl = _drive_wrapper(
            wrapper_repo, tmp_path, **{only: "127.0.0.1:8935:8125"}
        )

        assert result.returncode == 2
        assert "go together" in result.stdout
        assert sbx == []
        assert systemctl == []

    def test_a_missing_factory_checkout(self, wrapper_repo, estate, tmp_path):
        shutil.rmtree(estate / "guardkitfactory")
        result, sbx, systemctl = _drive_wrapper(
            wrapper_repo, tmp_path, **_factory_env(estate, tmp_path)
        )

        assert result.returncode == 2
        assert f"no checkout at {estate}/guardkitfactory" in result.stdout
        assert [line for line in sbx if line.startswith("create ")] == []
        assert systemctl == []

    def test_a_receipts_root_that_is_not_there(self, wrapper_repo, estate, tmp_path):
        env = _factory_env(estate, tmp_path)
        env["SANDBOX_RECEIPTS_PATH"] = str(tmp_path / "no-such-receipts")
        result, sbx, systemctl = _drive_wrapper(wrapper_repo, tmp_path, **env)

        assert result.returncode == 2
        assert "no such folder" in result.stdout
        assert [line for line in sbx if line.startswith("create ")] == []
        assert systemctl == []

    def test_an_env_file_that_is_not_there(self, wrapper_repo, estate, tmp_path):
        env = _factory_env(estate, tmp_path)
        env["SANDBOX_ENV_FILE"] = str(tmp_path / "not-rendered.env")
        result, sbx, systemctl = _drive_wrapper(wrapper_repo, tmp_path, **env)

        assert result.returncode == 2
        assert "no such file" in result.stdout
        assert [line for line in sbx if line.startswith("create ")] == []
        assert systemctl == []


# ---------------------------------------------------------------------------
# The bootstrap inside the sandbox: deploy/sandbox-runner.sh
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """What the bootstrap finds inside a sandbox: the mounts as git checkouts,
    the repository clone with the shipped bootstrap, a fresh home, the fakes."""
    estate = tmp_path / "estate"
    for name in FACTORY_CHECKOUTS:
        checkout = estate / name
        checkout.mkdir(parents=True)
        _git(checkout, "init", "-q")
        (checkout / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.0.1"\n', encoding="utf-8"
        )
        if name == "forge":
            (checkout / "forge.langgraph.json").write_text("{}\n", encoding="utf-8")
        _commit_all(checkout, "init")
    repo = estate / "bench-one"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(TEMPLATES / "sandbox-runner.sh", repo / "deploy" / "sandbox-runner.sh")
    (repo / "deploy" / "sandbox-runner.sh").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_fake(fake_bin, "uv", FAKE_UV)
    _write_fake(fake_bin, "fake-service", FAKE_SERVICE)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    return {
        "estate": estate,
        "repo": repo,
        "home": home,
        "fake_bin": fake_bin,
        "log": tmp_path / "fake.log",
        "uv_env_log": tmp_path / "fake-uv-env.log",
        "receipts": receipts,
    }


def _bootstrap_env(sandbox: dict[str, Path], **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{sandbox['fake_bin']}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(sandbox["home"]),
        "FAKE_LOG": str(sandbox["log"]),
        "FAKE_BIN": str(sandbox["fake_bin"]),
        "UV_ENV_LOG": str(sandbox["uv_env_log"]),
        "SANDBOX_RECEIPTS_PATH": str(sandbox["receipts"]),
        "SANDBOX_RUNNER_RESTART_SECONDS": "0",
    }
    for name in (
        "FORGE_DB_PATH",
        "FORGE_RECEIPTS_DIR",
        "FORGE_GUARDKIT_PATH",
        "GUARDKIT_HARNESS",
        # Nothing outside the script may supply this one: the whole point of the
        # test below is that the script itself no longer leaves it lying about.
        "UV_PYTHON_DOWNLOADS",
    ):
        env.pop(name, None)
    env.update(extra)
    return env


def _bootstrap_only(sandbox: dict[str, Path], **extra: str) -> subprocess.CompletedProcess[str]:
    sandbox["log"].write_text("", encoding="utf-8")
    sandbox["uv_env_log"].write_text("", encoding="utf-8")
    return subprocess.run(
        [str(sandbox["repo"] / "deploy" / "sandbox-runner.sh")],
        cwd=sandbox["repo"],
        env=_bootstrap_env(sandbox, SANDBOX_RUNNER_BOOTSTRAP_ONLY="1", **extra),
        capture_output=True,
        text=True,
    )


class TestTheBootstrapMakesTheVenvOnce:
    def test_the_first_run_copies_makes_the_venv_installs_and_proves_it(self, sandbox):
        result = _bootstrap_only(sandbox)

        assert result.returncode == 0, result.stdout + result.stderr
        home, src, venv = sandbox["home"], sandbox["home"] / ".forge-src", sandbox["home"] / ".forge-venv"
        assert _log_lines(sandbox["log"]) == [
            f"uv venv --python /usr/bin/python3 {venv}",
            (f"uv pip install --python {venv}/bin/python {src}/nats-core "
            f"{src}/fleet-memory {src}/forge[providers,memory,sidecar] "
            f"{src}/guardkitfactory {src}/guardkit[autobuild] deepagents==0.7.14 deepagents-code==0.1.69"),
            f"uv pip check --python {venv}/bin/python",
            ("python -c import importlib.metadata as m; import forge, guardkit, "
            "guardkit._installer_core, guardkitfactory, deepagents_code, claude_agent_sdk; assert "
            "m.version('deepagents') == '0.7.14'; assert m.version('deepagents-code') == '0.1.69'"),
        ]
        # The copies are the tracked files at each mount's HEAD.
        for name in FACTORY_CHECKOUTS:
            assert (src / name / "pyproject.toml").is_file(), name
            assert (src / f"{name}.commit").read_text().strip() == _git(
                sandbox["estate"] / name, "rev-parse", "HEAD"
            )
        assert (src / "forge" / "forge.langgraph.json").is_file()
        # The runner shells `guardkit`; the venv's console script is guardkit-py.
        assert os.readlink(venv / "bin" / "guardkit") == "guardkit-py"
        assert "bootstrap only" in result.stdout
        assert home == sandbox["home"]

    def test_the_second_run_copies_nothing_and_installs_nothing(self, sandbox):
        first = _bootstrap_only(sandbox)
        assert first.returncode == 0, first.stdout + first.stderr

        second = _bootstrap_only(sandbox)

        assert second.returncode == 0, second.stdout + second.stderr
        lines = _log_lines(sandbox["log"])
        assert len(lines) == 2
        assert lines[0].startswith("uv pip check ")
        assert "deepagents_code" in lines[1]
        assert "venv already at" in second.stdout
        assert "install already matches the copies" in second.stdout
        assert second.stdout.count("copy already at") == len(FACTORY_CHECKOUTS)

    def test_a_moved_commit_copies_and_installs_again_without_a_new_venv(self, sandbox):
        first = _bootstrap_only(sandbox)
        assert first.returncode == 0, first.stdout + first.stderr
        forge = sandbox["estate"] / "forge"
        (forge / "README.md").write_text("moved\n", encoding="utf-8")
        head = _commit_all(forge, "move")

        again = _bootstrap_only(sandbox)

        assert again.returncode == 0, again.stdout + again.stderr
        lines = _log_lines(sandbox["log"])
        assert not any(line.startswith("uv venv") for line in lines)
        assert sum(line.startswith("uv pip install") for line in lines) == 1
        assert sum(line.startswith("uv pip check") for line in lines) == 1
        src = sandbox["home"] / ".forge-src"
        assert (src / "forge.commit").read_text().strip() == head
        assert (src / "forge" / "README.md").is_file()

    def test_untracked_files_never_come_along(self, sandbox):
        # The operator's .env beside forge is exactly what must not reach the
        # sandbox's services: only tracked files are copied.
        forge = sandbox["estate"] / "forge"
        (forge / ".env").write_text(
            "FORGE_NATS_URL=nats://not-a-real-value@example.invalid:4222\n", encoding="utf-8"
        )

        result = _bootstrap_only(sandbox)

        assert result.returncode == 0, result.stdout + result.stderr
        assert not (sandbox["home"] / ".forge-src" / "forge" / ".env").exists()

    def test_a_missing_mount_is_refused_in_one_sentence(self, sandbox):
        shutil.rmtree(sandbox["estate"] / "guardkitfactory")

        result = _bootstrap_only(sandbox)

        assert result.returncode == 2
        assert "guardkitfactory is not mounted at" in result.stdout
        assert _log_lines(sandbox["log"]) == []

    def test_a_mount_that_is_not_a_checkout_is_refused(self, sandbox):
        shutil.rmtree(sandbox["estate"] / "nats-core" / ".git")

        result = _bootstrap_only(sandbox)

        assert result.returncode == 2
        assert "nats-core is not mounted at" in result.stdout


def _wait_for(predicate, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("the services did not reach the expected state in time")


class TestTheBootstrapKeepsBothServicesUp:
    def test_both_start_a_died_one_starts_again_and_the_ledger_is_never_opened(
        self, sandbox, tmp_path
    ):
        die_once = tmp_path / "die-once"
        die_once.mkdir()
        (die_once / "runner").write_text("", encoding="utf-8")
        sandbox["log"].write_text("", encoding="utf-8")
        stdout_path = tmp_path / "bootstrap.out"
        env = _bootstrap_env(
            sandbox,
            FAKE_DIE_ONCE_DIR=str(die_once),
            # The host's ledger path, as the old host unit carried it: it must
            # not reach either service.
            FORGE_DB_PATH="/home/someone/forge-prod-state/.forge/forge.db",
        )
        venv = sandbox["home"] / ".forge-venv"
        src = sandbox["home"] / ".forge-src"

        with stdout_path.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(
                [str(sandbox["repo"] / "deploy" / "sandbox-runner.sh")],
                cwd=sandbox["repo"],
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:

                def runner_started_twice() -> bool:
                    lines = _log_lines(sandbox["log"])
                    return sum(line.startswith("langgraph dev ") for line in lines) >= 2

                _wait_for(runner_started_twice)
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                returncode = proc.wait(timeout=15)

        assert returncode == 0
        lines = _log_lines(sandbox["log"])
        runner_starts = [line for line in lines if line.startswith("langgraph dev ")]
        assert runner_starts == [
            "langgraph dev --config forge.langgraph.json --host 0.0.0.0 --port 8124 "
            "--no-browser --no-reload --allow-blocking"
        ] * 2
        sidecar_starts = [line for line in lines if line.startswith("python -c ") and "serve(" in line]
        assert sidecar_starts == [
            "python -c import os; from forge.deploy_sidecar.service import serve; "
            'serve(host=os.environ["SANDBOX_RUNNER_BIND"], '
            'port=int(os.environ["FORGE_DEPLOY_SIDECAR_PORT"]))'
        ]
        # What the services could see: the venv's guardkit, the receipts root,
        # the mission harness, the ports — and no ledger path at all.
        runner_env = [line for line in lines if line.startswith("env langgraph ")][0]
        assert "FORGE_DB_PATH=unset" in runner_env
        assert f"FORGE_GUARDKIT_PATH={venv}/bin/guardkit" in runner_env
        assert f"FORGE_RECEIPTS_DIR={sandbox['receipts']}" in runner_env
        assert "GUARDKIT_HARNESS=langgraph" in runner_env
        assert f"PWD={src}/forge" in runner_env
        sidecar_env = [line for line in lines if line.startswith("env python ")][0]
        assert "FORGE_DB_PATH=unset" in sidecar_env
        assert "SIDECAR_PORT=8125 BIND=0.0.0.0" in sidecar_env
        stdout = stdout_path.read_text(encoding="utf-8")
        assert "the build runner exited 1; starting it again in 0s" in stdout
        assert "FORGE_DB_PATH was set in this environment; unsetting it" in stdout
        assert "asked to stop; stopping both services" in stdout
        # Names only, never a value.
        assert "forge-prod-state" not in stdout


class TestUvsDownloadSwitchStaysWithTheVenvCommand:
    """The switch that forbids uv to fetch an interpreter belongs to the one
    command that makes the factory's own venv, and to nothing else.

    Why it matters (seen in api_test's sandbox on 2026-09-08): the bootstrap
    used to export that switch, so the two services it starts inherited it, and
    so did guardkit's work leg beneath them. The work leg pins a repository's
    own build venv to the floor of that repository's ``requires-python`` — 3.11
    for api_test — which the sandbox's Python (3.14) is newer than; uv was
    forbidden to fetch a 3.11, and every work leg failed for want of an
    interpreter. The attended cure was a setting in the sandbox; the cure here
    is that the bootstrap keeps the switch to itself.
    """

    def test_the_venv_command_carries_it_and_the_installs_run_without_it(self, sandbox):
        result = _bootstrap_only(sandbox)

        assert result.returncode == 0, result.stdout + result.stderr
        assert _log_lines(sandbox["uv_env_log"]) == [
            "uv venv UV_PYTHON_DOWNLOADS=never",
            "uv pip UV_PYTHON_DOWNLOADS=unset",
            "uv pip UV_PYTHON_DOWNLOADS=unset",
        ]

    def test_neither_started_service_carries_it(self, sandbox, tmp_path):
        sandbox["log"].write_text("", encoding="utf-8")
        stdout_path = tmp_path / "services.out"

        with stdout_path.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(
                [str(sandbox["repo"] / "deploy" / "sandbox-runner.sh")],
                cwd=sandbox["repo"],
                env=_bootstrap_env(sandbox),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:

                def both_reported() -> bool:
                    lines = _log_lines(sandbox["log"])
                    return any(line.startswith("env langgraph ") for line in lines) and any(
                        line.startswith("env python ") for line in lines
                    )

                _wait_for(both_reported)
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=15)

        lines = _log_lines(sandbox["log"])
        runner_env = [line for line in lines if line.startswith("env langgraph ")][0]
        sidecar_env = [line for line in lines if line.startswith("env python ")][0]
        assert "UV_PYTHON_DOWNLOADS=unset" in runner_env
        assert "UV_PYTHON_DOWNLOADS=unset" in sidecar_env


# ---------------------------------------------------------------------------
# The profile's six settings load, are checked, and are threaded when set
# ---------------------------------------------------------------------------


def _profile(sandbox_block: dict):
    return parse_deploy_profile(
        {
            "env_id": "local",
            "compose": {"file": "docker-compose.yml", "script": "deploy/sandbox-deploy.sh"},
            "cwd": "/home/rich/Projects/appmilla_github/api_test",
            "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
            "sandbox": sandbox_block,
        }
    )


FACTORY_BLOCK = {
    "name": "api-test-deploy",
    "memory": "6g",
    "cpus": 4,
    "publish": ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"],
    "allow_network": ["pypi.org"],
    "sidecar_publish": "127.0.0.1:8925:8125",
    "runner_publish": "127.0.0.1:8924:8124",
    "env_file": "/run/user/1000/forge-sandbox/api-test-deploy.env",
    "forge_path": "/home/rich/Projects/appmilla_github/forge",
    "guardkit_path": "/home/rich/Projects/appmilla_github/guardkit",
    "receipts_path": "/home/rich/forge-state/receipts",
}


class TestTheProfileSettings:
    def test_the_six_load(self):
        sandbox = _profile(FACTORY_BLOCK).sandbox
        assert sandbox is not None
        assert sandbox.sidecar_publish == "127.0.0.1:8925:8125"
        assert sandbox.runner_publish == "127.0.0.1:8924:8124"
        assert sandbox.env_file == "/run/user/1000/forge-sandbox/api-test-deploy.env"
        assert sandbox.forge_path == "/home/rich/Projects/appmilla_github/forge"
        assert sandbox.guardkit_path == "/home/rich/Projects/appmilla_github/guardkit"
        assert sandbox.receipts_path == "/home/rich/forge-state/receipts"

    def test_absent_means_none_and_today_s_shape(self):
        sandbox = _profile({"name": "a-deploy"}).sandbox
        assert sandbox is not None
        assert all(
            getattr(sandbox, field) is None
            for field in (
                "sidecar_publish",
                "runner_publish",
                "env_file",
                "forge_path",
                "guardkit_path",
                "receipts_path",
            )
        )

    @pytest.mark.parametrize("only", ["sidecar_publish", "runner_publish"])
    def test_one_service_port_without_the_other_is_refused(self, only):
        with pytest.raises(DeployProfileError, match="go together"):
            _profile({"name": "a-deploy", only: "127.0.0.1:8925:8125"})

    @pytest.mark.parametrize("rule", ["8925", "0:8125", "127.0.0.1:8925", "a:b:c", ""])
    def test_a_bad_service_port_is_refused(self, rule):
        with pytest.raises(DeployProfileError, match="sandbox.sidecar_publish"):
            _profile(
                {
                    "name": "a-deploy",
                    "sidecar_publish": rule,
                    "runner_publish": "127.0.0.1:8924:8124",
                }
            )

    @pytest.mark.parametrize("field", ["env_file", "forge_path", "guardkit_path", "receipts_path"])
    @pytest.mark.parametrize("bad", ["", "   ", 7, ["/a"], "/a,/b"])
    def test_a_bad_path_is_refused(self, field, bad):
        with pytest.raises(DeployProfileError, match=f"sandbox.{field}"):
            _profile({"name": "a-deploy", field: bad})

    def test_they_are_threaded_only_when_set(self):
        env = sandbox_env(_profile(FACTORY_BLOCK))
        assert all(name in env for name in FIVE)
        assert env["SANDBOX_SIDECAR_PUBLISH"] == "127.0.0.1:8925:8125"
        assert env["SANDBOX_RUNNER_PUBLISH"] == "127.0.0.1:8924:8124"
        assert env["SANDBOX_ENV_FILE"] == "/run/user/1000/forge-sandbox/api-test-deploy.env"
        assert env["SANDBOX_FORGE_PATH"] == "/home/rich/Projects/appmilla_github/forge"
        assert env["SANDBOX_GUARDKIT_PATH"] == "/home/rich/Projects/appmilla_github/guardkit"
        assert env["SANDBOX_RECEIPTS_PATH"] == "/home/rich/forge-state/receipts"

    def test_without_them_the_environment_is_exactly_the_five(self):
        env = sandbox_env(_profile({"name": "a-deploy"}))
        assert tuple(env) == FIVE

    def test_the_two_ports_alone_thread_only_the_two_ports(self):
        env = sandbox_env(
            _profile(
                {
                    "name": "a-deploy",
                    "sidecar_publish": "127.0.0.1:8925:8125",
                    "runner_publish": "127.0.0.1:8924:8124",
                }
            )
        )
        assert set(env) == set(FIVE) | {"SANDBOX_SIDECAR_PUBLISH", "SANDBOX_RUNNER_PUBLISH"}

    def test_they_reach_the_steps_that_run_a_script(self):
        from datetime import UTC, datetime

        runbook = build_deploy_runbook(
            _profile(FACTORY_BLOCK),
            runbook_id="deploy-1",
            target="local",
            now=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
            compose_extra_env={"CANDIDATE": "1"},
        )
        for step in runbook.steps:
            if step.step_type in ("deploy_compose", "health_check"):
                env = step.params["extra_env"]
                assert all(name in env for name in SIX), step.step_type
                assert env["SANDBOX_RUNNER_PUBLISH"] == "127.0.0.1:8924:8124"


# ---------------------------------------------------------------------------
# register-repo emits the settings for this repository
# ---------------------------------------------------------------------------


class TestRegisterRepoEmitsTheSettings:
    def test_the_service_ports_come_from_the_app_port(self):
        # The spec's example: api_test on 8901 has its runner on 8924 and its
        # sidecar on 8925.
        assert register_repo.service_ports_for(8901) == (8924, 8925)
        assert register_repo.service_ports_for(8911) == (8934, 8935)
        assert register_repo.MAX_DEPLOY_PORT == 65511

    def test_the_profile_names_the_ports_the_mounts_and_the_receipts_root(
        self, tmp_path, monkeypatch
    ):
        receipts = tmp_path / "receipts"
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))
        repo = tmp_path / "estate" / "bench-one"
        repo.mkdir(parents=True)

        rendered = register_repo.render_deploy_files(name="bench-one", repo=repo, app_port=8911)
        profile = parse_deploy_profile(yaml.safe_load(rendered["deploy/profile.yaml"]))

        sandbox = profile.sandbox
        assert sandbox is not None
        assert sandbox.name == "bench-one-deploy"
        assert sandbox.publish == ("127.0.0.1:8911:8911", "127.0.0.1:8912:8912")
        assert sandbox.sidecar_publish == "127.0.0.1:8935:8125"
        assert sandbox.runner_publish == "127.0.0.1:8934:8124"
        assert sandbox.forge_path == str(tmp_path / "estate" / "forge")
        assert sandbox.guardkit_path == str(tmp_path / "estate" / "guardkit")
        assert sandbox.receipts_path == str(receipts)
        # The environment file is named once it exists: the sandbox is created
        # with it, so the line is there to fill in, commented out.
        assert sandbox.env_file is None
        assert '# env_file: "/run/user/1000/forge-sandbox/bench-one-deploy.env"' in (
            rendered["deploy/profile.yaml"]
        )

    def test_named_mounts_and_receipts_root_win_over_the_defaults(self, tmp_path):
        repo = tmp_path / "estate" / "bench-one"
        repo.mkdir(parents=True)

        rendered = register_repo.render_deploy_files(
            name="bench-one",
            repo=repo,
            app_port=8911,
            forge_path=tmp_path / "elsewhere" / "forge",
            guardkit_path=tmp_path / "elsewhere" / "guardkit",
            receipts_path=tmp_path / "elsewhere" / "receipts",
        )
        sandbox = parse_deploy_profile(yaml.safe_load(rendered["deploy/profile.yaml"])).sandbox

        assert sandbox is not None
        assert sandbox.forge_path == str(tmp_path / "elsewhere" / "forge")
        assert sandbox.guardkit_path == str(tmp_path / "elsewhere" / "guardkit")
        assert sandbox.receipts_path == str(tmp_path / "elsewhere" / "receipts")

    def test_the_bootstrap_is_shipped_byte_for_byte_as_the_fifth_file(self, tmp_path):
        repo = tmp_path / "estate" / "bench-one"
        repo.mkdir(parents=True)

        rendered = register_repo.render_deploy_files(name="bench-one", repo=repo, app_port=8911)

        assert register_repo.DEPLOY_FILES == (
            "deploy/profile.yaml",
            "deploy/sandbox-deploy.sh",
            "deploy/sandbox-runner.sh",
            "deploy/deploy.sh",
            "deploy/docker-compose.candidate.yml",
        )
        assert set(rendered) == set(register_repo.DEPLOY_FILES)
        assert rendered["deploy/sandbox-runner.sh"] == (TEMPLATES / "sandbox-runner.sh").read_text(
            encoding="utf-8"
        )
        assert rendered["deploy/sandbox-deploy.sh"] == (TEMPLATES / "sandbox-deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "@@" not in rendered["deploy/sandbox-runner.sh"]

    def test_the_written_profile_loads_through_the_deploy_step_s_own_loader(self, tmp_path):
        from forge.deploy.profile import load_deploy_profile

        repo = tmp_path / "estate" / "bench-one"
        (repo / "deploy").mkdir(parents=True)
        rendered = register_repo.render_deploy_files(name="bench-one", repo=repo, app_port=8911)
        (repo / "deploy" / "profile.yaml").write_text(rendered["deploy/profile.yaml"], encoding="utf-8")

        profile = load_deploy_profile(repo / "deploy" / "profile.yaml")

        assert profile.sandbox is not None
        assert profile.sandbox.runner_publish == "127.0.0.1:8934:8124"
        assert "sandbox" not in profile.extra


# ---------------------------------------------------------------------------
# The host unit that holds the bootstrap open
# ---------------------------------------------------------------------------


def _unit_lines(name: str) -> list[str]:
    text = (REPO_ROOT / "ops" / "systemd" / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _unit_comments(name: str) -> str:
    """Everything the unit file says to a person reading it, comments only."""
    text = (REPO_ROOT / "ops" / "systemd" / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))


def _one_line(prefix: str) -> str:
    """The unit's single line starting with ``prefix``, value only."""
    lines = [line for line in _unit_lines(RUNNER_UNIT) if line.startswith(prefix)]
    assert len(lines) == 1, f"expected exactly one {prefix} line, found {lines}"
    return lines[0].split("=", 1)[1]


def _sandbox_argv(value: str, instance: str) -> list[str]:
    """An Exec line's command as argv, with systemd's prefixes and %i resolved."""
    return shlex.split(value.lstrip("-@+!:").replace("%i", instance))


RUNNER_UNIT = "forge-sandbox-runner@.service"

#: The start line, which this lane must leave exactly as it found it.
EXEC_START = "/usr/bin/sbx exec %i deploy/sandbox-runner.sh"

#: The true answer to "what does the bootstrap's stop return when there is
#: nothing to stop", in the words both documents use for it. The bootstrap is
#: ``deploy/sandbox-runner.sh`` in the repository the sandbox runs, and its stop
#: mode exits 0 whether or not it found anything to end; its own tests pin that.
IDLE_ANSWER = "it exits 0 when there is nothing to stop"

#: Wordings that answered the same question the other way round, or that gave
#: the leading '-' on the ExecStop line the idle case as its reason. Both
#: documents carried one of these beside the true sentence, so the lane building
#: against this file had two opposite answers to choose from.
CONTRADICTIONS = (
    "nothing left alive",
    "has nothing to do and says so with a non-zero exit",
    "says so with a non-zero exit",
    "the case that needs forgiving is a running unit",
)

#: Every wording, right or wrong, that answers that question. A document must
#: contain exactly one of these, in the place a reader looks for it; any other
#: passage that used to answer it now points at that place instead.
IDLE_ANSWERS = (IDLE_ANSWER,) + CONTRADICTIONS


def _readme_text() -> str:
    return (REPO_ROOT / "ops" / "systemd" / "README.md").read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """A document's words as one line, so a sentence wrapped across several
    lines — or across comment markers — reads the same as one written flat."""
    return re.sub(r"\s+", " ", text.replace("#", " "))


class TestTheRunnerUnit:
    def test_it_is_shaped_like_the_keeper(self):
        runner = _unit_lines(RUNNER_UNIT)
        keeper = _unit_lines("forge-sandbox-keeper@.service")

        assert f"ExecStart={EXEC_START}" in runner
        assert "Restart=always" in runner
        assert "KillMode=process" in runner
        assert "Type=simple" in runner
        assert "WantedBy=default.target" in runner
        path_lines = [line for line in runner if line.startswith("Environment=PATH=")]
        assert path_lines == [line for line in keeper if line.startswith("Environment=PATH=")]

    def test_it_runs_nothing_of_the_repository_on_the_host(self):
        # Every command the unit runs goes through the sandbox door: there is
        # no line here that runs any of the repository's own code on the host.
        runner = _unit_lines(RUNNER_UNIT)
        execs = [line for line in runner if line.startswith("Exec")]
        assert execs == [
            f"ExecStart={EXEC_START}",
            "ExecStop=-/usr/bin/sbx exec %i deploy/sandbox-runner.sh stop",
        ]
        for line in execs:
            argv = _sandbox_argv(line.split("=", 1)[1], "some-sandbox")
            assert argv[:2] == ["/usr/bin/sbx", "exec"]

    def test_the_start_line_is_untouched_by_the_stop_lane(self):
        assert _one_line("ExecStart=") == EXEC_START

    def test_stopping_it_reaches_inside_the_same_sandbox(self):
        # The stop has to end the work inside the sandbox, not just the client
        # on the host holding the session open.
        stop = _one_line("ExecStop=")
        assert "sbx exec %i" in stop
        start_argv = _sandbox_argv(_one_line("ExecStart="), "api-test-deploy")
        stop_argv = _sandbox_argv(stop, "api-test-deploy")
        # Same program, same door, same sandbox, same script — one word more.
        door = ["/usr/bin/sbx", "exec", "api-test-deploy"]
        assert start_argv[:3] == door
        assert stop_argv[:3] == door
        assert stop_argv[3] == start_argv[3] == "deploy/sandbox-runner.sh"
        assert stop_argv[4:] == ["stop"]

    def test_the_leading_dash_is_there_for_the_door_failing(self):
        # systemd's '-' prefix. What it forgives is the door itself failing —
        # the sandbox removed, sbx unable to reach the daemon, a session that
        # will not open — so a unit that cannot get in there is not left failed
        # on the host. It is NOT there for an idle sandbox: the bootstrap
        # succeeds in that case, which is why this test no longer says so.
        assert _one_line("ExecStop=").startswith("-")

    def test_the_stop_is_bounded_in_time(self):
        seconds = int(_one_line("TimeoutStopSec="))
        assert 0 < seconds <= 300

    def test_the_unit_says_in_plain_words_why_the_stop_is_there(self):
        comments = _unit_comments(RUNNER_UNIT)
        assert "WHY ExecStop" in comments
        assert "2026-09-11" in comments
        # The reason itself: a client on the host, the work inside the sandbox.
        assert "CLIENT on the host" in comments
        assert "inside the sandbox" in comments
        # And which script and mode the unit calls, so nobody has to guess.
        assert "deploy/sandbox-runner.sh stop" in comments
        # And one sentence of what it cost.
        assert "supervisors had piled up" in comments

    def test_the_unit_warns_that_the_bootstrap_must_know_the_stop_word_first(self):
        # An older bootstrap ignores the word and runs its start path, so a stop
        # against it would add a supervisor and then hang. The file has to say so.
        comments = _unit_comments(RUNNER_UNIT)
        assert "BEFORE INSTALLING THIS FILE" in comments
        assert "ignore the word" in comments
        assert "TWO supervisors" in comments

    def test_the_unit_says_systemd_runs_the_stop_by_itself(self):
        # The danger is not only a person typing "stop": with Restart=always,
        # systemd runs ExecStop itself on every automatic restart, so against an
        # old bootstrap the pile-up is unattended and repeating. And against the
        # right bootstrap the two services now bounce on a dropped session where
        # they used to keep serving. Both have to be written down.
        comments = _unit_comments(RUNNER_UNIT)
        assert "no protection" in comments
        assert "Restart=always with RestartSec=5" in comments
        assert "on a loop" in comments
        assert "BOUNCE where they used to keep serving" in comments

    def test_the_readme_says_why_stopping_has_to_reach_inside(self):
        readme = (REPO_ROOT / "ops" / "systemd" / "README.md").read_text(encoding="utf-8")
        assert "ExecStop=-/usr/bin/sbx exec %i deploy/sandbox-runner.sh stop" in readme
        assert "Stopping it has to reach inside the sandbox (2026-09-11)" in readme

    def test_the_readme_tells_the_operator_what_to_check_before_installing(self):
        readme = (REPO_ROOT / "ops" / "systemd" / "README.md").read_text(encoding="utf-8")
        assert "What the operator has to check before installing this unit." in readme
        assert "replace that repository's bootstrap first, then" in readme
        # And that not typing the word is no protection, because systemd runs
        # the stop itself on every automatic restart — plus what the unit now
        # does against a correct bootstrap, which is a real change of behaviour.
        assert "**Deciding never to type `stop` is not a way round that.**" in readme
        assert "unattended and repeating" in readme
        assert "**What changes against the correct bootstrap.**" in readme

    def test_the_readme_says_how_to_install_it(self):
        readme = (REPO_ROOT / "ops" / "systemd" / "README.md").read_text(encoding="utf-8")
        assert "cp ops/systemd/forge-sandbox-runner@.service ~/.config/systemd/user/" in readme


class TestTheIdleAnswerIsSaidOnceAndSaidRight:
    """What the bootstrap's stop returns when there is nothing to stop.

    The neighbouring lane builds against these two documents, so they have to
    give one answer. The answer is exit 0: ``deploy/sandbox-runner.sh``'s stop
    mode says "Ending what is already ended is a success: this exits 0 whether
    or not it found anything to end", and fails only on a process that will not
    die (4) or a supervisor that cannot be ended from underneath itself (5).
    Each document must state that once, where a reader looks for it, and the
    passage about the leading '-' must not state it again the other way round.
    """

    def test_the_unit_answers_the_question_exactly_once(self):
        prose = _prose((REPO_ROOT / "ops" / "systemd" / RUNNER_UNIT).read_text(encoding="utf-8"))
        answers = sum(prose.count(wording) for wording in IDLE_ANSWERS)
        assert answers == 1, (
            "the unit file must answer 'what does the stop return when there is "
            f"nothing to stop' exactly once; it answers it {answers} times"
        )
        assert prose.count(IDLE_ANSWER) == 1

    def test_the_readme_answers_the_question_exactly_once(self):
        prose = _prose(_readme_text())
        answers = sum(prose.count(wording) for wording in IDLE_ANSWERS)
        assert answers == 1, (
            "the README must answer 'what does the stop return when there is "
            f"nothing to stop' exactly once; it answers it {answers} times"
        )
        assert prose.count(IDLE_ANSWER) == 1

    @pytest.mark.parametrize("wording", CONTRADICTIONS)
    def test_neither_document_still_says_the_opposite(self, wording):
        # The absence is the point. A document that says both answers passes a
        # test that only looks for the right one, which is how this survived.
        unit = _prose((REPO_ROOT / "ops" / "systemd" / RUNNER_UNIT).read_text(encoding="utf-8"))
        readme = _prose(_readme_text())
        assert wording not in unit, f"the unit file still says {wording!r}"
        assert wording not in readme, f"the README still says {wording!r}"

    def test_the_unit_explains_the_dash_by_the_door_failing(self):
        prose = _prose(_unit_comments(RUNNER_UNIT))
        assert "the DOOR itself failing" in prose
        assert "the sandbox has been removed" in prose
        assert "cannot reach the daemon" in prose
        assert "the session will not open" in prose
        # And that an inactive unit is not the case being forgiven, because
        # systemd does not reach ExecStop for one at all.
        assert "never runs ExecStop for a unit that is already inactive" in prose

    def test_the_readme_explains_the_dash_by_the_door_failing(self):
        prose = _prose(_readme_text())
        assert "the **door** failing" in prose
        assert "the sandbox has been removed" in prose
        assert "cannot reach the daemon" in prose
        assert "the session will not open" in prose
        assert "does not run `ExecStop` for a unit that is already inactive" in prose


# ---------------------------------------------------------------------------
# The stop, driven for real: the unit's own ExecStop line against a real
# process this test started itself
# ---------------------------------------------------------------------------
#
# The command line comes from the unit file, unedited except for the two things
# that would reach the estate: `%i` becomes a made-up sandbox name, and the
# program `/usr/bin/sbx` becomes a stand-in script in a temporary folder. Every
# argument after it is the unit's own. The thing being stopped is a real
# `sleep` this test started in a temporary folder, standing in for the bootstrap
# inside the sandbox; the stop really kills it.

FAKE_SBX_DOOR = """#!/usr/bin/env bash
# Stands in for /usr/bin/sbx. Records the argv, checks it is an `exec` into the
# sandbox it expects, and runs the named script from the fake repository.
set -euo pipefail
printf '%s\\n' "$*" >>"${FAKE_SBX_LOG}"
[[ "$1" == "exec" ]] || { echo "not an exec: $1" >&2; exit 64; }
[[ "$2" == "${EXPECTED_SANDBOX}" ]] || { echo "wrong sandbox: $2" >&2; exit 65; }
script="$3"
shift 3
exec "${FAKE_REPO}/${script}" "$@"
"""

FAKE_BOOTSTRAP_WITH_A_STOP_MODE = """#!/usr/bin/env bash
# Stands in for the repository's deploy/sandbox-runner.sh, with only the stop
# mode the unit calls: end what is running and say nothing is wrong when
# nothing is.
set -euo pipefail
if [[ "${1:-run}" != "stop" ]]; then
  echo "this stand-in only knows how to stop" >&2
  exit 3
fi
if [[ -f "${FAKE_PIDFILE}" ]]; then
  kill "$(cat "${FAKE_PIDFILE}")" 2>/dev/null || true
  rm -f "${FAKE_PIDFILE}"
fi
exit 0
"""


@pytest.fixture
def stop_door(tmp_path: Path) -> dict:
    """A stand-in sandbox door and bootstrap, and the unit's own stop argv."""
    sandbox_name = "made-up-deploy"
    bin_dir = tmp_path / "bin"
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    bin_dir.mkdir()
    _write_fake(bin_dir, "sbx", FAKE_SBX_DOOR)
    _write_fake(repo / "deploy", "sandbox-runner.sh", FAKE_BOOTSTRAP_WITH_A_STOP_MODE)

    argv = _sandbox_argv(_one_line("ExecStop="), sandbox_name)
    assert argv[0] == "/usr/bin/sbx"
    argv[0] = str(bin_dir / "sbx")

    log = tmp_path / "sbx.log"
    pidfile = tmp_path / "supervisor.pid"
    env = {
        **os.environ,
        "FAKE_SBX_LOG": str(log),
        "FAKE_REPO": str(repo),
        "FAKE_PIDFILE": str(pidfile),
        "EXPECTED_SANDBOX": sandbox_name,
    }
    return {"argv": argv, "env": env, "log": log, "pidfile": pidfile, "dir": tmp_path}


class TestTheStopCommandActuallyStops:
    def test_it_ends_a_running_supervisor(self, stop_door):
        # A real process of this test's own making, in a temporary folder,
        # standing in for the bootstrap that keeps running inside the sandbox.
        supervisor = subprocess.Popen(
            ["sleep", "300"], cwd=stop_door["dir"], stdout=subprocess.DEVNULL
        )
        try:
            stop_door["pidfile"].write_text(str(supervisor.pid), encoding="utf-8")
            assert supervisor.poll() is None

            done = subprocess.run(
                stop_door["argv"],
                env=stop_door["env"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert done.returncode == 0, done.stderr
            # It really died, rather than the command merely claiming success.
            assert supervisor.wait(timeout=10) != 0
            assert supervisor.poll() is not None
            # And it was asked through the sandbox door, in the unit's words.
            assert _log_lines(stop_door["log"]) == [
                "exec made-up-deploy deploy/sandbox-runner.sh stop"
            ]
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=10)

    def test_stopping_what_is_already_stopped_is_not_a_failure(self, stop_door):
        # No pidfile: nothing is running in there. Stopping must still succeed,
        # or `systemctl --user stop` would leave the unit failed.
        assert not stop_door["pidfile"].exists()
        done = subprocess.run(
            stop_door["argv"],
            env=stop_door["env"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------
# Rule 72's inventory: every cited line exists and still says what is quoted
# ---------------------------------------------------------------------------

INVENTORY_HEADING = (
    "## What the runner writes to the ledger today "
    "(must move behind the bus for the sandbox runner — L3)"
)

CITATION = re.compile(r"^- `([^`:]+):(\d+)` — `([^`]+)`", re.MULTILINE)


def _inventory_section() -> str:
    readme = (REPO_ROOT / "ops" / "systemd" / "README.md").read_text(encoding="utf-8")
    assert INVENTORY_HEADING in readme
    section = readme.split(INVENTORY_HEADING, 1)[1]
    next_heading = section.find("\n## ")
    return section if next_heading < 0 else section[:next_heading]


class TestTheLedgerInventory:
    def test_every_cited_line_exists_and_says_what_is_quoted(self):
        citations = CITATION.findall(_inventory_section())
        assert len(citations) >= 10
        for path, line_no, quoted in citations:
            lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
            assert int(line_no) <= len(lines), f"{path}:{line_no} is past the end of the file"
            line = lines[int(line_no) - 1]
            assert quoted in line, f"{path}:{line_no} no longer says {quoted!r}: {line!r}"

    def test_every_ledger_touch_in_the_runner_is_cited(self):
        # The inventory is the whole truth: any line that imports sqlite,
        # resolves the ledger path or queries the builds table must be in it.
        runner = REPO_ROOT / "src" / "forge" / "subagents" / "autobuild_runner.py"
        touches = {
            index + 1
            for index, line in enumerate(runner.read_text(encoding="utf-8").splitlines())
            if re.search(r"^import sqlite3|sqlite3\.|resolve_db_path|FROM builds", line)
        }
        cited = {
            int(line_no)
            for path, line_no, _ in CITATION.findall(_inventory_section())
            if path == "src/forge/subagents/autobuild_runner.py"
        }
        assert touches, "the grep found nothing — the pattern is wrong"
        assert touches <= cited, f"uncited ledger touches at lines {sorted(touches - cited)}"

    def test_it_says_the_runner_writes_nothing_and_what_l3_must_move(self):
        section = _inventory_section()
        assert "writes it nowhere" in section
        assert "forge-prod is its only writer" in section
        assert "deploy/sandbox-runner.sh` unsets\n`FORGE_DB_PATH`" in section


class TestConsolidationRuntimeReuse:
    def test_missing_dcode_rejects_reuse_and_reinstalls(self, sandbox, tmp_path):
        assert _bootstrap_only(sandbox).returncode == 0
        missing = tmp_path / "missing-dcode"
        missing.touch()
        result = _bootstrap_only(sandbox, FAKE_BROKEN_RUNTIME=str(missing))
        assert result.returncode == 0, result.stdout + result.stderr
        lines = _log_lines(sandbox["log"])
        assert sum(line.startswith("uv pip install ") for line in lines) == 1
        assert not missing.exists()
        assert "deepagents-code==0.1.69" in "\n".join(lines)

    def test_changed_dependency_metadata_invalidates_reuse(self, sandbox):
        assert _bootstrap_only(sandbox).returncode == 0
        metadata = sandbox["home"] / ".forge-src/guardkitfactory/pyproject.toml"
        metadata.write_text(metadata.read_text() + '\ndependencies = ["changed"]\n')
        result = _bootstrap_only(sandbox)
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(line.startswith("uv pip install ") for line in _log_lines(sandbox["log"]))

    def test_changed_interpreter_retains_old_environment_and_rebuilds(self, sandbox):
        assert _bootstrap_only(sandbox).returncode == 0
        python = sandbox["home"] / ".forge-venv/bin/python"
        python.write_text('#!/bin/sh\nprintf "old-interpreter\\n"\n')
        python.chmod(0o755)
        result = _bootstrap_only(sandbox)
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(line.startswith("uv venv ") for line in _log_lines(sandbox["log"]))
        assert len(list(sandbox["home"].glob(".forge-venv.previous.*"))) == 1


class TestConsolidationLifecycle:
    def test_stop_never_installs_even_without_mounts(self, sandbox):
        shutil.rmtree(sandbox["estate"] / "guardkitfactory")
        result = subprocess.run(
            [str(sandbox["repo"] / "deploy/sandbox-runner.sh"), "stop"],
            env=_bootstrap_env(sandbox), capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert _log_lines(sandbox["log"]) == []
        assert not (sandbox["home"] / ".forge-venv").exists()

    def test_repeated_start_has_one_supervisor_and_stop_ends_descendants(self, sandbox, tmp_path):
        script = str(sandbox["repo"] / "deploy/sandbox-runner.sh")
        children = tmp_path / "children"
        fake = sandbox["fake_bin"] / "fake-service"
        fake.write_text(fake.read_text().replace(
            "exec sleep 300", 'sleep 300 &\nprintf "%s\\n" "$!" >> "$FAKE_CHILDREN"\nwait'
        ))
        env = _bootstrap_env(sandbox, FAKE_CHILDREN=str(children))
        with (tmp_path / "runner.log").open("w") as log:
            proc = subprocess.Popen([script], env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                _wait_for(lambda: children.exists() and len(children.read_text().splitlines()) == 2)
                before = _log_lines(sandbox["log"])
                again = subprocess.run([script], env=env, capture_output=True, text=True, timeout=10)
                assert again.returncode == 0
                assert "already running" in again.stdout
                assert _log_lines(sandbox["log"]) == before
                child_pids = [int(pid) for pid in children.read_text().splitlines()]
                stopped = subprocess.run([script, "stop"], env=env, capture_output=True, text=True, timeout=15)
                assert stopped.returncode == 0, stopped.stdout + stopped.stderr
                assert proc.wait(timeout=10) == 0
                for pid in child_pids:
                    stat = Path(f"/proc/{pid}/stat")
                    assert not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z ")
                assert _log_lines(sandbox["log"]) == before
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=15)

    def test_wrong_process_record_never_signals_unrelated_process(self, sandbox):
        import hashlib
        script = str(sandbox["repo"] / "deploy/sandbox-runner.sh")
        state = sandbox["home"] / ".forge-runner" / hashlib.sha256(str(sandbox["repo"]).encode()).hexdigest()
        state.mkdir(parents=True)
        other = subprocess.Popen(["sleep", "60"])
        try:
            token = Path(f"/proc/{other.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            (state / "supervisor").write_text(f"{other.pid} {token}\n")
            result = subprocess.run([script, "stop"], env=_bootstrap_env(sandbox), capture_output=True, text=True, timeout=10)
            assert result.returncode == 0
            assert "stale" in result.stdout
            assert other.poll() is None
        finally:
            other.terminate()
            other.wait(timeout=10)
