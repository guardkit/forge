"""A build runner with N job slots really runs N builds at once.

3 October 2026, concurrent builds. The build runner inside each project's
sandbox is ``langgraph dev``, and the installed server reads how many runs it
executes at once from ``--n-jobs-per-worker``; without the flag it sets one
(``langgraph_api/cli.py``), so a runner queued every build after the first.
The sandbox start-up script now passes the flag from
``FORGE_MAX_CONCURRENT_BUILDS`` (the rendered command is checked in
``tests/forge/deploy/test_sandbox_bootstrap_from_the_release_image.py``).

This file checks the other half: that the flag does what the design relies
on. A REAL ``langgraph dev`` from this repository's own development
environment is started on a random loopback port, serving a tiny stand-in
graph whose one node notes when it starts and ends and sleeps in between.
Four runs are asked for. With four slots all four are running before the
first one finishes; with today's command line (no flag) they run one after
another. Nothing else is started, and the server is stopped afterwards.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

#: The development server's own command line, as ``start_runner`` in
#: ``sandbox-runner.sh`` writes it, less the container around it.
LANGGRAPH = Path(sys.executable).with_name("langgraph")

#: How long the stand-in node sleeps. Long enough that four runs started by a
#: server with four slots clearly overlap, short enough that the one-slot
#: server's four in a row finish quickly.
NODE_SECONDS = 2.0

RUNS = 4

STANDIN_GRAPH = textwrap.dedent(
    """
    import asyncio
    import pathlib
    import time
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    OUT = pathlib.Path(__file__).with_name("marks")


    class State(TypedDict, total=False):
        tag: str


    async def work(state: State) -> dict:
        OUT.mkdir(exist_ok=True)
        (OUT / (state["tag"] + ".start")).write_text(repr(time.time()))
        await asyncio.sleep({seconds})
        (OUT / (state["tag"] + ".end")).write_text(repr(time.time()))
        return {{}}


    builder = StateGraph(State)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    graph = builder.compile()
    """
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def _wait_until_it_answers(url: str, proc: subprocess.Popen, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(
                f"langgraph dev stopped with {proc.returncode}:\n{output}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                response.read(0)
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise AssertionError(f"langgraph dev did not answer at {url} in {seconds}s")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


@pytest.fixture
def runner(tmp_path: Path) -> Iterator:
    """Start a real ``langgraph dev`` with the given extra arguments."""
    if not LANGGRAPH.exists():
        pytest.skip(f"the development server is not installed at {LANGGRAPH}")
    started: list[subprocess.Popen] = []

    def start(*extra: str) -> tuple[str, Path]:
        folder = tmp_path / f"runner-{len(started)}"
        folder.mkdir()
        (folder / "standin_graph.py").write_text(
            STANDIN_GRAPH.format(seconds=NODE_SECONDS)
        )
        (folder / "langgraph.json").write_text(
            json.dumps(
                {
                    "dependencies": ["."],
                    "graphs": {"standin": "./standin_graph.py:graph"},
                }
            )
        )
        port = _free_port()
        env = {
            **os.environ,
            "LANGSMITH_TRACING": "false",
            "LANGGRAPH_CLI_NO_ANALYTICS": "1",
        }
        proc = subprocess.Popen(  # noqa: S603
            [
                str(LANGGRAPH),
                "dev",
                "--config",
                str(folder / "langgraph.json"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-browser",
                "--no-reload",
                "--allow-blocking",
                *extra,
            ],
            cwd=str(folder),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        started.append(proc)
        url = f"http://127.0.0.1:{port}"
        _wait_until_it_answers(f"{url}/ok", proc, 60)
        return url, folder / "marks"

    yield start
    for proc in started:
        _stop(proc)


def _most_at_once(url: str, marks: Path) -> int:
    """Ask for four runs at once; return how many were running together."""
    for index in range(RUNS):
        thread = _post(f"{url}/threads", {})
        _post(
            f"{url}/threads/{thread['thread_id']}/runs",
            {"assistant_id": "standin", "input": {"tag": f"run-{index}"}},
        )
    deadline = time.monotonic() + RUNS * NODE_SECONDS * 3 + 30
    while time.monotonic() < deadline:
        if marks.exists() and len(list(marks.glob("*.end"))) == RUNS:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"the four runs did not all finish: {sorted(marks.glob('*'))}")
    spans = [
        (
            float((marks / f"run-{index}.start").read_text()),
            float((marks / f"run-{index}.end").read_text()),
        )
        for index in range(RUNS)
    ]
    return max(
        sum(1 for start, end in spans if start <= moment < end)
        for moment, _ in spans
    )


def test_four_slots_run_four_builds_at_once(runner) -> None:
    url, marks = runner("--n-jobs-per-worker", "4")
    assert _most_at_once(url, marks) == RUNS


def test_todays_command_line_runs_one_at_a_time(runner) -> None:
    """The negative control: the command line before this change."""
    url, marks = runner()
    assert _most_at_once(url, marks) == 1
