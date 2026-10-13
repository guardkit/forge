"""Both factory containers in a sandbox start with an init process.

WHY THIS FILE EXISTS (13 October 2026, design
``factory-feature-check-repair-and-init-2026-10-13``, section C). The deploy
helper (``python``) and the build runner (``langgraph``) ran as PID 1 in their
containers, and neither collects the exit status of the orphaned processes it
inherits: on 13 October the runner held 106 zombies out of 111 processes, the
helper 9 out of 14. ``docker run --init`` puts Docker's own ``docker-init``
(tini) in front of each, which collects them.

One thing must stay exactly as it was for the helper: how it stops. As PID 1
with no SIGTERM handler the kernel dropped the SIGTERM ``docker stop`` sends,
so a stop waited its ten seconds, ended in SIGKILL, and reported 137, while the
deploy and merge commands the helper had started (each in a session of its
own) received no signal at all. Under tini the SIGTERM would reach the helper
and end it at once. So the helper's entry installs a do-nothing SIGTERM handler
before anything else. A HANDLER, not ``SIG_IGN``: an ignored signal stays
ignored across ``exec``, and every command the helper starts would then ignore
the SIGTERM of the helper's own polite stop of them; a handled one goes back to
the default in the new program.

The first three tests read the script; the rest run real processes (a
stand-in for ``serve`` behind the helper's real prelude). None needs Docker,
and nothing of the estate is touched.
"""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import forge.cli.deploy_templates as templates

#: The bootstrap under test, exactly as Forge ships it into a repository.
BOOTSTRAP = Path(templates.__file__).resolve().parent / "sandbox-runner.sh"

SIGTERM_BIT = 1 << (signal.SIGTERM - 1)


def _body_of(function: str) -> str:
    """A shell function's body, read as ``test_the_runner_start_runs_the_sweep_first`` does."""
    text = BOOTSTRAP.read_text()
    body = re.search(rf"^{function}\(\) \{{\n(.*?)^\}}\n", text, re.M | re.S)
    assert body, f"{function} is no longer defined in sandbox-runner.sh"
    return body.group(1)


def _docker_run_options(function: str) -> list[str]:
    """The option lines of the function's ``docker run``, up to the image reference."""
    body = _body_of(function)
    start = body.index('"${DOCKER}" run')
    end = body.index('"${IMAGE_REFERENCE}"', start)
    return [
        line.strip().rstrip("\\").strip()
        for line in body[start:end].splitlines()[1:]
        if line.strip()
    ]


def _helper_program() -> str:
    """The ``-c`` program the helper's container runs."""
    body = _body_of("start_helper")
    found = re.search(r"^\s*-c '([^']*)' \\$", body, re.M)
    assert found, "start_helper no longer runs a -c program"
    return found.group(1)


def _is_the_service_import(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.ImportFrom) and statement.module == (
        "forge.deploy_sidecar.service"
    )


def _is_the_sigterm_handler(statement: ast.stmt) -> bool:
    """``signal.signal(signal.SIGTERM, <a function>)``, and not SIG_IGN."""
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
        return False
    call = statement.value
    return (
        ast.unparse(call.func) == "signal.signal"
        and len(call.args) == 2
        and ast.unparse(call.args[0]) == "signal.SIGTERM"
        and isinstance(call.args[1], ast.Lambda)
    )


def _prelude(*, with_the_handler: bool = True) -> str:
    """Everything the helper's program does before it imports the service."""
    statements = ast.parse(_helper_program()).body
    cut = next(i for i, s in enumerate(statements) if _is_the_service_import(s))
    kept = [
        s for s in statements[:cut]
        if with_the_handler or not _is_the_sigterm_handler(s)
    ]
    return "\n".join(ast.unparse(s) for s in kept)


# --- what the script asks for ------------------------------------------------
@pytest.mark.parametrize("function", ["start_helper", "start_runner"])
def test_both_factory_containers_start_with_an_init_process(function):
    options = _docker_run_options(function)
    # A Docker option, before the image reference, not an argument handed to
    # the program; and straight after --no-healthcheck, where the design put it.
    assert "--init" in options, options
    assert options[options.index("--no-healthcheck") + 1] == "--init", options


def test_the_helper_entry_installs_its_sigterm_handler_first():
    statements = ast.parse(_helper_program()).body
    service = next(i for i, s in enumerate(statements) if _is_the_service_import(s))
    handlers = [i for i, s in enumerate(statements) if _is_the_sigterm_handler(s)]
    assert handlers, "the helper's program installs no SIGTERM handler"
    assert handlers[0] < service
    # Before anything else: only plain imports come before it.
    assert all(isinstance(s, ast.Import) for s in statements[: handlers[0]])
    # Never SIG_IGN, which its child commands would inherit.
    assert "SIG_IGN" not in _helper_program()


def test_the_comment_above_start_helper_says_why():
    text = BOOTSTRAP.read_text()
    above = text[: text.index("start_helper() {")].rsplit("\n\n", 1)[-1]
    assert "--init" in above
    assert "SIGTERM" in above and "SIG_IGN" in above


# --- the helper's real prelude, run as a real process -----------------------
#: What stands in for ``serve(...)``: start one long command in a session of
#: its own, as ``service.py`` starts a project's deploy and merge commands,
#: report it, and wait for ever, as a server does.
STAND_IN_FOR_SERVE = r'''
import subprocess, sys, threading, time
child = subprocess.Popen(["sleep", "300"], start_new_session=True)
def _watch():
    code = child.wait()
    with open(sys.argv[1] + ".ended", "w") as handle:
        handle.write(str(code))
threading.Thread(target=_watch, daemon=True).start()
with open(sys.argv[1], "w") as handle:
    handle.write(str(child.pid))
while True:
    time.sleep(3600)
'''


def _status_of(pid: int) -> dict[str, str]:
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def _alive(pid: int) -> bool:
    try:
        return _status_of(pid)["State"][0] not in "ZX"
    except (FileNotFoundError, ProcessLookupError):
        return False


def _start_the_helper(tmp_path: Path, *, with_the_handler: bool = True):
    program = _prelude(with_the_handler=with_the_handler) + "\n" + STAND_IN_FOR_SERVE
    report = tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, "-c", program, str(report)])
    for _ in range(100):
        if report.exists() and report.read_text():
            break
        time.sleep(0.05)
    else:  # pragma: no cover - a stuck stand-in
        parent.kill()
        pytest.fail("the stand-in helper never reported its child command")
    return parent, int(report.read_text()), report


def _end(parent, child: int) -> None:
    if parent.poll() is None:
        parent.kill()
        parent.wait(timeout=10)
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc")
def test_the_helper_survives_sigterm_and_its_child_commands_are_untouched(tmp_path):
    parent, child, report = _start_the_helper(tmp_path)
    try:
        # The helper itself HANDLES SIGTERM; it does not ignore it.
        mine = _status_of(parent.pid)
        assert int(mine["SigCgt"], 16) & SIGTERM_BIT
        assert not int(mine["SigIgn"], 16) & SIGTERM_BIT

        # SIGTERM, as tini passes on docker stop's: nothing happens.
        os.kill(parent.pid, signal.SIGTERM)
        time.sleep(1)
        assert parent.poll() is None, "the helper ended on SIGTERM"
        assert _alive(child), "the helper's command ended on the helper's SIGTERM"

        # Its command has the default SIGTERM disposition, neither ignored nor
        # caught, exactly as today.
        theirs = _status_of(child)
        assert not int(theirs["SigIgn"], 16) & SIGTERM_BIT
        assert not int(theirs["SigCgt"], 16) & SIGTERM_BIT

        # So the helper's own polite stop of its commands still works.
        os.kill(child, signal.SIGTERM)
        ended = Path(str(report) + ".ended")
        for _ in range(100):
            if ended.exists() and ended.read_text():
                break
            time.sleep(0.05)
        assert ended.read_text() == str(-signal.SIGTERM)

        # And SIGKILL, as docker stop sends after its ten seconds, ends it.
        os.kill(parent.pid, signal.SIGKILL)
        assert parent.wait(timeout=10) == -signal.SIGKILL
    finally:
        _end(parent, child)


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc")
def test_without_the_handler_sigterm_ends_the_helper_at_once(tmp_path):
    """Control: what tini would do to the helper without the handler."""
    parent, child, _ = _start_the_helper(tmp_path, with_the_handler=False)
    try:
        os.kill(parent.pid, signal.SIGTERM)
        assert parent.wait(timeout=5) == -signal.SIGTERM
        # Its command, in a session of its own, still received nothing.
        assert _alive(child)
    finally:
        _end(parent, child)
