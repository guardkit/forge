"""A project's own programs run without the factory's credentials.

Design: "Project commands run without the factory's credentials" (13 October
2026). The factory runs a project's own programs — its declared test, its
live-gate driver, its deploy script and checks, its owned deploy — and until
this change each of them was handed the factory's whole named list, which
carries the model key, the bus password and the memory store's address. A
project's code is written by local models; it has no business seeing any of
them, and a library that reads ``OPENAI_API_KEY`` by default would even spend
the factory's key.

So a project program's environment is built from nothing by
:func:`forge.launch_environment.build_project_command_env`: ``PATH``, a private
``HOME`` and ``TMPDIR`` under ``<repository>/.git/forge-project-home``, the
locale, the proxy settings, the names the project declared, the values the
factory sets for that one command, the review preview's issued values, the
profile-allowed overlay, a proxy check and the run-owner marker last.

Every test here plants SENTINEL values for the factory's credentials in THIS
process — which is the process the helper service, the coordinator's readers
and the deploy executor run in for these tests — and then looks for them in
what a real child process was given. Nothing live is touched: every service is
a test server on 127.0.0.1 on a port the kernel picks, every program is a
small script written here, and no value printed anywhere is a real secret.

The factory's OWN launches (the merge, the legs, the checks, which start the
build system) keep their credentials; two positive controls prove it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

from forge.config.models import ForgeConfig

REPO = "org/shop"
FEATURE = "FEAT-AB12"
BUILD = "build-FEAT-AB12-20261013"

#: The factory's credentials, as the design lists them. Each value is a
#: string written here, shaped like a fresh link token (URL-safe and longer
#: than 32 characters) so that a refusal of one passed off as an issued value
#: is a refusal for where it came from, never for its shape.
SENTINELS: dict[str, str] = {
    name: f"SENTINEL_{name}_{'q' * 24}"
    for name in (
        "GUARDKIT_NATS_PASSWORD",
        "FLEET_MEMORY_PG_DSN",
        "FLEET_MEMORY_NATS_URL",
        "FORGE_NATS_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "FORGE_DB_PATH",
        "SSH_AUTH_SOCK",
    )
}

#: The marker every sentinel value carries, so one search finds any of them.
MARK = "SENTINEL_"

#: A setting the project declares by name in ``launch.settings``.
DECLARED = "STANDIN_SETTING"

#: A value the profile's ``live_gate.env`` allows a request to carry.
ALLOWED_NAME = "GATE_BASE_URL"
ALLOWED_VALUE = "http://127.0.0.1:1/the-candidate"

#: A freshly-made link token, fixed so the tests are repeatable.
TOKEN = "Tk" + "a1B2_c3-" * 5

SLOT = {
    "port": 8950,
    "internal_port": 18950,
    "public_url": "https://preview.example.test:8950",
}

#: The loader the helper asks for a repository's declared test command. The
#: real one lives in the build system's own package; a stand-in under the
#: same name reads the repository's own file.
LOADER_NAME = "guardkit.orchestrator.toolchain_declaration"


# ---------------------------------------------------------------------------
# A project whose every program writes down what it was given
# ---------------------------------------------------------------------------


def _writer(path: Path, label: str, *, envelope: bool = False) -> None:
    """A program that writes its whole environment to ``env-<label>.json``.

    The file goes in the program's working directory: its environment is the
    thing under test, so it cannot be told the path through one.
    """
    lines = [
        "#!/usr/bin/env python3",
        "import json, os, pathlib",
        f"pathlib.Path(os.getcwd(), 'env-{label}.json').write_text(",
        "    json.dumps(dict(os.environ)), encoding='utf-8')",
    ]
    if envelope:
        lines.append(
            "print(json.dumps({'verdict': 'pass', 'run_id': 'r1', 'gates': []}))"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _a_project(
    root: Path,
    *,
    preview: bool = True,
    gate_env: dict[str, str] | None = None,
) -> Path:
    """A committed repository with a deploy profile and a declared test."""
    for label, envelope in (
        ("deploy", False),
        ("check", False),
        ("journey", True),
        ("test", False),
    ):
        _writer(root / "deploy" / f"{label}.sh", label, envelope=envelope)
    profile: dict[str, Any] = {
        "env_id": "shop",
        "compose": {"file": "compose.yml", "script": "deploy/deploy.sh"},
        "health_checks": [{"cmd": "deploy/check.sh"}],
        "live_gate": {
            "driver": ["deploy/journey.sh"],
            "timeout_seconds": 60,
            "env": dict(gate_env if gate_env is not None else {ALLOWED_NAME: ""}),
        },
    }
    if preview:
        profile["preview"] = {"enabled": True, "slots": [SLOT]}
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(profile), encoding="utf-8"
    )
    (root / ".guardkit").mkdir(parents=True, exist_ok=True)
    (root / ".guardkit" / "config.yaml").write_text(
        f"toolchain:\n  test: {root / 'deploy' / 'test.sh'}\n  test_timeout: 60\n"
        f"launch:\n  settings: [{DECLARED}]\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "env-*.json\n.forge/\n.forge-candidates/\n", encoding="utf-8"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the project as it is")
    return root


def _given(where: Path, label: str) -> dict[str, str]:
    answer = where / f"env-{label}.json"
    assert answer.is_file(), f"the {label} program never ran"
    return json.loads(answer.read_text(encoding="utf-8"))


def _no_sentinel(given: dict[str, str] | str, what: str) -> None:
    text = given if isinstance(given, str) else json.dumps(given)
    for name, value in SENTINELS.items():
        assert value not in text, f"{name} reached {what}"
    assert MARK not in text, f"a factory credential reached {what}"


def _home_is_private(given: dict[str, str], root: Path) -> None:
    home = Path(given["HOME"])
    private = (root / ".git" / "forge-project-home").resolve()
    assert private in home.resolve().parents, given["HOME"]
    assert private in Path(given["TMPDIR"]).resolve().parents, given["TMPDIR"]


@pytest.fixture(autouse=True)
def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process holds the factory's credentials, as the helper's does."""
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(DECLARED, "present")
    monkeypatch.setenv("FORGE_SIDECAR_IN_SANDBOX", "1")
    monkeypatch.delenv("GUARDKIT_RUN_OWNER", raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _a_project(tmp_path / "shop")


def _config(root: Path, **extra: Any) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(root.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(root)}, **extra},
        }
    )


@pytest.fixture
def toolchain_loader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The build system's declared-test loader, stood in under its own name."""
    module = ModuleType(LOADER_NAME)

    def load_toolchain_declaration(root: Any) -> Any:
        path = Path(root) / ".guardkit" / "config.yaml"
        if not path.is_file():
            return None
        toolchain = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "toolchain"
        ) or {}
        if not toolchain.get("test"):
            return None
        return SimpleNamespace(
            test=str(toolchain["test"]),
            test_timeout=int(toolchain.get("test_timeout", 60)),
        )

    module.load_toolchain_declaration = load_toolchain_declaration  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, LOADER_NAME, module)
    return module


@contextlib.contextmanager
def _a_helper(config: ForgeConfig) -> Iterator[tuple[Any, str]]:
    """The REAL helper service, built as its entry point builds it: no runner
    injected. One loopback test server on a port the kernel picks."""
    from forge.deploy_sidecar.service import build_server

    server = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def helper(project: Path) -> Iterator[tuple[Any, str]]:
    with _a_helper(_config(project)) as running:
        yield running


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _the_runners_are_split(server: Any) -> None:
    from forge.deploy_sidecar.service import run_merge_command, run_project_program

    assert server.project_runner is run_project_program
    assert server.merge_runner is run_merge_command


# ---------------------------------------------------------------------------
# The helper itself: five steps, from nothing
# ---------------------------------------------------------------------------


class TestTheHelper:
    def test_the_helper_builds_from_nothing(self, project: Path) -> None:
        from forge.launch_environment import build_project_command_env

        env = build_project_command_env(project, declared=[DECLARED])

        _no_sentinel(env, "the helper's result")
        assert set(env) == {"PATH", "HOME", "TMPDIR", DECLARED} | {
            name for name in os.environ if name in ("LANG", "LC_ALL") or name.startswith("LC_")
        }
        assert env[DECLARED] == "present"
        _home_is_private(env, project)

    def test_the_passed_names_match_guardkits_list(self) -> None:
        """Mirror test: GuardKit's ``project_command_env`` passes PATH, a
        private HOME and TMPDIR, the locale and these eight proxy names
        (copied from GuardKit's ``PROXY_NAMES``), and checks these five for
        credentials (``_CHECKED_PROXY_NAMES``)."""
        from forge.launch_environment import (
            PROJECT_CHECKED_PROXY_NAMES,
            PROJECT_PROXY_NAMES,
        )

        guardkit_proxy_names = (
            "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
            "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
        )
        guardkit_checked = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY"}
        assert tuple(PROJECT_PROXY_NAMES) == guardkit_proxy_names
        assert set(PROJECT_CHECKED_PROXY_NAMES) == guardkit_checked

    def test_the_five_steps_follow_guardkits_helper_rules(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The in-step rules GuardKit's helper states, step by step."""
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        # Step 2: an addition may not be credential-shaped or the marker.
        for bad in ("SOME_TOKEN", "GUARDKIT_RUN_OWNER", "not a name"):
            with pytest.raises(ValueError):
                build_project_command_env(project, additions={bad: "x"})
        # Step 3: an overlay may not name a reserved or credential-shaped
        # name, nor one an addition set.
        for bad in ("OPENAI_API_KEY", "GUARDKIT_RUN_OWNER", "PATH", "DB_PASSWORD"):
            with pytest.raises(ProjectCommandEnvironmentError) as refused:
                build_project_command_env(project, overlay={bad: "x"})
            assert bad in str(refused.value)
        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(
                project, additions={"ENV_FILE": "/a"}, overlay={"ENV_FILE": "/b"}
            )
        assert "ENV_FILE" in str(refused.value)
        # Step 4: the proxy check runs on everything composed, overlay too.
        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(
                project, overlay={"ftp_proxy": "http://u:SENTINEL_p@proxy:3128"}
            )
        assert "ftp_proxy" in str(refused.value)
        assert "SENTINEL_p" not in str(refused.value)
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:3128")
        assert (
            build_project_command_env(project)["HTTP_PROXY"]
            == "http://proxy.example.test:3128"
        )
        # Step 5: the marker comes last, and only from ``run_owner``.
        monkeypatch.setenv("GUARDKIT_RUN_OWNER", "inherited-never-used")
        assert "GUARDKIT_RUN_OWNER" not in build_project_command_env(project)
        assert (
            build_project_command_env(project, run_owner="build-x")[
                "GUARDKIT_RUN_OWNER"
            ]
            == "build-x"
        )

    def test_a_request_overlay_naming_the_run_owner_is_refused(
        self, project: Path
    ) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(
                project, overlay={"GUARDKIT_RUN_OWNER": "other"}, run_owner="build-x"
            )
        assert "GUARDKIT_RUN_OWNER" in str(refused.value)
        assert (
            build_project_command_env(project, run_owner="build-x")[
                "GUARDKIT_RUN_OWNER"
            ]
            == "build-x"
        )
        assert "GUARDKIT_RUN_OWNER" not in build_project_command_env(project)

    @pytest.mark.parametrize("name", ["HTTPS_PROXY", "https_proxy"])
    def test_a_declared_proxy_with_credentials_is_refused(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        monkeypatch.setenv(name, "http://u:SENTINEL_proxy@p:3128")
        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(project, declared=[name])
        assert name in str(refused.value)
        assert "SENTINEL_proxy" not in str(refused.value)
        # Without credentials it still arrives.
        monkeypatch.setenv(name, "http://p:3128")
        assert build_project_command_env(project, declared=[name])[name] == (
            "http://p:3128"
        )

    def test_a_plain_dictionary_is_not_an_issued_value(self, project: Path) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        with pytest.raises((ProjectCommandEnvironmentError, TypeError)):
            build_project_command_env(
                project, issued={"FACTORY_PREVIEW_TOKEN": TOKEN}  # type: ignore[arg-type]
            )

    def test_a_folder_with_no_git_directory_runs_nothing(self, tmp_path: Path) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        # A folder whose ``.git`` names no git directory. (A folder with no
        # ``.git`` at all could still sit inside some other checkout of this
        # machine's temporary folder, which is no business of this test's.)
        bare = tmp_path / "not-a-repository"
        bare.mkdir()
        (bare / ".git").write_text("not a git directory\n", encoding="utf-8")
        with pytest.raises(ProjectCommandEnvironmentError):
            build_project_command_env(bare)


# ---------------------------------------------------------------------------
# F1 and F2: the declared test and the live-gate driver, through /run
# ---------------------------------------------------------------------------


class TestTheHelpersRunRoute:
    """``build_server()`` with no runner injected, then real ``POST /run``."""

    def test_the_declared_test_hides_factory_credentials(
        self, helper: tuple[Any, str], project: Path, toolchain_loader: ModuleType
    ) -> None:
        from forge.cli._serve_conductor import run_declared_command_in_sandbox

        server, url = helper
        tree = project / ".forge" / "worktrees" / "journey-1"
        tree.mkdir(parents=True)
        exit_code, detail = run_declared_command_in_sandbox(
            command=str(project / "deploy" / "test.sh"),
            cwd=tree,
            timeout_seconds=60,
            sandbox=SimpleNamespace(name="shop-sbx", sidecar_url=url),
            repo=REPO,
            launch_settings=[DECLARED],
            by_hand=True,
        )
        assert exit_code == 0, detail

        given = _given(tree, "test")
        _no_sentinel(given, "the declared test")
        assert given[DECLARED] == "present"
        _home_is_private(given, project)
        _the_runners_are_split(server)

    def test_the_live_gate_driver_hides_factory_credentials(
        self, helper: tuple[Any, str], project: Path
    ) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        server, url = helper
        outcome = SidecarLiveGateInvoker(
            base_url=url,
            repo=REPO,
            repo_path=project,
            driver_argv=["deploy/journey.sh"],
            timeout_seconds=60,
            extra_env={ALLOWED_NAME: ALLOWED_VALUE},
            launch_settings=[DECLARED],
            by_hand=True,
        ).invoke(feature=FEATURE, target="candidate")
        assert outcome.verdict == "pass", outcome.detail

        given = _given(project, "journey")
        _no_sentinel(given, "the live-gate driver")
        assert given[DECLARED] == "present"
        assert given[ALLOWED_NAME] == ALLOWED_VALUE
        _home_is_private(given, project)
        _the_runners_are_split(server)


# ---------------------------------------------------------------------------
# The factory's own launches keep their credentials (positive controls)
# ---------------------------------------------------------------------------


@pytest.fixture
def stand_in_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real executable named guardkit that writes down what it was given."""
    out = tmp_path / "guardkit-was-given.jsonl"
    binary = tmp_path / "bin" / "guardkit"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(out)!r}, 'a').write(json.dumps(dict(os.environ)) + '\\n')\n"
        "print(json.dumps({'outcome': 'merged', 'post_sha': 'b' * 40,"
        " 'verify_ok': True}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
    return out


def _what_guardkit_got(out: Path) -> list[dict[str, str]]:
    assert out.is_file(), "the stand-in build system never ran"
    return [json.loads(line) for line in out.read_text().splitlines() if line]


class TestTheFactorysOwnLaunchesKeepTheirCredentials:
    def test_the_merge_route_still_hands_guardkit_its_credentials(
        self, helper: tuple[Any, str], stand_in_guardkit: Path
    ) -> None:
        server, url = helper
        status, body = _post(
            f"{url}/guardkit-merge",
            {"repo": REPO, "feature_id": FEATURE, "expect_main_sha": "a" * 40},
        )
        assert status == 200, body
        (given,) = _what_guardkit_got(stand_in_guardkit)
        assert given["OPENAI_API_KEY"] == SENTINELS["OPENAI_API_KEY"]
        assert given["GUARDKIT_NATS_PASSWORD"] == SENTINELS["GUARDKIT_NATS_PASSWORD"]
        _the_runners_are_split(server)

    def test_the_leg_route_still_hands_guardkit_its_credentials(
        self, helper: tuple[Any, str], project: Path, stand_in_guardkit: Path
    ) -> None:
        _server, url = helper
        tree = project / ".forge" / "worktrees" / "journey-2"
        _git(project, "worktree", "add", "-q", "-b", "repair/one", str(tree))
        status, body = _post(
            f"{url}/guardkit-leg",
            {"repo": REPO, "cwd": str(tree), "subcommand": "task-work"},
        )
        assert status == 200, body
        (given,) = _what_guardkit_got(stand_in_guardkit)
        assert given["OPENAI_API_KEY"] == SENTINELS["OPENAI_API_KEY"]
        assert given["GUARDKIT_NATS_PASSWORD"] == SENTINELS["GUARDKIT_NATS_PASSWORD"]


# ---------------------------------------------------------------------------
# F3 to F8: every other place a project program starts
# ---------------------------------------------------------------------------


class TestEveryOtherProjectProgram:
    def test_the_deploy_script_step_hides_factory_credentials(
        self, project: Path
    ) -> None:
        """F3: ``_run_script_step``, the core every deploy script runs through."""
        from forge.executor.shell_steps import _run_script_step

        exit_code, output = _run_script_step(
            cwd=str(project),
            script="deploy/deploy.sh",
            env_file="/the/project/.env",
            extra_env={ALLOWED_NAME: ALLOWED_VALUE},
            launch_settings=[DECLARED],
        )
        assert exit_code == 0, output
        given = _given(project, "deploy")
        _no_sentinel(given, "the deploy script")
        assert given["ENV_FILE"] == "/the/project/.env"
        assert given[DECLARED] == "present"
        assert given[ALLOWED_NAME] == ALLOWED_VALUE
        _home_is_private(given, project)

    def test_the_runbook_steps_hide_factory_credentials(self, project: Path) -> None:
        """F4: the ``deploy_compose`` and ``run_smoke_tests`` handlers."""
        from forge.executor.shell_steps import deploy_compose, run_smoke_tests
        from forge.persistence.repositories.runbook_models import Step, StepStatus

        compose = deploy_compose(
            Step(
                step_type="deploy_compose",
                params={
                    "cwd": str(project),
                    "script": "deploy/deploy.sh",
                    "extra_env": {ALLOWED_NAME: ALLOWED_VALUE},
                    "launch_settings": [DECLARED],
                },
                status=StepStatus.pending,
                sequence_index=0,
            )
        )
        assert compose.status == StepStatus.passed, compose.result
        given = _given(project, "deploy")
        _no_sentinel(given, "the compose step")
        assert given[ALLOWED_NAME] == ALLOWED_VALUE
        smoke = run_smoke_tests(
            Step(
                step_type="run_smoke_tests",
                params={"cwd": str(project), "script": "deploy/check.sh"},
                status=StepStatus.pending,
                sequence_index=1,
            )
        )
        assert smoke.status == StepStatus.passed, smoke.result
        _no_sentinel(_given(project, "check"), "the smoke step")

    def test_the_owned_deploy_hides_factory_credentials(
        self, project: Path, tmp_path: Path
    ) -> None:
        """F5: the deploy executor's child environment."""
        from forge.deploy_sidecar.deploy_executor import DeployExecutor, DeployRequest

        executor = DeployExecutor(notes_root=tmp_path / "notes")
        env = executor._child_environment(
            DeployRequest(
                target="shop-live",
                target_counter=3,
                build=BUILD,
                cwd=str(project),
                script="deploy/deploy.sh",
                env_file="/the/project/.env",
                extra_env={"PROMOTE": "1"},
                launch_settings=(DECLARED,),
                identity="shop:abc123",
                identity_setting="DEPLOY_IDENTITY",
                artifact="sha256:" + "0" * 64,
                artifact_setting="DEPLOY_ARTIFACT",
            )
        )
        _no_sentinel(env, "the owned deploy")
        assert env["DEPLOY_IDENTITY"] == "shop:abc123"
        assert env["DEPLOY_ARTIFACT"] == "sha256:" + "0" * 64
        assert env["ENV_FILE"] == "/the/project/.env"
        assert env["PROMOTE"] == "1"
        assert env[DECLARED] == "present"
        _home_is_private(env, project)

    def test_the_declared_test_without_a_sandbox_hides_factory_credentials(
        self, project: Path
    ) -> None:
        """F6: the coordinator's own run of a declared test."""
        from forge.cli._serve_conductor import _run_declared_command

        exit_code, detail = _run_declared_command(
            command=str(project / "deploy" / "test.sh"),
            cwd=project,
            timeout_seconds=60,
        )
        assert exit_code == 0, detail
        given = _given(project, "test")
        _no_sentinel(given, "the declared test (no sandbox)")
        _home_is_private(given, project)

    def test_the_live_gate_without_a_sandbox_hides_factory_credentials(
        self, project: Path
    ) -> None:
        """F7: the coordinator's own run of the live-gate driver."""
        from forge.deploy.live_gate import RepoDriverLiveGateInvoker

        outcome = RepoDriverLiveGateInvoker(
            repo_path=project,
            driver_argv=[str(project / "deploy" / "journey.sh")],
            timeout_seconds=60,
            extra_env={ALLOWED_NAME: ALLOWED_VALUE},
        ).invoke(feature=FEATURE, target="candidate")
        assert outcome.verdict == "pass", outcome.detail
        given = _given(project, "journey")
        _no_sentinel(given, "the live-gate driver (no sandbox)")
        assert given[ALLOWED_NAME] == ALLOWED_VALUE
        _home_is_private(given, project)

    def test_the_build_packages_test_run_hides_factory_credentials(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F8: ``verify_tests``, through its own seam."""
        from forge.build.test_verification import verify_tests

        bin_dir = tmp_path / "pytest-bin"
        _writer(bin_dir / "pytest", "pytest")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        asyncio.run(verify_tests(project, test_command="pytest"))
        given = _given(project, "pytest")
        _no_sentinel(given, "the build package's test run")
        _home_is_private(given, project)


# ---------------------------------------------------------------------------
# The review preview's issued values (R4): one narrow path of their own
# ---------------------------------------------------------------------------


def _run_body(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repo": REPO,
        "script": "deploy/deploy.sh",
        "timeout_seconds": 60,
        "by_hand": True,
    }
    body.update(fields)
    return body


def _issued(**changes: str) -> dict[str, str]:
    values = {
        "PREVIEW": "1",
        "COMPOSE_PROJECT_NAME": "shop-pv0",
        "FACTORY_PREVIEW_PORT": "8950",
        "FACTORY_PREVIEW_INTERNAL_PORT": "18950",
        "FACTORY_PREVIEW_URL": SLOT["public_url"],
        "FACTORY_PREVIEW_TOKEN": TOKEN,
        "FACTORY_RESOURCE_LABEL": "forge.preview.lease=pv000000000001",
        "FACTORY_EVIDENCE_DIR": "/clone/.forge-candidates/preview-pv000000000001/.factory-evidence",
    }
    values.update(changes)
    return values


class TestIssuedValuesAreCheckedWhereTheyAreRead:
    def test_an_issued_token_reaches_the_deploy_script(
        self, helper: tuple[Any, str], project: Path
    ) -> None:
        _server, url = helper
        status, body = _post(f"{url}/run", _run_body(issued_env=_issued()))
        assert status == 200 and body["exit_code"] == 0, body
        given = _given(project, "deploy")
        assert given["FACTORY_PREVIEW_TOKEN"] == TOKEN
        assert given["PREVIEW"] == "1"
        _no_sentinel(given, "the preview's deploy script")

    def test_a_factory_credential_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        # A credential-shaped name the profile itself lets a request carry.
        root = _a_project(
            tmp_path / "shop", gate_env={"FACTORY_API_TOKEN": "", ALLOWED_NAME: ""}
        )
        with _a_helper(_config(root)) as (_server, url):
            status, body = _post(
                f"{url}/run",
                {
                    "repo": REPO,
                    "driver": ["deploy/journey.sh"],
                    "args": [],
                    "env": {"FACTORY_API_TOKEN": "a-value-written-in-this-test"},
                    "by_hand": True,
                },
            )
            assert "FACTORY_API_TOKEN" in json.dumps(body)
            assert not (root / "env-journey.json").exists(), "it ran"
            # And a preview name sent the old way, through ``env``.
            status, body = _post(
                f"{url}/run", _run_body(env={"FACTORY_PREVIEW_TOKEN": TOKEN})
            )
            assert "FACTORY_PREVIEW_TOKEN" in json.dumps(body)
            assert TOKEN not in json.dumps(body)
            assert not (root / "env-deploy.json").exists(), "it ran"

    def test_a_parent_credential_cannot_be_passed_off_as_issued(
        self, helper: tuple[Any, str], project: Path
    ) -> None:
        _server, url = helper
        status, body = _post(
            f"{url}/run",
            _run_body(
                issued_env=_issued(FACTORY_PREVIEW_TOKEN=SENTINELS["OPENAI_API_KEY"])
            ),
        )
        assert status == 400, body
        assert "FACTORY_PREVIEW_TOKEN" in body["error"]
        _no_sentinel(json.dumps(body), "the refusal")
        assert not (project / "env-deploy.json").exists(), "it ran"

    def test_an_issued_value_needs_the_preview_switched_on(
        self, tmp_path: Path
    ) -> None:
        root = _a_project(tmp_path / "shop", preview=False)
        with _a_helper(_config(root)) as (_server, url):
            status, body = _post(f"{url}/run", _run_body(issued_env=_issued()))
        assert status == 400, body
        assert "preview" in body["error"]
        assert not (root / "env-deploy.json").exists(), "it ran"

    def test_issued_values_take_only_preview_names(
        self, helper: tuple[Any, str], project: Path
    ) -> None:
        _server, url = helper
        status, body = _post(
            f"{url}/run", _run_body(issued_env=_issued(OTHER_TOKEN=TOKEN))
        )
        assert status == 400, body
        assert "OTHER_TOKEN" in body["error"]
        assert not (project / "env-deploy.json").exists(), "it ran"

    @pytest.mark.parametrize("token", ["0123456789", "a" * 20 + "/" + "b" * 20])
    def test_a_short_or_odd_token_is_refused(
        self, helper: tuple[Any, str], project: Path, token: str
    ) -> None:
        _server, url = helper
        status, body = _post(
            f"{url}/run", _run_body(issued_env=_issued(FACTORY_PREVIEW_TOKEN=token))
        )
        assert status == 400, body
        assert "FACTORY_PREVIEW_TOKEN" in body["error"]
        assert token not in body["error"]
        assert not (project / "env-deploy.json").exists(), "it ran"


# ---------------------------------------------------------------------------
# The two positive controls: the whole production chain, I1 to I12
# ---------------------------------------------------------------------------


class _TheCoordinatorsAnswer(BaseHTTPRequestHandler):
    """A stand-in for the coordinator's read-only answer: which commit this
    build was recorded as starting from."""

    records: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — the base class's spelling
        from urllib.parse import parse_qs, urlparse

        build = (parse_qs(urlparse(self.path).query).get("build") or [""])[0]
        answer: dict[str, Any] = {}
        if build in self.records:
            answer = {"build": build, "start_commit": self.records[build]}
        payload = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # noqa: D102
        return


class _NoBus:
    """The bus the publishers would use; nothing is sent anywhere."""

    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Venue:
    """Only the parts that need a container engine are stood in for."""

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.lease: str | None = None

    async def lay_out(self, lease_id: str, *, onto: str, candidate: str) -> Any:
        from forge.deploy.review_preview import PreviewTree

        self.lease = lease_id
        return PreviewTree(commit="d" * 40, tree="e" * 40, path=str(self.tree))

    async def remove_tree(self, lease_id: str) -> bool:
        return True

    async def preview_containers(self, compose_project: str) -> Any:
        return {self.lease}, [(f"{compose_project}-app-1", self.lease)]

    async def probe(self, lease_id: str, port: int, token: str) -> dict[str, Any]:
        return {"no_token": 410, "wrong_token": 410, "right_token": 200}

    async def fetch_evidence(self, lease_id: str) -> tuple[bytes | None, str | None]:
        return None, "no evidence is kept in this test"

    async def list_leases(self) -> set[str] | None:
        return set()

    async def remove_project(self, lease_id: str) -> bool:
        return True


class _RecordingBodies:
    """Records every request body the two transports send."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bodies: list[dict[str, Any]] = []
        real = urllib.request.urlopen

        def _open(request: Any, *args: Any, **kwargs: Any) -> Any:
            data = getattr(request, "data", None)
            if data and str(getattr(request, "full_url", "")).endswith("/run"):
                self.bodies.append(json.loads(data.decode("utf-8")))
            return real(request, *args, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", _open)


@pytest.fixture
def the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """The preview code, the dispatcher, the stage, the runbook, the step
    handlers, the two HTTP transports and a REAL helper, wired as production
    wires them. The preview tree is laid out by hand (git and the engine are
    what lay it out for real)."""
    from forge.adapters.sqlite.connect import connect_writer
    from forge.deploy.profile import parse_deploy_profile
    from forge.deploy.review_preview import PreviewLeaseStore, ReviewPreviewService
    from forge.lifecycle import migrations
    from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

    root = _a_project(tmp_path / "shop")
    start = _git(root, "rev-parse", "HEAD")
    lease = "pv000000000001"
    tree = root / ".forge-candidates" / f"preview-{lease}"
    for label, envelope in (("deploy", False), ("check", False), ("journey", True)):
        _writer(tree / "deploy" / f"{label}.sh", label, envelope=envelope)

    db = tmp_path / "forge.db"
    cx = connect_writer(db)
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id, memory_project, "
        "launch_settings, start_commit, target_branch) VALUES (?, ?, ?, "
        "'autobuild/FEAT-AB12', 'f.yaml', 'COMPLETE', 'cli', 'corr', "
        "'2026-10-13T00:00:00Z', '2026-10-13T00:00:00Z', '/wt', 'mode-a', "
        "'TASK-AB12', 'shop', ?, ?, 'main')",
        (BUILD, FEATURE, REPO, json.dumps([DECLARED]), start),
    )
    cx.commit()

    answer = type("_Answer", (_TheCoordinatorsAnswer,), {"records": {BUILD: start}})
    coordinator = ThreadingHTTPServer(("127.0.0.1", 0), answer)
    threading.Thread(target=coordinator.serve_forever, daemon=True).start()
    host, port = coordinator.server_address[:2]
    monkeypatch.setenv(
        "FORGE_TARGET_OWNER_URL", f"http://{host}:{port}/what-did-you-record"
    )

    with _a_helper(_config(root)) as (server, url):
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
                "planning": {
                    "target_repo_paths": {REPO: str(root)},
                    "sandboxes": {
                        REPO: {
                            "name": "shop-sbx",
                            "sidecar_url": url,
                            "runner_url": "http://127.0.0.1:9",
                        }
                    },
                },
                "deploy": {"enabled": True, "run_live_gate": True},
            }
        )
        profile = parse_deploy_profile(
            yaml.safe_load((root / "deploy" / "profile.yaml").read_text())
        )
        recorded = _RecordingBodies(monkeypatch)
        service = ReviewPreviewService(
            store=PreviewLeaseStore(cx),
            profile_reader=lambda _project: profile,
            venue_for=lambda _project: _Venue(tree),
            deploy_dispatcher=build_in_daemon_deploy_dispatcher(
                config=config, nats_client=_NoBus(), db_path=db
            ),
            repo_paths={REPO: str(root)},
            evidence_root=tmp_path / "evidence",
            evidence_base_url="https://evidence.example.test",
            clock=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
            token_factory=lambda: TOKEN,
            lease_id_factory=lambda: lease,
            run_id_factory=lambda: "pvrun-000000000001",
            blocking_sleep=lambda _s: None,
        )
        yield SimpleNamespace(
            root=root, tree=tree, service=service, bodies=recorded.bodies, db=db,
            server=server,
        )
    coordinator.shutdown()
    coordinator.server_close()
    cx.close()


def _prepare(chain: SimpleNamespace) -> Any:
    from forge.deploy.review_preview import PreviewRequest

    return asyncio.run(
        chain.service.prepare(
            PreviewRequest(
                project=REPO,
                feature_id=FEATURE,
                build_id=BUILD,
                branch=f"autobuild/{FEATURE}",
                candidate_sha="b" * 40,
                candidate_tree="c" * 40,
                expect_main_sha="a" * 40,
                target_branch="main",
                preview_base_sha="f" * 40,
            )
        )
    )


def _preview_names() -> frozenset[str]:
    from forge.deploy.review_preview import PREVIEW_ENV_KEYS

    return PREVIEW_ENV_KEYS


class TestTheProductionChain:
    def test_a_preview_deploy_receives_the_issued_token(
        self, the_chain: SimpleNamespace
    ) -> None:
        _prepare(the_chain)

        for label in ("deploy", "check"):
            given = _given(the_chain.tree, label)
            assert given.get("FACTORY_PREVIEW_TOKEN") == TOKEN, label
            # ``PREVIEW=1`` goes to the compose step only, as it always has.
            expected = _preview_names() - {"FACTORY_RECORD_VIDEO"}
            if label == "check":
                expected -= {"PREVIEW"}
            for name in expected:
                assert name in given, f"{name} did not reach the {label} step"
            _no_sentinel(given, f"the preview's {label} step")
            _home_is_private(given, the_chain.root)
        assert _given(the_chain.tree, "deploy")["PREVIEW"] == "1"

        scripts = [b for b in the_chain.bodies if "script" in b]
        assert scripts, "no script request was sent"
        for body in scripts:
            assert not set(body.get("env") or {}) & _preview_names(), body.get("env")
            assert body["issued_env"]["FACTORY_PREVIEW_TOKEN"] == TOKEN

        # The step parameters keep the two apart.
        cx = sqlite3.connect(the_chain.db)
        try:
            params = {
                row[0]: json.loads(row[1])
                for row in cx.execute(
                    "SELECT step_type, params FROM runbook_steps "
                    "WHERE runbook_id LIKE 'deploy-pv-%'"
                )
            }
        finally:
            cx.close()
        assert set(params) >= {"deploy_compose", "health_check"}, params
        for step in params.values():
            assert not set(step.get("extra_env") or {}) & _preview_names(), step
            assert step["issued_env"]["FACTORY_PREVIEW_TOKEN"] == TOKEN

    def test_a_recorded_live_gate_journey_receives_the_issued_token(
        self, the_chain: SimpleNamespace
    ) -> None:
        _prepare(the_chain)

        given = _given(the_chain.tree, "journey")
        assert given.get("FACTORY_PREVIEW_TOKEN") == TOKEN
        assert given.get("FACTORY_RECORD_VIDEO") == "1"
        assert given.get(DECLARED) == "present"
        _no_sentinel(given, "the preview's journey")
        _home_is_private(given, the_chain.root)
        drivers = [b for b in the_chain.bodies if "driver" in b]
        assert drivers, "no journey request was sent"
        for body in drivers:
            assert not set(body.get("env") or {}) & _preview_names(), body.get("env")
            assert body["issued_env"]["FACTORY_PREVIEW_TOKEN"] == TOKEN


# ---------------------------------------------------------------------------
# Section 5: a guard that cannot be missed by leaving a site out
# ---------------------------------------------------------------------------

SOURCE = Path(__file__).resolve().parents[2] / "src" / "forge"

_LAUNCH_NAMES = {
    "subprocess": {
        "run", "Popen", "call", "check_call", "check_output", "getoutput",
        "getstatusoutput",
    },
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {
        "system", "popen", "execv", "execve", "execvp", "execvpe", "execl",
        "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
        "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn",
        "posix_spawnp", "fork", "forkpty",
    },
    "pty": {"spawn", "fork"},
}

#: The factory's own programs, recognised from the first argument.
_FACTORY_PROGRAMS = {"git", "docker", "pgrep", "ps"}

#: The helper whose result a project command's ``env=`` must be.
_HELPER = "build_project_command_env"

#: Launches allowed by name, each with its one-line reason. Keyed by the file
#: (under ``src/forge``) and the function the launch is written in.
ALLOWED: dict[tuple[str, str], str] = {
    ("adapters/gh/operations.py", "_execute"): "runs gh, the factory's own GitHub client",
    ("adapters/git/operations.py", "_default_execute"): "runs git commands the git adapter composes",
    ("adapters/guardkit/run.py", "_execute_subprocess"): "starts GuardKit with the factory's launch list",
    ("build/git_operations.py", "_execute_via_deepagents"): "generic runner; every caller is checked below",
    ("build_processes.py", "_engine_ids"): "the container engine, called through a variable",
    ("build_processes.py", "_remove_networks_and_volumes"): "the container engine, called through a variable",
    ("cli/register_repo.py", "_run_command"): "the operator's register command: git, docker and sbx",
    ("deploy/review_preview.py", "_run_command"): "git and the container engine for the preview's tree",
    ("deploy_sidecar/deploy_executor.py", "_default_spawn"): "generic runner; every caller is checked below",
    ("deploy_sidecar/service.py", "_start_and_collect"): "generic runner; every caller is checked below",
    ("deploy_sidecar/service.py", "blob"): "git cat-file, the argument list from _git_argv",
    ("deploy_sidecar/service.py", "opened"): "git cat-file, the argument list from _git_argv",
    ("pipeline/routing_stamps.py", "read_last_code_commit_time"): "git log, from a literal git list",
    ("planning/target_terminal_tools.py", "_default_normalizer_subprocess"): "the planner's normalizer, a factory program",
    ("review_gate/record.py", "validate_review_findings"): "guardkit qa validate, a factory program",
    ("subagents/autobuild_runner.py", "_guardkit_feature_validate"): "guardkit feature validate, a factory program",
    ("subagents/autobuild_runner.py", "_run_one_build"): "starts GuardKit's build with the factory's launch list",
}

#: Generic runners: every call to them is checked as a launch of its own.
#: The value is the names they are called by.
GENERIC_RUNNERS: dict[tuple[str, str], set[str]] = {
    ("build/git_operations.py", "_execute_via_deepagents"): {"_execute_via_deepagents"},
    ("deploy_sidecar/deploy_executor.py", "_default_spawn"): {"_spawn", "_default_spawn"},
    ("deploy_sidecar/service.py", "_start_and_collect"): {"_start_and_collect"},
}

#: Calls to a generic runner allowed by name, with the reason.
ALLOWED_RUNNER_CALLS: dict[tuple[str, str], str] = {
    ("build/git_operations.py", "_run_via_execute"): "git and gh, the factory's own programs, with its own environment",
    ("build/test_verification.py", "_execute_via_deepagents"): "a pass-through seam; its caller verify_tests is checked",
    ("deploy_sidecar/service.py", "run_merge_command"): "starts GuardKit (merge, legs, checks) with the factory's launch list",
}

#: The only two places that may build an issued preview value.
ISSUED_BUILDERS: set[tuple[str, str]] = {
    ("deploy/review_preview.py", "_prepare"),
    ("deploy_sidecar/service.py", "_read_issued_env"),
}


def _function_of(tree: ast.AST) -> dict[int, ast.AST]:
    owner: dict[int, ast.AST] = {}

    def visit(node: ast.AST, current: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            here = (
                child
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            if current is not None:
                owner[id(child)] = current
            visit(child, here)

    visit(tree, None)
    return owner


def _is_helper_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name == _HELPER


def _returns_helper(function: ast.AST) -> bool:
    """Every ``return`` of ``function`` is the helper's result."""
    returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
    if not returns:
        return False
    assigned = _helper_names(function)
    return all(
        _is_helper_call(r.value)
        or (isinstance(r.value, ast.Name) and r.value.id in assigned)
        for r in returns
    )


def _helper_names(function: ast.AST, functions: dict[str, ast.AST] | None = None) -> set[str]:
    """Local names assigned from the helper (or a same-file function that
    returns only the helper's result)."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _env_is_helper(
                node.value, function, functions, names
            ):
                names.add(target.id)
    return names


def _env_is_helper(
    value: ast.AST | None,
    function: ast.AST | None,
    functions: dict[str, ast.AST] | None,
    assigned: set[str] | None = None,
) -> bool:
    if _is_helper_call(value):
        return True
    if isinstance(value, ast.Name) and function is not None:
        known = assigned if assigned is not None else _helper_names(function, functions)
        return value.id in known
    if isinstance(value, ast.Call) and functions:
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        target = functions.get(str(name))
        return target is not None and _returns_helper(target)
    return False


def _first_program(call: ast.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.List) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _the_problems() -> list[str]:
    problems: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _function_of(tree)
        functions = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        direct: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _LAUNCH_NAMES:
                for alias in node.names:
                    if alias.name in _LAUNCH_NAMES[node.module]:
                        direct[alias.asname or alias.name] = node.module
        runner_names = {
            name
            for (file, _fn), names in GENERIC_RUNNERS.items()
            for name in names
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            kind = None
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.attr in _LAUNCH_NAMES.get(func.value.id, set())
            ):
                if not (func.value.id == "asyncio" and func.attr == "run"):
                    kind = "launch"
            elif isinstance(func, ast.Name) and func.id in direct:
                kind = "launch"
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if kind is None and name in runner_names:
                kind = "runner-call"
            if kind is None:
                continue
            function = owner.get(id(node))
            fname = getattr(function, "name", "<module>")
            where = f"{rel}:{node.lineno} ({fname})"
            if kind == "runner-call" and (rel, fname) in GENERIC_RUNNERS:
                continue  # the runner's own body, e.g. a self-call
            if _first_program(node) in _FACTORY_PROGRAMS:
                continue
            env = next((k.value for k in node.keywords if k.arg == "env"), None)
            if _env_is_helper(env, function, functions):
                continue
            if kind == "launch" and (rel, fname) in ALLOWED:
                continue
            if kind == "runner-call" and (rel, fname) in ALLOWED_RUNNER_CALLS:
                continue
            problems.append(
                f"{where}: a process launch whose env= is not "
                f"{_HELPER}(...) and that is on no allowed list"
            )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            built = (isinstance(func, ast.Name) and func.id == "IssuedPreviewValues") or (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "IssuedPreviewValues"
            )
            if not built:
                continue
            fname = getattr(owner.get(id(node)), "name", "<module>")
            if (rel, fname) not in ISSUED_BUILDERS:
                problems.append(
                    f"{rel}:{node.lineno} ({fname}): IssuedPreviewValues is built "
                    "outside the preview's preparation and the helper's reader"
                )
    return problems


def test_every_process_launch_is_accounted_for() -> None:
    problems = _the_problems()
    assert not problems, "\n".join(problems)


def test_the_guard_sees_a_launch_that_was_left_out(tmp_path: Path) -> None:
    """The guard is not vacuous: a launch that inherits everything fails it."""
    tree = ast.parse(
        "import subprocess\n"
        "def f(cmd):\n"
        "    subprocess.run(cmd)\n"
        "def g(cmd, folder):\n"
        "    env = build_project_command_env(folder)\n"
        "    subprocess.run(cmd, env=env)\n"
    )
    owner = _function_of(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run"]
    verdicts = []
    for call in calls:
        env = next((k.value for k in call.keywords if k.arg == "env"), None)
        verdicts.append(_env_is_helper(env, owner.get(id(call)), {}))
    assert verdicts == [False, True]
