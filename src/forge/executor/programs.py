"""The one way the merge press's legs start a program and read what it did.

15 October 2026, after six review rounds on the merge press's publication
hold. Each backend used to start its program its own way and sort what
happened its own way, and each one, sooner or later, called an error that
came AFTER the program had started a refusal — so the press put its hold
down while something it had started might still be running. This module
draws the one line every backend now draws in the same place:

* the operating system refused to START the program (any ``OSError`` raised
  by the spawn itself): nothing ran — :class:`RefusedBeforeStart`, an answer;
* the program was stopped at its time limit — :class:`TimedOut`;
* anything raised AFTER the program started (reading its output, waiting for
  it, stopping it) — :class:`RaisedWhileRunning`.

The last two are read like an answer that was lost: what the program started
may still be running. Every backend the press's legs use goes through
:func:`start_program` (or :func:`start_program_async`), most of them through
:func:`run_program`; a test pins that none of them calls ``subprocess``
directly.

The three results are tuples, so they unpack exactly like the plain
``(exit code, output…)`` tuple each backend has always answered with, and a
caller that does not ask is unchanged. Each says what it is in a field of its
own, never in its words.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "ProgramOutcome",
    "ProgramRefused",
    "RaisedWhileRunning",
    "RefusedBeforeStart",
    "TimedOut",
    "run_program",
    "start_program",
    "start_program_async",
]


class ProgramRefused(OSError):
    """The operating system refused to start the program; nothing ran.

    An ``OSError``, so every place that caught the spawn's own error before
    still catches it. The original error is its ``__cause__``.
    """


class _Marked(tuple):
    """A result tuple that says what it is in fields of its own.

    Built from the items (``Marked(124, "out")``) or from one tuple of them
    (``Marked((124, "out", "err"))``), so a backend answering two items and one
    answering three both use it. Copies and pickles keep the class.
    """

    refused_before_running = False
    timed_out = False
    answer_lost = False

    def __new__(cls, *items: Any) -> "_Marked":
        if len(items) == 1 and isinstance(items[0], tuple):
            items = tuple(items[0])
        return super().__new__(cls, items)

    def __getnewargs__(self) -> tuple[Any, ...]:
        return tuple(self)


class RefusedBeforeStart(_Marked):
    """The program was never started: an answer, nothing is running."""

    refused_before_running = True


class TimedOut(_Marked):
    """The program was stopped at its time limit; what it started may still
    be running, so it is read like an answer that was lost."""

    timed_out = True
    answer_lost = True


class RaisedWhileRunning(_Marked):
    """Something raised after the program started; nobody knows whether what
    it started is still running, so it is read like an answer that was lost."""

    answer_lost = True


@dataclass(frozen=True)
class ProgramOutcome:
    """What :func:`run_program` saw. Exactly one of the three flags, or none
    for a program that ran and exited on its own."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    refused_before_start: bool = False
    timed_out: bool = False
    raised_while_running: bool = False
    #: The spawn's own error, or what was raised while it ran.
    error: BaseException | None = None


def start_program(argv: Sequence[str], **popen_kwargs: Any) -> "subprocess.Popen[bytes]":
    """Start ``argv``; raise :class:`ProgramRefused` if it could not be started.

    The spawn is the only thing inside the ``try``: an error here means the
    operating system refused, and nothing ran.
    """
    try:
        return subprocess.Popen(list(argv), **popen_kwargs)  # noqa: S603 — no shell
    except OSError as exc:
        raise ProgramRefused(exc.errno, str(exc)) from exc


async def start_program_async(*argv: str, **kwargs: Any) -> Any:
    """:func:`start_program`, for an asyncio caller."""
    try:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    except OSError as exc:
        raise ProgramRefused(exc.errno, str(exc)) from exc


def _stop(process: "subprocess.Popen[bytes]", *, group: bool, grace: float) -> None:
    """Stop the program (and its whole group when it leads one). Never raises."""
    if group:
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = None
        if pgid is not None and pgid != os.getpgrp():
            for sig, wait_for in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.0)):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    return
                if wait_for > 0:
                    deadline = time.monotonic() + wait_for
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
            return
    try:
        process.kill()
    except OSError:
        return


def run_program(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float,
    new_session: bool = True,
    kill_grace_seconds: float = 2.0,
    post_kill_read_seconds: float = 5.0,
    stdin: Any = subprocess.DEVNULL,
) -> ProgramOutcome:
    """Start ``argv``, wait for it under ``timeout``, and read its output.

    Never raises. A program that could not be started is
    ``refused_before_start``; one stopped at its limit is ``timed_out`` (its
    group stopped too when ``new_session``); anything raised once it had
    started is ``raised_while_running``, after the program is stopped.
    """
    try:
        process = start_program(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=new_session,
        )
    except ProgramRefused as exc:
        return ProgramOutcome(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            refused_before_start=True,
            error=exc.__cause__ or exc,
        )
    try:
        out, err = process.communicate(timeout=timeout)
        return ProgramOutcome(exit_code=process.returncode, stdout=out or b"", stderr=err or b"")
    except subprocess.TimeoutExpired as first:
        _stop(process, group=new_session, grace=kill_grace_seconds)
        # What was read before the limit rides the exception; the read after
        # the stop is bounded, because a child that outlived the stop can
        # hold the pipe open for ever.
        try:
            out, err = process.communicate(timeout=post_kill_read_seconds)
        except subprocess.TimeoutExpired as second:
            out = second.stdout or first.stdout or b""
            err = second.stderr or first.stderr or b""
        except Exception:  # noqa: BLE001 — the tails are surrendered, not waited for
            out, err = first.stdout or b"", first.stderr or b""
        return ProgramOutcome(
            exit_code=process.returncode,
            stdout=out or b"",
            stderr=err or b"",
            timed_out=True,
        )
    except BaseException as exc:  # noqa: BLE001 — after the start: nobody knows
        _stop(process, group=new_session, grace=kill_grace_seconds)
        if not isinstance(exc, Exception):
            raise
        return ProgramOutcome(
            exit_code=process.returncode,
            stdout=b"",
            stderr=b"",
            raised_while_running=True,
            error=exc,
        )
