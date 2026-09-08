"""The fix task's forward context — the review's findings AND the failure pack.

Revival design pass §a.3 / §b.2 (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

The conductor's ``fix_task_context_builder`` seam is consulted once per
``/task-work`` dispatch (``supervisor.py`` Mode C turn). Until now it was
``None`` in production, so a fix task was dispatched with its fix-task
reference alone.

This adapter fills it, and does exactly two things:

1. **Delegates** to the shipped
   :class:`~forge.pipeline.forward_context_builder.ForwardContextBuilder`
   for the review→work data dependency — the ``--fix-task`` entry plus one
   allow-listed ``--context`` entry per review artefact. That builder owns
   the allowlist gating; this adapter never re-implements it.
2. **Extends** the context with the failed build's **failure pack** index
   (:mod:`forge.pipeline.fix_journey_receipts`) so the fix task starts
   from the evidence the failed build left rather than from a reason
   string.

It is an *adapter*, not a second builder: no allowlist logic, no stage_log
reads, no path arithmetic of its own.

Since 2026-09-08 this module also builds the **follow-up review's
verification document** — the prior review's findings, the commits the
cycle's work legs made since, and one plain instruction to check each
finding against the code that is there now. It is here because the facts
come from the same exported receipts this module already reads. See the
long note above :data:`VERIFY_DOCUMENT_NAME`.

Never raises. A pack that cannot be read degrades to "no pack" — the
supervisor's own call site also guards, but a context builder that can
kill a fix journey would be the wrong shape to hand it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from forge.lifecycle.modes import BuildMode
from forge.pipeline.fix_journey_receipts import read_failure_pack
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

__all__ = [
    "VERIFY_DOCUMENT_NAME",
    "VERIFY_EVIDENCE_SOURCE",
    "VERIFY_INSTRUCTION",
    "CycleCommit",
    "FixTaskContextBuilder",
    "PriorFinding",
    "PriorReviewEvidence",
    "build_review_verification_context",
    "read_prior_review_evidence",
    "render_verify_prior_findings",
    "write_verify_prior_findings",
]


class FixTaskContextBuilder:
    """Adapter over :class:`ForwardContextBuilder` that also reads the pack.

    Call shape matches the supervisor's ``fix_task_context_builder``
    field exactly — ``(stage, build_id, fix_task) -> Mapping[str, Any]``
    — so it drops straight onto the dataclass.

    Args:
        forward_context_builder: The shipped Mode C forward-context
            builder. Consulted through its public ``build_for``.
        source_build_id_reader: ``(fix_build_id) -> str | None`` — which
            FAILED build's pack this journey is repairing. ``None`` (the
            default) reads the pack from the fix journey's OWN build id,
            which is where the queue step that mints a fix build from a
            terminal failure lands it (design pass §b.2). Injected rather
            than derived because the ``builds`` table carries no
            parent-build column today — an honest seam, not a guess.
        receipts_root: Injectable receipts root (tests point it at a
            ``tmp_path``); ``None`` uses the routine path's own law.
        review_artefact_paths_reader: ``(build_id, fix_task) ->
            Iterable[str]`` — the artefact paths the originating
            ``/task-review`` emitted, which the forward-context builder
            gates through the worktree allowlist and threads onto
            ``--context``. ``None`` (the default) yields no paths; the
            review's findings then reach the fix task through the
            failure-pack index alone. See :meth:`_translate_fix_task`.
    """

    def __init__(
        self,
        forward_context_builder: Any,
        *,
        source_build_id_reader: Callable[[str], str | None] | None = None,
        receipts_root: "Path | str | None" = None,
        review_artefact_paths_reader: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._forward = forward_context_builder
        self._source_reader = source_build_id_reader
        self._receipts_root = receipts_root
        self._review_artefact_paths_reader = review_artefact_paths_reader

    def __call__(
        self,
        stage: StageClass,
        build_id: str,
        fix_task: Any,
    ) -> Mapping[str, Any]:
        """Return the forward context for one ``/task-work`` dispatch.

        Returns:
            ``{"context_entries": [...], "failure_pack": {...} | None}``.
            ``context_entries`` is a list of plain
            ``{"flag", "value", "kind"}`` dicts so the mapping is
            JSON-safe end to end (it rides a dispatch payload and a
            ``stage_log`` row).
        """
        entries = self._build_entries(stage, build_id, fix_task)
        pack = self._read_pack(build_id)
        context: dict[str, Any] = {
            "context_entries": entries,
            "failure_pack": pack.to_context() if pack is not None else None,
        }
        return context

    # -- internals ----------------------------------------------------

    def _translate_fix_task(self, build_id: str, fix_task: Any) -> Any:
        """Translate the PLANNER's fix-task ref into the BUILDER's.

        Two distinct ``FixTaskRef`` types exist in the tree and the
        conductor sits between them:

        * :class:`forge.pipeline.mode_c_planner.FixTaskRef` — what the
          planner mints and the supervisor threads (``fix_task_id`` /
          ``review_history_index`` / ``review_stage_label``).
        * :class:`forge.pipeline.forward_context_builder.FixTaskRef` —
          what ``build_for`` consumes (``fix_task_id`` /
          ``task_review_entry_id`` / ``review_artefact_paths``), and whose
          ``to_json()`` becomes the ``--fix-task`` argv payload.

        Handing the planner's value straight to the builder raised
        ``AttributeError: 'FixTaskRef' object has no attribute 'to_json'``
        — which this adapter's own except-clause then swallowed into "no
        forward context entries". The fix task was dispatched with the
        review's findings silently missing, and nothing said so. The
        translation is the adapter's actual job; doing it here is what
        makes the ``--fix-task`` entry appear at all.

        ``review_artefact_paths`` come from the injected
        ``review_artefact_paths_reader`` when one is wired; absent it they
        are empty, and the review's findings ride the failure-pack index
        instead. Empty is honest — a guessed path list is not.
        """
        from forge.pipeline.forward_context_builder import (
            FixTaskRef as ForwardFixTaskRef,
        )

        if fix_task is None or isinstance(fix_task, ForwardFixTaskRef):
            return fix_task
        fix_task_id = getattr(fix_task, "fix_task_id", None)
        if not fix_task_id:
            return fix_task
        entry_id = getattr(fix_task, "task_review_entry_id", None)
        if not entry_id:
            # The planner's back-reference is an INDEX into its history,
            # not a stage_log entry_id. Render it as the audit anchor it
            # is rather than inventing a row identifier.
            label = getattr(fix_task, "review_stage_label", "task-review")
            index = getattr(fix_task, "review_history_index", None)
            entry_id = f"{label}#{index}" if index is not None else str(label)
        paths: tuple[str, ...] = ()
        if self._review_artefact_paths_reader is not None:
            try:
                raw = self._review_artefact_paths_reader(build_id, fix_task)
                paths = tuple(str(p) for p in (raw or ()))
            except Exception as exc:  # noqa: BLE001 — a reader defect is not fatal
                logger.warning(
                    "fix_task_context_builder: review_artefact_paths_reader "
                    "raised %s: %s for build_id=%s fix_task_id=%s — the fix "
                    "task carries no review artefact paths",
                    type(exc).__name__,
                    exc,
                    build_id,
                    fix_task_id,
                )
        return ForwardFixTaskRef(
            fix_task_id=str(fix_task_id),
            task_review_entry_id=str(entry_id),
            review_artefact_paths=paths,
        )

    def _build_entries(
        self, stage: StageClass, build_id: str, fix_task: Any
    ) -> list[dict[str, Any]]:
        try:
            entries = self._forward.build_for(
                stage,
                build_id,
                None,
                mode=BuildMode.MODE_C,
                fix_task=self._translate_fix_task(build_id, fix_task),
            )
        except Exception as exc:  # noqa: BLE001 — a builder defect is not fatal
            logger.warning(
                "fix_task_context_builder: forward-context build_for raised "
                "%s: %s for build_id=%s stage=%s — dispatching with no "
                "forward context entries",
                type(exc).__name__,
                exc,
                build_id,
                getattr(stage, "value", stage),
            )
            return []
        rendered: list[dict[str, Any]] = []
        for entry in entries or ():
            rendered.append(
                {
                    "flag": getattr(entry, "flag", None),
                    "value": getattr(entry, "value", None),
                    "kind": getattr(entry, "kind", None),
                }
            )
        return rendered

    def _read_pack(self, build_id: str):
        source_build_id = build_id
        if self._source_reader is not None:
            try:
                resolved = self._source_reader(build_id)
            except Exception as exc:  # noqa: BLE001 — reader defect is not fatal
                logger.warning(
                    "fix_task_context_builder: source_build_id_reader raised "
                    "%s: %s for build_id=%s — falling back to the fix "
                    "journey's own receipts directory",
                    type(exc).__name__,
                    exc,
                    build_id,
                )
                resolved = None
            if resolved:
                source_build_id = resolved
        return read_failure_pack(
            source_build_id, receipts_root=self._receipts_root
        )


# ---------------------------------------------------------------------------
# The follow-up review's verification document (attempt eight, 2026-09-08)
# ---------------------------------------------------------------------------
#
# WHAT WENT WRONG. Attempt eight ran five work legs inside the sandbox and
# every one of them was approved: the migration column made timezone-aware,
# the model column to match, the delete endpoint's database errors answered
# as 503, the 503 documented, and a Postgres test for the soft delete
# written. Then the follow-up review — the second and last cycle — reported
# the FIRST review's three findings again, word for word, same files, same
# lines, same severities, off a tree where every one of them had been fixed.
# It had re-derived its findings from the task's description instead of
# reading the code in front of it. The driver read two identical reviews as
# "nothing changed" and stopped the journey one step short of its
# merge-ready checks with every fix in place.
#
# WHAT THIS ADDS. One more context document for a review leg that follows at
# least one work leg in the same journey, carrying three things:
#
#   1. the prior review's findings — id, severity, title, file, line, detail;
#   2. the commits the cycle's work legs made since that review — subject and
#      the files each one touched;
#   3. the instruction, in plain English: check each prior finding against
#      the code that is there now, and do not restate the task description.
#
# WHERE THE FACTS COME FROM. The fix journey already exports one directory of
# receipts per stage (``<receipts>/<build id>/stages/<NNN>-<stage>/``), and
# those directories already carry both halves: a review leg's
# ``review_findings.json`` and a work leg's ``task_work_leg_results.json``
# (which records the commit it made) beside its ``task_work_results.json``
# (which records the files it touched). So this reads the receipts, and runs
# no git command of its own. That is not only the smaller change — it is the
# only one that works where it matters: for a repository with a sandbox the
# journey's tree lives INSIDE that sandbox and forge cannot read it (the same
# wall that moved the receipts export onto the sidecar's own route), so a git
# call from here would have nothing to read. The receipts are written from
# inside and read from here, which is exactly the seam that already works.
#
# HOW IT TRAVELS. As one more ``--context`` value on the review leg's
# dispatch, which is how every other context document already travels, and
# which the sandbox door forwards to the leg unchanged. The leg reads a
# ``--context`` value that names a readable file by reading that file, and an
# unreadable one as inline text, and puts either into the review prompt the
# same way. So the document is WRITTEN into the journey worktree when that
# worktree is here to write into, and its path is passed; and when the tree
# is inside a sandbox, where forge can neither read nor write it, the very
# same Markdown travels inline instead — the way the failure-pack summary
# already travels. One document, one flag, whichever carriage can reach the
# leg. A first review, with no prior findings behind it, gets nothing at all
# and its dispatch is byte-identical to what it has always been.


#: Filename of the verification document inside the journey worktree.
VERIFY_DOCUMENT_NAME: str = "verify-prior-findings.md"

#: What the follow-up review is asked to do, in plain English. Stated once,
#: here, so the document and its tests cannot drift into two instructions.
VERIFY_INSTRUCTION: str = (
    "For each prior finding, state whether the code now in this worktree "
    "resolves it, citing the file and line you checked. Report a finding as "
    "open only if the current code still shows it. Do not restate the task "
    "description; review the code."
)

#: Where the facts in the document came from, said in the document itself so
#: a reader never has to guess which source was used.
VERIFY_EVIDENCE_SOURCE: str = (
    "the fix journey's own exported receipts (one directory per stage under "
    "the build's receipts pack)"
)

#: Filenames inside a stage's exported receipts.
_REVIEW_FINDINGS_FILE: str = "review_findings.json"
_WORK_LEG_RESULTS_FILE: str = "task_work_leg_results.json"
_WORK_RESULTS_FILE: str = "task_work_results.json"

#: Where a leg's receipts sit inside a stage export.
_AUTOBUILD_FAMILY: tuple[str, str] = (".guardkit", "autobuild")

#: ``NNN-stage`` stage-directory grammar, the same one the receipts fold
#: writes (:func:`forge.pipeline.fix_journey_receipts.next_stage_key`).
_STAGE_DIR_PATTERN = re.compile(r"^(?P<seq>\d{3})-(?P<rest>.+)$")


@dataclass(frozen=True)
class PriorFinding:
    """One finding the previous review reported."""

    id: str
    severity: str
    title: str
    file: str
    line: str
    detail: str


@dataclass(frozen=True)
class CycleCommit:
    """One commit a work leg made after the previous review."""

    fix_task_id: str
    subject: str
    commit: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class PriorReviewEvidence:
    """Everything the follow-up review is asked to verify against.

    Attributes:
        task_id: The journey's subject task — the directory name the
            review's own receipts were written under, so the document
            lands beside them rather than in a guessed place.
        review_stage_key: The ``NNN-task-review`` directory the findings
            were read from, named in the document so a reader can go and
            look at the same file.
        findings: The prior review's findings, in the order it wrote them.
        commits: The commits the cycle's work legs made since that review,
            oldest first, one per commit (a leg's receipts are re-copied
            by every later stage, so repeats are dropped).
    """

    task_id: str
    review_stage_key: str
    findings: tuple[PriorFinding, ...]
    commits: tuple[CycleCommit, ...]


def _read_json(path: Path) -> Any:
    """Read one JSON file, or ``None`` if it cannot be read or parsed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info(
            "review verification: could not read %s (%s: %s) — carrying on without it",
            path,
            type(exc).__name__,
            exc,
        )
        return None


def _stage_dirs(
    build_id: str, receipts_root: "Path | str | None"
) -> list[tuple[int, str, Path]]:
    """Return ``(sequence, stage name, directory)`` for one build's stages."""
    from forge.pipeline.fix_journey_receipts import (
        STAGES_DIRNAME,
        fix_journey_receipts_root,
    )

    stages_dir = fix_journey_receipts_root(receipts_root) / build_id / STAGES_DIRNAME
    found: list[tuple[int, str, Path]] = []
    try:
        if not stages_dir.is_dir():
            return []
        children = sorted(stages_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.info(
            "review verification: could not list the stage receipts for %s "
            "(%s: %s) — no verification document this turn",
            build_id,
            type(exc).__name__,
            exc,
        )
        return []
    for child in children:
        if not child.is_dir():
            continue
        match = _STAGE_DIR_PATTERN.match(child.name)
        if match is None:
            continue
        found.append((int(match.group("seq")), match.group("rest"), child))
    return found


def _leg_dirs(stage_dir: Path) -> list[Path]:
    """Return the per-task receipt directories inside one stage export."""
    autobuild = stage_dir.joinpath(*_AUTOBUILD_FAMILY)
    try:
        if not autobuild.is_dir():
            return []
        return sorted(
            (p for p in autobuild.iterdir() if p.is_dir()), key=lambda p: p.name
        )
    except OSError:
        return []


def _findings_from_review_stage(
    stage_dir: Path,
) -> tuple[str, tuple[PriorFinding, ...]] | None:
    """Read one review stage's findings, with the task they belong to."""
    for leg_dir in _leg_dirs(stage_dir):
        data = _read_json(leg_dir / _REVIEW_FINDINGS_FILE)
        if not isinstance(data, Mapping):
            continue
        raw_findings = data.get("findings")
        if not isinstance(raw_findings, (list, tuple)) or not raw_findings:
            continue
        findings: list[PriorFinding] = []
        for element in raw_findings:
            if not isinstance(element, Mapping):
                continue
            findings.append(
                PriorFinding(
                    id=str(element.get("id") or "(no id)"),
                    severity=str(element.get("severity") or "(no severity)"),
                    title=str(element.get("title") or "(no title)"),
                    file=str(element.get("file") or "(no file named)"),
                    line=str(
                        element.get("line")
                        if element.get("line") is not None
                        else "(no line named)"
                    ),
                    detail=str(element.get("detail") or ""),
                )
            )
        if findings:
            return leg_dir.name, tuple(findings)
    return None


def _is_receipt_path(value: str) -> bool:
    """``True`` for a leg's own paperwork rather than the repository's code.

    A leg writes its receipts inside the tree it works in, and they are
    counted among the files it changed. Listing them would send the
    reviewer to look at the leg's notes instead of at the code.
    """
    parts = Path(value).parts
    return ".guardkit" in parts


def _committed_shas(stage_dirs: "list[tuple[int, str, Path]]") -> set[str]:
    """Every commit sha that the given stage exports already carry.

    Used for the stages up to and including the previous review, so that
    work the previous review had already seen is not offered to the next
    one as though it were new.
    """
    shas: set[str] = set()
    for _seq, _name, stage_dir in stage_dirs:
        for leg_dir in _leg_dirs(stage_dir):
            leg = _read_json(leg_dir / _WORK_LEG_RESULTS_FILE)
            if not isinstance(leg, Mapping):
                continue
            commit = leg.get("commit")
            if not isinstance(commit, Mapping) or commit.get("committed") is not True:
                continue
            sha = str(commit.get("head_after") or "").strip()
            if sha:
                shas.add(sha)
    return shas


def _commits_from_work_stages(
    stage_dirs: "list[tuple[int, str, Path]]",
    *,
    already_seen: "set[str] | frozenset[str]" = frozenset(),
) -> tuple[CycleCommit, ...]:
    """Read the commits the work legs made, oldest first, without repeats.

    Every stage export re-copies the whole receipt family, so a leg that
    ran early appears again in every later stage's directory. Walking the
    stages in order and keeping the first sighting of each commit gives
    one row per commit, in the order the legs ran.

    ``already_seen`` carries the commits the previous review could already
    see in its own receipts. In a journey that reaches a third review those
    copies sit in the later stages too, and listing them would tell the
    reviewer that a finding was fixed by a commit made before the review
    that reported it. They are dropped.
    """
    seen: set[str] = set(already_seen)
    commits: list[CycleCommit] = []
    for _seq, _name, stage_dir in stage_dirs:
        for leg_dir in _leg_dirs(stage_dir):
            leg = _read_json(leg_dir / _WORK_LEG_RESULTS_FILE)
            if not isinstance(leg, Mapping):
                continue
            commit = leg.get("commit")
            if not isinstance(commit, Mapping) or commit.get("committed") is not True:
                continue
            sha = str(commit.get("head_after") or "").strip()
            if not sha or sha in seen:
                continue
            seen.add(sha)
            message = str(commit.get("message") or "").strip()
            subject = message.splitlines()[0] if message else "(no commit message)"
            files: list[str] = []
            results = _read_json(leg_dir / _WORK_RESULTS_FILE)
            if isinstance(results, Mapping):
                for key in ("files_modified", "files_created"):
                    values = results.get(key)
                    if isinstance(values, (list, tuple)):
                        # A leg's own receipts land inside the tree and are
                        # counted among the files it changed. They are not
                        # code, and listing them would send the reviewer to
                        # look at the leg's paperwork.
                        files.extend(
                            str(v) for v in values if not _is_receipt_path(str(v))
                        )
            commits.append(
                CycleCommit(
                    fix_task_id=str(leg.get("task_id") or leg_dir.name),
                    subject=subject,
                    commit=sha,
                    files=tuple(dict.fromkeys(files)),
                )
            )
    return tuple(commits)


def read_prior_review_evidence(
    build_id: str,
    *,
    receipts_root: "Path | str | None" = None,
) -> PriorReviewEvidence | None:
    """Read what the follow-up review is being asked to verify.

    Returns ``None`` — meaning "no document, dispatch exactly as before" —
    whenever any of the three conditions for one is missing:

    * no exported stages at all (this IS the journey's first review);
    * a previous review that reported nothing (there is nothing to check);
    * no work leg between that review and now (nothing has changed).

    Never raises. Unreadable or malformed receipts are the same answer as
    missing ones, said once at INFO.
    """
    stages = _stage_dirs(build_id, receipts_root)
    if not stages:
        return None
    reviews = [row for row in stages if row[1].startswith("task-review")]
    if not reviews:
        return None
    prior_seq, _name, prior_dir = max(reviews, key=lambda row: row[0])
    work_after = [
        row for row in stages if row[0] > prior_seq and row[1].startswith("task-work")
    ]
    if not work_after:
        logger.info(
            "review verification: build_id=%s has a previous review (%s) but "
            "no work leg after it — no verification document this turn",
            build_id,
            prior_dir.name,
        )
        return None
    found = _findings_from_review_stage(prior_dir)
    if found is None:
        logger.info(
            "review verification: build_id=%s found no earlier findings to "
            "check in the previous review's receipts (%s) — it reported none, "
            "or they could not be read. The review is dispatched as before, "
            "with no verification document",
            build_id,
            prior_dir.name,
        )
        return None
    task_id, findings = found
    # The previous review's own stage export, and every stage before it,
    # already carried these commits. They are not this cycle's work.
    seen_before = _committed_shas(
        sorted((row for row in stages if row[0] <= prior_seq), key=lambda row: row[0])
    )
    commits = _commits_from_work_stages(
        sorted(work_after, key=lambda row: row[0]), already_seen=seen_before
    )
    return PriorReviewEvidence(
        task_id=task_id,
        review_stage_key=prior_dir.name,
        findings=findings,
        commits=commits,
    )


def render_verify_prior_findings(evidence: PriorReviewEvidence) -> str:
    """Render the verification document as plain Markdown."""
    lines: list[str] = [
        "# Verify the previous review's findings against the code that is here now",
        "",
        "This review follows work that has already been done in this worktree. "
        "Below are the findings the previous review reported and the commits "
        "made since then.",
        "",
        f"Source of these facts: {VERIFY_EVIDENCE_SOURCE}. The findings were "
        f"read from the `{evidence.review_stage_key}` receipts; the commits "
        "from the work legs that ran after it.",
        "",
        "## What you are asked to do",
        "",
        VERIFY_INSTRUCTION,
        "",
        f"## The previous review's findings ({len(evidence.findings)})",
        "",
    ]
    for finding in evidence.findings:
        lines.extend(
            [
                f"### {finding.id} — {finding.title}",
                "",
                f"- Severity: {finding.severity}",
                f"- File: {finding.file}",
                f"- Line: {finding.line}",
                "",
                finding.detail or "(no detail recorded)",
                "",
            ]
        )
    lines.extend([f"## Commits made since that review ({len(evidence.commits)})", ""])
    if not evidence.commits:
        lines.extend(
            [
                "No commit was recorded for the work legs that ran after that "
                "review. Read the worktree itself before reporting anything as "
                "still open.",
                "",
            ]
        )
    for entry in evidence.commits:
        files = ", ".join(entry.files) if entry.files else "(no files recorded)"
        lines.extend(
            [
                f"### {entry.subject}",
                "",
                f"- Commit: {entry.commit}",
                f"- Fix task: {entry.fix_task_id}",
                f"- Files touched: {files}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_verify_prior_findings(
    *,
    worktree_path: "Path | str | None",
    evidence: PriorReviewEvidence,
    text: str,
) -> Path | None:
    """Write the document into the journey worktree, or say why it could not.

    The document belongs beside the review's own receipts, in
    ``<worktree>/.guardkit/autobuild/<task id>/``. It is written only when
    that worktree is really here: for a repository with a sandbox the tree
    is inside the sandbox and forge can neither read nor write it, and
    creating a lookalike directory on this side would leave the leg a path
    that does not exist where it runs. ``None`` then, and the caller sends
    the same Markdown inline instead.

    Never raises.
    """
    if worktree_path is None:
        return None
    tree = Path(str(worktree_path))
    try:
        if not tree.is_dir():
            logger.info(
                "review verification: the journey worktree %s is not on this "
                "side (a repository with a sandbox keeps its tree inside), so "
                "the verification document travels with the dispatch instead "
                "of being written to a file",
                tree,
            )
            return None
        dest_dir = tree / _AUTOBUILD_FAMILY[0] / _AUTOBUILD_FAMILY[1] / evidence.task_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / VERIFY_DOCUMENT_NAME
        dest.write_text(text, encoding="utf-8")
        return dest
    except OSError as exc:
        logger.info(
            "review verification: could not write %s into %s (%s: %s) — the "
            "verification document travels with the dispatch instead",
            VERIFY_DOCUMENT_NAME,
            tree,
            type(exc).__name__,
            exc,
        )
        return None


def build_review_verification_context(
    *,
    build_id: str,
    worktree_path: "Path | str | None",
    receipts_root: "Path | str | None" = None,
) -> dict[str, Any] | None:
    """Return the one extra context entry for a follow-up review, or ``None``.

    ``None`` means "change nothing": a first review, a previous review that
    found nothing, no work since it, or receipts that could not be read.
    The entry, when there is one, has the same plain
    ``{"flag", "value", "kind"}`` shape every other forward-context entry
    has, so it rides the dispatch and the ``stage_log`` row unchanged.

    Never raises.
    """
    try:
        evidence = read_prior_review_evidence(build_id, receipts_root=receipts_root)
        if evidence is None:
            return None
        text = render_verify_prior_findings(evidence)
        path = write_verify_prior_findings(
            worktree_path=worktree_path, evidence=evidence, text=text
        )
        if path is not None:
            logger.info(
                "review verification: build_id=%s is handed %d earlier "
                "finding(s) and %d commit(s) to check, written to %s",
                build_id,
                len(evidence.findings),
                len(evidence.commits),
                path,
            )
            return {"flag": "--context", "value": str(path), "kind": "path"}
        logger.info(
            "review verification: build_id=%s is handed %d earlier finding(s) "
            "and %d commit(s) to check, sent with the dispatch itself",
            build_id,
            len(evidence.findings),
            len(evidence.commits),
        )
        return {"flag": "--context", "value": text, "kind": "text"}
    except Exception as exc:  # noqa: BLE001 — a reader defect must not kill a journey
        logger.warning(
            "review verification: building the document raised %s: %s for "
            "build_id=%s — the review is dispatched exactly as before",
            type(exc).__name__,
            exc,
            build_id,
        )
        return None
