"""Publication cannot be switched on until the isolation is proven.

One-true-copy design pass, item 1, third revision section G:

    *The switch that turns publication on runs a check first and refuses, in
    plain words, unless all of these hold. Nothing in the coordinator can
    start a build or a project check inside itself. No sandbox can write the
    coordinator's settings file or see the ledger. No sandbox can reach the
    publisher. The publisher's credential is not in the coordinator's, the
    runner's or any sandbox's settings. The check is run again each time the
    coordinator starts; if it fails, publication goes off and every merge word
    reports "publication is switched off: …" with the reason, never a merge.*

SIX NAMED QUESTIONS, each one testable on its own. They are questions and
not assertions: each is asked of what is true, and each answers **yes**, **no**
or **it cannot be told from here** — and the third is a refusal, because a
wall nobody has looked at is not a wall.

The sixth was added on 22 September 2026, with the way "no sandbox can reach
the publisher" is actually made true. The publisher listens on every address
inside its own container and publishes no port; what can reach it is
therefore exactly what is on its network, so the network is counted, and the
answer publication needs is "the coordinator, and nothing else".

WHICH ONES CAN BE PROVEN ON THIS MACHINE, AND WHICH ONLY AT ROLLOUT. This is
the honest split, and it is written here rather than left to be discovered:

* **provable here, from the settings alone** — question 1 (the setting that
  permits builds inside the coordinator) and the settings half of question 6
  (the credential file is named, and named in no other settings). Both are
  read off a configuration object, so a test can make them true and false at
  will. **A credential file that is not named at all is a refusal**, not a
  pass: with no path there is nothing to search for, and a search that found
  nothing because it looked for nothing is not an all-clear;
* **only at rollout, on the real machine** — questions 2, 3, 4 and 5 (whether
  a sandbox can write the coordinator's settings file, see the ledger or
  reach the publisher, and what else is on the publisher's network) and the
  readability half of question 6. Each of those is a fact about mounts, users
  and networks that no amount of reading a settings file establishes. They
  are asked of :class:`WhatTheMachineSays`, which is the stand-in: a thing
  that reports what somebody looked at. Nothing supplies one today, so today
  the check **refuses**, which is the safe side and is exactly where the
  design says publication stands ("still gated").

WHY IT FAILS CLOSED. An unanswered question is not a pass. If the machine has
told us nothing about a wall, the check says so by name and publication stays
off. It is never possible for publication to switch on because a probe was
missing.

Nothing here names a language, a test runner, a hosting provider or a
product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "THE_QUESTIONS",
    "Answer",
    "TheVerdict",
    "WhatTheMachineSays",
    "run_the_activation_check",
    "what_is_true_here",
]


@dataclass(frozen=True)
class WhatTheMachineSays:
    """What somebody who looked at the machine reports back.

    Every field is ``True``, ``False`` or ``None``. ``None`` means **nobody
    has looked**, and the check treats it as a refusal rather than a pass.
    This is the stand-in the design asks for: at rollout it is filled in by
    looking at the real mounts, users and networks; in a test it is filled in
    by hand, one field at a time, which is how each question is proven to
    refuse on its own.
    """

    a_sandbox_can_write_the_coordinators_settings_file: bool | None = None
    a_sandbox_can_see_the_ledger: bool | None = None
    a_sandbox_can_reach_the_publisher: bool | None = None
    the_credential_file_can_be_read_by_them: bool | None = None
    #: Is the coordinator the ONLY thing on the publisher's network besides
    #: the publisher itself? The publisher listens on every address inside its
    #: own container and publishes no port, so what can reach it is exactly
    #: what shares that network — which makes "who else is on it" the thing
    #: that has to be counted. ``True`` is the answer publication needs.
    only_the_coordinator_is_on_the_publishers_network: bool | None = None

    #: Where the answers came from, for the record. Free text: "looked at the
    #: sandbox's mounts on <machine>, <date>", or "a stand-in, in a test".
    looked_at_by: str | None = None


@dataclass(frozen=True)
class WhatIsTrue:
    """The facts the questions are asked of, gathered in one place."""

    builds_may_run_inside_the_coordinator: bool | None
    the_credential_file: str | None
    where_the_credential_file_is_named: tuple[str, ...]
    machine: WhatTheMachineSays


@dataclass(frozen=True)
class Answer:
    """One question, asked and answered."""

    name: str
    question: str
    holds: bool | None
    said: str
    #: Can this question be settled on this machine, from the settings alone?
    provable_here: bool

    @property
    def refuses(self) -> bool:
        return self.holds is not True

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "question": self.question,
            "holds": self.holds,
            "said": self.said,
            "provable_here": self.provable_here,
        }


@dataclass(frozen=True)
class TheVerdict:
    """What the check found, and the one sentence it says when it refuses."""

    all_hold: bool
    answers: tuple[Answer, ...] = field(default_factory=tuple)

    @property
    def refusals(self) -> tuple[Answer, ...]:
        return tuple(answer for answer in self.answers if answer.refuses)

    @property
    def sentence(self) -> str:
        """Why publication is off, in plain words, naming what is wrong."""
        if self.all_hold:
            return (
                "every one of the things publication needs was checked "
                "and holds"
            )
        said = "; ".join(answer.said for answer in self.refusals)
        return said or "the activation check could not be run"

    def to_wire(self) -> dict[str, Any]:
        return {
            "all_hold": self.all_hold,
            "sentence": self.sentence,
            "answers": [answer.to_wire() for answer in self.answers],
        }


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------


def _nothing_is_built_inside_the_coordinator(true: WhatIsTrue) -> Answer:
    name = "nothing-is-built-inside-the-coordinator"
    question = (
        "can anything in the coordinator start a build, or a project's own "
        "check, inside itself?"
    )
    allowed = true.builds_may_run_inside_the_coordinator
    if allowed is None:
        return Answer(
            name,
            question,
            None,
            (
                "it cannot be told whether the coordinator may build inside "
                "itself: the setting that permits it was not readable"
            ),
            provable_here=True,
        )
    if allowed:
        return Answer(
            name,
            question,
            False,
            (
                "the coordinator may still start builds and project checks "
                "inside itself, and a build that runs in there can write the "
                "very record the publisher trusts. Switch that setting off, "
                "and give every project a sandbox of its own, before "
                "publication is switched on"
            ),
            provable_here=True,
        )
    return Answer(
        name,
        question,
        True,
        "the coordinator refuses to build or check inside itself",
        provable_here=True,
    )


def _machine_question(
    name: str,
    question: str,
    *,
    answer: bool | None,
    when_true: str,
    when_false: str,
    when_unknown: str,
) -> Answer:
    """One of the three questions only the real machine can settle.

    ``answer`` is what somebody who looked reported. ``True`` means the thing
    the question asks about IS possible, which is the wrong way round for
    publication, so it is the refusal.
    """
    if answer is None:
        return Answer(name, question, None, when_unknown, provable_here=False)
    if answer:
        return Answer(name, question, False, when_false, provable_here=False)
    return Answer(name, question, True, when_true, provable_here=False)


def _no_sandbox_can_write_the_settings_file(true: WhatIsTrue) -> Answer:
    return _machine_question(
        "no-sandbox-can-write-the-coordinators-settings-file",
        "can a sandbox write the coordinator's settings file?",
        answer=true.machine.a_sandbox_can_write_the_coordinators_settings_file,
        when_true="no sandbox can write the coordinator's settings file",
        when_false=(
            "a sandbox can write the coordinator's settings file, so a build "
            "in there could change which projects are registered and which "
            "paths are allowed. Move that file out of a sandbox's reach "
            "before publication is switched on"
        ),
        when_unknown=(
            "nobody has looked at whether a sandbox can write the "
            "coordinator's settings file, and an unexamined wall is not a "
            "wall. This one can only be settled on the real machine, at "
            "rollout"
        ),
    )


def _no_sandbox_can_see_the_ledger(true: WhatIsTrue) -> Answer:
    return _machine_question(
        "no-sandbox-can-see-the-ledger",
        "can a sandbox see the ledger?",
        answer=true.machine.a_sandbox_can_see_the_ledger,
        when_true="no sandbox can see the ledger",
        when_false=(
            "a sandbox can see the ledger, which is the record the publisher "
            "trusts; a build in there could forge a passed record. Take the "
            "ledger out of every sandbox's reach before publication is "
            "switched on"
        ),
        when_unknown=(
            "nobody has looked at whether a sandbox can see the ledger. This "
            "one can only be settled on the real machine, at rollout"
        ),
    )


def _no_sandbox_can_reach_the_publisher(true: WhatIsTrue) -> Answer:
    return _machine_question(
        "no-sandbox-can-reach-the-publisher",
        "can a sandbox reach the publisher?",
        answer=true.machine.a_sandbox_can_reach_the_publisher,
        when_true="no sandbox can reach the publisher",
        when_false=(
            "a sandbox can reach the publisher, so a build in there could ask "
            "for a send. Bind the publisher where no sandbox can reach it "
            "before publication is switched on"
        ),
        when_unknown=(
            "nobody has looked at whether a sandbox can reach the publisher. "
            "This one can only be settled on the real machine, at rollout"
        ),
    )


def _the_credential_is_out_of_their_reach(true: WhatIsTrue) -> Answer:
    """The one question with a half that IS provable here.

    The settings half — is the credential file named in the coordinator's
    settings, the runner's launch list or any sandbox's settings? — is read
    off the configuration and settled here. The readability half needs the
    machine.
    """
    name = "the-credential-is-out-of-their-reach"
    question = (
        "is the publisher's credential file named in, or readable from, the "
        "coordinator's, the runner's or any sandbox's settings?"
    )
    # WITHOUT THE PATH THERE IS NO SEARCH, so there is no answer, so there is
    # no pass. The settings half of this question is "is this exact path named
    # anywhere else?", and with no path to look for, the search trivially
    # finds nothing — which reads like an all-clear and is not one. The
    # readability half cannot stand in for it either: it is about a file
    # nobody has named. The path is what makes the question askable, so its
    # absence is a refusal, and the refusal names the setting to set.
    if not true.the_credential_file:
        return Answer(
            name,
            question,
            False,
            (
                "the publisher's credential file is not named at all "
                "(publication.publisher_credential_file is not set), so there "
                "is no path to look for in the coordinator's settings, the "
                "runner's launch list or any sandbox's settings, and this "
                "question cannot be answered. Set "
                "publication.publisher_credential_file to the path of the one "
                "file the publisher's credential is in, before publication is "
                "switched on"
            ),
            provable_here=True,
        )
    if true.where_the_credential_file_is_named:
        named = ", ".join(true.where_the_credential_file_is_named)
        return Answer(
            name,
            question,
            False,
            (
                f"the publisher's credential file is named in {named}. It "
                "belongs to the publisher and to nothing else; take it out of "
                "those settings before publication is switched on"
            ),
            provable_here=True,
        )
    readable = true.machine.the_credential_file_can_be_read_by_them
    if readable is None:
        return Answer(
            name,
            question,
            None,
            (
                "the credential file is named in no settings but the "
                "coordinator's own note of where the publisher's is, and "
                "nobody has looked at whether it can be READ by the "
                "coordinator, the runner or a sandbox. That half can only be "
                "settled on the real machine, at rollout"
            ),
            provable_here=False,
        )
    if readable:
        return Answer(
            name,
            question,
            False,
            (
                "the publisher's credential file can be read by the "
                "coordinator, the runner or a sandbox. Give it to the "
                "publisher's own user and to nobody else before publication "
                "is switched on"
            ),
            provable_here=False,
        )
    return Answer(
        name,
        question,
        True,
        (
            "the publisher's credential file is named in no other settings "
            "and cannot be read by the coordinator, the runner or any sandbox"
        ),
        provable_here=False,
    )


def _only_the_coordinator_is_on_the_publishers_network(true: WhatIsTrue) -> Answer:
    """Who else can reach the publisher, counted rather than assumed.

    The publisher listens on every address INSIDE its own container and
    publishes no port to the host, so the set of things that can reach it is
    exactly the set of things on its network. "No sandbox can reach it" is
    the property; this is the way it is made true and the way it is checked —
    count what is on that network, and find the coordinator and nothing else.

    A STAND-IN HERE. Nothing in this process can see a container network, so
    like the three walls above it is asked of :class:`WhatTheMachineSays` and
    answers "nobody has looked" — a refusal — until somebody looks at the
    real machine at rollout.
    """
    return _machine_question(
        "only-the-coordinator-is-on-the-publishers-network",
        "is the coordinator the only thing on the publisher's network?",
        # The field reads the RIGHT way round (True = the good answer), and
        # _machine_question reads the wrong way round (True = the refusal),
        # so it is turned over here rather than at the call site.
        answer=(
            None
            if true.machine.only_the_coordinator_is_on_the_publishers_network is None
            else not true.machine.only_the_coordinator_is_on_the_publishers_network
        ),
        when_true=(
            "the publisher's network has the coordinator on it and nothing "
            "else, and no port of the publisher's is published anywhere"
        ),
        when_false=(
            "something other than the coordinator is on the publisher's "
            "network, and anything that can reach the publisher can ask for a "
            "send. Put the publisher on a network of its own, shared with the "
            "coordinator alone and with no sandbox, before publication is "
            "switched on"
        ),
        when_unknown=(
            "nobody has counted what is on the publisher's network. The "
            "publisher listens on every address inside its own container, so "
            "what shares that network is exactly what can reach it, and an "
            "uncounted network is not a wall. This one can only be settled on "
            "the real machine, at rollout"
        ),
    )


#: The questions, in the order they are asked and reported.
THE_QUESTIONS = (
    _nothing_is_built_inside_the_coordinator,
    _no_sandbox_can_write_the_settings_file,
    _no_sandbox_can_see_the_ledger,
    _no_sandbox_can_reach_the_publisher,
    _only_the_coordinator_is_on_the_publishers_network,
    _the_credential_is_out_of_their_reach,
)


# ---------------------------------------------------------------------------
# Gathering what is true
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _names_the_credential_file(config: Any, credential_file: str) -> tuple[str, ...]:
    """Every settings block that names the publisher's credential file.

    Looked for, by exact path, in the three places the design names: the
    coordinator's own settings (anywhere but the one field that records where
    the publisher's file is, which is a note of a path and not a copy of a
    secret), the list of settings a build is launched with, and every
    sandbox's declared settings.
    """
    found: list[str] = []
    wanted = _text(credential_file)
    if not wanted:
        return ()

    def _looks_at(block: Any, said: str, *, skip: Iterable[str] = ()) -> None:
        if block is None:
            return
        skipped = set(skip)
        items: Iterable[tuple[str, Any]]
        if isinstance(block, dict):
            items = block.items()
        else:
            # Named fields only. ``model_`` is skipped because a settings
            # object built with pydantic answers those with a warning and
            # never with a path.
            items = (
                (name, getattr(block, name, None))
                for name in dir(block)
                if not name.startswith("_") and not name.startswith("model_")
            )
        for field_name, value in items:
            if field_name in skipped:
                continue
            if callable(value):
                continue
            if isinstance(value, str) and value.strip() == wanted:
                found.append(f"{said}.{field_name}")
            elif isinstance(value, (list, tuple)) and any(
                isinstance(entry, str) and entry.strip() == wanted for entry in value
            ):
                found.append(f"{said}.{field_name}")
            elif isinstance(value, dict) and any(
                isinstance(entry, str) and entry.strip() == wanted
                for entry in value.values()
            ):
                found.append(f"{said}.{field_name}")

    publication = getattr(config, "publication", None)
    # The coordinator's note of WHERE the publisher's credential file is, is
    # a path and not a credential: the check has to know the path to look for
    # it anywhere else, so that one field is not itself a finding.
    _looks_at(publication, "publication", skip={"publisher_credential_file"})
    planning = getattr(config, "planning", None)
    _looks_at(planning, "planning", skip={"sandboxes"})
    sandboxes = getattr(planning, "sandboxes", None)
    if isinstance(sandboxes, dict):
        for sandbox_name, entry in sorted(sandboxes.items()):
            _looks_at(entry, f"planning.sandboxes.{sandbox_name}")
    launch = getattr(getattr(config, "conductor", None), "launch_settings", None)
    if isinstance(launch, (list, tuple)) and wanted in {
        _text(entry) for entry in launch
    }:
        found.append("conductor.launch_settings")
    return tuple(sorted(set(found)))


def what_is_true_here(
    config: Any, machine: WhatTheMachineSays | None = None
) -> WhatIsTrue:
    """Gather the facts the questions are asked of.

    The settings are read off ``config``; the machine's answers come from
    ``machine``, which is ``None`` when nobody has looked — and then every
    machine question refuses by name.
    """
    publication = getattr(config, "publication", None)
    allowed = getattr(publication, "builds_may_run_inside_the_coordinator", None)
    if not isinstance(allowed, bool):
        # No setting at all reads as "the old behaviour": the coordinator
        # builds inside itself, which is what every forge does today.
        allowed = True
    credential_file = _text(
        getattr(publication, "publisher_credential_file", None)
    ) or None
    return WhatIsTrue(
        builds_may_run_inside_the_coordinator=allowed,
        the_credential_file=credential_file,
        where_the_credential_file_is_named=(
            _names_the_credential_file(config, credential_file)
            if credential_file
            else ()
        ),
        machine=machine or WhatTheMachineSays(),
    )


def run_the_activation_check(
    config: Any, machine: WhatTheMachineSays | None = None
) -> TheVerdict:
    """Ask them all, and say plainly which of them refuse.

    Never raises: a configuration of an unexpected shape reads as "it cannot
    be told from here", which is a refusal, which is the safe side.
    """
    try:
        true = what_is_true_here(config, machine)
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a refusal
        logger.warning(
            "publication: the activation check could not read the settings "
            "(%s: %s) — publication stays off",
            type(exc).__name__,
            exc,
        )
        return TheVerdict(
            all_hold=False,
            answers=(
                Answer(
                    "the-settings-could-not-be-read",
                    "can the settings the check reads be read at all?",
                    None,
                    (
                        "the settings the activation check reads could not be "
                        f"read ({type(exc).__name__}), so publication stays off"
                    ),
                    provable_here=True,
                ),
            ),
        )
    answers = tuple(question(true) for question in THE_QUESTIONS)
    return TheVerdict(
        all_hold=all(answer.holds is True for answer in answers), answers=answers
    )
