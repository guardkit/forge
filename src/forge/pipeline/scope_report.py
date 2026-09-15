"""The scope pass — hold the finished build against its plan AND against the
sentence the person actually sent.

WHY (the planner fix, 2026-09-15). Twelve builds for one sentence: the
specification was right twelve times out of twelve and the plan behind it was
different every time. Something nobody asked for turned up in ten of the
twelve — five modules here, a database migration there — and in three of them
the web address moved, which is what got a run refused at the live gate an
hour later. None of it reached the card Rich taps to say merge.

So this asks TWO questions of a finished build, and they are deliberately not
the same question:

* **against the plan** — which files did this build change that no task
  document named? That is the blast radius, and "the plan did not name it" is
  the right test for it, because a sentence names no files at all.
* **against the request** — does this build answer at a web address the
  sentence never named, or add a capability the sentence never asked for?
  That is read from what the branch WROTE, not from what the plan promised,
  and it is the half that catches a plan that was followed faithfully into
  the wrong place. It is read in what the branch DECLARES — the files it
  changed and the lines that say what the software now does — and not in
  every line of source it wrote, because a capability word inside a test
  fixture, a comment or a bare import is not a capability this build added.

Both answers land in ``<receipts>/<build id>/scope_report.json`` at the moment
the merge card is offered, which is before anyone says merge, so one sentence
of it can ride on the card.

THE ONE RULE THIS MODULE WILL NOT BREAK: **a count nobody took is never
published as a count of nothing.** Three separate things can go unread — the
branch, the plan's own declaration of files, and the request — and the report
says which, in ordinary words, rather than printing a zero.

Pure: no git, no network, no database. It is handed the reading and the
request and it compares them. Never raises.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SCOPE_REPORT_NAME",
    "DeclaredFiles",
    "ScopeReport",
    "files_the_plan_named",
    "promises_the_capability",
    "read_declared_files",
    "scope_of_the_build",
    "unread_scope",
    "what_the_branch_declares",
    "what_the_branch_wrote",
    "write_scope_report",
]

#: The receipt's filename, under ``<receipts>/<build id>/``.
SCOPE_REPORT_NAME: str = "scope_report.json"

#: The two sections a task document declares its files under. They are an
#: existing convention in this estate, not a new invention: Rich introduced
#: them by hand in fourteen forge task documents on 2026-05-07.
_CREATE_HEADING = "## Files to Create"
_MODIFY_HEADING = "## Files to Modify"

#: What a section with nothing to declare writes. Present-and-empty is NOT
#: the same as absent: this means "this task creates nothing", and a missing
#: section means the task never said.
_NONE_LINE = "_none_"

#: Folders whose files are scaffolding rather than the thing that was asked
#: for. A test file and a documentation page beside a new endpoint are the
#: ordinary cost of building it, and naming them on the card would bury the
#: five-module package the card exists to show.
_DOC_FOLDERS: tuple[str, ...] = ("docs", "doc", "documentation")

#: Folders the factory writes its OWN paperwork into, and which are therefore
#: inside every routine build branch whether anybody asked for them or not.
#:
#: WHY THIS LIST EXISTS. A routine build branch opens with four planning
#: commits before the coder writes a line: the feature's own file, the
#: specification input, the feature plan with its task documents, and the QA
#: pass bars and gate scripts. Read on 2026-09-15 over all twenty-two real
#: build branches in the api_test repository, every one of them carries
#: ``.guardkit/features/FEAT-XXXX.yaml``, ``feature_spec_inputs/<id>.md``,
#: ``features/<name>/*``, ``qa/pass-bar-*.yaml`` and a dozen or more files
#: under ``tasks/``. None of that is the code the build was asked for, and
#: reading it as such put six to twelve false sentences on the card Rich taps
#: to say merge: a shebang line became "It also answers at a web address the
#: request did not name: /usr/bin/env.", and an open question in the
#: specification input ("Are there any authentication or authorization
#: requirements for this endpoint?") became "It also added authentication,
#: which the request did not ask for."
_FACTORY_PAPERWORK_FOLDERS: tuple[str, ...] = (
    ".guardkit",
    "feature_spec_inputs",
    "features",
    "qa",
    "tasks",
)

#: A line that is a note to whoever reads the code next, rather than something
#: the software now does. A comment saying "error handling" does not make the
#: build handle errors. The capability words were measured on the prose of
#: task documents, where such a word is a deliberate promise; in source text
#: the same word turns up in the commentary of almost every honest build.
_A_NOTE_TO_A_READER = re.compile(r"^\s*(#|//|/\*|\*/|\*\s)")

#: The quotes that open and close a Python docstring — prose again, and not a
#: thing the software does.
_A_BLOCK_OF_PROSE = re.compile('"""' + "|'''")

#: Lines that wire a module up rather than declare anything: an import, and a
#: logger being made. ``import logging`` at the top of a file is the plainest
#: false alarm there is, and it was putting "It also added logging, which the
#: request did not ask for." on the card Rich taps to say merge.
_ORDINARY_WIRING = re.compile(
    r"^\s*(from\s+\S+\s+)?import\s"
    r"|^\s*import\s+\S"
    r"|\brequire\s*\("
    r"|=\s*logging\.(getLogger|Logger)\b"
    r"|^\s*logging\.(basicConfig|config)\b"
)


#: Words that turn a capability word on the same line into a DENIAL of that
#: capability rather than a promise of it.
#:
#: WHY THIS EXISTS. Driven read-only over all twenty-three real build branches
#: in the api_test repository on 2026-09-15, two of them put the sentence "It
#: also added authentication, which the request did not ask for." on the card
#: the owner taps to say merge — and both were wrong. The evidence in each was
#: the route's own description string, which says the opposite of what the
#: card said: "This endpoint does not require authentication." on one branch
#: and "This endpoint is publicly accessible without authentication." on the
#: other. A line that says the software does NOT do a thing is not evidence
#: that the build added it.
_A_DENIAL: frozenset[str] = frozenset({"not", "no", "never", "without"})

#: How many words before the capability word a denial still reaches. Six is
#: short on purpose: "does not require authentication" is three words, and a
#: denial further back than that is usually a different clause saying a
#: different thing.
_HOW_FAR_A_DENIAL_REACHES = 6

#: One word, hyphens and apostrophes kept, so that "X-Auth-Token",
#: "non-authenticated" and "doesn't" are each read as the single word they are.
_A_WORD = re.compile("[A-Za-z][A-Za-z'’-]*")


def _a_denial_stands_before(line: str, at: int) -> bool:
    """True when the capability word starting at ``at`` on this line is being
    denied rather than promised.

    Two ways a line denies a capability, and both are read here:

    * the word itself is the capability's absence — "unauthenticated",
      "non-authenticated" — which is a match that begins after an ``un`` or
      ``non`` prefix inside one word;
    * a denial stands a few words in front of it — "not", "no", "never",
      "without", or anything ending in "n't".

    Never raises.
    """
    words = [(match.group(0), match.start()) for match in _A_WORD.finditer(line)]
    for word, start in words:
        if start <= at < start + len(word):
            lowered = word.lower()
            inside = at - start
            if lowered.startswith("un") and inside >= 2:
                return True
            if lowered.startswith("non") and inside >= 3:
                return True
            break
    before = [word.lower() for word, start in words if start < at]
    for word in before[-_HOW_FAR_A_DENIAL_REACHES:]:
        if word in _A_DENIAL or word.endswith("n't") or word.endswith("n’t"):
            return True
    return False


def promises_the_capability(text: str, pattern: re.Pattern[str]) -> bool:
    """True when at least one LINE of ``text`` really says the software now
    does the thing ``pattern`` names.

    The question is asked one line at a time, and on each line a match that a
    denial stands before does not count. Asking it of the whole text at once
    was what put a false sentence on two of twenty-three real merge cards: the
    only mention of authentication on either branch was a line saying the
    endpoint does not require any. Reading line by line also keeps a true
    mention elsewhere in the same file: a branch whose description string
    denies authentication on one line and whose code requires an
    ``X-Auth-Token`` header on another has still added authentication, and the
    card still says so.

    Never raises.
    """
    for line in str(text or "").splitlines():
        for match in pattern.finditer(line):
            if not _a_denial_stands_before(line, match.start()):
                return True
    return False


@dataclass(frozen=True)
class DeclaredFiles:
    """What one task document says it will touch.

    The two sections are kept APART, because a task that creates nothing and
    changes two files must be able to say exactly that. ``present`` is false
    when the section is missing altogether, which is a defect in the task
    document rather than a declaration of nothing.
    """

    create: tuple[str, ...] = ()
    modify: tuple[str, ...] = ()
    create_present: bool = False
    modify_present: bool = False

    @property
    def declared_anything(self) -> bool:
        """True when this task wrote either section at all, empty or not."""
        return self.create_present or self.modify_present

    @property
    def all_files(self) -> tuple[str, ...]:
        """Both sections together, for the one question that needs them
        together: was this file named anywhere in the plan?"""
        return tuple(dict.fromkeys((*self.create, *self.modify)))


def _section_body(text: str, heading: str) -> str | None:
    """One ``##`` section's body, or ``None`` when the heading is not there."""
    lowered = str(text or "").lower()
    at = lowered.find(heading.lower())
    if at < 0:
        return None
    rest = str(text)[at + len(heading) :]
    lines: list[str] = []
    for line in rest.splitlines():
        if line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def _paths_in(body: str) -> tuple[str, ...]:
    """The repository-relative paths a section lists, back ticks stripped.

    The single line ``- _none_`` declares nothing, which is a real answer and
    not a missing one, so it yields no paths and the section still counts as
    present.
    """
    found: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line.startswith(("-", "*")):
            continue
        item = line[1:].strip().strip("`").strip()
        if not item or item.lower() == _NONE_LINE:
            continue
        if item.startswith("_") and item.endswith("_"):
            continue
        if item not in found:
            found.append(item)
    return tuple(found)


def read_declared_files(document: str) -> DeclaredFiles:
    """Read one task document's two file sections.

    This is a NEW reader on purpose. guardkit has one that does the opposite
    of what is wanted here — it treats a section holding only ``- _none_`` as
    absent and returns the two sections merged into one set — and two of its
    tests pin that behaviour, so it keeps its own job and this keeps its own
    rules. Never raises.
    """
    text = str(document or "")
    create_body = _section_body(text, _CREATE_HEADING)
    modify_body = _section_body(text, _MODIFY_HEADING)
    return DeclaredFiles(
        create=_paths_in(create_body or ""),
        modify=_paths_in(modify_body or ""),
        create_present=create_body is not None,
        modify_present=modify_body is not None,
    )


def files_the_plan_named(
    plan_documents: Mapping[str, str],
) -> tuple[tuple[str, ...], bool]:
    """Every file the plan of record declares, and whether it declared at all.

    The second half of the answer is the honest half: a plan whose task
    documents carry neither section has told us nothing, and comparing a
    build against nothing would report every file it changed as a surprise.
    """
    named: list[str] = []
    declared = False
    for _path, document in sorted(dict(plan_documents or {}).items()):
        files = read_declared_files(document)
        declared = declared or files.declared_anything
        for name in files.all_files:
            if name not in named:
                named.append(name)
    return tuple(named), declared


def _repo_path(path: str) -> str:
    """One repository-relative path, written the one way this module reads.

    Backslashes become slashes and a leading ``./`` is dropped. Only a
    leading ``./`` — the earlier version stripped the characters ``.`` and
    ``/`` one by one, which quietly turned ``.guardkit/features/FEAT-1.yaml``
    into ``guardkit/features/FEAT-1.yaml`` and so hid the factory's own
    feature file from every folder rule below it.
    """
    cleaned = str(path or "").replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _pieces_of_a_web_address(route: str) -> tuple[str, ...]:
    """A web address and every run of whole parts inside it.

    ``/users/created-per-day`` gives ``/users``, ``/created-per-day`` and
    ``/users/created-per-day``. This exists because a router declares its
    address in pieces — ``APIRouter(prefix="/users")`` on one line and
    ``@router.get("/created-per-day")`` on another — and each piece read on
    its own looked like an address the request never named. Driven over the
    real build branches, that put two false sentences on almost every card,
    while the address that really did move,
    ``/stats/users-created-per-day``, is no run of parts of the request's
    own address and is still named.
    """
    parts = [part for part in str(route or "").strip("/").split("/") if part]
    pieces: list[str] = []
    for start in range(len(parts)):
        for end in range(start + 1, len(parts) + 1):
            piece = "/" + "/".join(parts[start:end])
            if piece not in pieces:
                pieces.append(piece)
    return tuple(pieces)


def _is_scaffolding(path: str) -> bool:
    """True for a file that is not the code this build was asked for: a test
    file, a documentation page, or one of the factory's own planning papers.

    The first two are the ordinary cost of building the thing that was asked
    for. The third is the factory's own writing — the feature file, the
    specification input, the feature and its task documents, the QA pass bars
    and gate scripts — which rides on every routine build branch because the
    planning commits come before the coder's, and which nobody asked for in
    the sense the merge card means.
    """
    from forge.pipeline.merge_ready_checkpoint import path_is_test

    cleaned = _repo_path(path)
    if not cleaned:
        return False
    if path_is_test(cleaned):
        return True
    first = cleaned.split("/", 1)[0].lower()
    return first in _DOC_FOLDERS or first in _FACTORY_PAPERWORK_FOLDERS


def _what_one_file_declares(text: str) -> list[str]:
    """The lines one file added that say something about what the software
    now does.

    Three kinds of line are dropped, because a web address or a capability
    word in one of them is not something this build does:

    * a note to a reader — a comment, which includes the ``#!/usr/bin/env``
      line at the top of a script;
    * the prose inside a docstring, where an example address such as
      ``/stats`` is an illustration and not a route the software answers at;
    * ordinary wiring — an import, or a logger being made.

    Never raises.
    """
    kept: list[str] = []
    inside_prose = False
    for line in str(text or "").splitlines():
        quotes = len(_A_BLOCK_OF_PROSE.findall(line))
        was_inside_prose = inside_prose
        if quotes % 2:
            inside_prose = not inside_prose
        if was_inside_prose or inside_prose or quotes:
            continue
        if _A_NOTE_TO_A_READER.match(line) or _ORDINARY_WIRING.search(line):
            continue
        kept.append(line)
    return kept


def what_the_branch_wrote(added_by_file: Mapping[str, str]) -> str:
    """Every line this branch added that says what the software now does, in
    a file that is the code the build was asked for.

    The web addresses are read here rather than in everything the branch
    touched, and for the same two reasons the capability words are. A test
    fixture holding ``permissions: {filesystem: {allowlist: [/tmp]}}`` was
    otherwise read as this build answering at ``/tmp``; the shebang on a QA
    gate script the factory itself wrote was read as ``/usr/bin/env``; and an
    example address in that script's docstring was read as ``/stats``. All
    three are false sentences on the card Rich taps to say merge. A web
    address the build really answers at is declared in the code it was asked
    for, so nothing true is lost by leaving the tests, the documentation, the
    factory's own paperwork and the commentary out. Never raises.
    """
    kept: list[str] = []
    for path, text in dict(added_by_file or {}).items():
        name = str(path or "").strip()
        if not name or _is_scaffolding(name):
            continue
        kept.extend(_what_one_file_declares(text))
    return "\n".join(kept)


def what_the_branch_declares(added_by_file: Mapping[str, str]) -> str:
    """What this branch DECLARES, as one piece of text to read capability
    words in: the names of the files it changed, and the lines it added to
    them that say something about what the software now does.

    Three kinds of line are left out, because a capability word in one of them
    is not a capability the build added:

    * everything in a test file or a documentation page — the same scaffolding
      rule the file comparison uses. A fixture holding the word "permissions",
      or ``import logging`` at the top of a test, says nothing about what was
      built;
    * a note to a reader — a comment, or the prose inside a docstring;
    * ordinary wiring — an import, or a logger being made.

    The file's own name IS kept, and deliberately: a build that adds
    ``migrations/0001_add_users.py`` has declared a database migration by
    putting the file there, whatever its lines say.

    WHY THIS EXISTS. The first version of this pass read every added line of
    the branch with no idea which file it came from, and the capability words,
    which were measured on the prose of task documents, then fired on ordinary
    code. Driven over one real 184-line commit it put a sentence about
    permissions on the merge card because a test fixture contained the word.
    A false sentence on the card Rich taps to say merge buries the true one
    next to it, so the reading is narrowed to what the branch actually
    declares. Never raises.
    """
    declared: list[str] = []
    for path, text in dict(added_by_file or {}).items():
        name = str(path or "").strip()
        if not name or _is_scaffolding(name):
            continue
        declared.append(name)
        declared.extend(_what_one_file_declares(text))
    return "\n".join(declared)


@dataclass
class ScopeReport:
    """What the scope pass found, and what it could not take a count of."""

    #: The branch's own changed files were read.
    read: bool = False
    #: One plain sentence for each thing that could not be read, joined.
    why_not: str | None = None
    #: The sentence the person sent, word for word, or ``None``.
    request: str | None = None
    #: Where that sentence came from, said plainly.
    request_source: str | None = None
    #: How many files this build changed.
    files_changed: int = 0
    #: The files the plan of record declared.
    files_the_plan_named: list[str] = field(default_factory=list)
    #: Files this build changed that no task document named, scaffolding
    #: aside.
    files_the_plan_did_not_name: list[str] = field(default_factory=list)
    #: Files this build changed that the plan did not name and that are a
    #: test or a documentation page — the ordinary cost of the work.
    files_allowed_as_scaffolding: list[str] = field(default_factory=list)
    #: The plan of record declared its files at all. False for every plan
    #: written before task documents carried the two sections, and the
    #: comparison is then not taken rather than reported as a sprawl.
    plan_read: bool = False
    #: The web addresses the request itself names.
    routes_in_the_request: list[str] = field(default_factory=list)
    #: The web addresses this branch wrote.
    routes_the_branch_declares: list[str] = field(default_factory=list)
    #: Those of them the request never named.
    routes_the_request_did_not_name: list[str] = field(default_factory=list)
    #: Capabilities this branch added that the request never asked for.
    capabilities_the_request_did_not_name: list[str] = field(default_factory=list)
    #: The comparison with the sentence was taken at all.
    routes_read: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The receipt, exactly as it is written to disk and put on the row."""
        return {
            "read": self.read,
            "why_not": self.why_not,
            "request": self.request,
            "request_source": self.request_source,
            "files_changed": self.files_changed,
            "files_the_plan_named": list(self.files_the_plan_named),
            "files_the_plan_did_not_name": list(self.files_the_plan_did_not_name),
            "files_allowed_as_scaffolding": list(self.files_allowed_as_scaffolding),
            "plan_read": self.plan_read,
            "routes_in_the_request": list(self.routes_in_the_request),
            "routes_the_branch_declares": list(self.routes_the_branch_declares),
            "routes_the_request_did_not_name": list(
                self.routes_the_request_did_not_name
            ),
            "capabilities_the_request_did_not_name": list(
                self.capabilities_the_request_did_not_name
            ),
            "routes_read": self.routes_read,
        }


def scope_of_the_build(
    *,
    reading: Any,
    request: str | None,
    request_source: str | None = None,
    request_why_not: str | None = None,
    test_roots: Sequence[str] | None = None,
) -> ScopeReport:
    """Compare a finished branch with its plan and with the request.

    ``reading`` is a :class:`~forge.pipeline.branch_scope.BranchScopeReading`
    (or anything carrying the same attributes). ``request`` is the sentence
    the person sent, or ``None`` when it could not be found — in which case
    the comparison with the sentence is simply not taken, and the report says
    so instead of reporting nothing wrong. ``request_why_not`` is the caller's
    own sentence for why it could not be found, used in place of the plain
    one below when the caller knows something more useful.

    Never raises.
    """
    from forge.pipeline.merge_ready_checkpoint import parse_changed_files
    from forge.planning.task_traceability import (
        _ALL_CAPABILITIES,
        _is_a_place_on_disk,
        _route_shaped,
    )

    report = ScopeReport(request=request or None, request_source=request_source)
    reasons: list[str] = []

    error = str(getattr(reading, "error", "") or "").strip()
    if error:
        report.why_not = error
        return report

    changes = parse_changed_files(str(getattr(reading, "name_status", "") or ""))
    changed_paths = [change.path for change in changes if str(change.path).strip()]
    report.read = True
    report.files_changed = len(changed_paths)

    # (a) AGAINST THE PLAN — the blast radius.
    named, declared = files_the_plan_named(
        getattr(reading, "plan_documents", {}) or {}
    )
    report.files_the_plan_named = list(named)
    report.plan_read = bool(declared)
    if declared:
        wanted = {_repo_path(name) for name in named}
        for path in changed_paths:
            cleaned = _repo_path(path)
            if cleaned in wanted:
                continue
            if _is_scaffolding(cleaned):
                report.files_allowed_as_scaffolding.append(path)
                continue
            report.files_the_plan_did_not_name.append(path)
    else:
        reasons.append(
            "the plan of record names no files in any of its task documents, "
            "so there was nothing to compare what this build changed against"
        )

    # (b) AGAINST THE SENTENCE — the web addresses and the capability words in
    # what the branch actually wrote. A different question from (a), asked of
    # the finished build rather than of the plan.
    added_by_file = getattr(reading, "added_by_file", None)
    # A reading that never said which file its lines came from cannot be
    # judged here, and a count nobody could take is never published as a count
    # of nothing.
    added_read_whole = bool(
        getattr(reading, "added_lines_read_whole", False)
    ) and isinstance(added_by_file, Mapping)
    if not request:
        reasons.append(
            str(request_why_not).strip()
            if request_why_not and str(request_why_not).strip()
            else (
                "the sentence this build was asked for could not be found, so "
                "what it built was not compared against it"
            )
        )
    elif not added_read_whole:
        reasons.append(
            "what this branch added could not be read whole, so what it "
            "built was not compared against the request"
        )
    else:
        report.routes_read = True
        # The web addresses are read in the code this build was asked for.
        # Read every file the branch touched instead and a path inside a test
        # fixture is reported as an address the build answers at.
        added = what_the_branch_wrote(added_by_file or {})
        roots = tuple(test_roots) if test_roots else ()
        in_request = [
            route.rstrip("/") for route in _route_shaped(request)
        ]
        report.routes_in_the_request = list(dict.fromkeys(in_request))
        # An address the request named, and every run of whole parts inside
        # it, counts as named: a router writes its address in pieces.
        known = {
            piece.lower()
            for route in report.routes_in_the_request
            for piece in _pieces_of_a_web_address(route)
        }
        declares: list[str] = []
        for route in _route_shaped(added):
            trimmed = route.rstrip("/")
            if _is_a_place_on_disk(trimmed, roots or ("tests", "test", "spec", "specs")):
                continue
            if trimmed not in declares:
                declares.append(trimmed)
        report.routes_the_branch_declares = declares
        report.routes_the_request_did_not_name = [
            route for route in declares if route.lower() not in known
        ]
        # The capability words are read in what the branch DECLARES — the
        # files it changed and the lines that say what the software now does —
        # and not in every line of source it wrote. Read the raw diff instead
        # and a test fixture or a bare import puts a sentence about a
        # capability on the merge card that nothing in the build supports.
        what_it_declares = what_the_branch_declares(added_by_file or {})
        # Read one line at a time, and never count a capability word that the
        # same line denies: two of twenty-three real build branches said "It
        # also added authentication" on the merge card purely because their
        # route description reads "This endpoint does not require
        # authentication."
        for capability, pattern in _ALL_CAPABILITIES:
            if not promises_the_capability(what_it_declares, pattern):
                continue
            if pattern.search(request):
                continue  # the person asked for it; a reading, not an addition
            if capability not in report.capabilities_the_request_did_not_name:
                report.capabilities_the_request_did_not_name.append(capability)

    # Each reason is its own sentence, and both starts and ends like one:
    # joined by a bare space they ran together into one unreadable line, and
    # joined only by a full stop the second one still began in lower case.
    def _as_a_sentence(reason: str) -> str:
        trimmed = str(reason).strip().rstrip(".")
        return trimmed[:1].upper() + trimmed[1:] if trimmed else ""

    report.why_not = (
        ". ".join(_as_a_sentence(reason) for reason in reasons) + "."
        if reasons
        else None
    )
    return report


def unread_scope(why_not: str) -> ScopeReport:
    """The report for a branch that could not be read at all."""
    return ScopeReport(read=False, why_not=str(why_not))


def write_scope_report(
    build_id: str, report: ScopeReport, *, receipts_dir: "Path | None" = None
) -> "Path | None":
    """Write the receipt BESIDE the build's own evidence. Never raises.

    It lands in the directory this build's receipts already live in, and it
    does not make that directory: a real build has one from its first line of
    output, and a build id nobody has ever run is not a reason to start a new
    tree of receipts somewhere. Returns the path it wrote, or ``None`` when
    it did not write — which is always a logged sentence and never a reason
    to hold up a merge card.
    """
    from forge.receipts import receipts_root

    try:
        root = (
            Path(receipts_dir)
            if receipts_dir is not None
            else receipts_root() / str(build_id)
        )
        if not root.is_dir():
            logger.info(
                "the scope pass: %s has no receipts directory at %s, so the "
                "scope receipt was not written — what it found is still on "
                "the card and on the build's own record",
                build_id,
                root,
            )
            return None
        path = root / SCOPE_REPORT_NAME
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return path
    except Exception as exc:  # noqa: BLE001 — a receipt never holds up a card
        logger.warning(
            "the scope pass: the scope receipt for %s could not be written "
            "(%s: %s) — the card still says what was found",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None
