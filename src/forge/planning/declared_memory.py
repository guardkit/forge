"""What a project declares, read at the commit the work starts from.

TWO questions are answered here, out of ONE file read at ONE commit — the
project's own ``.guardkit/config.yaml``:

1. *Which memory does this work belong to?* The answer is the name, or "it
   declares none", or "the name it declares is not allowed" — and the last two
   carry the plain sentence a person is shown, naming the two lines to add.
2. *Which settings do this project's own builds need from the launching
   process, beyond the factory's own list?* (added 22 September 2026). The
   answer is a list of NAMES, or "it declares none", or a refusal naming the
   one bad name. Values never come out of the project: only names, and the
   launch takes each value from the launching process if it has one.

THE PARSE IS BOUNDED, and every way it can fail is an answer rather than a
crash (22 September 2026, after the stage's second independent review). The
file comes out of a commit this factory did not write. The review sent a
declaration of about 1.2 KB whose nesting was deep enough to exhaust the
parser's own stack: the failure was a ``RecursionError``, which is not a YAML
error, so it escaped the one thing being caught and left a run RUNNING with
nobody told. Now the text is size-capped AND depth-capped before the parser
sees it, and every remaining parser failure — of any kind — becomes the plain
"could not be read" refusal.

WHY AT THE COMMIT AND NOT IN THE WORKING FOLDER (design pass 2026-09-21, item
2, "Which copy of the declaration"). Forge reads ``memory.project`` from the
project's settings file **at the fetched, recorded starting commit**, never
from whatever the project's main copy has checked out, and hands that name to
the build on purpose when it launches it. A stale checkout therefore cannot
supply the name, and Forge and GuardKit cannot disagree: GuardKit uses the name
it was handed and reads its own declaration only when nothing handed one over
(GuardKit used by hand, with no Forge).

THE NAME'S RULE IS THE MEMORY SERVICE'S RULE, and it is the same rule GuardKit's
own resolver applies (``guardkit/knowledge/memory_project.py``): letters, digits
and underscores, and **never rewritten**. A rewritten name would be a second
place records can hide, so a name that breaks the rule is refused rather than
made acceptable.

Nothing here knows or cares what language the project is written in, what it
tests with, how it is laid out or where it is hosted. It reads one optional
file — the project's own ``.guardkit/config.yaml`` — out of one commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from forge.launch_environment import (
    MAX_DECLARED_SETTINGS,
    declared_setting_refusal,
)

__all__ = [
    "DECLARATION_PATH",
    "DeclaredLaunchSettings",
    "DeclaredMemory",
    "LAUNCH_KEY",
    "MAX_DECLARATION_BYTES",
    "MAX_DECLARATION_DEPTH",
    "MAX_NAME_LENGTH",
    "MEMORY_KEY",
    "NAME_PATTERN",
    "PROJECT_KEY",
    "SETTINGS_KEY",
    "THE_TWO_LINES",
    "read_declared_launch_settings",
    "read_declared_memory",
]


#: Where a project declares its memory name, relative to the project's root.
#: The same path GuardKit's own resolver reads, so there is one file and not two.
DECLARATION_PATH: str = ".guardkit/config.yaml"

#: The declaration block and the key inside it.
MEMORY_KEY: str = "memory"
PROJECT_KEY: str = "project"

#: The other block this factory reads out of the same file, and its one key:
#: ``launch: settings: [NAME, NAME]`` — the names a project's own builds need
#: from the launching process beyond the factory's own list.
LAUNCH_KEY: str = "launch"
SETTINGS_KEY: str = "settings"

#: The memory service's rule for a name — letters, digits and underscores only
#: (``fleet-memory/src/fleet_memory/payloads/base.py``). A name that fails this
#: is refused by the service itself, so a build that used it would lose every
#: write.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

#: A name longer than this is refused. The service sets no limit; this one keeps
#: an accidental paste (a whole file, a token) out of every natural key. Matches
#: GuardKit's resolver so the two cannot disagree about what is acceptable.
MAX_NAME_LENGTH: int = 128

#: A settings file larger than this is not parsed. Bounded on purpose: the file
#: comes out of a commit that the factory did not write, and a huge or hostile
#: one must not be able to stall a planning run. Matches the bound GuardKit's
#: own readers use.
MAX_DECLARATION_BYTES: int = 256 * 1024

#: How deeply a settings file may nest before this factory stops reading it.
#: The parser walks nesting with its own stack, so a small file nested deeply
#: enough exhausts it — the review's 1.2 KB declaration did exactly that. A
#: real project's settings file is a handful of levels deep; this is far above
#: anything anyone writes and far below what the parser cannot survive.
MAX_DECLARATION_DEPTH: int = 40

#: The two lines a project adds to turn its memory on. Written once, quoted by
#: every refusal below, so a person is never shown two different recipes.
THE_TWO_LINES: str = "  memory:\n    project: <a name of letters, digits and underscores>"

#: The two lines a project adds to name a setting its own builds need. Written
#: once, for the same reason.
THE_LAUNCH_LINES: str = "  launch:\n    settings: [SOME_NAME, ANOTHER_NAME]"


Outcome = Literal["declared", "declares-none", "not-allowed", "unreadable"]


@dataclass(frozen=True)
class DeclaredMemory:
    """What the project declares at one commit, and the sentence for a person.

    Exactly one of ``project`` and ``refusal`` is ever set. ``outcome`` says
    which of the four answers this is, so a caller can tell "the project has not
    said" from "the project said something unusable" from "the settings file
    could not be read at all" without reading the sentence.
    """

    project: str | None = None
    outcome: Outcome = "declares-none"
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        """True when this is a usable memory name."""
        return bool(self.project) and self.refusal is None


def _at(commit: str) -> str:
    return f"the commit this work starts from ({commit})"


def _declares_none(repo: str, commit: str) -> DeclaredMemory:
    return DeclaredMemory(
        outcome="declares-none",
        refusal=(
            f"{repo} does not say which memory it uses. Its {DECLARATION_PATH} "
            f"declares no memory name at {_at(commit)}, so a build of it would "
            f"read no prior decisions and write its outcomes nowhere — and this "
            f"factory will not quietly file them under another project's name. "
            f"Add these two lines to {DECLARATION_PATH}, commit them to the "
            f"branch this work starts from, and ask again:\n{THE_TWO_LINES}"
        ),
    )


def _not_allowed(repo: str, commit: str, what: str) -> DeclaredMemory:
    return DeclaredMemory(
        outcome="not-allowed",
        refusal=(
            f"the memory name {repo} declares in {DECLARATION_PATH} at "
            f"{_at(commit)} is not allowed: {what}. A memory name may contain "
            f"only letters, digits and underscores, and it is never rewritten "
            f"for you, because the rewritten name would be a second place "
            f"records can hide. Correct these two lines in "
            f"{DECLARATION_PATH}, commit them to the branch this work starts "
            f"from, and ask again:\n{THE_TWO_LINES}"
        ),
    )


def _unreadable(repo: str, commit: str, why: str) -> DeclaredMemory:
    return DeclaredMemory(
        outcome="unreadable",
        refusal=(
            f"{repo}'s {DECLARATION_PATH} could not be read at {_at(commit)}, "
            f"so there is no way to tell which memory this work belongs to: "
            f"{why}"
        ),
    )


def _nesting_too_deep(content: str) -> int | None:
    """The depth this text reaches, when that is past the cap; else ``None``.

    A cheap scan, done BEFORE the parser is handed anything, because the
    parser's failure on a deeply nested document is a stack exhaustion rather
    than a parse error — and a stack exhaustion caught late is a run nobody is
    told about. Two kinds of nesting are counted, which is every kind YAML has:

    * the flow kind, ``[[[[`` and ``{{{{``, counted by bracket depth;
    * the block kind, counted by the indentation of a line that opens a
      mapping or a sequence — each deeper indent is one more level.

    Neither count needs to be exact. It is a bound, not a measurement: it says
    "this is deeper than anything anyone writes by hand", and the answer is a
    refusal in plain words rather than a crash.
    """
    flow_depth = 0
    worst = 0
    indents: list[int] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        while indents and indent <= indents[-1]:
            indents.pop()
        indents.append(indent)
        worst = max(worst, len(indents) + flow_depth)
        for char in line:
            if char in "[{":
                flow_depth += 1
                worst = max(worst, len(indents) + flow_depth)
            elif char in "]}":
                flow_depth = max(0, flow_depth - 1)
        if worst > MAX_DECLARATION_DEPTH:
            return worst
    return worst if worst > MAX_DECLARATION_DEPTH else None


def _parse(content: str, repo: str, commit: str) -> tuple[dict | None, DeclaredMemory | None]:
    """``(settings, None)`` when the file parsed, or ``(None, refusal)``.

    The one bounded parse both questions are answered from, so a file is read
    the same way whichever question is being asked and a hostile one cannot
    stall a run through either door.
    """
    if len(content.encode("utf-8", errors="ignore")) > MAX_DECLARATION_BYTES:
        return None, _unreadable(
            repo,
            commit,
            f"it is larger than {MAX_DECLARATION_BYTES} bytes, which this "
            f"factory will not parse",
        )
    too_deep = _nesting_too_deep(content)
    if too_deep is not None:
        return None, _unreadable(
            repo,
            commit,
            f"it nests more than {MAX_DECLARATION_DEPTH} levels deep, which "
            f"this factory will not parse",
        )
    try:
        data = yaml.safe_load(content) or {}
    except Exception as exc:  # noqa: BLE001 — SEE BELOW; a parse is input, not code
        # EVERY parser failure, not just the ones the parser calls its own.
        # ``yaml.YAMLError`` alone was what this caught, and the stage's second
        # review broke it with a ``RecursionError`` — a builtin, raised by the
        # interpreter rather than the parser, which sailed straight past and
        # left the run RUNNING with nobody told. A settings file out of a
        # commit this factory did not write is INPUT: whatever it does to the
        # parser is an answer to a person, never a stranded run.
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        return None, _unreadable(
            repo,
            commit,
            first or f"reading it raised {type(exc).__name__}",
        )
    if not isinstance(data, dict):
        return None, _unreadable(repo, commit, "it is not a set of settings")
    return data, None


def _check_name(raw: Any, repo: str, commit: str) -> DeclaredMemory:
    """The name as declared, or a refusal saying exactly what is wrong with it."""
    if not isinstance(raw, str):
        return _not_allowed(
            repo, commit, f"it is not text (it reads as {type(raw).__name__})"
        )
    name = raw.strip()
    if not name:
        return _not_allowed(repo, commit, "it is empty")
    if len(name) > MAX_NAME_LENGTH:
        return _not_allowed(
            repo, commit, f"it is longer than {MAX_NAME_LENGTH} characters"
        )
    if not NAME_PATTERN.fullmatch(name):
        return _not_allowed(repo, commit, f"{name!r} is not a name of that shape")
    return DeclaredMemory(project=name, outcome="declared")


def read_declared_memory(
    *,
    repo: str,
    commit: str,
    content: str | None,
    found: bool,
    unreadable_because: str | None = None,
) -> DeclaredMemory:
    """Turn one settings file, as it is at one commit, into the answer.

    Args:
        repo: How the project is named to a person (``org/name``). It appears in
            every sentence, so a person reading Slack knows which project.
        commit: The recorded starting commit the file was read at. Named in
            every sentence for the same reason.
        content: The settings file's text at that commit, or ``None``.
        found: Whether the file exists at that commit at all. ``False`` with
            ``content=None`` means the project declares nothing; ``True`` with
            ``content=None`` means it is there and could not be read.
        unreadable_because: Why the read failed, when it did. One plain clause.

    Returns:
        A :class:`DeclaredMemory`. Never raises: a settings file out of a commit
        the factory did not write is input, not code, and a malformed one is an
        answer rather than a crash.
    """
    if unreadable_because:
        return _unreadable(repo, commit, unreadable_because)
    if not found:
        return _declares_none(repo, commit)
    if content is None:
        return _unreadable(repo, commit, "its contents came back empty-handed")
    data, refusal = _parse(content, repo, commit)
    if refusal is not None:
        return refusal
    assert data is not None  # _parse answers one or the other, never neither
    if MEMORY_KEY not in data:
        return _declares_none(repo, commit)
    block = data[MEMORY_KEY]
    if not isinstance(block, dict) or PROJECT_KEY not in block:
        # A ``memory:`` block with no ``project:`` is ordinary — other settings
        # live in that block too. Nothing is declared about the name, which is
        # the same answer as no block at all.
        return _declares_none(repo, commit)
    return _check_name(block[PROJECT_KEY], repo, commit)


# ---------------------------------------------------------------------------
# The second question: which settings does the project say its builds need?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredLaunchSettings:
    """The names a project declares, or the sentence saying why it got none.

    ``declared`` tells "the project said nothing about this" (the honest
    answer for every project written before this existed) apart from "the
    project said: nothing extra", which is a decision somebody made.
    """

    names: tuple[str, ...] = ()
    declared: bool = False
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        """True when this is a usable answer, empty or not."""
        return self.refusal is None


def _launch_refusal(repo: str, commit: str, what: str) -> DeclaredLaunchSettings:
    return DeclaredLaunchSettings(
        refusal=(
            f"the settings {repo} declares its builds need, in "
            f"{DECLARATION_PATH} at {_at(commit)}, cannot be used: {what}. A "
            f"project names the settings its own builds need, and nothing "
            f"else: names only, never values, never a name this factory keeps "
            f"for itself. Correct these two lines in {DECLARATION_PATH}, "
            f"commit them to the branch this work starts from, and ask "
            f"again:\n{THE_LAUNCH_LINES}"
        )
    )


def read_declared_launch_settings(
    *,
    repo: str,
    commit: str,
    content: str | None,
    found: bool,
    unreadable_because: str | None = None,
) -> DeclaredLaunchSettings:
    """Turn one settings file, as it is at one commit, into the names.

    The same file, the same commit and the same bounded parse as
    :func:`read_declared_memory` — the caller reads the file once and asks both
    questions of it.

    A project that says nothing gets an empty answer and a build launched with
    the factory's own list, exactly as before this existed. A project that says
    something unusable is REFUSED, in plain words naming the one bad name,
    because a build launched without a setting its own project said it needs
    fails somewhere further on, in a sentence about something else.

    Never raises.
    """
    if unreadable_because:
        return _launch_refusal(repo, commit, unreadable_because)
    if not found or content is None:
        # No file, or nothing came back: the project has not said. The memory
        # question already refuses a project with no declaration at all, so
        # this is only ever reached for a file that exists and says nothing
        # about its launch.
        return DeclaredLaunchSettings()
    data, parse_refusal = _parse(content, repo, commit)
    if parse_refusal is not None:
        return DeclaredLaunchSettings(refusal=parse_refusal.refusal)
    assert data is not None
    if LAUNCH_KEY not in data:
        return DeclaredLaunchSettings()
    block = data[LAUNCH_KEY]
    if not isinstance(block, dict) or SETTINGS_KEY not in block:
        # A ``launch:`` block with no ``settings:`` is ordinary — other
        # settings may live in that block. Nothing is declared about names.
        return DeclaredLaunchSettings()
    raw = block[SETTINGS_KEY]
    if raw is None:
        return DeclaredLaunchSettings(declared=True)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return _launch_refusal(
            repo,
            commit,
            f"it is not a list of names (it reads as {type(raw).__name__})",
        )
    if len(raw) > MAX_DECLARED_SETTINGS:
        return _launch_refusal(
            repo,
            commit,
            f"it names {len(raw)} settings and this factory passes at most "
            f"{MAX_DECLARED_SETTINGS}",
        )
    names: list[str] = []
    for entry in raw:
        refusal = declared_setting_refusal(entry)
        if refusal is not None:
            return _launch_refusal(repo, commit, refusal)
        name = str(entry).strip()
        if name not in names:
            names.append(name)
    return DeclaredLaunchSettings(names=tuple(names), declared=True)
