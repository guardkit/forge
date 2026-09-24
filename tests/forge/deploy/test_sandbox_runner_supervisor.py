"""The sandbox runner's stop really ends the work inside the sandbox.

WHY THIS TEST EXISTS (24 September 2026). The service under test
(``deploy/compose/sandbox-runner/run.sh``) replaces two host units. The whole
reason those units were hard is written in one of them
(``ops/systemd/forge-sandbox-runner@.service``, "WHY ExecStop"): the client
runs outside the sandbox and all the work runs inside it, so ending the client
ends nothing, and every restart quietly added another supervisor in there. On
2026-09-11 four of them had piled up, fighting over the same two ports, and a
build died one second after its gate was tapped with nothing reporting a
problem.

So the one thing this service must get right is that a stop signal reaches
*inside* the sandbox, through the same door the start went through, and is
WAITED for. That is what is proven here.

HOW, without a sandbox. The script's whole contact with the outside world is
one binary. The tests put a stand-in on PATH that records every call, so what
is checked is exactly what the service would have asked the real client to do,
in order. Nothing here creates, touches or needs a sandbox, a container or a
daemon.

Nothing in this file names a language, a test runner, a package manager or any
project's layout: the service forwards names a project declares, and the test
declares its own throwaway ones.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

#: forge/deploy/compose/sandbox-runner/run.sh — the service under test.
RUN_SH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "compose"
    / "sandbox-runner"
    / "run.sh"
)

#: A stand-in for the sandbox client. It records the whole of every call, one
#: line each, and then behaves the way the real client would for that call:
#: the hold and the bootstrap stay open until they are killed, the stop takes a
#: moment and then returns, and anything else returns at once.
FAKE_CLIENT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
for word in "$@"; do
  case "$word" in
    *keeper-hold*) exec sleep 600 ;;
    stop)
      sleep "${FAKE_STOP_SECONDS:-1}"
      printf 'stop-finished %s\\n' "$(date +%s.%N)" >> "$CALLS"
      exit 0
      ;;
    pkill) exit 0 ;;
  esac
done
exec sleep "${FAKE_BOOTSTRAP_SECONDS:-600}"
"""


def _settings(root: Path) -> dict[str, str]:
    """A whole, valid environment for the service, with a stand-in client."""
    return {
        "PATH": os.environ["PATH"],
        "SANDBOX_CLIENT": str(root / "sbx"),
        "SANDBOX_NAME": "a-throwaway-sandbox",
        "SANDBOX_BOOTSTRAP": "the/projects/own/bootstrap",
        "SANDBOX_BOOTSTRAP_STOP_ARGUMENT": "stop",
        "SANDBOX_RESTART_SECONDS": "1",
        "SANDBOX_STOP_TIMEOUT_SECONDS": "20",
        "SANDBOXES_STORAGE_ROOT": str(root / "client-state"),
        "CALLS": str(root / "calls"),
    }


@pytest.fixture()
def root():
    """A throwaway folder holding the stand-in client and a real socket.

    The socket is real (and bound) because the service checks for one at the
    exact path the client looks for it under its storage root — the fact the
    24 September probe cost two tries to learn.
    """
    made = Path(tempfile.mkdtemp(prefix="sbxrun-"))
    client = made / "sbx"
    client.write_text(FAKE_CLIENT)
    client.chmod(0o755)
    socket_dir = made / "client-state" / "state" / "sandboxes" / "sandboxes" / "sandboxd"
    socket_dir.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_dir / "sandboxd.sock"))
    try:
        yield made
    finally:
        listener.close()
        shutil.rmtree(made, ignore_errors=True)


def _calls(root: Path) -> list[str]:
    path = root / "calls"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def _start(root: Path, **extra: str) -> subprocess.Popen:
    env = _settings(root)
    env.update(extra)
    return subprocess.Popen(
        ["bash", str(RUN_SH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for(predicate, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class TestTheStopEndsTheWorkInside:
    def test_a_stop_signal_runs_the_bootstraps_own_stop_mode(self, root: Path) -> None:
        service = _start(root)
        try:
            assert _wait_for(
                lambda: any("the/projects/own/bootstrap" in c for c in _calls(root))
            ), "the bootstrap was never started inside the sandbox"

            service.send_signal(signal.SIGTERM)
            output = service.communicate(timeout=60)[0]
        finally:
            if service.poll() is None:
                service.kill()

        calls = _calls(root)
        stop_calls = [
            c for c in calls if c.endswith("the/projects/own/bootstrap stop")
        ]
        assert stop_calls, (
            "no stop went into the sandbox. Ending the client ends nothing in "
            f"there; this is the whole point of the service.\ncalls: {calls}\n"
            f"{output}"
        )
        assert "exec a-throwaway-sandbox" in stop_calls[0], (
            "the stop must go through the same door as the start — an exec "
            f"into the same named sandbox: {stop_calls[0]}"
        )
        assert service.returncode == 0

    def test_it_waits_for_that_stop_before_it_exits(self, root: Path) -> None:
        """A container that dies without waiting is the defect, not the fix."""
        service = _start(root, FAKE_STOP_SECONDS="3")
        try:
            assert _wait_for(
                lambda: any("the/projects/own/bootstrap" in c for c in _calls(root))
            )
            service.send_signal(signal.SIGTERM)
            started_waiting = time.monotonic()
            service.communicate(timeout=60)
            waited = time.monotonic() - started_waiting
        finally:
            if service.poll() is None:
                service.kill()

        assert any(c.startswith("stop-finished") for c in _calls(root)), (
            "the service exited before the stop inside the sandbox had finished"
        )
        assert waited >= 3, (
            f"the stop takes three seconds and the service was gone in {waited:.1f}s"
        )

    def test_it_lets_the_sandbox_sleep_again_afterwards(self, root: Path) -> None:
        """Its own hold, and only its own — by a name nothing else uses."""
        service = _start(root)
        try:
            assert _wait_for(lambda: len(_calls(root)) >= 2)
            service.send_signal(signal.SIGTERM)
            service.communicate(timeout=60)
        finally:
            if service.poll() is None:
                service.kill()

        calls = _calls(root)
        assert any("pkill" in c and "keeper-hold" in c for c in calls), (
            f"the hold on the sandbox was never released: {calls}"
        )
        stop_at = next(
            i for i, c in enumerate(calls) if c.endswith("bootstrap stop")
        )
        hold_at = next(i for i, c in enumerate(calls) if "pkill" in c)
        assert stop_at < hold_at, (
            "the work inside must be stopped before the sandbox is let go: "
            f"{calls}"
        )


class TestItNeverLeavesTwoSupervisorsBehind:
    def test_a_dropped_session_is_stopped_before_another_is_opened(
        self, root: Path
    ) -> None:
        """Why the units needed an ExecStop on every automatic restart.

        A session that merely drops leaves its supervisor running inside the
        sandbox. Opening a second one without stopping the first is exactly
        how four of them piled up on 2026-09-11.
        """
        service = _start(root, FAKE_BOOTSTRAP_SECONDS="2")
        try:
            assert _wait_for(
                lambda: len(
                    [c for c in _calls(root) if c.endswith("own/bootstrap")]
                )
                >= 2,
                seconds=30,
            ), f"the bootstrap session was never opened again: {_calls(root)}"
            calls = _calls(root)
            service.send_signal(signal.SIGTERM)
            service.communicate(timeout=60)
        finally:
            if service.poll() is None:
                service.kill()

        starts = [i for i, c in enumerate(calls) if c.endswith("own/bootstrap")]
        stops = [i for i, c in enumerate(calls) if c.endswith("bootstrap stop")]
        assert stops, f"a dropped session was replaced without a stop: {calls}"
        assert stops[0] < starts[1], (
            "the second session was opened before the first was stopped: "
            f"{calls}"
        )


class TestWhatItHandsTheBootstrap:
    def test_names_with_values_are_handed_in_and_empty_ones_are_not(
        self, root: Path
    ) -> None:
        service = _start(
            root,
            SANDBOX_ENV_NAMES="A_NAME_WITH_A_VALUE A_NAME_WITH_NOTHING",
            A_NAME_WITH_A_VALUE="something",
        )
        try:
            assert _wait_for(
                lambda: any("own/bootstrap" in c for c in _calls(root))
            )
            start = next(c for c in _calls(root) if c.endswith("own/bootstrap"))
        finally:
            service.send_signal(signal.SIGTERM)
            try:
                service.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                service.kill()

        assert "env A_NAME_WITH_A_VALUE=something" in start
        assert "A_NAME_WITH_NOTHING" not in start

    def test_it_prints_names_and_never_a_value(self, root: Path) -> None:
        service = _start(
            root,
            SANDBOX_ENV_NAMES="A_NAME_WITH_A_VALUE",
            A_NAME_WITH_A_VALUE="a-value-that-must-not-be-printed",
        )
        try:
            assert _wait_for(
                lambda: any("own/bootstrap" in c for c in _calls(root))
            )
        finally:
            service.send_signal(signal.SIGTERM)
            output = service.communicate(timeout=60)[0]

        assert "A_NAME_WITH_A_VALUE" in output
        assert "a-value-that-must-not-be-printed" not in output


class TestItRefusesAtTheDoor:
    @pytest.mark.parametrize(
        "missing, expected",
        [
            ("SANDBOX_NAME", "SANDBOX_NAME is not set"),
            ("SANDBOX_BOOTSTRAP", "SANDBOX_BOOTSTRAP is not set"),
            ("SANDBOXES_STORAGE_ROOT", "SANDBOXES_STORAGE_ROOT is not set"),
        ],
    )
    def test_a_missing_setting_is_one_sentence_and_no_start(
        self, root: Path, missing: str, expected: str
    ) -> None:
        env = _settings(root)
        env.pop(missing)
        done = subprocess.run(
            ["bash", str(RUN_SH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 2
        assert expected in done.stdout
        assert not _calls(root), "it reached into a sandbox before checking itself"

    def test_a_socket_that_is_not_there_says_so_and_says_where(
        self, root: Path
    ) -> None:
        env = _settings(root)
        env["SANDBOXES_STORAGE_ROOT"] = str(root / "somewhere-else")
        (root / "somewhere-else").mkdir()
        done = subprocess.run(
            ["bash", str(RUN_SH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 2
        assert "state/sandboxes/sandboxes/sandboxd/sandboxd.sock" in done.stdout
