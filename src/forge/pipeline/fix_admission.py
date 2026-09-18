"""Opening a fix journey: the one place a repair becomes a queued build.

Public surface
==============

- :func:`admit_fix_build` — the whole of what ``forge queue --mode c`` used
  to do inline: THE CAP LAW, the task-id check, the fix-task YAML's
  ``parent_feature``, the build row, the publish.
- :func:`admit_fix_row` — the queue's path into the same function: it mints
  the task id, writes the fix-task YAML beside the target repository's
  features, and then calls :func:`admit_fix_build`.
- :func:`republish_build_queued` — says a written-but-never-announced
  build's queued event again, rebuilt from the build row itself.
- :class:`FixAdmission` — what a successful admission hands back.
- :class:`FixAdmissionRefused` / :class:`FixPublishFailed` — the two ways it
  can end badly, each with a sentence a person can read.

Why it is a module and not a branch of the CLI
----------------------------------------------

Until now the only way to open a fix journey was to type
``forge queue --mode c`` — which is why the conductor has been idle since 4
August. The work queue can now admit a repair by itself, and the one thing
that must not happen is a second, subtly different statement of how a fix
journey opens. In particular THE CAP LAW — a fix journey whose budget
profile carries no review-cycle cap does not open, because the 2026-08-02
crossing ran about two hundred legs when nothing capped it — has to be the
same rule on both paths. So the steps live here, both callers call them, and
the law is read from :mod:`forge.config.conductor` exactly once per
admission.

**Never a shell-out.** The queue does not run ``forge queue`` in a
subprocess: it calls this function in process, with the queue row's own
correlation id, so the row, the build and every downstream receipt share one
spine.

The task id, and the file beside the features
---------------------------------------------

A fix journey's subject is a TASK id, not a feature id, and the wire's
pattern for one is narrow: ``TASK-`` followed by three to twelve upper-case
letters and digits. :func:`mint_fix_task_id` spells it
``TASK-<feature8>FIX<n>`` — the parent feature with its punctuation removed
and cut to eight characters, then ``FIX``, then the next free number — and
trims the feature half further if the whole would overflow twelve.

The fix-task YAML it writes is the drive-6 shape and nothing more: ``id``,
``name``, ``parent_feature``. On the queue's path it lives at
``.guardkit/features/<task id>.yaml`` ON THE REPAIR BRANCH, beside the task
file; the CLI's path takes it wherever the operator put it (``--feature-yaml``)
and commits a copy at the same place on the branch.

The task file on the repair branch (Part L, 2026-09-07)
------------------------------------------------------

Journey one refused in four seconds: guardkit's review leg loads its subject
by id from ``tasks/backlog/**/<TASK-id>*.md`` in the build's worktree, which
is a branch the conductor cuts from the build's branch
(:func:`forge.cli._conductor_worktree.prepare_journey_worktree`), so only
committed files on that branch are visible to the legs — and the admission
used to write one uncommitted YAML into the shared checkout. Now
:func:`materialise_repair_task` gathers what was observed (the merge report,
the gate evidence, the failure pack), renders
a task file in the repository's own frontmatter shape, and commits it with
the YAML on ``repair/<task id>``, cut from the build's target branch, through
:mod:`forge.pipeline.repair_branch`. The build is queued on that branch. Both
doors take the same path: :func:`admit_fix_row` always; ``forge queue --mode
c`` when the branch it was given carries no task file for the id.

References
----------
- ``docs/conductor-rewire-spec-2026-09-05.md`` rule 2.
- ``docs/rewrite-on-refusal-spec-2026-09-06.md`` Part L, rules 48 to 52.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)


#: The wire's pattern for a fix journey's subject identifier. Mirrors
#: ``TASK_ID_PATTERN`` in ``nats_core.events._pipeline`` so a bad id is
#: refused here rather than one stack frame deeper.
TASK_ID_REGEX: re.Pattern[str] = re.compile(r"^TASK-[A-Z0-9]{3,12}$")

#: How long the identifier after ``TASK-`` may be.
MAX_TASK_SUFFIX_CHARS: int = 12

#: The word that separates the parent feature from the repair's number.
FIX_TOKEN: str = "FIX"

#: Where a target repository keeps the specs its legs are given.
FEATURES_DIR_PARTS: tuple[str, ...] = (".guardkit", "features")

#: NATS subject family for build-queued events.
BUILD_QUEUED_SUBJECT_PREFIX: str = "pipeline.build-queued"

#: Source id stamped on the envelope the queue's own admission publishes.
SOURCE_ID: str = "forge"

#: The events-row action written on the queue row when its build is open.
ADMITTED_BUILD_ACTION: str = "admitted_build"

#: The events-row action written on the queue row when the queue says a
#: written-but-never-dispatched build's queued event again (close-out item 2).
REPUBLISHED_ACTION: str = "republished"

#: The refusal reason that means "there is already a build for this".
DUPLICATE_REASON: str = "duplicate"

#: The refusals that mean "not now" rather than "not ever". Everything else a
#: refusal can say — an unknown repository, an uncapped profile, a malformed
#: fix-task file, a row that names no build — will say the same thing on the
#: next tick, so the queue closes the row instead of asking again for ever.
TRANSIENT_REFUSAL_REASONS: frozenset[str] = frozenset({DUPLICATE_REASON})

#: The refusal reason when the repair's task file could not be written or
#: committed on its branch (Part L, rule 50).
REPAIR_TASK_REASON: str = "repair-task"

#: The events-row action written on the queue row once its repair branch
#: carries the task file — read back so a second admission of the same row
#: reuses the same task id and branch.
REPAIR_BRANCH_ACTION: str = "repair_branch"

#: Where a merge's receipts live under the receipts root, and the report's name.
MERGE_RECEIPTS_PREFIX: str = "merge-"
MERGE_REPORT_NAME: str = "merge_deploy_report.json"

#: Where a repository's live gate writes its evidence, and the file's name.
GATE_EVIDENCE_DIR_PARTS: tuple[str, ...] = ("qa", "gates", "evidence")
GATE_EVIDENCE_NAME: str = "EVIDENCE.yaml"

#: The complexity a repair task declares. guardkit's legs read neither this
#: nor ``task_type``; the value keeps the frontmatter in the repository's own
#: shape, and a repair scoped to named checks is small.
REPAIR_TASK_COMPLEXITY: int = 3

#: Plain words for why the queue filed a repair, by the producer's source word.
_FILED_BECAUSE: dict[str, str] = {
    "merge-report": "the merge landed and the checks after it went red",
    "candidate-refused": (
        "the branch failed its sandbox check before the merge, so nothing was merged"
    ),
    "build-failed": "the build failed",
}

#: A build-failed repair whose retained candidate cannot be identified exactly.
REPAIR_BASE_REASON: str = "repair-base"


#: ``expected '…', observed '…'`` at the end of a gate evidence description.
_EXPECTED_OBSERVED: re.Pattern[str] = re.compile(
    r"""expected (['"])(.*?)\1, observed (['"])(.*)\3\s*$""", re.DOTALL
)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixAdmission:
    """What opening a fix journey produced."""

    build_id: str
    task_id: str
    feature_id: str
    correlation_id: str
    repo: str
    fix_task_path: str
    source_build_id: str | None = None
    published: bool = True
    #: The branch the build was queued on — ``repair/<task id>`` when the
    #: admission put the task file on one, else the branch it was given.
    branch: str = "main"
    #: The task file's path on that branch, relative to the checkout.
    task_file_path: str | None = None
    #: The tip of the repair branch when one was prepared.
    repair_commit: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedBranch:
    """What a ``prepare_branch`` hook hands back: the branch to queue on."""

    branch: str
    task_file_path: str | None = None
    commit: str | None = None


@dataclass(frozen=True, slots=True)
class RepairBase:
    """The branch a repair is cut from and its pinned commit, when required."""

    branch: str
    expected_commit: str | None = None


class FixAdmissionRefused(Exception):
    """The journey did not open, and nothing was written.

    Attributes:
        message: One plain sentence saying why.
        reason: A short machine word for the caller to map onto its own
            exit code — one of ``cap``, ``task-id``, ``fix-task-yaml``,
            ``parent-feature``, ``repo-not-allowed``, ``repo-unknown``,
            ``no-source-build``, ``repair-base``, ``repair-task`` or
            ``duplicate``.
        permanent: Whether trying again changes anything. A repository the
            configuration does not know, a budget profile with no cap, a row
            that names no build, a fix-task file that will not parse: every
            one of those refuses exactly the same way on the next tick, so
            the queue closes the row rather than offering it for ever. The
            one refusal that is NOT permanent is ``duplicate`` — another
            build for the same feature is in flight right now, and when it
            ends this row can go.
    """

    def __init__(
        self, message: str, *, reason: str, permanent: bool | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.permanent = (
            reason not in TRANSIENT_REFUSAL_REASONS if permanent is None else permanent
        )


class FixPublishFailed(Exception):
    """The build row was written but the pipeline was not told about it.

    The row is deliberately NOT rolled back: SQLite is the pipeline's truth
    and the on-boot reconciler redrives an orphaned row. Carries the
    admission so the caller can say what was written.
    """

    def __init__(self, message: str, *, admission: FixAdmission) -> None:
        super().__init__(message)
        self.message = message
        self.admission = admission


# ---------------------------------------------------------------------------
# The task id and the file beside the features
# ---------------------------------------------------------------------------


def features_dir(repo_path: Path | str) -> Path:
    """The directory a repository keeps its feature and fix-task specs in."""
    return Path(repo_path).joinpath(*FEATURES_DIR_PARTS)


def _feature_stem(feature_id: str) -> str:
    """The parent feature as up to eight upper-case letters and digits."""
    stripped = "".join(ch for ch in feature_id.upper() if ch.isalnum())
    return stripped[:8] or "FIX"


def mint_fix_task_id(feature_id: str, *, existing: Iterable[str] = ()) -> str:
    """``TASK-<feature8>FIX<n>`` — the next repair of this feature.

    ``existing`` is every task id already spoken for (the fix-task files
    already in the repository's features directory, say). The number is the
    first one not in that set, counting from 1, and the feature half is cut
    down as far as it has to be so the whole identifier stays inside the
    wire's twelve characters.
    """
    taken = {str(item).upper() for item in existing}
    stem = _feature_stem(feature_id)
    number = 1
    while True:
        tail = f"{FIX_TOKEN}{number}"
        room = MAX_TASK_SUFFIX_CHARS - len(tail)
        if room < 1:
            # A repair number long enough to crowd out the feature name is
            # not a numbering problem any more; say so rather than mint an
            # identifier nobody can read back to a feature.
            raise FixAdmissionRefused(
                f"there are already {number - 1} repairs of {feature_id} and "
                "no room left in a task identifier for another",
                reason="task-id",
                permanent=True,
            )
        candidate = f"TASK-{stem[:room]}{tail}"
        if candidate not in taken:
            return candidate
        number += 1


def existing_fix_task_ids(repo_path: Path | str) -> set[str]:
    """Every task id that already has a file in the repository's features."""
    directory = features_dir(repo_path)
    try:
        names = {path.stem.upper() for path in directory.glob("TASK-*.y*ml")}
    except OSError:  # pragma: no cover - unreadable directory
        names = set()
    # A repair that already rides a ``repair/<task id>`` branch has its id
    # spoken for, whether or not a YAML sits in the checkout.
    try:
        from forge.pipeline.repair_branch import repair_task_ids_on_branches

        names |= repair_task_ids_on_branches(repo_path)
    except Exception:  # noqa: BLE001 — a checkout git cannot read counts no ids
        pass
    return names


def write_fix_task_yaml(
    *,
    repo_path: Path | str,
    task_id: str,
    parent_feature: str,
    name: str,
) -> Path:
    """Write the three-field fix-task spec and return where it landed.

    The drive-6 shape and nothing else: ``id``, ``name``, ``parent_feature``.
    Written beside the repository's features, because that is where the legs
    of the journey are pointed.
    """
    directory = features_dir(repo_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.yaml"
    path.write_text(
        fix_task_yaml_text(task_id=task_id, parent_feature=parent_feature, name=name),
        encoding="utf-8",
    )
    return path


def fix_task_yaml_text(*, task_id: str, parent_feature: str, name: str) -> str:
    """The three-field fix-task YAML as text — one spelling for both doors."""
    import yaml

    return yaml.safe_dump(
        {"id": task_id, "name": name, "parent_feature": parent_feature},
        sort_keys=False,
        default_flow_style=False,
    )


def fix_task_yaml_relpath(task_id: str) -> str:
    """``.guardkit/features/<task id>.yaml`` — where the YAML sits on the branch."""
    return "/".join((*FEATURES_DIR_PARTS, f"{task_id}.yaml"))


def read_parent_feature(yaml_path: Path | str) -> str:
    """The ``parent_feature`` a fix-task YAML declares.

    Raises:
        FixAdmissionRefused: when the file cannot be read, is not a mapping,
            or declares no non-empty ``parent_feature``.
    """
    import yaml

    path = Path(yaml_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixAdmissionRefused(
            f"Cannot read fix-task YAML {str(path)!r}: {exc}",
            reason="fix-task-yaml",
            permanent=True,
        ) from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FixAdmissionRefused(
            f"Fix-task YAML {str(path)!r} is malformed: {exc}",
            reason="fix-task-yaml",
            permanent=True,
        ) from exc

    if not isinstance(data, dict):
        raise FixAdmissionRefused(
            f"Fix-task YAML {str(path)!r} must be a YAML mapping at the top level",
            reason="fix-task-yaml",
            permanent=True,
        )

    parent = data.get("parent_feature")
    if not isinstance(parent, str) or not parent.strip():
        raise FixAdmissionRefused(
            "Mode C requires the fix-task YAML to declare a non-empty "
            f"'parent_feature' field (string); got {parent!r} in {path}",
            reason="fix-task-yaml",
            permanent=True,
        )
    return parent


def read_fix_task_name(yaml_path: Path | str) -> str | None:
    """The ``name`` a fix-task YAML declares, or None when it declares none."""
    import yaml

    try:
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return _one_line(name) if isinstance(name, str) and name.strip() else None


# ---------------------------------------------------------------------------
# The task file on the repair branch (Part L)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedCheck:
    """One check that went red, with what it expected and saw when recorded."""

    name: str
    expected: str | None = None
    observed: str | None = None


@dataclass(frozen=True, slots=True)
class RepairTaskFacts:
    """Everything the task file says, gathered from the records that exist."""

    task_id: str
    feature_id: str
    name: str
    base_branch: str = "main"
    source_build_id: str | None = None
    filed_because: str | None = None
    result: str | None = None
    detail: str | None = None
    merged_sha: str | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    failed_checks: tuple[FailedCheck, ...] = ()
    merge_report_path: str | None = None
    gate_evidence_path: str | None = None
    failure_pack_path: str | None = None
    parent_review: str | None = None


def repair_task_relpath(folder: str, task_id: str) -> str:
    """``tasks/backlog/<folder>/<task id>-repair.md``."""
    return f"tasks/backlog/{folder}/{task_id}-repair.md"


def repair_task_folder(feature_id: str, files_on_base: Iterable[str]) -> str:
    """The parent feature's task folder under ``tasks/backlog/``, else the id lower-cased.

    The repository's own task files are named ``TASK-<feature code>-NNN-…``
    (``TASK-39F6-003-update-user-crud.md`` for FEAT-39F6), so the folder is
    the one under ``tasks/backlog/`` holding files of that name — matched on
    the feature's code after ``FEAT-`` and, for safety, on its eight-character
    stem too. A loose file directly under ``tasks/backlog/`` names no folder.
    """
    short = feature_id.split("-", 1)[1] if "-" in feature_id else feature_id
    stems = {f"TASK-{short.upper()}-", f"TASK-{_feature_stem(feature_id)}-"}
    counts: dict[str, int] = {}
    for path in files_on_base:
        parts = Path(path).parts
        if len(parts) != 4 or parts[0] != "tasks" or parts[1] != "backlog":
            continue
        name = parts[3].upper()
        if name.endswith(".MD") and any(name.startswith(stem) for stem in stems):
            counts[parts[2]] = counts.get(parts[2], 0) + 1
    if counts:
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return feature_id.lower()


def render_repair_task_file(facts: RepairTaskFacts) -> str:
    """The task file's text: the repository's own frontmatter, then rule 48's body."""
    import yaml

    from forge.pipeline.repair_branch import repair_branch_name

    title = (
        f"Repair of {facts.source_build_id}"
        if facts.source_build_id
        else f"Repair of {facts.feature_id}"
    )
    front: dict[str, Any] = {"id": facts.task_id, "title": title, "task_type": "fix"}
    if facts.parent_review:
        front["parent_review"] = facts.parent_review
    front["feature_id"] = facts.feature_id
    front["wave"] = 1
    front["implementation_mode"] = "task-work"
    front["complexity"] = REPAIR_TASK_COMPLEXITY
    front["dependencies"] = []
    header = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)

    observed: list[str] = []
    if facts.result:
        line = f"- Result: {facts.result}"
        if facts.detail:
            line += f" — {facts.detail}"
        observed.append(line)
    elif facts.filed_because:
        observed.append(f"- Why the repair was filed: {facts.filed_because}")
    if facts.source_build_id:
        line = f"- Source build: {facts.source_build_id} (feature {facts.feature_id}"
        if facts.merged_sha:
            line += f", merged commit {facts.merged_sha[:12]}"
        observed.append(line + ")")
    if facts.checks_passed is not None and facts.checks_total is not None:
        observed.append(
            f"- Checks: {facts.checks_passed} of {facts.checks_total} passed"
        )
    if facts.failed_checks:
        observed.append("- These checks failed:")
        for check in facts.failed_checks:
            line = f"  - {check.name}"
            if check.expected or check.observed:
                line += (
                    f": expected {check.expected or 'not recorded'}, "
                    f"observed {check.observed or 'not recorded'}"
                )
            observed.append(line)
    else:
        observed.append(
            "- No failing check was named on the report or the gate evidence; "
            "the evidence below is the record."
        )

    if facts.merge_report_path:
        report_line = f"- Merge report: {facts.merge_report_path}"
    elif facts.source_build_id:
        report_line = "- Merge report: none was found under the receipts root"
    else:
        report_line = "- Merge report: not recorded (this repair names no source build)"
    evidence = [
        report_line,
        f"- Gate evidence: {facts.gate_evidence_path or 'not recorded'}",
        f"- Failure pack: {facts.failure_pack_path or 'none was recorded for this build'}",
    ]

    if facts.failed_checks:
        names = ", ".join(check.name for check in facts.failed_checks)
        first = f"- [ ] The failed checks pass: {names}"
    else:
        first = "- [ ] The checks that failed pass"
    criteria = [first, "- [ ] The feature's existing tests stay green"]

    notes = [
        "- Read the evidence named above before changing code.",
        f"- This task and its YAML are committed on the branch "
        f"{repair_branch_name(facts.task_id)}, cut from {facts.base_branch}; the "
        "fix journey's own branch is cut from there, so both files are in its "
        "worktree.",
    ]

    body = "\n".join(
        [
            f"# {title}",
            "",
            facts.name,
            "",
            "## What was observed",
            "",
            *observed,
            "",
            "## Where the evidence is",
            "",
            *evidence,
            "",
            "## Acceptance Criteria",
            "",
            *criteria,
            "",
            "## Implementation Notes",
            "",
            *notes,
            "",
        ]
    )
    return f"---\n{header}---\n\n{body}"


def _frontmatter(text: str) -> dict[str, Any]:
    """The YAML between the leading ``---`` lines, or ``{}``."""
    import yaml

    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str | None:
    return _one_line(value) if isinstance(value, str) and value.strip() else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _default_receipts_root() -> Path:
    try:
        from forge.receipts import receipts_root

        return receipts_root()
    except Exception:  # noqa: BLE001 — the report is then simply not found
        return Path("~/forge-state/receipts").expanduser()


def _read_merge_report(
    source_build_id: str, receipts_root: Path | str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """``(path, report)`` — the path when the file exists, the report when it parses."""
    root = Path(receipts_root).expanduser() if receipts_root else _default_receipts_root()
    path = root / f"{MERGE_RECEIPTS_PREFIX}{source_build_id}" / MERGE_REPORT_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (str(path) if path.is_file() else None), None
    return str(path), (data if isinstance(data, dict) else None)


def _newest_gate_evidence(repo: Path, feature_id: str) -> tuple[str | None, str | None]:
    """``(relative path, text)`` of the newest ``qa/gates/evidence/<FEAT>-*/EVIDENCE.yaml``."""
    root = repo.joinpath(*GATE_EVIDENCE_DIR_PARTS)
    try:
        runs = sorted(
            entry
            for entry in root.iterdir()
            if entry.is_dir()
            and entry.name.startswith(f"{feature_id}-")
            and (entry / GATE_EVIDENCE_NAME).is_file()
        )
    except OSError:
        return None, None
    if not runs:
        return None, None
    path = runs[-1] / GATE_EVIDENCE_NAME
    try:
        return str(path.relative_to(repo)), path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, None


def _parse_gate_evidence(text: str) -> tuple[int, list[FailedCheck]]:
    """``(passed, failed)`` from an EVIDENCE.yaml; the checks say ``[pass]``/``[fail]``."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return 0, []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0, []
    passed = 0
    failed: list[FailedCheck] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        description = str(entry.get("description") or "")
        verdict = str(entry.get("verdict") or "").lower()
        if verdict in {"fail", "failed"} or "[fail]" in description:
            name = str(entry.get("checkpoint_or_assertion_id") or entry.get("id") or description)
            match = _EXPECTED_OBSERVED.search(description)
            failed.append(
                FailedCheck(
                    name=_one_line(name),
                    expected=_one_line(match.group(2)) if match else None,
                    observed=_one_line(match.group(4)) if match else None,
                )
            )
        elif verdict in {"pass", "passed"} or "[pass]" in description:
            passed += 1
    return passed, failed


def _parent_review_on_branch(
    repo: Path, base_branch: str, feature_id: str, folder: str, files_on_base: Iterable[str]
) -> str | None:
    """The ``parent_review`` the feature's own task files carry, when they do."""
    from forge.pipeline.repair_branch import read_branch_file

    head = f"tasks/backlog/{folder}/"
    for path in sorted(files_on_base):
        name = Path(path).name.upper()
        if not (path.startswith(head) and name.startswith("TASK-") and name.endswith(".MD")):
            continue
        front = _frontmatter(read_branch_file(repo, base_branch, path) or "")
        declared = front.get("feature_id")
        if declared not in (None, feature_id):
            continue
        review = front.get("parent_review")
        if isinstance(review, str) and review.strip():
            return review.strip()
    return None


def gather_repair_facts(
    *,
    repo_path: Path | str,
    task_id: str,
    feature_id: str,
    name: str,
    base_branch: str = "main",
    source_build_id: str | None = None,
    minted: Mapping[str, Any] | None = None,
    receipts_root: Path | str | None = None,
    files_on_base: Iterable[str] | None = None,
) -> RepairTaskFacts:
    """Read what the records say about the failure; never raise for a missing one.

    The merge report under ``<receipts root>/merge-<source build>/`` gives the
    result word, the detail, the merged commit and the counts; the newest
    gate evidence for the feature in the checkout's ``qa/gates/evidence/``
    names the checks that failed with what they expected and saw; the queue
    row's filing note (``minted``) gives the failure pack and why the row was
    filed; the feature's own task files on the base branch give the review id.
    """
    from forge.pipeline.repair_branch import list_branch_files

    repo = Path(repo_path)
    note = dict(minted or {})
    files = list(files_on_base) if files_on_base is not None else list_branch_files(
        repo, base_branch, "tasks"
    )
    folder = repair_task_folder(feature_id, files)

    result = detail = merged_sha = None
    checks_passed = checks_total = None
    failed: list[FailedCheck] = []
    report_path: str | None = None
    if source_build_id:
        report_path, report = _read_merge_report(source_build_id, receipts_root)
        if report:
            result = _text(report.get("result"))
            detail = _text(report.get("detail"))
            merged_sha = _text(report.get("merged_sha"))
            checks_passed = _int(report.get("checks_passed"))
            checks_total = _int(report.get("checks_total"))
            gate = report.get("gate_before_merge")
            if isinstance(gate, dict):
                for check in gate.get("failed_checks") or []:
                    if _text(check):
                        failed.append(FailedCheck(name=_one_line(check)))
                if checks_total is None:
                    checks_total = _int(gate.get("checks_total"))
                    checks_passed = _int(gate.get("checks_passed"))

    evidence_path, evidence_text = _newest_gate_evidence(repo, feature_id)
    if evidence_text:
        passed_count, from_evidence = _parse_gate_evidence(evidence_text)
        if from_evidence:
            by_name = {check.name: check for check in from_evidence}
            merged = [by_name.pop(check.name, check) for check in failed]
            merged.extend(by_name.values())
            failed = merged
        if checks_total is None:
            checks_total = passed_count + len(from_evidence)
            checks_passed = passed_count

    return RepairTaskFacts(
        task_id=task_id,
        feature_id=feature_id,
        name=name,
        base_branch=base_branch,
        source_build_id=source_build_id,
        filed_because=_FILED_BECAUSE.get(str(note.get("source") or "")),
        result=result,
        detail=detail,
        merged_sha=merged_sha,
        checks_passed=checks_passed,
        checks_total=checks_total,
        failed_checks=tuple(failed),
        merge_report_path=report_path,
        gate_evidence_path=evidence_path,
        failure_pack_path=_text(note.get("failure_pack_path")),
        parent_review=_parent_review_on_branch(
            repo, base_branch, feature_id, folder, files
        ),
    )


def materialise_repair_task(
    *,
    repo_path: Path | str,
    task_id: str,
    feature_id: str,
    name: str,
    base_branch: str = "main",
    source_build_id: str | None = None,
    minted: Mapping[str, Any] | None = None,
    expected_base_commit: str | None = None,
    receipts_root: Path | str | None = None,
    sidecar: tuple[str, str] | None = None,
    post: Any = None,
) -> PreparedBranch:
    """Put the task file and the YAML on ``repair/<task id>``, cut from ``base_branch``.

    The one materialisation both doors use. Raises
    :class:`forge.pipeline.repair_branch.RepairBranchError` with a plain
    sentence when the branch cannot be cut, written or committed; then
    nothing this call made is left behind.

    ``sidecar`` (open item 24, 2026-09-13) is ``(sidecar_url, repo key)`` for
    a repository that has a sandbox: the branch is then cut on the FACTORY'S
    clone, through the sidecar's git routes, because that is where the fix
    journey's worktree is cut from — a branch made with host git in the
    operator's checkout is invisible there and the conductor refuses the
    build. ``post`` is the sidecar transport, injectable for tests.

    ``expected_base_commit`` pins a retained candidate across admission and
    branch creation. A moved base ref or an existing repair branch with no
    candidate ancestry is refused before any files are written.
    """
    from forge.pipeline.repair_branch import (
        list_branch_files,
        materialise_repair_branch,
    )

    repo = Path(repo_path)
    files_on_base = list_branch_files(repo, base_branch, "tasks")
    folder = repair_task_folder(feature_id, files_on_base)
    facts = gather_repair_facts(
        repo_path=repo,
        task_id=task_id,
        feature_id=feature_id,
        name=name,
        base_branch=base_branch,
        source_build_id=source_build_id,
        minted=minted,
        receipts_root=receipts_root,
        files_on_base=files_on_base,
    )
    task_relpath = repair_task_relpath(folder, task_id)
    files = {
        task_relpath: render_repair_task_file(facts),
        fix_task_yaml_relpath(task_id): fix_task_yaml_text(
            task_id=task_id, parent_feature=feature_id, name=name
        ),
    }
    subject = source_build_id or task_id
    if sidecar is not None:
        from forge.pipeline.repair_branch import materialise_repair_branch_via_sidecar

        sidecar_url, repo_key = sidecar
        result = materialise_repair_branch_via_sidecar(
            sidecar_url,
            repo=repo_key,
            repo_root=repo,
            task_id=task_id,
            base_branch=base_branch,
            files=files,
            message=f"repair task for {subject}: {name}",
            expected_base_commit=expected_base_commit,
            post=post,
        )
    else:
        result = materialise_repair_branch(
            repo,
            task_id=task_id,
            base_branch=base_branch,
            files=files,
            message=f"repair task for {subject}: {name}",
            expected_base_commit=expected_base_commit,
        )
    logger.info(
        "fix admission: %s carries %s and %s at %s (%s)",
        result.branch,
        task_relpath,
        fix_task_yaml_relpath(task_id),
        result.commit[:12],
        "committed" if result.committed else "already there, nothing committed",
    )
    return PreparedBranch(
        branch=result.branch, task_file_path=task_relpath, commit=result.commit
    )


# ---------------------------------------------------------------------------
# The admission itself
# ---------------------------------------------------------------------------


async def admit_fix_build(
    *,
    config: Any,
    persistence: Any,
    task_id: str,
    fix_task_yaml: Path | str,
    repo_path: Path | str,
    correlation_id: str,
    publish: Callable[[str, bytes], Any],
    branch: str = "main",
    profile: str | None = None,
    uncapped_acknowledged: bool = False,
    max_turns: int | None = None,
    sdk_timeout_seconds: int | None = None,
    originating_user: str | None = None,
    triggered_by: str = "cli",
    originating_adapter: str | None = None,
    parent_request_id: str | None = None,
    source_build_id: str | None = None,
    parent_feature: str | None = None,
    prepare_branch: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FixAdmission:
    """Open one fix journey: check it, write its row, tell the pipeline.

    In order, and every step before any side effect of the next:

    1. **THE CAP LAW** — an uncapped or unresolvable budget profile does not
       open a fix journey. Read from :mod:`forge.config.conductor`, the one
       statement of the rule both this and the daemon's router read.
    2. **The subject** must be a TASK identifier of the shape the wire
       allows.
    3. **The parent feature** comes from the fix-task YAML's
       ``parent_feature`` and is validated as a feature identifier.
    4. **The repository** must be one ``queue.repo_allowlist`` allows (an
       empty allowlist, the default, allows everything).
    5. **The row is written first, then published** — the write-then-publish
       discipline the whole pipeline keeps. A publish that fails raises
       :class:`FixPublishFailed` and the row deliberately stays.

    Args:
        publish: ``(subject, body) -> None`` or an awaitable of the same. The
            transports differ — the CLI opens a one-shot connection, the
            daemon has a live client — so the caller brings its own.
        source_build_id: The FAILED build this journey repairs, recorded for
            the caller's own audit trail. The journey itself finds the pack
            through the correlation id (``fix-<source build id>``).
        parent_feature: The parent feature when the caller already knows it
            (the queue's path reads it off the source build's row and writes
            the YAML from it, on the branch); the YAML is then not read.
        prepare_branch: Called after every check and before the row is
            written; returns a :class:`PreparedBranch` (or an awaitable of
            one) naming the branch the build rides — the repair branch
            carrying the task file. A
            :class:`forge.pipeline.repair_branch.RepairBranchError` from it
            refuses the admission with reason ``repair-task``. Left unset,
            the build rides ``branch`` as given.

    Returns:
        The :class:`FixAdmission` describing what was opened.

    Raises:
        FixAdmissionRefused: at any of steps 1 to 4, having written nothing.
        FixPublishFailed: when the row landed and the publish did not.
    """
    from forge.config.conductor import mode_c_cap_refusal_from_config
    from forge.lifecycle.identifiers import (
        InvalidIdentifierError,
        validate_feature_id,
    )
    from forge.lifecycle.modes import BuildMode
    from forge.lifecycle.persistence import DuplicateBuildError

    # 1. THE CAP LAW, before every side effect.
    cap_refusal = mode_c_cap_refusal_from_config(
        config, profile, uncapped_acknowledged=uncapped_acknowledged
    )
    if cap_refusal is not None:
        raise FixAdmissionRefused(
            cap_refusal.message, reason="cap", permanent=True
        )

    # 2. The subject.
    if not TASK_ID_REGEX.match(task_id):
        raise FixAdmissionRefused(
            "Mode C requires positional argument to match "
            f"{TASK_ID_REGEX.pattern}; got {task_id!r}",
            reason="task-id",
            permanent=True,
        )

    # 3. The parent feature, from the fix-task YAML unless the caller knows it.
    raw_parent = (
        parent_feature if parent_feature and parent_feature.strip()
        else read_parent_feature(fix_task_yaml)
    )
    try:
        feature_id = validate_feature_id(raw_parent)
    except InvalidIdentifierError as exc:
        raise FixAdmissionRefused(
            f"Invalid parent_feature in fix-task YAML ({exc.reason}): "
            f"{exc.value!r}",
            reason="parent-feature",
            permanent=True,
        ) from exc

    # 4. The repository.
    repo = Path(repo_path)
    if not _repo_allowed(repo, config):
        raise FixAdmissionRefused(
            f"Repository {str(repo)!r} is not in queue.repo_allowlist; "
            "refusing to enqueue (Group C path-allowlist refused).",
            reason="repo-not-allowed",
            permanent=True,
        )

    # 4b. The branch the build rides. The repair's own, with the task file
    #     the review leg will look for committed on it (Part L, rule 48) —
    #     before the row, so a repair whose task file cannot be written never
    #     gets a build row.
    prepared: PreparedBranch | None = None
    if prepare_branch is not None:
        from forge.pipeline.repair_branch import RepairBranchError

        try:
            outcome = prepare_branch()
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except RepairBranchError as exc:
            raise FixAdmissionRefused(
                "Nothing was queued: the repair's task file could not be put on "
                f"a repair branch ({exc}), and the review leg cannot find a "
                "repair task without its file.",
                reason=REPAIR_TASK_REASON,
                permanent=True,
            ) from exc
        if not isinstance(outcome, PreparedBranch):
            raise TypeError(
                "prepare_branch must return a PreparedBranch, "
                f"got {type(outcome).__name__}"
            )
        prepared = outcome
        branch = prepared.branch

    # 5. The payload, the row, then the publish.
    from nats_core.envelope import EventType, MessageEnvelope
    from nats_core.events import BuildQueuedPayload

    now = (clock or (lambda: datetime.now(UTC)))()
    queue_config = getattr(config, "queue", None)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo=repo_slug(repo),
        branch=branch,
        feature_yaml_path=str(Path(fix_task_yaml)),
        max_turns=(
            max_turns
            if max_turns is not None
            else getattr(queue_config, "default_max_turns", 5)
        ),
        sdk_timeout_seconds=(
            sdk_timeout_seconds
            if sdk_timeout_seconds is not None
            else getattr(queue_config, "default_sdk_timeout_seconds", 1800)
        ),
        triggered_by=triggered_by,
        originating_adapter=originating_adapter,
        originating_user=originating_user,
        correlation_id=correlation_id,
        parent_request_id=parent_request_id,
        requested_at=now,
        queued_at=now,
        mode=BuildMode.MODE_C.value,
        task_id=task_id,
    )

    if persistence.exists_active_build(feature_id):
        raise FixAdmissionRefused(
            f"duplicate build refused: an active build for {feature_id} "
            "is already in flight (Group C).",
            reason="duplicate",
            permanent=False,
        )

    try:
        build_id = persistence.queue_build(
            payload, mode=BuildMode.MODE_C, profile=profile
        )
    except DuplicateBuildError as exc:
        raise FixAdmissionRefused(
            f"duplicate build refused: {exc} (Group B).",
            reason="duplicate",
            permanent=False,
        ) from exc

    admission = FixAdmission(
        build_id=str(build_id),
        task_id=task_id,
        feature_id=feature_id,
        correlation_id=correlation_id,
        repo=payload.repo,
        fix_task_path=str(Path(fix_task_yaml)),
        source_build_id=source_build_id,
        branch=branch,
        task_file_path=prepared.task_file_path if prepared else None,
        repair_commit=prepared.commit if prepared else None,
    )

    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    subject = f"{BUILD_QUEUED_SUBJECT_PREFIX}.{feature_id}"
    try:
        result = publish(subject, envelope.model_dump_json().encode("utf-8"))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — translated, never swallowed
        raise FixPublishFailed(
            f"Queued {feature_id} (build pending) but pipeline NOT NOTIFIED — "
            f"publish failed (messaging-layer): {exc}",
            admission=admission,
        ) from exc

    logger.info(
        "fix admission: opened %s for %s (task %s, repo %s, branch %s, "
        "correlation id %s, profile %s)",
        admission.build_id,
        feature_id,
        task_id,
        admission.repo,
        branch,
        correlation_id,
        profile,
    )
    return admission


async def admit_fix_row(
    *,
    config: Any,
    persistence: Any,
    store: Any,
    queue_id: int,
    correlation_id: str,
    sentence: str,
    target_repo: str | None,
    publish: Callable[[str, bytes], Any],
    originating_user: str | None = None,
    source_build_id: str | None = None,
    branch: str = "main",
    profile: str | None = None,
    actor_identity: str = "forge-work-queue",
    receipts_root: Path | str | None = None,
    clock: Callable[[], datetime] | None = None,
    sidecar_post: Any = None,
) -> FixAdmission:
    """Turn one ``kind='fix'`` queue row into an open fix journey.

    Mints the task id (or reuses the one this row already has), puts the
    task file and the fix-task YAML on ``repair/<task id>`` cut from
    ``branch`` (Part L, rule 48), and hands the rest to
    :func:`admit_fix_build`, which queues the build on that branch. Records
    what it opened against the queue row so the row and its build can be
    read back as one thing. The shared checkout's working tree and index are
    not touched.

    Args:
        branch: The build's target branch — what the repair branch is cut
            from and what the repair will merge into.
        receipts_root: Where the merge report is read from (tests point it
            at a temporary directory); the estate's own root when unset.

    Raises:
        FixAdmissionRefused: when the source build, the repository or the
            fix-task spec cannot be resolved, or the shared checks refuse.
        FixPublishFailed: when the row landed and the publish did not.
    """
    from forge.pipeline.fix_row_producer import source_build_id_from_correlation_id
    from forge.planning.target_repos import (
        refusal_message,
        resolve_target_repo,
    )

    source = source_build_id or source_build_id_from_correlation_id(correlation_id)
    if not source:
        raise FixAdmissionRefused(
            f"#{queue_id} does not name the build it is repairing, so there "
            "is no failed build to review.",
            reason="no-source-build",
            permanent=True,
        )

    row = persistence.get_build_row(source)
    if row is None:
        raise FixAdmissionRefused(
            f"#{queue_id} is a repair of {source}, and there is no such "
            "build on record any more.",
            reason="no-source-build",
            permanent=True,
        )
    parent_feature = str(getattr(row, "feature_id", "") or "")
    if not parent_feature:
        raise FixAdmissionRefused(
            f"the build {source} names no feature, so there is nothing for a "
            "repair to point at.",
            reason="parent-feature",
            permanent=True,
        )

    name = str(target_repo or getattr(row, "repo", "") or "")
    paths = dict(getattr(config.planning, "target_repo_paths", {}) or {})
    resolution = resolve_target_repo(name, paths)
    if resolution.name is None:
        raise FixAdmissionRefused(
            refusal_message(name, resolution, paths),
            reason="repo-unknown",
            permanent=True,
        )
    repo_path = Path(str(paths[resolution.name])).expanduser()

    task_id = _task_id_already_on_row(store, queue_id) or mint_fix_task_id(
        parent_feature, existing=existing_fix_task_ids(repo_path)
    )
    name = _one_line(sentence)
    fix_task_path = features_dir(repo_path) / f"{task_id}.yaml"
    minted = _minted_details(store, queue_id)

    # Where the repair is cut from, and where (open item 24, 2026-09-13). A
    # build refused at the candidate gate was never merged, so its code lives
    # only on its own autobuild branch — a repair cut from main would repair a
    # tree without the code. A failed build may also have a retained candidate;
    # its failure pack must identify that exact ref and commit before it can be
    # used. And a repository with a sandbox keeps the clone the build runs on
    # INSIDE it, so the branch is cut there, through the sidecar, or the
    # conductor cannot find it.
    sidecar = _sidecar_for(config, resolution.name)
    repair_base = await resolve_repair_base(
        minted=minted,
        branch=branch,
        parent_feature=parent_feature,
        source_build_id=source,
        source_build=row,
        repo_path=repo_path,
        receipts_root=receipts_root,
        sidecar=sidecar,
        sidecar_post=sidecar_post,
    )
    base_branch = repair_base.branch
    if base_branch != branch or sidecar is not None:
        logger.info(
            "fix admission: %s's repair branch is cut from %s%s",
            task_id,
            base_branch,
            f" in the sandbox behind {sidecar[0]}" if sidecar else "",
        )

    async def _prepare() -> PreparedBranch:
        import asyncio

        prepared = await asyncio.to_thread(
            materialise_repair_task,
            repo_path=repo_path,
            task_id=task_id,
            feature_id=parent_feature,
            name=name,
            base_branch=base_branch,
            expected_base_commit=repair_base.expected_commit,
            source_build_id=source,
            minted=minted,
            receipts_root=receipts_root,
            sidecar=sidecar,
            post=sidecar_post,
        )
        _record_repair_branch(
            store,
            queue_id=queue_id,
            task_id=task_id,
            prepared=prepared,
            actor_identity=actor_identity,
        )
        return prepared

    admission = await admit_fix_build(
        config=config,
        persistence=persistence,
        task_id=task_id,
        fix_task_yaml=fix_task_path,
        repo_path=repo_path,
        correlation_id=correlation_id,
        publish=publish,
        branch=branch,
        profile=profile,
        parent_feature=parent_feature,
        prepare_branch=_prepare,
        max_turns=getattr(config.queue, "default_max_turns", None),
        sdk_timeout_seconds=getattr(config.queue, "default_sdk_timeout_seconds", None),
        originating_user=originating_user,
        triggered_by="forge-internal",
        source_build_id=source,
        clock=clock,
    )

    _record_admitted_build(
        store,
        queue_id=queue_id,
        admission=admission,
        actor_identity=actor_identity,
    )
    return admission


async def republish_build_queued(
    build: Any,
    *,
    publish: Callable[[str, bytes], Any],
) -> str:
    """Say the queued event again for a build row that was never announced.

    The write comes before the publish, deliberately, so a publish that fails
    leaves a real build row that nothing was ever told about. Everything the
    event says is on that row, so this rebuilds the SAME event from the row —
    same feature, same repository, same fix-task file, same correlation id,
    same task, same queued moment — and says it on the same subject. Saying it
    twice is safe: the build row it names already exists and is keyed by
    ``(feature_id, correlation_id)``, so a second hearing finds the same
    build rather than starting another one.

    Args:
        build: The ``builds`` row, however the caller reads rows — a
            ``sqlite3.Row``, a mapping, or the typed ``BuildRow``.
        publish: ``(subject, body) -> None`` or an awaitable of the same, the
            caller's own transport.

    Returns:
        The subject it published on.
    """
    from nats_core.envelope import EventType, MessageEnvelope
    from nats_core.events import BuildQueuedPayload

    feature_id = str(_row_value(build, "feature_id", ""))
    correlation_id = str(_row_value(build, "correlation_id", ""))
    queued_at = _as_datetime(_row_value(build, "queued_at"))
    mode = _row_value(build, "mode", "mode-c")
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo=str(_row_value(build, "repo", "")),
        branch=str(_row_value(build, "branch", "main")),
        feature_yaml_path=str(_row_value(build, "feature_yaml_path", "")),
        max_turns=int(_row_value(build, "max_turns", 5)),
        sdk_timeout_seconds=int(_row_value(build, "sdk_timeout_seconds", 1800)),
        triggered_by=str(_row_value(build, "triggered_by", "forge-internal")),
        originating_adapter=_row_value(build, "originating_adapter"),
        originating_user=_row_value(build, "originating_user"),
        correlation_id=correlation_id,
        parent_request_id=_row_value(build, "parent_request_id"),
        requested_at=queued_at,
        queued_at=queued_at,
        mode=str(getattr(mode, "value", mode)),
        task_id=_row_value(build, "task_id"),
    )
    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    subject = f"{BUILD_QUEUED_SUBJECT_PREFIX}.{feature_id}"
    result = publish(subject, envelope.model_dump_json().encode("utf-8"))
    if inspect.isawaitable(result):
        await result
    logger.info(
        "fix admission: said the queued event again for %s (%s, correlation "
        "id %s) — it was written and never announced",
        _row_value(build, "build_id", feature_id),
        feature_id,
        correlation_id,
    )
    return subject


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """One field of a build row, whether it reads like a mapping or an object."""
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, name, None)
    return default if value is None else value


def _as_datetime(value: Any) -> datetime:
    """A moment from a build row: a datetime as it is, a string parsed, else now."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def _record_admitted_build(
    store: Any,
    *,
    queue_id: int,
    admission: FixAdmission,
    actor_identity: str,
) -> None:
    """Write down which build this row opened; never stop the journey for it."""
    try:
        store.record_event(
            queue_id=queue_id,
            action=ADMITTED_BUILD_ACTION,
            actor_identity=actor_identity,
            details={
                "build_id": admission.build_id,
                "task_id": admission.task_id,
                "feature_id": admission.feature_id,
                "source_build_id": admission.source_build_id,
                "fix_task_path": admission.fix_task_path,
                "branch": admission.branch,
                "task_file_path": admission.task_file_path,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a note never costs a journey
        logger.warning(
            "fix admission: could not record the build against #%d (%s: %s)",
            queue_id,
            type(exc).__name__,
            exc,
        )


def _record_repair_branch(
    store: Any,
    *,
    queue_id: int,
    task_id: str,
    prepared: PreparedBranch,
    actor_identity: str,
) -> None:
    """Write down which branch this row's repair rides; never stop the journey for it."""
    try:
        store.record_event(
            queue_id=queue_id,
            action=REPAIR_BRANCH_ACTION,
            actor_identity=actor_identity,
            details={
                "task_id": task_id,
                "branch": prepared.branch,
                "task_file_path": prepared.task_file_path,
                "commit": prepared.commit,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a note never costs a journey
        logger.warning(
            "fix admission: could not record the repair branch against #%d (%s: %s)",
            queue_id,
            type(exc).__name__,
            exc,
        )


def _row_events(store: Any, queue_id: int) -> list[tuple[str, dict[str, Any]]]:
    """``(action, details)`` for every event on the row, oldest first; empty on any trouble."""
    try:
        rows = list(store.list_events(queue_id))
    except Exception:  # noqa: BLE001 — a store that cannot be read has no notes
        return []
    events: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        action = str(_row_value(row, "action", "") or "")
        raw = _row_value(row, "details_json") or _row_value(row, "details")
        details: Any = raw
        if isinstance(raw, (str, bytes)):
            try:
                details = json.loads(raw)
            except ValueError:
                details = {}
        events.append((action, details if isinstance(details, dict) else {}))
    return events


def _task_id_already_on_row(store: Any, queue_id: int) -> str | None:
    """The task id this row's repair already has — from an earlier admission
    or an earlier materialised branch — so a second admission reuses it."""
    found: str | None = None
    for action, details in _row_events(store, queue_id):
        if action in (ADMITTED_BUILD_ACTION, REPAIR_BRANCH_ACTION):
            candidate = details.get("task_id")
            if isinstance(candidate, str) and TASK_ID_REGEX.match(candidate):
                found = candidate
    return found


def _minted_details(store: Any, queue_id: int) -> dict[str, Any]:
    """The producer's filing note on the row (source, failure pack), if any."""
    for action, details in _row_events(store, queue_id):
        if action == "minted":
            return details
    return {}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _git_text(repo: Path, *args: str) -> str | None:
    """One bounded, read-only Git query, or ``None`` when Git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _git_common_dir(repo: Path) -> Path | None:
    value = _git_text(repo, "rev-parse", "--git-common-dir")
    if value is None:
        return None
    path = Path(value)
    return (path if path.is_absolute() else repo / path).resolve()


def _repair_base_refusal(source_build_id: str, detail: str) -> FixAdmissionRefused:
    return FixAdmissionRefused(
        f"Nothing was queued: {source_build_id} failed after coding began, but "
        f"its retained candidate could not be verified ({detail}). Refusing to "
        "fall back to main because that would discard the failed build's work.",
        reason=REPAIR_BASE_REASON,
        permanent=True,
    )


async def _retained_candidate_base(
    *,
    source_build_id: str,
    source_build: Any,
    parent_feature: str,
    repo_path: Path,
    receipts_root: Path | str | None,
    sidecar: tuple[str, str] | None,
    sidecar_post: Any,
    fallback_branch: str,
) -> RepairBase:
    """Resolve a build-failed repair to its exact retained candidate.

    A failure before the GuardKit subprocess started provably has no candidate,
    so it keeps the caller's branch. Once coding began, every identity must
    agree: manifest/build/feature/source branch, retained-worktree evidence,
    candidate branch, committed feature contract and commit. A local target
    also proves the outer and candidate worktrees share the configured
    repository; a sandbox target reads its branch and contract through the
    sidecar because its worktree path is not mounted in the coordinator.
    The materialiser pins the commit again at the final local/sidecar ref read,
    closing the race between this inspection and the branch cut.
    """
    from forge.pipeline.fix_journey_receipts import read_failure_pack

    pack = read_failure_pack(source_build_id, receipts_root=receipts_root)
    if pack is None or not pack.has_manifest:
        raise _repair_base_refusal(
            source_build_id, "the failure pack has no readable manifest"
        )
    recorded_build = str(pack.raw.get("build_id") or "")
    if recorded_build != source_build_id:
        raise _repair_base_refusal(
            source_build_id,
            f"the manifest names build {recorded_build!r}",
        )
    if pack.feature_id != parent_feature:
        raise _repair_base_refusal(
            source_build_id,
            f"the manifest names feature {pack.feature_id!r}, not {parent_feature!r}",
        )
    row_correlation = str(getattr(source_build, "correlation_id", "") or "")
    if row_correlation and pack.correlation_id != row_correlation:
        raise _repair_base_refusal(
            source_build_id,
            "the manifest correlation id does not match the source build",
        )
    row_branch = str(getattr(source_build, "branch", "") or "")
    if row_branch and pack.branch != row_branch:
        raise _repair_base_refusal(
            source_build_id,
            f"the manifest branch {pack.branch!r} does not match the source "
            f"build branch {row_branch!r}",
        )

    evidence = pack.evidence
    subprocess_ran = (
        evidence.get("subprocess_ran") if isinstance(evidence, Mapping) else None
    )
    if subprocess_ran is False:
        return RepairBase(branch=fallback_branch)
    if subprocess_ran is not True:
        raise _repair_base_refusal(
            source_build_id,
            "the manifest does not say whether the coding subprocess ran",
        )
    if evidence.get("worktree_kept") is not True:
        raise _repair_base_refusal(
            source_build_id,
            "the manifest does not verify that the failed worktree was retained",
        )
    if not pack.worktree_path:
        raise _repair_base_refusal(
            source_build_id, "the manifest records no retained worktree path"
        )

    outer_record = Path(pack.worktree_path)
    if outer_record.name != source_build_id:
        raise _repair_base_refusal(
            source_build_id,
            f"the recorded worktree {pack.worktree_path} does not belong to this build",
        )
    candidate_branch = f"autobuild/{parent_feature}"
    feature_path = "/".join((*FEATURES_DIR_PARTS, f"{parent_feature}.yaml"))

    if sidecar is not None:
        from forge.planning.sidecar_git_runner import SidecarGitRunner, _urllib_post

        sidecar_url, repo_key = sidecar
        runner = SidecarGitRunner(
            sidecar_url,
            repo=repo_key,
            post=sidecar_post or _urllib_post,
        )
        candidate_commit = await runner.rev_parse(str(repo_path), candidate_branch)
        if candidate_commit is None:
            raise _repair_base_refusal(
                source_build_id,
                f"the sandbox cannot resolve the retained ref {candidate_branch!r}",
            )
        feature_text = await runner.read_file_from_branch(
            repo_path=str(repo_path),
            branch=candidate_branch,
            file_path=feature_path,
        )
    else:
        outer = outer_record.resolve()
        if not outer.is_dir():
            raise _repair_base_refusal(
                source_build_id, f"the recorded worktree {outer} is missing"
            )
        candidate = outer.joinpath(".guardkit", "worktrees", parent_feature)
        if not candidate.is_dir():
            raise _repair_base_refusal(
                source_build_id,
                f"the retained candidate worktree {candidate} is missing",
            )
        observed_branch = _git_text(
            candidate, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if observed_branch != candidate_branch:
            raise _repair_base_refusal(
                source_build_id,
                (
                    f"the retained worktree is on {observed_branch!r}, not "
                    f"{candidate_branch!r}"
                ),
            )
        candidate_commit = _git_text(candidate, "rev-parse", "--verify", "HEAD")
        if candidate_commit is None:
            raise _repair_base_refusal(
                source_build_id, "Git cannot resolve the retained candidate commit"
            )
        outer_common = _git_common_dir(outer)
        candidate_common = _git_common_dir(candidate)
        if outer_common is None or candidate_common != outer_common:
            raise _repair_base_refusal(
                source_build_id,
                "the retained candidate is not a worktree of the recorded failed build",
            )
        if _git_common_dir(repo_path) != outer_common:
            raise _repair_base_refusal(
                source_build_id,
                "the retained candidate belongs to a different repository",
            )
        feature_text = _git_text(candidate, "show", f"HEAD:{feature_path}")
    if feature_text is None:
        raise _repair_base_refusal(
            source_build_id,
            (
                "the retained candidate does not carry its original contract "
                f"{feature_path}"
            ),
        )
    try:
        import yaml

        feature = yaml.safe_load(feature_text)
    except yaml.YAMLError as exc:
        raise _repair_base_refusal(
            source_build_id, f"the original feature contract is malformed ({exc})"
        ) from exc
    if (
        not isinstance(feature, Mapping)
        or str(feature.get("id") or "") != parent_feature
    ):
        raise _repair_base_refusal(
            source_build_id,
            "the retained candidate's original feature contract has the wrong id",
        )
    return RepairBase(branch=candidate_branch, expected_commit=candidate_commit)


async def resolve_repair_base(
    *,
    minted: Mapping[str, Any] | None,
    branch: str,
    parent_feature: str,
    source_build_id: str,
    source_build: Any,
    repo_path: Path,
    receipts_root: Path | str | None,
    sidecar: tuple[str, str] | None,
    sidecar_post: Any = None,
) -> RepairBase:
    """Choose the repair base, preserving a verified build-failed candidate."""
    from forge.pipeline.fix_row_producer import SOURCE_BUILD_FAILED

    chosen = choose_repair_base(minted, branch, parent_feature)
    source = str((minted or {}).get("source") or "")
    if source != SOURCE_BUILD_FAILED:
        return RepairBase(branch=chosen)
    return await _retained_candidate_base(
        source_build_id=source_build_id,
        source_build=source_build,
        parent_feature=parent_feature,
        repo_path=repo_path,
        receipts_root=receipts_root,
        sidecar=sidecar,
        sidecar_post=sidecar_post,
        fallback_branch=branch,
    )


def choose_repair_base(
    minted: Mapping[str, Any] | None, branch: str, parent_feature: str
) -> str:
    """The branch a repair is cut from.

    A build whose candidate was refused before the merge (the producer's
    ``source`` is ``candidate-refused``) was never merged: its code exists
    only on ``autobuild/<feature>``, and that is what needs repairing. Every
    other repair — a merge that went red live, a queued repair naming its own
    branch — is cut from ``branch`` exactly as before.
    """
    from forge.pipeline.fix_row_producer import SOURCE_CANDIDATE_REFUSED

    source = str((minted or {}).get("source") or "")
    if source == SOURCE_CANDIDATE_REFUSED and parent_feature:
        return f"autobuild/{parent_feature}"
    return branch


def _sidecar_for(config: Any, repo_key: str | None) -> tuple[str, str] | None:
    """``(sidecar_url, repo key)`` when the repository has a sandbox, else None.

    The same lookup the conductor's worktree writer makes
    (``planning.sandboxes``); kept here so the pipeline does not import the
    CLI. Never raises: a config with no sandboxes has none.
    """
    if not repo_key:
        return None
    sandboxes = getattr(getattr(config, "planning", None), "sandboxes", None) or {}
    try:
        entry = sandboxes.get(str(repo_key))
    except AttributeError:  # pragma: no cover — a mapping is what the model gives
        return None
    if entry is None:
        return None
    url = getattr(entry, "sidecar_url", None)
    if url is None and isinstance(entry, Mapping):
        url = entry.get("sidecar_url")
    return (str(url), str(repo_key)) if url else None


def _sanitise_segment(segment: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with ``_``."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in segment)


def repo_slug(repo: Path | str) -> str:
    """An ``org/name`` slug for a checkout path, as the wire requires.

    The last two components of the resolved path, with anything outside
    ``[A-Za-z0-9._-]`` replaced — the same bridge ``forge queue`` has always
    used between a filesystem path and the wire's GitHub-shaped slug. A
    single-component path becomes ``local/<name>``.
    """
    resolved = Path(repo).expanduser().resolve()
    name = _sanitise_segment(resolved.name) or "repo"
    parent = resolved.parent.name
    org = _sanitise_segment(parent) if parent else "local"
    return f"{org or 'local'}/{name}"


def path_in_allowlist(repo: Path | str, allowlist: Iterable[Path | str]) -> bool:
    """Whether ``repo`` is a checkout the allowlist allows.

    Compared against the RESOLVED absolute path, and a nested checkout under
    an allowed root passes. An empty allowlist — the schema default — means
    no restriction, so everything passes.
    """
    entries = list(allowlist)
    if not entries:
        return True
    repo_resolved = Path(repo).expanduser().resolve()
    for entry in entries:
        try:
            entry_resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            # Defensive — a pathological symlink loop in forge.yaml should
            # not crash an admission. Skip the bad entry.
            logger.warning("repo_allowlist entry %r could not be resolved", entry)
            continue
        if repo_resolved == entry_resolved:
            return True
        try:
            repo_resolved.relative_to(entry_resolved)
        except ValueError:
            continue
        return True
    return False


def _repo_allowed(repo: Path, config: Any) -> bool:
    """Whether ``queue.repo_allowlist`` allows this checkout (empty = all)."""
    allowlist = list(getattr(getattr(config, "queue", None), "repo_allowlist", []) or [])
    return path_in_allowlist(repo, allowlist)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


__all__ = [
    "ADMITTED_BUILD_ACTION",
    "DUPLICATE_REASON",
    "GATE_EVIDENCE_DIR_PARTS",
    "GATE_EVIDENCE_NAME",
    "MERGE_RECEIPTS_PREFIX",
    "MERGE_REPORT_NAME",
    "REPAIR_BRANCH_ACTION",
    "REPAIR_TASK_COMPLEXITY",
    "REPAIR_TASK_REASON",
    "REPUBLISHED_ACTION",
    "TRANSIENT_REFUSAL_REASONS",
    "BUILD_QUEUED_SUBJECT_PREFIX",
    "FEATURES_DIR_PARTS",
    "FixAdmission",
    "FailedCheck",
    "FixAdmissionRefused",
    "FixPublishFailed",
    "MAX_TASK_SUFFIX_CHARS",
    "PreparedBranch",
    "RepairTaskFacts",
    "SOURCE_ID",
    "TASK_ID_REGEX",
    "admit_fix_build",
    "admit_fix_row",
    "existing_fix_task_ids",
    "features_dir",
    "fix_task_yaml_relpath",
    "fix_task_yaml_text",
    "gather_repair_facts",
    "materialise_repair_task",
    "mint_fix_task_id",
    "path_in_allowlist",
    "read_fix_task_name",
    "read_parent_feature",
    "render_repair_task_file",
    "repair_task_folder",
    "repair_task_relpath",
    "repo_slug",
    "republish_build_queued",
    "write_fix_task_yaml",
]
