"""Publication cannot be switched on until three things hold.

Publication (the merge word sending the joined commit to the project's remote,
then deploying) is on only when ``publication.enabled`` is set AND every one of
these holds:

1. **nothing is built inside the coordinator** — a setting, read here;
2. **the publisher's credential file is named in no other settings** — the
   coordinator's, the runner's launch list or any sandbox's. Read here, from
   the settings. A credential file that is not named at all is a refusal, not
   a pass: with no path there is nothing to search for;
3. **the publisher passed its self-check** — the credential file is a regular
   file owned by the publisher's UID with no group or other access, and the
   publisher has exactly one network interface besides loopback. It will not
   start without that, and its health route runs the check again on every
   request; the coordinator asks that route
   (:func:`forge.pipeline.publisher_client.the_publishers_self_check`).

WHAT THIS NO LONGER PROVES (2 October 2026). It replaced a separate check
(``estate-check --publication-facts``) whose written answers expired. That
check looked at things this one does not, and publication no longer depends
on them: whether a sandbox can reach the coordinator's settings or the ledger;
whether any other container mounts the credential file; which containers are
on the publisher's network and whether a sandbox can reach the publisher; and
that the answers were fresh. The self-check also has limits of its own:
several estate containers and the host login share UID 1000, so "owned by the
publisher's UID" is not "readable by the publisher only"; one interface does
not say which network it is or who else is on it. Network membership and
reachability are still checked by ``publisher-host-policy verify`` and by
estate-check items 7c and 8g, but publication does not wait on them. The
sandbox's git export the publisher reads from is unauthenticated and readable
by anything that can reach the gateway address.

IT FAILS CLOSED. A publisher that cannot be asked is not a pass: the answer is
"unknown", and unknown keeps publication off with the reason said.

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
    """What the publisher said about its own start-up self-check.

    ``True`` it passed, ``False`` it is running without having passed,
    ``None`` nobody could ask it — which is a refusal, never a pass.
    """

    the_publisher_passed_its_self_check: bool | None = None
    #: Why the publisher could not be asked, said in the refusal.
    why_nobody_has_looked: str | None = None


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

    @property
    def refuses(self) -> bool:
        return self.holds is not True

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "question": self.question,
            "holds": self.holds,
            "said": self.said,
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
            "it cannot be told whether the coordinator may build inside "
            "itself: the setting that permits it was not readable",
        )
    if allowed:
        return Answer(
            name,
            question,
            False,
            "the coordinator may still start builds and project checks "
            "inside itself, and a build that runs in there can write the "
            "very record the publisher trusts. Switch that setting off, "
            "and give every project a sandbox of its own, before "
            "publication is switched on",
        )
    return Answer(
        name,
        question,
        True,
        "the coordinator refuses to build or check inside itself",
    )


def _the_credential_is_named_in_no_other_settings(true: WhatIsTrue) -> Answer:
    name = "the-credential-is-named-in-no-other-settings"
    question = (
        "is the publisher's credential file named in the coordinator's, the "
        "runner's or any sandbox's settings?"
    )
    # WITHOUT THE PATH THERE IS NO SEARCH, so there is no answer, so there is
    # no pass: a search for nothing finds nothing, which is not an all-clear.
    if not true.the_credential_file:
        return Answer(
            name,
            question,
            False,
            "the publisher's credential file is not named at all "
            "(publication.publisher_credential_file is not set), so there "
            "is no path to look for in the coordinator's settings, the "
            "runner's launch list or any sandbox's settings, and this "
            "question cannot be answered. Set "
            "publication.publisher_credential_file to the path of the one "
            "file the publisher's credential is in, before publication is "
            "switched on",
        )
    if true.where_the_credential_file_is_named:
        named = ", ".join(true.where_the_credential_file_is_named)
        return Answer(
            name,
            question,
            False,
            f"the publisher's credential file is named in {named}. It "
            "belongs to the publisher and to nothing else; take it out of "
            "those settings before publication is switched on",
        )
    return Answer(
        name,
        question,
        True,
        "the publisher's credential file is named in no other settings",
    )


def _the_publisher_passed_its_self_check(true: WhatIsTrue) -> Answer:
    name = "the-publisher-passed-its-self-check"
    question = (
        "does the publisher find its credential file owned by its own UID "
        "with no group or other access, and itself with exactly one network "
        "interface besides loopback?"
    )
    passed = true.machine.the_publisher_passed_its_self_check
    if passed is None:
        why = true.machine.why_nobody_has_looked or "nobody asked it"
        return Answer(
            name,
            question,
            None,
            f"the publisher could not be asked whether it passed its "
            f"start-up self-check ({why}), and an unanswered question is not "
            "a pass",
        )
    if not passed:
        return Answer(
            name,
            question,
            False,
            "the publisher is running without having passed its "
            "self-check (its credential file owned by its own UID with no "
            "group or other access, and one network interface besides "
            "loopback)",
        )
    return Answer(
        name,
        question,
        True,
        "the publisher passed its start-up self-check",
    )


#: The questions, in the order they are asked and reported.
THE_QUESTIONS = (
    _nothing_is_built_inside_the_coordinator,
    _the_credential_is_named_in_no_other_settings,
    _the_publisher_passed_its_self_check,
)


# ---------------------------------------------------------------------------
# Gathering what is true
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _names_the_credential_file(config: Any, credential_file: str) -> tuple[str, ...]:
    """Every settings block that names the publisher's credential file.

    Looked for, by exact path, in the coordinator's own settings (anywhere but
    the one field that records where the publisher's file is, which is a note
    of a path and not a copy of a secret), the list of settings a build is
    launched with, and every sandbox's declared settings.
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
    """Gather the facts the questions are asked of."""
    publication = getattr(config, "publication", None)
    allowed = getattr(publication, "builds_may_run_inside_the_coordinator", None)
    if not isinstance(allowed, bool):
        # No setting at all reads as "the old behaviour": the coordinator
        # builds inside itself.
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
    """Ask them all, and say plainly which of them refuse. Never raises."""
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
                    "the settings the activation check reads could not be "
                    f"read ({type(exc).__name__}), so publication stays off",
                ),
            ),
        )
    answers = tuple(question(true) for question in THE_QUESTIONS)
    return TheVerdict(
        all_hold=all(answer.holds is True for answer in answers),
        answers=answers,
    )
