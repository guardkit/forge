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
* a cancel that lands before the leg's process has started stops it the
  moment it starts;
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


class TestACancelBeforeTheProcessStarts:
    def test_the_process_is_stopped_the_moment_it_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service, "LEG_CANCEL_GRACE_SECONDS", 1.0)
        leg = service._RunningLeg(number=7, build=BUILD_ID)
        leg.cancel()
        assert leg.cancelled and not leg.started

        process = subprocess.Popen(  # noqa: S603 — a fixed test argv
            ["sleep", "300"], start_new_session=True
        )
        try:
            leg.process_started(process)
            assert process.wait(timeout=PROMPT_SECONDS) != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


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
