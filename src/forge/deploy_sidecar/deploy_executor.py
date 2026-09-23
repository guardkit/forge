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
e. **On its own start the executor accepts nothing until it has reconciled.**
   For each target with a note of an active command it looks for survivors BY
   IDENTITY. Alive ⇒ the slot is OCCUPIED, not empty: the command is adopted
   and treated exactly as if the executor had never restarted. None alive ⇒
   the note is cleared, and the next holder reads what is running before it
   acts.
f. **Notes missing or unreadable is not an empty slot.** The executor looks
   for any process carrying a deploy marker for that target; found ⇒ occupied;
   cannot tell ⇒ deploys for that target are refused and the refusal says why.
   When it can show that nothing is alive, it CONFIRMS the target's current
   counter and owning build **with the coordinator** — a question put to the
   coordinator's own read-only answer, not the counter the delayed request
   presents, because a delayed request presenting an old counter cannot
   establish who owns the target. (This is the point the design's sign-off
   note asks reviewers to watch.)
g. **Every deploy command gets a hard time limit**, and the deploy step's
   lease in the ledger is longer than it, so a healthy deploy is not taken
   over.
h. **The environment door.** The child's environment is built from the named
   list, plus the recorded memory name and the project's own declared setting
   names carried on the request. It is never a copy of this process's own.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from forge.launch_environment import build_launch_env

logger = logging.getLogger(__name__)

__all__ = [
    "DEPLOY_MARKER_PREFIX",
    "DEFAULT_COMMAND_SECONDS",
    "DEFAULT_STOP_CONFIRM_SECONDS",
    "DeployExecutor",
    "DeployRequest",
    "ExecutorAnswer",
    "ProcessTable",
    "declared_names",
    "request_from",
    "the_marker_for",
]


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
    parts = str(marker).split(":")
    return parts[1] if len(parts) > 2 else ""


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
        }


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
        # One lock across every target, held only while the slot is being
        # decided — never while a command runs. Two requests for DIFFERENT
        # targets therefore do not wait on each other for longer than it takes
        # to read and write a note.
        self._gate = threading.Lock()
        #: Targets whose notes could not be read and whose slot could not be
        #: settled. Deploys for these are refused until somebody looks.
        self._cannot_tell: dict[str, str] = {}
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

    def _write_note(self, note: _Note) -> None:
        """Whole, or not at all: written beside and moved into place."""
        path = self._note_path(note.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        temporary.write_text(
            json.dumps(note.to_wire(), sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _clear_note(self, note: _Note) -> None:
        """Clear the ACTIVE command, and keep the highest counter for ever.

        The note file stays, with no live command on it, because the highest
        counter accepted for this target has to survive: it is what refuses a
        delayed request that presents an older one.
        """
        highest = max(note.highest_counter, note.counter)
        kept = _Note(
            target=note.target,
            counter=0,
            build="",
            marker="",
            group=0,
            started_at=None,
            started_wall=0.0,
            limit=note.limit,
            highest_counter=highest,
            highest_build=(
                note.build if note.counter >= note.highest_counter else note.highest_build
            ),
        )
        self._write_note(kept)

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
        """
        if not note.group:
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
            logger.error(
                "deploy executor: its own notes folder %s could not be read "
                "(%s) — every deploy is refused until somebody looks",
                self._root,
                exc,
            )
            self._reconciled = True
            return {"*": f"the executor's notes folder could not be read ({exc})"}
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
            if note is None or not note.group:
                settled[path.stem] = "no active command"
                continue
            alive = self._alive(note)
            if alive is True:
                settled[note.target] = "occupied (adopted)"
            elif alive is None:
                settled[note.target] = "cannot tell"
                self._cannot_tell[note.target] = (
                    f"whether the deploy command recorded for {note.target} is "
                    "still alive could not be told, so deploys for it are "
                    "refused until somebody looks"
                )
            else:
                self._clear_note(note)
                settled[note.target] = "cleared"
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

        with self._gate:
            settled = self._settle_the_slot(target, counter, request.build)
            if settled is not None:
                return settled
            note = self._start(request, counter=counter, target=target)
            if isinstance(note, ExecutorAnswer):
                return note
            started, process = note

        # THE COMMAND RUNS OUTSIDE THE GATE. Holding it here would make the
        # slot a queue of requests rather than a slot of one live command,
        # and a request for another target would wait on this one.
        return self._wait_for(started, process, request)

    # -- the slot ----------------------------------------------------------

    def _settle_the_slot(
        self, target: str, counter: int, build: str
    ) -> ExecutorAnswer | None:
        """``None`` = the slot is this request's. Anything else is a refusal."""
        refused = self._cannot_tell.get(target)
        if refused:
            return ExecutorAnswer(
                accepted=False,
                word="the-slot-cannot-be-settled",
                sentence=f"{refused} Nothing was deployed.",
            )
        note, unreadable = self._read_note(target)
        if unreadable is not None:
            # RULE (f). A note that cannot be read is NOT an empty slot.
            anything = self._anything_for(target)
            if anything is None:
                self._cannot_tell[target] = (
                    f"{unreadable}, and whether anything is still deploying to "
                    f"{target} could not be told either"
                )
                return ExecutorAnswer(
                    accepted=False,
                    word="the-slot-cannot-be-settled",
                    sentence=(
                        f"{unreadable}, and whether anything is still "
                        f"deploying to {target} could not be told either, so "
                        "deploys for it are refused until somebody looks. "
                        "Nothing was deployed."
                    ),
                )
            if anything is True:
                return ExecutorAnswer(
                    accepted=False,
                    word="the-slot-is-occupied",
                    sentence=(
                        f"{unreadable}, and a process carrying a deploy marker "
                        f"for {target} is still alive, so the slot is occupied "
                        "and nothing was deployed."
                    ),
                )
            # Nothing is alive. The counter and the owning build are CONFIRMED
            # WITH THE COORDINATOR, not taken from this request: a delayed
            # request presenting an old counter cannot establish who owns the
            # target.
            return self._confirm_with_the_coordinator(target, counter, build)

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
        if note is not None and counter == highest:
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

        if note is None or not note.group:
            return None

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
            self._clear_note(note)
            return None

        # A COMMAND IS ALIVE. Nothing new starts beside it, ever.
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
        stopped, why = self._stop_and_confirm(note)
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
        self._clear_note(note)
        return None

    def _confirm_with_the_coordinator(
        self, target: str, counter: int, build: str
    ) -> ExecutorAnswer | None:
        """Rule (f)'s last clause: ask the COORDINATOR who owns this target.

        The request cannot establish it. A delayed request carries whatever
        counter it was made with, and with the executor's notes gone there is
        nothing here to compare it against — so the question goes to the
        coordinator's own read-only answer, and this request is accepted only
        if the coordinator says the same counter and the same build.
        """
        if self._ask is None:
            return ExecutorAnswer(
                accepted=False,
                word="nobody-can-be-asked-who-owns-it",
                sentence=(
                    f"the executor's notes for {target} are gone and it has no "
                    "way to ask the coordinator who owns the target, so it "
                    "cannot accept a counter on this request's word alone. "
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
                    f"the executor's notes for {target} are gone and the "
                    "coordinator did not say who owns the target, so nothing "
                    "was deployed."
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
                    f"the executor's notes for {target} are gone, so it asked "
                    f"the coordinator who owns the target: the coordinator "
                    f"says counter {says_counter} and build "
                    f"{says_build or 'nobody'}, and this request carries "
                    f"counter {counter} for build {build}. Nothing was "
                    "deployed."
                ),
            )
        logger.warning(
            "deploy executor: %s's notes were gone; the coordinator confirms "
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
            env[str(request.identity_setting)] = str(request.identity)
        return env

    def _start(
        self, request: DeployRequest, *, counter: int, target: str
    ) -> tuple[_Note, "subprocess.Popen[bytes]"] | ExecutorAnswer:
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
            return ExecutorAnswer(
                accepted=False,
                word="the-deploy-command-would-not-start",
                sentence=(
                    f"the deploy step for {target} would not start "
                    f"({type(exc).__name__}: {exc}), so nothing was deployed."
                ),
                marker=marker,
            )
        group = getattr(process, "pid", 0) or 0
        note = _Note(
            target=target,
            counter=counter,
            build=str(request.build),
            marker=marker,
            group=int(group),
            started_at=self._table.started_at(int(group)),
            started_wall=time.time(),
            limit=limit,
            highest_counter=counter,
            highest_build=str(request.build),
        )
        # RULE (d): the note is durable BEFORE the command can do anything.
        try:
            self._write_note(note)
        except OSError as exc:
            # The note could not be written, so the executor would not be able
            # to find this command again. Nothing is left running that it
            # cannot account for.
            self._kill_now(int(group))
            return ExecutorAnswer(
                accepted=False,
                word="the-note-could-not-be-written",
                sentence=(
                    f"the executor could not write down that it was about to "
                    f"deploy to {target} ({exc}), so it started nothing it "
                    "could not account for and nothing was deployed."
                ),
                marker=marker,
            )
        return note, process

    @staticmethod
    def _kill_now(group: int) -> None:
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
        """Wait for the command, under its hard limit, then confirm it is gone."""
        timed_out = False
        try:
            output_bytes, _ = process.communicate(timeout=note.limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._stop_and_confirm(note)
            try:
                output_bytes, _ = process.communicate(timeout=5.0)
            except Exception:  # noqa: BLE001 — the answer matters, not the pipe
                output_bytes = b""
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            self._stop_and_confirm(note)
            with self._gate:
                self._clear_note(note)
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
        # RULE (d): the note is cleared only after every process of the command
        # is confirmed gone — not when the one we spawned returned.
        gone, why = self._stop_and_confirm(note)
        with self._gate:
            if gone:
                self._clear_note(note)
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


def request_from(ownership: Any, **defaults: Any) -> DeployRequest | str:
    """Build a :class:`DeployRequest` off a request's ``deploy`` block.

    Returns the request, or one plain sentence naming the field that is not
    there. The three ownership fields are required on every deploy request;
    everything the command itself needs comes from the caller, which has
    already checked it the way this route checks every other field.
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
    )
