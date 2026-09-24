"""A stop that fails starts nothing, and a stop carries the settings the start did.

WHY THIS FILE EXISTS (24 September 2026). A reviewer drove the committed
supervisor (``deploy/compose/sandbox-runner/run.sh``) against a stand-in client
with real processes standing for the work inside the sandbox, and found two
ways it could still leave two supervisors running in there — the very pile-up
the service exists to prevent:

1. **A failed stop still permitted a new start.** The stop's return code was
   thrown away, so a stop that ended nonzero, or that never finished, was
   followed by a start anyway — and the shutdown printed "stopped" and exited
   zero with the work still running inside.
2. **The stop left out the settings the start was given.** A bootstrap that
   picks its own process record or its target out of one of those settings was
   therefore asked to stop something else, found nothing, and said it had
   succeeded while its own work carried on.

Its companion file, ``test_sandbox_runner_supervisor.py``, proves the ordinary
path with a stand-in that only records calls. This one needs more than a record
of calls: it keeps **real processes** standing for the work inside, so each test
can ask the question that matters — *is the work actually gone, and is there
only ever one of it?* — instead of trusting that a stop was asked for.

Nothing here creates, touches or needs a sandbox, a container or a daemon. The
only processes involved are this test's own ``sleep`` fixtures, and every one of
them is ended when the test ends. Nothing here names a language, a test runner,
a package manager or any project's layout: the service forwards names a project
declares, and this file declares its own throwaway ones.
"""

from __future__ import annotations

import json
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

#: The made-up command this test's project declares as its bootstrap.
BOOTSTRAP = "the/projects/own/bootstrap"

#: A stand-in for the sandbox client that behaves the way the real one does in
#: the single respect that matters: the work it starts runs somewhere it does
#: not control, so killing the client leaves that work running. Here "somewhere
#: else" is a process of its own session; in life it is inside the sandbox.
#:
#: It writes one line per call, and what a stop does depends on ``config.json``:
#: ``stop_fails_from`` / ``stop_times_out_from`` are the numbered stop at which
#: stops begin to fail or to hang, and ``drop_sessions`` is how many of the
#: first bootstrap sessions end by themselves with the work still running.
STAND_IN_CLIENT = '''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
config = json.loads((here / "config.json").read_text())
ledger = here / "events.jsonl"
NEVER = 10 ** 6


def record(item):
    handle = os.open(ledger, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.write(handle, (json.dumps(item) + "\\n").encode())
    os.close(handle)


def happened():
    if not ledger.exists():
        return []
    out = []
    for line in ledger.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def alive(pid):
    try:
        return Path("/proc/%d/stat" % pid).read_text().split(") ")[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


request = sys.argv[3:]  # everything after: <client> exec <sandbox>
settings = {}
if request and request[0] == "env":
    rest = request[1:]
    while rest and "=" in rest[0]:
        name, value = rest.pop(0).split("=", 1)
        settings[name] = value
    request = rest
record({"kind": "call", "request": request, "settings": settings})
slot = settings.get("BOOTSTRAP_SLOT", "default")

if not request:
    sys.exit(0)
elif request[0] == "bash":
    record({"kind": "hold", "slot": slot})
    time.sleep(600)
elif request[0] == "pkill":
    record({"kind": "hold-released", "slot": slot})
elif len(request) > 1 and request[-1] == "stop":
    count = 1 + sum(1 for item in happened() if item["kind"] == "stop")
    record({"kind": "stop", "slot": slot, "count": count})
    if count >= config.get("stop_times_out_from", NEVER):
        time.sleep(600)
    if count >= config.get("stop_fails_from", NEVER):
        sys.exit(17)
    for item in happened():
        if item["kind"] == "worker" and item["slot"] == slot and alive(item["pid"]):
            try:
                os.killpg(item["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    record({"kind": "stop-worked", "slot": slot})
else:
    worker = subprocess.Popen(
        ["sleep", "600"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    record({"kind": "worker", "slot": slot, "pid": worker.pid})
    started = sum(1 for item in happened() if item["kind"] == "worker")
    if started <= config.get("drop_sessions", 0):
        sys.exit(0)  # the session ends out here; the work carries on in there
    worker.wait()
'''


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _events(root: Path) -> list[dict]:
    ledger = root / "events.jsonl"
    if not ledger.exists():
        return []
    out = []
    for line in ledger.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:  # a line still being written
            pass
    return out


def _of_kind(root: Path, kind: str) -> list[dict]:
    return [item for item in _events(root) if item["kind"] == kind]


def _workers_alive(root: Path) -> list[int]:
    return [
        item["pid"] for item in _of_kind(root, "worker") if _alive(item["pid"])
    ]


def _starts(root: Path) -> list[dict]:
    return [c for c in _of_kind(root, "call") if c["request"] == [BOOTSTRAP]]


def _stops(root: Path) -> list[dict]:
    return [c for c in _of_kind(root, "call") if c["request"] == [BOOTSTRAP, "stop"]]


def _configure(root: Path, **how: object) -> None:
    (root / "config.json").write_text(json.dumps(how))


def _settings(root: Path) -> dict[str, str]:
    """A whole, valid environment for the service, with the stand-in client."""
    return {
        "PATH": os.environ["PATH"],
        "SANDBOX_CLIENT": str(root / "sbx"),
        "SANDBOX_NAME": "a-throwaway-sandbox",
        "SANDBOX_BOOTSTRAP": BOOTSTRAP,
        "SANDBOX_BOOTSTRAP_STOP_ARGUMENT": "stop",
        "SANDBOX_RESTART_SECONDS": "0.2",
        "SANDBOX_STOP_TIMEOUT_SECONDS": "10",
        "SANDBOX_STOP_ATTEMPTS": "2",
        "SANDBOX_STOP_RETRY_SECONDS": "0.2",
        "SANDBOXES_STORAGE_ROOT": str(root / "client-state"),
    }


@pytest.fixture()
def root():
    """A throwaway folder with the stand-in client, a real socket, and cleanup.

    The socket is real and bound because the service checks for one at the exact
    path the client looks for under its storage root. On the way out, every
    process this test's fixtures started is ended, whatever the test did.
    """
    made = Path(tempfile.mkdtemp(prefix="sbxstop-"))
    client = made / "sbx"
    client.write_text(STAND_IN_CLIENT)
    client.chmod(0o755)
    _configure(made)
    socket_dir = made / "client-state" / "state" / "sandboxes" / "sandboxes" / "sandboxd"
    socket_dir.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_dir / "sandboxd.sock"))
    try:
        yield made
    finally:
        listener.close()
        for item in _of_kind(made, "worker"):
            try:
                os.killpg(item["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _wait_for(lambda: not _workers_alive(made), seconds=10)
        shutil.rmtree(made, ignore_errors=True)


def _seed_the_work_already_inside(root: Path) -> int:
    """A worker from an earlier life of the container, still running in there."""
    already = subprocess.Popen(
        ["sleep", "600"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ledger = root / "events.jsonl"
    with ledger.open("a") as handle:
        handle.write(
            json.dumps({"kind": "worker", "slot": "default", "pid": already.pid}) + "\n"
        )
    return already.pid


def _start(root: Path, **extra: str) -> subprocess.Popen:
    env = _settings(root)
    env.update(extra)
    # A session of its own, so that a service which will not go can be ended
    # together with the client sessions it is holding open. Without that, a
    # regressed script leaves a stand-in holding this pipe open and the test
    # waits for it rather than failing.
    return subprocess.Popen(
        ["bash", str(RUN_SH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait_for(predicate, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _finish(service: subprocess.Popen, seconds: float = 60) -> str:
    """Wait for the service to go, and end it and its sessions if it will not."""
    try:
        return service.communicate(timeout=seconds)[0]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(service.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            service.kill()
        try:
            return service.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:  # nothing left to read it from
            return ""


#: The one sentence the service says when it will not start on top of work that
#: may still be running.
THE_SENTENCE = "would not stop after"


class TestAFailedStopStartsNothing:
    """Codex's finding 1, as four cases: nothing starts on top of live work."""

    def test_a_failed_first_stop_starts_nothing_and_says_so(self, root: Path) -> None:
        _configure(root, stop_fails_from=1)
        already_inside = _seed_the_work_already_inside(root)

        service = _start(root)
        output = _finish(service)

        assert service.returncode != 0, (
            "a container that could not stop the work inside reported success"
        )
        assert service.returncode == 3
        assert THE_SENTENCE in output and "may still be running in there" in output
        assert not _starts(root), (
            f"it started a second supervisor on top of live work: {_events(root)}"
        )
        assert not _of_kind(root, "hold"), "it held the sandbox awake for nothing"
        assert _workers_alive(root) == [already_inside], (
            "the work already inside should be the only thing alive: "
            f"{_workers_alive(root)}"
        )

    def test_a_first_stop_that_never_finishes_starts_nothing(self, root: Path) -> None:
        _configure(root, stop_times_out_from=1)
        already_inside = _seed_the_work_already_inside(root)

        service = _start(root, SANDBOX_STOP_TIMEOUT_SECONDS="0.5")
        output = _finish(service)

        assert service.returncode == 3
        assert "did not finish within" in output
        assert THE_SENTENCE in output
        assert not _starts(root), (
            f"a stop that hung was followed by a start: {_events(root)}"
        )
        assert _workers_alive(root) == [already_inside]

    def test_a_failed_stop_after_a_dropped_session_opens_no_second_one(
        self, root: Path
    ) -> None:
        """The pile-up route: the session drops, the work stays, the stop fails."""
        _configure(root, drop_sessions=1, stop_fails_from=2)

        service = _start(root)
        assert _wait_for(lambda: len(_workers_alive(root)) == 1), (
            f"the bootstrap never ran: {_events(root)}"
        )
        still_inside = _workers_alive(root)[0]
        output = _finish(service)

        assert service.returncode == 3
        assert THE_SENTENCE in output
        assert len(_starts(root)) == 1, (
            "a second session was opened although the first would not stop: "
            f"{_events(root)}"
        )
        assert _workers_alive(root) == [still_inside]
        assert not _of_kind(root, "hold-released"), (
            "the hold was let go with the work still inside, so the sandbox "
            "would fall asleep on top of it"
        )

    def test_a_failed_stop_on_shutdown_never_claims_it_stopped(
        self, root: Path
    ) -> None:
        _configure(root, stop_fails_from=2)

        service = _start(root)
        assert _wait_for(lambda: len(_workers_alive(root)) == 1), (
            f"the bootstrap never ran: {_events(root)}"
        )
        still_inside = _workers_alive(root)[0]
        service.send_signal(signal.SIGTERM)
        output = _finish(service)

        assert service.returncode == 3, (
            "`docker compose stop` would have shown a clean exit with the work "
            "still running inside"
        )
        lines = output.splitlines()
        assert "[sandbox-runner] stopped" not in lines, (
            f"it said it had stopped and it had not: {lines}"
        )
        assert THE_SENTENCE in output
        assert _workers_alive(root) == [still_inside]
        assert not _of_kind(root, "hold-released"), (
            "the hold is left in place on purpose when the stop fails"
        )


class TestTheStopCarriesTheSettingsTheStartDid:
    """Codex's finding 2: the stop must be able to find what the start made."""

    def test_a_bootstrap_that_finds_its_work_by_a_setting_is_stopped(
        self, root: Path
    ) -> None:
        secret = "two words and a third"
        service = _start(
            root,
            SANDBOX_ENV_NAMES="BOOTSTRAP_SLOT A_SETTING_WITH_SPACES",
            BOOTSTRAP_SLOT="blue",
            A_SETTING_WITH_SPACES=secret,
        )
        assert _wait_for(lambda: len(_workers_alive(root)) == 1), (
            f"the bootstrap never ran: {_events(root)}"
        )
        in_the_blue_slot = _of_kind(root, "worker")[0]
        assert in_the_blue_slot["slot"] == "blue"

        service.send_signal(signal.SIGTERM)
        output = _finish(service)

        assert service.returncode == 0
        assert _wait_for(lambda: not _alive(in_the_blue_slot["pid"]), seconds=10), (
            "the worker the bootstrap had started is still running: the stop "
            "went in without the setting that identifies it, found nothing "
            f"and said it had succeeded. calls: {_of_kind(root, 'call')}"
        )
        for stop in _stops(root):
            assert stop["settings"] == {
                "BOOTSTRAP_SLOT": "blue",
                "A_SETTING_WITH_SPACES": secret,
            }, (
                "the stop must carry exactly the settings the start carried, "
                f"spaces and all: {stop['settings']}"
            )
        assert secret not in output, "a value was printed; only names may be"

    def test_the_stop_before_the_first_start_carries_them_too(
        self, root: Path
    ) -> None:
        """The stop that runs before anything starts is the same stop."""
        service = _start(
            root,
            SANDBOX_ENV_NAMES="BOOTSTRAP_SLOT",
            BOOTSTRAP_SLOT="blue",
        )
        try:
            assert _wait_for(lambda: bool(_starts(root)))
        finally:
            service.send_signal(signal.SIGTERM)
            _finish(service)

        first = _stops(root)[0]
        assert first["settings"] == {"BOOTSTRAP_SLOT": "blue"}


class TestTheOrdinaryPathStillWorks:
    def test_a_clean_shutdown_ends_the_work_and_lets_the_sandbox_sleep(
        self, root: Path
    ) -> None:
        service = _start(root)
        assert _wait_for(lambda: len(_workers_alive(root)) == 1)

        service.send_signal(signal.SIGTERM)
        output = _finish(service)

        assert service.returncode == 0
        assert "[sandbox-runner] stopped" in output.splitlines()
        assert _wait_for(lambda: not _workers_alive(root), seconds=10), (
            "the work inside outlived the container"
        )
        assert _of_kind(root, "hold-released"), "the sandbox was never let go"

    def test_sessions_that_keep_dropping_never_leave_two(self, root: Path) -> None:
        """Every replacement is preceded by a stop that really worked."""
        _configure(root, drop_sessions=3)

        service = _start(root)
        assert _wait_for(lambda: len(_starts(root)) >= 4), (
            f"the sessions were never opened again: {_events(root)}"
        )
        most_workers_seen = 0
        for _ in range(40):
            most_workers_seen = max(most_workers_seen, len(_workers_alive(root)))
            time.sleep(0.05)
        service.send_signal(signal.SIGTERM)
        output = _finish(service)

        assert service.returncode == 0
        assert most_workers_seen <= 1, (
            f"two supervisors were running inside at once: {_events(root)}"
        )
        assert len(_of_kind(root, "worker")) >= 4
        assert _wait_for(lambda: not _workers_alive(root), seconds=10), (
            f"work was left running inside: {output}"
        )
