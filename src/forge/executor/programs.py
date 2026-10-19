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
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "Finished",
    "ProgramOutcome",
    "ProgramRefused",
    "RaisedWhileRunning",
    "RefusedBeforeStart",
    "TimedOut",
    "StoppableRun",
    "run_program",
    "start_program",
    "start_program_async",
    "stoppable",
]

logger = logging.getLogger(__name__)

#: The run, on this thread, the next program :func:`run_program` starts
#: belongs to (see :func:`stoppable`).
_started_observer = threading.local()


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
    #: True only for a complete answer (15 October 2026): the program ran to
    #: an end, or was refused before it started. Every other result — and any
    #: result a caller built without saying — is unconfirmed.
    confirmed = False

    def __new__(cls, *items: Any) -> "_Marked":
        if len(items) == 1 and isinstance(items[0], tuple):
            items = tuple(items[0])
        return super().__new__(cls, items)

    def __getnewargs__(self) -> tuple[Any, ...]:
        return tuple(self)


class RefusedBeforeStart(_Marked):
    """The program was never started: an answer, nothing is running."""

    refused_before_running = True
    confirmed = True


class Finished(_Marked):
    """The program ran to an end and said how (or a request was refused in
    words before anything ran): a complete answer."""

    confirmed = True


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


# ---------------------------------------------------------------------------
# A program that another thread may stop (19 October 2026)
# ---------------------------------------------------------------------------
#
# The helper's ``/guardkit-leg/cancel`` stops a fix journey's leg from another
# request's thread. Three rules make that safe and complete:
#
# * NEVER SIGNAL A REAPED PROCESS. Once a process is reaped its id, and the
#   id of the group it led, can be given to a new process. So the thread that
#   runs the program observes its end WITHOUT reaping it
#   (``waitid(..., WNOWAIT)``), marks the run finished under the run's lock,
#   and only then reaps it. A stop takes the same lock, refuses once the run
#   is finished, and holds the lock from the first signal to the last, so the
#   leader stays unreaped (a zombie keeps its id and its group's id reserved)
#   for the whole of the stop.
# * EVERYTHING IT STARTED, NOT ONLY ITS GROUP. GuardKit starts the project's
#   own test commands in sessions of their own, so a signal to the leg's group
#   never reaches them. Before the first signal, while the leader is alive or
#   unreaped, every descendant of the leader is found in ``/proc`` and each
#   distinct process group among them is stopped (never this process's own).
# * A STOP ASKED FOR BEFORE THE START means the program is never started; one
#   that lands between the check and the start stops it at once.


def _process_table() -> dict[int, tuple[int, int, str]] | None:
    """``{pid: (parent pid, process group, state)}`` from ``/proc``, or None."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    table: dict[int, tuple[int, int, str]] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                raw = handle.read().decode("utf-8", errors="replace")
            fields = raw.rsplit(")", 1)[1].split()
            table[int(entry)] = (int(fields[1]), int(fields[2]), fields[0])
        except (OSError, IndexError, ValueError):
            continue  # it ended while the table was read
    return table


def _groups_to_stop(leader: int) -> set[int]:
    """The leader's group and the group of every one of its descendants."""
    groups: set[int] = set()
    try:
        groups.add(os.getpgid(leader))
    except OSError:
        pass
    table = _process_table()
    if table is not None:
        children: dict[int, list[int]] = {}
        for pid, (ppid, _pgid, _state) in table.items():
            children.setdefault(ppid, []).append(pid)
        seen: set[int] = set()
        todo = [leader]
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if pid in table:
                groups.add(table[pid][1])
            todo.extend(children.get(pid, ()))
    groups.discard(0)
    groups.discard(os.getpgrp())
    return groups


def _groups_still_running(groups: set[int]) -> set[int]:
    """Which of ``groups`` still have a member that is not a zombie."""
    table = _process_table()
    if table is None:
        remaining = set()
        for group in groups:
            try:
                os.killpg(group, 0)
            except OSError:
                continue
            remaining.add(group)
        return remaining
    live = {pgid for _ppid, pgid, state in table.values() if state != "Z"}
    return groups & live


class StoppableRun:
    """One program :func:`run_program` starts inside :func:`stoppable`, which
    another thread may stop with :meth:`request_stop`. See the block above."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: "subprocess.Popen[bytes] | None" = None
        self._finished = False
        self._stop_requested = False
        self._stopped = False
        self._grace = 2.0
        #: Test hooks: called on the running thread just before and just
        #: after the run is marked finished (the leader exited, unreaped).
        self._before_finish_mark: "Callable[[], None] | None" = None
        self._after_finish_mark: "Callable[[], None] | None" = None

    # The three readings take no lock: each is one attribute read, and a stop
    # holds the lock for its whole grace, which a reader (``GET /busy``) must
    # never wait behind.

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def finished(self) -> bool:
        return self._finished

    def request_stop(self, *, grace: float) -> bool:
        """Ask for the program and everything it started to be stopped.

        Before the start: it will not be started. Running: TERM to every
        group, KILL to those still running after ``grace``; this returns
        once that is done. Finished: nothing is signalled. ``True`` when
        this call signalled something, or will stop the program at its start.
        """
        with self._lock:
            first = not self._stop_requested
            self._stop_requested = True
            self._grace = grace
            if self._process is None:
                return first and not self._finished
        return self._stop_now()

    def may_start(self) -> bool:
        with self._lock:
            return not self._stop_requested

    def _attach(self, process: "subprocess.Popen[bytes]") -> None:
        with self._lock:
            self._process = process
            stop = self._stop_requested
        if stop:
            self._stop_now()

    def _stop_now(self) -> bool:
        """The stop itself, under the lock from the first signal to the last."""
        with self._lock:
            process = self._process
            if process is None or self._finished or self._stopped:
                return False
            self._stopped = True
            groups = _groups_to_stop(process.pid)
            logger.warning(
                "stopping pid %s and everything it started: process groups %s",
                process.pid,
                sorted(groups),
            )
            for group in groups:
                try:
                    os.killpg(group, signal.SIGTERM)
                except OSError:
                    continue
            deadline = time.monotonic() + max(0.0, self._grace)
            remaining = _groups_still_running(groups)
            while remaining and time.monotonic() < deadline:
                time.sleep(0.05)
                remaining = _groups_still_running(groups)
            # The KILL is sent under the same lock, so the run cannot have
            # been marked finished, and its leader cannot have been reaped.
            for group in remaining:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    continue
            return bool(groups)

    def _mark_finished(self) -> None:
        if self._before_finish_mark is not None:
            self._before_finish_mark()
        with self._lock:
            self._finished = True
        if self._after_finish_mark is not None:
            self._after_finish_mark()


@contextlib.contextmanager
def stoppable(run: StoppableRun) -> Iterator[None]:
    """Run the next program :func:`run_program` starts on this thread as
    ``run``, so another thread can stop it with ``run.request_stop``.

    The runners that start a leg keep their signatures; the leg's handler
    thread says which run it is.
    """
    previous = getattr(_started_observer, "run", None)
    _started_observer.run = run
    try:
        yield
    finally:
        _started_observer.run = previous


def _exited_unreaped(pid: int) -> bool:
    """Has the child ``pid`` exited? It is left unreaped either way."""
    result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    return result is not None and result.si_pid == pid


def _reader(stream: Any, into: list[bytes]) -> threading.Thread:
    def read() -> None:
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                into.append(chunk)
        except (OSError, ValueError):
            return

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def _run_stoppable(
    process: "subprocess.Popen[bytes]",
    run: StoppableRun,
    *,
    timeout: float,
    kill_grace_seconds: float,
    post_kill_read_seconds: float,
) -> ProgramOutcome:
    """Wait for a program started under :func:`stoppable`; never reap early.

    The same three endings as :func:`run_program`'s own wait: the program
    exited (and its output reached its end), it was stopped at ``timeout``,
    or something raised after the start. Its end is observed without reaping
    it, the run is marked finished, and only then is it reaped.
    """
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [_reader(process.stdout, out), _reader(process.stderr, err)]
    timed_out = False
    try:
        run._attach(process)
        deadline = time.monotonic() + timeout
        while True:
            if _exited_unreaped(process.pid) and not any(r.is_alive() for r in readers):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        if timed_out:
            with run._lock:
                run._grace = kill_grace_seconds
            run._stop_now()
            until = time.monotonic() + post_kill_read_seconds
            while time.monotonic() < until:
                if _exited_unreaped(process.pid) and not any(r.is_alive() for r in readers):
                    break
                time.sleep(0.05)
        run._mark_finished()
        try:
            process.wait(timeout=post_kill_read_seconds if timed_out else None)
        except subprocess.TimeoutExpired:
            pass
        return ProgramOutcome(
            exit_code=process.returncode,
            stdout=b"".join(out),
            stderr=b"".join(err),
            timed_out=timed_out,
        )
    except BaseException as exc:  # noqa: BLE001 — after the start: nobody knows
        if not isinstance(exc, ChildProcessError):
            # (A ChildProcessError means something else reaped it: its ids
            # may already be reused, so nothing is signalled.)
            with run._lock:
                run._grace = kill_grace_seconds
            run._stop_now()
        run._mark_finished()
        if not isinstance(exc, Exception):
            raise
        return ProgramOutcome(
            exit_code=process.returncode,
            stdout=b"",
            stderr=b"",
            raised_while_running=True,
            error=exc,
        )


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
    run: StoppableRun | None = getattr(_started_observer, "run", None)
    if run is not None:
        # Taken once: the next program on this thread is the stoppable one.
        _started_observer.run = None
        if not run.may_start():
            return ProgramOutcome(
                exit_code=None,
                stdout=b"",
                stderr=b"",
                refused_before_start=True,
                error=ProgramRefused(
                    "it was asked to stop before it started, so it was not started"
                ),
            )
    try:
        process = start_program(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True if run is not None else new_session,
        )
    except ProgramRefused as exc:
        return ProgramOutcome(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            refused_before_start=True,
            error=exc.__cause__ or exc,
        )
    if run is not None:
        return _run_stoppable(
            process,
            run,
            timeout=timeout,
            kill_grace_seconds=kill_grace_seconds,
            post_kill_read_seconds=post_kill_read_seconds,
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
