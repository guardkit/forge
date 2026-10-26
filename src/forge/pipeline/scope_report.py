"""The scope pass — hold the finished build against its plan.

WHY (the planner fix, 2026-09-15). Twelve builds for one sentence: the
specification was right twelve times out of twelve and the plan behind it was
different every time. Something nobody asked for turned up in ten of the
twelve — five modules here, a database migration there. None of it reached the
card Rich taps to say merge.

So this asks one question of a finished build: which files did this build
change that no task document named? That is the blast radius, and "the plan
did not name it" is the right test for it, because a sentence names no files
at all. It also lists the files that already existed and that the branch
changed.

FACTS, NOT VERDICTS (step 2, 26 October 2026). The second half of this pass
read what the branch wrote for "web addresses the request did not name" and
"capabilities the request did not name". It read an ``echo`` line as a route
(first step 1 run, feature 2: the local merge judge declined good work over
it), a file path as a route (feature 8) and CORS headers as permissions
(re-run feature 7). It was deleted. The card keeps the changed files and the
diff, which whoever answers the card reads for themselves.

The answer lands in ``<receipts>/<build id>/scope_report.json`` at the moment
the merge card is offered, which is before anyone says merge, so one sentence
of it can ride on the card.

THE ONE RULE THIS MODULE WILL NOT BREAK: **a count nobody took is never
published as a count of nothing.** The branch and the plan's own declaration
of files can go unread, and the report says which, in ordinary words, rather
than printing a zero.

Pure: no git, no network, no database. It is handed the reading and the
request and it compares them. Never raises.
"""

from __future__ import annotations

import json
import logging
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
    "read_declared_files",
    "scope_of_the_build",
    "unread_scope",
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
    #: What the branch was held against: the commit the build started from,
    #: or the branch name used when the record named none.
    compared_against: str | None = None
    #: Every file that was already there at that commit and that this branch
    #: changed, deleted or renamed (a rename once, under its new name), each a
    #: :class:`~forge.pipeline.merge_ready_checkpoint.ExistingFileChange`.
    #: Repair 2 part B, 12 October 2026: a passing suite measured with these
    #: changes is not evidence that the original tests pass unchanged, so the
    #: card and this receipt both say which files they were. Empty when the
    #: branch only added files, and when the branch could not be read (then
    #: :attr:`read` is ``False`` and the card says so instead).
    files_that_already_existed: list[Any] = field(default_factory=list)
    #: Where this report was written (``<receipts>/<build id>/scope_report.json``),
    #: set once :func:`write_scope_report` has written it; ``None`` before
    #: then or when it could not be written. Not part of the receipt itself:
    #: it is where the receipt is. The card points at it when the list of
    #: files that already existed is too long to name in full.
    receipt_path: str | None = None
    #: ``False`` when this repository's own settings for where its tests live
    #: could not be read in time, so the files above were marked as tests by
    #: the usual test locations only. The list of files is complete either way.
    test_files_checked: bool = True
    #: Every change the branch made, parsed from git's ``name_status``
    #: (:class:`~forge.pipeline.merge_ready_checkpoint.BranchFileChange`), set
    #: only when the branch was read. Not part of the receipt: the merge card
    #: holds it against the files that judge the merge (15 October 2026).
    changes: tuple[Any, ...] | None = None

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
            "compared_against": self.compared_against,
            "files_that_already_existed": [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in self.files_that_already_existed
            ],
            "test_files_checked": self.test_files_checked,
        }


def scope_of_the_build(
    *,
    reading: Any,
    request: str | None,
    request_source: str | None = None,
    test_paths: Sequence[str] | None = None,
) -> ScopeReport:
    """Compare a finished branch with its plan.

    ``reading`` is a :class:`~forge.pipeline.branch_scope.BranchScopeReading`
    (or anything carrying the same attributes). ``request`` is the sentence
    the person sent, or ``None`` when it could not be found; it is carried on
    the receipt as it was sent and compared with nothing. ``test_paths`` are
    the path patterns this repository's tests live under, used only to mark
    which of the files that already existed are tests; ``None`` means the
    plain default.

    Never raises.
    """
    from forge.pipeline.merge_ready_checkpoint import (
        DEFAULT_TEST_PATHS,
        files_that_already_existed,
        parse_changed_files,
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

    # FILES THAT ALREADY EXISTED (repair 2 part B, 12 October 2026). From the
    # same reading, with no idea what kind of file anything is: whatever was
    # there at the base and this branch changed, the card names.
    report.files_that_already_existed = list(
        files_that_already_existed(
            changes, tuple(test_paths) if test_paths else DEFAULT_TEST_PATHS
        )
    )

    # AGAINST THE PLAN — the blast radius.
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

    # Each reason is its own sentence, and both starts and ends like one.
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
    build_id: str,
    report: ScopeReport,
    *,
    receipts_dir: "Path | None" = None,
    repo: "str | None" = None,
) -> "Path | None":
    """Write the receipt BESIDE the build's own evidence. Never raises.

    It lands in the directory this build's receipts already live in, and it
    does not make that directory: a real build has one from its first line of
    output, and a build id nobody has ever run is not a reason to start a new
    tree of receipts somewhere. Returns the path it wrote, or ``None`` when
    it did not write — which is always a logged sentence and never a reason
    to hold up a merge card.
    """
    from forge.receipts import receipts_root_for

    try:
        # The build's own project's receipts (one sandbox per project).
        root = (
            Path(receipts_dir)
            if receipts_dir is not None
            else receipts_root_for(repo) / str(build_id)
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
