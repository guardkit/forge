"""Which memory a project declares, read at the commit the work starts from.

ONE question is answered here: *at the commit this piece of work starts from,
what memory name does the project declare?* The answer is the name, or "it
declares none", or "the name it declares is not allowed" — and the last two
carry the plain sentence a person is shown, naming the two lines to add.

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

__all__ = [
    "DECLARATION_PATH",
    "DeclaredMemory",
    "MAX_DECLARATION_BYTES",
    "MAX_NAME_LENGTH",
    "MEMORY_KEY",
    "NAME_PATTERN",
    "PROJECT_KEY",
    "THE_TWO_LINES",
    "read_declared_memory",
]


#: Where a project declares its memory name, relative to the project's root.
#: The same path GuardKit's own resolver reads, so there is one file and not two.
DECLARATION_PATH: str = ".guardkit/config.yaml"

#: The declaration block and the key inside it.
MEMORY_KEY: str = "memory"
PROJECT_KEY: str = "project"

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

#: The two lines a project adds to turn its memory on. Written once, quoted by
#: every refusal below, so a person is never shown two different recipes.
THE_TWO_LINES: str = "  memory:\n    project: <a name of letters, digits and underscores>"


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
    if len(content.encode("utf-8", errors="ignore")) > MAX_DECLARATION_BYTES:
        return _unreadable(
            repo,
            commit,
            f"it is larger than {MAX_DECLARATION_BYTES} bytes, which this "
            f"factory will not parse",
        )
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "it is not valid YAML"
        return _unreadable(repo, commit, first)
    if not isinstance(data, dict):
        return _unreadable(repo, commit, "it is not a set of settings")
    if MEMORY_KEY not in data:
        return _declares_none(repo, commit)
    block = data[MEMORY_KEY]
    if not isinstance(block, dict) or PROJECT_KEY not in block:
        # A ``memory:`` block with no ``project:`` is ordinary — other settings
        # live in that block too. Nothing is declared about the name, which is
        # the same answer as no block at all.
        return _declares_none(repo, commit)
    return _check_name(block[PROJECT_KEY], repo, commit)
