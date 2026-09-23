"""The executor: one deploy command per target, and it is the gate.

One-true-copy design pass, 21 September 2026, item 1, fourth revision **H**
and fifth revision **I** and **J**.

WHY THIS EXISTS. Checking a number before a deploy is launched cannot stop a
deploy COMMAND that is already running. Build A passes the check and starts
deploying, then stalls; its lease runs out; build B takes over and deploys the
newer result; A's old command wakes and puts the older result back. Refusing
A's write to the ledger afterwards is too late — the older thing is running.
So the thing that actually RUNS a project's deploy step is the gate:

a. **Every deploy request carries ``{target, target_counter, build}``.** A
   counter lower than the highest this executor has accepted for that target
   is refused. An EQUAL counter from a different build is refused — the
   counter was granted to one build, and a second build presenting it is
   presenting somebody else's authority. A higher one is accepted, under (c).
b. **At most one deploy command per target**, and the slot is held for the
   whole life of the command's PROCESS GROUP, never for the length of a
   request. The command is started in a process group of its own so that all
   of it can be found and stopped.
c. **A higher counter while a command is alive does not start anything.** The
   old command's whole process group is stopped, and the executor WAITS until
   every process in it is gone and confirms that, before it answers. If it
   cannot confirm within its limit it REFUSES: nothing new starts, and the
   build stays "published, deployment pending" with that reason. A second
   command is never started beside a live one.
d. **Before a command is started, the note is written durably**: the target,
   the counter and build accepted, and the command's identity — its process
   group, the time it started, and a marker unique to this deploy that the
   command and its children carry. The note is cleared only after every
   process of the command is confirmed gone. The highest counter accepted for
   each target is kept the same way and never goes down.

   **EVERY WRITE TO A NOTE IS CONDITIONAL** on the note ON DISK still
   describing the command the writer owns — its marker, its counter, its
   build, its process group and the time it started. A writer whose command
   has been superseded writes NOTHING and stops. This is the rule the stage's
   own reviewer drove a hole through: the waiter of a command that a takeover
   had already stopped cleared the note it was holding in memory, which by
   then was the SUCCESSOR'S note, so a third request read an empty slot and
   started a second command beside a live one, the target's highest counter
   went DOWN, and the older command's result overwrote the newer one on the
   target. The highest counter and the build it was granted to are written in
   the same conditional write and are never lowered. After ANY note change the
   process table is read again, by marker, before this executor will answer
   that a slot is empty.

   **The note is written in two parts, and the docstring says so because the
   window is real.** What the note needs — the process group and the group
   leader's start time — does not exist until the command has been started, so
   a note written wholly before the spawn would be a note that cannot identify
   anything. A PROVISIONAL note (the target, the counter, the build, the
   marker, and the word "starting") is therefore written first, then the
   command is started, then the same note is COMPLETED with the group and its
   start time — each write conditional as above. Between the two the marker in
   the command's own argument list is the only identity there is, and it is
   what covers the window: a "starting" note is treated as OCCUPIED, by this
   executor and by the reconciler, until one of them has looked in the process
   table for that marker and found nothing.
e. **On its own start the executor accepts nothing until it has reconciled.**
   For each target with a note of an active command it looks for survivors BY
   IDENTITY. Alive ⇒ the slot is OCCUPIED, not empty: the command is adopted
   and treated exactly as if the executor had never restarted. None alive ⇒
   the note is cleared, and the next holder reads what is running before it
   acts.
f. **Notes missing or unreadable is not an empty slot.** MISSING and
   unreadable are the same case and are treated the same way, because the
   executor cannot tell a note it never wrote from a note it has lost. Either
   way it looks for any process carrying a deploy marker for that target;
   found ⇒ occupied; cannot tell ⇒ deploys for that target are refused and the
   refusal says why. When it can show that nothing is alive, it CONFIRMS the
   target's current counter and owning build **with the coordinator** — a
   question put to the coordinator's own read-only answer, not the counter the
   delayed request presents, because a delayed request presenting an old
   counter cannot establish who owns the target. (This is the point the
   design's sign-off note asks reviewers to watch.)

   **WITH NOBODY TO ASK, THE DEPLOY IS REFUSED — including a target's first.**
   Until 23 September 2026 there was one more line here, and it was wrong. The
   request carried the coordinator's reading of whether anything was running on
   the target, and a request that said "nothing is" was accepted as that
   target's first deploy. A reviewer drove it: build B completed at counter 2,
   the note was deleted, the executor restarted, and build A's DELAYED counter-1
   request arrived carrying a claim that had been true when the request was made
   and was stale by the time it landed. It was accepted and it replaced B.

   The source was the fault, not the value. **A request cannot establish its own
   freshness**, so no claim a request makes about the target is consulted on any
   path any more. The cost is said out loud: a coordinator with no read-only
   ownership route can deploy nothing once a note is missing, a target's first
   deployment included, and that route is named in the rollout as the thing that
   has to exist first. The alternative is a path on which a stale request lets
   itself in, which was measured rather than imagined.
g. **Every deploy command gets a hard time limit**, and the deploy step's
   lease in the ledger is longer than it, so a healthy deploy is not taken
   over.
h. **The environment door.** The child's environment is built from the named
   list, plus the recorded memory name and the project's own declared setting
   names carried on the request. It is never a copy of this process's own.

WHAT THIS ASKS OF A PROJECT'S DEPLOY STEP, said plainly because it is a
contract and not an implementation detail. The slot is the life of the
command's PROCESS GROUP, so when the step returns, the whole group is stopped
and confirmed gone before the slot is released. A step that leaves a process of
its own running in that group — a server started in the foreground's own
group, say — has it stopped. A project's deploy step must therefore finish its
changes before it returns, and whatever it leaves running must live somewhere
of its own: its own session, a service manager, a container. This is the same
requirement the design makes of the step already ("the deploy step must finish
its changes before it returns"); it is written here because it binds every
project and a project cannot read it out of the design.

HOW A COMMAND IS IDENTIFIED, and why it is not a process number. Process
numbers are reused. The identity is three things together: the process GROUP,
the time the group leader started (read from the kernel's own process table,
never from anybody's environment), and a MARKER naming the target, which the
command's own argument list carries so that it can be found again even when
every note is gone.

NOTHING HERE READS A PROCESS'S ENVIRONMENT. Survivors are found from the
kernel's process table — the group each process belongs to, when it started,
and the argument list it was started with. A process's environment is never
opened, which is both the estate's rule and the right shape: an environment is
where credentials live.

TWO LOCKS, AND WHAT EACH IS FOR. A deployment target's slot is decided,
stopped and started under **that target's own lock**, which is held from the
moment the slot is read until the new command has been started — and never
during the command itself. The executor's one shared lock is held only while
its own bookkeeping is touched (which target is refused, which lock belongs to
which target), for as long as a dictionary lookup takes. Stopping a command on
one target can take up to the stop-and-confirm limit, so it must never be able
to hold up a deploy to a DIFFERENT target: it runs under the per-target lock
alone.

NOTHING HERE NAMES A LANGUAGE, A TEST RUNNER, A PROTOCOL, A DATABASE, A
PACKAGE MANAGER OR A HOSTING PROVIDER. What is deployed, and what an identity
is, belong to the project; this file starts a program the project declared,
stops it, and counts.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from forge.launch_environment import build_launch_env, declared_setting_refusal

logger = logging.getLogger(__name__)

__all__ = [
    "DEPLOY_MARKER_PREFIX",
    "DEFAULT_COMMAND_SECONDS",
    "DEFAULT_STOP_CONFIRM_SECONDS",
    "NOTES_SETTING_NAME",
    "STOPPED_BY_A_TAKEOVER",
    "NOTHING_WAS_STARTED",
    "DeployExecutor",
    "DeployRequest",
    "ExecutorAnswer",
    "ProcessTable",
    "declared_names",
    "request_from",
    "setting_refusal",
    "the_marker_for",
]


#: The setting that names the folder these notes live in. It is defined here,
#: beside the thing that writes them, so that a refusal about the folder can
#: name the setting an operator has to set. The service reads it.
NOTES_SETTING_NAME: str = "FORGE_DEPLOY_NOTES_DIR"

#: What a deploy command's argument list carries so it can be found again with
#: no notes at all. It names the TARGET, because "is anything deploying to
#: this target?" is the question a note-less executor has to answer.
DEPLOY_MARKER_PREFIX: str = "forge-deploy-marker"

#: The hard time limit one deploy command gets when nothing says otherwise.
#: The deployment lock's lease must be LONGER than this (the design's H), and
#: the press checks the two against each other rather than assuming.
DEFAULT_COMMAND_SECONDS: float = 900.0

#: How long the executor waits, after stopping a process group, for every
#: process in it to be GONE. Not confirmed within this ⇒ refuse; nothing new
#: starts.
DEFAULT_STOP_CONFIRM_SECONDS: float = 60.0

#: How long a polite stop is given before the group is killed outright.
_GRACE_SECONDS: float = 5.0

#: THE WORD FOR A COMMAND A TAKEOVER STOPPED. It is its own word because it is
#: its own thing: the command did not run to an end and its exit is the signal
#: that stopped it, so reading it as "the deploy command ran" would report a
#: deploy that never happened — and, worse, report the SIGNAL as the deploy
#: step's own exit code. Whatever reads this must treat it as no deploy at all.
STOPPED_BY_A_TAKEOVER: str = "the-deploy-command-was-stopped-by-a-takeover"

#: The refusals after which NOTHING WAS STARTED on the target (23 September
#: 2026). A press that meets one of these has deployed nothing: the target is
#: owned by a later holder, or an earlier command is still alive, or the older
#: command could not be confirmed stopped. None of them is "the deploy failed",
#: and the press reads them all the same way as a takeover: the result stays
#: "published, deployment pending" with the reason said. One list, so the two
#: readers cannot drift apart.
NOTHING_WAS_STARTED: tuple[str, ...] = (
    STOPPED_BY_A_TAKEOVER,
    "the-counter-has-moved-on",
    "that-counter-belongs-to-another-build",
    "the-slot-is-occupied",
    "a-command-is-already-running",
    "the-old-command-could-not-be-confirmed-stopped",
)

#: The word a note carries while its command is being started — after the note
#: is written and before the command's process group is known. A note in this
#: state means the slot is OCCUPIED until somebody has looked in the process
#: table for the note's marker and found nothing.
_STARTING: str = "starting"

#: The word a note carries once its command is started and its group recorded.
_RUNNING: str = "running"

#: The word a note carries from the moment a takeover has decided to stop its
#: command. It is written BEFORE the stop, because the stop is what wakes the
#: old command's waiter: a waiter that woke to find its note unchanged would
#: report a command somebody killed as a command that ran, and would then clear
#: the successor's note. With this written first, the waiter's one question —
#: "is the note on disk still mine?" — answers itself.
_SUPERSEDED: str = "superseded"

_SAFE_TARGET = re.compile(r"[^A-Za-z0-9._:-]")


def the_marker_for(target: str, unique: str | None = None) -> str:
    """The marker one deploy command carries, naming its target.

    ``<prefix>:<target>:<unique>``. The target is written plainly so that a
    note-less executor can find a live deploy for it; the unique part tells
    two deploys of the same target apart.
    """
    safe = _SAFE_TARGET.sub("-", str(target or "").strip()) or "unnamed-target"
    return f"{DEPLOY_MARKER_PREFIX}:{safe}:{unique or uuid.uuid4().hex}"


def _target_in(marker: str) -> str:
    """The target a marker names, or ``""``.

    A target name may itself carry colons — the factory composes one out of a
    project and the environment its profile declares, with ``::`` between them
    — so the marker is taken apart from BOTH ends rather than split: the
    prefix off the front, the unique part off the back, and what is left is
    the target exactly as the note file's own name spells it. Splitting on
    colons truncated the name at its first one, which made a live command look
    like one for a target nobody had asked about.
    """
    text = str(marker)
    head = f"{DEPLOY_MARKER_PREFIX}:"
    if not text.startswith(head):
        return ""
    target, _, unique = text[len(head) :].rpartition(":")
    return target if unique else ""


# ---------------------------------------------------------------------------
# The kernel's own process table — the ONE place survivors are looked for
# ---------------------------------------------------------------------------


class ProcessTable:
    """What the kernel says about the processes that exist right now.

    Three questions, and nothing else: which group does a process belong to,
    when did it start, and what argument list was it started with. A process's
    ENVIRONMENT is never opened — that is both this estate's rule and the
    right shape, because an environment is where a credential would be.

    Every method answers ``None`` rather than raising when it cannot tell, and
    the executor treats "cannot tell" as its own ending: it refuses, which
    refuses more than it needs to and never less.
    """

    def __init__(self, root: str | Path = "/proc") -> None:
        self._root = Path(root)

    @property
    def available(self) -> bool:
        try:
            return self._root.is_dir()
        except OSError:
            return False

    def _pids(self) -> list[int] | None:
        try:
            return sorted(
                int(entry.name)
                for entry in self._root.iterdir()
                if entry.name.isdigit()
            )
        except OSError as exc:
            logger.warning(
                "deploy executor: the process table at %s could not be read "
                "(%s)",
                self._root,
                exc,
            )
            return None

    def _stat(self, pid: int) -> list[str] | None:
        try:
            raw = (self._root / str(pid) / "stat").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        # The second field is the program name in brackets and may itself
        # contain spaces and brackets, so the split is made after the LAST
        # closing bracket. Everything the caller wants is after it.
        close = raw.rfind(")")
        if close < 0:
            return None
        return raw[close + 2 :].split()

    def group_of(self, pid: int) -> int | None:
        """Which process group ``pid`` belongs to. ``None`` = cannot tell."""
        fields = self._stat(pid)
        if fields is None or len(fields) < 3:
            return None
        try:
            return int(fields[2])
        except ValueError:
            return None

    def started_at(self, pid: int) -> int | None:
        """When ``pid`` started, in the kernel's own ticks since boot.

        This is what makes a process number safe to record: a reused number
        has a different start time, so an identity made of the two together
        cannot be mistaken for a process that has gone.
        """
        fields = self._stat(pid)
        if fields is None or len(fields) < 20:
            return None
        try:
            return int(fields[19])
        except ValueError:
            return None

    def command_of(self, pid: int) -> str | None:
        """The argument list ``pid`` was started with, as one line."""
        try:
            raw = (self._root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return None
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")

    def members_of(self, group: int) -> list[int] | None:
        """Every process in ``group``. ``None`` = the table could not be read."""
        pids = self._pids()
        if pids is None:
            return None
        found: list[int] = []
        for pid in pids:
            if self.group_of(pid) == group:
                found.append(pid)
        return found

    def carrying(self, marker_fragment: str) -> list[int] | None:
        """Every process whose argument list carries this fragment.

        ``None`` = the table could not be read, which is "cannot tell" and
        never "there are none".
        """
        pids = self._pids()
        if pids is None:
            return None
        found: list[int] = []
        for pid in pids:
            line = self.command_of(pid)
            if line and marker_fragment in line:
                found.append(pid)
        return found


# ---------------------------------------------------------------------------
# What arrives, and what goes back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployRequest:
    """One request to run a project's declared deploy step.

    ``target``, ``target_counter`` and ``build`` are the ownership the design
    requires on EVERY deploy request. The rest is what the step needs: where
    to run, which program, what the project declared, and the identity the
    step must deploy.
    """

    target: str
    target_counter: int
    build: str
    cwd: str
    script: str
    env_file: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    memory_project: str | None = None
    launch_settings: tuple[str, ...] = ()
    timeout: float = DEFAULT_COMMAND_SECONDS
    identity: str | None = None
    identity_setting: str | None = None
    #: The ARTIFACT the check said it checked, and the setting the project
    #: wants it handed back in. The step must deploy exactly this, rather than
    #: resolve a name of its own at the moment it deploys — which is a name
    #: another build can have taken since the check (23 September 2026).
    artifact: str | None = None
    artifact_setting: str | None = None


@dataclass(frozen=True)
class ExecutorAnswer:
    """What the executor did, or refused to do, and why in plain words."""

    accepted: bool
    word: str
    sentence: str
    exit_code: int | None = None
    output: str = ""
    marker: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "word": self.word,
            "sentence": self.sentence,
            "exit_code": self.exit_code,
            "output_tail": self.output,
            "marker": self.marker,
        }


# ---------------------------------------------------------------------------
# The notes — durable, one folder, one file per target
# ---------------------------------------------------------------------------


@dataclass
class _Note:
    target: str
    counter: int
    build: str
    marker: str
    group: int
    started_at: int | None
    started_wall: float
    limit: float
    highest_counter: int
    #: WHICH BUILD the highest counter was granted to. It outlives the active
    #: command, because rule (a)'s second half — an EQUAL counter from a
    #: different build is refused — has to hold after the command has ended
    #: as well as while it is running. Without it, a second build presenting
    #: the first build's counter a moment later would be accepted.
    highest_build: str = ""
    #: ``"starting"`` between the note being written and the command's process
    #: group being known; ``"running"`` after that; ``""`` on a note with no
    #: active command at all. A ``"starting"`` note is occupied, not empty.
    phase: str = _RUNNING

    def to_wire(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "counter": self.counter,
            "build": self.build,
            "marker": self.marker,
            "group": self.group,
            "started_at": self.started_at,
            "started_wall": self.started_wall,
            "limit": self.limit,
            "highest_counter": self.highest_counter,
            "highest_build": self.highest_build,
            "phase": self.phase,
        }

    @property
    def has_a_command(self) -> bool:
        """Does this note describe a command that may still change the target?"""
        return bool(self.group) or self.phase == _STARTING


def _the_same_command(on_disk: _Note, mine: _Note) -> bool:
    """Is the note on disk still the very command ``mine`` describes?

    Every part of the identity is compared, because every part of it can be
    the thing that differs: the marker (unique to one deploy), the counter and
    the build the counter was granted to, the process group, and the time the
    command was started. A successor's note shares the target and nothing
    else, and that is exactly the case this exists to catch.

    THE PHASE IS PART OF THE IDENTITY, and that is how a waiter learns it has
    been taken over: a takeover writes "superseded" onto the note before it
    stops the command, so the waiter that stop wakes finds a note which is no
    longer its own — rather than its own note, which it would then destroy on
    the successor's behalf.
    """
    return (
        on_disk.marker == mine.marker
        and int(on_disk.counter) == int(mine.counter)
        and str(on_disk.build) == str(mine.build)
        and int(on_disk.group) == int(mine.group)
        and float(on_disk.started_wall) == float(mine.started_wall)
        and str(on_disk.phase) == str(mine.phase)
    )


def _the_highest_after(
    previous: _Note | None, counter: int, build: str
) -> tuple[int, str]:
    """The highest counter for a target, and the build it was granted to.

    IT NEVER GOES DOWN (the design's rule d). A write that carries a lower
    counter than the note it replaces keeps the note's own, together with the
    build that one was granted to, so a delayed request presenting an older
    counter is still refused afterwards.
    """
    highest = 0
    holder = ""
    if previous is not None:
        was = int(previous.counter or 0)
        highest = max(int(previous.highest_counter or 0), was)
        holder = (
            previous.build
            if was >= int(previous.highest_counter or 0) and previous.build
            else previous.highest_build
        )
    if int(counter) > highest:
        return int(counter), str(build)
    if int(counter) == highest and not holder:
        return highest, str(build)
    return highest, holder


def _note_from(decoded: Any) -> _Note | None:
    if not isinstance(decoded, dict):
        return None
    try:
        return _Note(
            target=str(decoded["target"]),
            counter=int(decoded["counter"]),
            build=str(decoded["build"]),
            marker=str(decoded["marker"]),
            group=int(decoded["group"]),
            started_at=(
                int(decoded["started_at"])
                if decoded.get("started_at") is not None
                else None
            ),
            started_wall=float(decoded.get("started_wall") or 0.0),
            limit=float(decoded.get("limit") or DEFAULT_COMMAND_SECONDS),
            highest_counter=int(decoded.get("highest_counter") or 0),
            highest_build=str(decoded.get("highest_build") or ""),
            phase=str(decoded.get("phase") or (_RUNNING if decoded.get("group") else "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


class DeployExecutor:
    """One per helper service. The gate on every deploy command it runs."""

    def __init__(
        self,
        *,
        notes_root: str | Path,
        ask_the_coordinator: Callable[[str], Mapping[str, Any] | None] | None = None,
        process_table: ProcessTable | None = None,
        parent_environment: Mapping[str, str] | None = None,
        command_seconds: float = DEFAULT_COMMAND_SECONDS,
        stop_confirm_seconds: float = DEFAULT_STOP_CONFIRM_SECONDS,
        spawn: Callable[..., "subprocess.Popen[bytes]"] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(notes_root)
        self._ask = ask_the_coordinator
        self._table = process_table or ProcessTable()
        self._parent = parent_environment
        self._command_seconds = float(command_seconds)
        self._stop_confirm_seconds = float(stop_confirm_seconds)
        self._spawn = spawn or self._default_spawn
        self._clock = clock
        # TWO LOCKS, AND THE SECOND ONE MATTERS (the stage's reviewer, third
        # finding). This one is the executor's own bookkeeping lock: which
        # targets are refused, and which lock belongs to which target. It is
        # held for as long as a dictionary lookup takes and NEVER across
        # anything that waits — in particular never across a stop, which may
        # take up to the stop-and-confirm limit. Held there, a stop on target X
        # would have blocked a deploy to target Y, which is the opposite of
        # what this comment used to promise.
        self._gate = threading.Lock()
        #: ONE LOCK PER DEPLOYMENT TARGET. It is held from reading the target's
        #: slot, through stopping whatever is in it and confirming that, to
        #: starting the new command — and it is released before the command
        #: itself runs. Targets do not wait on each other.
        self._per_target: dict[str, threading.RLock] = {}
        #: Targets whose notes could not be read and whose slot could not be
        #: settled. Deploys for these are refused until somebody looks.
        self._cannot_tell: dict[str, str] = {}
        #: Set when the notes folder itself cannot be read OR written. Both
        #: are the same ending: an executor that cannot write its note down
        #: cannot find its own command again, so it starts nothing. Proved at
        #: reconcile time rather than discovered on the first deploy, because
        #: "the folder is not writable by this user" is an operator's fact and
        #: should be said at the service's start, not in the middle of a
        #: merge.
        self._folder_problem: str | None = None
        self._reconciled = False

    # -- the notes ---------------------------------------------------------

    def _note_path(self, target: str) -> Path:
        safe = _SAFE_TARGET.sub("-", str(target).strip()) or "unnamed-target"
        return self._root / f"{safe}.json"

    def _read_note(self, target: str) -> tuple[_Note | None, str | None]:
        """``(note, why_it_could_not_be_read)``. Both ``None`` = no note, fine."""
        path = self._note_path(target)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"the note at {path} could not be read ({exc})"
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            return None, f"the note at {path} is not readable ({exc})"
        note = _note_from(decoded)
        if note is None:
            return None, f"the note at {path} does not say what it has to say"
        return note, None

    def _lock_for(self, target: str) -> threading.RLock:
        """This target's own lock. The bookkeeping lock is held to find it."""
        with self._gate:
            lock = self._per_target.get(target)
            if lock is None:
                lock = threading.RLock()
                self._per_target[target] = lock
            return lock

    def _write_note(self, note: _Note) -> None:
        """Whole, or not at all: written beside and moved into place.

        PRIVATE TO THE CONDITIONAL WRITERS BELOW. Nothing calls this without
        first proving, under the target's own lock, that the note on disk is
        still the one the caller owns — an unconditional write is how a
        superseded waiter destroyed its successor's note.
        """
        path = self._note_path(note.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        temporary.write_text(
            json.dumps(note.to_wire(), sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _the_note_is_still_mine(self, mine: _Note) -> bool:
        """Is the note on disk still the command this caller owns?"""
        on_disk, _ = self._read_note(mine.target)
        return on_disk is not None and _the_same_command(on_disk, mine)

    def _clear_if_it_is_still_mine(self, note: _Note) -> bool:
        """Clear the ACTIVE command — only if it is still the one on disk.

        ``True`` = cleared. ``False`` = **this command has been superseded and
        nothing was written**, which is the whole of the blocker's cure: a
        waiter that has been taken over must not touch its successor's note.

        The note file stays, with no live command on it, because the highest
        counter accepted for this target has to survive: it is what refuses a
        delayed request that presents an older one. The highest is taken from
        the note ON DISK, so it is never lowered by this write.
        """
        with self._lock_for(note.target):
            on_disk, _ = self._read_note(note.target)
            if on_disk is None or not _the_same_command(on_disk, note):
                logger.warning(
                    "deploy executor: the note for %s is no longer the command "
                    "this waiter owns (counter %s, build %s) — nothing was "
                    "written, and the command that holds the slot now keeps it",
                    note.target,
                    note.counter,
                    note.build,
                )
                return False
            highest, holder = _the_highest_after(
                on_disk, on_disk.counter, on_disk.build
            )
            self._write_note(
                _Note(
                    target=on_disk.target,
                    counter=0,
                    build="",
                    marker="",
                    group=0,
                    started_at=None,
                    started_wall=0.0,
                    limit=on_disk.limit,
                    highest_counter=highest,
                    highest_build=holder,
                    phase="",
                )
            )
            return True

    def _claim_the_slot(self, provisional: _Note) -> bool:
        """Write the PROVISIONAL note, only onto a slot with no command in it.

        The caller holds the target's lock and has just settled the slot, so
        this is the second half of that decision written down: the target, the
        counter, the build and the marker, before anything is started. The
        group and its start time do not exist yet and are filled in by
        :meth:`_complete_the_note` the moment they do.
        """
        with self._lock_for(provisional.target):
            on_disk, unreadable = self._read_note(provisional.target)
            if unreadable is not None:
                # A note that cannot be read carries no counter to keep and
                # names no command to wait for. The slot was settled before
                # this — by looking in the process table, and by asking the
                # coordinator who owns the target — so it is written over, and
                # said out loud because a lost note is a fact about the machine.
                logger.warning(
                    "deploy executor: %s is written over a note that could not "
                    "be read (%s); the slot was settled by looking, not by "
                    "believing the note",
                    provisional.target,
                    unreadable,
                )
                on_disk = None
            elif on_disk is not None and on_disk.has_a_command:
                return False
            highest, holder = _the_highest_after(
                on_disk, provisional.counter, provisional.build
            )
            self._write_note(
                replace(
                    provisional, highest_counter=highest, highest_build=holder
                )
            )
            return True

    def _mark_superseded(self, note: _Note) -> _Note | None:
        """Say on the note that this command is being taken over, BEFORE it is.

        Returns the note as it now stands on disk, or ``None`` when the note
        changed underneath this caller and nothing was written.

        THE ORDER IS THE POINT. Stopping a command is what wakes its waiter, so
        the waiter must be able to see that it has been superseded the moment
        it wakes. Written afterwards, the waiter would read its own note back,
        report a command somebody killed as a command that ran, and clear the
        note — which by then is the successor's.
        """
        with self._lock_for(note.target):
            on_disk, _ = self._read_note(note.target)
            if on_disk is None or not _the_same_command(on_disk, note):
                return None
            superseded = replace(on_disk, phase=_SUPERSEDED)
            self._write_note(superseded)
            return superseded

    def _complete_the_note(self, mine: _Note, whole: _Note) -> bool:
        """Fill the started command's group in — only onto this command's note.

        ``mine`` is the provisional note this caller wrote; ``whole`` is the
        same note with the group and its start time. ``False`` = somebody
        else's note is on disk, so nothing was written.
        """
        with self._lock_for(mine.target):
            on_disk, _ = self._read_note(mine.target)
            if on_disk is None or not _the_same_command(on_disk, mine):
                return False
            highest, holder = _the_highest_after(on_disk, whole.counter, whole.build)
            self._write_note(
                replace(whole, highest_counter=highest, highest_build=holder)
            )
            return True

    def _highest(self, note: _Note | None) -> int:
        if note is None:
            return 0
        return max(int(note.highest_counter or 0), int(note.counter or 0))

    # -- is a command alive? -----------------------------------------------

    def _alive(self, note: _Note) -> bool | None:
        """Is this command's process group still alive? ``None`` = cannot tell.

        Identity, not a process number: every process in the recorded group
        counts, and when the group leader is still there its start time has to
        match, so a reused number is not mistaken for the command.

        A note with no group is either a slot with nothing in it or a command
        that was being STARTED when this note was written. Those are not the
        same, and the second is answered by the marker rather than here.
        """
        if not note.group:
            if note.phase == _STARTING:
                return self._carrying_this_marker(note.marker)
            return False
        if not self._table.available:
            return None
        members = self._table.members_of(note.group)
        if members is None:
            return None
        if not members:
            return False
        if note.started_at is not None and note.group in members:
            started = self._table.started_at(note.group)
            if started is not None and started != note.started_at:
                # The number was reused by something that is not our command.
                # Anything else in "its" group is then somebody else's too.
                return False
        return True

    def _targets_alive_with_no_note(self, files: "list[Path]") -> list[str]:
        """Targets a live deploy marker names that no note file accounts for.

        The marker's middle part is the target with the characters a file name
        cannot carry replaced — the same transformation the note file's own
        name gets — so the two are compared as they stand. ``[]`` when the
        process table cannot be read: "cannot tell" is answered per request by
        :meth:`_settle_the_slot`, which refuses, rather than guessed at here.
        """
        alive = self._table.carrying(f"{DEPLOY_MARKER_PREFIX}:")
        if not alive:
            return []
        accounted = {path.stem for path in files}
        found: list[str] = []
        for pid in alive:
            line = self._table.command_of(pid) or ""
            for piece in line.split():
                if not piece.startswith(f"{DEPLOY_MARKER_PREFIX}:"):
                    continue
                stem = _target_in(piece)
                if stem and stem not in accounted and stem not in found:
                    found.append(stem)
        return found

    def _carrying_this_marker(self, marker: str) -> bool | None:
        """Is any process carrying THIS command's own marker? ``None`` = cannot tell.

        The one identity a command has between its note being written and its
        process group being known.
        """
        if not marker:
            return False
        found = self._table.carrying(marker)
        if found is None:
            return None
        return bool(found)

    def _anything_for(self, target: str) -> bool | None:
        """Is ANY process carrying a deploy marker for this target? (rule f)"""
        safe = _SAFE_TARGET.sub("-", str(target).strip()) or "unnamed-target"
        found = self._table.carrying(f"{DEPLOY_MARKER_PREFIX}:{safe}:")
        if found is None:
            return None
        return bool(found)

    # -- stopping one, and CONFIRMING it is gone ---------------------------

    def _stop_and_confirm(self, note: _Note) -> tuple[bool, str]:
        """Stop the whole group and wait until every process in it has gone.

        ``(True, "")`` only when the executor can SHOW that nothing of the
        command is left. Anything else is ``(False, why)`` and nothing new is
        started — which is the design's rule and the safe side: a command that
        might still be running is a command that can still change the target.
        """
        if not note.group:
            return True, ""
        deadline = self._clock() + self._stop_confirm_seconds
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(note.group, sig)
            except ProcessLookupError:
                break
            except PermissionError as exc:
                return False, (
                    f"the deploy command still running for {note.target} could "
                    f"not be stopped ({exc}), so nothing new was started"
                )
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    return False, (
                        f"the deploy command still running for {note.target} "
                        f"could not be stopped ({exc}), so nothing new was "
                        "started"
                    )
                break
            grace = _GRACE_SECONDS if sig is signal.SIGTERM else 0.0
            until = min(self._clock() + grace, deadline)
            while self._clock() < until:
                if self._alive(note) is False:
                    return True, ""
                time.sleep(0.05)
            if self._alive(note) is False:
                return True, ""
        while self._clock() < deadline:
            alive = self._alive(note)
            if alive is False:
                return True, ""
            if alive is None:
                return False, (
                    f"whether the deploy command still running for "
                    f"{note.target} has stopped could not be told, so nothing "
                    "new was started"
                )
            time.sleep(0.05)
        return False, (
            f"the deploy command already running for {note.target} was told to "
            f"stop and was not confirmed gone within "
            f"{self._stop_confirm_seconds:g}s, so nothing new was started"
        )

    # -- (e) reconcile on start --------------------------------------------

    def reconcile(self) -> dict[str, str]:
        """Settle every target's slot before ANY deploy is accepted.

        Run once, at the executor's own start. For each target with a note of
        an active command: survivors alive ⇒ the slot stays OCCUPIED and the
        command is adopted; none alive ⇒ the note is cleared and the next
        holder reads what is running before it acts; cannot tell ⇒ deploys for
        that target are refused until somebody looks.
        """
        settled: dict[str, str] = {}
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            files = sorted(self._root.glob("*.json"))
        except OSError as exc:
            self._folder_problem = (
                f"the executor's notes folder {self._root} could not be read "
                f"({exc})"
            )
            logger.error(
                "deploy executor: its own notes folder %s could not be read "
                "(%s) — every deploy is refused until somebody looks",
                self._root,
                exc,
            )
            self._reconciled = True
            return {"*": self._folder_problem}
        # CAN IT BE WRITTEN? A folder that can be read and not written fails
        # later, once, in the middle of a deploy, with a note that could not be
        # written. Proved here instead, so it is said at the service's start.
        probe = self._root / f".writable.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            probe.write_text("", encoding="utf-8")
        except OSError as exc:
            self._folder_problem = (
                f"the executor's notes folder {self._root} cannot be written "
                f"({exc}) — it is named by {NOTES_SETTING_NAME}, and without "
                "it the executor cannot write down what it is about to deploy "
                "and so will start nothing"
            )
            logger.error("deploy executor: %s", self._folder_problem)
        else:
            try:
                probe.unlink()
            except OSError:
                pass
        for path in files:
            try:
                decoded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # RULE (f): a note that cannot be read is NOT an empty slot.
                #
                # THE TARGET'S REAL NAME IS IN THAT NOTE, and the note is what
                # could not be read — so all this has to go on is the file's
                # own name, which is the target with the characters a file
                # name cannot carry replaced. That is enough to LOOK, and it
                # is reported, but nothing is written into the refusal list
                # under it: a request names the target itself, and the same
                # question is asked again, properly keyed, when one arrives.
                stem = path.stem
                anything = self._anything_for(stem)
                if anything is True:
                    settled[stem] = "occupied (a deploy marker is alive)"
                elif anything is None:
                    settled[stem] = "cannot tell"
                else:
                    settled[stem] = "nothing alive"
                logger.warning(
                    "deploy executor: its note at %s could not be read (%s) — "
                    "the slot for %s reads as %s, and it is settled again when "
                    "a request names the target",
                    path,
                    exc,
                    stem,
                    settled[stem],
                )
                continue
            note = _note_from(decoded)
            if note is None or not note.has_a_command:
                settled[path.stem] = "no active command"
                continue
            # A NOTE THAT SAYS "STARTING" IS OCCUPIED UNTIL SOMEBODY HAS
            # LOOKED. The executor was between writing this note and knowing
            # its command's process group when it stopped, so the marker in
            # the command's own argument list is the only identity there is —
            # and :meth:`_alive` answers by it for exactly this case.
            alive = self._alive(note)
            if alive is True:
                settled[note.target] = (
                    "occupied (adopted)"
                    if note.group
                    else "occupied (a command was being started; its marker is alive)"
                )
            elif alive is None:
                settled[note.target] = "cannot tell"
                self._cannot_tell[note.target] = (
                    f"whether the deploy command recorded for {note.target} is "
                    "still alive could not be told, so deploys for it are "
                    "refused until somebody looks"
                )
            else:
                self._clear_if_it_is_still_mine(note)
                settled[note.target] = "cleared"
        # AND THE TARGETS WITH NO NOTE AT ALL (rule f, the missing half). The
        # walk above can only see targets this executor has a file for, so a
        # helper that came back onto an empty folder — a fresh one, a folder
        # that was not durable, a note somebody removed — would have settled
        # NOTHING while a deploy command of its own was still alive. The
        # command carries its marker in its argument list for exactly this, so
        # the process table is swept for markers no note accounts for. Nothing
        # is written into the refusal list here: a request names the target
        # itself and the same question is asked again, properly keyed, by
        # :meth:`_settle_the_slot`.
        for stem in self._targets_alive_with_no_note(files):
            settled[stem] = (
                "occupied (a deploy marker is alive and no note accounts for it)"
            )
            logger.error(
                "deploy executor: a process carrying a deploy marker for %s is "
                "alive and this executor has no note of it — its notes did not "
                "survive. The slot is OCCUPIED, not empty, and deploys for it "
                "are refused until that command has gone",
                stem,
            )
        self._reconciled = True
        logger.info("deploy executor: reconciled %s", settled or "(nothing to settle)")
        return settled

    # -- the gate ----------------------------------------------------------

    def run(self, request: DeployRequest) -> ExecutorAnswer:
        """Accept or refuse this deploy request, and run it if it is accepted."""
        if not self._reconciled:
            self.reconcile()
        target = str(request.target or "").strip()
        if not target:
            return ExecutorAnswer(
                accepted=False,
                word="the-request-names-no-target",
                sentence=(
                    "a deploy request has to name the deployment target it "
                    "owns, and this one did not, so nothing was deployed."
                ),
            )
        if not str(request.build or "").strip():
            return ExecutorAnswer(
                accepted=False,
                word="the-request-names-no-build",
                sentence=(
                    f"a deploy request for {target} has to name the build the "
                    "target's counter was granted to, and this one did not, so "
                    "nothing was deployed."
                ),
            )
        try:
            counter = int(request.target_counter)
        except (TypeError, ValueError):
            counter = -1
        if counter < 1:
            return ExecutorAnswer(
                accepted=False,
                word="the-request-carries-no-counter",
                sentence=(
                    f"a deploy request for {target} has to carry the target's "
                    "own deployment counter, and this one did not, so nothing "
                    "was deployed."
                ),
            )

        # THE TARGET'S OWN LOCK, and not the executor's one bookkeeping lock.
        # Deciding the slot can mean stopping a command and waiting until every
        # process of it is gone, which has its own limit in seconds; under one
        # lock across every target that wait would have blocked a deploy to a
        # different target for no reason at all.
        with self._lock_for(target):
            settled = self._settle_the_slot(target, counter, request.build)
            if settled is not None:
                return settled
            note = self._start(request, counter=counter, target=target)
            if isinstance(note, ExecutorAnswer):
                return note
            started, process = note

        # THE COMMAND RUNS OUTSIDE EVERY LOCK. Holding one here would make the
        # slot a queue of requests rather than a slot of one live command, and
        # a takeover could never get in to stop this one.
        return self._wait_for(started, process, request)

    # -- the slot ----------------------------------------------------------

    def _settle_the_slot(
        self, target: str, counter: int, build: str
    ) -> ExecutorAnswer | None:
        """``None`` = the slot is this request's. Anything else is a refusal."""
        if self._folder_problem:
            return ExecutorAnswer(
                accepted=False,
                word="the-executors-notes-folder-cannot-be-used",
                sentence=f"{self._folder_problem}. Nothing was deployed.",
            )
        refused = self._cannot_tell.get(target)
        if refused:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=f"{refused} Nothing was deployed.",
            )
        note, unreadable = self._read_note(target)
        if note is None:
            # RULE (f). MISSING and unreadable are the SAME case, and this is
            # the hole the stage's reviewer drove through: a note that is
            # absent used to fall straight through to "accepted", so a deploy
            # command whose note had been removed — or a helper that came back
            # onto an empty notes folder — had a second command started beside
            # it. The executor cannot tell a note it never wrote from a note it
            # has lost, so it does not try: it looks.
            why = unreadable or (
                f"this executor has no note of its own for {target}"
            )
            return self._slot_with_no_note(target, counter, build, why=why)

        highest = self._highest(note)
        if counter < highest:
            return ExecutorAnswer(
                accepted=False,
                word="the-counter-has-moved-on",
                sentence=(
                    f"this deploy request carries {target}'s counter {counter} "
                    f"and {highest} has already been accepted for it: the "
                    "worker that made this request no longer owns the target, "
                    "so nothing was deployed."
                ),
            )
        if counter == highest:
            # AN EQUAL COUNTER FROM A DIFFERENT BUILD IS REFUSED (rule a). The
            # build it was granted to is remembered whether or not that
            # build's command is still running, because a second build
            # presenting the first build's counter a moment after it finished
            # is presenting somebody else's authority just the same.
            granted_to = note.build if note.counter == counter else note.highest_build
            if granted_to and granted_to != build:
                return ExecutorAnswer(
                    accepted=False,
                    word="that-counter-belongs-to-another-build",
                    sentence=(
                        f"this deploy request carries {target}'s counter "
                        f"{counter} for build {build}, and that counter was "
                        f"granted to build {granted_to}: one counter belongs "
                        "to one build, so nothing was deployed."
                    ),
                )

        if not note.has_a_command:
            # NO COMMAND ON THE NOTE — and the process table is read again
            # before that is believed, because a note is a record of what this
            # executor did and the process table is what is true.
            return self._nothing_is_alive_after_all(target)

        alive = self._alive(note)
        if alive is None:
            self._cannot_tell[target] = (
                f"whether the deploy command recorded for {target} is still "
                "alive could not be told"
            )
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=(
                    f"whether the deploy command already recorded for {target} "
                    "is still alive could not be told, so nothing new was "
                    "started and nothing was deployed."
                ),
            )
        if alive is False:
            self._clear_if_it_is_still_mine(note)
            return self._nothing_is_alive_after_all(target)

        # A COMMAND IS ALIVE. Nothing new starts beside it, ever.
        if not note.group:
            # It is a command that was being STARTED and whose group this
            # executor never got to write down (it stopped in that window).
            # There is nothing to stop by group, so nothing new starts.
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-is-occupied",
                sentence=(
                    f"a deploy command for {target} was being started when this "
                    "executor's note was last written, and a process carrying "
                    "its marker is still alive, so the slot is occupied and "
                    "nothing was deployed."
                ),
                marker=note.marker,
            )
        if counter == note.counter and build == note.build:
            return ExecutorAnswer(
                accepted=False,
                word="a-command-is-already-running",
                sentence=(
                    f"a deploy command for {target} from this same build is "
                    "already running, so a second one was not started."
                ),
                marker=note.marker,
            )
        # SAID ON THE NOTE BEFORE IT IS DONE. The stop is what wakes the old
        # command's waiter, and the waiter's one question is whether the note
        # is still its own.
        superseded = self._mark_superseded(note)
        if superseded is None:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=(
                    f"the note for {target} changed while this request was "
                    "being settled, so nothing was stopped and nothing was "
                    "deployed."
                ),
                marker=note.marker,
            )
        stopped, why = self._stop_and_confirm(superseded)
        if not stopped:
            return ExecutorAnswer(
                accepted=False,
                word="the-old-command-could-not-be-confirmed-stopped",
                sentence=f"{why}. The result stays published, deployment pending.",
                marker=note.marker,
            )
        logger.warning(
            "deploy executor: %s's earlier deploy command (counter %s, build "
            "%s) was stopped and confirmed gone before counter %s was "
            "accepted",
            target,
            note.counter,
            note.build,
            counter,
        )
        self._clear_if_it_is_still_mine(superseded)
        return self._nothing_is_alive_after_all(target)

    def _nothing_is_alive_after_all(self, target: str) -> ExecutorAnswer | None:
        """Look at the process table again before answering "the slot is empty".

        ``None`` = nothing of a deploy for this target is alive, so the slot is
        this request's. Every other answer is a refusal.

        THIS IS THE LAST CLAUSE OF THE BLOCKER'S CURE. A note says what this
        executor did; the process table says what is true. Every path that has
        just changed a note — cleared a command that had ended, or cleared one
        it stopped and confirmed — asks the kernel once more before it lets a
        second command anywhere near the target.
        """
        anything = self._anything_for(target)
        if anything is None:
            self._cannot_tell[target] = (
                f"whether anything is still deploying to {target} could not be "
                "told"
            )
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=(
                    f"whether anything is still deploying to {target} could not "
                    "be told, so nothing new was started and nothing was "
                    "deployed."
                ),
            )
        if anything is True:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-is-occupied",
                sentence=(
                    f"this executor's note for {target} has no live command on "
                    "it, and a process carrying a deploy marker for the target "
                    "is still alive, so the slot is occupied and nothing was "
                    "deployed."
                ),
            )
        return None

    def _slot_with_no_note(
        self,
        target: str,
        counter: int,
        build: str,
        *,
        why: str,
    ) -> ExecutorAnswer | None:
        """RULE (f) whole: a note that is not there is not an empty slot.

        The order is the design's. Look for a live deploy marker first, because
        that is the question with a fact behind it and the one that stops a
        second command starting beside a live one. Only when nothing is alive
        does ownership come up at all, and then it is the COORDINATOR that
        answers it, ALWAYS, and never this request.

        WHAT CHANGED ON 24 SEPTEMBER 2026, and why the last branch is gone.
        This used to have one more step: with nobody to ask, it read the
        request's own ``something_is_running`` and — when that said nothing was
        running — accepted the request as a target's first deploy. A reviewer
        drove the hole straight through it. Build B completed at counter 2, the
        note was deleted, the executor restarted, and build A's DELAYED counter-1
        request arrived carrying a ``something_is_running`` that had been true
        when it was made and was stale by the time it landed. It was accepted,
        and it replaced B.

        The mistake was not the value; it was the source. **A request cannot
        establish its own freshness.** Anything it says about the target was
        true when the request was made, and the whole reason this path exists is
        that time has passed since. So the request is no longer consulted about
        the target at all, on any path, and the coordinator's own read-only
        answer is required — for the first deployment of a target exactly as for
        every other one. With nobody to ask, the deploy is refused.

        THE COST, SAID PLAINLY. A coordinator with no such route configured can
        deploy nothing once a note is missing, including the very first deploy
        of a target. That is the point: the alternative is a path on which a
        stale request lets itself in, which was measured, not imagined. The
        route is named in the rollout as the thing that has to exist first.
        """
        anything = self._anything_for(target)
        if anything is None:
            self._cannot_tell[target] = (
                f"{why}, and whether anything is still deploying to {target} "
                "could not be told either"
            )
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=(
                    f"{why}, and whether anything is still deploying to "
                    f"{target} could not be told either, so deploys for it are "
                    "refused until somebody looks. Nothing was deployed."
                ),
            )
        if anything is True:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-is-occupied",
                sentence=(
                    f"{why}, and a process carrying a deploy marker for "
                    f"{target} is still alive, so the slot is occupied and "
                    "nothing was deployed."
                ),
            )
        # Nothing is alive. The counter and the owning build are CONFIRMED
        # WITH THE COORDINATOR — always, including a target's first deploy, and
        # never from this request, because a delayed request presenting an old
        # counter cannot establish who owns the target or when it last did.
        return self._confirm_with_the_coordinator(target, counter, build, why=why)

    def _confirm_with_the_coordinator(
        self, target: str, counter: int, build: str, *, why: str = ""
    ) -> ExecutorAnswer | None:
        """Rule (f)'s last clause: ask the COORDINATOR who owns this target.

        The request cannot establish it, and since 23 September 2026 it is not
        asked to: this runs on EVERY note-less request, a target's first
        deployment included. A delayed request carries whatever counter — and
        whatever claim about the target — it was made with, and with the
        executor's notes gone there is nothing here to compare either against.
        So the question goes to the coordinator's own read-only answer, and the
        request is accepted only if the coordinator says the same counter and
        the same build, right now.
        """
        opening = why or f"the executor's notes for {target} are gone"
        if self._ask is None:
            return ExecutorAnswer(
                accepted=False,
                word="nobody-can-be-asked-who-owns-it",
                sentence=(
                    f"{opening}, and it has no way to ask the coordinator who "
                    "owns the target, so it cannot accept a counter on this "
                    "request's word alone — not even for a first deployment. "
                    "Nothing was deployed."
                ),
            )
        try:
            answer = self._ask(target)
        except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
            answer = None
            logger.warning(
                "deploy executor: the coordinator could not be asked who owns "
                "%s (%s: %s)",
                target,
                type(exc).__name__,
                exc,
            )
        if not isinstance(answer, Mapping):
            return ExecutorAnswer(
                accepted=False,
                word="nobody-can-be-asked-who-owns-it",
                sentence=(
                    f"{opening}, and the coordinator did not say who owns the "
                    "target, so nothing was deployed."
                ),
            )
        try:
            says_counter = int(answer.get("counter"))
        except (TypeError, ValueError):
            says_counter = -1
        says_build = str(answer.get("build") or "").strip()
        if says_counter != counter or says_build != str(build).strip():
            return ExecutorAnswer(
                accepted=False,
                word="the-coordinator-says-somebody-else-owns-it",
                sentence=(
                    f"{opening}, so it asked the coordinator who owns the "
                    f"target: the coordinator says counter {says_counter} and "
                    f"build {says_build or 'nobody'}, and this request carries "
                    f"counter {counter} for build {build}. Nothing was "
                    "deployed."
                ),
            )
        logger.warning(
            "deploy executor: %s had no note here; the coordinator confirms "
            "counter %s is build %s's, and nothing is alive, so it is accepted",
            target,
            counter,
            build,
        )
        return None

    # -- starting one ------------------------------------------------------

    @staticmethod
    def _default_spawn(**kwargs: Any) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(**kwargs)  # noqa: S603 — a fixed argument list

    def _child_environment(self, request: DeployRequest) -> dict[str, str]:
        """RULE (h): the named list, never a copy of this process's own.

        The list is the factory's own, plus the memory name recorded for this
        build and the setting NAMES the project itself declared — both carried
        on the request, because the coordinator is the thing that read them off
        the ledger at the commit the work started from. The identity the step
        must deploy is added under the name the PROJECT declared for it.

        AND THOSE TWO NAMES ARE CHECKED HERE TOO (23 September 2026, the
        fourth review of this stage). The request's own setting names used to
        be written straight in. The door in front of this route refuses an
        environment key the project did not declare, and this line then let
        the same key in through the deploy block instead: a review drove a
        setting nobody declared into a live promote, and ``PATH`` — a name
        this factory keeps for itself — over the child's own, which ended the
        step at 127. The request is refused at the door now; a name
        that somehow arrives here anyway is DROPPED rather than passed, which
        is the rule :func:`declared_setting_refusal` already states for every
        other declared name.
        """
        env = build_launch_env(
            parent=self._parent,
            memory_project=request.memory_project,
            declared=request.launch_settings,
        )
        for name, value in (request.extra_env or {}).items():
            if isinstance(name, str) and isinstance(value, str):
                env[name] = value
        if request.env_file:
            env["ENV_FILE"] = str(request.env_file)
        if request.identity and request.identity_setting:
            self._put_declared(
                env,
                str(request.identity_setting),
                str(request.identity),
                what="the identity this step must deploy",
            )
        # AND THE ARTIFACT THAT WAS CHECKED, under the name the project chose
        # for it. Without it the step has to work out what to deploy at the
        # moment it deploys, and that is exactly the window another build gets
        # in through (23 September 2026).
        if request.artifact and request.artifact_setting:
            self._put_declared(
                env,
                str(request.artifact_setting),
                str(request.artifact),
                what="the artifact that was checked",
            )
        return env

    @staticmethod
    def _put_declared(
        env: dict[str, str], name: str, value: str, *, what: str
    ) -> None:
        """Put one project-declared setting in, or drop it and say why.

        The value is never logged: it is the project's, and a setting's value
        is where a secret would be if one were ever put in the wrong place.
        """
        refusal = declared_setting_refusal(name)
        if refusal is not None:
            logger.warning(
                "deploy executor: the name this request asked for %s to be "
                "handed over in is not passed — %s",
                what,
                refusal,
            )
            return
        env[name] = value

    def _start(
        self, request: DeployRequest, *, counter: int, target: str
    ) -> tuple[_Note, "subprocess.Popen[bytes]"] | ExecutorAnswer:
        """Write the note, start the command, complete the note.

        THE NOTE IS WRITTEN IN TWO PARTS, and the reason is worth saying
        plainly rather than dressing up as "before anything starts". Two of
        the three things that identify a command — its process group and the
        time that group's leader started — do not exist until the command has
        been started, so there is no honest way to write the whole note first.
        What IS written first is the provisional note: the target, the counter,
        the build, the marker and the word "starting". Then the command is
        started, with the marker in its own argument list. Then the same note
        is completed with the group and its start time.

        THE WINDOW IS THE MARKER'S TO COVER. Between the two writes a command
        may exist that the note cannot name by group — so a "starting" note is
        treated as OCCUPIED by this executor and by the reconciler until one of
        them has looked in the process table for that marker and found nothing
        (:meth:`_alive`, :meth:`reconcile`). Every write here is conditional:
        the provisional note is written only onto a slot with no command in it,
        and the completion only onto this command's own provisional note.
        """
        marker = the_marker_for(target)
        program = (
            request.script
            if os.path.dirname(request.script)
            else os.path.join(os.curdir, request.script)
        )
        # THE MARKER TRAVELS IN AN ARGUMENT LIST, not in an environment. A
        # process's environment is never opened here, so the only place a
        # marker can be found again is the argument list the kernel already
        # shows. A tiny runner of the factory's own carries it, starts the
        # project's program with no arguments — which is the contract a
        # project's deploy step is written to — and waits for it, so the marker
        # stays visible for the whole life of the command. Nothing about the
        # PROJECT is assumed by this: the program it runs is the one the
        # project declared.
        argv = [
            sys.executable,
            "-c",
            _RUNNER_SOURCE,
            marker,
            program,
        ]
        env = self._child_environment(request)
        limit = float(request.timeout or self._command_seconds)
        limit = min(limit, self._command_seconds) if limit > 0 else self._command_seconds
        # 1. THE PROVISIONAL NOTE, before anything is started.
        provisional = _Note(
            target=target,
            counter=counter,
            build=str(request.build),
            marker=marker,
            group=0,
            started_at=None,
            started_wall=time.time(),
            limit=limit,
            highest_counter=counter,
            highest_build=str(request.build),
            phase=_STARTING,
        )
        try:
            claimed = self._claim_the_slot(provisional)
        except OSError as exc:
            return self._the_note_would_not_be_written(target, marker, str(exc))
        if not claimed:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-is-occupied",
                sentence=(
                    f"the slot for {target} was taken between this request "
                    "being settled and its deploy command being started, so "
                    "nothing was deployed."
                ),
                marker=marker,
            )
        try:
            process = self._spawn(
                args=argv,
                cwd=str(request.cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
            # Nothing was started, so the provisional note is taken back — and
            # only if it is still this request's own.
            self._clear_if_it_is_still_mine(provisional)
            return ExecutorAnswer(
                accepted=False,
                word="the-deploy-command-would-not-start",
                sentence=(
                    f"the deploy step for {target} would not start "
                    f"({type(exc).__name__}: {exc}), so nothing was deployed."
                ),
                marker=marker,
            )
        # 2. THE COMMAND IS STARTED, so its identity exists. 3. The note is
        # completed with it, onto this command's own provisional note.
        group = getattr(process, "pid", 0) or 0
        note = replace(
            provisional,
            group=int(group),
            started_at=self._table.started_at(int(group)),
            phase=_RUNNING,
        )
        try:
            completed = self._complete_the_note(provisional, note)
        except OSError as exc:
            # The note could not be written, so the executor would not be able
            # to find this command again. Nothing is left running that it
            # cannot account for.
            self._kill_now(int(group))
            self._clear_if_it_is_still_mine(provisional)
            return self._the_note_would_not_be_written(target, marker, str(exc))
        if not completed:
            # Somebody else's note is on disk already, which means this
            # command's slot is not this command's any more. It is stopped
            # rather than left running beside whatever now holds the target.
            self._kill_now(int(group))
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-is-occupied",
                sentence=(
                    f"the slot for {target} was taken while this deploy command "
                    "was being started, so it was stopped and nothing was "
                    "deployed."
                ),
                marker=marker,
            )
        return note, process

    @staticmethod
    def _the_note_would_not_be_written(
        target: str, marker: str, why: str
    ) -> ExecutorAnswer:
        return ExecutorAnswer(
            accepted=False,
            word="the-note-could-not-be-written",
            sentence=(
                f"the executor could not write down that it was about to "
                f"deploy to {target} ({why}), so it started nothing it could "
                "not account for and nothing was deployed."
            ),
            marker=marker,
        )

    @staticmethod
    def _kill_now(group: int) -> None:
        # Never signal group 0 (this process's own group) or our own group: a
        # spawn seam that answers with no pid would otherwise turn this into
        # the deploy sidecar killing itself (23 September 2026, the hazard a
        # reviewer met with a stubbed spawn). _stop_confirmed guards the same.
        if not group or group == os.getpgrp():
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return

    def _wait_for(
        self,
        note: _Note,
        process: "subprocess.Popen[bytes]",
        request: DeployRequest,
    ) -> ExecutorAnswer:
        """Wait for the command, under its hard limit, then confirm it is gone.

        A WAITER WHOSE COMMAND HAS BEEN SUPERSEDED WRITES NOTHING AND STOPS.
        Everything below that touches a note is conditional on the note on
        disk still being this command's, and the answer for a command a
        takeover stopped is its own word — never "the deploy command ran".
        """
        timed_out = False
        try:
            output_bytes, _ = process.communicate(timeout=note.limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            if not self._the_note_is_still_mine(note):
                return self._stopped_by_a_takeover(note)
            self._stop_and_confirm(note)
            try:
                output_bytes, _ = process.communicate(timeout=5.0)
            except Exception:  # noqa: BLE001 — the answer matters, not the pipe
                output_bytes = b""
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            if not self._the_note_is_still_mine(note):
                return self._stopped_by_a_takeover(note)
            self._stop_and_confirm(note)
            self._clear_if_it_is_still_mine(note)
            return ExecutorAnswer(
                accepted=True,
                word="the-deploy-command-ended-badly",
                sentence=(
                    f"the deploy step for {note.target} ended in an error "
                    f"({type(exc).__name__}: {exc})."
                ),
                exit_code=1,
                marker=note.marker,
            )
        output = (output_bytes or b"").decode("utf-8", errors="replace")
        # HAS THIS COMMAND BEEN TAKEN OVER? Asked before anything is stopped,
        # cleared or answered. A takeover stops this command's whole process
        # group and CONFIRMS every process of it gone before it writes its own
        # note, so a note that is no longer this one's means this command was
        # stopped and there is nothing of it left to stop. It writes nothing —
        # the note on disk belongs to the command that holds the target now —
        # and it answers in its own word.
        if not self._the_note_is_still_mine(note):
            return self._stopped_by_a_takeover(note, output=output)
        # RULE (d): the note is cleared only after every process of the command
        # is confirmed gone — not when the one we spawned returned.
        #
        # AND THIS STOPS THE WHOLE GROUP, on the ordinary success path too.
        # That is a contract on every project's deploy step and it is named in
        # this module's own docstring: the slot is the life of the process
        # GROUP, so anything the step leaves running in its own group is
        # stopped when the step returns. A step must finish its changes before
        # it returns, and what it leaves running must live somewhere of its
        # own — its own session, a service manager, a container. The
        # alternative is worse: a slot released while something of the command
        # is still alive is the very thing rule (b) exists to prevent.
        gone, why = self._stop_and_confirm(note)
        if gone:
            # CONDITIONAL, and it is the whole of the blocker's cure: if a
            # takeover got in while this command was being stopped, the note on
            # disk is the successor's and nothing is written.
            self._clear_if_it_is_still_mine(note)
        else:
            logger.error(
                "deploy executor: %s's deploy command has returned and "
                "something in its process group is still alive (%s) — the "
                "slot stays occupied",
                note.target,
                why,
            )
        if timed_out:
            return ExecutorAnswer(
                accepted=True,
                word="the-deploy-command-ran-out-of-time",
                sentence=(
                    f"the deploy step for {note.target} was given "
                    f"{note.limit:.0f}s and had not finished, so it was "
                    "stopped."
                ),
                exit_code=124,
                output=output,
                marker=note.marker,
            )
        code = process.returncode if process.returncode is not None else 1
        return ExecutorAnswer(
            accepted=True,
            word="the-deploy-command-ran",
            sentence=(
                f"the deploy step for {note.target} ran and ended with "
                f"{code}."
            ),
            exit_code=int(code),
            output=output,
            marker=note.marker,
        )

    @staticmethod
    def _stopped_by_a_takeover(note: _Note, *, output: str = "") -> ExecutorAnswer:
        """A command a later holder of the target stopped. IT IS NOT A DEPLOY.

        It gets its own word because it is its own thing. The command did not
        run to an end: it was stopped part-way, and what it left on the target
        is whatever it had got to. Reported as "the deploy command ran" — with
        the signal that stopped it dressed up as the step's own exit code, which
        is what this used to do — it reads as a deploy that happened, and the
        press would go on to compare identities and write results about a
        deploy that never finished. Whatever reads this must treat it as NO
        DEPLOY: nothing was deployed by this request, and what is running on
        the target belongs to the holder that took over.
        """
        return ExecutorAnswer(
            accepted=False,
            word=STOPPED_BY_A_TAKEOVER,
            sentence=(
                f"the deploy command for {note.target} from build {note.build} "
                f"(the target's counter {note.counter}) was stopped part-way by "
                "a later holder of the target, so it did not run to an end and "
                "nothing was deployed by this request. What is running on "
                f"{note.target} belongs to the holder that took it over."
            ),
            output=output,
            marker=note.marker,
        )


#: The factory's own tiny runner. It exists for ONE reason: to carry the
#: marker somewhere the kernel will show without anybody opening a process's
#: environment. It starts the program it is given with NO arguments, which is
#: the contract every project's deploy step is written to, waits for it, and
#: ends with the same code. It reads nothing, decides nothing and knows
#: nothing about what it is starting.
_RUNNER_SOURCE: str = (
    "import subprocess,sys\n"
    "# sys.argv[1] is the marker; it is here so the kernel's own process\n"
    "# table shows it. sys.argv[2] is the program the project declared.\n"
    "sys.exit(subprocess.run([sys.argv[2]]).returncode)\n"
)


def declared_names(value: Any) -> tuple[str, ...]:
    """The declared setting names off a request body, as text, or ``()``."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(name) for name in value if isinstance(name, str) and name.strip())


def setting_refusal(
    kind: str, name: str, permitted: Sequence[str] | None
) -> str | None:
    """``None`` when a deploy block may name this setting, or why not.

    Two questions, in this order. Is it the shape of a setting name at all,
    and is it one the factory keeps for itself
    (:func:`declared_setting_refusal`)? And then: is it a name THIS PROJECT
    declared? ``permitted`` is the project's own declaration, read off the
    profile by the caller — ``None`` when the caller has no profile to read,
    and then only the first question is asked.
    """
    refusal = declared_setting_refusal(name)
    if refusal is not None:
        return (
            f"this deploy request asks for {kind} to be handed over in a "
            f"setting called {name!r}, and {refusal}. Nothing was deployed."
        )
    if permitted is None:
        return None
    wanted = [str(entry).strip() for entry in permitted if str(entry).strip()]
    if name in wanted:
        return None
    names = ", ".join(sorted(set(wanted))) or "(none — it declares no names)"
    return (
        f"this deploy request asks for {kind} to be handed over in a setting "
        f"called {name!r}, and this project's own deploy profile does not "
        f"declare that name — the names it declares are {names}. Nothing was "
        "deployed."
    )


def request_from(
    ownership: Any,
    *,
    permitted_settings: Sequence[str] | None = None,
    **defaults: Any,
) -> DeployRequest | str:
    """Build a :class:`DeployRequest` off a request's ``deploy`` block.

    Returns the request, or one plain sentence naming the field that is not
    there. The three ownership fields are required on every deploy request;
    everything the command itself needs comes from the caller, which has
    already checked it the way this route checks every other field.

    ``permitted_settings`` are the setting names THE NAMED PROJECT'S OWN
    PROFILE declares for the identity it is handed and the artifact it must
    deploy. They are checked here because this block is the one place a
    caller says what a setting is called, and a name that is not the
    project's is refused before anything starts (23 September 2026, the
    fourth review of this stage: the environment door in front of this route
    was closed and this block was still open, so a live promote ran with a
    setting nobody declared, and with the factory's own ``PATH`` replaced).
    ``None`` means the caller had no profile to read and only the shape and
    the reserved names are checked.
    """
    if not isinstance(ownership, Mapping):
        return (
            "a deploy request has to carry the deployment target it owns, that "
            "target's own counter and the build the counter was granted to"
        )
    target = str(ownership.get("target") or "").strip()
    build = str(ownership.get("build") or "").strip()
    raw_counter = ownership.get("target_counter")
    if not target:
        return "a deploy request has to name the deployment target it owns"
    if not build:
        return f"a deploy request for {target} has to name the build it is for"
    try:
        counter = int(raw_counter)
    except (TypeError, ValueError):
        return f"a deploy request for {target} has to carry that target's counter"
    identity = ownership.get("identity")
    setting = ownership.get("identity_setting")
    artifact = ownership.get("artifact")
    artifact_setting = ownership.get("artifact_setting")
    for kind, raw in (
        ("the identity this step must deploy", setting),
        ("the artifact that was checked", artifact_setting),
    ):
        if not raw:
            continue
        refused = setting_refusal(kind, str(raw).strip(), permitted_settings)
        if refused is not None:
            return refused
    return DeployRequest(
        target=target,
        target_counter=counter,
        build=build,
        cwd=str(defaults.get("cwd") or ""),
        script=str(defaults.get("script") or ""),
        env_file=defaults.get("env_file"),
        extra_env=dict(defaults.get("extra_env") or {}),
        memory_project=defaults.get("memory_project"),
        launch_settings=tuple(defaults.get("launch_settings") or ()),
        timeout=float(defaults.get("timeout") or DEFAULT_COMMAND_SECONDS),
        identity=str(identity).strip() if identity else None,
        identity_setting=str(setting).strip() if setting else None,
        artifact=str(artifact).strip() if artifact else None,
        artifact_setting=(
            str(artifact_setting).strip() if artifact_setting else None
        ),
    )
