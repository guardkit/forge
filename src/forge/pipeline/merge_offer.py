"""The merge offer — the card that makes the merge word a mechanism.

Make-merge-work build spec (2026-08-24): when a routine build's terminal
publish succeeds and the build finished clean (``tasks_failed == 0``), this
module offers the owner a [Merge & deploy] card.

Since 2026-09-09 it offers the fix journey's card too. The merge-ready
checkpoint used to build its own card through the ordinary approval gate,
which mints a different request id and writes no offer row, so the merge
press — which matches on both — ignored it: the owner said approve and
nothing merged. :meth:`MergeOfferService.offer` is now the one publisher
behind both cards, and each caller supplies only its own words.

The offer is a DUAL envelope, published in order:

1. The AGENTS :class:`~nats_core.events.ApprovalRequestPayload` on
   ``agents.approval.forge.merge-{feature_id}`` — the same approval-response
   plumbing the build gate's tap uses, so the press comes back on the
   ``.response`` mirror subject that :mod:`forge.pipeline.merge_executor`
   consumes.
2. The pipeline ``build-paused`` envelope — the card jarvis renders. Its
   ``build_id`` is deliberately ``merge-{feature_id}`` (NOT the real
   build_id): that is the join key jarvis uses, and the synthetic id keeps
   jarvis's terminal registry from refusing the tap on an already-terminal
   build.

Ordering laws (all load-bearing):

* **Durable latch FIRST.** The offer's stage row (target_identifier
  ``merge_deploy_offer``) is written via the same ``record_stage`` path the
  gate uses BEFORE any wire write — so a crash between latch and publish
  leaves an honest "offered" record and the offer is never doubled.
* **ONE publish attempt ever.** A raise mid-publish is an honest terminal
  log ("the card may be on the wire") — never retried; retrying could put
  two cards on the wire against one latch.
* **Fire-and-forget.** The wireup invokes :meth:`MergeOfferService.maybe_offer`
  via ``asyncio.create_task`` after the terminal publish + build-state
  write-back succeed; nothing here may delay ``_on_terminal``'s ack.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    ApprovalRequestPayload,
    BuildCompletePayload,
    BuildPausedPayload,
)

from forge.lifecycle.persistence import StageLogEntry
from forge.receipts import receipts_root

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_COULD_NOT_RUN",
    "CHECK_RAN",
    "CODE_CHECKS_UNAVAILABLE",
    "EVIDENCE_UNAVAILABLE",
    "FINISHED_FEATURE_BUDGET",
    "FINISHED_FEATURE_DETAILS_KEY",
    "FINISHED_FEATURE_READ_SECONDS",
    "MERGE_AGENT_ID",
    "MERGE_BASE_REF",
    "MERGE_OFFER_DETAILS_KEY",
    "MERGE_OFFER_STAGE_LABEL",
    "MERGE_OFFER_TARGET_IDENTIFIER",
    "MergeOfferService",
    "NO_CHECK_DECLARED",
    "NO_NOT_CHECKED_LIST",
    "WhatWasChecked",
    "approval_subject_for",
    "branch_to_merge",
    "default_merge_branch",
    "git_rev_parse_main",
    "merge_request_id",
    "read_baseline_failing",
    "read_what_was_checked",
    "request_behind_the_build",
    "run_the_scope_pass",
    "what_was_checked",
]

#: ``stage_log.target_identifier`` of the durable offer latch.
MERGE_OFFER_TARGET_IDENTIFIER: str = "merge_deploy_offer"

#: The card's stage label — plain words, per the estate's no-jargon law.
MERGE_OFFER_STAGE_LABEL: str = "the merge word"

#: ``stage_log.details_json`` key holding the offer snapshot.
MERGE_OFFER_DETAILS_KEY: str = "merge_offer"

#: ``agent_id`` stamped on the approval request payload.
MERGE_AGENT_ID: str = "merge-deploy-executor"

#: ``source_id`` on every envelope this module emits (the forge identity).
SOURCE_ID: str = "forge"

#: What a routine build's branch is held against when the scope pass asks
#: what it changed. The same branch the merge word merges into, and the same
#: one :func:`git_rev_parse_main` pins the offer to.
MERGE_BASE_REF: str = "main"

#: How the scope pass says where it found the person's own sentence, in
#: ordinary words, on the receipt.
REQUEST_FROM_THE_RUN: str = "planning_runs.request_text via builds.correlation_id"
REQUEST_FROM_THE_PARENT: str = (
    "planning_runs.request_text via the parent build's correlation_id"
)


def merge_request_id(build_id: str) -> str:
    """The offer's ``request_id`` — ``merge-{build_id}`` (spec-pinned)."""
    return f"merge-{build_id}"


def default_merge_branch(feature_id: str) -> str:
    """``autobuild/<feature id>`` — the branch every feature build is made on."""
    return f"autobuild/{feature_id}"


def branch_to_merge(feature_id: str, merge_branch: Any) -> str:
    """The branch the merge word merges for this build.

    Rewrite-on-refusal spec Part M, rule 54: the build row's ``merge_branch``
    when the conductor recorded one (a repair's commits land on the fix
    journey's own branch, ``fix/<task id>-<build8>``), else the feature's own
    ``autobuild/<feature id>`` — so every feature build, whose column is
    empty, behaves byte for byte as it always has. Every reader of the branch
    (the offer, the candidate check, the landed-merge detection, the merge
    command) goes through this one function so they can never disagree.
    """
    recorded = str(merge_branch or "").strip()
    return recorded or default_merge_branch(feature_id)


def approval_subject_for(feature_id: str) -> str:
    """The AGENTS subject the card's press answers on (spec-pinned)."""
    return f"agents.approval.forge.merge-{feature_id}"


async def git_rev_parse_main(repo_root: Path) -> str | None:
    """Read ``main``'s sha in ``repo_root`` — the merge's expect-main-sha pin.

    Returns ``None`` on any failure (missing repo, no ``main``, git absent):
    the caller refuses to make an offer it cannot pin, loudly.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "main",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort probe, honest None
        logger.warning(
            "merge-offer: git rev-parse main failed to run in %s (%s)",
            repo_root,
            exc,
        )
        return None
    if proc.returncode != 0:
        logger.warning(
            "merge-offer: git rev-parse main exited %s in %s (%s)",
            proc.returncode,
            repo_root,
            stderr_b.decode("utf-8", errors="replace").strip(),
        )
        return None
    sha = stdout_b.decode("utf-8", errors="replace").strip()
    return sha or None


def read_baseline_failing(build_id: str) -> list[str] | None:
    """Best-effort pre-merge baseline failing set — fail-open ``None``.

    Globs ``receipts_root()/<build_id>/**/baseline.json`` and accepts either
    a bare list of test names or a dict carrying a ``failing`` list. Any
    read/parse trouble reads as "no baseline recorded" — the merge verb then
    runs without a ``--baseline-json`` and compares against its own record.
    """
    try:
        root = receipts_root() / build_id
        for path in sorted(root.glob("**/baseline.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return list(data)
            if isinstance(data, dict):
                failing = data.get("failing")
                if isinstance(failing, list) and all(
                    isinstance(x, str) for x in failing
                ):
                    return list(failing)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.debug(
            "merge-offer: baseline read failed for %s (%s); proceeding without",
            build_id,
            exc,
        )
    return None


# ---------------------------------------------------------------------------
# What was actually checked about the finished feature — the card's own words
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS (21 September 2026). A build that reached this point is a
# build nothing refused. Until now that was the whole of what the card said,
# and "nothing was reported" read exactly like "nothing was wrong": the one
# build that answered with an empty list where seven entries were asked for,
# and whose own examples were never checked at all, offered a card that read
# "built clean". (That word was taken off the opening sentence on
# 21 September 2026; the sentence still counts the build's own tasks.)
#
# So the card gains a reading of two records the build leaves behind and the
# runner already exports: the whole-feature record (what the project's own
# check did, what it left unchecked, and what it saw when it asked the
# finished product something) and the code-checks summary (what the checks
# inside the build did, or did not do).
#
# THREE PROPERTIES THIS MUST KEEP, in order of importance:
#
# 1. It REPORTS and never refuses. Every way this can go wrong ends in a
#    sentence on the card and a card that is still offered. It is called the
#    way the scope pass is called and it never raises past itself.
# 2. "Not checked" is never worded as a pass. Four wordings, below, and none
#    of them can be mistaken for another.
# 3. It knows nothing about any language, test runner, protocol, database or
#    product. It carries the project's own TEXT and never reads, compares or
#    judges it. The one word here that belongs to anybody's toolchain is the
#    key ``shell_command_count``, which is the name of a field in a record
#    written elsewhere; this module repeats its number and says nothing about
#    what a command is.

#: Everything parts 1, 2 and 3a add to the card fits in this many characters
#: (design pass 21 September 2026, revision item 4). Jarvis does not cut long
#: text — it splits it into 2,900-character blocks — so the limit has to be
#: ours, and it is counted here over the whole block this reading adds.
FINISHED_FEATURE_BUDGET: int = 900

#: ``details`` key carrying the same reading as structured data.
FINISHED_FEATURE_DETAILS_KEY: str = "finished_feature_check"

#: The mark left wherever words were cut, so a reader can see it happened.
CUT_MARK: str = " …(cut)"

#: THE FOUR WORDINGS. They open the block, exactly one of them appears, and
#: no two of them can be read for each other. A check that ran and found
#: nothing wrong is NEVER worded like any of the last three.
CHECK_RAN: str = "The project's check of the finished feature ran."
CHECK_COULD_NOT_RUN: str = (
    "The project's check of the finished feature could not run:"
)
NO_CHECK_DECLARED: str = "This project declares no check of the finished feature."
EVIDENCE_UNAVAILABLE: str = "Feature-check evidence unavailable:"

#: A check that ran and came back red. It belongs to the first wording's
#: family ("it ran"), and a build whose check failed is not offered a card at
#: all — this is here so an unexpected record is still said plainly.
CHECK_RAN_AND_FAILED: str = (
    "The project's check of the finished feature ran and did not pass."
)

#: Said instead of "it left nothing on its not-checked list" when the record
#: carries no list that could be read (21 September 2026, the Stage C review).
#: An empty list is a statement; a list that is not there is not one, and the
#: difference has to be on the card or an absence reads as a clean run.
NO_NOT_CHECKED_LIST: str = "Its not-checked list could not be read."

#: Said when the summary of the checks inside the build could not be read at
#: all (21 September 2026, the Stage C review). GuardKit writes that summary
#: on every finished build and never raises, so an absence means something
#: went wrong — and a missing line would cost the card a sentence and read as
#: nothing to report.
CODE_CHECKS_UNAVAILABLE: str = (
    "Code checks: no summary of them could be read, so nothing here says "
    "whether anything looked at the code."
)

#: How long the card will wait for the two records to be read. It is a disk
#: read of a small tree, so this is not a budget, it is a stop: a records
#: folder that has stopped answering must cost the owner a sentence on the
#: card and not a card that never arrives.
FINISHED_FEATURE_READ_SECONDS: float = 15.0

#: The record names, as the build leaves them. Only the file NAME is used:
#: nothing here assumes where in the exported tree they landed.
FEATURE_CHECK_RECORD_NAME: str = "feature_check.json"
CODE_CHECKS_RECORD_NAME: str = "code_checks.json"

#: The states one check reports about itself in that summary, in the record's
#: own words. Only these two mean a check actually looked at something; every
#: other state means it did not, and none of them may be worded as if it had.
#: Nothing here works out what a check is or what it looked at.
_CHECK_LOOKED: tuple[str, ...] = ("ran_and_found_nothing", "found_something")
#: The one state that says the check has nothing to say about a project of
#: this kind. It is counted and said in so many words, because a summary full
#: of it used to come out as "no findings" (21 September 2026, Stage C review).
_CHECK_KIND_NOT_SUPPORTED: str = "kind_of_project_not_supported"

#: Caps, applied before the budget is counted.
MAX_OBSERVATIONS_ON_THE_CARD: int = 6
_OBSERVATION_SIDE_CHARS: int = 260
_NOT_CHECKED_LINE_CHARS: int = 300
_CODE_CHECKS_LINE_CHARS: int = 220
_NAMES_ON_THE_CARD: int = 3
_REASON_CHARS: int = 160
#: How many entries of each list the DETAILS carry (the card's own words are
#: capped much harder by the budget above; the details are the full record's
#: shape for anyone reading the row afterwards).
_DETAILS_LIST_CAP: int = 50


@dataclass(frozen=True)
class WhatWasChecked:
    """The card's sentences about the finished feature, and the same as data.

    ``lines`` is what goes on the face of the card, already inside
    :data:`FINISHED_FEATURE_BUDGET`. ``details`` is the same reading as
    structured data for the durable row and the approval envelope. ``state``
    is which of the four wordings was used.
    """

    state: str
    lines: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The block as it goes on the card: ONE SENTENCE PER LINE.

        Joined with a line break and not a space (21 September 2026). Read
        as one paragraph, the four wordings, the not-checked list, each
        observation and the code-checks line ran together into a wall of
        text a person skims past — and the whole point of this reading is
        that a person reads it. A line break costs exactly what a space
        cost, so :data:`FINISHED_FEATURE_BUDGET` is unchanged and the
        breaks are counted inside it.
        """
        return "\n".join(self.lines)


def _tidy(value: Any) -> str:
    """One line of whitespace-collapsed text, never ``None``."""
    return " ".join(str(value if value is not None else "").split())


def _shorten(value: Any, limit: int) -> str:
    """``value`` as one line, cut at ``limit`` with a visible mark."""
    text = _tidy(value)
    if len(text) <= limit:
        return text
    keep = max(limit - len(CUT_MARK), 0)
    return text[:keep].rstrip() + CUT_MARK


def _read_one_record(
    build_id: str, name: str, feature_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """One exported record by FILE NAME — ``(record, why not)``, never raises.

    Globs ``receipts_root()/<build_id>/**/<name>``, exactly as
    :func:`read_baseline_failing` does, so nothing here knows or assumes
    where in the exported tree a record landed. When several copies were
    exported (the runner exports the outer tree and every inner one), a copy
    whose own ``feature`` matches this build's feature wins, then the
    shallowest path, then alphabetical order — a rule with no ties in it, so
    two runs of this reader can never disagree.
    """
    try:
        root = receipts_root() / build_id
        if not root.is_dir():
            return None, "nothing was exported for this build"
        found = sorted(
            root.glob(f"**/{name}"), key=lambda p: (len(p.parts), str(p))
        )
        if not found:
            return None, f"no {name} was exported for this build"
        records: list[tuple[int, Path, dict[str, Any]]] = []
        first_trouble: str | None = None
        for path in found:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                if first_trouble is None:
                    first_trouble = (
                        f"{name} could not be read ({type(exc).__name__})"
                    )
                continue
            if not isinstance(data, dict):
                if first_trouble is None:
                    first_trouble = f"{name} is not a record this could read"
                continue
            mine = (
                0
                if feature_id and _tidy(data.get("feature")) == _tidy(feature_id)
                else 1
            )
            records.append((mine, path, data))
        if not records:
            return None, first_trouble or f"no {name} could be read"
        records.sort(key=lambda item: item[0])
        return records[0][2], None
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        logger.debug(
            "the finished-feature reading: %s could not be looked for under "
            "%s (%s)",
            name,
            build_id,
            exc,
        )
        return None, f"{name} could not be looked for ({type(exc).__name__})"


def read_what_was_checked(
    build_id: str, feature_id: str | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """The two exported records for this build — ``(record, code checks, why not)``.

    ``why not`` is a short reason the whole-feature record could not be read,
    and it is the only one that changes what the card says: the code-checks
    summary simply goes unmentioned when it is not there. Never raises.
    """
    record, why_not = _read_one_record(
        build_id, FEATURE_CHECK_RECORD_NAME, feature_id
    )
    code_checks, _ = _read_one_record(
        build_id, CODE_CHECKS_RECORD_NAME, feature_id
    )
    return record, code_checks, why_not


def _pairs(raw: Any, keys: tuple[str, str]) -> list[tuple[str, str]]:
    """A list of two-sided entries, read defensively. Anything else is dropped."""
    out: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        left = _tidy(item.get(keys[0]))
        right = _tidy(item.get(keys[1]))
        if left or right:
            out.append((left, right))
    return out


def _not_checked_line(names: list[str], total: int, limit: int) -> str:
    """``Not checked: 7 — a; b — and 5 more.`` in the record's own words.

    Names are added one at a time only while the WHOLE line still fits, so
    the line is never cut in the middle of a name and the count at the front
    is always the truth. With room for none of them it is a bare count, which
    is still an honest sentence.
    """
    head = f"Not checked: {total}"

    def assembled(shown: list[str]) -> str:
        if not shown:
            return f"{head}."
        rest = total - len(shown)
        tail = f" — and {rest} more." if rest > 0 else "."
        return f"{head} — " + "; ".join(shown) + tail

    shown: list[str] = []
    for name in names[:_NAMES_ON_THE_CARD]:
        tidy = _shorten(name, 140)
        if not tidy:
            continue
        if len(assembled(shown + [tidy])) > limit:
            break
        shown.append(tidy)
    return assembled(shown)


def _code_checks_line(code_checks: Mapping[str, Any] | None) -> str:
    """One line about what the checks inside the build did.

    Findings are named, tasks nothing looked at are counted, and commands the
    build ran directly are noted because a file one of those wrote is in no
    list any check could read. It never refuses anything.

    **"No findings" is said only when a check actually looked at something**
    (21 September 2026, the Stage C review). A summary where every check said
    it does not cover this kind of project, or kept nothing at all, used to
    come out as the single line "Code checks: no findings." — which an owner
    reads as "the code was checked and was clean". The states are the
    record's own; they are counted, not interpreted.

    **A check that could read only part of what it was given says so**
    (21 September 2026). The record carries that count as ``inputs_not_read``
    beside the state, for a group of tasks and now for a task as well; the
    numbers are added up and repeated, and nothing here works out what an
    input is.
    """
    if not isinstance(code_checks, Mapping):
        return CODE_CHECKS_UNAVAILABLE
    said: list[str] = []
    try:
        finding_count = int(code_checks.get("finding_count") or 0)
    except (TypeError, ValueError):
        finding_count = 0
    names: list[str] = []
    blocks_total = 0
    looked = 0
    kind_not_supported = 0
    # How many of the things the checks were given they could not read. A
    # check that read only part of its input and found nothing in the rest
    # has not covered the part it could not read, so the record carries the
    # number beside the state and the card repeats it (21 September 2026).
    partly_read = 0
    for row in list(code_checks.get("tasks") or []) + list(
        code_checks.get("groups") or []
    ):
        if not isinstance(row, Mapping):
            continue
        checks = row.get("checks")
        blocks = (
            list(checks.values()) if isinstance(checks, Mapping) else [row]
        )
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            blocks_total += 1
            state = _tidy(block.get("state")).lower()
            if state in _CHECK_LOOKED:
                looked += 1
            elif state == _CHECK_KIND_NOT_SUPPORTED:
                kind_not_supported += 1
            try:
                unread = block.get("inputs_not_read")
                partly_read += int(unread) if unread is not None else 0
            except (TypeError, ValueError):
                pass
            for finding in list(block.get("findings") or []):
                if not isinstance(finding, Mapping):
                    continue
                name = _tidy(finding.get("name")) or _tidy(finding.get("file"))
                if name and name not in names:
                    names.append(name)
    if finding_count:
        shown = names[:_NAMES_ON_THE_CARD]
        rest = finding_count - len(shown)
        named = ", ".join(shown)
        if shown and rest > 0:
            named = f"{named} and {rest} more"
        elif not shown:
            named = "not named in the summary"
        said.append(
            f"{finding_count} finding{'s' if finding_count != 1 else ''} "
            f"({named})"
        )
    elif looked:
        said.append("no findings")
    elif blocks_total:
        said.append("nothing was checked")
    else:
        said.append("no check of the code is recorded here")
    if kind_not_supported:
        said.append(
            f"{kind_not_supported} of {blocks_total} checks do not cover this "
            "kind of project"
        )
    if partly_read > 0:
        said.append(
            f"{partly_read} thing{'s' if partly_read != 1 else ''} the checks "
            "were given could not be read"
        )
    try:
        not_checked = int(code_checks.get("tasks_with_something_not_checked") or 0)
        total = int(code_checks.get("tasks_total") or 0)
    except (TypeError, ValueError):
        not_checked, total = 0, 0
    if not_checked:
        said.append(f"{not_checked} of {total} tasks not fully checked")
    try:
        # The key belongs to the record this reads; its number is repeated
        # here and nothing is worked out from it.
        commands = int(code_checks.get("shell_command_count") or 0)
    except (TypeError, ValueError):
        commands = 0
    if commands:
        said.append(
            f"{commands} commands the build ran directly, and files they "
            "wrote are in no list a check could read"
        )
    return _shorten("Code checks: " + " · ".join(said) + ".", _CODE_CHECKS_LINE_CHARS)


def what_was_checked(
    record: Mapping[str, Any] | None,
    code_checks: Mapping[str, Any] | None = None,
    why_not: str | None = None,
) -> WhatWasChecked:
    """Turn the two records into the card's sentences. Never raises.

    The block is: one of the four wordings; then what was left unchecked;
    then up to six of the project's own observations, each said as what was
    asked and what came back; then one line about the checks inside the
    build. It is cut to :data:`FINISHED_FEATURE_BUDGET` with a visible mark,
    and the not-checked list shortens to a bare count BEFORE any observation
    is dropped.
    """
    try:
        return _what_was_checked(record, code_checks, why_not)
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        logger.warning(
            "the finished-feature reading: the records could not be put into "
            "words (%s: %s) — the card says the evidence was unavailable",
            type(exc).__name__,
            exc,
        )
        return WhatWasChecked(
            state="unavailable",
            lines=(f"{EVIDENCE_UNAVAILABLE} it could not be read here.",),
            details={
                "state": "unavailable",
                "reason": f"it could not be read here ({type(exc).__name__})",
            },
        )


def _what_was_checked(
    record: Mapping[str, Any] | None,
    code_checks: Mapping[str, Any] | None,
    why_not: str | None,
) -> WhatWasChecked:
    details: dict[str, Any] = {"budget_characters": FINISHED_FEATURE_BUDGET}

    # (1) Which of the four wordings opens the block.
    if not isinstance(record, Mapping):
        reason = _shorten(why_not or "no record of the check was found", _REASON_CHARS)
        opening = f"{EVIDENCE_UNAVAILABLE} {reason}"
        state = "unavailable"
        details.update({"state": state, "reason": reason})
        return _fit(state, [opening], [], "", details)

    status = _tidy(record.get("status")).lower()
    declared = record.get("declared")
    if status == "not_declared" or declared is False:
        state = "not_declared"
        details.update({"state": state, "reason": _tidy(record.get("reason"))})
        return _fit(state, [NO_CHECK_DECLARED], [], "", details)

    # (1b) A record that does not say the check ran is not a record of a
    # check that ran (21 September 2026, the Stage C review and its
    # re-check). GuardKit always writes a status, so a record without one is
    # truncated or corrupted — and a list beside it is not evidence that
    # anything ran, only that something was written down. Both cases take
    # the wording kept for evidence that cannot be read; neither may open
    # with "The project's check of the finished feature ran."
    carries_not_checked = isinstance(record.get("not_checked"), list)
    carries_a_list = (
        carries_not_checked
        or isinstance(record.get("observations"), list)
        or isinstance(record.get("scenarios_covered"), list)
    )
    if not status:
        reason = _shorten(
            why_not
            or (
                "the record does not say whether the check ran"
                if carries_a_list
                else "the record carries no status and none of its lists"
            ),
            _REASON_CHARS,
        )
        state = "unavailable"
        details.update(
            {
                "state": state,
                "reason": reason,
                "carried_a_list_without_a_status": carries_a_list,
            }
        )
        return _fit(state, [f"{EVIDENCE_UNAVAILABLE} {reason}"], [], "", details)

    # (2) What the check left unchecked. COUNT THE LIST, never the record's
    # own total: that field adds the central guard's names to the project's
    # names for the same examples, so it says fourteen beside seven entries
    # (found while driving stage B, 21 September 2026). The list is the one
    # thing here that cannot double-count itself.
    not_checked = _pairs(record.get("not_checked"), ("name", "reason"))
    not_checked_count = len(not_checked)

    if status == "could_not_run":
        reason = _shorten(
            record.get("could_not_run_reason")
            or record.get("reason")
            or "the project did not say why",
            _REASON_CHARS,
        )
        opening = f"{CHECK_COULD_NOT_RUN} {reason}"
        state = "could_not_run"
    elif status and status != "passed":
        opening = CHECK_RAN_AND_FAILED
        state = "ran"
    else:
        opening = CHECK_RAN
        state = "ran"

    head = [opening]
    if not_checked_count:
        head.append(
            _not_checked_line(
                [name for name, _ in not_checked],
                not_checked_count,
                _NOT_CHECKED_LINE_CHARS,
            )
        )
    elif state == "ran":
        # An empty list is the project saying "nothing was left unchecked".
        # A list that is not there says nothing at all, and the two must not
        # be worded the same (21 September 2026, the Stage C review).
        head.append(
            "It left nothing on its not-checked list."
            if carries_not_checked
            else NO_NOT_CHECKED_LIST
        )

    # (3) The project's own observations, as text, capped and never judged.
    observations = _pairs(record.get("observations"), ("asked", "answered"))[
        :MAX_OBSERVATIONS_ON_THE_CARD
    ]
    observed = [
        "Asked: "
        + (_shorten(asked, _OBSERVATION_SIDE_CHARS) or "(not said)")
        + " / Answered: "
        + (_shorten(answered, _OBSERVATION_SIDE_CHARS) or "(not said)")
        for asked, answered in observations
    ]

    details.update(
        {
            "state": state,
            "status": status or None,
            "could_not_run_reason": _tidy(record.get("could_not_run_reason")) or None,
            "not_checked_count": not_checked_count,
            "not_checked": [
                {"name": name, "reason": reason}
                for name, reason in not_checked[:_DETAILS_LIST_CAP]
            ],
            "scenarios_covered": [
                _tidy(name)
                for name in list(record.get("scenarios_covered") or [])
                if _tidy(name)
            ][:_DETAILS_LIST_CAP],
            "observations": [
                {"asked": asked, "answered": answered}
                for asked, answered in observations
            ],
            "observations_count": len(observations),
        }
    )
    details["code_checks_summary_read"] = isinstance(code_checks, Mapping)
    if isinstance(code_checks, Mapping):
        details["code_checks"] = {
            key: code_checks.get(key)
            for key in (
                "finding_count",
                "tasks_total",
                "tasks_with_something_not_checked",
                "tasks_not_checked",
                "shell_command_count",
            )
            if key in code_checks
        }

    return _fit(
        state,
        head,
        observed,
        _code_checks_line(code_checks),
        details,
        not_checked_count=not_checked_count,
    )


def _fit(
    state: str,
    head: list[str],
    observed: list[str],
    code_checks_line: str,
    details: dict[str, Any],
    *,
    not_checked_count: int = 0,
) -> WhatWasChecked:
    """Put the block inside the budget, in the order the design fixed.

    The not-checked list shortens to a bare count first; then observations
    are dropped from the end, and the card says how many are not shown; only
    then is what is left cut with a visible mark. The code-checks line is
    never one of the things dropped — it is the shortest and it is the only
    one that speaks for the checks inside the build.
    """
    tail = [code_checks_line] if code_checks_line else []

    def whole(parts: list[str]) -> str:
        # ONE SENTENCE PER LINE (21 September 2026). The separator is a line
        # break rather than a space, and it is one character either way, so
        # every number this function works out — the budget, the cut, the
        # card's own character count — counts the breaks and is unchanged.
        return "\n".join(p for p in parts if p)

    shortened = False
    dropped = 0
    lines = head + observed + tail
    if len(whole(lines)) > FINISHED_FEATURE_BUDGET and not_checked_count:
        head = [head[0], f"Not checked: {not_checked_count}."]
        shortened = True
        lines = head + observed + tail

    kept = list(observed)
    while len(whole(lines)) > FINISHED_FEATURE_BUDGET and kept:
        kept.pop()
        dropped = len(observed) - len(kept)
        note = [
            f"({dropped} more observation{'s' if dropped != 1 else ''} not "
            "shown here.)"
        ]
        lines = head + kept + note + tail

    text = whole(lines)
    cut = False
    if len(text) > FINISHED_FEATURE_BUDGET:
        text = _shorten(text, FINISHED_FEATURE_BUDGET)
        lines = [text]
        cut = True

    details.update(
        {
            "card_characters": len(whole(lines)),
            "not_checked_shortened_to_a_count": shortened,
            "observations_not_shown": dropped,
            "cut_to_fit": cut,
            "card_lines": list(lines),
        }
    )
    return WhatWasChecked(state=state, lines=tuple(lines), details=details)


def request_behind_the_build(pool: Any, row: Any) -> tuple[str | None, str | None, str | None]:
    """The sentence this build was asked for — ``(request, where from, why not)``.

    A routine build's ``correlation_id`` is the planning run's own, so one
    read of ``planning_runs`` finds the sentence; measured read-only against
    the live ledger on 2026-09-15, that join lands 90 times out of 91.

    A REPAIR build's correlation id is the made-up ``fix-build-<parent build
    id>``, which joins nothing at all — 0 times out of 112 — so it resolves
    through its parent build's row in one hop instead. If neither finds a
    sentence, this says why in ordinary words and the scope pass then
    publishes "the sentence could not be found" rather than an empty request.

    Never raises.
    """
    from forge.pipeline.fix_row_producer import source_build_id_from_correlation_id

    correlation_id = str(getattr(row, "correlation_id", "") or "").strip()
    source = REQUEST_FROM_THE_RUN
    if not correlation_id:
        return None, None, "this build's row carries no correlation id"

    parent_build = source_build_id_from_correlation_id(correlation_id)
    if parent_build:
        source = REQUEST_FROM_THE_PARENT
        try:
            parent_row = pool.get_build_row(parent_build)
        except Exception as exc:  # noqa: BLE001 — a reader never stops a card
            return None, None, (
                f"the build this repair belongs to ({parent_build}) could not "
                f"be read ({type(exc).__name__})"
            )
        if parent_row is None:
            return None, None, (
                f"the build this repair belongs to ({parent_build}) is not in "
                "the record, so the sentence behind it could not be found"
            )
        correlation_id = str(getattr(parent_row, "correlation_id", "") or "").strip()
        if not correlation_id:
            return None, None, (
                f"the build this repair belongs to ({parent_build}) carries no "
                "correlation id"
            )

    try:
        text = _read_request_text(pool, correlation_id)
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        return None, None, (
            f"the planning record for {correlation_id} could not be read "
            f"({type(exc).__name__})"
        )
    if not text:
        return None, None, (
            f"there is no planning record for {correlation_id}, so the "
            "sentence behind this build could not be found"
        )
    return text, source, None


def _read_request_text(pool: Any, correlation_id: str) -> str | None:
    """One read of ``planning_runs.request_text``, read-only where it can be.

    Opens a fresh read-only handle on the same database file the facade
    writes to — the pattern every other read side uses — and falls back to
    the facade's own connection for the in-memory databases tests run on.
    """
    from forge.adapters.sqlite.connect import read_only_connect

    db_path = getattr(pool, "db_path", None)
    statement = "SELECT request_text FROM planning_runs WHERE correlation_id = ?"
    if db_path is None or str(db_path) in ("", ":memory:"):
        cursor = pool.connection.execute(statement, (correlation_id,))
        found = cursor.fetchone()
    else:
        cx = read_only_connect(db_path)
        try:
            found = cx.execute(statement, (correlation_id,)).fetchone()
        finally:
            cx.close()
    if found is None:
        return None
    text = found[0] if not isinstance(found, dict) else found.get("request_text")
    text = str(text or "").strip()
    return text or None


def run_the_scope_pass(
    *,
    config: Any,
    pool: Any,
    build_id: str,
    feature_id: str,
    row: Any,
) -> Any | None:
    """Read the finished branch and hold it against the plan and the request.

    Returns the scope report, or ``None`` when the pass could not even be
    attempted — no repository path, say — which the card reads as "nobody
    counted" and says nothing about scope at all.

    Runs git, so it is called off the event loop. Never raises: every way
    this can fail is a sentence on the receipt, and none of them holds up a
    merge card.
    """
    from forge.config.sandboxes import sandbox_for
    from forge.pipeline.branch_scope import (
        read_branch_scope,
        read_branch_scope_in_sandbox,
    )
    from forge.pipeline.scope_report import (
        scope_of_the_build,
        unread_scope,
        write_scope_report,
    )

    repo_key = str(getattr(row, "repo", "") or "")
    repo_root_raw = config.planning.target_repo_paths.get(repo_key)
    if not repo_root_raw:
        logger.info(
            "the scope pass: %s has no entry in planning.target_repo_paths, "
            "so nothing was counted for %s and the card says nothing about "
            "scope",
            repo_key,
            build_id,
        )
        return None

    merge_branch = str(getattr(row, "merge_branch", None) or "").strip() or None
    head = branch_to_merge(feature_id, merge_branch)
    request, source, why_not = request_behind_the_build(pool, row)

    sandbox = sandbox_for(config, repo_key)
    try:
        if sandbox is not None:
            reading = read_branch_scope_in_sandbox(
                sandbox=sandbox,
                repo=repo_key,
                base=MERGE_BASE_REF,
                head=head,
                feature_id=feature_id,
            )
        else:
            reading = read_branch_scope(
                repo_root=Path(repo_root_raw),
                base=MERGE_BASE_REF,
                head=head,
                feature_id=feature_id,
            )
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        report = unread_scope(
            f"what {head} changed against {MERGE_BASE_REF} could not be read "
            f"({type(exc).__name__}: {exc})"
        )
        write_scope_report(build_id, report)
        return report

    report = scope_of_the_build(
        reading=reading,
        request=request,
        request_source=source,
        request_why_not=why_not,
    )
    write_scope_report(build_id, report)
    return report


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MergeOfferService:
    """Offers the [Merge & deploy] card after a clean routine build.

    Args:
        config: The validated ``ForgeConfig`` — reads
            ``merge_executor.enabled``, ``merge_executor.response_wait_seconds``
            and ``planning.target_repo_paths``.
        pool: The shared ``SqliteLifecyclePersistence`` facade (builds row
            re-read, offer latch probe + write).
        pipeline_publisher: The shared
            :class:`~forge.adapters.nats.pipeline_publisher.PipelinePublisher`
            (the ``build-paused`` mirror rides the existing publisher).
        raw_publish: ``async (subject, body_bytes)`` — the raw NATS publish
            for the AGENTS approval envelope (its subject is not in the
            pipeline family). Production binds the daemon's shared client's
            ``publish``.
        git_head: Injectable ``async (repo_root) -> sha | None`` seam;
            defaults to :func:`git_rev_parse_main`.
        baseline_reader: Injectable ``(build_id) -> list[str] | None`` seam;
            defaults to :func:`read_baseline_failing`.
        finished_feature_reader: Injectable ``(build_id, feature_id) ->
            (record, code checks, why not)`` seam; defaults to
            :func:`read_what_was_checked`. It reads two exported records off
            disk, so it is called off the event loop, and anything it does —
            including raising — costs the card nothing but the sentences.
        scope_pass: Injectable ``(config, pool, build_id, feature_id, row) ->
            report | None`` seam; defaults to :func:`run_the_scope_pass`. It
            runs git, so it is called off the event loop.
        clock: Wall-clock seam for the stage row / paused_at stamps.
    """

    def __init__(
        self,
        *,
        config: Any,
        pool: Any,
        pipeline_publisher: Any,
        raw_publish: Callable[[str, bytes], Awaitable[Any]],
        git_head: Callable[[Path], Awaitable[str | None]] = git_rev_parse_main,
        baseline_reader: Callable[[str], list[str] | None] = read_baseline_failing,
        finished_feature_reader: Callable[..., Any] = read_what_was_checked,
        scope_pass: Callable[..., Any] = run_the_scope_pass,
        git_surface: Callable[[str, Path], Any | None] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._pool = pool
        self._publisher = pipeline_publisher
        self._raw_publish = raw_publish
        self._git_head = git_head
        self._baseline_reader = baseline_reader
        self._finished_feature_reader = finished_feature_reader
        self._scope_pass = scope_pass
        self._git_surface = git_surface
        self._clock = clock

    async def maybe_offer(self, event: Any) -> None:
        """The wireup's fire-and-forget hook — never raises past itself."""
        try:
            await self._maybe_offer(event)
        except Exception as exc:  # noqa: BLE001 — hook must never propagate
            logger.error(
                "merge-offer: offer pass raised (%s) for payload_type=%s — "
                "if the raise was mid-publish the card may be on the wire; "
                "the offer is NOT retried",
                exc,
                type(event).__name__,
            )

    async def _maybe_offer(self, event: Any) -> None:
        merge_cfg = getattr(self._config, "merge_executor", None)
        if merge_cfg is None or not merge_cfg.enabled:
            return
        if not isinstance(event, BuildCompletePayload):
            return
        if event.tasks_failed != 0:
            logger.info(
                "merge-offer: %s finished with %d failed task(s) — no merge "
                "card is offered for a build that is not clean",
                event.build_id,
                event.tasks_failed,
            )
            return

        retained = getattr(event, "worktree_retention", None)
        if isinstance(retained, Mapping) and retained.get("ok"):
            path = str(retained.get("path") or "").strip()
            if (
                retained.get("build_id") != event.build_id
                or not path
                or Path(path).name != event.build_id
            ):
                logger.error(
                    "merge-offer: refusing %s — its retained worktree identity "
                    "does not bind the build id and path",
                    event.build_id,
                )
                return
            self._pool.record_worktree_path(event.build_id, path)

        # THE SCOPE PASS (the planner fix, 2026-09-15). Read the finished
        # branch once, here, before the card is built: what it changed that
        # the plan did not name, and what it built that the request never
        # asked for. It is a REPORT and never a refusal — anything it cannot
        # read is a sentence on its own receipt and the card simply says less.
        scope = await self._take_the_scope_pass(event)

        # WHAT WAS ACTUALLY CHECKED (parts 1, 2 and 3a, 2026-09-21). Beside
        # the scope pass, and on exactly the same terms: one reading, off the
        # event loop, that reports and never refuses. Its whole job is to
        # stop "nothing was reported" reading like "nothing was wrong".
        checked = await self._read_what_was_checked(event)

        def _words(branch: str, merge_branch: str | None) -> str:
            from forge.cli._serve_gate_activation import card_line_about_scope

            # The card names the branch only when it is not the feature's own
            # (Part M, rule 55): a feature build's card reads exactly as before.
            named = (
                f"{event.feature_id} (branch {branch})"
                if merge_branch is not None
                else event.feature_id
            )
            # THE OPENING SENTENCE no longer says "clean" (21 September
            # 2026). It counted the build's own tasks and then the card went
            # on to say that not one of the feature's examples had been
            # checked, so "clean" was the least true word on it. What it
            # counts is unchanged; only the word is gone.
            opening = [
                f"{named} built — {event.tasks_completed} of "
                f"{event.tasks_total} tasks passed."
            ]
            in_scope = card_line_about_scope(scope)
            if in_scope:
                opening.append(in_scope)

            # ONE SENTENCE PER LINE for everything the finished-feature
            # reading adds, and a line of its own for the closing sentence.
            # Slack renders the breaks as breaks (they ride an inert
            # ``plain_text`` block and are never cut), and a person can find
            # the not-checked line and each "Asked / Answered" pair without
            # reading a paragraph.
            lines = [" ".join(opening)]
            lines.extend(line for line in checked.lines if line)
            lines.append(
                "Approve = merge into main, deploy to the sandbox and run the "
                "checks; the branch is kept either way. Reject = nothing "
                "changes."
            )
            return "\n".join(lines)

        details: dict[str, Any] = {
            "tasks_completed": event.tasks_completed,
            "tasks_total": event.tasks_total,
        }
        if isinstance(retained, Mapping) and retained.get("ok"):
            details["runner_worktree_retention"] = dict(retained)
        if scope is not None:
            details["scope_report"] = scope.to_dict()
        details[FINISHED_FEATURE_DETAILS_KEY] = dict(checked.details)

        await self.offer(
            build_id=event.build_id,
            feature_id=event.feature_id,
            card_words=_words,
            extra_details=details,
        )

    async def _read_what_was_checked(self, event: Any) -> WhatWasChecked:
        """Read the two exported records and put them into words.

        Off the event loop, because it touches disk. It never raises, never
        refuses a card and never delays one by more than the read itself: a
        reader that falls over says so on the card in the words reserved for
        exactly that — "Feature-check evidence unavailable" — and the card is
        offered anyway. A finished build always leaves one of these records
        behind, even when the project declares no check, so an absent record
        means something went wrong and the card has to say so rather than
        fall silent and read as clean.
        """
        try:
            record, code_checks, why_not = await asyncio.wait_for(
                asyncio.to_thread(
                    self._finished_feature_reader,
                    event.build_id,
                    event.feature_id,
                ),
                timeout=FINISHED_FEATURE_READ_SECONDS,
            )
        except TimeoutError:
            reason = (
                f"the records did not come back within "
                f"{int(FINISHED_FEATURE_READ_SECONDS)} seconds"
            )
            logger.warning(
                "the finished-feature reading: %s for %s — the card is "
                "offered anyway and says the evidence was unavailable",
                reason,
                event.build_id,
            )
            return WhatWasChecked(
                state="unavailable",
                lines=(f"{EVIDENCE_UNAVAILABLE} {reason}.",),
                details={"state": "unavailable", "reason": reason},
            )
        except Exception as exc:  # noqa: BLE001 — a reader never stops a card
            logger.warning(
                "the finished-feature reading: nothing could be read for %s "
                "(%s: %s) — the card says the evidence was unavailable",
                event.build_id,
                type(exc).__name__,
                exc,
            )
            reason = f"the records could not be read ({type(exc).__name__})"
            return WhatWasChecked(
                state="unavailable",
                lines=(f"{EVIDENCE_UNAVAILABLE} {reason}.",),
                details={"state": "unavailable", "reason": reason},
            )
        return what_was_checked(record, code_checks, why_not)

    async def _take_the_scope_pass(self, event: Any) -> Any | None:
        """Run the scope pass off the event loop; ``None`` if nobody counted.

        Never raises and never delays the card by more than the reading
        itself: a scope line is worth having and no merge card has ever
        waited on one before, so every way this can go wrong ends in a logged
        sentence and a card that says nothing about scope.
        """
        try:
            row = self._pool.get_build_row(event.build_id)
            if row is None:
                return None
            return await asyncio.to_thread(
                self._scope_pass,
                config=self._config,
                pool=self._pool,
                build_id=event.build_id,
                feature_id=event.feature_id,
                row=row,
            )
        except Exception as exc:  # noqa: BLE001 — a report never stops a card
            logger.warning(
                "the scope pass: nothing was counted for %s (%s: %s) — the "
                "merge card says nothing about scope",
                event.build_id,
                type(exc).__name__,
                exc,
            )
            return None

    async def offer(
        self,
        *,
        build_id: str,
        feature_id: str,
        card_words: Callable[[str, str | None], str],
        extra_details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Latch the offer and put ONE merge card in front of the owner.

        This is the whole card — the durable row the merge press matches on,
        the approval request the press answers, and the ``build-paused``
        envelope jarvis renders — and it is the only place any of the three
        is built. Two callers reach it: the routine build's own hook
        (:meth:`maybe_offer`, whose words count the tasks that passed) and
        the fix journey's merge-ready checkpoint (whose words say what the
        checkpoint checked). They share this method precisely so the card the
        owner taps is always the card the merge press consumes; the fix
        journey's own card used to be built somewhere else, and the owner's
        merge word fell on the floor because nothing was listening for it
        (2026-09-09).

        Args:
            build_id: The build the card is for. The press reads it back out
                of the request id, so it is the join key of the whole press.
            feature_id: ``FEAT-XXXX`` — the approval subject and the card's
                synthetic join key are both built from it.
            card_words: ``(branch, merge_branch) -> str`` — the sentences on
                the face of the card. It is given the branch the merge word
                will merge and the row's own recorded branch (``None`` when
                the build never recorded one, which means the feature's own
                branch), so each caller can say what is true for it without
                either of them owning the card's plumbing.
            extra_details: Anything else this caller wants kept on the
                durable row and carried on the approval request. The press
                reads none of it; it is there so the record says who offered
                what.

        Returns:
            ``True`` when the offer was latched and the card publish was
            attempted — the state that must never be retried, whether or not
            the wire write itself raised. ``False`` when the offer refused
            before writing anything: the reason is always a logged sentence,
            and nothing reached the wire.
        """
        merge_cfg = getattr(self._config, "merge_executor", None)
        if merge_cfg is None or not merge_cfg.enabled:
            logger.info(
                "merge-offer: the merge press is switched off — no merge card "
                "is offered for %s",
                build_id,
            )
            return False

        # (a) Re-read the builds row — payload.repo is None BY DESIGN; the
        # durable row carries repo + correlation_id.
        row = self._pool.get_build_row(build_id)
        if row is None:
            logger.error(
                "merge-offer: no builds row for build_id=%s — cannot offer "
                "the merge card (the offer needs the row's repo and "
                "correlation_id)",
                build_id,
            )
            return False
        repo_root_raw = self._config.planning.target_repo_paths.get(row.repo)
        if not repo_root_raw:
            logger.error(
                "merge-offer: repo %r (build_id=%s) has no entry in "
                "planning.target_repo_paths — cannot offer the merge card",
                row.repo,
                build_id,
            )
            return False
        repo_root = Path(repo_root_raw)
        if not row.correlation_id:
            logger.error(
                "merge-offer: builds row %s carries an EMPTY correlation_id — "
                "jarvis drops empty-correlation cards, so no offer is made",
                build_id,
            )
            return False

        # (b) Pin main and the exact offered candidate now.
        merge_branch = str(getattr(row, "merge_branch", None) or "").strip() or None
        branch = branch_to_merge(feature_id, merge_branch)
        from forge.deploy.candidate_tree import InContainerCandidateGit

        git = (
            self._git_surface(row.repo, repo_root)
            if self._git_surface is not None
            else None
        ) or InContainerCandidateGit(repo_root)
        expect_main_sha = await self._git_head(repo_root)
        if expect_main_sha is None:
            logger.error(
                "merge-offer: could not read main's sha in %s — an offer "
                "without an expect-main-sha pin would not be honest; no card "
                "for %s",
                repo_root,
                build_id,
            )
            return False
        candidate_sha = await git.rev_parse(branch)
        candidate_tree = (
            await git.rev_parse(f"{candidate_sha}^{{tree}}")
            if candidate_sha
            else None
        )
        if not candidate_sha or not candidate_tree:
            logger.error(
                "merge-offer: could not pin the exact candidate %s for %s — "
                "no card is offered",
                branch,
                build_id,
            )
            return False

        retained_identity: dict[str, Any] | None = None
        runner_identity = (extra_details or {}).get("runner_worktree_retention")
        worktree_path = str(getattr(row, "worktree_path", None) or "").strip()
        if isinstance(runner_identity, Mapping):
            if not worktree_path:
                logger.error(
                    "merge-offer: refusing %s — the runner retained a worktree "
                    "but the build row has no recorded path",
                    build_id,
                )
                return False
            retained_identity = await git.inspect_autobuild_worktree(
                build_id, worktree_path
            )
            registrations = list(
                retained_identity.get("nested_registrations") or []
            )
            candidate_registration = [
                item
                for item in registrations
                if item.get("branch") == f"refs/heads/{branch}"
                and item.get("head") == candidate_sha
            ]
            if not retained_identity.get("ok") or len(candidate_registration) != 1:
                logger.error(
                    "merge-offer: refusing %s — the retained worktree does not "
                    "carry exactly one registered checkout of %s at %s (%s)",
                    build_id,
                    branch,
                    candidate_sha,
                    retained_identity.get("detail"),
                )
                return False
            retained_identity = dict(retained_identity)
            retained_identity["cleanup_registrations"] = candidate_registration

        baseline_failing = self._baseline_reader(build_id)

        # (c) DURABLE LATCH FIRST — probe, then write, BEFORE any wire.
        stages = self._pool.read_stages(build_id)
        if any(
            s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER for s in stages
        ):
            logger.info(
                "merge-offer: %s already has a merge card on record — not "
                "offering twice",
                build_id,
            )
            return False

        request_id = merge_request_id(build_id)
        subject = approval_subject_for(feature_id)
        details: dict[str, Any] = {
            "kind": "merge_deploy_offer",
            "build_id": build_id,
            "feature_id": feature_id,
            "repo": row.repo,
            "branch": branch,
            "merge_branch": merge_branch,
            "expect_main_sha": expect_main_sha,
            "candidate_identity_version": 1,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            **(
                {"worktree_retention": retained_identity}
                if retained_identity is not None
                else {}
            ),
            **dict(extra_details or {}),
            "baseline_failing": baseline_failing,
            "resume_options": ["approve", "reject"],
        }
        now = self._clock()
        self._pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_OFFER_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
                status="GATED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    MERGE_OFFER_DETAILS_KEY: {
                        "request_id": request_id,
                        "correlation_id": row.correlation_id,
                        "approval_subject": subject,
                        **details,
                    }
                },
            )
        )

        # (d) ONE publish attempt ever — dual envelope, approval FIRST.
        rationale = card_words(branch, merge_branch)
        try:
            approval = ApprovalRequestPayload(
                request_id=request_id,
                agent_id=MERGE_AGENT_ID,
                action_description=rationale,
                risk_level="high",
                timeout_seconds=merge_cfg.response_wait_seconds,
                details=details,
            )
            envelope = MessageEnvelope(
                source_id=SOURCE_ID,
                event_type=EventType.APPROVAL_REQUEST,
                correlation_id=row.correlation_id,
                payload=approval.model_dump(mode="json"),
            )
            await self._raw_publish(
                subject, envelope.model_dump_json().encode("utf-8")
            )
            paused = BuildPausedPayload(
                feature_id=feature_id,
                # Deliberately NOT the real build_id: merge-{feature_id} is
                # the join key jarvis uses, and the synthetic id keeps
                # jarvis's terminal registry from refusing the tap.
                build_id=f"merge-{feature_id}",
                stage_label=MERGE_OFFER_STAGE_LABEL,
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                coach_score=None,
                rationale=rationale,
                approval_subject=subject,
                paused_at=now.isoformat(),
                correlation_id=row.correlation_id,
            )
            await self._publisher.publish_build_paused(paused)
        except Exception as exc:  # noqa: BLE001 — one attempt, honest terminal log
            logger.error(
                "merge-offer: publish attempt for %s raised (%s) — the card "
                "may be on the wire; the offer is latched and will NOT be "
                "retried (forge merge-deploy is the attended fallback)",
                build_id,
                exc,
            )
        return True
