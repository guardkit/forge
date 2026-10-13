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

_LAUNCHES = {
    "subprocess": {
        "run", "Popen", "call", "check_call", "check_output", "getoutput",
        "getstatusoutput",
    },
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {
        "system", "popen", "execl", "execle", "execlp", "execlpe", "execv",
        "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
        "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
        "posix_spawnp", "fork", "forkpty",
    },
    "pty": {"spawn", "fork"},
}
#: Launch methods recognised on any object: ``asyncio.subprocess.create_*``
#: and an event loop's ``subprocess_exec`` / ``subprocess_shell``.
_ANY_RECEIVER_LAUNCHES = {
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec",
    "subprocess_shell",
}
#: Methods that change a dictionary in place.
_MUTATORS = {"update", "setdefault", "__setitem__", "__ior__", "pop", "popitem", "clear"}
#: Functions an environment may be handed to before the launch: they only
#: read it.
_READS_ONLY = {"dict", "len", "sorted", "list", "minimal_environment_note", "withheld_settings"}

#: Launches allowed by name, each with its one-line reason, keyed by the file
#: (under ``src/forge``) and the function the launch is written in.
ALLOWED: dict[tuple[str, str], str] = {
    ("adapters/gh/operations.py", "_execute"): "runs gh, the factory's own GitHub client",
    ("adapters/git/operations.py", "_default_execute"): "runs git commands the git adapter composes",
    ("adapters/guardkit/run.py", "_execute_subprocess"): "starts GuardKit with the factory's launch list",
    ("build/git_operations.py", "_execute_via_deepagents"): "generic runner; every call to it is checked below",
    ("build/git_operations.py", "_run_via_execute"): "git and gh, the factory's own programs, with its own environment",
    ("build/test_verification.py", "_execute_via_deepagents"): "a pass-through seam; its caller verify_tests is checked",
    ("build_processes.py", "_engine_ids"): "the container engine, called through a variable",
    ("build_processes.py", "_remove_networks_and_volumes"): "the container engine, called through a variable",
    ("build_processes.py", "remove_fixture_containers"): "the container engine, run in a worker thread",
    ("cli/register_repo.py", "_run_command"): "the operator's register command: git, docker and sbx",
    ("deploy/review_preview.py", "_run_command"): "git and the container engine for the preview's tree",
    ("deploy_sidecar/deploy_executor.py", "_default_spawn"): "generic runner; every call to it is checked below",
    ("deploy_sidecar/service.py", "_start_and_collect"): "generic runner; every call to it is checked below",
    ("deploy_sidecar/service.py", "run_merge_command"): "starts GuardKit (merge, legs, checks) with the factory's launch list",
    ("deploy_sidecar/service.py", "blob"): "git cat-file, the argument list from _git_argv",
    ("deploy_sidecar/service.py", "opened"): "git cat-file, the argument list from _git_argv",
    ("pipeline/routing_stamps.py", "read_last_code_commit_time"): "git log, from a literal git list",
    ("planning/target_terminal_tools.py", "_default_normalizer_subprocess"): "the planner's normalizer, a factory program",
    ("review_gate/record.py", "validate_review_findings"): "guardkit qa validate, a factory program",
    ("subagents/autobuild_runner.py", "_guardkit_feature_validate"): "guardkit feature validate, a factory program",
    ("subagents/autobuild_runner.py", "_run_one_build"): "starts GuardKit's build with the factory's launch list",
}

#: Generic runners: every call to them is judged as a launch of its own.
GENERIC_RUNNERS: dict[tuple[str, str], dict[str, Any]] = {
    ("build/git_operations.py", "_execute_via_deepagents"): {
        "names": {"_execute_via_deepagents"},
    },
    ("deploy_sidecar/deploy_executor.py", "_default_spawn"): {
        "names": {"_spawn", "_default_spawn"},
    },
    ("deploy_sidecar/service.py", "_start_and_collect"): {
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
    imports: dict[str, str]


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
            )
        )
    return modules


def _imports(tree: ast.Module, dotted: str, is_package: bool) -> dict[str, str]:
    """Every name an import binds anywhere in the module -> what it names."""
    out: dict[str, str] = {}
    package = dotted if is_package else dotted.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    out[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            for alias in node.names:
                out[alias.asname or alias.name] = f"{base}.{alias.name}"
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


def _resolve(mod: _Module, func_node: ast.AST, at: ast.AST) -> str | None:
    """The dotted name a call target refers to, or ``None`` when unknown."""
    if isinstance(func_node, ast.Name):
        name = func_node.id
        scope = _enclosing_function(mod, at)
        while scope is not None:  # a function defined in an enclosing function
            for child in ast.iter_child_nodes(scope):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    return f"{mod.dotted}.{_qualname(mod, child)}"
            scope = _enclosing_function(mod, scope)
        for child in mod.tree.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == name:
                return f"{mod.dotted}.{name}"
        return mod.imports.get(name)
    if isinstance(func_node, ast.Attribute):
        value = func_node.value
        if isinstance(value, ast.Name) and value.id in ("self", "cls"):
            klass = _enclosing(mod, at, ast.ClassDef)
            if klass is not None:
                return f"{mod.dotted}.{klass.name}.{func_node.attr}"
            return None
        base = _resolve(mod, value, at)
        return f"{base}.{func_node.attr}" if base else None
    return None


def _module_constants(modules: list[_Module]) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for mod in modules:
        for node in mod.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                out[node.target.id] = node.value
    return out


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


def _mutated(func: ast.AST, name: str) -> bool:
    """True when the dictionary ``name`` is changed in place in ``func``."""
    for node in ast.walk(func):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id == name:
                    return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            return True
    return False


def _in_return(func: ast.AST, target: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            if any(sub is target for sub in ast.walk(node.value)):
                return True
    return False


#: Callees an environment may be handed to: launches and generic runners are
#: judged on their own ``env=``; filled in by :func:`unaccounted_launches`.
_TRUSTED_CALLEES: set[str] = set()


def _escapes(func: ast.AST, name: str, before_line: int | None) -> bool:
    """True when the dictionary ``name`` could be widened through another
    name: bound to a second name, or handed to a function not known to only
    read it, before the launch at ``before_line``."""
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if isinstance(value, ast.Name) and value.id == name:
                return True
        if isinstance(node, ast.Call) and (before_line is None or node.lineno < before_line):
            callee = _last_name(node.func)
            if callee in _READS_ONLY or callee in _TRUSTED_CALLEES or _in_return(func, node):
                continue
            handed = list(node.args) + [kw.value for kw in node.keywords]
            for arg in handed:
                for sub in [arg, *([arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else [])]:
                    if isinstance(sub, ast.Name) and sub.id == name:
                        return True
    return False


def _reads_this_process_environment(mod: _Module, node: ast.AST) -> bool:
    """True when ``node`` reads the launching process's own environment
    (``os.environ`` or ``os.getenv``): a helper call fed from it could pass a
    factory credential under a name the helper does not refuse."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in ("environ", "environb", "getenv"):
            if isinstance(sub.value, ast.Name) and mod.imports.get(sub.value.id) == "os":
                return True
        if isinstance(sub, ast.Name) and mod.imports.get(sub.id) in ("os.environ", "os.getenv"):
            return True
    return False


def _helper_shaped(
    node: ast.AST | None, mod: _Module, func: Any, wrappers: set[str],
    before_line: int | None = None,
) -> bool:
    """True when ``node`` (an ``env=`` value) can only be the helper's result."""
    if node is None:
        return False
    if isinstance(node, ast.Call):
        target = _resolve(mod, node.func, node)
        if target in HELPERS:
            arguments = [*node.args, *(kw.value for kw in node.keywords)]
            return not any(_reads_this_process_environment(mod, a) for a in arguments)
        return target in wrappers
    if isinstance(node, ast.IfExp):
        return _helper_shaped(node.body, mod, func, wrappers, before_line) and _helper_shaped(
            node.orelse, mod, func, wrappers, before_line
        )
    if isinstance(node, ast.Name) and func is not None:
        if _is_parameter(func, node.id) or _mutated(func, node.id):
            return False
        if _escapes(func, node.id, before_line):
            return False
        values = _assignments(func, node.id)
        return bool(values) and all(_helper_shaped(v, mod, func, wrappers) for v in values)
    return False


def _wrappers(modules: list[_Module]) -> set[str]:
    """Functions whose every return is the helper's result, by dotted name."""
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for mod in modules:
            for node in ast.walk(mod.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                full = f"{mod.dotted}.{_qualname(mod, node)}"
                if full in found:
                    continue
                returns = [
                    r for r in ast.walk(node)
                    if isinstance(r, ast.Return) and _enclosing_function(mod, r) is node
                ]
                if returns and all(_helper_shaped(r.value, mod, node, found) for r in returns):
                    found.add(full)
                    changed = True
    return found


def _launch_kind(mod: _Module, node: ast.AST) -> str | None:
    """What launch ``node`` (a call target or a value) names, if any."""
    if isinstance(node, ast.Attribute):
        if node.attr in _ANY_RECEIVER_LAUNCHES:
            return _dotted(node)
        if isinstance(node.value, ast.Name):
            module = mod.imports.get(node.value.id)
            if module in _LAUNCHES and node.attr in _LAUNCHES[module]:
                return f"{module}.{node.attr}"
    if isinstance(node, ast.Name):
        target = mod.imports.get(node.id, "")
        module, _, name = target.rpartition(".")
        if module in _LAUNCHES and name in _LAUNCHES[module]:
            return target
    return None


def _factory_literal(
    node: ast.AST | None, constants: dict[str, ast.AST], func: Any = None
) -> bool:
    """A list whose first item is the literal git/docker/pgrep/ps."""
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return node.value in FACTORY_PROGRAMS
    if isinstance(node, ast.Name) and func is not None and not _is_parameter(func, node.id):
        values = _assignments(func, node.id)
        if values and all(_factory_literal(v, constants) for v in values):
            return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return (
            bool(node.elts)
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value in FACTORY_PROGRAMS
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _factory_literal(node.left, constants)
    if isinstance(node, ast.Call) and _dotted(node.func) in ("list", "tuple") and node.args:
        return _factory_literal(node.args[0], constants)
    if isinstance(node, ast.Name) and node.id in constants:
        return _factory_literal(constants[node.id], constants)
    return False


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


def _first_argument(call: ast.Call) -> ast.AST | None:
    if call.args:
        first = call.args[0]
        return first.value if isinstance(first, ast.Starred) else first
    for kw in call.keywords:
        if kw.arg in ("args", "argv", "command", "cmd"):
            return kw.value
    return None


def unaccounted_launches(package: Path) -> list[str]:
    modules = _load_modules(package)
    constants = _module_constants(modules)
    _TRUSTED_CALLEES.clear()
    for launches in _LAUNCHES.values():
        _TRUSTED_CALLEES.update(launches)
    _TRUSTED_CALLEES.update(_ANY_RECEIVER_LAUNCHES)
    for spec in GENERIC_RUNNERS.values():
        _TRUSTED_CALLEES.update(spec.get("names", ()))
    wrappers = _wrappers(modules)
    problems: list[str] = []

    def place(mod: _Module, node: ast.AST) -> tuple[Any, str]:
        func = _enclosing_function(mod, node)
        named = _enclosing(mod, node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        return func, getattr(named, "name", "<module>")

    def judge(mod: _Module, call: ast.Call, what: str) -> None:
        func, fname = place(mod, call)
        if (mod.rel, fname) in ALLOWED:
            return
        if _factory_literal(_first_argument(call), constants, func):
            return
        env = _env_keyword(call)
        if _helper_shaped(env, mod, func, wrappers, call.lineno):
            return
        reason = "no env= (it inherits everything)" if env is None else "env= is not the helper's"
        problems.append(f"{mod.rel}:{call.lineno} in {fname}: {what}: {reason}")

    def flag(mod: _Module, node: ast.AST, sentence: str) -> None:
        _, fname = place(mod, node)
        if (mod.rel, fname) not in ALLOWED:
            problems.append(f"{mod.rel}:{node.lineno} in {fname}: {sentence}")

    for mod in modules:
        called = {id(n.func) for n in ast.walk(mod.tree) if isinstance(n, ast.Call)}
        called |= _annotation_nodes(mod.tree)
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call):
                kind = _launch_kind(mod, node.func)
                if kind:
                    judge(mod, node, f"process launch {kind}")
                elif (
                    _dotted(node.func) in ("__import__", "importlib.import_module", "import_module")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).split(".")[0] in _LAUNCHES
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
            elif isinstance(node, (ast.Attribute, ast.Name)) and id(node) not in called:
                kind = _launch_kind(mod, node)
                if kind and isinstance(getattr(node, "ctx", None), ast.Load):
                    if isinstance(mod.parents.get(id(node)), ast.Attribute):
                        continue  # part of a longer dotted name, judged there
                    flag(
                        mod, node,
                        f"the launch function {kind} is used as a value (held in "
                        "a variable, passed on or wrapped), so its env= cannot be "
                        "checked",
                    )
    for (rel, fname), spec in GENERIC_RUNNERS.items():
        for mod in modules:
            for node in ast.walk(mod.tree):
                if not isinstance(node, ast.Call) or _last_name(node.func) not in spec["names"]:
                    continue
                func = _enclosing_function(mod, node)
                if mod.rel == rel and getattr(func, "name", None) == fname:
                    continue  # the runner's own body
                judge(mod, node, f"call to the generic runner {fname}")
    return sorted(set(problems))


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
