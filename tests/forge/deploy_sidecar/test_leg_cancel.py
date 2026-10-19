"""Stopping a running fix-journey leg in the helper (19 October 2026).

``forge cancel`` ends a running fix journey from outside. The coordinator
then released the build's place within seconds, but the leg it had sent to
the sandbox helper ran on to its end, up to half an hour of model time: the
helper does nothing when a client stops waiting. The helper now stops a
running leg on ``POST /guardkit-leg/cancel``.

What this file pins, against a real helper on loopback and a real process
group (a stand-in ``guardkit`` that starts a child of its own and waits):

* a cancel by build stops the leg AND the child it started, and the leg's own
  request answers as cancelled;
* ``GET /busy`` numbers a running leg and says once it has been asked to
  stop, and a leg that ignores TERM is killed after the grace;
* a cancel by the number ``/busy`` lists works too;
* an unknown build or number, or a request naming neither or both, is
  refused in one sentence and nothing is stopped;
* everything the leg started is stopped, a grandchild in a session of its
  own included (GuardKit starts a project's test commands that way), and a
  second build's leg is left running;
* a cancel that lands before the leg starts (even while the helper is still
  loading its configuration) means it never starts, and one that lands
  between the check and the start stops it at once;
* a finished leg is never signalled: a cancel after the finish mark sends
  nothing, and one that races the leg's own end signals only the unreaped
  (zombie) leader's group;
* the coordinator's side end to end: the leg door reads the cancelled answer
  as a failed leg that says so, and the coordinator's canceller, pointed at
  the sandbox by the build's repository, stops it.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    GUARDKIT_LEG_ROUTE,
    build_server,
)

REPO_KEY = "example-org/notes-app"
BUILD_ID = "build-FEAT-0C1A-20260101062916"
OTHER_BUILD_ID = "build-FEAT-0C1B-20260101070000"
#: The longest a test waits for something that should happen at once.
PROMPT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "notes-app"
    path.mkdir()
    return path.resolve()


@pytest.fixture
def cfg(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
        }
    )


@pytest.fixture
def tree(repo: Path) -> Path:
    path = repo / ".forge" / "worktrees" / BUILD_ID
    path.mkdir(parents=True)
    return path


def _sleeping_guardkit(
    bin_dir: Path, pids: Path, *, ignore_term: bool = False
) -> Path:
    """A stand-in ``guardkit`` that starts a child, writes both pids, waits."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "guardkit"
    trap = "trap '' TERM\n" if ignore_term else ""
    binary.write_text(
        "#!/bin/sh\n"
        f"{trap}"
        "sleep 300 &\n"
        f'echo "$$ $!" > {pids}.tmp && mv {pids}.tmp {pids}\n'
        "wait\n"
        "wait\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "leg.pids"
    monkeypatch.setenv(
        "FORGE_GUARDKIT_PATH", str(_sleeping_guardkit(tmp_path / "bin", path))
    )
    return path


@pytest.fixture
def server(cfg: ForgeConfig):
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


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


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _leg_body(tree: Path, build: str = BUILD_ID) -> dict[str, Any]:
    return {
        "repo": REPO_KEY,
        "cwd": str(tree),
        "subcommand": "task-work",
        "args": ["--task-id", "TASK-0C1A-FIX1"],
        "timeout_seconds": 120,
        "build": build,
    }


class _LegInFlight:
    """A leg request posted on its own thread; its answer kept."""

    def __init__(self, server: str, body: dict[str, Any]) -> None:
        self.answer: tuple[int, Any] | None = None
        self._thread = threading.Thread(
            target=self._run, args=(server + GUARDKIT_LEG_ROUTE, body), daemon=True
        )
        self._thread.start()

    def _run(self, url: str, body: dict[str, Any]) -> None:
        self.answer = _post(url, body, timeout=150)

    def join(self) -> tuple[int, Any]:
        self._thread.join(PROMPT_SECONDS * 2)
        assert self.answer is not None, "the leg's request never answered"
        return self.answer


def _wait_for(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + PROMPT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _started_pids(pids: Path) -> list[int]:
    _wait_for(pids.exists, "the leg to start")
    return [int(part) for part in pids.read_text().split()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _gone(pid: int) -> bool:
    try:
        _wait_for(lambda: not _alive(pid), f"pid {pid} to end")
    except AssertionError:
        return False
    return True


def _legs_on_busy(server: str) -> list[dict[str, Any]]:
    return [e for e in _get(server + BUSY_ROUTE)["executing"] if e["route"] == GUARDKIT_LEG_ROUTE]


# ---------------------------------------------------------------------------
# The helper's cancel route
# ---------------------------------------------------------------------------


class TestCancellingALegByItsBuild:
    def test_the_leg_and_the_child_it_started_are_stopped(
        self, server: str, tree: Path, pids: Path
    ) -> None:
        leg = _LegInFlight(server, _leg_body(tree))
        leader, child = _started_pids(pids)
        assert _alive(leader) and _alive(child)
        busy = _legs_on_busy(server)
        assert len(busy) == 1 and isinstance(busy[0]["leg"], int)
        assert "cancelled" not in busy[0]

        status, body = _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})

        assert status == 200, body
        assert body["cancelled"] == [
            {"leg": busy[0]["leg"], "build": BUILD_ID, "started": True}
        ]
        status, answer = leg.join()
        assert status == 200, answer
        assert answer["cancelled"] is True
        assert answer["exit_code"] != 0
        assert "because it was cancelled" in answer["stderr_tail"]
        assert _gone(leader) and _gone(child)
        _wait_for(lambda: _legs_on_busy(server) == [], "the leg to leave /busy")

    def test_busy_says_it_was_asked_to_stop_and_a_leg_ignoring_term_is_killed(
        self,
        server: str,
        tree: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stubborn = tmp_path / "stubborn.pids"
        monkeypatch.setenv(
            "FORGE_GUARDKIT_PATH",
            str(_sleeping_guardkit(tmp_path / "stubborn-bin", stubborn, ignore_term=True)),
        )
        monkeypatch.setattr(service, "LEG_CANCEL_GRACE_SECONDS", 1.5)
        leg = _LegInFlight(server, _leg_body(tree))
        leader, child = _started_pids(stubborn)

        cancel: dict[str, Any] = {}
        canceller = threading.Thread(
            target=lambda: cancel.update(
                answer=_post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})
            ),
            daemon=True,
        )
        canceller.start()
        # During the grace the leg is still there, and /busy says it was asked.
        _wait_for(
            lambda: any(e.get("cancelled") for e in _legs_on_busy(server)),
            "/busy to say the leg was asked to stop",
        )
        assert _alive(leader)
        canceller.join(PROMPT_SECONDS)
        assert cancel["answer"][0] == 200
        status, answer = leg.join()
        assert status == 200 and answer["cancelled"] is True
        assert _gone(leader) and _gone(child)

    def test_a_leg_of_another_build_is_left_running(
        self, server: str, tree: Path, pids: Path
    ) -> None:
        leg = _LegInFlight(server, _leg_body(tree))
        leader, child = _started_pids(pids)

        status, body = _post(
            server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": OTHER_BUILD_ID}
        )

        assert status == 404
        assert f"no fix-journey leg of build {OTHER_BUILD_ID} is running" in body["error"]
        time.sleep(0.3)
        assert _alive(leader) and _alive(child)
        # Clean up: stop it by its build.
        assert _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})[0] == 200
        leg.join()


class TestCancellingALegByItsNumber:
    def test_the_number_busy_lists_stops_that_leg(
        self, server: str, tree: Path, pids: Path
    ) -> None:
        leg = _LegInFlight(server, _leg_body(tree))
        leader, _child = _started_pids(pids)
        number = _legs_on_busy(server)[0]["leg"]

        status, body = _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"leg": number})

        assert status == 200, body
        assert [entry["leg"] for entry in body["cancelled"]] == [number]
        assert leg.join()[1]["cancelled"] is True
        assert _gone(leader)


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        ("body", "status", "words"),
        [
            ({"build": BUILD_ID}, 404, "no fix-journey leg of build"),
            ({"leg": 4242}, 404, "no fix-journey leg of number 4242"),
            ({}, 400, "exactly one of 'build'"),
            ({"build": BUILD_ID, "leg": 1}, 400, "exactly one of 'build'"),
            ({"build": "not a build id!"}, 400, "'build' is a build id"),
            ({"leg": "one"}, 400, "'leg' is a whole number"),
            ({"leg": True}, 400, "'leg' is a whole number"),
        ],
    )
    def test_nothing_running_or_a_malformed_ask_is_one_sentence(
        self, server: str, body: dict[str, Any], status: int, words: str
    ) -> None:
        got, answer = _post(server + GUARDKIT_LEG_CANCEL_ROUTE, body)
        assert got == status
        assert words in answer["error"]

    def test_a_body_that_is_not_an_object_is_refused(self, server: str) -> None:
        request = urllib.request.Request(
            server + GUARDKIT_LEG_CANCEL_ROUTE,
            data=b"[1, 2]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        assert caught.value.code == 400


class TestEverythingTheLegStartedIsStopped:
    """The coach's probes, 19 October 2026, adopted."""

    def test_a_grandchild_in_a_session_of_its_own_is_stopped(
        self, server: str, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GuardKit starts a project's test commands with start_new_session."""
        pids = tmp_path / "setsid.pids"
        binary = tmp_path / "setsid-bin" / "guardkit"
        binary.parent.mkdir()
        binary.write_text(
            "#!/bin/sh\n"
            "setsid sleep 300 >/dev/null 2>&1 </dev/null &\n"
            f'echo "$$ $!" > {pids}.tmp && mv {pids}.tmp {pids}\n'
            "wait\n"
            "sleep 300\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
        leg = _LegInFlight(server, _leg_body(tree))
        leader, grandchild = _started_pids(pids)
        _wait_for(
            lambda: _alive(grandchild) and os.getpgid(grandchild) != os.getpgid(leader),
            "the grandchild to be in a group of its own",
        )

        status, _body = _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})

        assert status == 200
        assert leg.join()[1]["cancelled"] is True
        try:
            assert _gone(leader)
            assert _gone(grandchild), "the grandchild in its own session survived"
        finally:
            if _alive(grandchild):
                os.kill(grandchild, 9)

    def test_cancelling_one_build_leaves_the_other_builds_leg_running(
        self, server: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = tmp_path / "two-bin" / "guardkit"
        binary.parent.mkdir()
        binary.write_text(
            "#!/bin/sh\nsleep 300 &\nn=$(basename \"$PWD\")\n"
            f'echo "$$ $!" > {tmp_path}/$n.tmp && mv {tmp_path}/$n.tmp {tmp_path}/$n.pids\n'
            "wait\nwait\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
        trees = {}
        for build in (BUILD_ID, OTHER_BUILD_ID):
            trees[build] = repo / ".forge" / "worktrees" / build
            trees[build].mkdir(parents=True)
        first = _LegInFlight(server, _leg_body(trees[BUILD_ID], BUILD_ID))
        second = _LegInFlight(server, _leg_body(trees[OTHER_BUILD_ID], OTHER_BUILD_ID))
        a1, c1 = _started_pids(tmp_path / f"{BUILD_ID}.pids")
        a2, c2 = _started_pids(tmp_path / f"{OTHER_BUILD_ID}.pids")

        status, body = _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})

        assert status == 200 and [e["build"] for e in body["cancelled"]] == [BUILD_ID]
        assert first.join()[1]["cancelled"] is True
        assert _gone(a1) and _gone(c1)
        time.sleep(0.3)
        assert _alive(a2) and _alive(c2)
        busy = _legs_on_busy(server)
        assert len(busy) == 1 and "cancelled" not in busy[0]
        _post(server + GUARDKIT_LEG_CANCEL_ROUTE, {"build": OTHER_BUILD_ID})
        assert second.join()[1]["cancelled"] is True


class TestACancelBeforeTheLegStarts:
    def test_a_cancel_while_its_configuration_loads_means_it_never_starts(
        self, cfg: ForgeConfig, tree: Path, pids: Path
    ) -> None:
        """Codex R2: the leg is cancellable from its admission."""
        loading = threading.Event()
        resume = threading.Event()

        def paused_loader() -> ForgeConfig:
            loading.set()
            assert resume.wait(PROMPT_SECONDS)
            return cfg

        srv = build_server(port=0, config_loader=paused_loader)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        url = f"http://{host}:{port}"
        try:
            leg = _LegInFlight(url, _leg_body(tree))
            assert loading.wait(PROMPT_SECONDS)
            assert len(_legs_on_busy(url)) == 1

            status, body = _post(url + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})
            assert status == 200, body
            assert body["cancelled"][0]["started"] is False
            resume.set()

            status, answer = leg.join()
            assert status == 200 and answer["cancelled"] is True
            assert "was not started" in answer["stderr_tail"]
            time.sleep(0.3)
            assert not pids.exists(), "the leg ran although it was cancelled first"
        finally:
            resume.set()
            srv.shutdown()
            srv.server_close()

    def test_a_cancel_between_the_check_and_the_start_stops_it_at_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.executor import programs

        run = programs.StoppableRun()
        real_start = programs.start_program

        def start_then_cancel(argv: Any, **kwargs: Any) -> Any:
            process = real_start(argv, **kwargs)
            # The cancel lands after the check, before the run holds the process.
            assert run.request_stop(grace=1.0) is True
            return process

        monkeypatch.setattr(programs, "start_program", start_then_cancel)
        with programs.stoppable(run):
            outcome = programs.run_program(
                ["sleep", "300"], cwd=None, env=None, timeout=PROMPT_SECONDS * 3
            )
        assert outcome.exit_code is not None and outcome.exit_code < 0
        assert not outcome.timed_out

    def test_a_run_asked_to_stop_first_is_never_started(self, tmp_path: Path) -> None:
        from forge.executor import programs

        marker = tmp_path / "ran"
        run = programs.StoppableRun()
        assert run.request_stop(grace=1.0) is True
        with programs.stoppable(run):
            outcome = programs.run_program(
                ["touch", str(marker)], cwd=None, env=None, timeout=10
            )
        assert outcome.refused_before_start
        assert not marker.exists()


class TestAFinishedLegIsNeverSignalled:
    """Codex R1: never signal a reaped process (deterministic hooks)."""

    def _signals(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
        """Every signal the stop sends, by any of its three ways."""
        from forge.executor import programs

        sent: list[tuple[str, int, int]] = []
        real_killpg, real_kill = programs.os.killpg, programs.os.kill
        real_pidfd = programs._pidfd_send_signal

        def recording_killpg(group: int, sig: int) -> None:
            sent.append(("group", group, sig))
            real_killpg(group, sig)

        def recording_kill(pid: int, sig: int) -> None:
            sent.append(("pid", pid, sig))
            real_kill(pid, sig)

        def recording_pidfd(fd: int, sig: int) -> None:
            sent.append(("pidfd", fd, sig))
            real_pidfd(fd, sig)

        monkeypatch.setattr(programs.os, "killpg", recording_killpg)
        monkeypatch.setattr(programs.os, "kill", recording_kill)
        monkeypatch.setattr(programs, "_pidfd_send_signal", recording_pidfd)
        return sent

    def test_a_cancel_after_the_finish_mark_signals_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.executor import programs

        sent = self._signals(monkeypatch)
        run = programs.StoppableRun()
        answers: list[bool] = []
        run._after_finish_mark = lambda: answers.append(run.request_stop(grace=0.2))

        with programs.stoppable(run):
            outcome = programs.run_program(["true"], cwd=None, env=None, timeout=10)

        assert outcome.exit_code == 0
        assert answers == [False]
        assert sent == []

    def test_completion_racing_the_cancel_signals_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cancel lands after the wrapper exited, before the finish mark:
        it is a zombie (its ids still reserved), nothing is under it, so the
        stop sends nothing and the program keeps its own exit code."""
        from forge.executor import programs

        sent = self._signals(monkeypatch)
        run = programs.StoppableRun()
        seen: dict[str, Any] = {}

        def cancel_while_a_zombie() -> None:
            pid = run._process.pid
            stat = Path(f"/proc/{pid}/stat").read_text()
            seen["state"] = stat.rsplit(")", 1)[1].split()[0]
            seen["pid"] = pid
            seen["answer"] = run.request_stop(grace=0.2)

        run._before_finish_mark = cancel_while_a_zombie
        with programs.stoppable(run):
            outcome = programs.run_program(["true"], cwd=None, env=None, timeout=10)

        assert seen["state"] == "Z"
        assert seen["answer"] is False
        assert sent == []
        assert outcome.exit_code == 0
        assert run.finished


# ---------------------------------------------------------------------------
# The coordinator's side, against the same real helper
# ---------------------------------------------------------------------------


class TestTheCoordinatorsSideEndToEnd:
    def test_the_leg_door_reads_a_cancelled_leg_as_failed_and_says_why(
        self, server: str, repo: Path, tree: Path, pids: Path
    ) -> None:
        from forge.adapters.guardkit.run_via_sidecar import (
            build_sidecar_leg_run,
            cancel_leg_via_sidecar,
        )

        run_leg = build_sidecar_leg_run(
            base_url=server, repo_paths={REPO_KEY: str(repo)}, repo=REPO_KEY
        )

        async def scenario() -> Any:
            journey_leg = asyncio.ensure_future(
                run_leg(
                    subcommand="task-work",
                    args=["--task-id", "TASK-0C1A-FIX1"],
                    repo_path=tree,
                    timeout_seconds=120,
                    with_nats_streaming=False,
                    build=BUILD_ID,
                )
            )
            await asyncio.to_thread(_started_pids, pids)
            stopped, sentence = await asyncio.to_thread(
                cancel_leg_via_sidecar, base_url=server, build=BUILD_ID
            )
            assert stopped is True, sentence
            return await asyncio.wait_for(journey_leg, PROMPT_SECONDS * 2)

        result = asyncio.run(scenario())

        assert result.status == "failed"
        assert "leg_cancelled" in [w.code for w in result.warnings]

    def test_a_cancel_asked_for_a_build_with_nothing_running_says_so(
        self, server: str
    ) -> None:
        from forge.adapters.guardkit.run_via_sidecar import cancel_leg_via_sidecar

        stopped, sentence = cancel_leg_via_sidecar(base_url=server, build=BUILD_ID)
        assert stopped is False
        assert "HTTP 404" in sentence and "no fix-journey leg" in sentence

    def test_a_helper_that_cannot_be_reached_is_a_sentence_not_an_error(
        self,
    ) -> None:
        from forge.adapters.guardkit.run_via_sidecar import cancel_leg_via_sidecar

        stopped, sentence = cancel_leg_via_sidecar(
            base_url="http://127.0.0.1:9", build=BUILD_ID, timeout=2
        )
        assert stopped is False and "could not be asked" in sentence

    def test_the_coordinators_canceller_finds_the_sandbox_and_stops_the_leg(
        self, server: str, repo: Path, tree: Path, pids: Path
    ) -> None:
        from forge.cli._serve_conductor import make_conductor_leg_canceller

        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {
                    "target_repo_paths": {REPO_KEY: str(repo)},
                    "sandboxes": {
                        REPO_KEY: {
                            "name": "notes-app-sandbox",
                            "sidecar_url": server,
                            "runner_url": "http://127.0.0.1:8224",
                        }
                    },
                },
            }
        )

        class _Pool:
            def get_build_row(self, build_id: str) -> Any:
                assert build_id == BUILD_ID
                return type("Row", (), {"repo": REPO_KEY})()

        cancel = make_conductor_leg_canceller(pool=_Pool(), config=config)
        leg = _LegInFlight(server, _leg_body(tree))
        leader, child = _started_pids(pids)

        asyncio.run(cancel(BUILD_ID))

        assert leg.join()[1]["cancelled"] is True
        assert _gone(leader) and _gone(child)

    def test_a_build_whose_repository_has_no_sandbox_asks_nobody(
        self, repo: Path
    ) -> None:
        from forge.cli._serve_conductor import make_conductor_leg_canceller

        asked: list[Any] = []
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
            }
        )

        class _Pool:
            def get_build_row(self, build_id: str) -> Any:  # pragma: no cover
                raise AssertionError("no sandboxes: the row is not read")

        cancel = make_conductor_leg_canceller(
            pool=_Pool(), config=config, cancel_leg=lambda **kw: asked.append(kw)
        )
        asyncio.run(cancel(BUILD_ID))
        assert asked == []


# ---------------------------------------------------------------------------
# Codex round 2: descendants are held by pidfd; admission is one step
# ---------------------------------------------------------------------------


def _run_in_thread(run: Any, argv: list[str]) -> tuple[threading.Thread, dict[str, Any]]:
    from forge.executor import programs

    result: dict[str, Any] = {}

    def go() -> None:
        with programs.stoppable(run):
            result["outcome"] = programs.run_program(
                argv, cwd=None, env=None, timeout=PROMPT_SECONDS * 6
            )

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, result


def _read_pid(path: Path) -> int:
    _wait_for(path.exists, f"{path.name} to be written")
    return int(path.read_text().split()[0])


class TestDescendantsAreHeldByPidfd:
    def test_a_pid_the_walk_gives_that_is_not_ours_is_never_signalled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A descendant's id reused by an unrelated process: the walk is made
        to report that process as the leader's child; it is never signalled."""
        from forge.executor import programs

        started = tmp_path / "leader.pid"
        unrelated = subprocess.Popen(  # noqa: S603 — ours, not started under the leg
            ["sleep", "300"], start_new_session=True
        )
        try:
            run = programs.StoppableRun()
            real_children = programs._children_of
            leader_pid: dict[str, int] = {}

            def children_with_a_stranger(pid: int) -> list[int]:
                found = real_children(pid)
                if pid == leader_pid.get("pid"):
                    found = [*found, unrelated.pid]
                return found

            monkeypatch.setattr(programs, "_children_of", children_with_a_stranger)
            thread, result = _run_in_thread(
                run, ["sh", "-c", f"echo $$ > {started}; sleep 300 & wait"]
            )
            leader_pid["pid"] = _read_pid(started)

            assert run.request_stop(grace=1.0) is True
            thread.join(PROMPT_SECONDS)

            assert result["outcome"].exit_code is not None
            time.sleep(0.2)
            assert unrelated.poll() is None, "an unrelated process was signalled"
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_the_kill_reaches_a_descendant_started_during_the_grace(
        self, tmp_path: Path
    ) -> None:
        """A descendant in a session of its own answers TERM by starting a new
        child; the second walk after the grace finds that child and kills it."""
        from forge.executor import programs

        d_pid, n_pid = tmp_path / "d.pid", tmp_path / "n.pid"
        descendant = tmp_path / "descendant.sh"
        descendant.write_text(
            "#!/bin/sh\n"
            f"trap 'sleep 300 >/dev/null 2>&1 </dev/null & echo $! > {n_pid}.tmp; "
            f"mv {n_pid}.tmp {n_pid}' TERM\n"
            "while :; do sleep 0.05; done\n",
            encoding="utf-8",
        )
        descendant.chmod(0o755)
        run = programs.StoppableRun()
        thread, result = _run_in_thread(
            run,
            [
                "sh",
                "-c",
                f"setsid {descendant} >/dev/null 2>&1 </dev/null & "
                f"echo $! > {d_pid}; wait",
            ],
        )
        d = _read_pid(d_pid)
        _wait_for(lambda: _alive(d) and os.getpgid(d) == d, "the descendant's own session")
        try:
            assert run.request_stop(grace=1.5) is True
            thread.join(PROMPT_SECONDS)
            n = _read_pid(n_pid)  # started on the TERM, during the grace
            assert _gone(d), "the descendant survived the KILL"
            assert _gone(n), "the child started during the grace survived"
        finally:
            for path in (d_pid, n_pid):
                if path.exists():
                    pid = int(path.read_text().split()[0])
                    if _alive(pid):
                        os.kill(pid, 9)


class TestAdmissionIsOneStep:
    def test_a_cancel_straight_after_admission_finds_the_pending_leg(
        self, cfg: ForgeConfig, tree: Path, pids: Path
    ) -> None:
        """Codex R2: counted as executing and cancellable in one hold of the
        lock; a hook pauses the request straight after that."""
        admitted = threading.Event()
        resume = threading.Event()
        srv = build_server(port=0, config_loader=lambda: cfg)

        def pause_after_admission(number: int, route: str) -> None:
            if route == GUARDKIT_LEG_ROUTE:
                admitted.set()
                assert resume.wait(PROMPT_SECONDS)

        srv.after_admission = pause_after_admission
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        url = f"http://{host}:{port}"
        try:
            leg = _LegInFlight(url, _leg_body(tree))
            assert admitted.wait(PROMPT_SECONDS)

            status, body = _post(url + GUARDKIT_LEG_CANCEL_ROUTE, {"build": BUILD_ID})

            assert status == 200, body
            assert body["cancelled"][0]["started"] is False
            resume.set()
            status, answer = leg.join()
            assert status == 200 and answer["cancelled"] is True
            assert "was not started" in answer["stderr_tail"]
            time.sleep(0.3)
            assert not pids.exists(), "the leg ran although it was cancelled first"
        finally:
            resume.set()
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------------------
# Codex round 3: everything stays under a subreaper wrapper
# ---------------------------------------------------------------------------


class TestEverythingStaysUnderTheWrapper:
    def test_a_child_of_a_detached_parent_that_exits_during_the_grace_is_stopped(
        self, tmp_path: Path
    ) -> None:
        """Codex R3: a detached parent answers TERM by starting a child and
        exiting at once, before the KILL. The orphan is re-parented to the
        wrapper (not to init), so the walk before the KILL finds it."""
        from forge.executor import programs

        d_pid, n_pid = tmp_path / "d.pid", tmp_path / "n.pid"
        detached = tmp_path / "detached.sh"
        detached.write_text(
            "#!/bin/sh\n"
            f"trap 'sleep 300 >/dev/null 2>&1 </dev/null & echo $! > {n_pid}.tmp; "
            f"mv {n_pid}.tmp {n_pid}; exit 0' TERM\n"
            "while :; do sleep 0.05; done\n",
            encoding="utf-8",
        )
        detached.chmod(0o755)
        run = programs.StoppableRun()
        thread, result = _run_in_thread(
            run,
            [
                "sh",
                "-c",
                f"setsid {detached} >/dev/null 2>&1 </dev/null & "
                f"echo $! > {d_pid}; wait",
            ],
        )
        d = _read_pid(d_pid)
        _wait_for(lambda: _alive(d) and os.getpgid(d) == d, "the detached parent")
        try:
            assert run.request_stop(grace=1.5) is True
            thread.join(PROMPT_SECONDS)
            n = _read_pid(n_pid)
            assert _gone(d)
            assert _gone(n), "the orphan of a detached parent survived the stop"
        finally:
            for path in (d_pid, n_pid):
                if path.exists():
                    pid = int(path.read_text().split()[0])
                    if _alive(pid):
                        os.kill(pid, 9)

    @pytest.mark.parametrize(
        "argv",
        [
            ["sh", "-c", "echo out; echo err >&2; exit 3"],
            ["sh", "-c", "echo before; kill -TERM $$"],
            ["sh", "-c", "exit 0"],
        ],
    )
    def test_a_normal_programs_exit_code_and_output_are_unchanged(
        self, argv: list[str]
    ) -> None:
        from forge.executor import programs

        plain = programs.run_program(argv, cwd=None, env=None, timeout=20)
        with programs.stoppable(programs.StoppableRun()):
            wrapped = programs.run_program(argv, cwd=None, env=None, timeout=20)
        assert (wrapped.exit_code, wrapped.stdout, wrapped.stderr) == (
            plain.exit_code,
            plain.stdout,
            plain.stderr,
        )
        assert not wrapped.timed_out and not wrapped.refused_before_start

    def test_a_program_that_cannot_be_started_is_still_refused_before_start(
        self, tmp_path: Path
    ) -> None:
        from forge.executor import programs

        missing = str(tmp_path / "no-such-program")
        plain = programs.run_program([missing], cwd=None, env=None, timeout=20)
        with programs.stoppable(programs.StoppableRun()):
            wrapped = programs.run_program([missing], cwd=None, env=None, timeout=20)
        assert plain.refused_before_start and wrapped.refused_before_start
        assert isinstance(wrapped.error, FileNotFoundError)

    def test_a_time_limit_still_stops_it_and_says_so(self) -> None:
        from forge.executor import programs

        with programs.stoppable(programs.StoppableRun()):
            outcome = programs.run_program(
                ["sh", "-c", "echo started; sleep 300"],
                cwd=None,
                env=None,
                timeout=1,
                kill_grace_seconds=1,
            )
        assert outcome.timed_out
        assert outcome.stdout == b"started\n"
