"""One planning step in the project's sandbox (``POST /planning-step``, 28 October 2026).

The helper starts one short-lived container from the planner image per
planning step, with a read-only copy of the project at the planning start
commit for the spec and plan steps. What this file pins, with a fake container
client (no container is ever started here) and a real git repository:

* plain request validation, each refusal before anything is made or started;
* the exact client argument list and the container's settings, per role, with
  none of the helper's other settings;
* the copy at the start commit (tracked files only) for the spec and plan
  steps, and none, no code root and no memory for ``greenfield``;
* the planner's ``result.json`` answered as it is;
* the copy, the request and answer folders and the settings file removed on
  every ending, and the container removed by name when the step is stopped at
  its time limit or cancelled;
* the route listed on ``GET /busy`` and stopped by ``POST
  /guardkit-leg/cancel`` with its build, through the real service.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.deploy_sidecar import service
from forge.deploy_sidecar.service import (
    BUSY_ROUTE,
    GUARDKIT_LEG_CANCEL_ROUTE,
    PLANNING_STEP_ROUTE,
    build_server,
    planner_container_settings,
    process_planning_step_request,
    run_planning_container,
)
from forge.executor import programs
from forge.executor.programs import StoppableRun

REPO = "example-org/notes-app"
BUILD = "PLAN-0C1A-20261028"
IMAGE = "specialist-agent:2026-10-28-abc1234"
MODEL_DOOR = "http://192.0.2.10:31823"
MEMORY_DOOR = "http://192.0.2.10:31851/mcp"
PROMPT_SECONDS = 10.0

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}

RESULT = {
    "command": "feature_plan",
    "result": {"role_id": "architect", "role_output": {"plan": "yes"}},
    "correlation_id": "ab" * 16,
    "success": True,
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, env=_GIT_ENV, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "notes-app"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "app.py").write_text("print('first')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "first")
    # Committed later, and an untracked file: neither is in the copy.
    (root / "later.py").write_text("later\n", encoding="utf-8")
    _git(root, "add", "later.py")
    _git(root, "commit", "-q", "-m", "later")
    (root / "untracked.txt").write_text("not tracked\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def first_commit(clone: Path) -> str:
    return _git(clone, "rev-parse", "HEAD~1")


@pytest.fixture
def config(clone: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )


def _helper_env(**extra: str) -> dict[str, str]:
    return {
        "FORGE_SIDECAR_IN_SANDBOX": "1",
        "FORGE_PLANNER_IMAGE": IMAGE,
        "OPENAI_BASE_URL": MODEL_DOOR,
        "FLEET_MEMORY_MCP_URL": MEMORY_DOOR,
        # A setting the helper holds that the planner must never be given.
        "FLEET_MEMORY_PG_DSN": "postgresql://not-for-the-planner",
        **extra,
    }


def _body(command: str, start_commit: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repo": REPO,
        "build": BUILD,
        "role": "architect" if command == "feature_plan" else "product-owner",
        "request": {
            "command": command,
            "args": {"from_input": "a feature", "time_budget_seconds": 3480},
            "correlation_id": "ab" * 16,
        },
        "timeout": 3600,
    }
    if start_commit is not None:
        body["start_commit"] = start_commit
        body["declared_at"] = start_commit
    body.update(extra)
    return body


class _FakeClient:
    """Records every client call; on ``run`` writes the planner's answer and
    captures what the container would have been given."""

    def __init__(
        self,
        *,
        result: Any = RESULT,
        run_answer: Any = (0, "", "planner log\n"),
        during_run: Any = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.run_answer = run_answer
        self.during_run = during_run
        self.env_file: dict[str, str] | None = None
        self.env_file_mode: int | None = None
        self.request: Any = None
        self.copy_files: list[str] | None = None
        self.copy_path: Path | None = None
        self.output: Path | None = None

    def __call__(self, *, argv: list[str], cwd: str, timeout: float, what: str = "") -> Any:
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if argv[1] != "run":
            return (0, "", "")
        mounts = {
            argv[i + 1].split(":")[1]: argv[i + 1].split(":")[0]
            for i, word in enumerate(argv)
            if word == "-v"
        }
        env_path = Path(argv[argv.index("--env-file") + 1])
        self.env_file_mode = stat.S_IMODE(env_path.stat().st_mode)
        self.env_file = dict(
            line.split("=", 1) for line in env_path.read_text().splitlines() if line
        )
        self.request = json.loads(Path(mounts["/in/request.json"]).read_text())
        if "/work" in mounts:
            self.copy_path = Path(mounts["/work"])
            self.copy_files = sorted(
                str(p.relative_to(self.copy_path))
                for p in self.copy_path.rglob("*")
                if p.is_file()
            )
        self.output = Path(mounts["/app/output"])
        if self.result is not None:
            (self.output / "result.json").write_text(
                self.result if isinstance(self.result, str) else json.dumps(self.result)
            )
        if self.during_run is not None:
            self.during_run()
        return self.run_answer

    def runs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["argv"][1] == "run"]

    def removals(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["argv"][1] == "rm"]


def _recorded_start(sha: str):
    return lambda build: {"build": build, "start_commit": sha}


def _leftovers(clone: Path) -> list[str]:
    root = clone / ".forge-candidates"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _run(config, payload, client, **kwargs) -> tuple[int, dict[str, Any]]:
    kwargs.setdefault("env", _helper_env())
    kwargs.setdefault("docker", "docker")
    return process_planning_step_request(payload, config=config, runner=client, **kwargs)


# ---------------------------------------------------------------------------
# Request validation: refused before anything is made or started
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"repo": "nobody/unknown"}, "nobody/unknown"),
            ({"role": "builder"}, "'role' must be one of architect, product-owner"),
            ({"request": "feature_plan"}, "'request' must be the command"),
            (
                {"request": {"command": "task-work", "args": {}}},
                "'request.command' must be one of greenfield, feature_spec, feature_plan",
            ),
            (
                {"request": {"command": "feature_plan", "args": ["x"]}},
                "'request.args' must be an object",
            ),
            ({"start_commit": "abc1234"}, "'start_commit' is required for feature_plan"),
            ({"timeout": 0}, "'timeout' must be a positive number"),
            ({"timeout": True}, "'timeout' must be a positive number"),
            ({"memory_project": "not a name!"}, "'memory_project'"),
            ({"build": "bad build id"}, "'build'"),
        ],
    )
    def test_a_bad_request_is_refused_and_nothing_starts(
        self, config, clone, first_commit, change, said
    ) -> None:
        client = _FakeClient()
        status, body = _run(
            config, {**_body("feature_plan", first_commit), **change}, client,
            ask=_recorded_start(first_commit),
        )
        assert status == 400, body
        assert said in body["error"]
        assert client.calls == []
        assert _leftovers(clone) == []

    def test_the_spec_step_needs_a_start_commit_too(self, config, clone) -> None:
        client = _FakeClient()
        status, body = _run(config, _body("feature_spec", None), client)
        assert status == 400
        assert "'start_commit' is required for feature_spec" in body["error"]
        assert client.calls == []

    def test_a_host_helper_starts_no_planning_container(self, config, first_commit) -> None:
        client = _FakeClient()
        env = _helper_env()
        env.pop("FORGE_SIDECAR_IN_SANDBOX")
        status, body = _run(config, _body("feature_plan", first_commit), client, env=env)
        assert status == 400
        assert "not inside a repository's sandbox" in body["error"]
        assert client.calls == []

    def test_no_planner_image_is_a_clear_error(self, config, clone, first_commit) -> None:
        client = _FakeClient()
        env = _helper_env()
        env.pop("FORGE_PLANNER_IMAGE")
        status, body = _run(config, _body("feature_plan", first_commit), client, env=env)
        assert status == 500
        assert "planner image not configured" in body["error"]
        assert client.calls == []
        assert _leftovers(clone) == []

    def test_a_memory_name_the_coordinator_cannot_confirm_is_refused(
        self, config, clone, first_commit
    ) -> None:
        client = _FakeClient()
        status, body = _run(
            config,
            _body("feature_plan", first_commit, memory_project="notes_app"),
            client,
            ask=lambda build: {"build": build, "start_commit": "f" * 40},
        )
        assert status == 400
        assert "different commits" in body["error"]
        assert client.calls == []

    def test_a_longer_time_limit_is_cut_back_not_refused(
        self, config, first_commit
    ) -> None:
        client = _FakeClient()
        status, body = _run(
            config, _body("feature_plan", first_commit, timeout=10_000_000), client
        )
        assert status == 200
        assert client.calls[0]["timeout"] == service.LEG_TIMEOUT_MAX
        assert body["warnings"][0]["code"] == "planning_step_timeout_clamped"


# ---------------------------------------------------------------------------
# What is started, and with what
# ---------------------------------------------------------------------------


class TestThePlanStep:
    def test_the_exact_argument_list_settings_copy_and_answer(
        self, config, clone, first_commit
    ) -> None:
        client = _FakeClient()
        status, body = _run(
            config,
            _body("feature_plan", first_commit, memory_project="notes_app"),
            client,
            ask=_recorded_start(first_commit),
        )
        assert status == 200, body
        assert body["result"] == RESULT
        assert body["exit_code"] == 0 and body["timed_out"] is False
        assert "planner log" in body["stderr_tail"]

        [argv] = client.runs()
        name = body["container"]
        assert name.startswith(f"forge-planning-{BUILD}-feature-plan-")
        copy = clone / ".forge-candidates" / name
        io = clone / ".forge-candidates" / f"{name}.io"
        env_file = argv[argv.index("--env-file") + 1]
        assert argv == [
            "docker", "run", "--rm", "--name", name,
            "--user", "1000:1000", "--network", "host",
            "-v", f"{copy}:/work:ro",
            "-v", f"{io / 'output'}:/app/output",
            "-v", f"{io / 'request.json'}:/in/request.json:ro",
            "--env-file", env_file,
            IMAGE,
            "run-mode", "--role", "architect",
            "--request", "/in/request.json", "--output", "/app/output",
        ]
        # The copy is the start commit's tracked files only.
        assert client.copy_files == ["app.py"]
        # The request is Forge's command, unchanged.
        assert client.request == _body("feature_plan", first_commit)["request"]
        assert client.env_file_mode == 0o600
        assert client.env_file == {
            "LOCAL_MODEL": "architect-agent",
            "SPECIALIST_AGENT_REASK_TEMPERATURE": "0",
            "SPECIALIST_AGENT_SCENARIO_OWNERSHIP": "on",
            "SPECIALIST_AGENT_TASK_MAX_CRITERIA": "8",
            "SPECIALIST_AGENT_TASK_MAX_MINUTES": "180",
            "SPECIALIST_AGENT_LOCAL_TIMEOUT": "900",
            "SPECIALIST_AGENT_FEATURE_ROUTING_REQUIRED": "1",
            "AGENT_MODELS__REASONING_MODEL": "local",
            "HOME": "/tmp",
            "OPENAI_API_KEY": "no-key-the-model-door-adds-it",
            "LLM_BASE_URL": MODEL_DOOR,
            "SPECIALIST_CODE_ROOT": "/work",
            "FLEET_MEMORY_ENABLED": "true",
            "FLEET_MEMORY_MCP_URL": MEMORY_DOOR,
            "SPECIALIST_MEMORY_PROJECT": "notes_app",
        }
        # Nothing is left behind: not the copy, the folders or the settings file.
        assert _leftovers(clone) == []
        assert not Path(env_file).exists()
        assert client.removals() == []

    def test_the_writer_thinking_setting_is_passed_on_only_when_set(self) -> None:
        on = planner_container_settings(
            role="architect",
            with_copy=True,
            memory_project=None,
            env={"SPECIALIST_AGENT_WRITER_THINKING": "on"},
        )
        off = planner_container_settings(
            role="architect", with_copy=True, memory_project=None, env={}
        )
        assert on["SPECIALIST_AGENT_WRITER_THINKING"] == "on"
        assert "SPECIALIST_AGENT_WRITER_THINKING" not in off
        # No memory name: memory stays off rather than searching a shelf the
        # memory door refuses.
        assert off["FLEET_MEMORY_ENABLED"] == "false"
        assert "SPECIALIST_MEMORY_PROJECT" not in off

    def test_the_spec_step_runs_as_the_product_owner(
        self, config, clone, first_commit
    ) -> None:
        client = _FakeClient()
        status, _ = _run(config, _body("feature_spec", first_commit), client)
        assert status == 200
        [argv] = client.runs()
        assert argv[-6:-4] == ["--role", "product-owner"]
        assert client.env_file["LOCAL_MODEL"] == "product-owner-agent"
        assert client.env_file["SPECIALIST_AGENT_REASK_TEMPERATURE"] == "1.0"
        assert "SPECIALIST_AGENT_SCENARIO_OWNERSHIP" not in client.env_file
        assert client.env_file["SPECIALIST_CODE_ROOT"] == "/work"
        assert client.copy_files == ["app.py"]


class TestGreenfield:
    def test_no_copy_no_code_root_no_memory(self, config, clone) -> None:
        client = _FakeClient(result={**RESULT, "command": "greenfield"})
        status, body = _run(config, _body("greenfield", None), client)
        assert status == 200, body
        [argv] = client.runs()
        assert not any(word.endswith(":/work:ro") for word in argv)
        assert client.copy_path is None
        assert "SPECIALIST_CODE_ROOT" not in client.env_file
        assert client.env_file["FLEET_MEMORY_ENABLED"] == "false"
        assert "FLEET_MEMORY_MCP_URL" not in client.env_file
        assert "SPECIALIST_MEMORY_PROJECT" not in client.env_file
        assert client.env_file["LOCAL_MODEL"] == "product-owner-agent"
        assert body["result"]["command"] == "greenfield"
        assert _leftovers(clone) == []


# ---------------------------------------------------------------------------
# Every ending cleans up; a stopped step's container is removed by name
# ---------------------------------------------------------------------------


class TestEndings:
    def test_no_answer_is_said_and_cleaned_up(self, config, clone, first_commit) -> None:
        client = _FakeClient(result=None, run_answer=(1, "", "it broke"))
        status, body = _run(config, _body("feature_plan", first_commit), client)
        assert status == 200
        assert body["result"] is None
        assert "exited 1 and wrote no result.json" in body["error"]
        assert client.removals() == []
        assert _leftovers(clone) == []

    def test_an_unreadable_answer_is_said(self, config, clone, first_commit) -> None:
        client = _FakeClient(result="{not json")
        status, body = _run(config, _body("feature_plan", first_commit), client)
        assert status == 200
        assert body["result"] is None
        assert "could not be read" in body["error"]
        assert _leftovers(clone) == []

    def test_a_time_limit_removes_the_container_and_the_copy(
        self, config, clone, first_commit
    ) -> None:
        client = _FakeClient(
            result=None,
            run_answer=programs.TimedOut(124, "", "stopped after 3600 seconds"),
        )
        status, body = _run(config, _body("feature_plan", first_commit), client)
        assert status == 200
        assert body["timed_out"] is True and body["result"] is None
        assert client.removals() == [["docker", "rm", "-f", body["container"]]]
        assert _leftovers(clone) == []

    def test_a_cancel_removes_the_container_and_the_copy(
        self, config, clone, first_commit
    ) -> None:
        run = StoppableRun()
        client = _FakeClient(
            result=None,
            run_answer=(143, "", "terminated"),
            during_run=lambda: run.request_stop(grace=0),
        )
        status, body = _run(
            config, _body("feature_plan", first_commit), client, stop_run=run
        )
        assert status == 200
        assert body["cancelled"] is True and body["result"] is None
        assert client.removals() == [["docker", "rm", "-f", body["container"]]]
        assert _leftovers(clone) == []

    def test_a_client_that_raised_is_cleaned_up_too(
        self, config, clone, first_commit
    ) -> None:
        def boom(**_kwargs: Any) -> Any:
            raise RuntimeError("engine gone")

        status, body = _run(config, _body("feature_plan", first_commit), boom)
        assert status == 500
        assert "engine gone" in body["error"]
        assert _leftovers(clone) == []

    def test_the_real_client_runner_stops_at_its_limit(
        self, config, clone, first_commit, tmp_path: Path
    ) -> None:
        """The production runner, with a stand-in container client (a shell
        script, never a container): stopped at its limit, then the container
        is removed by name."""
        calls = tmp_path / "calls"
        docker = tmp_path / "bin" / "docker"
        docker.parent.mkdir()
        docker.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {calls}\n'
            'if [ "$1" = run ]; then sleep 30; fi\n',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        started = time.monotonic()
        status, body = _run(
            config,
            _body("feature_plan", first_commit, timeout=1),
            run_planning_container,
            docker=str(docker),
        )
        assert time.monotonic() - started < 20
        assert status == 200, body
        assert body["timed_out"] is True
        said = calls.read_text().splitlines()
        assert said[0].startswith("run --rm --name forge-planning-")
        assert said[-1] == f"rm -f {body['container']}"
        assert _leftovers(clone) == []


# ---------------------------------------------------------------------------
# Through the real service: listed on /busy, stopped by the leg cancel route
# ---------------------------------------------------------------------------


def _post(url: str, body: dict[str, Any], timeout: float = 30) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_for(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + PROMPT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class TestThroughTheService:
    def test_listed_on_busy_and_cancelled_by_build(
        self, config, clone, first_commit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in _helper_env().items():
            monkeypatch.setenv(name, value)
        running = threading.Event()

        def wait_for_the_cancel() -> None:
            # The handler thread's stoppable run, as run_program would see it.
            run = programs._started_observer.run
            running.set()
            _wait_for(lambda: run.stop_requested, "the cancel")

        client = _FakeClient(
            result=None, run_answer=(143, "", ""), during_run=wait_for_the_cancel
        )
        srv = build_server(
            port=0, config_loader=lambda: config, planning_runner=client
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        url = f"http://{host}:{port}"
        answer: list[Any] = []
        try:
            poster = threading.Thread(
                target=lambda: answer.append(
                    _post(url + PLANNING_STEP_ROUTE, _body("feature_spec", first_commit))
                ),
                daemon=True,
            )
            poster.start()
            assert running.wait(PROMPT_SECONDS), "the planning step never started"
            with urllib.request.urlopen(url + BUSY_ROUTE, timeout=5) as response:
                busy = json.loads(response.read().decode("utf-8"))
            [entry] = busy["executing"]
            assert entry["route"] == PLANNING_STEP_ROUTE and entry["repo"] == REPO
            assert "leg" in entry

            status, cancelled = _post(url + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD})
            assert status == 200, cancelled
            poster.join(PROMPT_SECONDS * 2)
            [(status, body)] = answer
            assert status == 200, body
            assert body["cancelled"] is True and body["result"] is None
            [engine] = {argv[0] for argv in client.runs()}
            assert client.removals() == [[engine, "rm", "-f", body["container"]]]
            assert _leftovers(clone) == []
        finally:
            srv.shutdown()
            srv.server_close()

    def test_an_unknown_route_spelling_is_still_refused(self, config) -> None:
        srv = build_server(port=0, config_loader=lambda: config)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        try:
            status, _ = _post(f"http://{host}:{port}/planning-steps", {})
            assert status == 404
        finally:
            srv.shutdown()
            srv.server_close()


def test_the_container_client_is_given_nothing_of_the_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The client's own environment: enough to reach the engine, no key."""
    seen: dict[str, Any] = {}

    def fake_start(*, argv, cwd, env, timeout, what):
        seen["env"] = env
        return (0, "", "")

    monkeypatch.setattr(service, "_start_and_collect", fake_start)
    monkeypatch.setenv("OPENAI_API_KEY", "a-real-key")
    monkeypatch.setenv("NATS_PASSWORD", "a-password")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    run_planning_container(argv=["docker", "ps"], cwd=str(tmp_path), timeout=5)
    assert "PATH" in seen["env"]
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "NATS_PASSWORD" not in seen["env"]
