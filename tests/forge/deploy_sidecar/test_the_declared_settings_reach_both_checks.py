"""What a project declares reaches the two checks that run its own programs.

Codex's open gap on stage 3b. Stage 3b filtered the sandbox helper's command
runner and threaded the memory name and the project's declared setting NAMES
through the merge route. It did not thread them through the two OTHER routes
that run a project's own program:

* the live check (``/run`` with a ``driver``), which runs the driver the
  repository's ``deploy/profile.yaml`` declares;
* the declared test command (``/run`` with a ``declared_test``), which runs
  the command the repository's ``.guardkit/config.yaml`` declares.

Both handlers called the filtered runner with neither field, and both upstream
callers left them off the request body, so a project's declarations were
stripped on those routes — and a check that launches the build system got
memory OFF whatever the project had declared.

These tests drive each COMPLETE path with a REAL child process that writes
down the names of the settings it was given: the upstream caller, the request
body, the real HTTP handler on an ephemeral loopback port, the filtered runner,
and the child. Nothing live is touched: the "sandbox" is this same process's
own test server on 127.0.0.1, no image is built, no service is started, and no
real credential exists anywhere here — the planted values are strings written
in this file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import stat
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.deploy.live_gate import SidecarLiveGateInvoker
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence

REPO = "org/widget-shop"
BUILD_ID = "build-FEAT-WS1-20260922"
FEATURE_ID = "FEAT-WS1"

#: The name of the loader the sidecar asks for a repository's declared test
#: command. The real one lives in the build system's own package, which is not
#: installed beside forge here; a stand-in under the same name is put in the
#: module table so the route reads the repository's own file exactly as it
#: would in the sandbox.
REAL_LOADER_NAME = "guardkit.orchestrator.toolchain_declaration"

#: What the project declares its builds need, by name, and what the ledger
#: recorded for this build.
DECLARED_SETTING = "SOME_TOOL_CACHE"
MEMORY_NAME = "widget_shop"

#: The commit this build's work starts from, as the ledger records it. The
#: helper reads the project's declaration files THERE (26 September 2026).
START_COMMIT = "1f2e3d4c5b6a79880123456789abcdef01234567"

#: Planted in THIS process, which is the process the helper runs in for these
#: tests. None of it is on the factory's list and none of it may reach a child.
PLANTED = {
    "GH_TOKEN": "a-fake-publishing-credential-written-in-this-test",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "SOMETHING_ON_NO_LIST": "and nothing should carry it",
}

#: Where the child writes down what it was given. Under the working directory
#: it was pointed at, because its environment is the thing under test and it
#: cannot be told the path through one.
ANSWER_FILE = "what-the-child-was-given.json"


def _child_script(path: Path) -> Path:
    """A child that writes down the NAMES it was given, and two values.

    It also prints a results envelope, because one of the two routes reads the
    driver's stdout as one. The file is what the tests assert on: it is
    unambiguous and identical on both routes.
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib\n"
        "answer = {\n"
        "    'names': sorted(os.environ),\n"
        f"    'memory': os.environ.get('GUARDKIT_MEMORY_PROJECT'),\n"
        f"    'declared': os.environ.get({DECLARED_SETTING!r}),\n"
        "}\n"
        f"pathlib.Path(os.getcwd(), {ANSWER_FILE!r}).write_text(\n"
        "    json.dumps(answer), encoding='utf-8')\n"
        "print(json.dumps({'verdict': 'pass', 'run_id': 'r1', 'gates': []}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(autouse=True)
def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper's own process holds things no child may be handed."""
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    # And it holds the value of the name the PROJECT declared, which the child
    # may be handed — because the project asked for it by name.
    monkeypatch.setenv(DECLARED_SETTING, "/var/cache/some-tool")
    # The two routes only answer inside a sandbox.
    monkeypatch.setenv("FORGE_SIDECAR_IN_SANDBOX", "1")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository that declares a live-gate driver and a test command.

    Both are the same child script, so one stand-in answers for both routes
    and the two paths are compared on equal terms. Nothing here is specific to
    any language: the driver is an argument list and the test command is a
    command line, which is all either declaration ever is.
    """
    root = tmp_path / "widget-shop"
    (root / "deploy").mkdir(parents=True)
    (root / ".guardkit").mkdir(parents=True)
    (root / ".forge" / "worktrees" / "journey-1").mkdir(parents=True)
    child = _child_script(root / "say-what-i-was-given")
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "env_id": "widgetshop",
                "compose": {"file": "docker-compose.yml", "script": "deploy/go.sh"},
                "cwd": str(root),
                "live_gate": {
                    "driver": [str(child)],
                    "timeout_seconds": 120,
                    "env": {},
                },
            }
        ),
        encoding="utf-8",
    )
    # AND THE PROJECT SAYS WHAT ITS OWN BUILDS NEED, in the same file, in its
    # own words. Since 23 September 2026 this is the ONLY thing that widens
    # the helper's environment door: a request presenting a name the project
    # has not declared here is refused and nothing starts.
    (root / ".guardkit" / "config.yaml").write_text(
        f"toolchain:\n  test: {child}\n  test_timeout: 120\n"
        f"launch:\n  settings: [{DECLARED_SETTING}]\n",
        encoding="utf-8",
    )
    # AND IT IS COMMITTED (26 September 2026). A declaration is a committed
    # line: the helper reads both of the project's declaration files out of its
    # history, at the recorded starting commit or at committed HEAD, and never
    # off the working copy it runs the project's own programs out of.
    _commit_the_project(root)
    return root


def _commit_the_project(root: Path) -> None:
    """Put everything in ``root`` into one commit of its own history."""
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        ("commit", "-q", "-m", "the project as it is"),
    ):
        subprocess.run(
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
        )


@pytest.fixture
def config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(repo)}},
        }
    )


@pytest.fixture
def toolchain_loader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The build system's own declaration loader, stood in under its own name."""
    module = ModuleType(REAL_LOADER_NAME)

    class _Declaration:
        def __init__(self, test: str, test_timeout: int) -> None:
            self.test = test
            self.test_timeout = test_timeout

    def load_toolchain_declaration(root: Any) -> Any:
        path = Path(root) / ".guardkit" / "config.yaml"
        if not path.is_file():
            return None
        block = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        toolchain = block.get("toolchain") or {}
        if not toolchain.get("test"):
            return None
        return _Declaration(
            str(toolchain["test"]), int(toolchain.get("test_timeout", 90))
        )

    module.load_toolchain_declaration = load_toolchain_declaration  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, REAL_LOADER_NAME, module)
    return module


@pytest.fixture
def helper(config: ForgeConfig):
    """The REAL sidecar handler on an ephemeral loopback port, in this process.

    Not a sandbox and not a service: one test server, bound to 127.0.0.1 on a
    port the kernel picks, shut down at the end of the test.
    """
    server = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _what_the_child_got(where: Path) -> dict[str, Any]:
    answer = where / ANSWER_FILE
    assert answer.is_file(), "the child never ran, so it was never given anything"
    return json.loads(answer.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Route one: the live check
# ---------------------------------------------------------------------------


class TestTheLiveCheckRoute:
    """``/run`` with a ``driver`` — the repository's own live-gate driver."""

    def test_the_recorded_name_and_a_declared_name_reach_the_driver(
        self, helper: str, repo: Path
    ) -> None:
        invoker = SidecarLiveGateInvoker(
            base_url=helper,
            repo=REPO,
            repo_path=repo,
            driver_argv=[str(repo / "say-what-i-was-given")],
            timeout_seconds=60,
            memory_project=MEMORY_NAME,
            launch_settings=[DECLARED_SETTING],
        )

        outcome = invoker.invoke(feature=FEATURE_ID, target="candidate")
        assert outcome.verdict == "pass", outcome.detail

        given = _what_the_child_got(repo)
        assert given["memory"] == MEMORY_NAME
        assert given["declared"] == "/var/cache/some-tool"
        assert DECLARED_SETTING in given["names"]

    def test_a_planted_unlisted_name_reaches_nothing(
        self, helper: str, repo: Path
    ) -> None:
        invoker = SidecarLiveGateInvoker(
            base_url=helper,
            repo=REPO,
            repo_path=repo,
            driver_argv=[str(repo / "say-what-i-was-given")],
            timeout_seconds=60,
            memory_project=MEMORY_NAME,
            launch_settings=[DECLARED_SETTING],
        )

        invoker.invoke(feature=FEATURE_ID, target="candidate")

        given = _what_the_child_got(repo)
        for name in PLANTED:
            assert name not in given["names"], f"{name} still reaches the driver"
        # And the helper itself still holds them — it kept them for itself.
        assert os.environ["GH_TOKEN"] == PLANTED["GH_TOKEN"]

    def test_a_caller_that_declares_nothing_gets_memory_off(
        self, helper: str, repo: Path
    ) -> None:
        """The honest state, not a guessed name: nothing recorded, nothing handed."""
        invoker = SidecarLiveGateInvoker(
            base_url=helper,
            repo=REPO,
            repo_path=repo,
            driver_argv=[str(repo / "say-what-i-was-given")],
            timeout_seconds=60,
        )

        invoker.invoke(feature=FEATURE_ID, target="candidate")

        given = _what_the_child_got(repo)
        assert given["memory"] is None
        assert given["declared"] is None
        assert DECLARED_SETTING not in given["names"]

    def test_a_copy_of_the_invoker_carries_them_too(self, repo: Path) -> None:
        """The candidate leg copies the invoker; a copy must not lose them."""
        invoker = SidecarLiveGateInvoker(
            base_url="http://127.0.0.1:1",
            repo=REPO,
            repo_path=repo,
            driver_argv=["x"],
            memory_project=MEMORY_NAME,
            launch_settings=[DECLARED_SETTING],
        )

        copied = invoker.with_repo_path(repo / ".forge").with_extra_env({"A": "b"})

        assert copied._memory_project == MEMORY_NAME
        assert copied._launch_settings == (DECLARED_SETTING,)


# ---------------------------------------------------------------------------
# Route two: the declared test command
# ---------------------------------------------------------------------------


class TestTheDeclaredTestRoute:
    """``/run`` with a ``declared_test`` — the repository's own test command."""

    @staticmethod
    def _run(helper: str, repo: Path, **kwargs: Any) -> tuple[int | None, str]:
        from forge.cli._serve_conductor import run_declared_command_in_sandbox

        return run_declared_command_in_sandbox(
            command=str(repo / "say-what-i-was-given"),
            cwd=repo / ".forge" / "worktrees" / "journey-1",
            timeout_seconds=60,
            sandbox=SimpleNamespace(name="widget-shop-sbx", sidecar_url=helper),
            repo=REPO,
            **kwargs,
        )

    def test_the_recorded_name_and_a_declared_name_reach_the_command(
        self, helper: str, repo: Path, toolchain_loader: ModuleType
    ) -> None:
        exit_code, detail = self._run(
            helper,
            repo,
            memory_project=MEMORY_NAME,
            launch_settings=[DECLARED_SETTING],
        )
        assert exit_code == 0, detail

        given = _what_the_child_got(repo / ".forge" / "worktrees" / "journey-1")
        assert given["memory"] == MEMORY_NAME
        assert given["declared"] == "/var/cache/some-tool"
        assert DECLARED_SETTING in given["names"]

    def test_a_planted_unlisted_name_reaches_nothing(
        self, helper: str, repo: Path, toolchain_loader: ModuleType
    ) -> None:
        exit_code, detail = self._run(
            helper,
            repo,
            memory_project=MEMORY_NAME,
            launch_settings=[DECLARED_SETTING],
        )
        assert exit_code == 0, detail

        given = _what_the_child_got(repo / ".forge" / "worktrees" / "journey-1")
        for name in PLANTED:
            assert name not in given["names"], f"{name} still reaches the command"

    def test_a_caller_that_declares_nothing_gets_memory_off(
        self, helper: str, repo: Path, toolchain_loader: ModuleType
    ) -> None:
        exit_code, detail = self._run(helper, repo)
        assert exit_code == 0, detail

        given = _what_the_child_got(repo / ".forge" / "worktrees" / "journey-1")
        assert given["memory"] is None
        assert given["declared"] is None
        assert DECLARED_SETTING not in given["names"]


# ---------------------------------------------------------------------------
# The two upstream callers read the ledger, and do not invent anything
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A ledger with one build row carrying a memory name and a declaration."""
    path = tmp_path / "forge.db"
    cx: sqlite3.Connection = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id, memory_project, "
        "launch_settings, start_commit, target_branch) VALUES (?, ?, ?, "
        "'autobuild/FEAT-WS1', 'f.yaml', "
        "'COMPLETE', 'cli', 'corr-ws1', '2026-09-22T00:00:00Z', "
        "'2026-09-22T00:00:00Z', '/wt', 'mode-a', 'TASK-WS1', ?, ?, ?, 'main')",
        (
            BUILD_ID,
            FEATURE_ID,
            REPO,
            MEMORY_NAME,
            json.dumps([DECLARED_SETTING]),
            START_COMMIT,
        ),
    )
    cx.commit()
    cx.close()
    return path


class TestTheUpstreamCallersReadTheLedger:
    def test_the_deploy_dispatchers_reader_takes_both_off_the_row(
        self, ledger: Path
    ) -> None:
        from forge.pipeline.merge_executor import _the_builds_declarations

        name, names, started_at = _the_builds_declarations(ledger, BUILD_ID)

        assert name == MEMORY_NAME
        assert names == (DECLARED_SETTING,)
        # AND WHERE THE PROJECT SAID THEM: the build's recorded starting
        # commit, which the helper reads the declaration at.
        assert started_at == START_COMMIT

    def test_a_build_with_nothing_recorded_reads_as_nothing(
        self, ledger: Path
    ) -> None:
        from forge.pipeline.merge_executor import _the_builds_declarations

        assert _the_builds_declarations(ledger, "no-such-build") == (None, (), None)
        assert _the_builds_declarations(None, BUILD_ID) == (None, (), None)

    def test_the_gates_readers_own_readers_take_both_off_the_row(
        self, ledger: Path
    ) -> None:
        from forge.cli._serve_conductor import (
            _the_builds_declared_settings,
            _the_builds_memory_name,
        )

        cx = sqlite_connect.connect_writer(ledger)
        pool = SqliteLifecyclePersistence(connection=cx, db_path=ledger)
        try:
            assert _the_builds_memory_name(pool, BUILD_ID) == MEMORY_NAME
            assert _the_builds_declared_settings(pool, BUILD_ID) == (DECLARED_SETTING,)
            assert _the_builds_memory_name(pool, "no-such-build") is None
            assert _the_builds_declared_settings(pool, "no-such-build") == ()
        finally:
            cx.close()

    def test_a_facade_that_cannot_answer_is_nothing_never_a_default(self) -> None:
        """"Not recorded" is never turned into some other project's name."""
        from forge.cli._serve_conductor import (
            _the_builds_declared_settings,
            _the_builds_memory_name,
        )

        class _Raises:
            def read_memory_project(self, build_id: str) -> str:
                raise RuntimeError("the ledger is not readable")

            def read_launch_settings(self, build_id: str) -> tuple[str, ...]:
                raise RuntimeError("the ledger is not readable")

        assert _the_builds_memory_name(_Raises(), BUILD_ID) is None
        assert _the_builds_declared_settings(_Raises(), BUILD_ID) == ()
        assert _the_builds_memory_name(object(), BUILD_ID) is None
        assert _the_builds_declared_settings(object(), BUILD_ID) == ()

    def test_the_gates_reader_hands_them_to_the_sandbox_runner(
        self, ledger: Path, tmp_path: Path
    ) -> None:
        """The closure the conductor builds, driven, with the runner recorded.

        What is asserted is the one thing this stage added: the request the
        gates reader makes for a sandboxed repository carries the ledger's
        memory name and the project's declared setting names.
        """
        from forge.cli._serve_conductor import make_gates_green_reader

        worktree = tmp_path / "wt"
        worktree.mkdir()
        cx = sqlite_connect.connect_writer(ledger)
        cx.execute(
            "UPDATE builds SET worktree_path = ? WHERE build_id = ?",
            (str(worktree), BUILD_ID),
        )
        cx.commit()
        pool = SqliteLifecyclePersistence(connection=cx, db_path=ledger)
        seen: list[dict[str, Any]] = []

        def _record(**kwargs: Any) -> tuple[int, str]:
            seen.append(kwargs)
            return 0, "`the declared command` exited 0"

        reader = make_gates_green_reader(
            pool=pool,
            config=ForgeConfig.model_validate(
                {
                    "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
                    "planning": {
                        "target_repo_paths": {REPO: str(tmp_path / "canonical")},
                        "sandboxes": {
                            REPO: {
                                "name": "widget-shop-sbx",
                                "sidecar_url": "http://127.0.0.1:1",
                                "runner_url": "http://127.0.0.1:2",
                            }
                        },
                    },
                }
            ),
            sandbox_declaration_loader=lambda _root, **_kw: SimpleNamespace(
                test="the declared command", test_timeout=60
            ),
            sandbox_command_runner=_record,
            sandbox_stamps_leg=lambda **_kw: SimpleNamespace(
                status="not-enforced", detail="no stamps here", failed=()
            ),
        )
        try:
            reader(build_id=BUILD_ID, branch="autobuild/FEAT-WS1")
        finally:
            cx.close()

        assert seen, "the declared command was never asked for"
        assert seen[0]["memory_project"] == MEMORY_NAME
        assert seen[0]["launch_settings"] == (DECLARED_SETTING,)
