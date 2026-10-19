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
import select
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
#   never reaches them. The program runs under a subreaper wrapper, so all it
#   starts stays under the wrapper even when a parent in between exits; every
#   process under the wrapper is found in ``/proc``, held by a pidfd, and
#   signalled through it (see "the descendants are held by pidfd" and "the
#   subreaper wrapper" below).
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


# THE DESCENDANTS ARE HELD BY PIDFD, NEVER BY NUMBER (Codex round 2, 19
# October 2026). A saved process or group id can be reused by an unrelated
# process; a pidfd always refers to the one process it was opened for. The
# leader is the exception: it is unreaped for the whole stop, so its id and
# its group's id are still its own, and its group is signalled by number.
#
# A child is taken as ours only if, after its pidfd is open, its parent is
# still the parent it was found under, and that parent is itself still
# pinned (the unreaped leader, or a process whose pidfd shows it has not
# exited). A pid read from /proc that has meanwhile become someone else's
# fails that check and is never signalled.

#: The two Linux system calls, by their numbers, which are the same on every
#: architecture the factory runs on. Used only when this Python lacks
#: ``os.pidfd_open`` / ``signal.pidfd_send_signal`` (uv's standalone 3.14
#: build for aarch64 has neither).
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434
_libc: Any = None


def _syscall(*args: int | None) -> int:
    global _libc
    import ctypes

    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    result = _libc.syscall(
        *(ctypes.c_long(a) if a is not None else None for a in args)
    )
    if result < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return int(result)


def _pidfd_open(pid: int) -> int:
    # (Attribute access, never the module object handed on: the launch check
    # in the test suite reads every reference to ``os``.)
    try:
        opener = os.pidfd_open
    except AttributeError:
        return _syscall(_SYS_PIDFD_OPEN, pid, 0)
    return opener(pid)


def _pidfd_send_signal(fd: int, sig: int) -> None:
    try:
        sender = signal.pidfd_send_signal
    except AttributeError:
        _syscall(_SYS_PIDFD_SEND_SIGNAL, fd, int(sig), None, 0)
        return
    sender(fd, sig)


def _pidfd_exited(fd: int) -> bool:
    """Has the process this pidfd refers to ended? (Readable once it has.)"""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def _parent_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", errors="replace")
        return int(raw.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _children_of(pid: int) -> list[int]:
    """The ids /proc gives as ``pid``'s children (each still to be checked).

    ``/proc/<pid>/task/*/children`` where the kernel has it; otherwise every
    process whose parent id is ``pid``.
    """
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    found: list[int] = []
    try:
        for task in tasks:
            with open(f"/proc/{pid}/task/{task}/children", "rb") as handle:
                found.extend(int(part) for part in handle.read().split())
        return found
    except FileNotFoundError:
        pass  # this kernel has no children files
    except (OSError, ValueError):
        return found
    table = _process_table() or {}
    return [child for child, (ppid, _pgid, _state) in table.items() if ppid == pid]


class _Descendants:
    """Pidfds for the leader's descendants, each checked to be ours."""

    def __init__(self, leader: int) -> None:
        self.leader = leader
        self.fds: dict[int, int] = {}

    def collect(self) -> None:
        """Walk from the leader and from every held process still running,
        and hold a pidfd for each new descendant that checks out."""
        todo: list[tuple[int, int | None]] = [(self.leader, None)]
        todo += [(pid, fd) for pid, fd in self.fds.items() if not _pidfd_exited(fd)]
        seen: set[int] = set()
        while todo:
            parent, parent_fd = todo.pop()
            if parent in seen:
                continue
            seen.add(parent)
            for child in _children_of(parent):
                if child == self.leader or child == os.getpid():
                    continue
                held = self.fds.get(child)
                if held is not None and not _pidfd_exited(held):
                    todo.append((child, held))
                    continue
                fd = self._open_if_ours(child, parent, parent_fd)
                if fd is None:
                    continue
                if held is not None:
                    os.close(held)
                self.fds[child] = fd
                todo.append((child, fd))

    @staticmethod
    def _open_if_ours(child: int, parent: int, parent_fd: int | None) -> int | None:
        try:
            fd = _pidfd_open(child)
        except OSError:
            return None  # it ended already
        ours = (
            _parent_of(child) == parent
            and (parent_fd is None or not _pidfd_exited(parent_fd))
            and not _pidfd_exited(fd)
        )
        if not ours:
            os.close(fd)
            return None
        return fd

    def running(self) -> list[int]:
        return [fd for fd in self.fds.values() if not _pidfd_exited(fd)]

    def signal(self, sig: int) -> bool:
        sent = False
        for fd in self.running():
            try:
                _pidfd_send_signal(fd, sig)
            except OSError:
                continue
            sent = True
        return sent

    def forget_exited(self) -> None:
        """Close the pidfds of processes that have ended."""
        for pid, fd in list(self.fds.items()):
            if _pidfd_exited(fd):
                os.close(fd)
                del self.fds[pid]

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


#: How long the stop's drain may go on killing what is under the wrapper.
_DRAIN_SECONDS: float = 10.0

#: How long one drain pass waits for what it killed to end.
_DRAIN_PASS_WAIT_SECONDS: float = 2.0


class StoppableRun:
    """One program :func:`run_program` starts inside :func:`stoppable`, which
    another thread may stop with :meth:`request_stop`. See the block above."""

    def __init__(self, label: str = "") -> None:
        #: Names this run in the stop's log lines (the helper: which leg).
        self.label = label
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
        """The stop itself, under the lock from the first signal to the last.

        The process held is the subreaper wrapper (see :data:`_SUBREAPER`),
        unreaped throughout. Everything under it — the program and every
        process in its tree, re-parented orphans included — is held by pidfd
        and sent TERM; during the grace the walk is repeated from the wrapper,
        so a process started or orphaned during the grace is found too. Then
        the drain (:meth:`_drain`) KILLs everything under the wrapper, pass
        after pass, until a scan finds nothing; only then is the wrapper
        itself KILLed. The wrapper is never sent TERM: it is first sent the
        hold signal, so it stays, reaping, until that KILL. ``True`` when
        anything was signalled.
        """
        with self._lock:
            process = self._process
            if process is None or self._finished or self._stopped:
                return False
            self._stopped = True
            descendants = _Descendants(process.pid)
            signalled = False
            try:
                if not _exited_unreaped(process.pid):
                    # Hold the wrapper first (it is unreaped: the id is its own).
                    try:
                        os.kill(process.pid, _SUBREAPER_HOLD_SIGNAL)
                    except OSError:
                        pass
                descendants.collect()
                logger.warning(
                    "stopping pid %s and the %d processes under it",
                    process.pid,
                    len(descendants.fds),
                )
                signalled = descendants.signal(signal.SIGTERM)
                deadline = time.monotonic() + max(0.0, self._grace)
                ticks = 0
                while time.monotonic() < deadline:
                    ticks += 1
                    if ticks % 5 == 0:
                        descendants.collect()
                    if not descendants.running():
                        break
                    time.sleep(0.05)
                # THE DRAIN (Codex round 5). Under the same lock, the wrapper
                # held and unreaped: scan everything under it, KILL each one
                # through its pidfd, wait until each has exited, scan again.
                # A killed process cannot fork once the signal lands, so each
                # pass sees only what was forked just before its parent died;
                # the loop ends when a scan finds nothing (bounded).
                signalled = self._drain(descendants) or signalled
                if not _exited_unreaped(process.pid):
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                        signalled = True
                    except OSError:
                        pass
            finally:
                descendants.close()
            return signalled

    def _drain(self, descendants: "_Descendants") -> bool:
        """KILL everything under the wrapper until a scan finds nothing."""
        signalled = False
        until = time.monotonic() + _DRAIN_SECONDS
        while True:
            descendants.collect()
            found = descendants.running()
            if not found:
                return signalled
            if time.monotonic() >= until:
                logger.warning(
                    "stopping %s: %d processes under it are still there after "
                    "%g seconds of killing; going on",
                    self.label or "a stoppable program",
                    len(found),
                    _DRAIN_SECONDS,
                )
                return signalled
            signalled = descendants.signal(signal.SIGKILL) or signalled
            wait_until = min(until, time.monotonic() + _DRAIN_PASS_WAIT_SECONDS)
            while time.monotonic() < wait_until and not all(
                _pidfd_exited(fd) for fd in found
            ):
                time.sleep(0.01)
            descendants.forget_exited()

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


#: THE SUBREAPER WRAPPER (Codex round 3, 19 October 2026). A stoppable
#: program is started under this small Python program, which marks itself a
#: Linux child subreaper (prctl PR_SET_CHILD_SUBREAPER, 36 on every
#: architecture the factory runs on) and then starts the program as its
#: child. A process anywhere in the program's tree whose parent exits is then
#: re-parented to the wrapper, never to init, so everything the program
#: started always stays under the wrapper, where a stop can find it. When the
#: program exits, the wrapper records its status and stays, reaping, until it
#: has no child left (Codex round 4): a child the program left running keeps
#: the leg running, under the wrapper, until it ends or a stop or the time
#: limit stops it. Then the wrapper exits with the program's own exit code
#: (dying of the same signal when the program did), so exit codes and output
#: are what they were.
#:
#: A stop first sends it :data:`_SUBREAPER_HOLD_SIGNAL`; a held wrapper does
#: not exit when the program does, but stays (reaping) until the stop's final
#: KILL, so a process orphaned during the stop is still re-parented to it. Its
#: handler is installed before it says it has started, so the signal never
#: kills it. A program that is never stopped never sees any of this.
#:
#: It writes one line on a status pipe of its own: ``started <pid> <subreaper
#: or no-subreaper>``, or ``refused <errno> <reason>`` when the program could
#: not be started, which is then reported as refused before it started,
#: exactly as before. It runs with ``-I``, so nothing in the program's
#: environment or working directory changes what it imports.
_SUBREAPER = r"""
import ctypes, os, signal, sys, time
status = int(sys.argv[1])
hold_signal = int(sys.argv[2])
argv = sys.argv[4:]
held = []
signal.signal(hold_signal, lambda *_: held.append(True))
reaper = "no-subreaper"
try:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(ctypes.c_int(36), ctypes.c_ulong(1), ctypes.c_ulong(0),
                  ctypes.c_ulong(0), ctypes.c_ulong(0)) == 0:
        reaper = "subreaper"
except Exception:
    pass
os.set_inheritable(status, False)
defaults = [getattr(signal, n) for n in ("SIGPIPE", "SIGXFZ", "SIGXFSZ") if hasattr(signal, n)]
try:
    child = os.posix_spawnp(argv[0], argv, os.environ, setsigdef=defaults)
except OSError as exc:
    os.write(status, ("refused %d %s" % (exc.errno or 0, exc.strerror or exc)).encode())
    os._exit(127)
os.write(status, ("started %d %s" % (child, reaper)).encode())
os.close(status)
while True:
    pid, how = os.wait()
    if pid == child:
        break
# The program's own status is recorded above. As a subreaper every orphan of
# its tree is a child of this wrapper, so it stays, reaping, until none is
# left: whatever the program left running is still under it, for a stop or
# the time limit to reach.
while True:
    try:
        os.wait()
    except ChildProcessError:
        break
while held:
    # A stop is under way: stay, so everything orphaned during it is still
    # re-parented here, until the stop's final KILL.
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        pass
    time.sleep(0.05)
code = os.waitstatus_to_exitcode(how)
if code < 0:
    try:
        signal.signal(-code, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), -code)
    os._exit(128 - code)
os._exit(code)
"""

#: What a stop sends the wrapper first, to keep it until the final KILL.
_SUBREAPER_HOLD_SIGNAL: int = int(signal.SIGRTMIN) + 3

#: How long the parent waits for the wrapper's status line.
_SUBREAPER_STATUS_SECONDS = 30.0


def _subreaper_argv(argv: Sequence[str], status_write: int) -> list[str]:
    """``argv`` run under the wrapper, which reports on ``status_write``."""
    import sys

    return [
        sys.executable,
        "-I",
        "-c",
        _SUBREAPER,
        str(status_write),
        str(_SUBREAPER_HOLD_SIGNAL),
        "--",
        *argv,
    ]


def _await_subreaper(process: "subprocess.Popen[bytes]", status_read: int) -> None:
    """Read the wrapper's status line; raise :class:`ProgramRefused` (nothing
    is running) when it says the program could not be started."""
    said = b""
    try:
        poller = select.poll()
        poller.register(status_read, select.POLLIN | select.POLLHUP)
        until = time.monotonic() + _SUBREAPER_STATUS_SECONDS
        while time.monotonic() < until:
            if not poller.poll(100):
                continue
            chunk = os.read(status_read, 4096)
            if not chunk:
                break
            said += chunk
    except OSError as exc:  # unread: it is running, and is waited for as such
        said = f"unread status: {exc}".encode()
    words = said.decode("utf-8", errors="replace").split(" ", 2)
    if words[0] == "refused":
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        number = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
        reason = words[2] if len(words) > 2 else "the program could not be started"
        raise ProgramRefused(number, reason) from OSError(number, reason)
    if words[0] != "started" or "no-subreaper" in words[-1]:
        logger.warning(
            "the stoppable program's wrapper said %r: what it starts may not all "
            "stay under it",
            said[:200],
        )


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
    # A stoppable program runs under the subreaper wrapper (see _SUBREAPER),
    # which reports whether it started on a pipe of its own.
    status_pipe = os.pipe() if run is not None else None
    try:
        process = start_program(
            _subreaper_argv(argv, status_pipe[1]) if status_pipe else argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True if status_pipe else new_session,
            pass_fds=(status_pipe[1],) if status_pipe else (),
        )
        if status_pipe:
            os.close(status_pipe[1])
            status_pipe = (status_pipe[0], -1)
            _await_subreaper(process, status_pipe[0])
    except ProgramRefused as exc:
        return ProgramOutcome(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            refused_before_start=True,
            error=exc.__cause__ or exc,
        )
    finally:
        for end in status_pipe or ():
            if end >= 0:
                os.close(end)
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
