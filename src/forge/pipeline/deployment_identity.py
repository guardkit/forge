"""The identity of what was checked — one that cannot be reused (section C).

One-true-copy design pass, 21 September 2026, item 1, second revision,
section C: *"when Forge's live check of J passes, the thing that was checked
is given an identity that cannot be reused: its content fingerprint, and a
name made from J; the project's declared deploy step is handed that identity
and must deploy exactly it, then report the identity of what is now running;
Forge compares the two, and a mismatch is a failed deploy."*

WHY IT EXISTS. Saving an identity in the record proves nothing if the deploy
promotes a shared name that another build can overwrite. Between the check of
J and the deploy of J, a second build can build its own thing under that same
shared name, and the deploy then puts the second build's work live under the
first build's sentence. The shared name is the defect; a name made from J with
a fingerprint beside it is the repair.

WHAT AN IDENTITY *IS* BELONGS TO THE PROJECT. This module makes TEXT: a name
made from the joined commit, and a fingerprint of the content that was
checked. It is handed to the project's own deploy step under a setting name
the PROJECT declares, and the project's step decides what to do with it — one
project may tag a container image, another may name a directory, a third may
write it into a manifest. The step then prints the identity of what is now
running, under a marker the project also declares, and this module reads that
one line back out. Forge compares the two as text and nothing else. There is
no language, no packaging tool, no hosting provider and no protocol anywhere
in this file.

WHY THE STEP REPORTS RATHER THAN FORGE LOOKING. Forge cannot look: looking
would mean knowing what kind of thing the project deploys. The project's own
step is the only thing that knows, so it is the thing that answers — and its
answer is checked against what it was handed, which is what catches a step
that deployed something else, a step that deployed nothing, and a step that
ignored the identity altogether.

WHAT THE SECOND REVIEW OF THE EXECUTOR STAGE ADDED (24 September 2026). A
reviewer drove two holes through the first build of section C, and both were
the same mistake: **a recorded statement was trusted where the target should
have been looked at.**

* The identity travelled, but the THING it named did not. The project's deploy
  step was left to find what it had checked by a name of its own at PROMOTE
  time, and a name can be given to another build's work in between. So the
  project is now asked, **at the check**, for the artifact's own unrepeatable
  identity (``checked_as``), Forge records that, and hands it back to the
  promote (``artifact_setting``) — which must deploy exactly it.
* Nothing ever ASKED THE TARGET what it was running. Only the ledger was read,
  and a crash between a deploy and its ledger line makes the ledger wrong. So a
  project may declare a **read-only step** — asked with ``asked_with``,
  answering under ``running_as`` — that says what is running right now, changing
  nothing. A project that declares none is not deployed over on a pick-up: it is
  left pending, which is the safe half of not knowing.

All four are names, declared by the project, carried as text. Nothing here
knows what an artifact is, how one is asked about, or what the answer means.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ARTIFACT_SETTING",
    "DEFAULT_ASK_SETTING",
    "DEFAULT_CHECKED_MARKER",
    "DEFAULT_REPORT_MARKER",
    "DEFAULT_RUNNING_MARKER",
    "DEFAULT_SETTING_NAME",
    "TARGET_ANSWERS",
    "FixedIdentity",
    "IdentityDeclaration",
    "declared_identity",
    "fixed_identity",
    "identity_reported_by",
    "the_identities_differ",
    "what_the_target_says",
]


#: The setting the identity is handed in when a project declares no other
#: name. A project says its own under ``deploy/profile.yaml``'s ``identity:``
#: block; this is only so that a project which declares the block but not the
#: name still gets one rather than silently getting nothing.
DEFAULT_SETTING_NAME: str = "DEPLOY_IDENTITY"

#: The marker the deploy step prints the running identity after, when a
#: project declares no other. One line, ``<marker>=<identity>``.
DEFAULT_REPORT_MARKER: str = "DEPLOYED_IDENTITY"

#: The marker the CHECK prints the artifact it actually checked under, when a
#: project declares no other. One line, ``<marker>=<artifact>``. The artifact is
#: the project's own unrepeatable name for the thing that was checked, captured
#: at the moment it was checked — not a name that can later be given to
#: something else.
DEFAULT_CHECKED_MARKER: str = "CHECKED_ARTIFACT"

#: The setting that artifact is handed BACK to the deploy step in, when a
#: project declares no other. The step must deploy exactly it.
DEFAULT_ARTIFACT_SETTING: str = "DEPLOY_ARTIFACT"

#: The setting that asks a project, READ-ONLY, what it is running right now,
#: when a project declares no other. The step so asked changes nothing.
DEFAULT_ASK_SETTING: str = "RUNNING_IDENTITY"

#: The marker that read-only answer prints under, when a project declares no
#: other. One line, ``<marker>=<identity>``; an empty value means the project
#: could not say, and then nothing is deployed.
DEFAULT_RUNNING_MARKER: str = "RUNNING_IDENTITY"


#: What a name made from a commit may contain. Letters, digits, dashes and
#: dots: the intersection of what almost everything anybody deploys will
#: accept as a name. Nothing here knows what it will be used as.
_SAFE = re.compile(r"[^A-Za-z0-9.\-]")


@dataclass(frozen=True)
class FixedIdentity:
    """What was checked, named so that nothing else can be it.

    ``name`` is made from the joined commit, so two joined commits never share
    one. ``fingerprint`` is of the content that was checked, so a name that was
    somehow reused still does not match. ``text`` is the two together and is
    the ONE thing that travels: it is what the deploy step is handed, what it
    must report back, and what Forge compares.
    """

    name: str
    fingerprint: str
    text: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fingerprint": self.fingerprint,
            "identity": self.text,
        }


@dataclass(frozen=True)
class IdentityDeclaration:
    """How THIS project wants the identity handed over and reported back.

    Both are names the project declares in its own deploy profile. Forge
    carries them as text and never invents one that the project did not ask
    for beyond the two defaults above, which exist so that a project which
    declares the block half-way still gets a working arrangement rather than
    silence.
    """

    setting: str
    marker: str
    #: True when the project declared an ``identity:`` block at all. A project
    #: that declared nothing is NOT given a fabricated arrangement: the press
    #: records that the project declares no identity, and the deploy is
    #: refused rather than run blind (the caller decides; this only reports).
    declared: bool = False
    #: The marker the CHECK prints the artifact it checked under. What comes
    #: back is recorded and handed to the deploy, so the deploy runs the thing
    #: that was checked rather than whatever a shared name points at later.
    checked_as: str = DEFAULT_CHECKED_MARKER
    #: The setting that recorded artifact is handed back in.
    artifact_setting: str = DEFAULT_ARTIFACT_SETTING
    #: The setting that asks the project, read-only, what is running now.
    #: Empty when the project declares no such step, and then a pick-up that
    #: cannot otherwise account for the target deploys nothing.
    asked_with: str = ""
    #: The marker that read-only answer prints under.
    running_as: str = DEFAULT_RUNNING_MARKER

    @property
    def can_be_asked(self) -> bool:
        """Has this project declared a read-only "what is running" step?"""
        return bool(self.asked_with and self.running_as)

    def to_wire(self) -> dict[str, Any]:
        return {
            "setting": self.setting,
            "marker": self.marker,
            "declared": self.declared,
            "checked_as": self.checked_as,
            "artifact_setting": self.artifact_setting,
            "asked_with": self.asked_with,
            "running_as": self.running_as,
        }


def fixed_identity(
    *, j_commit: str, content: str | None = None, prefix: str = "j"
) -> FixedIdentity:
    """The identity of the thing checked for this joined commit.

    Args:
        j_commit: The joined commit. The name is made from it, which is what
            makes the identity unrepeatable: a second build has a second
            joined commit and therefore a second name.
        content: What was checked, as text — in practice the tree of the
            joined commit, which is exactly the content the live check was
            pointed at. ``None`` falls back to the commit itself, which is
            honest but weaker, and the caller is warned.
        prefix: A short word in front of the name, so a person reading the
            running thing's own labels can see where the name came from.

    Returns:
        A :class:`FixedIdentity`. ``text`` is the whole of it and the only
        part that has to travel.
    """
    commit = str(j_commit or "").strip()
    if not commit:
        raise ValueError(
            "a fixed identity has to be made from a joined commit, and none "
            "was given"
        )
    material = str(content).strip() if content is not None else ""
    if not material:
        logger.warning(
            "deployment identity: nothing was given as the content of %s, so "
            "its fingerprint is of the commit alone",
            commit[:10],
        )
        material = commit
    name = _SAFE.sub("-", f"{prefix}-{commit[:12]}")
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return FixedIdentity(
        name=name, fingerprint=fingerprint, text=f"{name}@{fingerprint}"
    )


def declared_identity(profile: Any) -> IdentityDeclaration:
    """What the project declared about handing over and reading back.

    Reads the profile's own ``identity`` block when it has one. A profile that
    does not carry the block at all — every profile written before this — gets
    the two defaults with ``declared`` false, so a caller can tell the
    difference between "the project asked for these names" and "nobody has
    said".
    """
    block = getattr(profile, "identity", None)
    if block is None:
        extra = getattr(profile, "extra", None)
        if isinstance(extra, Mapping):
            block = extra.get("identity")
    if block is None:
        return IdentityDeclaration(
            setting=DEFAULT_SETTING_NAME, marker=DEFAULT_REPORT_MARKER, declared=False
        )

    def _read(*names: str) -> Any:
        for name in names:
            value = (
                block.get(name)
                if isinstance(block, Mapping)
                else getattr(block, name, None)
            )
            if value:
                return value
        return None

    setting = _read("setting")
    marker = _read("reported_as", "marker")
    checked_as = _read("checked_as", "checked_marker")
    artifact_setting = _read("artifact_setting", "artifact")
    # ``asked_with`` has NO default: a project that has not said how to ask it
    # what is running has not got a read-only step, and inventing a setting name
    # for one would mean running the project's deploy step in a mode nobody
    # declared. "Nobody has said" is a first-class answer here.
    asked_with = _read("asked_with", "ask_with", "running_setting")
    running_as = _read("running_as", "running_marker")
    return IdentityDeclaration(
        setting=str(setting or DEFAULT_SETTING_NAME).strip() or DEFAULT_SETTING_NAME,
        marker=str(marker or DEFAULT_REPORT_MARKER).strip() or DEFAULT_REPORT_MARKER,
        declared=True,
        checked_as=(
            str(checked_as or DEFAULT_CHECKED_MARKER).strip() or DEFAULT_CHECKED_MARKER
        ),
        artifact_setting=(
            str(artifact_setting or DEFAULT_ARTIFACT_SETTING).strip()
            or DEFAULT_ARTIFACT_SETTING
        ),
        asked_with=str(asked_with or "").strip(),
        running_as=(
            str(running_as or DEFAULT_RUNNING_MARKER).strip() or DEFAULT_RUNNING_MARKER
        ),
    )


def identity_reported_by(output: str | None, *, marker: str) -> str | None:
    """The identity the deploy step said is now running, or ``None``.

    The step prints one line, ``<marker>=<identity>``. The LAST such line
    wins, because a step that prints its progress may say what it is about to
    do before it says what it did. Whitespace and one layer of quotes are
    taken off, because a shell-written line very often carries them and a
    quoted identity is the same identity.

    ``None`` means the step said nothing the caller can read, which is NOT the
    same as saying something that does not match — and the caller says which
    of the two happened in its own sentence.
    """
    if not output or not marker:
        return None
    found: str | None = None
    needle = f"{marker}="
    for raw in str(output).splitlines():
        line = raw.strip()
        position = line.find(needle)
        if position < 0:
            continue
        value = line[position + len(needle) :].strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1].strip()
        if value:
            found = value
    return found


#: What a project's read-only answer amounted to. ``"identity"`` carries a
#: value; ``"nothing"`` is the project saying nothing is running there;
#: ``"no-answer"`` is the project not having said anything this side can read,
#: which is NOT the same and is never treated as "nothing".
TARGET_ANSWERS: tuple[str, ...] = ("identity", "nothing", "no-answer")


def what_the_target_says(
    output: str | None, *, marker: str
) -> tuple[str, str | None]:
    """Read a project's read-only "what are you running" answer.

    One line, ``<marker>=<value>``, and the LAST such line wins, for the same
    reason the deploy step's own line does. The VALUE is the whole of the
    answer, and it has exactly three readings:

    * a token       ⇒ ``("identity", token)`` — that is what is running there,
      in the project's own terms. Whether the caller can place that token is
      the caller's problem, and a token it cannot place is a target it cannot
      account for;
    * empty         ⇒ ``("nothing", None)`` — the project says nothing is
      running there. This is the ONLY way a project says that, so a project
      that knows something is running but cannot name it must answer with a
      token rather than with nothing;
    * no line at all ⇒ ``("no-answer", None)`` — the project said nothing this
      side can read, which is never read as "nothing is running".

    Nothing here knows what a project deploys or what its tokens mean.
    """
    if not output or not marker:
        return ("no-answer", None)
    needle = f"{marker}="
    found: str | None = None
    seen = False
    for raw in str(output).splitlines():
        line = raw.strip()
        position = line.find(needle)
        if position < 0:
            continue
        seen = True
        value = line[position + len(needle) :].strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1].strip()
        found = value
    if not seen:
        return ("no-answer", None)
    if not found:
        return ("nothing", None)
    return ("identity", found)


def the_identities_differ(handed: str | None, reported: str | None) -> bool:
    """Is what is running NOT what was handed over? Text, compared as text.

    ``True`` when they differ, and ``True`` when nothing was reported at all:
    a step that does not say what is running has not shown that the right
    thing is running, and the design's rule is that a mismatch is a FAILED
    deploy rather than a warning beside a pass.
    """
    if not handed:
        return True
    if not reported:
        return True
    return str(handed).strip() != str(reported).strip()
