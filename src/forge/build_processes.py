"""The processes a build owns, and stopping all of them.

Until 3 October 2026 a cancelled, timed-out or stuck build was stopped by
sending SIGKILL to ONE process: the ``guardkit`` child the runner spawned. That
child shared the runner's process group, and everything it had started — test
runners, agents, a served product, its fixtures — carried on, still writing
into the build's tree and still talking to the model, while the factory
counted the build's place as free. The design of that date ("Stopping a
cancelled or timed-out build") says what a build owns and how it is stopped;
this module is that, and nothing else.

WHICH PROCESSES A BUILD OWNS. A process group is not enough: GuardKit's serve
probe deliberately starts a session of its own. So ownership is read from
``/proc`` at the moment the stop begins:

* the build child's whole process tree, by parent links;
* any process whose environment carries the build's owner marker
  (``GUARDKIT_RUN_OWNER=<build id>``) — which catches a process orphaned
  earlier, whose parent link to the build is gone;
* the descendants of either, and any process in a group or session led by an
  owned process.

Each is recorded as a (process ID, start time) pair, so a process number the
kernel has since handed to something else is never mistaken for an owned one.

THE STOP. SIGTERM every owned process group (and the processes themselves),
wait up to a grace period, read the tree and the markers again (catching
anything started during the grace), SIGKILL every owned process and group,
then confirm every recorded pair is gone. A zombie counts as gone: it holds no
files, no connections and no CPU, and in a container whose first process is
the runner nothing else will reap it.

``still_running`` is the stateless question the factory asks before it lets a
cancelled build's place go: is any process carrying this build's marker, or
any fixture container carrying its label, still alive here? An engine that
cannot be asked is "cannot confirm", never "stopped".

This module also holds the one copy of :func:`signal_process_group`, which the
one-shot GuardKit adapter (``adapters/guardkit/run.py``) uses for its own
timeout and cancel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "FIXTURE_OWNER_LABEL",
    "OWNER_MARKER",
    "OwnedBuild",
    "OwnedProcess",
    "StopReport",
    "build_stop_grace_seconds",
    "find_owned",
    "lookup",
    "register",
    "remove_fixture_containers",
    "signal_process_group",
    "still_running",
    "stop_build",
    "stop_owned",
    "stop_pending",
    "forget",
    "unregister",
]

#: The environment entry every process a build starts carries. Set per build by
#: the launch (``forge.launch_environment``), never inherited.
OWNER_MARKER: str = "GUARDKIT_RUN_OWNER"

#: The label GuardKit puts on every fixture container it starts for a build.
FIXTURE_OWNER_LABEL: str = "guardkit.fixture.owner"

#: How long owned processes are given to go after SIGTERM before SIGKILL.
DEFAULT_GRACE_SECONDS: float = 10.0

#: The setting that shortens or lengthens that grace (seconds).
BUILD_STOP_GRACE_ENV: str = "FORGE_BUILD_STOP_GRACE_SECONDS"

#: How long a SIGKILLed process is given to disappear before it is reported.
_KILL_CONFIRM_SECONDS: float = 5.0

#: How often ``/proc`` is read while waiting.
_POLL_SECONDS: float = 0.1

#: The engine's own ceiling for one fixture question.
_ENGINE_TIMEOUT_SECONDS: float = 30.0


def build_stop_grace_seconds() -> float:
    """The SIGTERM grace, from :data:`BUILD_STOP_GRACE_ENV` or the default."""
    raw = os.environ.get(BUILD_STOP_GRACE_ENV, "").strip()
    if not raw:
        return DEFAULT_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "build_processes: %s=%r is not a number; using %ss",
            BUILD_STOP_GRACE_ENV,
            raw,
            DEFAULT_GRACE_SECONDS,
        )
        return DEFAULT_GRACE_SECONDS
    return max(value, 0.0)


# ---------------------------------------------------------------------------
# Reading /proc
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnedProcess:
    """One owned process: its number and the kernel's start time for it."""

    pid: int
    starttime: int


@dataclass(frozen=True, slots=True)
class _Stat:
    pid: int
    state: str
    ppid: int
    pgid: int
    sid: int
    starttime: int


def _read_stat(pid: int) -> _Stat | None:
    """Parse ``/proc/<pid>/stat``; ``None`` when the process is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # The command name is in parentheses and may itself contain ')' or
    # spaces, so the fields are read after the LAST ')'.
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    try:
        return _Stat(
            pid=pid,
            state=fields[0],
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            sid=int(fields[3]),
            starttime=int(fields[19]),
        )
    except (IndexError, ValueError):
        return None


def _all_stats() -> dict[int, _Stat]:
    stats: dict[int, _Stat] = {}
    try:
        names = os.listdir("/proc")
    except OSError:  # pragma: no cover — no /proc means no Linux
        return stats
    for name in names:
        if not name.isdigit():
            continue
        stat = _read_stat(int(name))
        if stat is not None:
            stats[stat.pid] = stat
    return stats


def _carries_marker(pid: int, needle: bytes) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            environ = handle.read()
    except OSError:
        return False
    return needle in environ.split(b"\0")


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:200]


def snapshot_process(pid: int) -> OwnedProcess | None:
    """Record ``pid`` as an owned process, or ``None`` when it is not alive."""
    stat = _read_stat(pid)
    if stat is None or stat.state in ("Z", "X"):
        return None
    return OwnedProcess(pid=pid, starttime=stat.starttime)


def is_alive(process: OwnedProcess) -> bool:
    """True when that exact process (number AND start time) still runs."""
    stat = _read_stat(process.pid)
    if stat is None or stat.starttime != process.starttime:
        return False
    return stat.state not in ("Z", "X")


def find_owned(
    build_id: str | None,
    *,
    roots: Iterable[OwnedProcess] = (),
) -> set[OwnedProcess]:
    """Every live process this build owns, read from ``/proc`` now.

    ``roots`` are processes already known to be the build's (the child the
    runner spawned, anything recorded earlier in this stop). Only those whose
    start time still matches are trusted as roots.
    """
    stats = _all_stats()
    me = os.getpid()
    my_pgid = os.getpgrp()
    my_sid = os.getsid(0)
    roots = list(roots)
    seeds: set[int] = set()
    for root in roots:
        stat = stats.get(root.pid)
        if stat is not None and stat.starttime == root.starttime:
            seeds.add(root.pid)
    if build_id:
        needle = f"{OWNER_MARKER}={build_id}".encode()
        for pid in stats:
            if pid != me and _carries_marker(pid, needle):
                seeds.add(pid)
    seeds.discard(me)

    children: dict[int, list[int]] = {}
    for stat in stats.values():
        children.setdefault(stat.ppid, []).append(stat.pid)

    owned: set[int] = set()
    frontier = list(seeds)
    while frontier:
        pid = frontier.pop()
        if pid in owned or pid == me:
            continue
        owned.add(pid)
        frontier.extend(children.get(pid, ()))

    # A group or session LED by an owned process is owned too: a descendant
    # that was reparented before this read is still in its leader's group and
    # session. The kernel does not hand a number to a new process while a
    # group or session still bears it, so a recorded root that has died still
    # names its group truthfully — unless a different process now has its
    # number, in which case it names nothing of this build's. Never the
    # runner's own group or session.
    dead_roots = {
        root.pid
        for root in roots
        if root.pid not in stats or stats[root.pid].starttime == root.starttime
    }
    leaders = {pid for pid in owned | dead_roots if pid not in (my_pgid, my_sid)}
    for stat in stats.values():
        if stat.pid == me or stat.pid in owned:
            continue
        if stat.pgid in leaders or stat.sid in leaders:
            owned.add(stat.pid)

    return {
        OwnedProcess(pid=pid, starttime=stats[pid].starttime)
        for pid in owned
        if stats[pid].state not in ("Z", "X")
    }


def describe(processes: Iterable[OwnedProcess]) -> list[dict[str, Any]]:
    """JSON-ready description of processes, for a log or an answer."""
    return [
        {"pid": p.pid, "starttime": p.starttime, "command": _cmdline(p.pid)}
        for p in sorted(processes, key=lambda item: item.pid)
    ]


# ---------------------------------------------------------------------------
# Signalling
# ---------------------------------------------------------------------------


def signal_process_group(proc: Any, sig: int) -> None:
    """Signal the child's whole process group, never just the child.

    The spawn runs with ``start_new_session=True``, so the child LEADS a
    group of its own and this call cannot reach the forge daemon. That
    pairing is the point: a ``guardkit`` leg spawns its own children (a
    test runner, a harness), and ``Process.terminate()`` reaches exactly
    one pid — the grandchildren survive, keep working, and keep the
    child's pipes open.

    **The pgid is the child's pid, and it is NOT read back from the
    kernel.** ``start_new_session=True`` makes the child both session and
    group leader, so ``pgid == pid`` by construction — and a process
    group outlives its dead leader. The escalation step depends on
    exactly that: by the time SIGKILL runs, the child is usually already
    dead from the group SIGTERM *and reaped* by asyncio's child watcher,
    so ``os.getpgid(pid)`` raises :class:`ProcessLookupError` and a
    getpgid-first implementation signals NOTHING — not the group, not
    even the child. That is the one case the SIGTERM → grace → SIGKILL
    ladder exists for (a grandchild that ignored SIGTERM), so reading the
    pgid back would disarm the ladder precisely when it matters.

    A :class:`ProcessLookupError` from :func:`os.killpg` therefore means
    what it says: **no process remains in the group** — nothing to signal.
    (The pid is not reused while the child is unreaped, and after that the
    window is the kill ladder's few seconds; the estate accepts that
    residual over an escalation that never fires.) The child-alone
    fallback is for the cases where a *group* signal is impossible but a
    process may still be there: a platform with no :func:`os.killpg`, or a
    permission/OS error from the group call. A failure to signal is
    logged, never raised: this is the teardown half of a boundary whose
    contract is "never raises".

    Moved here from ``adapters/guardkit/run.py`` on 3 October 2026 so the
    long-running build's stop and the one-shot adapter use one copy.
    """
    pid = getattr(proc, "pid", None)
    if pid is None:  # pragma: no cover — asyncio always sets pid
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            # The whole group is gone — nothing to signal, nothing to say.
            return
        except OSError as exc:
            # PermissionError is an OSError subclass; one clause covers
            # both, and the group signal failing is the only thing that
            # makes the child-alone fallback below worth trying.
            logger.warning(
                "guardkit adapter: could not signal process group of pid=%s "
                "(%s: %s) — falling back to the child alone; any "
                "grandchildren it started may survive",
                pid,
                type(exc).__name__,
                exc,
            )
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):  # pragma: no cover — race only
        return


@dataclass(frozen=True, slots=True)
class _GroupOf:
    """Adapter so :func:`signal_process_group` can signal a bare group id."""

    pid: int

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)


def _signal_all(processes: Iterable[OwnedProcess], sig: int) -> None:
    """Signal every owned process's group, then every owned process itself.

    The runner's own group is never signalled as a group; a process in it is
    signalled alone.
    """
    my_pgid = os.getpgrp()
    groups: set[int] = set()
    live: list[OwnedProcess] = []
    for process in processes:
        stat = _read_stat(process.pid)
        if stat is None or stat.starttime != process.starttime:
            continue
        live.append(process)
        if stat.pgid != my_pgid and stat.pgid > 1:
            groups.add(stat.pgid)
    for group in groups:
        signal_process_group(_GroupOf(group), sig)
    for process in live:
        # Re-checked immediately before the signal: never signal a number
        # that now belongs to something else.
        if not is_alive(process):
            continue
        try:
            os.kill(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


async def _wait_gone(processes: set[OwnedProcess], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(is_alive(p) for p in processes):
            return
        await asyncio.sleep(_POLL_SECONDS)


async def stop_owned(
    build_id: str | None,
    *,
    roots: Iterable[OwnedProcess] = (),
    recorded: set[OwnedProcess] | None = None,
    grace_seconds: float | None = None,
) -> list[OwnedProcess]:
    """Stop every process the build owns; return the ones still alive.

    ``recorded`` (when given) is extended in place with every process this
    stop found, so a later pass still knows a process whose parent link has
    gone since. An empty answer means every recorded process is confirmed
    gone.
    """
    grace = build_stop_grace_seconds() if grace_seconds is None else grace_seconds
    known: set[OwnedProcess] = recorded if recorded is not None else set()
    known |= find_owned(build_id, roots=[*roots, *known])
    if known:
        logger.info(
            "build_processes: stopping build %s — %d owned process(es): %s",
            build_id,
            len(known),
            ", ".join(str(p.pid) for p in sorted(known, key=lambda p: p.pid)),
        )
    _signal_all(known, signal.SIGTERM)
    await _wait_gone(known, grace)
    # Read again: anything started during the grace is owned too.
    known |= find_owned(build_id, roots=known)
    _signal_all(known, signal.SIGKILL)
    await _wait_gone(known, _KILL_CONFIRM_SECONDS)
    known |= find_owned(build_id, roots=known)
    remaining = [p for p in known if is_alive(p)]
    if remaining:
        _signal_all(remaining, signal.SIGKILL)
    return sorted(remaining, key=lambda p: p.pid)


# ---------------------------------------------------------------------------
# Fixture containers
# ---------------------------------------------------------------------------


def _engine() -> str | None:
    """The engine's client, or ``None`` when there is no engine here at all.

    No client, or a client whose engine endpoint is not there (no socket at
    the path, nothing listening on the address), means no fixture container
    can have been started from here: "no containers", never a place held for
    ever. An engine that is there but refuses or errors stays "cannot
    confirm" (the caller's ``None``).
    """
    client = shutil.which(os.environ.get("FORGE_FIXTURE_ENGINE", "docker"))
    if client is None or not _engine_endpoint_reachable():
        return None
    return client


def _engine_endpoint_reachable() -> bool:
    host = os.environ.get("DOCKER_HOST", "").strip() or "unix:///var/run/docker.sock"
    try:
        if host.startswith("unix://"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect(host[len("unix://") :])
            return True
        if host.startswith("tcp://"):
            address = host[len("tcp://") :].split("/", 1)[0]
            name, _, port = address.rpartition(":")
            with socket.create_connection((name, int(port)), timeout=2.0):
                return True
    except (OSError, ValueError):
        return False
    # Any other kind of endpoint (ssh://, a named context) is the client's to
    # judge: asked, and a failure there is "cannot confirm".
    return True


def _engine_ids(build_id: str, *, include_stopped: bool) -> list[str] | None:
    """Container ids carrying this build's fixture label; ``None`` if unknown."""
    engine = _engine()
    if engine is None:
        return []
    argv = [engine, "ps", "--quiet", "--no-trunc"]
    if include_stopped:
        argv.append("--all")
    argv += ["--filter", f"label={FIXTURE_OWNER_LABEL}={build_id}"]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_ENGINE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "build_processes: the container engine could not be asked about "
            "build %s's fixtures (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None
    if done.returncode != 0:
        logger.warning(
            "build_processes: the container engine refused to list build %s's "
            "fixtures (exit %s): %s",
            build_id,
            done.returncode,
            done.stderr.strip()[:300],
        )
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


async def remove_fixture_containers(build_id: str) -> list[str] | None:
    """Remove this build's labelled fixture containers; the ones left, or None.

    ``None`` means the engine could not be asked, so nothing is confirmed.
    """
    ids = await asyncio.to_thread(_engine_ids, build_id, include_stopped=True)
    if not ids:
        return ids
    engine = _engine()
    assert engine is not None  # ids came from it
    logger.info(
        "build_processes: removing %d fixture container(s) of build %s",
        len(ids),
        build_id,
    )
    try:
        await asyncio.to_thread(
            subprocess.run,
            [engine, "rm", "--force", *ids],
            capture_output=True,
            text=True,
            timeout=_ENGINE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "build_processes: removing build %s's fixtures failed (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
    return await asyncio.to_thread(_engine_ids, build_id, include_stopped=False)


# ---------------------------------------------------------------------------
# Builds running in this process
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OwnedBuild:
    """A build whose child this process spawned, while it runs.

    Plain data only: the runner's HTTP route and the build's node may run on
    different event loops, so nothing here is an asyncio object.
    """

    build_id: str
    root: OwnedProcess | None
    recorded: set[OwnedProcess] = field(default_factory=set)
    stop_requested: bool = False


_BUILDS: dict[str, OwnedBuild] = {}

#: Builds asked to stop before this process registered their child (queued for
#: a job slot, or still in the steps before the spawn), with when they were
#: asked. The run's node finishes them CANCELLED without spawning anything,
#: and clears the entry when it ends. An entry for a build that never runs
#: here again (the factory asks the same route before it lets a cancelled
#: build's place go) is forgotten after a day.
_STOP_PENDING: dict[str, float] = {}
_STOP_PENDING_SECONDS: float = 24 * 3600.0


def stop_pending(build_id: str) -> bool:
    """True when a stop was asked for this build before its child existed."""
    now = time.monotonic()
    for stale in [b for b, at in _STOP_PENDING.items() if now - at > _STOP_PENDING_SECONDS]:
        del _STOP_PENDING[stale]
    return build_id in _STOP_PENDING


def forget(build_id: str) -> None:
    """The run of ``build_id`` here has ended: drop what is kept for it."""
    _STOP_PENDING.pop(build_id, None)
    _BUILDS.pop(build_id, None)


def register(build_id: str, pid: int) -> OwnedBuild:
    """Record that ``pid`` is the root of ``build_id``'s processes.

    The pid is trusted as a root only when ``/proc`` says it is this
    process's own child — a stand-in process object (tests) or a number that
    has already gone is recorded as no root at all, so a stop can never reach
    an unrelated process through it.
    """
    root: OwnedProcess | None = None
    stat = _read_stat(pid) if isinstance(pid, int) else None
    if stat is not None and stat.ppid == os.getpid():
        root = OwnedProcess(pid=pid, starttime=stat.starttime)
    entry = OwnedBuild(build_id=build_id, root=root)
    if root is not None:
        entry.recorded.add(root)
    # A stop asked for while the child was being spawned is honoured now.
    entry.stop_requested = stop_pending(build_id)
    _BUILDS[build_id] = entry
    return entry


def unregister(entry: OwnedBuild) -> None:
    if _BUILDS.get(entry.build_id) is entry:
        del _BUILDS[entry.build_id]


def lookup(build_id: str) -> OwnedBuild | None:
    return _BUILDS.get(build_id)


@dataclass(frozen=True, slots=True)
class StopReport:
    """The answer to "is everything this build owns gone?"."""

    build_id: str
    processes: tuple[OwnedProcess, ...] = ()
    containers: tuple[str, ...] = ()
    confirmed: bool = True
    reason: str = ""
    #: The stop was remembered for a run not (yet) started here.
    pending: bool = False

    @property
    def stopped(self) -> bool:
        return self.confirmed and not self.processes and not self.containers

    def as_json(self) -> dict[str, Any]:
        answer: dict[str, Any] = {"build_id": self.build_id, "stopped": self.stopped}
        if self.pending:
            answer["pending"] = True
        if not self.stopped:
            answer["remaining"] = {
                "processes": describe(self.processes),
                "containers": list(self.containers),
            }
            if self.reason:
                answer["reason"] = self.reason
        return answer


async def still_running(build_id: str) -> StopReport:
    """Stateless: is anything carrying this build's marker or label alive here?"""
    entry = _BUILDS.get(build_id)
    roots = sorted(entry.recorded, key=lambda p: p.pid) if entry else []
    processes = find_owned(build_id, roots=roots)
    if entry is not None:
        processes |= {p for p in entry.recorded if is_alive(p)}
    containers = await asyncio.to_thread(
        _engine_ids, build_id, include_stopped=False
    )
    if containers is None:
        return StopReport(
            build_id=build_id,
            processes=tuple(sorted(processes, key=lambda p: p.pid)),
            confirmed=False,
            reason="the container engine could not be asked about fixtures",
        )
    return StopReport(
        build_id=build_id,
        processes=tuple(sorted(processes, key=lambda p: p.pid)),
        containers=tuple(containers),
    )


async def stop_build(build_id: str) -> StopReport:
    """Stop one build's processes and fixtures here, then report what is left.

    When this process is running the build, its node is told the stop was
    requested (so it finishes as cancelled, not failed) and the stop starts
    from the child it spawned. Otherwise only the marker and the label say
    what is the build's — the answer is the same after a runner restart.
    """
    entry = _BUILDS.get(build_id)
    if entry is not None:
        entry.stop_requested = True
    else:
        # Nothing registered here yet: the run may be waiting for a job slot
        # or in its steps before the spawn. Remember the request so it never
        # spawns; it finishes CANCELLED.
        _STOP_PENDING[build_id] = time.monotonic()
    remaining = await stop_owned(
        build_id,
        roots=[entry.root] if entry is not None and entry.root is not None else (),
        recorded=entry.recorded if entry is not None else None,
    )
    if not remaining:
        await remove_fixture_containers(build_id)
    report = await still_running(build_id)
    if entry is None:
        report = StopReport(
            build_id=report.build_id,
            processes=report.processes,
            containers=report.containers,
            confirmed=report.confirmed,
            reason=report.reason,
            pending=True,
        )
    return report
