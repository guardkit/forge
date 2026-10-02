"""The machine's answers for publication, read from a file a check wrote.

Release -3, TC8 (upgrade runbook of 1 October 2026, section 3, and 2.9).

WHAT THIS IS FOR. Publication is on only if ``publication.enabled`` is true
AND every one of section G's questions holds
(:mod:`forge.pipeline.publication_activation`). Four of those questions, and
half of a fifth, can only be answered by looking at the real machine: whether
a sandbox can reach the coordinator's settings or the ledger, whether a sandbox
can reach the publisher, what is on the publisher's network, and whether the
publisher's credential file can be read by anything but the publisher. Until
now nothing in production supplied those answers, so they were always "nobody
has looked" and publication could never switch on.

``estate-check --publication-facts`` LOOKS — at the sandboxes' workspaces and
the containers inside them, at a probe from inside each running sandbox, at
the publisher's network and at the credential file — and writes what it found
into one small file on a volume mounted read-only into the coordinator. This
module reads that file. It is the ONE reader, and every caller asks it the
same way:

* the merge press (:mod:`forge.pipeline.merge_executor`) calls it on EVERY
  merge word, through ``MergeExecutorDeps.what_the_machine_says``, so facts
  written after the coordinator started turn publication on at the next merge
  word without a restart, and facts that have gone stale turn it off again;
* ``python -m forge.pipeline.publication_status`` calls it the same way, so a
  person can ask what the next merge word would decide.

IT FAILS CLOSED. Anything short of a complete, fresh record written for THIS
coordinator reads as "nobody has looked" — every machine answer ``None`` —
which the activation check refuses, and the reason is said in the sentence a
person reads. In particular:

* no ``FORGE_PUBLICATION_FACTS_FILE`` in the environment (release -2's env,
  and every forge that is not in a containerised estate): the reader returns
  ``None``, which is EXACTLY today's behaviour;
* the file is missing, unreadable, not JSON, or not the shape written here;
* it was written for a different coordinator container — a container belongs
  to one compose project and runs one image, and a new image means a new
  container, so this is how "another project or another image" is caught from
  inside the coordinator, which cannot see either name;
* it was written AT OR BEFORE this coordinator's start, which is taken from
  PID 1 of the container, so every reader in the container judges against
  the same moment and a restart always needs a new look;
* it is older than :data:`DEFAULT_MAX_AGE_SECONDS`, or dated in the future.

Nothing here reads a credential, prints a secret, or names a language, a host
or a product.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from forge.pipeline.publication_activation import WhatTheMachineSays

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "FACTS_FILE_ENV",
    "REFRESH_THE_CHECK",
    "where_the_facts_stand",
    "FACTS_FORMAT",
    "MACHINE_ANSWER_NAMES",
    "ThisCoordinator",
    "process_start",
    "read_publication_facts",
    "the_machine_now",
    "this_coordinator",
]

#: The one environment name that says where the facts are. Only release -3's
#: compose file declares it for the coordinator, and only release -3's env
#: file sets it, so a release -2 env file stays valid and behaves as before.
FACTS_FILE_ENV = "FORGE_PUBLICATION_FACTS_FILE"

#: What the check writes in ``format``; anything else is not this record.
FACTS_FORMAT = "forge-publication-facts/1"

#: How old a record may be and still be acted on. Mounts, networks and
#: sandboxes can change without the coordinator restarting (a new sandbox can
#: be made at any time), so a look has a shelf life. A day is long enough that
#: a check run when the estate is brought up covers the day's merge words, and
#: short enough that a look nobody repeated does not quietly stand for a week.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60

#: A record dated this far ahead of this machine's clock is a clock nobody
#: trusts, not a fresh look. The writer and the reader share one kernel clock,
#: so the allowance is only for rounding.
_FUTURE_ALLOWANCE_SECONDS = 5

#: The machine's answers the record carries, by the names
#: :class:`WhatTheMachineSays` gives them.
MACHINE_ANSWER_NAMES: tuple[str, ...] = (
    "a_sandbox_can_write_the_coordinators_settings_file",
    "a_sandbox_can_see_the_ledger",
    "a_sandbox_can_reach_the_publisher",
    "only_the_coordinator_is_on_the_publishers_network",
    "the_credential_file_can_be_read_by_them",
)

#: What a person reads when the machine check is missing or out of date, and
#: the exact command that refreshes it. Nothing in the estate re-runs the
#: check by itself: it needs the host's Docker, the sandbox daemon and root on
#: the host to read the publisher's kernel policy, and no container holds all
#: three, deliberately. So the merge word says so plainly instead.
REFRESH_THE_CHECK = (
    "The machine check is out of date: publication needs a look at this machine "
    "taken after the coordinator last started and less than a day ago. Refresh "
    "it on the host, from the estate bundle, with: "
    "deploy/estate/estate-check --env-file <the estate's env file> --publication-facts"
)

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID_IN_A_PATH = re.compile(r"/containers/([0-9a-f]{64})/")


@dataclass(frozen=True)
class ThisCoordinator:
    """Which coordinator is asking, and exactly which start of it.

    ``container_id`` is the full id of the container this process runs in,
    or ``None`` when that cannot be told (not in a container at all).

    THE START IDENTITY is PID 1 of that container as the kernel counts it:
    ``boot_time_epoch`` (``btime`` in ``/proc/stat``, whole seconds),
    ``start_ticks`` (field 22 of ``/proc/1/stat``, clock ticks since boot) and
    ``ticks_per_second``. These are integers the kernel keeps, and the check
    that writes the facts reads the very same three for the same process from
    the host, so a record is bound to ONE start by exact equality — no
    tolerance is needed, and none is allowed. Any restart gives PID 1 a new
    start-tick count; a stepped wall clock changes ``btime`` and so refuses
    too, which is the safe side. ``started_at_epoch`` is the same moment in
    seconds, derived from the three. Any of them ``None`` means it could not
    be read.
    """

    container_id: str | None
    started_at_epoch: float | None
    boot_time_epoch: int | None = None
    start_ticks: int | None = None
    ticks_per_second: int | None = None

    @property
    def start_identity(self) -> tuple[int, int, int] | None:
        parts = (self.boot_time_epoch, self.start_ticks, self.ticks_per_second)
        if any(not _a_positive_integer(part) for part in parts):
            return None
        return parts  # type: ignore[return-value]


def _a_positive_integer(value: Any) -> bool:
    """A strictly positive int, and not a bool, float, NaN, inf or string."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _the_container_id(mountinfo: str) -> str | None:
    """The container id, read off the files Docker mounts into every container.

    Docker bind-mounts ``/etc/hostname``, ``/etc/hosts`` and
    ``/etc/resolv.conf`` from ``<data-root>/containers/<id>/``, and those
    mounts are listed in ``/proc/self/mountinfo`` whatever the hostname was
    set to. One id, found consistently, is the answer; none, or two different
    ones, is "cannot be told".
    """
    found = {match.group(1) for match in _CONTAINER_ID_IN_A_PATH.finditer(mountinfo)}
    if len(found) != 1:
        return None
    return next(iter(found))


def process_start(proc: Path, pid: str = "1") -> tuple[int, int, int] | None:
    """(boot time, start ticks, ticks per second) of one process. Never raises."""
    try:
        stat = (proc / pid / "stat").read_text(encoding="utf-8")
        # The command name is in brackets and may itself contain spaces or
        # brackets, so the fields are counted from after the LAST ')'.
        after = stat[stat.rindex(")") + 2 :].split()
        # Field 22 overall is the start time; after the name, field 3 is the
        # first, so field 22 is index 19 here.
        ticks = int(after[19])
        boot = None
        for line in (proc / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
        per_second = int(os.sysconf("SC_CLK_TCK"))
        if boot is None or ticks <= 0 or boot <= 0 or per_second <= 0:
            return None
        return boot, ticks, per_second
    except (OSError, ValueError, IndexError):
        return None


def this_coordinator(proc: Path = Path("/proc"), pid: str = "1") -> ThisCoordinator:
    """Who is asking: this container's id, and its PID 1's start. Never raises."""
    try:
        mountinfo = (proc / "self" / "mountinfo").read_text(encoding="utf-8")
    except OSError:
        mountinfo = ""
    start = process_start(proc, pid)
    if start is None:
        return ThisCoordinator(_the_container_id(mountinfo), None)
    boot, ticks, per_second = start
    return ThisCoordinator(
        container_id=_the_container_id(mountinfo),
        started_at_epoch=boot + ticks / per_second,
        boot_time_epoch=boot,
        start_ticks=ticks,
        ticks_per_second=per_second,
    )


def _refuse_a_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number this record may carry")


def _nobody_has_looked(why: str) -> WhatTheMachineSays:
    """Every machine answer unknown, and the reason said."""
    return WhatTheMachineSays(
        why_nobody_has_looked=f"nobody has looked at this machine for this coordinator: {why}"
    )


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def read_publication_facts(
    *,
    environ: Mapping[str, str] | None = None,
    who_is_asking: Callable[[], ThisCoordinator] = this_coordinator,
    now: Callable[[], float] = time.time,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> WhatTheMachineSays | None:
    """What the last look at the machine found, if it may be acted on now.

    Returns ``None`` when ``FORGE_PUBLICATION_FACTS_FILE`` is not set, which
    is exactly what every production caller passed before release -3. In
    every other case it returns a :class:`WhatTheMachineSays`: the recorded
    answers when the record is complete, fresh and for this coordinator, and
    otherwise every answer ``None`` with the reason in
    ``why_nobody_has_looked``. Never raises.

    It is called with no arguments by the merge press, once per merge word;
    the keyword arguments exist so that a test can say who is asking and
    what time it is.
    """
    env = os.environ if environ is None else environ
    named = str(env.get(FACTS_FILE_ENV, "") or "").strip()
    if not named:
        return None
    path = Path(named)
    try:
        return _read(path, who_is_asking(), now(), max_age_seconds)
    except Exception as exc:  # noqa: BLE001 - a reader that broke has not looked
        return _nobody_has_looked(
            f"the publication facts at {path} could not be read "
            f"({type(exc).__name__}), so nobody has looked as far as this "
            "coordinator can tell"
        )


def _read(
    path: Path, asking: ThisCoordinator, now: float, max_age_seconds: int
) -> WhatTheMachineSays:
    rerun = REFRESH_THE_CHECK
    if not path.is_file():
        return _nobody_has_looked(
            f"there is no publication facts file at {path}: nobody has run "
            f"the check that looks at the machine for this coordinator. {rerun}"
        )
    try:
        record = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_refuse_a_constant
        )
    except (OSError, UnicodeDecodeError) as exc:
        return _nobody_has_looked(
            f"the publication facts file at {path} could not be read "
            f"({type(exc).__name__}). {rerun}"
        )
    except ValueError:
        return _nobody_has_looked(
            f"the publication facts file at {path} is not one complete JSON "
            f"record, so it says nothing. {rerun}"
        )
    problem = _what_is_wrong_with(record)
    if problem:
        return _nobody_has_looked(
            f"the publication facts file at {path} is not a record this "
            f"coordinator can act on: {problem}. {rerun}"
        )

    written = record["written_at_epoch"]
    if asking.container_id is None:
        return _nobody_has_looked(
            "this process could not tell which container it is running in, "
            "so publication facts written for a coordinator container cannot "
            "be shown to be about it"
        )
    if record["coordinator_container_id"] != asking.container_id:
        return _nobody_has_looked(
            f"the publication facts at {path} were written for the "
            f"coordinator container {record['coordinator_container_id'][:12]} "
            f"and this is {asking.container_id[:12]}: a different container "
            f"is a different estate or image. {rerun}"
        )
    identity = asking.start_identity
    if identity is None or asking.started_at_epoch is None:
        return _nobody_has_looked(
            "this coordinator's start time could not be read, so the "
            "publication facts cannot be shown to be about this start of it"
        )
    recorded = record["coordinator_pid1_start"]
    recorded_identity = (
        recorded["boot_time_epoch"],
        recorded["start_ticks"],
        recorded["ticks_per_second"],
    )
    if recorded_identity != identity:
        return _nobody_has_looked(
            f"the publication facts at {path} were written for a start of this "
            "coordinator that is not the one running now (it has restarted "
            f"since, or the machine's clock was stepped). {rerun}"
        )
    if written <= asking.started_at_epoch:
        return _nobody_has_looked(
            f"the publication facts at {path} were written at "
            f"{_when(written)}, at or before this coordinator started "
            f"({_when(asking.started_at_epoch)}), so they say nothing about "
            f"the coordinator that is running. {rerun}"
        )
    age = now - written
    if age < -_FUTURE_ALLOWANCE_SECONDS:
        return _nobody_has_looked(
            f"the publication facts at {path} are dated {_when(written)}, "
            "which is in the future of this machine's clock, so their age "
            f"cannot be trusted. {rerun}"
        )
    if age > max_age_seconds:
        return _nobody_has_looked(
            f"the publication facts at {path} were written at "
            f"{_when(written)}, {int(age)} seconds ago, and a look at the "
            f"machine is acted on for {max_age_seconds} seconds at most. {rerun}"
        )
    machine = record["machine"]
    return WhatTheMachineSays(
        **{name: machine[name] for name in MACHINE_ANSWER_NAMES},
        looked_at_by=(
            f"estate-check --publication-facts, written {record['written_at']} "
            f"for coordinator container {record['coordinator_container_id'][:12]}"
        ),
    )


def _what_is_wrong_with(record: Any) -> str | None:
    """Why this is not the record the check writes, or ``None`` if it is."""
    if not isinstance(record, dict):
        return "it is not a JSON object"
    if record.get("format") != FACTS_FORMAT:
        return f"its format is {record.get('format')!r}, not {FACTS_FORMAT!r}"
    if not _a_positive_integer(record.get("written_at_epoch")):
        return "it carries no time it was written, as a whole number of seconds"
    start = record.get("coordinator_pid1_start")
    if not isinstance(start, dict) or not all(
        _a_positive_integer(start.get(name))
        for name in ("boot_time_epoch", "start_ticks", "ticks_per_second")
    ):
        return "it does not say which start of the coordinator it was written for"
    if not isinstance(record.get("written_at"), str):
        return "it carries no readable time it was written"
    container = record.get("coordinator_container_id")
    if not isinstance(container, str) or not _CONTAINER_ID.match(container):
        return "it names no coordinator container"
    machine = record.get("machine")
    if not isinstance(machine, dict):
        return "it carries no machine answers"
    if set(machine) != set(MACHINE_ANSWER_NAMES):
        return "its machine answers are not exactly the five the check gives"
    for name in MACHINE_ANSWER_NAMES:
        if machine[name] is not None and not isinstance(machine[name], bool):
            return f"its answer to {name} is not true, false or unknown"
    return None


def the_machine_now(value: Any) -> WhatTheMachineSays | None:
    """Resolve ``what_the_machine_says`` for ONE question. Never raises.

    ``MergeExecutorDeps.what_the_machine_says`` is built once, when the
    coordinator starts, and may be a plain value (a test's stand-in, or
    ``None``) or a zero-argument reader such as :func:`read_publication_facts`.
    A reader is called here, afresh, each time — which is what makes the
    merge press read the facts on every merge word rather than once at boot.
    A reader that raises has not looked, and says so.
    """
    if value is None or isinstance(value, WhatTheMachineSays):
        return value
    if callable(value):
        try:
            answer = value()
        except Exception as exc:  # noqa: BLE001 - a reader that broke has not looked
            return _nobody_has_looked(
                f"reading the machine's answers failed ({type(exc).__name__})"
            )
        if answer is None or isinstance(answer, WhatTheMachineSays):
            return answer
        return _nobody_has_looked(
            "reading the machine's answers gave something that is not an answer"
        )
    return _nobody_has_looked(
        "the machine's answers were given in a shape nothing reads"
    )


def where_the_facts_stand(
    *,
    environ: Mapping[str, str] | None = None,
    who_is_asking: Callable[[], ThisCoordinator] = this_coordinator,
    now: Callable[[], float] = time.time,
) -> str:
    """ONE plain line saying whether the machine's answers are there and fresh.

    Said at the coordinator's boot beside the publication line, so whoever
    started it learns at once that the check has to be run again — a record
    can never be newer than a start that came after it. Never raises.
    """
    try:
        said = read_publication_facts(
            environ=environ, who_is_asking=who_is_asking, now=now
        )
    except Exception as exc:  # noqa: BLE001 - a boot line never stops a boot
        return f"publication facts: could not be read ({type(exc).__name__})"
    if said is None:
        return (
            f"publication facts: none are configured ({FACTS_FILE_ENV} is not "
            "set), so nobody's look at this machine is read and publication "
            "stays off"
        )
    if said.why_nobody_has_looked:
        return f"publication facts: not usable — {said.why_nobody_has_looked}"
    return f"publication facts: present and fresh ({said.looked_at_by})"
