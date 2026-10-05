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

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from forge.launch_environment import (
    MAX_DECLARED_SETTINGS,
    declared_setting_refusal,
)

__all__ = [
    "BINDING_DOCUMENTS_FIELD",
    "DECLARATION_PATH",
    "DeclarationsAtCommit",
    "DeclaredLaunchSettings",
    "DeclaredMemory",
    "DeclaredPath",
    "DeclaredProjectDocuments",
    "PLAYER_ALLOWED_KEYS",
    "LAUNCH_KEY",
    "MAX_DECLARATION_BYTES",
    "MAX_DECLARATION_DEPTH",
    "MAX_NAME_LENGTH",
    "MEMORY_KEY",
    "NAME_PATTERN",
    "PROJECT_KEY",
    "SETTINGS_KEY",
    "THE_TWO_LINES",
    "read_declarations_at_commit",
    "read_declared_launch_settings",
    "read_declared_memory",
    "read_declared_project_documents",
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


def _parse(content: str) -> tuple[dict | None, str | None]:
    """``(settings, None)`` when the file parsed, or ``(None, why not)``.

    The one bounded parse both questions are answered from, so a file is read
    the same way whichever question is being asked and a hostile one cannot
    stall a run through either door.

    IT ANSWERS WITH THE CLAUSE, NOT THE SENTENCE (23 September 2026, the fifth
    review). It used to hand back a finished memory-reader refusal, and the
    launch-settings reader passed that straight on — so a project whose
    settings file would not parse was told "there is no way to tell which
    memory this work belongs to" when it had asked which settings its builds
    need. The clause ("it is not a set of settings") belongs to the file; the
    sentence around it belongs to the question being asked, and each reader
    now writes its own.
    """
    if len(content.encode("utf-8", errors="ignore")) > MAX_DECLARATION_BYTES:
        return None, (
            f"it is larger than {MAX_DECLARATION_BYTES} bytes, which this "
            f"factory will not parse"
        )
    too_deep = _nesting_too_deep(content)
    if too_deep is not None:
        return None, (
            f"it nests more than {MAX_DECLARATION_DEPTH} levels deep, which "
            f"this factory will not parse"
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
        return None, (first or f"reading it raised {type(exc).__name__}")
    if not isinstance(data, dict):
        return None, "it is not a set of settings"
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
    data, why_not = _parse(content)
    if why_not is not None:
        return _unreadable(repo, commit, why_not)
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


def _launch_refusal(
    repo: str, commit: str, what: str, *, at: str | None = None
) -> DeclaredLaunchSettings:
    return DeclaredLaunchSettings(
        refusal=(
            f"the settings {repo} declares its builds need, in "
            f"{DECLARATION_PATH} at {at or _at(commit)}, cannot be used: {what}. A "
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
    at: str | None = None,
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

    ``at`` is the caller's own plain words for WHERE the file was read, for a
    caller whose answer is not "the commit this work starts from" — the helper
    service falls back to a project copy's committed HEAD, and a refusal that
    called that "the commit this work starts from (the committed HEAD of …)"
    was a sentence inside a sentence. With nothing given the wording is
    unchanged.

    Never raises.
    """
    if unreadable_because:
        return _launch_refusal(repo, commit, unreadable_because, at=at)
    if not found or content is None:
        # No file, or nothing came back: the project has not said. The memory
        # question already refuses a project with no declaration at all, so
        # this is only ever reached for a file that exists and says nothing
        # about its launch.
        return DeclaredLaunchSettings()
    data, why_not = _parse(content)
    if why_not is not None:
        # ITS OWN SENTENCE. This used to hand on the memory reader's, which
        # said the work's memory could not be told — a true sentence about a
        # question nobody had asked here.
        return _launch_refusal(repo, commit, why_not, at=at)
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
            at=at,
        )
    if len(raw) > MAX_DECLARED_SETTINGS:
        return _launch_refusal(
            repo,
            commit,
            f"it names {len(raw)} settings and this factory passes at most "
            f"{MAX_DECLARED_SETTINGS}",
            at=at,
        )
    names: list[str] = []
    for entry in raw:
        refusal = declared_setting_refusal(entry)
        if refusal is not None:
            return _launch_refusal(repo, commit, refusal, at=at)
        name = str(entry).strip()
        if name not in names:
            names.append(name)
    return DeclaredLaunchSettings(names=tuple(names), declared=True)


# ---------------------------------------------------------------------------
# Both questions, read once at one commit, for every caller (4 October 2026)
# ---------------------------------------------------------------------------
#
# The planning door used to hold this read itself. A feature planned elsewhere
# and queued straight to a build has no planning run, yet its build needs the
# same two answers out of the same file, refused in the same words. So the
# read lives here and both callers use it: the planning door at the commit its
# work starts from, and the build admission at the commit it admits. Neither
# keeps a copy of it.


@dataclass(frozen=True)
class DeclarationsAtCommit:
    """The memory name and setting names declared at one commit, or why not.

    ``content`` is the file's text when it was read, so a caller that needs
    one more answer out of the same file (the binding documents) does not read
    it a second time.
    """

    memory_project: str | None = None
    launch_settings: tuple[str, ...] = ()
    content: str | None = None
    found: bool = False
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.memory_project)


async def read_declarations_at_commit(
    runner: Any, *, repo: str, repo_path: str, commit: str
) -> DeclarationsAtCommit:
    """Read ``.guardkit/config.yaml`` at ``commit`` and answer both questions.

    ``runner`` is the git runner for this repository (the sandbox's for a
    sandboxed project, the coordinator's otherwise); it must offer
    ``read_file_at_commit``. Every refusal is the sentence the planning door
    has always shown: a runner that cannot read at a commit, a read that
    raised, a project that declares no memory or one that is not allowed, a
    setting name this factory keeps for itself, a file that could not be read.
    Never raises.
    """
    read = getattr(runner, "read_file_at_commit", None)
    if read is None:
        return DeclarationsAtCommit(
            refusal=(
                "the git runner wired for this factory cannot read a file "
                "at a commit, so there is no way to tell which memory this "
                "work belongs to"
            )
        )
    try:
        answer = await read(repo_path, commit, DECLARATION_PATH)
    except Exception as exc:  # noqa: BLE001 — boundary, never crash the caller
        return DeclarationsAtCommit(
            refusal=(
                f"{repo}'s {DECLARATION_PATH} could not be read at "
                f"the commit this work starts from ({commit}): "
                f"{type(exc).__name__}: {exc}"
            )
        )

    content = getattr(answer, "content", None)
    found = bool(getattr(answer, "found", False))
    unreadable = getattr(answer, "refusal", None)

    declared = read_declared_memory(
        repo=repo,
        commit=commit,
        content=content,
        found=found,
        unreadable_because=unreadable,
    )
    if not declared.ok:
        return DeclarationsAtCommit(
            content=content,
            found=found,
            refusal=declared.refusal or "the project's memory name could not be read",
        )
    wanted = read_declared_launch_settings(
        repo=repo,
        commit=commit,
        content=content,
        found=found,
        unreadable_because=unreadable,
    )
    if not wanted.ok:
        return DeclarationsAtCommit(
            content=content,
            found=found,
            refusal=(
                wanted.refusal
                or "the settings this project asked for could not be read"
            ),
        )
    return DeclarationsAtCommit(
        memory_project=str(declared.project),
        launch_settings=tuple(wanted.names),
        content=content,
        found=found,
    )


#: Where a project names the documents its builds are held to: the list the
#: builder (GuardKit's Player) already reads. One list for every role, not a
#: second one (project-initialisation design, 4 October 2026).
BINDING_DOCUMENTS_FIELD: str = "autobuild.player.required_documents"


#: The keys GuardKit's Player loader allows in ``autobuild.player``
#: (guardkit ``orchestrator/harness/selector.py`` ``_PLAYER_PATH_FIELDS``).
PLAYER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"skills", "memory", "instructions", "protected_paths", "required_documents"}
)


@dataclass(frozen=True)
class DeclaredPath:
    """One declared path: as the project spelled it, and normalised to read."""

    spelling: str
    path: str


@dataclass(frozen=True)
class DeclaredProjectDocuments:
    """``autobuild.player.instructions`` and ``required_documents``, in order."""

    instructions: tuple[DeclaredPath, ...] = ()
    documents: tuple[DeclaredPath, ...] = ()


def read_declared_project_documents(
    content: str | None,
) -> tuple[DeclaredProjectDocuments, str | None]:
    """What the one reading rule reads, as declared — or ``(empty, why)``.

    Opt-in exactly as GuardKit is (the one reading rule, 4 October 2026):
    unless ``autobuild.player.required_documents`` is present and non-empty
    the answer is empty and nothing about the block is judged — a malformed
    block is not this rule's business. When it is present, the
    ``autobuild.player`` block is held to GuardKit's own checks: only its
    allowed keys, and every path list a list of non-empty repository paths.
    The two lists read here must also stay inside the repository. Spellings
    are kept for labels. Never raises.
    """
    empty = DeclaredProjectDocuments()
    if not content:
        return empty, None
    data, why_not = _parse(content)
    if why_not is not None or data is None:
        return empty, None
    autobuild = data.get("autobuild")
    player = autobuild.get("player") if isinstance(autobuild, dict) else None
    declared = player.get("required_documents") if isinstance(player, dict) else None
    if not declared:
        return empty, None
    assert isinstance(player, dict)
    unknown = sorted(str(key) for key in set(player) - PLAYER_ALLOWED_KEYS)
    if unknown:
        return empty, (
            f"`autobuild.player` in {DECLARATION_PATH} has unknown keys "
            f"{unknown}; the allowed keys are {sorted(PLAYER_ALLOWED_KEYS)}"
        )
    lists: dict[str, tuple[DeclaredPath, ...]] = {}
    for key in sorted(PLAYER_ALLOWED_KEYS):
        raw = player.get(key)
        if raw is None:
            lists[key] = ()
            continue
        field = f"autobuild.player.{key}"
        if not isinstance(raw, list):
            return empty, (
                f"`{field}` in {DECLARATION_PATH} is not a list of repository "
                f"paths"
            )
        paths: list[DeclaredPath] = []
        for index, value in enumerate(raw):
            if not isinstance(value, str) or not value.strip():
                return empty, (
                    f"`{field}[{index}]` in {DECLARATION_PATH} is not a "
                    f"repository path"
                )
            normal = posixpath.normpath(value)
            if value.startswith("/") or (
                key in ("instructions", "required_documents")
                and (normal in (".", "..") or normal.startswith("../"))
            ):
                return empty, (
                    f"`{field}[{index}]` in {DECLARATION_PATH} ({value!r}) is "
                    f"not a path inside the repository"
                )
            paths.append(DeclaredPath(spelling=value, path=normal))
        lists[key] = tuple(paths)
    return (
        DeclaredProjectDocuments(
            instructions=lists["instructions"], documents=lists["required_documents"]
        ),
        None,
    )
