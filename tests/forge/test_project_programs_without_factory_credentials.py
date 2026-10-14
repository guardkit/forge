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
import functools
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
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


def _writer(
    path: Path, label: str, *, envelope: bool = False, exit_code: int = 0
) -> None:
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
        verdict = "pass" if exit_code == 0 else "fail"
        lines.append(
            f"print(json.dumps({{'verdict': '{verdict}', 'run_id': 'r1', 'gates': []}}))"
        )
    if exit_code:
        lines.append(f"raise SystemExit({exit_code})")
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
    exit_code: int = 0,
    declares: tuple[str, ...] = (DECLARED,),
) -> Path:
    """A committed repository with a deploy profile and a declared test.

    ``exit_code`` is what every one of its programs ends with, after writing
    down what it was given; ``declares`` is its ``launch.settings``.
    """
    for label, envelope in (
        ("deploy", False),
        ("check", False),
        ("journey", True),
        ("test", False),
    ):
        _writer(
            root / "deploy" / f"{label}.sh", label, envelope=envelope, exit_code=exit_code
        )
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
        f"launch:\n  settings: [{', '.join(declares)}]\n",
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
# A failed program is told what it was not given (design section 4)
# ---------------------------------------------------------------------------

#: Set where the factory runs, not declared: offered by name, never shown.
WITHHELD = "STANDIN_WITHHELD"
WITHHELD_VALUE = "a-value-that-must-never-be-shown-" + "w" * 12
#: Declared by the project, not set where the factory runs.
UNSET_DECLARED = "STANDIN_UNSET"


@pytest.fixture
def failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(WITHHELD, WITHHELD_VALUE)
    monkeypatch.delenv(UNSET_DECLARED, raising=False)
    return _a_project(
        tmp_path / "shop", exit_code=3, declares=(DECLARED, UNSET_DECLARED)
    )


def _names_never_values(detail: Any, where: str) -> None:
    text = detail if isinstance(detail, str) else json.dumps(detail)
    assert UNSET_DECLARED in text, f"{where} does not name the declared, unset setting"
    assert WITHHELD in text, f"{where} does not name the setting it was not given"
    assert "minimal environment" in text, where
    assert WITHHELD_VALUE not in text, f"{where} shows a value"
    _no_sentinel(text, where)


class TestAFailedProgramIsToldWhatItWasNotGiven:
    def test_the_declared_test_through_the_helper(
        self, failing: Path, toolchain_loader: ModuleType
    ) -> None:
        from forge.cli._serve_conductor import run_declared_command_in_sandbox

        tree = failing / ".forge" / "worktrees" / "journey-1"
        tree.mkdir(parents=True)
        with _a_helper(_config(failing)) as (_server, url):
            exit_code, detail = run_declared_command_in_sandbox(
                command=str(failing / "deploy" / "test.sh"),
                cwd=tree,
                timeout_seconds=60,
                sandbox=SimpleNamespace(name="shop-sbx", sidecar_url=url),
                repo=REPO,
                launch_settings=[DECLARED, UNSET_DECLARED],
                by_hand=True,
            )
        assert exit_code == 3, detail
        _names_never_values(detail, "the declared test's failure detail")

    def test_the_live_gate_driver_through_the_helper(self, failing: Path) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        with _a_helper(_config(failing)) as (_server, url):
            outcome = SidecarLiveGateInvoker(
                base_url=url,
                repo=REPO,
                repo_path=failing,
                driver_argv=["deploy/journey.sh"],
                timeout_seconds=60,
                launch_settings=[DECLARED, UNSET_DECLARED],
                by_hand=True,
            ).invoke(feature=FEATURE, target="candidate")
        assert outcome.verdict != "pass"
        _names_never_values(outcome.detail, "the live-gate driver's failure detail")

    def test_the_deploy_script(self, failing: Path) -> None:
        from forge.executor.shell_steps import _run_script_step

        exit_code, output = _run_script_step(
            cwd=str(failing),
            script="deploy/deploy.sh",
            env_file=None,
            launch_settings=[DECLARED, UNSET_DECLARED],
        )
        assert exit_code == 3
        _names_never_values(output, "the deploy script's output")

    def test_the_declared_test_without_a_sandbox(self, failing: Path) -> None:
        from forge.cli._serve_conductor import _run_declared_command

        exit_code, detail = _run_declared_command(
            command=str(failing / "deploy" / "test.sh"),
            cwd=failing,
            timeout_seconds=60,
            launch_settings=[DECLARED, UNSET_DECLARED],
        )
        assert exit_code == 3
        _names_never_values(detail, "the declared test's detail (no sandbox)")

    def test_the_live_gate_without_a_sandbox(self, failing: Path) -> None:
        from forge.deploy.live_gate import RepoDriverLiveGateInvoker

        outcome = RepoDriverLiveGateInvoker(
            repo_path=failing,
            driver_argv=[str(failing / "deploy" / "journey.sh")],
            timeout_seconds=60,
            launch_settings=[DECLARED, UNSET_DECLARED],
        ).invoke(feature=FEATURE, target="candidate")
        assert outcome.verdict != "pass"
        _names_never_values(outcome.detail, "the live gate's detail (no sandbox)")

    def test_the_owned_deploy(self, failing: Path, tmp_path: Path) -> None:
        from forge.deploy_sidecar.deploy_executor import DeployExecutor, DeployRequest

        target = f"shop-{tmp_path.name}::live"
        answer = DeployExecutor(
            notes_root=tmp_path / "notes",
            # A stand-in coordinator that agrees this build owns the target.
            ask_the_coordinator=lambda _t: {"counter": 1, "build": BUILD},
        ).run(
            DeployRequest(
                target=target,
                target_counter=1,
                build=BUILD,
                cwd=str(failing),
                script="deploy/deploy.sh",
                launch_settings=(DECLARED, UNSET_DECLARED),
                repository=str(failing),
            )
        )
        assert answer.accepted and answer.exit_code == 3, answer
        _names_never_values(answer.sentence, "the owned deploy's answer")


# ---------------------------------------------------------------------------
# The private home goes only in a real git directory of the right checkout
# ---------------------------------------------------------------------------


class TestTheGitDirectoryIsARealOne:
    def _refused(self, folder: Path, **kwargs: Any) -> str:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(folder, **kwargs)
        return str(refused.value)

    def test_an_empty_folder_called_git_above_is_not_a_git_directory(
        self, tmp_path: Path
    ) -> None:
        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        inner = outer / "inner"
        inner.mkdir()
        self._refused(inner)
        assert not (outer / ".git" / "forge-project-home").exists()

    def test_an_empty_gitdir_line_is_not_a_git_directory(self, tmp_path: Path) -> None:
        odd = tmp_path / "odd"
        odd.mkdir()
        (odd / ".git").write_text("gitdir: \n", encoding="utf-8")
        self._refused(odd)
        assert not (odd / "forge-project-home").exists()

    def test_a_gitdir_naming_the_work_folder_is_refused(self, tmp_path: Path) -> None:
        odd = tmp_path / "odd"
        odd.mkdir()
        (odd / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (odd / "objects").mkdir()
        (odd / ".git").write_text("gitdir: .\n", encoding="utf-8")
        self._refused(odd)
        assert not (odd / "forge-project-home").exists()

    def test_the_walk_stops_at_the_first_git_entry(self, tmp_path: Path) -> None:
        """A real checkout above an empty ``.git`` is never reached."""
        outer = _a_project(tmp_path / "shop")
        nested = outer / "vendor" / "thing"
        (nested / ".git").mkdir(parents=True)
        self._refused(nested)

    def test_nothing_above_the_named_repository_is_used(self, tmp_path: Path) -> None:
        outer = _a_project(tmp_path / "shop")
        inner = outer / "sub"
        inner.mkdir()
        sentence = self._refused(inner, repository=inner)
        assert "sub" in sentence
        assert not (outer / ".git" / "forge-project-home").exists()

    def test_a_work_folder_outside_the_named_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        first = _a_project(tmp_path / "shop")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        self._refused(elsewhere, repository=first)

    def test_the_named_repository_is_used_for_a_tree_inside_it(
        self, tmp_path: Path
    ) -> None:
        from forge.launch_environment import build_project_command_env

        root = _a_project(tmp_path / "shop")
        tree = root / ".forge-candidates" / "FEAT-AB12"
        tree.mkdir(parents=True)
        env = build_project_command_env(tree, repository=root)
        _home_is_private(env, root)

    def test_a_linked_worktree_uses_the_shared_git_directory(
        self, tmp_path: Path
    ) -> None:
        from forge.launch_environment import build_project_command_env

        root = _a_project(tmp_path / "shop")
        tree = root / ".forge" / "worktrees" / "journey-9"
        _git(root, "worktree", "add", "-q", "-b", "repair/nine", str(tree))
        _home_is_private(build_project_command_env(tree), root)
        _home_is_private(build_project_command_env(tree, repository=root), root)


def test_a_request_env_file_that_clashes_with_the_factorys_is_refused(
    project: Path,
) -> None:
    """``ENV_FILE`` is the factory's value for this one command: a request's
    overlay naming it too is refused, as the deploy executor refuses it, and
    nothing runs."""
    from forge.executor.shell_steps import _run_script_step

    exit_code, output = _run_script_step(
        cwd=str(project),
        script="deploy/deploy.sh",
        env_file="/the/project/.env",
        extra_env={"ENV_FILE": "/somewhere/else"},
    )
    assert exit_code != 0
    assert "ENV_FILE" in output
    assert not (project / "env-deploy.json").exists(), "it ran"


# ---------------------------------------------------------------------------
# A value the factory sets may not carry one of the factory's own (14 Oct)
# ---------------------------------------------------------------------------


class TestAnAdditionCannotCarryTheFactorysOwnValues:
    @pytest.mark.parametrize(
        "name",
        [
            "FLEET_MEMORY_PG_DSN",
            "FORGE_NATS_URL",
            "FLEET_MEMORY_NATS_URL",
            "OPENAI_BASE_URL",
            "ANTHROPIC_BASE_URL",
        ],
    )
    def test_a_factory_name_is_refused_as_an_addition(
        self, project: Path, name: str
    ) -> None:
        from forge.launch_environment import build_project_command_env

        with pytest.raises(ValueError) as refused:
            build_project_command_env(project, additions={name: "x"})
        assert name in str(refused.value)

    @pytest.mark.parametrize("name", ["FLEET_MEMORY_PG_DSN", "OPENAI_BASE_URL"])
    def test_an_ordinary_name_carrying_a_factory_value_is_refused(
        self, project: Path, name: str
    ) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(
                project, additions={"DATABASE_URL": SENTINELS[name]}
            )
        assert "DATABASE_URL" in str(refused.value)
        assert name in str(refused.value)
        _no_sentinel(str(refused.value), "the refusal")

    def test_the_additions_the_factory_really_sets_still_arrive(
        self, project: Path
    ) -> None:
        from forge.launch_environment import build_project_command_env

        env = build_project_command_env(
            project,
            additions={
                "ENV_FILE": "/the/project/.env",
                "DEPLOY_IDENTITY": "shop:abc123",
                "DEPLOY_ARTIFACT": "sha256:" + "0" * 64,
            },
        )
        assert env["ENV_FILE"] == "/the/project/.env"
        assert env["DEPLOY_IDENTITY"] == "shop:abc123"

    def test_the_owned_deploy_refuses_an_identity_carrying_a_factory_value(
        self, project: Path, tmp_path: Path
    ) -> None:
        from forge.deploy_sidecar.deploy_executor import DeployExecutor, DeployRequest

        answer = DeployExecutor(
            notes_root=tmp_path / "notes",
            ask_the_coordinator=lambda _t: {"counter": 1, "build": BUILD},
        ).run(
            DeployRequest(
                target=f"shop-{tmp_path.name}::live",
                target_counter=1,
                build=BUILD,
                cwd=str(project),
                script="deploy/deploy.sh",
                identity=SENTINELS["FLEET_MEMORY_PG_DSN"],
                identity_setting="DEPLOY_IDENTITY",
                repository=str(project),
            )
        )
        assert not answer.accepted, answer
        assert "DEPLOY_IDENTITY" in answer.sentence
        _no_sentinel(answer.sentence, "the refusal")
        assert not (project / "env-deploy.json").exists(), "it ran"


# ---------------------------------------------------------------------------
# A named repository's git directory is its own (14 October 2026)
# ---------------------------------------------------------------------------


class TestANamedRepositoryUsesItsOwnGitDirectory:
    def _refused(self, folder: Path, **kwargs: Any) -> None:
        from forge.launch_environment import (
            ProjectCommandEnvironmentError,
            build_project_command_env,
        )

        with pytest.raises(ProjectCommandEnvironmentError):
            build_project_command_env(folder, **kwargs)

    def test_a_gitdir_file_naming_an_unrelated_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        foreign = _a_project(tmp_path / "foreign")
        redirect = tmp_path / "redirect"
        redirect.mkdir()
        (redirect / ".git").write_text(f"gitdir: {foreign / '.git'}\n", encoding="utf-8")
        self._refused(redirect, repository=redirect)
        self._refused(redirect)
        assert not (foreign / ".git" / "forge-project-home").exists()

    def test_a_commondir_naming_an_unrelated_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        foreign = _a_project(tmp_path / "foreign")
        admin = tmp_path / "admin"
        admin.mkdir()
        (admin / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")
        (admin / "commondir").write_text(f"{foreign / '.git'}\n", encoding="utf-8")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
        (admin / "gitdir").write_text(f"{checkout / '.git'}\n", encoding="utf-8")
        self._refused(checkout, repository=checkout)
        self._refused(checkout)
        assert not (foreign / ".git" / "forge-project-home").exists()

    def test_another_checkouts_worktree_folder_is_refused(self, tmp_path: Path) -> None:
        """A ``gitdir`` naming a real worktree folder of a real repository that
        does not point back at this checkout is borrowed, not owned."""
        root = _a_project(tmp_path / "shop")
        tree = root / ".forge" / "worktrees" / "journey-3"
        _git(root, "worktree", "add", "-q", "-b", "repair/three", str(tree))
        impostor = tmp_path / "impostor"
        impostor.mkdir()
        admin = (root / ".git" / "worktrees" / "journey-3").resolve()
        (impostor / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
        self._refused(impostor, repository=impostor)
        self._refused(impostor)

    def test_a_git_directory_with_a_commondir_of_its_own_is_refused(
        self, tmp_path: Path
    ) -> None:
        foreign = _a_project(tmp_path / "foreign")
        odd = _a_project(tmp_path / "odd")
        (odd / ".git" / "commondir").write_text(f"{foreign / '.git'}\n", encoding="utf-8")
        self._refused(odd, repository=odd)


def test_the_coordinators_declared_test_is_anchored_on_its_checkout(
    tmp_path: Path,
) -> None:
    """F6: with no repository named, the command's own working folder is the
    checkout, and nothing above it is looked at."""
    from forge.cli._serve_conductor import _run_declared_command

    root = _a_project(tmp_path / "shop")
    inside = root / "not-a-checkout"
    inside.mkdir()
    exit_code, detail = _run_declared_command(
        command=str(root / "deploy" / "test.sh"), cwd=inside, timeout_seconds=60
    )
    assert exit_code is None, detail
    assert not (inside / "env-test.json").exists(), "it ran"


def test_the_local_stage_tells_its_script_steps_their_checkout(tmp_path: Path) -> None:
    """Local runbook steps: the stage puts the checkout it knows on every step
    that runs the project's scripts."""
    from forge.config.models import DeployStageConfig
    from forge.deploy.runbook_builder import build_deploy_runbook
    from forge.deploy.profile import parse_deploy_profile
    from forge.deploy.stage import DeployStageRunner

    root = _a_project(tmp_path / "shop")
    runner = DeployStageRunner(
        repository=None,
        runbook_publisher=None,
        deploy_publisher=None,
        reservation=None,
        live_gate_invoker=None,
        broker_inspector=None,
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "records"),
        target_repo_root=str(root),
    )
    profile = parse_deploy_profile(
        yaml.safe_load((root / "deploy" / "profile.yaml").read_text())
    )
    runbook = build_deploy_runbook(
        profile, runbook_id="r", target="shop", now=datetime(2026, 10, 14, tzinfo=timezone.utc)
    )
    told = runner._with_the_checkout(runbook)
    scripts = [s for s in told.steps if s.step_type in ("deploy_compose", "health_check")]
    assert scripts and all(s.params["repository"] == str(root) for s in scripts)


# ---------------------------------------------------------------------------
# Every project-script step gets the declared names; F6 and F8 say what was
# withheld when they fail or time out (Codex implementation review 1, R1, R3)
# ---------------------------------------------------------------------------


def _a_local_stage(root: Path, tmp_path: Path) -> Any:
    from unittest.mock import AsyncMock

    from forge.config.models import DeployStageConfig
    from forge.deploy.live_gate import DryRunBrokerInspector, DryRunLiveGateInvoker
    from forge.deploy.reservation import InProcessReservationLease
    from forge.deploy.stage import DeployStageRunner
    from forge.persistence.migrations.runbook import apply
    from forge.persistence.repositories.runbook import RunbookRepository

    conn = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(conn)
    return DeployStageRunner(
        repository=RunbookRepository(connection=conn),
        runbook_publisher=AsyncMock(),
        deploy_publisher=AsyncMock(),
        reservation=InProcessReservationLease(),
        live_gate_invoker=DryRunLiveGateInvoker(),
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "records"),
        target_repo_root=str(root),
        launch_settings=(DECLARED,),
    )


class TestEveryProjectScriptStepGetsTheDeclaredNames:
    def test_a_local_runbook_hands_the_health_check_the_declared_names(
        self, tmp_path: Path
    ) -> None:
        from forge.deploy.profile import parse_deploy_profile
        from forge.deploy.runbook_builder import build_deploy_runbook

        root = _a_project(tmp_path / "shop")
        runner = _a_local_stage(root, tmp_path)
        profile = parse_deploy_profile(
            {**yaml.safe_load((root / "deploy" / "profile.yaml").read_text()), "cwd": str(root)}
        )
        runbook = build_deploy_runbook(
            profile, runbook_id="d1", target="shop", now=datetime(2026, 10, 14, tzinfo=timezone.utc)
        )
        result = asyncio.run(runner._run_runbook(runbook, "corr"))
        assert result.status == "complete", result
        for label in ("deploy", "check"):
            given = _given(root, label)
            assert given[DECLARED] == "present", label
            _no_sentinel(given, f"the local {label} step")
            _home_is_private(given, root)

    def test_a_local_smoke_test_gets_the_declared_names(self, tmp_path: Path) -> None:
        from forge.persistence.repositories.runbook_models import (
            Runbook,
            Step,
            StepStatus,
        )

        root = _a_project(tmp_path / "shop")
        runner = _a_local_stage(root, tmp_path)
        runbook = Runbook(
            runbook_id="s1",
            target="shop",
            steps=(
                Step(
                    step_type="run_smoke_tests",
                    params={"cwd": str(root), "script": "deploy/check.sh"},
                    status=StepStatus.pending,
                    sequence_index=0,
                ),
            ),
            current_step_index=0,
            status=StepStatus.pending,
            created_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        result = asyncio.run(runner._run_runbook(runbook, "corr"))
        assert result.status == "complete", result
        given = _given(root, "check")
        assert given[DECLARED] == "present"
        _no_sentinel(given, "the local smoke test")

    def test_the_health_check_handler_forwards_them_to_its_runner(
        self, tmp_path: Path
    ) -> None:
        from forge.deploy.steps import make_health_check_handler
        from forge.persistence.repositories.runbook_models import Step, StepStatus

        seen: list[dict[str, Any]] = []

        def _runner(**kwargs: Any) -> tuple[int, str]:
            seen.append(kwargs)
            return 0, ""

        make_health_check_handler(dry_run=False, script_runner=_runner)(
            Step(
                step_type="health_check",
                params={
                    "cwd": str(tmp_path),
                    "checks": [{"cmd": "deploy/check.sh"}],
                    "launch_settings": [DECLARED],
                },
                status=StepStatus.pending,
                sequence_index=0,
            )
        )
        assert seen and seen[0]["launch_settings"] == (DECLARED,)


class TestTimeoutsAndTheBuildPackageAreToldWhatWasWithheld:
    def test_the_build_packages_failed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.build.test_verification import verify_tests

        monkeypatch.setenv(WITHHELD, WITHHELD_VALUE)
        root = _a_project(tmp_path / "shop")
        bin_dir = tmp_path / "pytest-bin"
        _writer(bin_dir / "pytest", "pytest", exit_code=1)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        result = asyncio.run(verify_tests(root, test_command="pytest"))
        assert result["passed"] is False
        assert WITHHELD in result["output_tail"]
        assert WITHHELD_VALUE not in json.dumps(result)
        _no_sentinel(json.dumps(result), "the build package's failed run")

    def test_the_build_packages_timed_out_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.build.test_verification import verify_tests

        monkeypatch.setenv(WITHHELD, WITHHELD_VALUE)
        root = _a_project(tmp_path / "shop")
        bin_dir = tmp_path / "pytest-bin"
        bin_dir.mkdir()
        slow = bin_dir / "pytest"
        slow.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        slow.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        result = asyncio.run(verify_tests(root, test_command="pytest", timeout_seconds=1))
        assert result["passed"] is False
        assert WITHHELD in result["output_tail"]
        assert WITHHELD_VALUE not in json.dumps(result)

    def test_the_coordinators_declared_test_that_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli._serve_conductor import _run_declared_command

        monkeypatch.setenv(WITHHELD, WITHHELD_VALUE)
        monkeypatch.delenv(UNSET_DECLARED, raising=False)
        root = _a_project(tmp_path / "shop")
        exit_code, detail = _run_declared_command(
            command="exec sleep 30",
            cwd=root,
            timeout_seconds=1,
            launch_settings=[DECLARED, UNSET_DECLARED],
        )
        assert exit_code is None
        _names_never_values(detail, "the declared test's timeout")


def test_an_attended_deploy_hands_every_project_program_the_declared_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: ``forge deploy`` (the attended path) reads the project's declared
    names at the commit being deployed and hands them to the deploy script,
    the health check and the live-gate driver, while the factory's
    credentials stay absent."""
    from unittest.mock import AsyncMock

    from click.testing import CliRunner

    from forge.cli import _deploy_run
    from forge.cli.deploy import deploy_cmd
    from forge.persistence.migrations.runbook import apply
    from forge.persistence.repositories.runbook import RunbookRepository

    root = _a_project(tmp_path / "shop")
    profile = yaml.safe_load((root / "deploy" / "profile.yaml").read_text())
    profile["cwd"] = str(root)
    profile.pop("preview", None)
    (root / "deploy" / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    _git(root, "commit", "-q", "-am", "deploy from the checkout")

    conn = sqlite3.connect(str(tmp_path / "forge.db"))
    apply(conn)

    async def _backends(config: Any, *, dry_run: bool) -> Any:
        async def _close() -> None:
            return None

        return RunbookRepository(connection=conn), AsyncMock(), AsyncMock(), _close

    monkeypatch.setattr(_deploy_run, "_aopen_backends", _backends)
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {"target_repo_paths": {REPO: str(root)}},
            "deploy": {"enabled": True, "run_live_gate": True},
        }
    )
    result = CliRunner().invoke(deploy_cmd, [FEATURE, "--repo", REPO], obj=config)
    assert result.exit_code == 0, result.output
    for label in ("deploy", "check", "journey"):
        given = _given(root, label)
        assert given.get(DECLARED) == "present", f"the {label} program was not given it"
        _no_sentinel(given, f"the attended deploy's {label} program")
        _home_is_private(given, root)


# ---------------------------------------------------------------------------
# Section 5: a guard that cannot be missed by leaving a site out
# ---------------------------------------------------------------------------
#
# Hardened after the first review (13 October 2026) to the level of the build
# system's own guard: the helper is recognised by where it is imported from,
# not by its name; an environment counts as the helper's only when every
# assignment to it is the helper and nothing changes it before the launch;
# module aliases, launches on an event loop or held in a variable,
# ``functools.partial``, ``getattr`` and ``__import__`` are all seen; and a
# helper call that reads this process's own environment into its arguments
# does not count.

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "forge"

#: The helper, by where it is defined.
HELPERS = {"forge.launch_environment.build_project_command_env"}

#: The factory's own programs, recognised from a literal first argument.
FACTORY_PROGRAMS = {"git", "docker", "pgrep", "ps"}

#: The modules that can start a process, each with all its submodules.
LAUNCH_MODULES: tuple[str, ...] = (
    "subprocess", "os", "asyncio", "pty", "multiprocessing", "posix",
)

#: Every function name the guard knows to start a process.
LAUNCH_NAMES: frozenset[str] = frozenset(
    {
        "run", "call", "check_call", "check_output", "Popen", "getoutput",
        "getstatusoutput", "create_subprocess_shell", "create_subprocess_exec",
        "subprocess_shell", "subprocess_exec", "system", "popen", "spawn",
        "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
        "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe",
        "execv", "execve", "execvp", "execvpe", "posix_spawn",
        "posix_spawnp", "fork", "forkpty", "startfile",
    }
)


@functools.lru_cache(maxsize=None)
def _is_launch_module(dotted: str) -> bool:
    """``dotted`` is a launch module or one of its real submodules (found
    with ``importlib.util.find_spec``), never a function or class inside
    one: ``asyncio.subprocess`` is a module, ``os.getenv`` is not."""
    root = dotted.split(".", 1)[0]
    if root not in LAUNCH_MODULES:
        return False
    if dotted == root:
        return True
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


@functools.lru_cache(maxsize=None)
def _is_launch_bearing_module(dotted: str) -> bool:
    """``dotted`` is a launch module itself, or a real submodule of one that
    defines at least one launch name (14 October 2026): ``subprocess``,
    ``multiprocessing``, ``asyncio.subprocess`` and
    ``multiprocessing.popen_fork`` bear launches; ``os.path`` does not. A
    submodule is decided by importing the (standard library) module and
    reading its attributes; cached."""
    if not _is_launch_module(dotted):
        return False
    if dotted in LAUNCH_MODULES:
        return True
    try:
        module = importlib.import_module(dotted)
    except Exception:  # noqa: BLE001 — a module that cannot be imported here
        return True  # cannot be shown harmless, so it is treated as bearing launches
    return any(_is_launch(f"{dotted}.{name}") and hasattr(module, name) for name in LAUNCH_NAMES)


def _is_launch(dotted: str) -> bool:
    """THE one launch predicate (14 October 2026): a resolved function is a
    launch when its last component is a launch name and its module is a
    launch module or a submodule of one — so ``asyncio.subprocess.
    create_subprocess_shell`` imported directly is as much a launch as
    ``asyncio.create_subprocess_shell``. One carve-out: asyncio's ``run``
    (``asyncio.run``, ``asyncio.Runner.run``) runs a coroutine, not a
    program."""
    module, _, last = dotted.rpartition(".")
    if not module or last not in LAUNCH_NAMES or not _is_launch_module(module):
        return False
    return not (last == "run" and (module == "asyncio" or module.startswith("asyncio.")))
#: Launch methods recognised on any object: ``asyncio.subprocess.create_*``
#: and an event loop's ``subprocess_exec`` / ``subprocess_shell``.
_ANY_RECEIVER_LAUNCHES = {
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec",
    "subprocess_shell",
}
#: Launches allowed by name, each with its one-line reason, keyed by the file
#: (under ``src/forge``) and the function the launch is written in.
ALLOWED: dict[tuple[str, str], str] = {
    ("adapters/gh/operations.py", "_execute"): "runs gh, the factory's own GitHub client",
    ("adapters/git/operations.py", "_default_execute"): "runs git commands the git adapter composes",
    ("adapters/guardkit/run.py", "_execute_subprocess"): "starts GuardKit with the factory's launch list",
    ("build/git_operations.py", "_execute_via_deepagents"): "generic runner; every call to it is checked below",
    ("build/git_operations.py", "_run_via_execute"): "git and gh, the factory's own programs, with its own environment",
    ("build/test_verification.py", "_execute_via_deepagents"): (
        "a pass-through seam onto the git operations' runner; it is a generic "
        "runner itself, so every call to it (verify_tests) is checked"
    ),
    ("build_processes.py", "_engine_ids"): "the container engine, called through a variable",
    ("build_processes.py", "_remove_networks_and_volumes"): "the container engine, called through a variable",
    ("build_processes.py", "remove_fixture_containers"): "the container engine, run in a worker thread",
    ("cli/register_repo.py", "_run_command"): "the operator's register command: git, docker and sbx",
    ("deploy/review_preview.py", "_run_command"): "git and the container engine for the preview's tree",
    ("deploy_sidecar/deploy_executor.py", "_default_spawn"): "generic runner; every call to it is checked below",
    ("deploy_sidecar/deploy_executor.py", "_start"): (
        "the owned deploy: its additions are the request's ENV_FILE, identity "
        "and artifact under names the project declared, and the helper refuses "
        "any of them carrying a value of the factory's own settings"
    ),
    ("deploy_sidecar/service.py", "_start_and_collect"): "generic runner; every call to it is checked below",
    ("deploy_sidecar/service.py", "run_merge_command"): "starts GuardKit (merge, legs, checks) with the factory's launch list",
    ("deploy_sidecar/service.py", "blob"): "git cat-file, the argument list from _git_argv",
    ("deploy_sidecar/service.py", "opened"): "git cat-file, the argument list from _git_argv",
    ("pipeline/routing_stamps.py", "read_last_code_commit_time"): "git log, from a literal git list",
    ("planning/target_terminal_tools.py", "_default_normalizer_subprocess"): "the planner's normalizer, a factory program",
    ("review_gate/record.py", "validate_review_findings"): "guardkit qa validate, a factory program",
    ("subagents/autobuild_runner.py", "_guardkit_feature_validate"): "guardkit feature validate, a factory program",
    ("subagents/autobuild_runner.py", "_run_git"): (
        "git with the local verbs its callers pass (rev-parse, worktree); the "
        "program is the literal git, and its arguments are unpacked, so the "
        "inline-literal rule cannot see it"
    ),
    ("subagents/autobuild_runner.py", "_run_one_build"): "starts GuardKit's build with the factory's launch list",
}

#: Generic runners: every call to them is judged as a launch of its own, and
#: using one as a value (a variable, ``functools.partial``, ``x = self._spawn``)
#: is flagged. ``dotted`` is where each is defined, so an import under another
#: name or a module attribute is still recognised; ``names`` are the
#: attribute names it is reached by on an object (``self._spawn``).
GENERIC_RUNNERS: dict[tuple[str, str], dict[str, Any]] = {
    ("build/git_operations.py", "_execute_via_deepagents"): {
        "dotted": {"forge.build.git_operations._execute_via_deepagents"},
        "names": {"_execute_via_deepagents"},
    },
    ("build/test_verification.py", "_execute_via_deepagents"): {
        "dotted": {"forge.build.test_verification._execute_via_deepagents"},
        "names": {"_execute_via_deepagents"},
    },
    ("deploy_sidecar/deploy_executor.py", "_spawn"): {
        "dotted": {
            "forge.deploy_sidecar.deploy_executor.DeployExecutor._spawn",
            "forge.deploy_sidecar.deploy_executor.DeployExecutor._default_spawn",
        },
        "names": {"_spawn", "_default_spawn"},
        # Its two bodies: the seam-or-real switch and the real launch.
        "bodies": {"_spawn", "_default_spawn"},
    },
    ("deploy_sidecar/service.py", "_start_and_collect"): {
        "dotted": {"forge.deploy_sidecar.service._start_and_collect"},
        "names": {"_start_and_collect"},
    },
}

#: The only two places that may build an issued preview value.
ISSUED_BUILDERS: set[tuple[str, str]] = {
    ("deploy/review_preview.py", "_prepare"),
    ("deploy_sidecar/service.py", "_read_issued_env"),
}
ISSUED_TYPE = "forge.deploy.review_preview.IssuedPreviewValues"


class _Module(SimpleNamespace):
    rel: str
    dotted: str
    tree: ast.Module
    parents: dict[int, ast.AST]
    imports: dict[str, set[str]]
    package: str


def _load_modules(package: Path) -> list[_Module]:
    modules = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        rel = path.relative_to(package).as_posix()
        parts = [package.name, *Path(rel).with_suffix("").parts]
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        dotted = ".".join(parts)
        modules.append(
            _Module(
                rel=rel, dotted=dotted, tree=tree, parents=parents,
                imports=_imports(tree, dotted, is_package),
                package=dotted if is_package else dotted.rsplit(".", 1)[0],
            )
        )
    return modules


def _import_bindings(node: ast.AST, package: str) -> list[tuple[str, str]]:
    """``(local name, what it names)`` for each name one import binds."""
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                out.append((alias.asname, alias.name))
            else:
                first = alias.name.split(".")[0]
                out.append((first, first))
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            anchor = package.split(".")
            anchor = anchor[: len(anchor) - (node.level - 1)]
            base = ".".join([*anchor, base] if base else anchor)
        for alias in node.names:
            out.append((alias.asname or alias.name, f"{base}.{alias.name}"))
    return out


def _imports(tree: ast.Module, dotted: str, is_package: bool) -> dict[str, set[str]]:
    """Every name an import binds anywhere in the module -> EVERY thing it
    names in any scope (14 October 2026: one name can be one module in one
    function and another in another, so a launch is any of them and the
    helper only all of them)."""
    out: dict[str, set[str]] = {}
    package = dotted if is_package else dotted.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        for local, target in _import_bindings(node, package):
            out.setdefault(local, set()).add(target)
    return out


#: The package's own modules by dotted name, for following a name it
#: re-exports to what it finally is (set by :func:`unaccounted_launches`).
_PACKAGE_MODULES: dict[str, _Module] = {}

#: A name whose chain through the package's own modules cannot be followed
#: to an end (a cycle, a missing or doubled binding, a binding other than an
#: import, a definition or one plain assignment): in call position it is
#: treated as a possible launch.
UNRESOLVED = "?unresolved:"

_FINAL_CACHE: dict[str, frozenset[str]] = {}


def _finalise(dotted: str, seen: frozenset[str] = frozenset()) -> set[str]:
    """Follow a name through the package's own modules to its final identity
    (14 October 2026).

    ``forge.a.run`` where ``forge/a.py`` says ``from subprocess import run``
    finally names ``subprocess.run``: the exporting module's own binding of
    the name is followed — an import to what it imports, again and again —
    until the name leaves the package or reaches a single definition or a
    single plain assignment (the exporting module's own direct-use rule keeps
    a launch, a runner or a launch module out of any stored name). A cycle,
    several bindings, any other kind of binding or no binding at all cannot
    be followed and ends ``UNRESOLVED``. A name outside the package, or a
    module itself, is its own end.
    """
    cached = _FINAL_CACHE.get(dotted)
    if cached is not None and not seen:
        return set(cached)
    parts = dotted.split(".")
    if dotted in seen:
        return {UNRESOLVED + dotted}
    # The longest prefix that is one of the package's modules. A name that
    # is both a package binding and a submodule cannot arise: that collision
    # is itself a violation (:func:`_module_name_collisions`).
    readings: list[tuple[str, int]] = []
    for cut in range(len(parts), 0, -1):
        if ".".join(parts[:cut]) in _PACKAGE_MODULES:
            readings.append((".".join(parts[:cut]), cut))
            break
    if not readings:
        return {dotted}
    result: set[str] = set()
    for prefix, cut in readings:
        result |= _follow(_PACKAGE_MODULES[prefix], parts, cut, dotted, seen)
    if not seen:
        _FINAL_CACHE[dotted] = frozenset(result)
    return result


def _follow(
    mod: _Module, parts: list[str], cut: int, dotted: str, seen: frozenset[str]
) -> set[str]:
    """One reading of ``dotted``: ``parts[:cut]`` is the package module
    ``mod``, and ``parts[cut]`` is a name it binds (or ``dotted`` is the
    module itself)."""
    if cut == len(parts):
        return {dotted}
    name, rest = parts[cut], parts[cut + 1:]
    bound = _bindings(mod.tree).get(name, [])
    kinds = {kind for kind, _node in bound}
    if not bound and mod.rel.endswith("__init__.py"):
        # A package's ``__init__`` that does not bind the name: the name is
        # a submodule of the package (one this scan did not load), never a
        # re-export, so it is its own end.
        return {dotted}
    if len(bound) == 1 and kinds <= {"def", "class", "assign", "annassign"}:
        # A single definition, or a single plain assignment, is an end: the
        # exporting module's own direct-use rule already refuses to store a
        # launch function, runner or launch module in a name.
        prefix = ".".join(parts[: cut + 1])
        return {prefix if not rest else f"{prefix}.{'.'.join(rest)}"}
    if bound and kinds == {"import"}:
        result: set[str] = set()
        for _kind, node in bound:
            for local, target in _import_bindings(node, mod.package):
                if local != name:
                    continue
                for final in _finalise(target, seen | {dotted}):
                    if not rest:
                        result.add(final)
                    elif final.startswith(UNRESOLVED):
                        result.add(f"{final}.{'.'.join(rest)}")
                    else:
                        # The remaining attribute path is resolved again from
                        # where the import led (R1): ``a.b.run`` where ``a``
                        # imports ``b`` and ``b`` imports ``subprocess.run``.
                        result |= _finalise(
                            f"{final}.{'.'.join(rest)}", seen | {dotted}
                        )
        return result or {UNRESOLVED + dotted}
    return {UNRESOLVED + dotted}


def _targets(mod: _Module, name: str) -> set[str]:
    """Every final identity a name imported in ``mod`` may have."""
    out: set[str] = set()
    for target in mod.imports.get(name, set()):
        out |= _finalise(target)
    return out


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return "?"


def _last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _enclosing(mod: _Module, node: ast.AST, kinds: Any) -> ast.AST | None:
    current = mod.parents.get(id(node))
    while current is not None and not isinstance(current, kinds):
        current = mod.parents.get(id(current))
    return current


def _enclosing_function(mod: _Module, node: ast.AST) -> ast.AST | None:
    return _enclosing(mod, node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _qualname(mod: _Module, func: Any) -> str:
    names = [func.name]
    current = mod.parents.get(id(func))
    while current is not None:
        if isinstance(current, ast.ClassDef):
            names.append(current.name)
        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(f"{current.name}.<locals>")
        current = mod.parents.get(id(current))
    return ".".join(reversed(names))


#: Each scope's bindings, worked out once per guard run (cleared by
#: :func:`unaccounted_launches`): scope id -> name -> [(kind, node)].
_SCOPE_CACHE: dict[int, dict[str, list[tuple[str, ast.AST]]]] = {}


def _bindings(scope: ast.AST) -> dict[str, list[tuple[str, ast.AST]]]:
    """EVERY binding made directly in ``scope`` (a module, function or
    lambda), by name: the one enumerator every "bound once", "shadowed" and
    "reserved name" decision uses (14 October 2026).

    Kinds: ``param`` (a parameter), ``assign`` (a plain assignment), ``augassign``,
    ``annassign``, ``walrus``, ``for``, ``with``, ``except``, ``comprehension``,
    ``import`` (an import or from-import alias), ``def`` and ``class``,
    ``global``, ``nonlocal``, ``del`` and ``match`` (a match-case capture).
    Nested function, class and lambda bodies are scopes of their own and are
    not looked inside, but their names, defaults and decorators belong here.
    """
    cached = _SCOPE_CACHE.get(id(scope))
    if cached is not None:
        return cached
    found: dict[str, list[tuple[str, ast.AST]]] = {}

    def bind(name: str | None, kind: str, node: ast.AST) -> None:
        if name:
            found.setdefault(name, []).append((kind, node))

    def targets_of(target: ast.AST, kind: str, node: ast.AST) -> None:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                bind(sub.id, kind, node)

    pending: list[ast.AST] = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            bind(arg.arg, "param", arg)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                bind(arg.arg, "param", arg)
        pending = list(scope.body) if not isinstance(scope, ast.Lambda) else [scope.body]
    else:
        pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, "class" if isinstance(node, ast.ClassDef) else "def", node)
            # Its decorators, defaults and bases are evaluated here.
            pending.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                pending.extend(node.bases)
                pending.extend(kw.value for kw in node.keywords)
            else:
                pending.extend(d for d in node.args.defaults)
                pending.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            pending.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for local, _target in _import_bindings(node, ""):
                bind(local, "import", node)
            continue
        if isinstance(node, ast.Global):
            for name in node.names:
                bind(name, "global", node)
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                bind(name, "nonlocal", node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                targets_of(target, "assign", node)
        elif isinstance(node, ast.AugAssign):
            targets_of(node.target, "augassign", node)
        elif isinstance(node, ast.AnnAssign):
            targets_of(node.target, "annassign", node)
        elif isinstance(node, ast.NamedExpr):
            targets_of(node.target, "walrus", node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets_of(node.target, "for", node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    targets_of(item.optional_vars, "with", node)
        elif isinstance(node, ast.ExceptHandler):
            bind(node.name, "except", node)
        elif isinstance(node, ast.comprehension):
            targets_of(node.target, "comprehension", node)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                targets_of(target, "del", node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            bind(node.name, "match", node)
        elif isinstance(node, ast.MatchMapping):
            bind(node.rest, "match", node)
        pending.extend(ast.iter_child_nodes(node))
    _SCOPE_CACHE[id(scope)] = found
    return found


def _scope_bindings(scope: ast.AST, name: str) -> tuple[set[str], ast.AST | None]:
    """``({"def"|"import"|"other"}, the definition)`` for ``name`` in ``scope``."""
    kinds: set[str] = set()
    definition: ast.AST | None = None
    for kind, node in _bindings(scope).get(name, []):
        if kind in ("def", "class"):
            kinds.add("def")
            definition = node
        elif kind == "import":
            kinds.add("import")
        else:
            kinds.add("other")
    return kinds, definition


def _defining_scope(mod: _Module, at: ast.AST) -> ast.AST | None:
    """The function whose scope a name at ``at`` is looked up in.

    A parameter's default, an annotation and a decorator are evaluated where
    the function is DEFINED, not inside it, so for those the scope is the
    function enclosing the definition (14 October 2026: a default such as
    ``_start_and_collect=_start_and_collect`` names the runner itself).
    """
    child: ast.AST = at
    current = mod.parents.get(id(at))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            evaluated_outside = (
                child is current.args
                or any(child is d for d in current.decorator_list)
                or child is current.returns
            )
            if not evaluated_outside:
                return current
        if isinstance(current, ast.Lambda) and child is current.args:
            pass  # a lambda's defaults are evaluated where it is written
        child, current = current, mod.parents.get(id(current))
    return None


def _resolve_all(mod: _Module, func_node: ast.AST, at: ast.AST) -> set[str]:
    """Every dotted name a call target may refer to (empty when unknown).

    A bare name is looked up as Python would: in each enclosing function,
    innermost first, then in the module. Bound in a function by anything but
    a nested definition or an import, it is unknown; so is a module name
    rebound at module level after it is imported. An imported name may name
    several things, one per import of it anywhere in the module.
    """
    if isinstance(func_node, ast.Name):
        name = func_node.id
        scope = _defining_scope(mod, at)
        while scope is not None:
            kinds, definition = _scope_bindings(scope, name)
            if "other" in kinds:
                return set()
            if "def" in kinds and definition is not None:
                if "import" in kinds:
                    return set()
                return {f"{mod.dotted}.{_qualname(mod, definition)}"}
            if "import" in kinds:
                return set(_targets(mod, name))
            scope = _enclosing_function(mod, scope)
        kinds, _definition = _scope_bindings(mod.tree, name)
        if "other" in kinds or ("def" in kinds and "import" in kinds):
            return set()
        if "def" in kinds:
            return {f"{mod.dotted}.{name}"}
        return set(_targets(mod, name))
    if isinstance(func_node, ast.Attribute):
        value = func_node.value
        if isinstance(value, ast.Name) and value.id in ("self", "cls"):
            klass = _enclosing(mod, at, ast.ClassDef)
            if klass is not None:
                return {f"{mod.dotted}.{klass.name}.{func_node.attr}"}
            return set()
        out: set[str] = set()
        for base in _resolve_all(mod, value, at):
            if base.startswith(UNRESOLVED):
                # An unresolved base stays unresolved down the chain (R2).
                out.add(f"{base}.{func_node.attr}")
            else:
                out |= _finalise(f"{base}.{func_node.attr}")
        return out
    return set()


def _resolve(mod: _Module, func_node: ast.AST, at: ast.AST) -> str | None:
    """The one dotted name a call target refers to, or ``None`` when it is
    unknown or could be more than one thing."""
    every = _resolve_all(mod, func_node, at)
    return next(iter(every)) if len(every) == 1 else None


def _assignments(func: ast.AST, name: str) -> list[ast.AST]:
    values: list[ast.AST] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                values.append(node.value)
            elif any(
                isinstance(sub, ast.Name) and sub.id == name
                for t in node.targets
                if isinstance(t, (ast.Tuple, ast.List))
                for sub in ast.walk(t)
            ):
                values.append(ast.Constant(value="<unpacked>"))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:
                values.append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.NamedExpr)):
            targets = (
                [node.target] if isinstance(node, (ast.For, ast.AsyncFor, ast.NamedExpr))
                else [i.optional_vars for i in node.items if i.optional_vars is not None]
            )
            for target in targets:
                if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target)):
                    values.append(ast.Constant(value="<bound by a loop or with>"))
    return values


def _is_parameter(func: Any, name: str) -> bool:
    args = func.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    every += [a for a in (args.vararg, args.kwarg) if a is not None]
    return any(a.arg == name for a in every)


def _reads_this_process_environment(
    mod: _Module, node: ast.AST, func: Any = None, seen: frozenset[str] = frozenset()
) -> bool:
    """True when ``node`` reads the launching process's own environment
    (``os.environ`` or ``os.getenv``), directly or through a local name that
    was assigned from such a read in ``func``: a helper call fed from it
    could pass a factory credential under a name the helper does not refuse."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in ("environ", "environb", "getenv"):
            if isinstance(sub.value, ast.Name) and "os" in _targets(mod, sub.value.id):
                return True
        if isinstance(sub, ast.Name) and _targets(mod, sub.id) & {"os.environ", "os.getenv"}:
            return True
        if (
            func is not None
            and isinstance(sub, ast.Name)
            and sub.id not in seen
            and any(
                _reads_this_process_environment(mod, value, func, seen | {sub.id})
                for value in _assignments(func, sub.id)
            )
        ):
            return True
    return False


#: The helper's arguments that put values into the program's environment by
#: a name the helper does not check against the project's declarations.
_VALUE_ARGUMENTS = {"additions", "parent"}


def _feeds_values_from_parameters(call: ast.Call, func: Any) -> bool:
    """True when a parameter of ``func`` flows into the helper's ``additions``
    or ``parent``: a wrapper like that is only as clean as its every caller."""
    if func is None:
        return False
    for kw in call.keywords:
        if kw.arg in _VALUE_ARGUMENTS:
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Name) and _is_parameter(func, sub.id):
                    return True
    return False


def _inline_helper(node: ast.AST | None, mod: _Module, func: Any, wrappers: set[str]) -> bool:
    """``node`` is a call to the helper — or to a module-level function that
    only ever returns one inline — written right there by its plain name,
    with no argument fed from this process's own environment.

    A call through an attribute (``self.env()``, ``cls.env()``,
    ``obj.env()``) never counts (14 October 2026): which method runs is
    decided at run time, by the instance's class, a subclass's override or
    a conditional definition, so no definition can vouch for it.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    target = _resolve(mod, node.func, node)
    if target in HELPERS:
        arguments = [*node.args, *(kw.value for kw in node.keywords)]
        return not any(_reads_this_process_environment(mod, a, func) for a in arguments)
    return target in wrappers


def _is_launch_or_runner(mod: _Module, call: ast.Call) -> bool:
    return bool(_launch_kind(mod, call.func) or _runner_named(mod, call.func))


def _single_use_helper_name(name: str, mod: _Module, func: Any, wrappers: set[str]) -> bool:
    """``name`` has exactly ONE binding in ``func`` — a single-target
    assignment of an inline helper call — and every other use of it is as the
    ``env=`` keyword of a launch or generic-runner call in ``func`` itself:
    never an attribute, a subscript, an argument to another call, a return
    value, an alias or a closure capture. Any other binding of the name (an
    import, a definition, a loop or match capture, a ``del``...) disqualifies
    it."""
    bound = _bindings(func).get(name, [])
    if len(bound) != 1:
        return False
    kind, node = bound[0]
    if (
        kind != "assign"
        or not isinstance(node, ast.Assign)
        or len(node.targets) != 1
        or not isinstance(node.targets[0], ast.Name)
    ):
        return False
    if not _inline_helper(node.value, mod, func, wrappers):
        return False
    for node in ast.walk(func):
        if not (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)):
            continue
        parent = mod.parents.get(id(node))
        if not (isinstance(parent, ast.keyword) and parent.arg == "env"):
            return False  # an attribute, subscript, argument, return, alias ...
        call = mod.parents.get(id(parent))
        if not isinstance(call, ast.Call) or not _is_launch_or_runner(mod, call):
            return False
        scope = mod.parents.get(id(call))
        while scope is not None and scope is not func:
            if isinstance(scope, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return False  # captured by a closure
            scope = mod.parents.get(id(scope))
    return True


def _helper_shaped(node: ast.AST | None, mod: _Module, func: Any, wrappers: set[str]) -> bool:
    """True when ``node`` (an ``env=`` value) is, provably, the helper's
    result: the helper called inline, or a name that holds nothing else and
    is used for nothing else (:func:`_single_use_helper_name`)."""
    if node is None:
        return False
    if _inline_helper(node, mod, func, wrappers):
        return True
    if isinstance(node, ast.Name) and func is not None:
        return _single_use_helper_name(node.id, mod, func, wrappers)
    return False


#: How many definitions each full qualified identity has in a module, worked
#: out once per guard run (cleared by :func:`unaccounted_launches`).
_QUALNAME_CACHE: dict[int, dict[str, int]] = {}


def _definitions_by_identity(mod: _Module) -> dict[str, int]:
    counted = _QUALNAME_CACHE.get(id(mod.tree))
    if counted is None:
        counted = {}
        for node in ast.walk(mod.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = _qualname(mod, node)
                counted[name] = counted.get(name, 0) + 1
        _QUALNAME_CACHE[id(mod.tree)] = counted
    return counted


def _bound_once_where_defined(mod: _Module, node: Any) -> bool:
    scope = _enclosing(mod, node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or mod.tree
    bound = _bindings(scope).get(node.name, [])
    return len(bound) == 1 and bound[0][1] is node


def _defined_once(mod: _Module, node: Any) -> bool:
    """``node`` is the ONLY definition of its full qualified identity in the
    module, and it and every class or function enclosing it is bound exactly
    once in its own scope, by the single binding enumerator: no second
    definition, no conditional alternative (two conditional ``class X:``
    blocks each with a method of the same name included), no later
    rebinding of any form (14 October 2026). Only such a function's identity
    is trusted."""
    if _definitions_by_identity(mod).get(_qualname(mod, node), 0) != 1:
        return False
    current: Any = node
    while current is not None:
        if not _bound_once_where_defined(mod, current):
            return False
        current = _enclosing(mod, current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    return True


def _wrappers(modules: list[_Module]) -> set[str]:
    """MODULE-LEVEL functions whose whole body is ``return <helper call>``
    (or a call to another such function), by dotted name, none of whose
    parameters flows into the helper's ``additions`` or ``parent``. A method
    or a nested function is never a wrapper: it is reached by dispatch or
    by a closure, not by one fixed definition (14 October 2026)."""
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for mod in modules:
            for node in mod.tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                full = f"{mod.dotted}.{_qualname(mod, node)}"
                if full in found:
                    continue
                # Only a function defined exactly once in its scope: a second
                # (or conditional) definition, or a later rebinding, can put
                # something else under the same name.
                if not _defined_once(mod, node):
                    continue
                # Never a decorated function: a decorator can turn the
                # helper's refusal into None, and env=None inherits everything.
                if node.decorator_list:
                    continue
                # Its body, a leading docstring aside, is exactly one
                # statement: ``return <helper call>`` (or a call to another
                # such function). Anything else can fall through to an
                # implicit None, and env=None inherits everything.
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body = body[1:]
                if len(body) != 1 or not isinstance(body[0], ast.Return):
                    continue
                only = body[0].value
                if isinstance(only, ast.Call) and _feeds_values_from_parameters(only, node):
                    continue
                if _inline_helper(only, mod, node, found):
                    found.add(full)
                    changed = True
    return found


def _launch_kind(mod: _Module, node: ast.AST) -> str | None:
    """What launch ``node`` (a call target or a value) names, if any."""
    candidates: set[str] = set()
    if isinstance(node, ast.Attribute):
        if node.attr in _ANY_RECEIVER_LAUNCHES:
            return _dotted(node)
        candidates |= _resolve_all(mod, node, node)
        if isinstance(node.value, ast.Name):
            candidates |= {f"{t}.{node.attr}" for t in _targets(mod, node.value.id)}
    elif isinstance(node, ast.Name):
        candidates |= _resolve_all(mod, node, node) | _targets(mod, node.id)
    for target in sorted(candidates):
        if _is_launch(target) or target.startswith(UNRESOLVED):
            return target
    return None


def _factory_program(call: ast.Call) -> bool:
    """True only for a factory program launched by an inline literal.

    The simplified rule (14 October 2026, after a review found a named
    command could be aliased and changed, and ``executable=`` could replace
    the program): a launch skips the environment check as one of the
    factory's own programs ONLY when

    * its command is a list or tuple literal written inline in the launch
      call as its only positional argument, whose first element is the
      literal ``"git"``, ``"docker"``,
      ``"pgrep"`` or ``"ps"`` — or, for ``create_subprocess_exec``, whose
      program is that literal written as the first positional argument; and
    * the call has no ``executable=``, no ``shell=`` other than the literal
      ``False``, no ``**`` and no ``*`` unpacking.

    A named command and a module-level constant never qualify: such a launch
    uses the helper's environment or is on the allowed list with a reason.
    """
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return False
    for kw in call.keywords:
        if kw.arg is None or kw.arg == "executable":
            return False
        if kw.arg == "shell" and not (
            isinstance(kw.value, ast.Constant) and kw.value.value is False
        ):
            return False
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        # The ONLY positional argument: a second one can be the executable
        # (Popen's third) or the shell switch.
        return (
            len(call.args) == 1
            and bool(first.elts)
            and isinstance(first.elts[0], ast.Constant)
            and first.elts[0].value in FACTORY_PROGRAMS
        )
    return (
        _last_name(call.func) == "create_subprocess_exec"
        and isinstance(first, ast.Constant)
        and first.value in FACTORY_PROGRAMS
    )


def _env_keyword(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "env":
            return kw.value
    return None


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Nodes inside type annotations: naming a launch as a type runs nothing."""
    found: set[int] = set()
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            roots.append(node.returns)
    for root in roots:
        found.update(id(n) for n in ast.walk(root))
    return found


def _runner_named(mod: _Module, node: ast.AST) -> tuple[str, str] | None:
    """Which generic runner ``node`` names, if any: by where it is defined
    (an import under any name, a module attribute) or, on an object, by the
    attribute name it is reached by (``self._spawn``)."""
    every = _resolve_all(mod, node, node) if isinstance(node, (ast.Name, ast.Attribute)) else set()
    target = next(iter(every)) if len(every) == 1 else None
    for key, spec in GENERIC_RUNNERS.items():
        if every & spec["dotted"]:
            return key
    for key, spec in GENERIC_RUNNERS.items():
        if isinstance(node, ast.Attribute) and node.attr in spec["names"]:
            return key
        if isinstance(node, ast.Name) and node.id in spec["names"] and target is None:
            return key
    return None


def _reserved_names() -> set[str]:
    """Names that may never be rebound: the helper's, and every generic
    runner's."""
    reserved = {target.rsplit(".", 1)[1] for target in HELPERS}
    for (_rel, fname), spec in GENERIC_RUNNERS.items():
        reserved.add(fname)
        reserved.update(spec["names"])
    return reserved


def _reserved_name_rebindings(modules: list[_Module]) -> list[str]:
    """Every rebinding of a reserved name, and every launch imported under
    another name.

    A reserved name is the helper's, a generic runner's or a launch
    function's — an imported alias of one included. In every module,
    function and lambda scope, each binding of one (found by the one binding
    enumerator, :func:`_bindings`) is flagged unless it is an import of the
    very thing itself, or a module-level definition of a name the module
    does not also import. Separately, a launch module or launch function
    imported under a different local name (``as``) is flagged anywhere.
    """
    base = _reserved_names()
    problems: list[str] = []
    for mod in modules:
        reserved = set(base)
        for alias in mod.imports:
            for target in _targets(mod, alias):
                last = target.rpartition(".")[2]
                if last in base or target in HELPERS or _is_launch(target):
                    reserved.add(alias)

        def flag(node: ast.AST, sentence: str) -> None:
            holder = _enclosing(mod, node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            where = getattr(holder, "name", "<module>")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                outer = _enclosing(mod, node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                where = getattr(outer, "name", "<module>")
            problems.append(f"{mod.rel}:{getattr(node, 'lineno', 0)} in {where}: {sentence}")

        def is_the_thing_itself(local: str, node: ast.AST) -> bool:
            for name, target in _import_bindings(node, mod.package):
                if name != local:
                    continue
                for final in _finalise(target):
                    if not (
                        final in HELPERS
                        or any(final in spec["dotted"] for spec in GENERIC_RUNNERS.values())
                        or _is_launch(final)
                        or _is_launch_module(final)
                    ):
                        return False
            return True

        scopes: list[ast.AST] = [mod.tree] + [
            n for n in ast.walk(mod.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        for scope in scopes:
            for name, bound in _bindings(scope).items():
                if name not in reserved:
                    continue
                for kind, node in bound:
                    if kind == "import" and is_the_thing_itself(name, node):
                        continue
                    if (
                        kind in ("def", "class")
                        and scope is mod.tree
                        and name not in mod.imports
                    ):
                        continue
                    flag(
                        node,
                        f"the reserved name {name!r} (the helper's, a generic "
                        f"runner's or a launch function's) is rebound by a "
                        f"{kind} binding",
                    )
        # A launch module or launch function imported under another name, by
        # the one launch predicate on the resolved import.
        for node in ast.walk(mod.tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for alias, (local, target) in zip(node.names, _import_bindings(node, mod.package)):
                if not alias.asname or local == alias.name.rpartition(".")[2]:
                    continue
                if _is_launch(target):
                    flag(node, f"the launch function {target} is imported as {local!r}")
                elif _is_launch_module(target):
                    flag(node, f"the launch module {target} is imported as {local!r}")
    return problems


def _guarded_callables() -> set[str]:
    """Names that may only ever be called: the helper's and every generic
    runner's."""
    names = {target.rsplit(".", 1)[1] for target in HELPERS}
    for (_rel, fname), spec in GENERIC_RUNNERS.items():
        names.add(fname)
        names.update(spec["names"])
        names.update(spec.get("bodies", ()))
    return names


def _direct_use_violations(modules: list[_Module]) -> list[str]:
    """The direct-use-only rule (14 October 2026).

    For every guarded name — a launch module, a launch function, a generic
    runner and the helper:

    1. it is imported only under its own name: any ``as`` is a violation;
    2. every reference to it is a direct use: a function, runner or the
       helper only as the ``func`` of a call; a module only as the base of an
       attribute, and when that attribute is a launch function, that
       attribute only as the ``func`` of a call. Any other reference — an
       assignment, an argument, a keyword value, a return, a default, a
       container, a lambda's value, a decorator — is a violation, and so is
       rebinding a runner or the helper as an attribute.

    Type annotations name things without running them and are left out.
    """
    callables = _guarded_callables()
    problems: list[str] = []
    for mod in modules:
        skip = _annotation_nodes(mod.tree)

        def report(node: ast.AST, sentence: str) -> None:
            holder = _enclosing_function(mod, node) or _enclosing(mod, node, ast.ClassDef)
            where = getattr(holder, "name", "<module>")
            problems.append(f"{mod.rel}:{getattr(node, 'lineno', 0)} in {where}: {sentence}")

        runner_dotted = {d for spec in GENERIC_RUNNERS.values() for d in spec["dotted"]}

        def names_a_callable(name: str) -> bool:
            """The name itself is guarded, or an import makes it one."""
            if name in callables:
                return True
            return any(
                t in HELPERS or t in runner_dotted or t.rpartition(".")[2] in callables
                for t in _targets(mod, name)
            )

        def called(node: ast.AST) -> bool:
            parent = mod.parents.get(id(node))
            return isinstance(parent, ast.Call) and parent.func is node

        for node in ast.walk(mod.tree):
            if id(node) in skip:
                continue
            # (1) imported only under its own name. Every import — relative
            # ones included — is first resolved to the absolute name it binds
            # by the one resolver (:func:`_import_bindings`, 14 October 2026).
            # An ``as`` is a violation when the imported name's last
            # component is a guarded name's (the helper's, a runner's or a
            # launch module's), whatever module text precedes it, or when it
            # is a launch function of a launch module. Generic words (run,
            # call, spawn ...) count only on a launch module.
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                # A wildcard import binds names nobody can see, so nothing it
                # brings in can be checked (14 October 2026).
                where = "." * node.level + (node.module or "")
                report(node, f"wildcard import from {where}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                aliases = [a for a in node.names if a.asname]
                for alias, (local, target) in zip(
                    node.names, _import_bindings(node, mod.package)
                ):
                    if alias not in aliases or local == alias.name.rpartition(".")[2]:
                        continue
                    last = target.rpartition(".")[2]
                    if (
                        last in callables
                        or last in LAUNCH_MODULES
                        or target in HELPERS
                        or target in runner_dotted
                        or _is_launch(target)
                        or _is_launch_module(target)
                    ):
                        report(node, f"{target} is imported as {local!r}")
            # (2) every reference is a direct use
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                targets = _targets(mod, node.id)
                if names_a_callable(node.id) and not called(node):
                    report(node, f"{node.id} is referred to other than by calling it")
                elif any(_is_launch(t) for t in targets) and not called(node):
                    report(node, f"the launch function {node.id} is referred to other than by calling it")
                elif any(_is_launch_module(t) for t in targets):
                    parent = mod.parents.get(id(node))
                    if not (isinstance(parent, ast.Attribute) and parent.value is node):
                        if any(_is_launch_bearing_module(t) for t in targets):
                            report(node, f"the launch module {node.id} is referred to other than as the base of an attribute")
                    elif any(
                        _is_launch(f"{m}.{parent.attr}") for m in targets if _is_launch_module(m)
                    ) and not called(parent):
                        report(
                            node,
                            f"the launch function {node.id}.{parent.attr} is referred "
                            "to other than by calling it",
                        )
            elif isinstance(node, ast.Attribute):
                if node.attr in callables or node.attr in _ANY_RECEIVER_LAUNCHES:
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        report(node, f"{node.attr} is rebound as an attribute")
                    elif not called(node):
                        report(node, f"{node.attr} is referred to other than by calling it")
                elif isinstance(node.ctx, ast.Load) and not called(node):
                    # The WHOLE dotted chain, at any depth (14 October 2026):
                    # ``multiprocessing.popen_fork.Popen`` is a launch however
                    # deep it sits, and held as a value it cannot be checked.
                    chain = sorted(_attribute_chain_targets(mod, node))
                    launch = next(
                        (
                            t for t in chain
                            if _is_launch(t)
                            or (
                                t.startswith(UNRESOLVED)
                                and t.rpartition(".")[2] in LAUNCH_NAMES
                            )
                        ),
                        None,
                    )
                    parent = mod.parents.get(id(node))
                    bearing = next((t for t in chain if _is_launch_bearing_module(t)), None)
                    if launch is not None:
                        report(node, f"the launch function {launch} is referred to other than by calling it")
                    elif bearing is not None and not (
                        isinstance(parent, ast.Attribute) and parent.value is node
                    ):
                        # A submodule that bears launches, held or passed on,
                        # lets a launch be called through it unseen (R2).
                        report(
                            node,
                            f"the launch-bearing module {bearing} is referred to other "
                            "than as the base of an attribute",
                        )
    return problems


def _attribute_chain_targets(mod: _Module, node: ast.Attribute) -> set[str]:
    """Every dotted name an attribute chain ``a.b.c`` may refer to: its base
    name resolved (by scope, and by every import of it in the module), then
    each attribute in turn."""
    attributes: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        attributes.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return set()
    # Component by component, as :func:`_resolve_all` does: each attribute
    # is resolved from what the chain so far finally is, and an unresolved
    # step stays unresolved to the end (14 October 2026, review 3, R4).
    names = _resolve_all(mod, current, current) | _targets(mod, current.id)
    for attribute in reversed(attributes):
        step: set[str] = set()
        for base in names:
            if base.startswith(UNRESOLVED):
                step.add(f"{base}.{attribute}")
            else:
                step |= _finalise(f"{base}.{attribute}")
        names = step
    return names


#: Names no module or package inside the scanned package may have: the launch
#: module roots, and ``nt``.
FORBIDDEN_MODULE_NAMES: frozenset[str] = frozenset({*LAUNCH_MODULES, "nt"})

#: The real source's two existing name collisions, reported for a decision
#: (14 October 2026) rather than renamed in this change. Each is keyed by the
#: file and the name; remove the entry when the name is changed.
PENDING_NAME_DECISIONS: dict[tuple[str, str], str] = {
    ("pipeline/dispatchers/subprocess.py", "subprocess"): (
        "the subprocess-stage dispatcher, imported as "
        "forge.pipeline.dispatchers.subprocess by serve and three other "
        "modules; awaiting a decision to rename"
    ),
    ("discovery/__init__.py", "resolve"): (
        "the package re-exports the function resolve from its own submodule "
        "resolve (from forge.discovery.resolve import resolve); awaiting a "
        "decision to rename one of the two"
    ),
}


def _module_name_collisions(modules: list[_Module]) -> list[str]:
    """Two blunt rules that make name collisions impossible (14 October 2026,
    review 3, R4: the coordinator's decision to stop emulating import
    semantics for them, as with aliases).

    1. No module or package inside the scanned package is named like a launch
       module root (``subprocess``, ``os``, ``asyncio``, ``pty``,
       ``multiprocessing``, ``posix``, ``nt``).
    2. A package's ``__init__`` binds no name that is also the name of one
       of its own submodules or subpackages, except the plain import of that
       very submodule (``from pkg import sub``, ``from . import sub``); any
       other binding of it — an import of something else, a definition, a
       class, an assignment — is a violation.
    """
    problems: list[str] = []
    for mod in modules:
        parts = Path(mod.rel).with_suffix("").parts
        for part in parts:
            if part in FORBIDDEN_MODULE_NAMES and (mod.rel, part) not in PENDING_NAME_DECISIONS:
                problems.append(
                    f"{mod.rel}:0 in <module>: a module or package is named "
                    f"{part!r}, the name of a launch module"
                )
                break
    by_dotted = {mod.dotted: mod for mod in modules}
    for mod in modules:
        if not mod.rel.endswith("__init__.py"):
            continue
        children = {
            dotted.rpartition(".")[2]
            for dotted in by_dotted
            if dotted.rpartition(".")[0] == mod.dotted and dotted != mod.dotted
        }
        bound = _bindings(mod.tree)
        for child in sorted(children):
            if (mod.rel, child) in PENDING_NAME_DECISIONS:
                continue
            for kind, node in bound.get(child, []):
                if kind == "import" and all(
                    target == f"{mod.dotted}.{child}"
                    for local, target in _import_bindings(node, mod.package)
                    if local == child
                ):
                    continue  # the plain import of that very submodule
                problems.append(
                    f"{mod.rel}:{getattr(node, 'lineno', 0)} in <module>: the "
                    f"package binds {child!r} by a {kind} binding, and {child!r} "
                    "is also one of its own submodules"
                )
    return problems


def unaccounted_launches(package: Path) -> list[str]:
    _SCOPE_CACHE.clear()
    _QUALNAME_CACHE.clear()
    _FINAL_CACHE.clear()
    modules = _load_modules(package)
    _PACKAGE_MODULES.clear()
    _PACKAGE_MODULES.update({m.dotted: m for m in modules})
    wrappers = _wrappers(modules)
    problems_of_trust = _untrusted_definitions(modules)
    problems: list[str] = []

    def place(mod: _Module, node: ast.AST) -> tuple[Any, str]:
        func = _enclosing_function(mod, node)
        named = func or _enclosing(mod, node, ast.ClassDef)
        return func, getattr(named, "name", "<module>")

    def judge(mod: _Module, call: ast.Call, what: str) -> None:
        func, fname = place(mod, call)
        if (mod.rel, fname) in ALLOWED and func is not None and _defined_once(mod, func):
            return
        if _factory_program(call):
            return
        env = _env_keyword(call)
        if _helper_shaped(env, mod, func, wrappers):
            return
        reason = "no env= (it inherits everything)" if env is None else "env= is not the helper's"
        problems.append(f"{mod.rel}:{call.lineno} in {fname}: {what}: {reason}")

    def flag(mod: _Module, node: ast.AST, sentence: str) -> None:
        _, fname = place(mod, node)
        if (mod.rel, fname) not in ALLOWED:
            problems.append(f"{mod.rel}:{node.lineno} in {fname}: {sentence}")

    for mod in modules:
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call):
                kind = _launch_kind(mod, node.func)
                if kind:
                    judge(mod, node, f"process launch {kind}")
                elif (
                    _dotted(node.func) in ("__import__", "importlib.import_module", "import_module")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and _is_launch_module(str(node.args[0].value))
                ):
                    flag(
                        mod, node,
                        f"the launch module {node.args[0].value} is imported by "
                        "name, so its launches cannot be checked",
                    )
                elif (
                    _dotted(node.func) == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and _launch_kind(
                        mod, ast.Attribute(value=node.args[0], attr=node.args[1].value, ctx=ast.Load())
                    )
                ):
                    flag(
                        mod, node,
                        "a launch function looked up with getattr, so its env= "
                        "cannot be checked",
                    )
                elif _resolve(mod, node.func, node) == ISSUED_TYPE:
                    _, fname = place(mod, node)
                    if (mod.rel, fname) not in ISSUED_BUILDERS:
                        problems.append(
                            f"{mod.rel}:{node.lineno} in {fname}: IssuedPreviewValues "
                            "is built outside the preview's preparation and the "
                            "helper's reader"
                        )
    problems.extend(_reserved_name_rebindings(modules))
    problems.extend(_direct_use_violations(modules))
    for (rel, fname), spec in GENERIC_RUNNERS.items():
        for mod in modules:
            for node in ast.walk(mod.tree):
                if not isinstance(node, ast.Call) or _runner_named(mod, node.func) != (rel, fname):
                    continue
                func = _enclosing_function(mod, node)
                if (
                    mod.rel == rel
                    and getattr(func, "name", None) in spec.get("bodies", {fname})
                    and _defined_once(mod, func)
                ):
                    continue  # the runner's own body
                judge(mod, node, f"call to the generic runner {fname}")
    problems.extend(problems_of_trust)
    problems.extend(_module_name_collisions(modules))
    return sorted(set(problems))


def _untrusted_definitions(modules: list[_Module]) -> list[str]:
    """Every function whose identity the guard trusts — the helper, a generic
    runner (each of its bodies) and every allowed launch site — must be
    defined exactly once in its scope; one that is not is reported."""
    trusted_dotted = set(HELPERS) | {
        d for spec in GENERIC_RUNNERS.values() for d in spec["dotted"]
    }
    trusted_places = set(ALLOWED) | {
        (rel, body)
        for (rel, fname), spec in GENERIC_RUNNERS.items()
        for body in spec.get("bodies", {fname})
    }
    problems: list[str] = []
    for mod in modules:
        for node in ast.walk(mod.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            dotted = f"{mod.dotted}.{_qualname(mod, node)}"
            if (mod.rel, node.name) not in trusted_places and dotted not in trusted_dotted:
                continue
            if not _defined_once(mod, node):
                problems.append(
                    f"{mod.rel}:{node.lineno} in {node.name}: {node.name} is trusted "
                    "by the launch guard but has more than one binding in its "
                    "scope (a second or conditional definition, or a rebinding)"
                )
    return problems


#: The first review's probes (13 October 2026), and the build system's guard's
#: own: every function here starting with a letter and an underscore must be
#: named by the guard, and none starting ``ok_``. Fed to the guard as
#: synthetic source; never added to the package.
GUARD_PROBES = textwrap.dedent(
    '''\
    import asyncio
    import functools
    import os
    import subprocess
    import subprocess as sp
    import os as o
    from forge.launch_environment import build_project_command_env


    def a_reassigned(cmd, folder):
        env = build_project_command_env(folder)
        env = dict(os.environ)
        subprocess.run(cmd, env=env)


    def b_mutated_after(cmd, folder):
        env = build_project_command_env(folder)
        env.update(os.environ)
        subprocess.run(cmd, env=env)


    def c_empty_then_mutated(cmd, folder):
        env = {}
        env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        subprocess.run(cmd, env=env)


    def d_item_set_after(cmd, folder):
        env = build_project_command_env(folder)
        env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")
        subprocess.Popen(cmd, env=env)


    def e_module_alias(cmd):
        sp.run(cmd)


    def f_os_alias(cmd):
        o.system(cmd)


    async def g_loop_subprocess(cmd):
        loop = asyncio.get_running_loop()
        await loop.subprocess_shell(asyncio.SubprocessProtocol, cmd)
        await loop.subprocess_exec(asyncio.SubprocessProtocol, cmd)


    async def h_asyncio_attr_chain(cmd):
        await asyncio.subprocess.create_subprocess_shell(cmd)


    def i_runner_in_variable(cmd):
        runner = subprocess.run
        runner(cmd)


    def j_partial(cmd):
        functools.partial(subprocess.run, cmd)()


    def k_getattr(cmd):
        getattr(subprocess, "run")(cmd)


    def l_spelled_runner(cmd, folder):
        def build_project_command_env(_folder):
            return dict(os.environ)
        subprocess.run(cmd, env=build_project_command_env(folder))


    def m_helper_then_ignored(cmd, folder):
        env = build_project_command_env(folder)
        if cmd:
            env = os.environ.copy()
        subprocess.run(cmd, env=env)


    def n_asyncio_shell_no_env(cmd):
        return asyncio.create_subprocess_shell(cmd)


    def o_dunder_import(cmd):
        __import__("subprocess").run(cmd)


    def p_os_environ_mutated_before_helper(cmd, folder):
        env = build_project_command_env(folder, parent={**os.environ})
        subprocess.run(cmd, env=env)


    def q_helper_with_parent_overlay(cmd, folder):
        env = build_project_command_env(
            folder, additions={"DATABASE_URL": os.environ.get("FLEET_MEMORY_PG_DSN", "")}
        )
        subprocess.run(cmd, env=env)


    def _widen(env):
        env.update(os.environ)


    def r_widened_through_an_alias(cmd, folder):
        env = build_project_command_env(folder)
        other = env
        other.update(os.environ)
        subprocess.run(cmd, env=env)


    def s_handed_to_a_function_first(cmd, folder):
        env = build_project_command_env(folder)
        _widen(env)
        subprocess.run(cmd, env=env)


    def t_or_assigned(cmd, folder):
        env = build_project_command_env(folder)
        env |= dict(os.environ)
        subprocess.run(cmd, env=env)


    class u_PopenSubclass(subprocess.Popen):
        pass


    def _copy_everything():
        out = {}
        for k, v in os.environ.items():
            out[k] = v
        return out


    def v_wrapper_built_from_empty(cmd):
        subprocess.run(cmd, env=_copy_everything())


    def ok_helper(cmd, folder):
        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)


    def ok_git(cmd):
        subprocess.run(["git", "status"])


    def ok_wrapper(cmd, folder):
        subprocess.run(cmd, env=_real_wrapper(folder))


    def _real_wrapper(folder):
        return build_project_command_env(folder, additions={"X": "1"})
    '''
)

#: The first review's seventeen probes, by name: each must be caught.
COACH_PROBES = {
    "a_reassigned", "b_mutated_after", "c_empty_then_mutated",
    "d_item_set_after", "e_module_alias", "f_os_alias", "g_loop_subprocess",
    "h_asyncio_attr_chain", "i_runner_in_variable", "j_partial", "k_getattr",
    "l_spelled_runner", "m_helper_then_ignored", "n_asyncio_shell_no_env",
    "o_dunder_import", "p_os_environ_mutated_before_helper",
    "q_helper_with_parent_overlay",
}


def _probe_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe.py").write_text(GUARD_PROBES, encoding="utf-8")
    return pkg


def test_the_guard_catches_every_probe(tmp_path: Path) -> None:
    """Synthetic source fed to the guard (never added to the package)."""
    problems = unaccounted_launches(_probe_package(tmp_path))
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.walk(ast.parse(GUARD_PROBES))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.name[:2] in {f"{c}_" for c in "abcdefghijklmnopqrstuv"}
    }
    assert COACH_PROBES <= expected
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {"ok_helper", "ok_git", "ok_wrapper", "_real_wrapper"} & named, problems


#: The second review's probes (14 October 2026): a generic runner reached
#: under another name, as a module attribute, in a variable, through
#: ``functools.partial`` or as ``spawn = self._spawn``; a wrapper whose
#: parameter flows into the helper's additions; a value read from this
#: process's environment into a local first. Every ``p2_`` function must be
#: caught and no ``ok_`` one.
GUARD_PROBES_2 = textwrap.dedent(
    '''\
    import functools
    import os
    import subprocess

    from forge.deploy_sidecar.service import _start_and_collect as launch
    from forge.deploy_sidecar import service as svc
    from forge.launch_environment import build_project_command_env
    from forge.launch_environment import build_project_command_env as bpce


    def p2_runner_alias(cmd):
        launch(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p2_runner_module_attr(cmd):
        svc._start_and_collect(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p2_runner_in_variable(cmd):
        runner = svc._start_and_collect
        runner(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p2_runner_partial(cmd):
        functools.partial(launch, env=dict(os.environ))(argv=cmd, cwd=".", timeout=1, what="x")


    def _sanitise(env):
        return dict(env)


    def p2_sanitiser_copies_input(cmd):
        subprocess.run(cmd, env=_sanitise(os.environ))


    def _clean(extra):
        return build_project_command_env(".", additions=extra)


    def p2_wrapper_with_parameter_additions(cmd):
        subprocess.run(cmd, env=_clean({"FLEET_MEMORY_PG_DSN": os.environ["FLEET_MEMORY_PG_DSN"]}))


    def p2_value_read_first_then_additions(cmd):
        dsn = os.environ.get("FLEET_MEMORY_PG_DSN", "")
        env = build_project_command_env(".", additions={"DATABASE_URL": dsn})
        subprocess.run(cmd, env=env)


    def p2_helper_alias_direct_environ(cmd):
        subprocess.run(cmd, env=bpce(".", overlay=dict(os.environ)))


    def p2_popen_subclass(cmd):
        class P(subprocess.Popen):
            pass
        P(cmd)


    def p2_self_spawn_variable(self, cmd):
        spawn = self._spawn
        spawn(args=cmd, env=dict(os.environ))


    def p2_parent_from_environ_copy(cmd):
        parent = os.environ.copy()
        env = build_project_command_env(".", additions={"X": parent["OPENAI_BASE_URL"]})
        subprocess.run(cmd, env=env)


    def ok_plain(cmd):
        env = build_project_command_env(".")
        subprocess.run(cmd, env=env)


    def ok_runner_with_helper(cmd):
        env = build_project_command_env(".")
        launch(argv=cmd, cwd=".", env=env, timeout=1, what="x")


    def _fixed(folder):
        return build_project_command_env(folder, additions={"ENV_FILE": "/the/file"})


    def ok_wrapper_with_fixed_additions(cmd):
        subprocess.run(cmd, env=_fixed("."))
    '''
)


def test_the_guard_catches_the_second_reviews_probes(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe2.py").write_text(GUARD_PROBES_2, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_2).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("p2_")
    }
    assert len(expected) == 11
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_") or n == "_fixed"}, problems


#: The factory-program exemption, probed (14 October 2026): a named command
#: that an alias changes, ``executable=``, ``shell=True``, ``**`` and ``*``
#: unpacking, a named list and a module-level tuple all lose the exemption;
#: an inline literal and a helper-built environment keep passing.
GUARD_PROBES_3 = textwrap.dedent(
    '''\
    import subprocess

    GIT_STATUS = ("git", "status")


    def q3_chained_alias(cmd):
        argv = other = ["git", "-c", "x"]
        other[0] = "sh"
        subprocess.run(argv)


    def q3_executable_override(cmd):
        subprocess.run(["git", "status"], executable="/bin/sh")


    def q3_shell_true(cmd):
        subprocess.run(["git", "status"], shell=True)


    def q3_double_star(cmd, kw):
        subprocess.run(["git", "status"], **kw)


    def q3_star_args(cmd):
        parts = ["git", "status"]
        subprocess.run(*[parts])


    def q3_named_list(cmd):
        command = ["git", "status"]
        subprocess.run(command)


    def q3_module_tuple(cmd):
        subprocess.run(GIT_STATUS)


    def ok_inline_literal(cmd):
        subprocess.run(["git", "status"], capture_output=True, shell=False)


    def ok_helper_env(cmd, folder):
        from forge.launch_environment import build_project_command_env

        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)
    '''
)


def test_only_an_inline_literal_factory_program_skips_the_check(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe3.py").write_text(GUARD_PROBES_3, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_3).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("q3_")
    }
    assert len(expected) == 7
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: The strict rules, probed (14 October 2026): a second positional argument
#: (the executable or the shell switch) loses the factory-program exemption,
#: and an environment counts as the helper's only when it is the helper call
#: written inline or a name assigned once and used only as ``env=`` — so a
#: chained alias, a tuple unpacking, a name handed to another call and a
#: returned name are all flagged.
GUARD_PROBES_4 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env


    def q4_positional_executable(cmd):
        subprocess.Popen(["git", "-c", "env"], -1, "/bin/sh")


    def q4_positional_shell(cmd):
        subprocess.Popen(["git", "status"], -1, None, None, None, None, None, True, True)


    def q4_chained_env_alias(cmd, folder):
        env = other = build_project_command_env(folder)
        other.update(os.environ)
        subprocess.run(cmd, env=env)


    def q4_tuple_unpack_env(cmd, folder):
        env, extra = build_project_command_env(folder), dict(os.environ)
        subprocess.run(cmd, env=env)


    def _log(value):
        return value


    def q4_env_passed_to_another_call(cmd, folder):
        env = build_project_command_env(folder)
        _log(env)
        subprocess.run(cmd, env=env)


    def q4_env_returned(cmd, folder):
        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)
        return env


    def ok_inline_helper_env(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_single_use_named_env(cmd, folder):
        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)


    def ok_inline_git_literal(cmd):
        subprocess.run(["git", "status"], capture_output=True)
    '''
)


def test_the_strict_rules_catch_every_probe(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe4.py").write_text(GUARD_PROBES_4, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_4).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("q4_")
    }
    assert len(expected) == 6
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: The forwarding seam in the build package and the one-statement wrapper
#: rule, probed (14 October 2026): the seam's local call and an imported
#: alias of it carrying this process's environment, the seam held as a
#: value, and wrappers that can fall through to an implicit None (an
#: exception swallowed, a conditional return, an extra statement first).
GUARD_PROBES_5_SEAM = textwrap.dedent(
    '''\
    import os

    from forge.build.git_operations import _execute_via_deepagents as _inner


    async def _execute_via_deepagents(*, command, cwd, timeout, env):
        return await _inner(command=command, cwd=cwd, timeout=timeout, env=env)


    async def p5_local_call(cmd):
        return await _execute_via_deepagents(
            command=cmd, cwd=".", timeout=1, env=dict(os.environ)
        )


    async def ok_local_call_with_helper(cmd, folder):
        from forge.launch_environment import build_project_command_env

        return await _execute_via_deepagents(
            command=cmd, cwd=".", timeout=1, env=build_project_command_env(folder)
        )
    '''
)
GUARD_PROBES_5 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.build.test_verification import _execute_via_deepagents as seam
    from forge.launch_environment import build_project_command_env


    async def p5_imported_alias(cmd):
        return await seam(command=cmd, cwd=".", timeout=1, env=dict(os.environ))


    def p5_seam_as_value(cmd):
        runner = seam
        return runner


    def _exception_fallthrough(folder):
        try:
            return build_project_command_env(folder)
        except OSError:
            pass


    def p5_exception_fallthrough(cmd, folder):
        subprocess.run(cmd, env=_exception_fallthrough(folder))


    def _conditional(folder, flag):
        if flag:
            return build_project_command_env(folder)


    def p5_conditional_return(cmd, folder):
        subprocess.run(cmd, env=_conditional(folder, True))


    def _extra_statement(folder):
        os.environ["X"] = "1"
        return build_project_command_env(folder)


    def p5_extra_statement(cmd, folder):
        subprocess.run(cmd, env=_extra_statement(folder))


    def _single(folder):
        \"\"\"A docstring is not a statement that can fall through.\"\"\"
        return build_project_command_env(folder)


    def ok_single_return_wrapper(cmd, folder):
        subprocess.run(cmd, env=_single(folder))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    async def ok_alias_with_helper(cmd, folder):
        return await seam(command=cmd, cwd=".", timeout=1, env=build_project_command_env(folder))
    '''
)


def test_the_build_seam_and_the_wrapper_rule_catch_every_probe(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    (pkg / "build").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "build" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "build" / "test_verification.py").write_text(GUARD_PROBES_5_SEAM, encoding="utf-8")
    (pkg / "zz_probe5.py").write_text(GUARD_PROBES_5, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for source in (GUARD_PROBES_5_SEAM, GUARD_PROBES_5)
        for n in ast.parse(source).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("p5_")
    }
    assert len(expected) == 6
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_") or n == "_single"}, problems


#: Decorators and shadowing, probed (14 October 2026): a decorated wrapper
#: (its decorator turns the helper's refusal into None), the helper's name
#: shadowed by a parameter default, by a local assignment, and rebound at
#: module level after its import.
GUARD_PROBES_6 = textwrap.dedent(
    '''\
    import functools
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env
    from forge.launch_environment import build_project_command_env as make_env


    def _none_on_refusal(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except OSError:
                return None

        return wrapped


    @_none_on_refusal
    def _decorated(folder):
        return build_project_command_env(folder)


    def p6_decorated_wrapper(cmd, folder):
        subprocess.run(cmd, env=_decorated(folder))


    def p6_parameter_default_shadowing(cmd, folder, build_project_command_env=lambda *a: dict(os.environ)):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def p6_local_reassignment(cmd, folder):
        build_project_command_env = lambda *a: dict(os.environ)  # noqa: E731
        subprocess.run(cmd, env=build_project_command_env(folder))


    def _single(folder):
        return build_project_command_env(folder)


    def ok_undecorated_single_return_wrapper(cmd, folder):
        subprocess.run(cmd, env=_single(folder))


    def ok_unshadowed_import_alias(cmd, folder):
        subprocess.run(cmd, env=make_env(folder))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))
    '''
)
GUARD_PROBES_6_REBOUND = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env

    build_project_command_env = lambda *a: dict(os.environ)  # noqa: E731


    def p6_module_level_rebinding(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))
    '''
)


def test_decorators_and_shadowed_names_are_never_the_helper(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe6.py").write_text(GUARD_PROBES_6, encoding="utf-8")
    (pkg / "zz_probe6_rebound.py").write_text(GUARD_PROBES_6_REBOUND, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for source in (GUARD_PROBES_6, GUARD_PROBES_6_REBOUND)
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p6_")
    }
    assert len(expected) == 4
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Defaults and reserved names, probed (14 October 2026): a default that
#: captures a generic runner under its own name or another, a lambda default
#: that calls one, and a local named like the helper.
GUARD_PROBES_7 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.deploy_sidecar.service import _start_and_collect
    from forge.launch_environment import build_project_command_env


    def p7_same_named_default_capture(cmd, _start_and_collect=_start_and_collect):
        return _start_and_collect(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p7_differently_named_default(cmd, run_it=_start_and_collect):
        return run_it(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p7_lambda_default_calling_runner(cmd, go=lambda **k: _start_and_collect(**k)):
        return go(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p7_local_named_like_the_helper(cmd, folder):
        build_project_command_env = dict
        subprocess.run(cmd, env=build_project_command_env(os.environ))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_single_use_named_env(cmd, folder):
        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)


    def ok_runner_with_helper(cmd, folder):
        return _start_and_collect(
            argv=cmd, cwd=".", env=build_project_command_env(folder), timeout=1, what="x"
        )
    '''
)


def test_defaults_and_reserved_names_are_caught(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe7.py").write_text(GUARD_PROBES_7, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    by_function: dict[str, list[str]] = {}
    for problem in problems:
        where, why = problem.split(" in ", 1)[1].split(": ", 1)
        by_function.setdefault(where, []).append(why)
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_7).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p7_")
    }
    assert len(expected) == 4
    assert expected - set(by_function) == set(), problems
    assert not {n for n in by_function if n.startswith("ok_")}, problems
    assert any("reserved name '_start_and_collect'" in why for why in by_function["p7_same_named_default_capture"])
    assert any(
        "referred to other than by calling it" in why
        for why in by_function["p7_differently_named_default"]
    )
    assert any("call to the generic runner" in why for why in by_function["p7_lambda_default_calling_runner"])
    assert any(
        "reserved name 'build_project_command_env'" in why
        for why in by_function["p7_local_named_like_the_helper"]
    )


#: Every binding form, probed (14 October 2026): a named helper environment
#: rebound by an import, a from-import, a definition, a class, a match-case
#: capture, or deleted and reassigned.
GUARD_PROBES_8 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env


    def p8_import_rebinding(cmd, folder):
        env = build_project_command_env(folder)
        import json as env
        subprocess.run(cmd, env=env)


    def p8_from_import_rebinding(cmd, folder):
        env = build_project_command_env(folder)
        from os import environ as env
        subprocess.run(cmd, env=env)


    def p8_def_rebinding(cmd, folder):
        env = build_project_command_env(folder)

        def env():
            return dict(os.environ)

        subprocess.run(cmd, env=env)


    def p8_class_rebinding(cmd, folder):
        env = build_project_command_env(folder)

        class env(dict):
            pass

        subprocess.run(cmd, env=env)


    def p8_match_capture(cmd, folder, value):
        env = build_project_command_env(folder)
        match value:
            case dict() as env:
                pass
        subprocess.run(cmd, env=env)


    def p8_del_then_reassign(cmd, folder):
        env = build_project_command_env(folder)
        del env
        env = dict(os.environ)
        subprocess.run(cmd, env=env)


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_single_use_named_env(cmd, folder):
        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)
    '''
)

#: The import table, probed (14 October 2026): one local name imported as a
#: launch module or function in one scope and as something harmless in
#: another — across functions, as a from-import, and module level against
#: function level.
GUARD_PROBES_9 = textwrap.dedent(
    '''\
    import json as worker


    def p9_alias_reused_across_functions(cmd):
        import subprocess as tool
        tool.run(cmd, shell=True)


    def _elsewhere():
        import json as tool
        return tool.dumps({})


    def p9_from_import_variant(cmd):
        from subprocess import run as go
        go(cmd, shell=True)


    def _elsewhere_too():
        from json import dumps as go
        return go({})


    def p9_module_level_against_function_level(cmd):
        import subprocess as worker
        worker.run(cmd, shell=True)


    def ok_helper_environment(cmd, folder):
        import subprocess

        from forge.launch_environment import build_project_command_env

        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_single_use_helper_environment(cmd, folder):
        import subprocess

        from forge.launch_environment import build_project_command_env

        env = build_project_command_env(folder)
        subprocess.run(cmd, env=env)
    '''
)


@pytest.mark.parametrize(
    ("source", "prefix", "count"),
    [("GUARD_PROBES_8", "p8_", 6), ("GUARD_PROBES_9", "p9_", 3)],
)
def test_every_binding_and_every_import_is_seen(
    tmp_path: Path, source: str, prefix: str, count: int
) -> None:
    text = globals()[source]
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe.py").write_text(text, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith(prefix)
    }
    assert len(expected) == count
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems
    if prefix == "p9_":
        assert any("is imported as 'tool'" in p for p in problems), problems
        assert any("is imported as 'go'" in p for p in problems), problems


#: The direct-use-only rule, probed (14 October 2026): a runner imported
#: ``as`` another name that is reused elsewhere, a launch module assigned to a
#: variable, passed as an argument or taken as a default, a launch function
#: kept in a dict, and a runner returned.
GUARD_PROBES_10 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.deploy_sidecar.service import _start_and_collect
    from forge.launch_environment import build_project_command_env


    def p10_runner_alias_reused(cmd, folder):
        from forge.deploy_sidecar.service import _start_and_collect as invoke

        return invoke(argv=cmd, cwd=".", env=build_project_command_env(folder), timeout=1, what="x")


    def _elsewhere():
        from json import dumps as invoke

        return invoke({})


    def p10_module_assigned_to_a_variable(cmd, folder):
        tool = subprocess
        tool.run(cmd, env=build_project_command_env(folder))


    def _hand_on(module, cmd):
        return module, cmd


    def p10_module_passed_as_an_argument(cmd):
        _hand_on(subprocess, cmd)


    def p10_module_as_a_default(cmd, folder, tool=subprocess):
        tool.run(cmd, env=build_project_command_env(folder))


    def p10_launch_function_in_a_dict(cmd):
        table = {"go": subprocess.run}
        table["go"](cmd)


    def p10_runner_returned(cmd):
        return _start_and_collect


    def ok_direct_subprocess_run_with_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_direct_runner_with_helper(cmd, folder):
        return _start_and_collect(
            argv=cmd, cwd=".", env=build_project_command_env(folder), timeout=1, what="x"
        )


    def ok_os_path_and_environ(path):
        return os.path.join(path, "x"), os.environ.get("HOME")
    '''
)


def test_guarded_names_are_only_used_directly(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe10.py").write_text(GUARD_PROBES_10, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_10).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p10_")
    }
    assert len(expected) == 6
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Relative imports, probed (14 October 2026): a runner imported by a
#: relative ``from .service import ... as go`` and assigned to a variable, the
#: same alias reused in an unrelated scope, a parent-relative alias, and an
#: ``import ... as`` whose last component is a guarded name under other module
#: text. Every import is resolved by the one resolver first.
GUARD_PROBES_11_SIDECAR = textwrap.dedent(
    '''\
    import os

    from .service import _start_and_collect as go
    from ..launch_environment import build_project_command_env


    def p11_relative_runner_alias_assigned(cmd):
        runner = go
        return runner(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p11_relative_runner_alias_reused_here(cmd, folder):
        return go(argv=cmd, cwd=".", env=build_project_command_env(folder), timeout=1, what="x")
    '''
)
GUARD_PROBES_11_BUILD = textwrap.dedent(
    '''\
    import os


    def p11_parent_relative_alias_in_one_scope(cmd):
        from ..deploy_sidecar.service import _start_and_collect as go

        return go(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


    def p11_same_alias_unrelated_scope(cmd):
        from json import dumps as go

        return go({"cmd": cmd})


    def p11_last_component_alias(cmd):
        import tools.vendored.subprocess as sp

        return sp.run(cmd)


    def ok_unaliased_relative_import_with_helper(cmd, folder):
        from ..deploy_sidecar.service import _start_and_collect
        from ..launch_environment import build_project_command_env

        return _start_and_collect(
            argv=cmd, cwd=".", env=build_project_command_env(folder), timeout=1, what="x"
        )
    '''
)


def test_relative_imports_are_resolved_before_every_check(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    for sub in ("deploy_sidecar", "build"):
        (pkg / sub).mkdir(parents=True)
        (pkg / sub / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "deploy_sidecar" / "zz_probe11.py").write_text(GUARD_PROBES_11_SIDECAR, encoding="utf-8")
    (pkg / "build" / "zz_probe11.py").write_text(GUARD_PROBES_11_BUILD, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        "p11_relative_runner_alias_assigned",
        "p11_parent_relative_alias_in_one_scope",
        "p11_same_alias_unrelated_scope",
        "p11_last_component_alias",
    }
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert any(
        "deploy_sidecar/zz_probe11.py:3 in <module>: "
        "forge.deploy_sidecar.service._start_and_collect is imported as 'go'" in p
        for p in problems
    ), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: One definition, probed (14 October 2026): a wrapper defined safely under
#: one condition and unsafely under another, defined twice, or rebound by an
#: assignment after its definition, and a generic runner defined twice (safe,
#: then unsafe). Only a function with exactly one binding in its scope is
#: trusted.
GUARD_PROBES_12 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env

    if os.environ.get("SOME_SWITCH"):

        def _conditional(folder):
            return build_project_command_env(folder)

    else:

        def _conditional(folder):
            return dict(os.environ)


    def p12_conditional_safe_and_unsafe_definitions(cmd, folder):
        subprocess.run(cmd, env=_conditional(folder))


    def _duplicate(folder):
        return build_project_command_env(folder)


    def _duplicate(folder):  # noqa: F811
        return dict(os.environ)


    def p12_duplicate_definitions(cmd, folder):
        subprocess.run(cmd, env=_duplicate(folder))


    def _rebound(folder):
        return build_project_command_env(folder)


    _rebound = lambda folder: dict(os.environ)  # noqa: E731


    def p12_definition_rebound_by_assignment(cmd, folder):
        subprocess.run(cmd, env=_rebound(folder))


    def _single(folder):
        return build_project_command_env(folder)


    def ok_single_definition_wrapper(cmd, folder):
        subprocess.run(cmd, env=_single(folder))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))
    '''
)
GUARD_PROBES_12_RUNNER = textwrap.dedent(
    '''\
    import subprocess


    def _start_and_collect(*, argv, cwd, env, timeout, what):
        return subprocess.Popen(argv, cwd=cwd, env=env)


    def _start_and_collect(*, argv, cwd, env, timeout, what):  # noqa: F811
        return subprocess.Popen(argv, cwd=cwd)
    '''
)


def test_only_a_function_defined_once_is_trusted(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    (pkg / "deploy_sidecar").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "deploy_sidecar" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe12.py").write_text(GUARD_PROBES_12, encoding="utf-8")
    (pkg / "deploy_sidecar" / "service.py").write_text(GUARD_PROBES_12_RUNNER, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_12).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p12_")
    }
    assert len(expected) == 3
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert any(
        "deploy_sidecar/service.py" in p and "has more than one binding" in p
        for p in problems
    ), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Qualified identity, probed (14 October 2026): two conditional ``class``
#: definitions each with a method of the same name (one safe, one not), two
#: plain duplicate classes, a class rebound by an assignment, and nested
#: functions of the same name under a duplicated outer function. A wrapper
#: is trusted only as the one definition of its full qualified identity,
#: with every enclosing class and function bound once.
GUARD_PROBES_13 = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env

    if os.environ.get("SOME_SWITCH"):

        class Conditional:
            def env(self, folder):
                return build_project_command_env(folder)

    else:

        class Conditional:
            def env(self, folder):
                return dict(os.environ)


    def p13_conditional_duplicate_classes(cmd, folder):
        subprocess.run(cmd, env=Conditional.env(None, folder))


    class Plain:
        def env(self, folder):
            return build_project_command_env(folder)


    class Plain:  # noqa: F811
        def env(self, folder):
            return dict(os.environ)


    def p13_duplicate_plain_classes(cmd, folder):
        subprocess.run(cmd, env=Plain.env(None, folder))


    class Rebound:
        def env(self, folder):
            return build_project_command_env(folder)


    Rebound = dict  # noqa: F811


    def p13_class_rebound_by_assignment(cmd, folder):
        subprocess.run(cmd, env=Rebound.env(None, folder))


    def p13_duplicated_outer(cmd, folder):
        def env(f):
            return build_project_command_env(f)

        subprocess.run(cmd, env=env(folder))


    def p13_duplicated_outer(cmd, folder):  # noqa: F811
        def env(f):
            return dict(os.environ)

        subprocess.run(cmd, env=env(folder))


    def _single(folder):
        return build_project_command_env(folder)


    def ok_single_module_level_wrapper(cmd, folder):
        # (A method wrapper is no longer trusted at all, 14 October 2026:
        # method dispatch is decided at run time.)
        subprocess.run(cmd, env=_single(folder))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))
    '''
)


def test_only_the_one_definition_of_a_qualified_identity_is_trusted(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe13.py").write_text(GUARD_PROBES_13, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_13).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p13_")
    }
    assert len(expected) == 4
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Wildcard imports and method dispatch, probed (14 October 2026): a
#: wildcard import from subprocess, from os and from an unrelated module are
#: each a violation; a method wrapper reached through ``self`` and a
#: subclass override defined under a class-level ``if`` are never trusted.
GUARD_PROBES_14_WILD_SUBPROCESS = textwrap.dedent(
    '''\
    from subprocess import *  # noqa: F403


    def p14_launch_after_wildcard_subprocess(cmd):
        run(cmd)  # noqa: F405
    '''
)
GUARD_PROBES_14_WILD_OS = textwrap.dedent(
    '''\
    from os import *  # noqa: F403


    def p14_launch_after_wildcard_os(cmd):
        system(cmd)  # noqa: F405
    '''
)
GUARD_PROBES_14_WILD_OTHER = textwrap.dedent(
    '''\
    from json import *  # noqa: F403


    def p14_name_after_unrelated_wildcard(value):
        return dumps(value)  # noqa: F405
    '''
)
GUARD_PROBES_14_DISPATCH = textwrap.dedent(
    '''\
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env


    class Base:
        def env(self, folder):
            return build_project_command_env(folder)

        def p14_plain_method_wrapper_via_self(self, cmd, folder):
            subprocess.run(cmd, env=self.env(folder))


    class Child(Base):
        if os.environ.get("SOME_SWITCH"):

            def env(self, folder):
                return dict(os.environ)

        def p14_conditional_subclass_override(self, cmd, folder):
            subprocess.run(cmd, env=self.env(folder))


    def _module_wrapper(folder):
        return build_project_command_env(folder)


    def ok_module_level_wrapper(cmd, folder):
        subprocess.run(cmd, env=_module_wrapper(folder))


    def ok_inline_helper(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_explicit_import_launch_with_helper_env(cmd, folder):
        from subprocess import run

        run(cmd, env=build_project_command_env(folder))
    '''
)


def test_wildcard_imports_and_method_dispatch_are_never_trusted(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    sources = {
        "zz_wild_subprocess.py": GUARD_PROBES_14_WILD_SUBPROCESS,
        "zz_wild_os.py": GUARD_PROBES_14_WILD_OS,
        "zz_wild_other.py": GUARD_PROBES_14_WILD_OTHER,
        "zz_dispatch.py": GUARD_PROBES_14_DISPATCH,
    }
    for name, text in sources.items():
        (pkg / name).write_text(text, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    for name, module in (
        ("zz_wild_subprocess.py", "subprocess"),
        ("zz_wild_os.py", "os"),
        ("zz_wild_other.py", "json"),
    ):
        assert any(
            p.startswith(name) and f"wildcard import from {module}" in p for p in problems
        ), problems
    named = {
        p.split(" in ", 1)[1].split(":", 1)[0]
        for p in problems
        if p.startswith("zz_dispatch.py")
    }
    assert {
        "p14_plain_method_wrapper_via_self",
        "p14_conditional_subclass_override",
    } <= named, problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: The one launch predicate, probed (14 October 2026): create_subprocess_shell
#: and create_subprocess_exec imported directly from asyncio.subprocess,
#: system from os, spawn from pty and create_subprocess_exec from asyncio, each
#: called bare. The controls pass the helper's environment, and asyncio.run is
#: not a launch.
GUARD_PROBES_15 = textwrap.dedent(
    '''\
    import asyncio
    import subprocess

    from forge.launch_environment import build_project_command_env


    async def p15_create_subprocess_shell_from_asyncio_subprocess(cmd):
        from asyncio.subprocess import create_subprocess_shell

        await create_subprocess_shell(cmd)


    async def p15_create_subprocess_exec_from_asyncio_subprocess(cmd):
        from asyncio.subprocess import create_subprocess_exec

        await create_subprocess_exec(cmd)


    def p15_system_from_os(cmd):
        from os import system

        system(cmd)


    def p15_spawn_from_pty(cmd):
        from pty import spawn

        spawn(cmd)


    async def p15_create_subprocess_exec_from_asyncio(cmd):
        from asyncio import create_subprocess_exec

        await create_subprocess_exec(cmd)


    async def ok_create_subprocess_shell_with_helper_env(cmd, folder):
        from asyncio.subprocess import create_subprocess_shell

        await create_subprocess_shell(cmd, env=build_project_command_env(folder))


    def ok_subprocess_run_with_helper_env(cmd, folder):
        subprocess.run(cmd, env=build_project_command_env(folder))


    def ok_asyncio_run_is_not_a_launch(main):
        return asyncio.run(main())
    '''
)


def test_one_launch_predicate_sees_every_import_form(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe15.py").write_text(GUARD_PROBES_15, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_15).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("p15_")
    }
    assert len(expected) == 5
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Deep attribute chains, probed (14 October 2026, implementation review 1,
#: R1): ``multiprocessing.popen_fork.Popen`` and
#: ``asyncio.subprocess.create_subprocess_exec`` held in a variable. ``os.path``
#: held is not a launch, and the deep direct call with the helper's
#: environment stays clean.
GUARD_PROBES_16 = textwrap.dedent(
    '''\
    import asyncio
    import multiprocessing.popen_fork
    import os

    from forge.launch_environment import build_project_command_env


    def p16_deep_launch_held_in_a_variable(obj):
        launch = multiprocessing.popen_fork.Popen
        return launch(obj)


    def p16_deep_create_subprocess_exec_held(cmd):
        f = asyncio.subprocess.create_subprocess_exec
        return f(cmd)


    def ok_os_path_held_is_not_a_launch(path):
        x = os.path
        return x.join(path, "x")


    async def ok_direct_deep_call_with_helper_env(cmd, folder):
        await asyncio.subprocess.create_subprocess_exec(cmd, env=build_project_command_env(folder))
    '''
)


def test_a_launch_is_seen_at_any_depth_of_an_attribute_chain(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe16.py").write_text(GUARD_PROBES_16, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    assert {
        "p16_deep_launch_held_in_a_variable",
        "p16_deep_create_subprocess_exec_held",
    } <= named, problems
    assert any("multiprocessing.popen_fork.Popen" in p for p in problems), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Launch-bearing modules, probed (14 October 2026, implementation review 2,
#: R2): a submodule that defines launches stored in a variable and called
#: through (``multiprocessing.popen_fork``, ``asyncio.subprocess``), a launch
#: module stored, and a launch module stored then reached deeply. The direct
#: dotted call and ``os.path`` held (it defines no launch) stay clean.
GUARD_PROBES_17 = textwrap.dedent(
    '''\
    import asyncio
    import multiprocessing.popen_fork
    import os
    import subprocess

    from forge.launch_environment import build_project_command_env


    def p17_submodule_stored_then_called_through(obj):
        backend = multiprocessing.popen_fork
        return backend.Popen(obj)


    async def p17_asyncio_subprocess_stored(cmd):
        m = asyncio.subprocess
        await m.create_subprocess_exec(cmd)


    def p17_subprocess_stored(cmd):
        s = subprocess
        return s


    def p17_root_stored_then_deep_call(obj):
        m = multiprocessing
        return m.popen_fork.Popen(obj)


    async def ok_direct_dotted_call(cmd, folder):
        await asyncio.subprocess.create_subprocess_exec(cmd, env=build_project_command_env(folder))


    def ok_os_path_held(path):
        x = os.path
        return x.join(path, "x")
    '''
)


def test_a_launch_bearing_module_is_only_ever_an_attribute_base(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "zz_probe17.py").write_text(GUARD_PROBES_17, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_17).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("p17_")
    }
    assert len(expected) == 4
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert any("multiprocessing.popen_fork is referred to" in p for p in problems), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Re-exports through the package's own modules, probed (14 October 2026):
#: subprocess.run and a generic runner re-exported through another module,
#: a two-hop re-export and a cyclic one. The control is a re-exported launch
#: given the helper's environment. Each name is followed to its final
#: identity; a cycle is treated as a possible launch.
GUARD_PROBES_18: dict[str, str] = {
    "zz_a.py": textwrap.dedent(
        '''\
        from subprocess import run

        from forge.deploy_sidecar.service import _start_and_collect
        '''
    ),
    "zz_b.py": textwrap.dedent(
        '''\
        import os

        from forge.launch_environment import build_project_command_env

        from .zz_a import _start_and_collect, run


        def p18_reexported_subprocess_run(cmd):
            run(cmd, shell=True)


        def p18_reexported_generic_runner(cmd):
            return _start_and_collect(argv=cmd, cwd=".", env=dict(os.environ), timeout=1, what="x")


        def ok_reexported_launch_with_helper_env(cmd, folder):
            run(cmd, env=build_project_command_env(folder))
        '''
    ),
    "zz_hop2.py": textwrap.dedent(
        '''\
        from .zz_a import run
        '''
    ),
    "zz_c.py": textwrap.dedent(
        '''\
        from .zz_hop2 import run


        def p18_two_hop(cmd):
            run(cmd, shell=True)
        '''
    ),
    "zz_cycle_one.py": textwrap.dedent(
        '''\
        from .zz_cycle_two import loop_name
        '''
    ),
    "zz_cycle_two.py": textwrap.dedent(
        '''\
        from .zz_cycle_one import loop_name
        '''
    ),
    "zz_d.py": textwrap.dedent(
        '''\
        from .zz_cycle_one import loop_name


        def p18_cyclic(cmd):
            loop_name(cmd)
        '''
    ),
}


def test_a_reexported_launch_is_followed_to_what_it_finally_is(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, text in GUARD_PROBES_18.items():
        (pkg / name).write_text(text, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    assert {
        "p18_reexported_subprocess_run",
        "p18_reexported_generic_runner",
        "p18_two_hop",
        "p18_cyclic",
    } <= named, problems
    assert any("in p18_two_hop: process launch subprocess.run" in p for p in problems), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: Re-exports, second review (14 October 2026, R1 and R2): a launch stored
#: and called through a re-exported module (``a.b.run``, where ``a`` imports
#: ``b`` and ``b`` imports ``subprocess.run``), and a module-qualified call and
#: a stored chain through a name its module binds twice (unresolved). The
#: control calls through the re-exported module with the helper's
#: environment.
GUARD_PROBES_19: dict[str, str] = {
    "zz_r1_b.py": textwrap.dedent(
        '''\
        from subprocess import run
        '''
    ),
    "zz_r1_a.py": textwrap.dedent(
        '''\
        from . import zz_r1_b
        '''
    ),
    "zz_r2_a.py": textwrap.dedent(
        '''\
        import subprocess

        subprocess: object
        '''
    ),
    "zz_main.py": textwrap.dedent(
        '''\
        from forge.launch_environment import build_project_command_env

        from . import zz_r1_a, zz_r2_a


        def p19_storage_through_a_reexported_module():
            saved = zz_r1_a.zz_r1_b.run
            return saved


        def p19_call_through_a_reexported_module(cmd):
            zz_r1_a.zz_r1_b.run(cmd, shell=True)


        def p19_module_qualified_call_through_an_unresolved_binding(cmd):
            zz_r2_a.subprocess.run(cmd)


        def p19_storing_an_unresolved_chain_ending_in_a_launch_name():
            saved = zz_r2_a.subprocess.run
            return saved


        def ok_call_through_a_reexported_module_with_helper_env(cmd, folder):
            zz_r1_a.zz_r1_b.run(cmd, env=build_project_command_env(folder))
        '''
    ),
}


def test_a_reexport_is_resolved_all_the_way_and_unresolved_spreads(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, text in GUARD_PROBES_19.items():
        (pkg / name).write_text(text, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    named = {p.split(" in ", 1)[1].split(":", 1)[0] for p in problems}
    expected = {
        n.name
        for n in ast.parse(GUARD_PROBES_19["zz_main.py"]).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("p19_")
    }
    assert len(expected) == 4
    assert expected - named == set(), f"slipped past the guard: {sorted(expected - named)}"
    assert any(
        "in p19_storage_through_a_reexported_module: the launch function subprocess.run" in p
        for p in problems
    ), problems
    assert not {n for n in named if n.startswith("ok_")}, problems


#: A name that is both a package binding and a submodule, probed (14 October
#: 2026, review 2, R3): ``forge/__init__.py`` binds ``run`` from subprocess
#: while an empty ``forge/run.py`` also exists. Since review 3 (R4) that
#: collision is itself a violation. A genuine submodule with no package
#: binding stays an ordinary module.
GUARD_PROBES_20: dict[str, str] = {
    "__init__.py": textwrap.dedent(
        '''\
        from subprocess import run
        '''
    ),
    "run.py": "",
    "tool.py": textwrap.dedent(
        '''\
        def helper_function(value):
            return value
        '''
    ),
    "zz_main.py": textwrap.dedent(
        '''\
        from forge import run, tool
        from forge.launch_environment import build_project_command_env


        def p20_collision_call(cmd):
            run(cmd)


        def p20_collision_store():
            saved = run
            return saved


        def ok_collision_call_with_helper_env(cmd, folder):
            run(cmd, env=build_project_command_env(folder))


        def ok_genuine_submodule_without_a_package_binding(value):
            return tool.helper_function(value)
        '''
    ),
}


def test_a_package_binding_and_a_same_named_submodule_are_a_violation(tmp_path: Path) -> None:
    """Review 2 (R3) kept both identities; review 3 (R4) made the collision
    itself a violation instead, as with aliases: the package's __init__ may
    not bind a name that is also one of its own submodules."""
    pkg = tmp_path / "forge"
    pkg.mkdir()
    for name, text in GUARD_PROBES_20.items():
        (pkg / name).write_text(text, encoding="utf-8")
    problems = unaccounted_launches(pkg)
    assert any(
        p.startswith("__init__.py:") and "binds 'run'" in p and "own submodules" in p
        for p in problems
    ), problems
    assert not any(" in ok_" in p for p in problems), problems


#: Name collisions made impossible, probed (14 October 2026, review 3, R4):
#: a package that imports subprocess while holding its own subprocess.py
#: (both blunt rules), and a subpackage whose __init__ binds a non-launch
#: name that is also one of its submodules. The controls (a call with the
#: helper's environment, a genuine local submodule, and the plain import of
#: the package's own submodule in its __init__) stay clean.
GUARD_PROBES_21_R4: dict[str, str] = {
    "__init__.py": textwrap.dedent(
        '''\
        import subprocess
        '''
    ),
    "subprocess.py": textwrap.dedent(
        '''\
        def run(cmd):
            return cmd
        '''
    ),
    "zz_main.py": textwrap.dedent(
        '''\
        import forge


        def p21_store_through_a_colliding_name(cmd):
            saved = forge.subprocess.run
            return saved
        '''
    ),
}
GUARD_PROBES_21_COLLISION: dict[str, str] = {
    "__init__.py": "",
    "sub/__init__.py": textwrap.dedent(
        '''\
        from . import tool
        from .tool import helper as reporting

        reporting_level = 1
        '''
    ),
    "sub/reporting.py": textwrap.dedent(
        '''\
        def summary(value):
            return value
        '''
    ),
    "sub/tool.py": textwrap.dedent(
        '''\
        def helper(value):
            return value
        '''
    ),
    "sub/zz_main.py": textwrap.dedent(
        '''\
        import subprocess

        from forge.launch_environment import build_project_command_env

        from . import tool


        def ok_helper_environment_call(cmd, folder):
            subprocess.run(cmd, env=build_project_command_env(folder))


        def ok_genuine_local_submodule(value):
            return tool.helper(value)
        '''
    ),
}


def _write_package(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "forge"
    for name, text in files.items():
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        (pkg / name).write_text(text, encoding="utf-8")
    return pkg


def test_name_collisions_are_violations(tmp_path: Path) -> None:
    problems = unaccounted_launches(_write_package(tmp_path / "r4", GUARD_PROBES_21_R4))
    assert any(
        p.startswith("subprocess.py:") and "named 'subprocess'" in p for p in problems
    ), problems
    assert any(
        p.startswith("__init__.py:") and "binds 'subprocess'" in p for p in problems
    ), problems

    problems = unaccounted_launches(
        _write_package(tmp_path / "collision", GUARD_PROBES_21_COLLISION)
    )
    assert any(
        p.startswith("sub/__init__.py:") and "binds 'reporting'" in p for p in problems
    ), problems
    # The plain import of its own submodule is not a collision, and the
    # controls are clean.
    assert not any("binds 'tool'" in p for p in problems), problems
    assert not any(" in ok_" in p for p in problems), problems


def test_every_process_launch_is_accounted_for() -> None:
    problems = unaccounted_launches(PACKAGE)
    assert not problems, (
        "These process launches neither build their environment with "
        "build_project_command_env, nor run the factory's own program by a "
        "literal first argument, nor are on the allowed list with a reason:\n  "
        + "\n  ".join(problems)
    )


def test_the_allowed_list_names_only_real_places() -> None:
    modules = {m.rel: m for m in _load_modules(PACKAGE)}
    missing = []
    for rel, fname in [*ALLOWED, *GENERIC_RUNNERS, *ISSUED_BUILDERS]:
        mod = modules.get(rel)
        names = (
            {
                n.name
                for n in ast.walk(mod.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if mod
            else set()
        )
        if fname not in names:
            missing.append(f"{rel}:{fname}")
    assert not missing, f"allowed-list entries that name nothing: {missing}"


def test_every_pending_name_decision_is_still_a_real_collision() -> None:
    """A pending entry is removed once its name is changed."""
    _SCOPE_CACHE.clear()
    modules = _load_modules(PACKAGE)
    saved = dict(PENDING_NAME_DECISIONS)
    PENDING_NAME_DECISIONS.clear()
    try:
        found = _module_name_collisions(modules)
    finally:
        PENDING_NAME_DECISIONS.update(saved)
    for rel, name in saved:
        assert any(p.startswith(f"{rel}:") and repr(name) in p for p in found), (rel, name)


def test_the_guard_catches_a_new_launch(tmp_path: Path) -> None:
    pkg = tmp_path / "forge"
    pkg.mkdir()
    (pkg / "new.py").write_text(
        textwrap.dedent(
            """\
            import os, subprocess
            from subprocess import Popen as P
            from forge.launch_environment import build_project_command_env
            def a(cmd):
                subprocess.run(cmd, shell=True)
            def b(cmd):
                P(cmd, env=os.environ.copy())
            def c(cmd):
                subprocess.run(["git", "status"])
            def d(cmd, wf):
                env = build_project_command_env(wf)
                subprocess.run(cmd, env=env)
            """
        ),
        encoding="utf-8",
    )
    problems = unaccounted_launches(pkg)
    assert any("new.py:5 in a" in p for p in problems)
    assert any("new.py:7 in b" in p for p in problems)
    assert not any(" in c:" in p or " in d:" in p for p in problems)
