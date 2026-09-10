"""The merge-ready checkpoint — one publisher, four call sites, no PRs.

Revival design pass §c (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

**The ruling (Rich, twice): no pull-request-review surfaces for Rich,
ever.** His acts per feature are exactly three — the spec word, the gate
tap, the merge word. The mode chains' "pull-request-review" leg is
pre-ruling vocabulary; the delivery primitive is a **gates-green branch
plus the merge word** (DF-021, approve-click delivery, settled live
2026-07-23).

One important finding the design pass pinned first: the conductor's
``pr_review_gate`` collaborator was never a GitHub machine. It is a
protocol over the FEAT-FORGE-004 *approval gate* — the approve-click card
surface. Nothing in the chain code has ever opened a pull request. So the
adaptation is (i) vocabulary, (ii) delivery shape, and (iii) one hard
precondition — not a rip-out.

What this module is
-------------------

:class:`MergeReadyCheckpointPublisher` is **one implementation** of the
``PRReviewGate`` protocol, wired at the four ``submit_decision`` call
sites the supervisor owns (the dispatch branch, the Mode B
post-autobuild route, the Mode C planner-chosen dispatch, the Mode C
terminal route). Its act, when the leg fires:

1. **Push the fixed branch** — MODELLED at this stage. The push is
   described, recorded on the decision and left to the injected
   ``push_branch`` seam; with no seam wired the decision honestly says
   ``pushed=False, push_modelled=True`` rather than claiming a push that
   never happened.
2. **Run the full gate set on it** — through the injected
   ``gates_green_reader``.
3. **On green, publish the merge card** — the SAME approve-click card the
   routine path already delivers, through the SAME publisher seam
   (``publish_card``, composed in the daemon over ``gate_check`` and the
   mirrored approval publisher). It never opens a PR, never renders a
   diff surface, never asks Rich to read code.

The laws it enforces
--------------------

* **Gates green first** (§c.3). A red gate is NEVER a card. It loops back
  into the fix cycle (the conductor's next review pass) or terminates
  FAILED with a failure pack. "Fix loops run BEFORE the merge word" made
  structural.
* **No-commit terminals stay silent** (§c.6). A journey that finds
  nothing to fix, or produces no commits, ends with a receipt — not a
  card. Rich hears about work only when there is a merge word to say.
* **The specification is not the machine's to edit** (Rich's ruling,
  2026-09-09). Before a card is published the checkpoint reads which files
  the journey's branch changed against its base. A branch that changes the
  files the repository calls its specification, or a line recording
  somebody's approval, is a RED checkpoint: no card, and one plain sentence
  naming the files and what was done to them. See the fence section below.
* **Never auto-merge** (§c.4, ADR-ARCH-026). ``auto_approve=True`` is
  refused here as well as by the constitutional guard upstream: belt and
  braces on "the merge word is human forever".
* **ONE card per journey — the publish latch** (§c.5 / risk h.5, Stage 2
  shakeout item 6). The moment a publish is *attempted* on a build the
  checkpoint latches: any later ``submit_decision`` for that build answers
  :attr:`MergeCardOutcome.ALREADY_CHECKPOINTED` and publishes nothing.
  The latch has **two halves** — an in-process set and an injected
  DURABLE probe over the gate's own rows, because the publisher is built
  fresh per build and a restart would otherwise re-card. See
  :meth:`MergeReadyCheckpointPublisher._already_carded`, which also names
  the one residual window this does not close.

  Why a latch and not a re-issue. Before this, a publish that raised
  reported ``PUBLISH_FAILED``, the supervisor mapped that to ``WAITING``,
  and the driver re-planned — so a single flaky publish could re-issue the
  card up to three more times. Three cards for one merge word is act
  inflation dressed up as a retry. And re-issuing is not even the estate's
  mechanism for a lost card: the merge card rides the SAME approve-click
  machinery as the pre-dispatch gate, whose ``request_id`` is durable and
  whose re-emit is owned by ``rearm_paused_gates`` at boot and by the
  subscriber's refresh loop within the window. Re-publishing from here
  would duplicate a job something else already owns, against a row that is
  already PAUSED. So a raised publish is an **honest terminal** carrying
  ``card_may_be_on_the_wire`` — the journey stops, the pack says what
  happened, and the existing rearm path is what gets the card in front of
  the owner.

The stage enum is deliberately NOT renamed — ``PULL_REQUEST_REVIEW``
names durable ``stage_log`` rows and renaming it would be cosmetic churn
across history. The *user surfaces* speak the plain name
(:data:`MERGE_READY_CHECKPOINT_LABEL`); the codename stays in the code.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_MARKER",
    "DECLARED_TEST_EVIDENCE_KEY",
    "DECLARED_TEST_OUTPUT_FILENAME",
    "DEFAULT_SPECIFICATION_PATHS",
    "MERGE_READY_CHECKPOINT_LABEL",
    "ApprovalLineChange",
    "BranchFileChange",
    "GateStatus",
    "GatesReport",
    "MergeCardDecision",
    "MergeCardOutcome",
    "MergeReadyCheckpointPublisher",
    "RedGateAction",
    "SpecificationFenceReport",
    "SpecificationFenceStatus",
    "approval_line_owner",
    "judge_branch_changes",
    "parse_changed_files",
    "parse_changed_approval_lines",
    "path_is_specification",
    "unreadable_branch_changes",
    "unreadable_specification_declaration",
]


#: The phrase-book plain name. This string is what a human reads — the
#: approval card's stage copy and every operator-facing message. The
#: codename (``pull-request-review``) stays on the durable stage rows.
MERGE_READY_CHECKPOINT_LABEL: str = "the merge-ready checkpoint"

#: Where the declared test's own output is filed on a decision's ``details``,
#: so anything reading a decision back — the receipts writer, an operator
#: looking at the run report — finds it under one agreed name.
DECLARED_TEST_EVIDENCE_KEY: str = "declared_test_evidence"

#: The file that output is written to in this checkpoint's receipts stage.
#: A plain text file, readable with ``cat``, sitting beside the turn's
#: rationale in ``<receipts>/<build_id>/stages/<NNN>-pull-request-review/``.
DECLARED_TEST_OUTPUT_FILENAME: str = "declared-test-output.txt"


class GateStatus(StrEnum):
    """Outcome of the full gate set on the candidate branch.

    Members:
        GREEN: Every gate passed. The only status that may publish a card.
        RED: At least one gate failed.
        UNKNOWN: The gate set could not be evaluated (no reader wired, a
            reader raised, a probe timed out). Treated exactly as RED —
            the precondition is "proven green", never "not proven red".
    """

    GREEN = "green"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GatesReport:
    """What the gate set said about the candidate branch.

    Attributes:
        status: The :class:`GateStatus`.
        failed_gates: Names of the gates that failed (empty when green).
        detail: Free-form summary, recorded verbatim on the decision.
        deferred_detail: One plain sentence about the stamped checks that have
            no evidence here, left to this repository's own merge press, which
            stands the candidate up in its sandbox and runs the repository's
            live gate on it before anything lands (ruled 2026-09-08). It is
            recorded on the decision (``MergeCardDecision.rationale``) and
            logged. This sentence itself does NOT appear on the face of the
            Slack card — it names the checks by their own ids, which mean
            nothing to the person holding the card. Since 2026-09-09 the
            card seam is handed this whole report and says the same fact in
            ordinary words: some checks could not be proved on this branch
            here, and they are run against the candidate in the sandbox
            before anything is merged. Empty for
            every gate set that defers nothing, which is every one until an
            operator gives a repository a sandbox and a candidate check.
        evidence: What the declared test command itself printed, kept so the
            failing tests can be read back without running the suite again
            (ruled 2026-09-08, after a red checkpoint kept nothing but the
            run's last line and the two failing tests had to be found by
            hand). It is the test tool's own lines naming what failed, when
            it wrote any, plus the tail of the output, bounded in size by the
            reader that fills it in. Empty by default, so every gate set that
            has no output to keep — and every existing caller — is exactly
            what it was.
    """

    status: GateStatus
    failed_gates: tuple[str, ...] = ()
    detail: str = ""
    deferred_detail: str = ""
    evidence: str = ""

    @property
    def is_green(self) -> bool:
        """``True`` only for a proven-green gate set."""
        return self.status is GateStatus.GREEN


class RedGateAction(StrEnum):
    """What a red gate does instead of publishing a card (§c.3).

    Members:
        LOOP_BACK: Return to the fix cycle — the conductor's next review
            pass picks the branch up again. The default.
        TERMINATE_FAILED: Stop the journey FAILED and leave a failure
            pack. Chosen when there is no cycle left to loop into.
    """

    LOOP_BACK = "loop-back"
    TERMINATE_FAILED = "terminate-failed"


# ---------------------------------------------------------------------------
# THE SPECIFICATION FENCE — the specification is not the machine's to edit
# (Rich's ruling, 2026-09-09)
# ---------------------------------------------------------------------------
#
# What happened, 2026-09-09, build ``build-FEAT-39F6-20260909195749``. The
# follow-up review asked in ordinary words for an acceptance twin to be
# "updated". The work leg did exactly that: it renamed
# ``qa/twins/users-delete-by-email/double-delete-honest-404.hurl`` to
# ``…-410.hurl``, changed the first delete's expected response from 204 to
# 410, rewrote the scenario's wording, and edited the line recording the
# owner's own ruling — keeping his name and the date on a sentence he never
# said. The twins ARE what the candidate check measures the running
# application against. Had it reached this checkpoint the suite would have
# passed (no unit test reads the twins), a green card would have gone to the
# owner, and the candidate check would have measured the code against a
# specification the code had just rewritten. Nothing forbade any of it.
#
# So, before a card is published, the checkpoint reads WHICH FILES THE
# JOURNEY'S BRANCH CHANGED against its base and refuses when any of them is
# the repository's specification, or when the change touches a line that
# records somebody's approval. Two separate rules, named separately, both a
# RED checkpoint: no card, one plain sentence naming the files and what was
# done to them, and the existing red-gate path carries it — back to a review
# leg to revert the edit while a review cycle remains, FAILED naming the
# files when none does.
#
# WHERE A REPOSITORY SAYS WHICH FILES ARE ITS SPECIFICATION. In the place it
# already declares things about its gates: ``.guardkit/config.yaml``, beside
# the ``toolchain:`` block the merge-ready checks already read, under a
# ``specification:`` key —
#
#     specification:
#       paths:
#         - "qa/twins/**"
#
# and a repository that says nothing gets :data:`DEFAULT_SPECIFICATION_PATHS`.
#
# HOW A DECLARED PATH IS READ. ``**`` crosses directory boundaries; ``*`` and
# ``?`` stop at a slash; and a path with no wildcard in it at all, or one
# written with a trailing slash, names a directory and everything beneath it.
# So ``qa/twins``, ``qa/twins/`` and ``qa/twins/**`` all say the same thing,
# and a repository cannot be left protected on nothing by naming the
# directory it means (:func:`_glob_matcher`).
# The declaration is read from the CANONICAL tree, never from the worktree
# the journey has been editing — the same law the toolchain declaration is
# read under, and for the same reason: a branch that could rewrite the
# declaration could free itself.
#
# SAYING NOTHING AND SAYING SOMETHING NOBODY CAN HEAR ARE DIFFERENT THINGS.
# No declaration file at all means "the default is my shape", and the default
# applies. A declaration file that is there and cannot be read or parsed is a
# reading that did not happen, and refuses — because reading it as "declares
# nothing" would fence the DEFAULT paths in place of the ones it names, which
# for a repository whose specification lives somewhere else is LESS
# protection, not more (:func:`unreadable_specification_declaration`).

#: What a repository's specification is when it declares nothing: the
#: acceptance twins under ``qa/twins/``. That is api_test's own shape and the
#: one the 2026-09-09 incident used.
DEFAULT_SPECIFICATION_PATHS: tuple[str, ...] = ("qa/twins/**",)

#: The word a recorded approval carries. It is deliberately the whole rule's
#: first half: a changed line must hold this word AND name the person whose
#: approval it records before the second rule fires, so a line saying
#: "approved" in passing is not an owner's sentence.
APPROVAL_MARKER: str = "APPROVED"

#: "APPROVED … by <Name>" — the shape of a recorded approval. The word, then
#: somebody's name after "by". ``# APPROVED AS PROPOSED by Rich 2026-07-28``
#: is the line the incident rewrote; this is what recognises it wherever it
#: lives, in any file, in any repository.
_APPROVAL_LINE = re.compile(
    r"\bAPPROVED\b.*?\b[Bb][Yy]\b\s+([A-Z][A-Za-z.'’-]*)"
)


class SpecificationFenceStatus(StrEnum):
    """What the fence made of the branch's own changes.

    Members:
        CLEAR: The branch's changes were read and none of them is the
            repository's specification or an owner's recorded approval.
            **The only status that lets the checkpoint carry on.**
        REFUSED: The branch changes the specification, or a recorded
            approval, or both.
        UNREADABLE: What the branch changed could not be read at all. Not a
            pass: a check that could not run must never become a green card,
            so this refuses too and says which it is.
    """

    CLEAR = "clear"
    REFUSED = "refused"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class BranchFileChange:
    """One file the branch changed against its base.

    Attributes:
        status: git's own letter — ``A`` added, ``M`` changed, ``D``
            deleted, ``R`` renamed, ``C`` copied, ``T`` type changed.
        path: The file's path after the change, relative to the repository.
        old_path: Where a renamed or copied file came from; empty otherwise.
    """

    status: str
    path: str
    old_path: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path this change touches — both ends of a rename."""
        return (self.path, self.old_path) if self.old_path else (self.path,)

    def says_what_happened(self) -> str:
        """One plain phrase: what the branch did to this file."""
        letter = (self.status or "").upper()[:1]
        if letter == "R" and self.old_path:
            return f"{self.old_path} (renamed to {self.path})"
        if letter == "C" and self.old_path:
            return f"{self.path} (copied from {self.old_path})"
        if letter == "A":
            return f"{self.path} (added)"
        if letter == "D":
            return f"{self.path} (deleted)"
        return f"{self.path} (changed)"


@dataclass(frozen=True, slots=True)
class ApprovalLineChange:
    """One line the branch added or removed that records somebody's approval.

    Attributes:
        path: The file the line is in.
        line: The line itself, as git printed it, without its ``+``/``-``.
        added: ``True`` for a line the branch adds, ``False`` for one it
            removes. An edit shows up as both, which is exactly what the
            incident's rewrite of the owner's ruling was.
        owner: The name the line records the approval against.
    """

    path: str
    line: str
    added: bool
    owner: str = ""


@dataclass(frozen=True, slots=True)
class SpecificationFenceReport:
    """What the fence says about one branch.

    Attributes:
        status: The :class:`SpecificationFenceStatus`.
        detail: The plain sentence a person reads — on the log line, on the
            decision and in the receipts. Empty when the branch is clear.
        failed_gates: The refusal's own names, one per rule that fired, each
            carrying the files it fired on, so the sentence that closes a
            journey out names them too.
        specification_files: What was changed under the specification rule.
        approval_files: What was changed under the recorded-approval rule.
    """

    status: SpecificationFenceStatus
    detail: str = ""
    failed_gates: tuple[str, ...] = ()
    specification_files: tuple[str, ...] = ()
    approval_files: tuple[str, ...] = ()

    @property
    def refuses(self) -> bool:
        """``True`` when no card may be published on this branch."""
        return self.status is not SpecificationFenceStatus.CLEAR


@functools.lru_cache(maxsize=256)
def _glob_matcher(pattern: str) -> re.Pattern[str]:
    """One declared path pattern, as a matcher.

    ``**`` crosses directory boundaries and ``*`` and ``?`` do not, so
    ``qa/twins/**`` means "everything under qa/twins" and ``qa/*.hurl`` means
    the twins directly in ``qa`` and no deeper.

    A pattern that names a plain directory covers everything beneath it:
    ``qa/twins``, ``qa/twins/`` and ``qa/twins/**`` all protect every file
    under ``qa/twins``. A repository that writes ``qa/twins`` means the
    twins, not one file with that exact name and no directory of its own —
    and reading it the literal way would leave that repository protected on
    nothing at all, silently, which is the one thing this fence may never do.
    So a pattern with no ``*`` or ``?`` anywhere in it, and any pattern
    ending in ``/``, is read as that path and everything below it. Wildcards
    are left exactly as written, because a repository that writes one is
    saying where it wants the match to stop.
    """
    cleaned = str(pattern or "").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    covers_what_is_beneath = cleaned.endswith("/") or not any(
        char in cleaned for char in "*?"
    )
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        # A pattern that names nothing protects nothing; ``(?!)`` never
        # matches, so an empty entry can never widen the fence to everything.
        return re.compile(r"(?!)")
    pattern = cleaned
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    tail = r"(?:/.*)?\Z" if covers_what_is_beneath else r"\Z"
    return re.compile("".join(out) + tail)


def path_is_specification(path: str, patterns: "tuple[str, ...]") -> bool:
    """Is ``path`` one of the files this repository calls its specification?

    A declared pattern that names a plain directory — ``qa/twins`` — covers
    every file beneath it, so a repository cannot end up protected on nothing
    by writing the directory it means. See :func:`_glob_matcher`.
    """
    cleaned = str(path or "").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return False
    return any(_glob_matcher(pattern).match(cleaned) for pattern in patterns)


def approval_line_owner(line: str) -> str:
    """The name a recorded-approval line names, or ``""`` when it is not one.

    The rule reads the CHANGE, not the path: a line holding ``APPROVED`` and
    naming whose approval it is, wherever the file lives.
    """
    if APPROVAL_MARKER not in (line or ""):
        return ""
    found = _APPROVAL_LINE.search(line)
    return found.group(1) if found else ""


def parse_changed_files(name_status: str) -> "tuple[BranchFileChange, ...]":
    """git's ``diff --name-status -M -z`` output, as changes.

    ``-z`` writes NUL-separated fields: a letter, then one path, except for a
    rename or a copy which write the letter (with its similarity score) and
    then TWO paths. Parsing the machine-readable form rather than the tabbed
    one means a path with a space, a tab or a quote in it is read exactly as
    git wrote it.
    """
    fields = [field for field in str(name_status or "").split("\0") if field != ""]
    changes: list[BranchFileChange] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        letter = status[:1].upper()
        index += 1
        if letter in ("R", "C"):
            if index + 1 >= len(fields):
                break
            changes.append(
                BranchFileChange(
                    status=letter, path=fields[index + 1], old_path=fields[index]
                )
            )
            index += 2
            continue
        if index >= len(fields):
            break
        changes.append(BranchFileChange(status=letter or "M", path=fields[index]))
        index += 1
    return tuple(changes)


def parse_changed_approval_lines(patch: str) -> "tuple[ApprovalLineChange, ...]":
    """A unified diff, as the recorded-approval lines it adds or removes.

    The patch this reads is git's own, taken with renames turned OFF on
    purpose: with renames on, a file moved wholesale carries no changed lines
    at all and a moved approval would be invisible. With them off the same
    move is a removal and an addition, which is what it is.

    WHERE THE HEADERS ARE MATTERS. ``--- a/file`` and ``+++ b/file`` name the
    two sides of a file, and they only ever appear between ``diff --git`` and
    that file's first ``@@``. Inside a hunk, a line beginning ``---`` is
    CONTENT: it is a deleted line whose own text starts with ``--``, which is
    how a comment is written in SQL, Lua and Haskell — so a deleted
    ``-- APPROVED AS PROPOSED by Rich 2026-07-28`` prints as
    ``--- APPROVED AS PROPOSED by Rich 2026-07-28``. Reading that as a header
    would miss the very removal rule (b) names, and would leave the file's
    name wrong for the rest of its hunks as well. So the two header shapes
    are honoured only outside a hunk: ``@@`` opens one, and the next
    ``diff --git`` closes it.
    """
    lines: list[ApprovalLineChange] = []
    added_file = ""
    removed_file = ""
    inside_a_hunk = False
    for raw in str(patch or "").splitlines():
        if raw.startswith("diff --git "):
            inside_a_hunk = False
            added_file = ""
            removed_file = ""
            continue
        if raw.startswith("@@"):
            inside_a_hunk = True
            continue
        if not inside_a_hunk:
            if raw.startswith("+++ "):
                added_file = _patch_path(raw[4:])
                continue
            if raw.startswith("--- "):
                removed_file = _patch_path(raw[4:])
                continue
        if not raw.startswith(("+", "-")):
            continue
        added = raw.startswith("+")
        body = raw[1:]
        owner = approval_line_owner(body)
        if not owner:
            continue
        path = (added_file if added else removed_file) or removed_file or added_file
        lines.append(
            ApprovalLineChange(
                path=path, line=body.strip(), added=added, owner=owner
            )
        )
    return tuple(lines)


def _patch_path(token: str) -> str:
    """``a/qa/twins/x.hurl`` → ``qa/twins/x.hurl``; ``/dev/null`` → ``""``."""
    cleaned = token.strip().split("\t", 1)[0].strip()
    if cleaned in ("/dev/null", ""):
        return ""
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) > 1:
        cleaned = cleaned[1:-1]
    for prefix in ("a/", "b/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _and_list(items: "tuple[str, ...]") -> str:
    return ", ".join(items)


def judge_branch_changes(
    *,
    changes: "tuple[BranchFileChange, ...]",
    approval_lines: "tuple[ApprovalLineChange, ...]",
    specification_paths: "tuple[str, ...]",
) -> SpecificationFenceReport:
    """The two rules, applied to one branch's own changes.

    Rule one reads the PATHS: a change to a file the repository calls its
    specification — both ends of a rename, because a twin renamed away is a
    twin removed. Rule two reads the CHANGE: a line holding ``APPROVED`` and
    naming whose approval it is, added or removed, in any file anywhere.
    They are named separately because they are different wrongs, and a
    branch that does both is told both.
    """
    spec_changes = tuple(
        change
        for change in changes
        if any(path_is_specification(path, specification_paths) for path in change.paths)
    )
    spec_files = tuple(change.says_what_happened() for change in spec_changes)

    approval_files: list[str] = []
    for path in dict.fromkeys(line.path for line in approval_lines):
        owners = tuple(
            dict.fromkeys(
                line.owner
                for line in approval_lines
                if line.path == path and line.owner
            )
        )
        named = " and ".join(owners)
        where = path or "a file the diff did not name"
        approval_files.append(
            f"{where} (the line recording {named}'s approval)"
            if named
            else f"{where} (a recorded approval)"
        )

    if not spec_files and not approval_files:
        return SpecificationFenceReport(status=SpecificationFenceStatus.CLEAR)

    sentences: list[str] = []
    gates: list[str] = []
    if spec_files:
        sentences.append(
            "the branch changes files this repository calls its "
            f"specification: {_and_list(spec_files)} — a specification change "
            "is the owner's to make, so no card was published"
        )
        gates.append(f"the repository's specification: {_and_list(spec_files)}")
    if approval_files:
        sentences.append(
            "the branch changes a line that records an approval: "
            f"{_and_list(tuple(approval_files))} — a recorded approval is the "
            "owner's own word, so no card was published"
        )
        gates.append(
            f"a recorded approval: {_and_list(tuple(approval_files))}"
        )
    return SpecificationFenceReport(
        status=SpecificationFenceStatus.REFUSED,
        detail=". ".join(sentences),
        failed_gates=tuple(gates),
        specification_files=spec_files,
        approval_files=tuple(approval_files),
    )


def unreadable_branch_changes(reason: str) -> SpecificationFenceReport:
    """What the fence answers when it could not read the branch at all.

    Never a pass. The whole point of the fence is that a card says the branch
    was looked at; a card published on a branch nobody could read would say
    something untrue.
    """
    return SpecificationFenceReport(
        status=SpecificationFenceStatus.UNREADABLE,
        detail=(
            "what this branch changed could not be read "
            f"({reason}), so it cannot be shown that the repository's "
            "specification and its recorded approvals are untouched, and no "
            "card was published"
        ),
        failed_gates=("what the branch changed could not be read",),
    )


def unreadable_specification_declaration(reason: str) -> SpecificationFenceReport:
    """What the fence answers when the repository's own declaration of which
    files are its specification is THERE and could not be read.

    Not the same thing as a repository that declares nothing. A repository
    with no declaration file has said, plainly, "the default is my shape",
    and the default applies. A declaration that exists but cannot be read or
    parsed has said something nobody could hear — and reading it as "declares
    nothing" would quietly protect the DEFAULT paths instead of the ones it
    names, which for a repository whose specification lives somewhere else is
    less protection, not more. So it refuses, like every other reading that
    did not happen.
    """
    return SpecificationFenceReport(
        status=SpecificationFenceStatus.UNREADABLE,
        detail=(
            "which files this repository calls its specification could not be "
            f"read ({reason}), so it cannot be shown that its specification "
            "and its recorded approvals are untouched, and no card was "
            "published"
        ),
        failed_gates=(
            "which files this repository calls its specification could not be read",
        ),
    )


class MergeCardOutcome(StrEnum):
    """The closed set of things the merge-ready checkpoint can do.

    Members:
        CARD_PUBLISHED: Gates green, the merge card is out, Rich's merge
            word is the only remaining act. **The only member that means
            a card was published.**
        NO_COMMITS_SILENT: Nothing was committed — a receipt, no card
            (§c.6).
        RED_GATE_LOOP_BACK: A gate is red; back into the fix cycle.
        RED_GATE_FAILED: A gate is red and there is no cycle left; FAILED
            with a pack.
        PUBLISH_FAILED: The gates were green and the card publish RAISED.
            The envelope may already be on the wire, so this is a
            **terminal** — never a retry. See the class-level note below.
        DELIVERY_NOT_WIRED: The gates were green and no card publisher is
            wired at all (the shadow-replay posture, design pass Stage 2
            where delivery is deliberately OFF). Nothing reached the wire
            and nothing will; the journey ends honestly rather than
            waiting for a delivery that has no mechanism.
        ALREADY_CHECKPOINTED: A checkpoint already fired for this build
            and its publish was attempted. Bounded to the ONE card
            (design pass risk h.5) — this decision publishes nothing.
    """

    CARD_PUBLISHED = "card-published"
    NO_COMMITS_SILENT = "no-commits-silent"
    RED_GATE_LOOP_BACK = "red-gate-loop-back"
    RED_GATE_FAILED = "red-gate-failed"
    PUBLISH_FAILED = "publish-failed"
    DELIVERY_NOT_WIRED = "delivery-not-wired"
    ALREADY_CHECKPOINTED = "already-checkpointed"


@dataclass(frozen=True, slots=True)
class MergeCardDecision:
    """Structured result of one merge-ready checkpoint firing.

    Returned from ``submit_decision`` and threaded onto the supervisor's
    :class:`~forge.pipeline.supervisor.TurnReport` as
    ``dispatch_result``, so the audit trail records exactly what the leg
    did — including, crucially, whether a card was published.

    Attributes:
        outcome: The :class:`MergeCardOutcome`.
        build_id / feature_id: Identity of the checkpoint.
        card_published: The h.5 audit's counter. ``True`` for exactly one
            decision per fix journey, and zero for a clean/no-commit run.
        branch: Branch the checkpoint ran against, when known.
        pushed: Whether a real push happened (an injected ``push_branch``
            seam ran and reported success).
        push_modelled: ``True`` when the push step was described but not
            executed — the honest Stage-1 posture.
        gates: The :class:`GatesReport` the precondition read.
        auto_approve_refused: ``True`` when a caller asked for
            auto-approve and this publisher refused it.
        rationale: The rationale carried through from the caller, plus
            this leg's own note.
        card_result: Whatever the card publisher returned — for the
            production publisher this is the owner's verdict on the card
            (approved / declined / expired). Never interpreted here; the
            driver loop reads it to pick the honest outcome WORD for the
            run report (Stage 2 shakeout item 7).
        card_may_be_on_the_wire: ``True`` when a publish was attempted and
            raised, so the envelope may or may not have reached the owner.
            The reason this decision is terminal rather than retried.
        failure_pack: Path of the failure pack written on the
            TERMINATE_FAILED branch, when one was written.
    """

    outcome: MergeCardOutcome
    build_id: str
    feature_id: str = ""
    card_published: bool = False
    branch: str | None = None
    pushed: bool = False
    push_modelled: bool = True
    gates: GatesReport | None = None
    auto_approve_refused: bool = False
    rationale: str = ""
    card_result: Any | None = None
    card_may_be_on_the_wire: bool = False
    failure_pack: Any | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_terminal_failed(self) -> bool:
        """``True`` when the journey should end FAILED on this decision."""
        return self.outcome is MergeCardOutcome.RED_GATE_FAILED

    @property
    def loops_back(self) -> bool:
        """``True`` when the journey should re-enter the fix cycle."""
        return self.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it unchanged.

    Every injected seam here may be sync (tests, in-memory fakes) or
    async (the production wire). Normalising once at the boundary keeps
    both shapes first-class without a second publisher implementation.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class MergeReadyCheckpointPublisher:
    """The one merge-card publisher behind every ``submit_decision``.

    Satisfies :class:`~forge.pipeline.supervisor.PRReviewGate` (the
    supervisor awaits the result, so ``submit_decision`` may be async).

    Args:
        publish_card: ``(*, build_id, feature_id, rationale, branch,
            gates) -> Any`` — the approve-click card seam. In production
            this is composed in the daemon over the SAME ``gate_check`` +
            mirrored-approval-publisher machinery the routine path uses;
            this class never builds an envelope of its own. ``None``
            means no delivery is wired: the checkpoint still runs its
            precondition and reports honestly (used by shadow replays,
            design pass Stage 2, where delivery is deliberately OFF).
        gates_green_reader: ``(build_id, branch) -> GatesReport | bool``
            — the full gate set on the candidate branch. ``None`` or a
            raise yields :attr:`GateStatus.UNKNOWN`, which is treated as
            red. The precondition is "proven green".
        has_commits_probe: ``(build_id) -> bool`` — the belt on §c.6. A
            checkpoint reached with no commits publishes nothing.
            ``None`` skips the belt (the callers' own no-commit
            terminals already stay silent).
        branch_reader: ``(build_id) -> str | None`` — the branch to push
            and gate.
        push_branch: ``(build_id, branch) -> bool`` — the real push. When
            ``None`` (the Stage-1 default) the push step is MODELLED:
            described, recorded, not executed.
        red_gate_action: ``(build_id, GatesReport) -> RedGateAction`` —
            loop back into the fix cycle, or terminate FAILED. Defaults
            to :attr:`RedGateAction.LOOP_BACK`.
        failure_pack_writer: ``(*, build_id, feature_id, reason, gates)
            -> Any`` — writes the journey's own failure pack on the
            TERMINATE_FAILED branch.
        specification_fence: ``(*, build_id, branch) ->
            SpecificationFenceReport`` — **the specification fence**
            (Rich's ruling, 2026-09-09). Reads which files the journey's
            branch changed against its base and answers whether any of them
            is the repository's specification, or carries somebody's
            recorded approval. A report that refuses is a RED checkpoint on
            the existing red-gate path: no card, and the plain sentence
            naming the files. ``None`` — every caller that predates the
            fence — means no fence runs at all and the checkpoint is byte
            for byte what it was.
        published_probe: ``(build_id) -> bool`` — **the DURABLE half of
            the one-card latch.** ``True`` when a merge card has already
            been published for this build according to a durable row
            (production reads the gate's own ``stage_log`` rows). ``None``
            leaves the latch in-memory-only, which is what every test
            double and every pre-existing caller gets. See
            :meth:`_already_carded` for the exact residual window this
            closes and the one it does not.
    """

    def __init__(
        self,
        *,
        publish_card: Callable[..., Any | Awaitable[Any]] | None = None,
        gates_green_reader: Callable[..., Any] | None = None,
        has_commits_probe: Callable[[str], Any] | None = None,
        branch_reader: Callable[[str], Any] | None = None,
        push_branch: Callable[..., Any] | None = None,
        red_gate_action: Callable[[str, GatesReport], RedGateAction] | None = None,
        failure_pack_writer: Callable[..., Any] | None = None,
        published_probe: Callable[[str], Any] | None = None,
        specification_fence: Callable[..., Any] | None = None,
    ) -> None:
        self._publish_card = publish_card
        self._gates_green_reader = gates_green_reader
        self._has_commits_probe = has_commits_probe
        self._branch_reader = branch_reader
        self._push_branch = push_branch
        self._red_gate_action = red_gate_action
        self._failure_pack_writer = failure_pack_writer
        self._published_probe = published_probe
        self._specification_fence = specification_fence
        # THE PUBLISH LATCH — build ids whose card publish has been
        # ATTEMPTED. Armed before the await, so even a raise inside the
        # publisher leaves it armed: "we may have put a card on the wire"
        # is the state that must never be retried. A red gate does NOT
        # arm it — looping back into the fix cycle and checkpointing again
        # later is the design, and that path never reached a publisher.
        #
        # This set is PROCESS state, which is why it is only half the
        # latch: see :meth:`_already_carded`.
        self._published: set[str] = set()

    async def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> MergeCardDecision:
        """Fire the merge-ready checkpoint for ``build_id``.

        The signature is the ``PRReviewGate`` protocol's verbatim, so all
        four supervisor call sites reach this one implementation without
        changing what they pass.
        """
        if await self._already_carded(build_id):
            # Bounded to the ONE card. See the class docstring's latch
            # note: a second checkpoint on a build whose publish was
            # already attempted publishes nothing, ever.
            logger.warning(
                "%s: build_id=%s has ALREADY had its card publish attempted "
                "— publishing NOTHING. A fix journey delivers exactly one "
                "merge card; re-issuing it would be act inflation, and the "
                "gate's own rearm/refresh path owns re-emitting a card the "
                "owner has not answered",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.ALREADY_CHECKPOINTED,
                build_id=build_id,
                feature_id=feature_id,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: already "
                    "checkpointed — one card per journey"
                ).strip(" |"),
            )

        auto_approve_refused = False
        if auto_approve:
            # ADR-ARCH-026 / §c.4 — the merge word is human forever. The
            # constitutional guard already vetoes this upstream; refusing
            # again here means no future wiring can route around it.
            auto_approve_refused = True
            logger.warning(
                "%s: auto_approve was requested for build_id=%s and is "
                "REFUSED — the merge word is human forever (ADR-ARCH-026); "
                "publishing the card for a human decision instead",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )

        branch = await self._read_branch(build_id)

        # §c.6 — no commits, no card. A receipt is the whole delivery.
        if self._has_commits_probe is not None:
            has_commits = await self._read_has_commits(build_id)
            if has_commits is False:
                logger.info(
                    "%s: build_id=%s produced no commits — ending with a "
                    "receipt, publishing NO card (design pass §c.6)",
                    MERGE_READY_CHECKPOINT_LABEL,
                    build_id,
                )
                return MergeCardDecision(
                    outcome=MergeCardOutcome.NO_COMMITS_SILENT,
                    build_id=build_id,
                    feature_id=feature_id,
                    branch=branch,
                    push_modelled=self._push_branch is None,
                    auto_approve_refused=auto_approve_refused,
                    rationale=(
                        f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: no "
                        "commits — receipt only, no card"
                    ).strip(" |"),
                )

        # Step (a0) — THE SPECIFICATION FENCE (Rich's ruling, 2026-09-09).
        # It runs BEFORE the push and before the suite for two reasons: a
        # branch that rewrote the specification cannot be carded whatever its
        # tests then say, and there is no sense spending fifteen minutes of
        # somebody's test suite on a branch that already cannot reach a card.
        # With no fence wired nothing runs here at all.
        fence = await self._read_specification_fence(build_id, branch)
        if fence is not None and getattr(fence, "refuses", False):
            fence_detail = str(getattr(fence, "detail", "") or "")
            logger.error(
                "%s: NO card is published for build_id=%s on branch=%s — %s",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
                branch,
                fence_detail,
            )
            return await self._red_gate_decision(
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=False,
                gates=GatesReport(
                    status=GateStatus.RED,
                    failed_gates=tuple(getattr(fence, "failed_gates", ()) or ()),
                    detail=fence_detail,
                ),
                auto_approve_refused=auto_approve_refused,
                rationale=rationale,
                evidence_details={},
            )

        # Step (a) — push the fixed branch. MODELLED at Stage 1.
        pushed = await self._push(build_id, branch)

        # Step (b) — the full gate set. THE HARD PRECONDITION.
        gates = await self._read_gates(build_id, branch)

        # WHAT THE TESTS THEMSELVES SAID. The gate set's reader keeps the
        # declared test command's own output (bounded), and it rides every
        # decision from here on under one agreed key, so the receipts writer
        # and anyone reading a decision back find it in the same place. A
        # gate set with nothing to keep adds no key at all, which is what
        # every decision looked like before this.
        evidence_details: dict[str, Any] = (
            {DECLARED_TEST_EVIDENCE_KEY: str(getattr(gates, "evidence", "") or "")}
            if str(getattr(gates, "evidence", "") or "").strip()
            else {}
        )

        if not gates.is_green:
            return await self._red_gate_decision(
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                rationale=rationale,
                evidence_details=evidence_details,
            )

        # Step (c) — green. Publish the approve-click merge card.
        if self._publish_card is None:
            logger.info(
                "%s: gates GREEN for build_id=%s but no card publisher is "
                "wired (delivery OFF) — recording the decision without "
                "publishing",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.DELIVERY_NOT_WIRED,
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                push_modelled=self._push_branch is None,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates "
                    "green, delivery not wired"
                ).strip(" |"),
                details={"delivery_wired": False, **evidence_details},
            )

        # ARM THE LATCH BEFORE THE AWAIT. From here on the envelope may
        # have reached the wire, and "may have" is the state that must
        # never be retried.
        self._published.add(build_id)
        try:
            card_result = await _maybe_await(
                self._publish_card(
                    build_id=build_id,
                    feature_id=feature_id,
                    rationale=rationale,
                    branch=branch,
                    gates=gates,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a publish failure is not a merge
            # TWO DIFFERENT THINGS, AND THE RECORD MUST NOT CONFUSE THEM.
            # A publisher that REFUSES says so on the exception it raises
            # (``card_reached_the_wire = False``): it decided not to offer
            # before writing anything, so no card exists and the journey can
            # say that plainly. Anything else — a raise from inside the
            # publish itself, or a publisher that says nothing — leaves the
            # envelope possibly on the wire, and the record has to hedge.
            # Read as an attribute, not as a class, so this module keeps its
            # no-import-edge discipline.
            on_the_wire = getattr(exc, "card_reached_the_wire", True) is not False
            if on_the_wire:
                logger.error(
                    "%s: card publish raised %s: %s for build_id=%s — the "
                    "journey STOPS here and never re-publishes. The envelope "
                    "may already be on the wire; the merge offer's own "
                    "durable row is latched before the wire is touched, so "
                    "nothing will offer this build a second card, and if the "
                    "card did go out the owner's answer reaches the merge "
                    "press, which is the only thing that acts on it",
                    MERGE_READY_CHECKPOINT_LABEL,
                    type(exc).__name__,
                    exc,
                    build_id,
                )
                reason = (
                    f"{MERGE_READY_CHECKPOINT_LABEL}: card publish raised "
                    f"{type(exc).__name__}: {exc}"
                )
                note = (
                    f"{MERGE_READY_CHECKPOINT_LABEL}: publish failed "
                    f"({type(exc).__name__}) — the card may be on the wire; "
                    "NOT re-published"
                )
            else:
                logger.error(
                    "%s: NO merge card was published for build_id=%s (%s: "
                    "%s) — the offer refused before anything reached the "
                    "wire, so there is nothing on the wire and nothing for "
                    "the owner to answer. The journey STOPS here; the "
                    "refusal's own reason is the logged sentence above this "
                    "one",
                    MERGE_READY_CHECKPOINT_LABEL,
                    build_id,
                    type(exc).__name__,
                    exc,
                )
                reason = (
                    f"{MERGE_READY_CHECKPOINT_LABEL}: no card was published: "
                    f"{type(exc).__name__}: {exc}"
                )
                note = (
                    f"{MERGE_READY_CHECKPOINT_LABEL}: no card was published "
                    f"({type(exc).__name__}) — nothing reached the wire; NOT "
                    "re-published"
                )
            failure_pack = await self._write_failure_pack(
                build_id=build_id,
                feature_id=feature_id,
                gates=gates,
                reason=reason,
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.PUBLISH_FAILED,
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                push_modelled=self._push_branch is None,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                card_may_be_on_the_wire=on_the_wire,
                rationale=f"{rationale} | {note}".strip(" |"),
                failure_pack=failure_pack,
                details={
                    "publish_error": f"{type(exc).__name__}: {exc}",
                    "card_may_be_on_the_wire": on_the_wire,
                    **evidence_details,
                },
            )

        logger.info(
            "%s: gates GREEN for build_id=%s on branch=%s — merge card "
            "published; the merge word is the owner's (DF-021)",
            MERGE_READY_CHECKPOINT_LABEL,
            build_id,
            branch,
        )
        # THE LINE ABOUT THE CHECKS LEFT TO THE MERGE PRESS. It goes on this
        # decision's rationale — the journey's own record, and the log. The
        # card seam IS handed this report (2026-09-09), and says the same
        # fact in ordinary words of its own; this sentence, which names the
        # checks by their ids and their homes, is not the one a person reads.
        # When the gate set deferred nothing this is empty and every word of
        # the decision is what it has always been.
        deferred = str(getattr(gates, "deferred_detail", "") or "").strip()
        return MergeCardDecision(
            outcome=MergeCardOutcome.CARD_PUBLISHED,
            build_id=build_id,
            feature_id=feature_id,
            card_published=True,
            branch=branch,
            pushed=pushed,
            push_modelled=self._push_branch is None,
            gates=gates,
            auto_approve_refused=auto_approve_refused,
            rationale=(
                f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates green, "
                "merge card published"
                + (f" — {deferred}" if deferred else "")
            ).strip(" |"),
            card_result=card_result,
            details=evidence_details,
        )

    # -- internals ----------------------------------------------------

    async def _red_gate_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        branch: str | None,
        pushed: bool,
        gates: GatesReport,
        auto_approve_refused: bool,
        rationale: str,
        evidence_details: Mapping[str, Any],
    ) -> MergeCardDecision:
        """The one red-gate ending: no card, loop back, or FAILED with a pack.

        Both things that can stop a card before it is offered come through
        here — a gate set that is not proven green, and the specification
        fence — so a refusal is carried by exactly one path: the same log
        line, the same choice between the fix cycle and a FAILED close-out,
        the same failure pack, the same shape of decision. The fence's own
        sentence rides in on ``gates.detail`` and its rule names on
        ``gates.failed_gates``, which is what the journey's close-out reads
        when it says why it stopped.
        """
        action = self._resolve_red_gate_action(build_id, gates)
        logger.warning(
            "%s: gates are %s for build_id=%s (failed=%s) — NO card is "
            "published; action=%s (design pass §c.3: fix loops run "
            "BEFORE the merge word)",
            MERGE_READY_CHECKPOINT_LABEL,
            gates.status.value,
            build_id,
            ", ".join(gates.failed_gates) or "unnamed",
            action.value,
        )
        failure_pack = None
        if action is RedGateAction.TERMINATE_FAILED:
            failure_pack = await self._write_failure_pack(
                build_id=build_id, feature_id=feature_id, gates=gates
            )
        return MergeCardDecision(
            outcome=(
                MergeCardOutcome.RED_GATE_FAILED
                if action is RedGateAction.TERMINATE_FAILED
                else MergeCardOutcome.RED_GATE_LOOP_BACK
            ),
            build_id=build_id,
            feature_id=feature_id,
            branch=branch,
            pushed=pushed,
            push_modelled=self._push_branch is None,
            gates=gates,
            auto_approve_refused=auto_approve_refused,
            rationale=(
                f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates "
                f"{gates.status.value} ({gates.detail or 'no detail'}) — "
                "no card"
            ).strip(" |"),
            failure_pack=failure_pack,
            details=dict(evidence_details),
        )

    async def _read_specification_fence(
        self, build_id: str, branch: str | None
    ) -> Any | None:
        """Ask the fence about this branch. ``None`` when none is wired.

        A fence that RAISES is not a pass. It answers the same way it answers
        a branch it could not read — a refusal naming the reason — because
        the two are the same fact: nobody looked at what this branch changed,
        so nobody can say the specification is untouched. The refusal loops
        back into the fix cycle like every other red gate, so a broken fence
        stops cards rather than stopping the estate silently.
        """
        if self._specification_fence is None:
            return None
        try:
            return await _maybe_await(
                self._specification_fence(build_id=build_id, branch=branch)
            )
        except Exception as exc:  # noqa: BLE001 — an unread branch is not clear
            logger.error(
                "%s: the specification fence raised %s: %s for build_id=%s — "
                "no card is published, because nothing read what this branch "
                "changed",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return unreadable_branch_changes(
                f"the fence itself raised {type(exc).__name__}: {exc}"
            )


    async def _already_carded(self, build_id: str) -> bool:
        """Has this build's merge card already been published? Both halves.

        **The in-memory half** (``self._published``) is armed the instant
        a publish is *attempted*, before the await. It is exact within one
        process and worthless across a restart: the publisher is
        constructed per build by the supervisor factory, so a daemon that
        restarts mid-journey builds a fresh publisher with an empty set
        and would happily card the same build a second time. One journey,
        two merge cards, for one merge word.

        **The durable half** (``published_probe``) closes that. Production
        wires it to the ``stage_log`` row that ``publish_card`` writes
        under the merge-ready checkpoint's own identifier once the card is
        really out. That row is durable and — unlike
        ``builds.pending_approval_request_id`` — it survives the owner
        answering, so the probe stays true for the rest of the build's
        life. A restart therefore reads "already carded" and refuses.

        **The residual window, honestly, and why it is now harmless.**
        That row is written *after* the card reaches the wire, so a daemon
        killed in the gap between the publish and the write leaves the
        checkpoint's own row missing. What it does NOT leave missing is
        the merge offer's own durable row, which the shared publisher
        latches BEFORE it touches the wire (2026-09-09). So a restart in
        that gap finds this probe false, tries again, and the offer itself
        refuses — loudly, with no card — rather than putting a second card
        in front of the owner. The two rows together mean one merge word
        gets one card even across a hard kill; the earlier design, which
        wrote a row from the gate on the same publish, had a real window
        here and this one does not.

        A probe that RAISES answers "not carded" and says so loudly: an
        unreadable probe must not wedge a journey that has never carded,
        and the in-memory half still covers the same-process case.
        """
        if build_id in self._published:
            return True
        if self._published_probe is None:
            return False
        try:
            carded = bool(await _maybe_await(self._published_probe(build_id)))
        except Exception as exc:  # noqa: BLE001 — an unreadable probe is not a card
            logger.error(
                "%s: published_probe raised %s: %s for build_id=%s — falling "
                "back to the in-process latch alone. A restart-crossing "
                "duplicate card is NOT guarded on this turn",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
        if carded:
            logger.warning(
                "%s: a DURABLE merge-card row already exists for build_id=%s "
                "— this publisher instance has published nothing, so this is "
                "a restart (or a second journey) meeting a card that is "
                "already out. Publishing nothing",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )
        return carded

    async def _read_branch(self, build_id: str) -> str | None:
        if self._branch_reader is None:
            return None
        try:
            value = await _maybe_await(self._branch_reader(build_id))
        except Exception as exc:  # noqa: BLE001 — an unknown branch is not fatal
            logger.warning(
                "%s: branch_reader raised %s: %s for build_id=%s — "
                "continuing with an unnamed branch",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None
        return str(value) if value else None

    async def _read_has_commits(self, build_id: str) -> bool | None:
        assert self._has_commits_probe is not None
        try:
            return bool(await _maybe_await(self._has_commits_probe(build_id)))
        except Exception as exc:  # noqa: BLE001 — an unknown answer is not "no"
            logger.warning(
                "%s: has_commits_probe raised %s: %s for build_id=%s — "
                "skipping the no-commit belt (the gate precondition still "
                "guards the card)",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None

    async def _push(self, build_id: str, branch: str | None) -> bool:
        if self._push_branch is None:
            logger.info(
                "%s: push of branch=%s for build_id=%s is MODELLED at this "
                "stage — described, not executed (design pass §c.2 step a)",
                MERGE_READY_CHECKPOINT_LABEL,
                branch,
                build_id,
            )
            return False
        try:
            return bool(
                await _maybe_await(self._push_branch(build_id=build_id, branch=branch))
            )
        except Exception as exc:  # noqa: BLE001 — a failed push is a red leg
            logger.warning(
                "%s: push_branch raised %s: %s for build_id=%s — reported as "
                "not pushed; the gate precondition decides the leg",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return False

    async def _read_gates(self, build_id: str, branch: str | None) -> GatesReport:
        if self._gates_green_reader is None:
            return GatesReport(
                status=GateStatus.UNKNOWN,
                detail=(
                    "no gates_green_reader is wired; the merge-ready "
                    "checkpoint requires PROVEN green and refuses to assume it"
                ),
            )
        try:
            raw = await _maybe_await(
                self._gates_green_reader(build_id=build_id, branch=branch)
            )
        except Exception as exc:  # noqa: BLE001 — unknown is red, never green
            return GatesReport(
                status=GateStatus.UNKNOWN,
                detail=(
                    f"gates_green_reader raised {type(exc).__name__}: {exc}; "
                    "treated as red (the precondition is proven green)"
                ),
            )
        if isinstance(raw, GatesReport):
            return raw
        if raw is True:
            return GatesReport(status=GateStatus.GREEN, detail="reader returned True")
        if raw is False:
            return GatesReport(
                status=GateStatus.RED, detail="reader returned False"
            )
        return GatesReport(
            status=GateStatus.UNKNOWN,
            detail=(
                f"gates_green_reader returned {type(raw).__name__}, which is "
                "neither a GatesReport nor a bool; treated as red"
            ),
        )

    def _resolve_red_gate_action(
        self, build_id: str, gates: GatesReport
    ) -> RedGateAction:
        if self._red_gate_action is None:
            return RedGateAction.LOOP_BACK
        try:
            action = self._red_gate_action(build_id, gates)
        except Exception as exc:  # noqa: BLE001 — default to the safer branch
            logger.warning(
                "%s: red_gate_action raised %s: %s for build_id=%s — "
                "defaulting to looping back into the fix cycle",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return RedGateAction.LOOP_BACK
        return action if isinstance(action, RedGateAction) else RedGateAction.LOOP_BACK

    async def _write_failure_pack(
        self,
        *,
        build_id: str,
        feature_id: str,
        gates: GatesReport,
        reason: str | None = None,
    ) -> Any | None:
        if self._failure_pack_writer is None:
            return None
        try:
            return await _maybe_await(
                self._failure_pack_writer(
                    build_id=build_id,
                    feature_id=feature_id,
                    reason=reason
                    or (
                        f"{MERGE_READY_CHECKPOINT_LABEL}: gates "
                        f"{gates.status.value} "
                        f"({', '.join(gates.failed_gates) or gates.detail})"
                    ),
                    gates=gates,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a pack failure is not fatal
            logger.warning(
                "%s: failure_pack_writer raised %s: %s for build_id=%s — the "
                "terminal stands, the pack is missing",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None
