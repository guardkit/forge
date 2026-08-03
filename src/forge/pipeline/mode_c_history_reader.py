"""The fix journey's history projection — durable rows → planner domain.

The conductor's revival, Stage 1b (design pass §a.3 row 3, risk §h.3). This
is the least-proven seam in the whole revival, and the only one that had no
code at all: :class:`forge.pipeline.mode_c_planner.ModeCCyclePlanner` reads
:class:`~forge.pipeline.mode_c_planner.StageEntry` rows — stage class,
status, **typed fix-task list**, ``hard_stop`` — and production stores
``stage_log`` rows shaped by SQLite concerns. Somebody has to translate.
Get the translation wrong and the planner fans ``/task-work`` out at the
wrong things, which is the one failure mode a fix journey must not have.

The two vocabularies
--------------------

``stage_log.status`` is constrained by the schema to four values
(``PASSED`` / ``FAILED`` / ``GATED`` / ``SKIPPED``); the gate's verdict
rides alongside on ``gate_mode``; and a *dispatch-attempt* row is written
as ``PASSED`` with ``details["lifecycle_state"] = "running"`` because the
schema has no RUNNING status (see
:mod:`forge.cli._serve_deps_stage_log`). The planner's vocabulary is
``approved`` / ``failed`` / ``rejected`` / ``cancelled`` plus the
non-terminal ``pending`` / ``running``. The full mapping is
:data:`_STATUS_MAP` and the gate table below it; the two rules worth
stating in prose are:

* **A dispatch-attempt row is ``running``, never ``approved``.** The
  ``PASSED`` on that row means "the dispatch was recorded", not "the
  stage passed". Reading it as approved would let the planner schedule a
  follow-up review over a ``/task-work`` still in flight — precisely the
  §h.1 defect Stage 1a just closed, re-introduced one layer down.
* **An unknown gate verdict is ``pending``, never ``approved``.**
  Waiting on an unreadable gate costs a tick. Guessing costs a wrong
  fan-out.

Garbage is loud (risk §h.3)
---------------------------

The typed fix-task list is recovered from the review row's recorded
output under a strict contract (:data:`FIX_TASKS_DETAILS_KEY`), and every
way of getting it wrong is treated the same way: **the projection appends
a hard-stopped review entry to the end of the history and logs at ERROR.**
The planner reads that as terminal FAILED and the terminal handler records
``mode-c-task-review-hard-stop``. There is deliberately no path from a
malformed recorded output to an empty fix-task list — an empty list is a
*clean review*, a real and meaningful outcome, and a build that ends
"clean" because its review output was unparseable is a silent lie about
work that was never done.

Malformed means, exactly:

* an approved ``task-review`` row with no ``fix_tasks`` key at all
  (an approved review must state its finding, even when the finding is
  "nothing");
* ``fix_tasks`` that is not a list/tuple — including a bare string, which
  would otherwise iterate into one fix task per character;
* any element that is not a non-empty string;
* duplicate identifiers, which make the planner's per-fix-task walk
  ambiguous (it matches dispatched work by identity);
* a ``task-work`` row with no readable :data:`FIX_TASK_ID_DETAILS_KEY` —
  unattributable work, which the walk would read as "never dispatched"
  and dispatch a second time.

A non-approved review row needs no ``fix_tasks`` key: the planner never
reads the list on those paths (it terminates or waits first).

The ``has_commits`` flag
------------------------

The :class:`~forge.pipeline.supervisor.ModeCHistoryReader` Protocol carries
a synchronous ``has_commits``; the authoritative commit check
(:mod:`forge.pipeline.mode_c_commit_probe`) is async and lives on the
terminal handler. So this reader answers the *conservative* ``False``
unless an operator injects a sync reader, and the real answer is taken by
the handler, which owns the probe — exactly the precedence the supervisor
already documents ("prefer the handler's view since it has access to the
commit probe", ``_mode_c_resolve_terminal``). Nothing is lost: a clean
follow-up review with ``has_commits=False`` routes to the terminal
handler, which probes and returns PR_REVIEW when commits exist.

References:
    - design pass §a.3 / §h.3 (`supervisor-revival-design-pass-2026-07-31`).
    - :mod:`forge.pipeline.mode_c_planner` — the ``StageEntry`` contract
      this module produces (documented at ``mode_c_planner.py`` §StageEntry).
    - :mod:`forge.lifecycle.persistence` — ``StageLogEntry`` / the
      ``stage_log`` schema this module consumes.
    - :mod:`forge.cli._serve_deps_stage_log` — the dispatch-attempt row
      convention (``lifecycle_state``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from forge.pipeline.finding_anchors import (
    FINDING_ANCHORS_DETAILS_KEY,
    derive_finding_anchors,
)
from forge.pipeline.mode_c_planner import StageEntry
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

__all__ = [
    "FINDING_ANCHORS_DETAILS_KEY",
    "FIX_TASKS_DETAILS_KEY",
    "FIX_TASK_ID_DETAILS_KEY",
    "LIFECYCLE_STATE_DETAILS_KEY",
    "MODE_C_STAGE_LABELS",
    "RATIONALE_DETAILS_KEY",
    "ProjectionError",
    "SqliteModeCHistoryReader",
    "derive_finding_anchors",
    "project_mode_c_history",
]


#: ``details_json`` key carrying an approved ``task-review``'s typed
#: fix-task list. A JSON array of unique, non-empty identifier strings.
#: An empty array is the legitimate "clean review" answer; an absent key on
#: an approved review is not.
FIX_TASKS_DETAILS_KEY: str = "fix_tasks"

#: ``details_json`` key carrying the fix-task identifier a ``task-work``
#: row was dispatched against. The audit anchor that lets the planner tell
#: a dispatched fix task from an outstanding one.
FIX_TASK_ID_DETAILS_KEY: str = "fix_task_id"

#: ``FINDING_ANCHORS_DETAILS_KEY`` and :func:`derive_finding_anchors` are
#: re-exported from :mod:`forge.pipeline.finding_anchors` (LI stage-2 §5).
#: They live there because the conductor's turn loop needs the same
#: vocabulary and must not take an import edge onto the planner/persistence
#: side; they are re-exported HERE so the fix journey's writer keeps
#: importing every ``details_json`` key it writes from one place, and a
#: rename cannot leave writer and reader disagreeing in silence.

#: ``details_json`` key marking a dispatch-attempt row (written by
#: :mod:`forge.cli._serve_deps_stage_log`, whose value is ``"running"``).
#: Its presence overrides the ``status`` column — see the module docstring.
LIFECYCLE_STATE_DETAILS_KEY: str = "lifecycle_state"

#: The ``lifecycle_state`` value meaning "dispatched, still in flight".
_LIFECYCLE_RUNNING: str = "running"

#: ``stage_label`` values this projection consumes. Every other label in
#: the build's ``stage_log`` (autobuild rows, planning rows, skip rows) is
#: not part of the fix journey's cycle and is skipped.
MODE_C_STAGE_LABELS: Mapping[str, StageClass] = {
    StageClass.TASK_REVIEW.value: StageClass.TASK_REVIEW,
    StageClass.TASK_WORK.value: StageClass.TASK_WORK,
}

#: Planner-domain statuses. Mirrors the vocabulary documented on
#: :class:`forge.pipeline.mode_c_planner.StageEntry`.
_APPROVED: str = "approved"
_FAILED: str = "failed"
_REJECTED: str = "rejected"
_CANCELLED: str = "cancelled"
_PENDING: str = "pending"
_RUNNING: str = "running"

#: ``stage_log.status`` → planner status, for rows that are not
#: dispatch-attempt rows and not gate rows.
_STATUS_MAP: Mapping[str, str] = {
    "PASSED": _APPROVED,
    "FAILED": _FAILED,
    "SKIPPED": _CANCELLED,
}

#: ``stage_log.gate_mode`` → planner status for ``GATED`` rows. A
#: HARD_STOP additionally sets ``hard_stop=True`` (see
#: :func:`_project_status`). ``FLAG_FOR_REVIEW`` and
#: ``MANDATORY_HUMAN_APPROVAL`` are open gates — the stage has not
#: resolved, so the planner waits.
_GATE_MODE_MAP: Mapping[str, str] = {
    "AUTO_APPROVE": _APPROVED,
    "HARD_STOP": _REJECTED,
    "FLAG_FOR_REVIEW": _PENDING,
    "MANDATORY_HUMAN_APPROVAL": _PENDING,
}


class ProjectionError(Exception):
    """Raised internally when one ``stage_log`` row cannot be projected.

    Never escapes :func:`project_mode_c_history` — it is caught there and
    converted into the hard-stopped error entry described in the module
    docstring. Carried as an exception rather than a return flag so the
    per-row projection helpers can bail from anywhere in their bodies
    without threading a sentinel back through three call layers.
    """


@runtime_checkable
class _StageLogReader(Protocol):
    """Duck-typed slice of the persistence facade this reader needs."""

    def read_stages(self, build_id: str) -> Sequence[Any]:  # pragma: no cover - stub
        """Return the build's ``stage_log`` rows in chronological order."""
        ...


def project_mode_c_history(
    rows: Iterable[Any],
    *,
    subject_task_id: str | None = None,
) -> tuple[StageEntry, ...]:
    """Project ``stage_log`` rows into the planner's ``StageEntry`` contract.

    Pure and I/O-free: takes anything with the
    :class:`~forge.lifecycle.persistence.StageLogEntry` attribute surface
    (``stage_label`` / ``status`` / ``gate_mode`` / ``details``) so it can
    be unit-tested against realistic row shapes without a database.

    Args:
        rows: The build's ``stage_log`` rows, chronological. Non-Mode-C
            labels are skipped.

    Returns:
        The projected history in the same order. When any row is
        malformed the tuple ends with a hard-stopped ``task-review``
        error entry (see module docstring) — the caller therefore never
        receives a history that could fan work out over garbage.
    """
    projected: list[StageEntry] = []
    problems: list[str] = []

    for index, row in enumerate(rows):
        label = str(getattr(row, "stage_label", "") or "")
        stage_class = MODE_C_STAGE_LABELS.get(label)
        if stage_class is None:
            continue
        try:
            projected.append(_project_row(row, stage_class, subject_task_id=subject_task_id))
        except ProjectionError as exc:
            problems.append(f"row {index} ({label}): {exc}")

    if problems:
        logger.error(
            "mode_c_history_projection_malformed: %d unreadable stage_log "
            "row(s) — projecting a hard-stopped review entry so the fix "
            "journey stops instead of fanning out on garbage. Problems: %s",
            len(problems),
            "; ".join(problems),
        )
        projected.append(
            StageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status=_FAILED,
                fix_tasks=(),
                hard_stop=True,
            )
        )

    return tuple(projected)


def _project_row(row: Any, stage_class: StageClass, *, subject_task_id: str | None = None) -> StageEntry:
    """Project one Mode C ``stage_log`` row. Raises :class:`ProjectionError`."""
    details = getattr(row, "details", None)
    if details is None:
        details = {}
    if not isinstance(details, Mapping):
        raise ProjectionError(
            f"details is {type(details).__name__}, expected a mapping"
        )

    status, hard_stop = _project_status(row, details)
    failure_reason = _project_failure_reason(details, status=status)

    if stage_class is StageClass.TASK_REVIEW:
        return StageEntry(
            stage_class=stage_class,
            status=status,
            fix_tasks=_project_fix_tasks(details, status=status, subject_task_id=subject_task_id),
            finding_anchors=_project_finding_anchors(details),
            hard_stop=hard_stop,
            failure_reason=failure_reason,
        )

    return StageEntry(
        stage_class=stage_class,
        status=status,
        fix_task_id=_project_fix_task_id(details),
        hard_stop=hard_stop,
        failure_reason=failure_reason,
    )


def _project_status(row: Any, details: Mapping[str, Any]) -> tuple[str, bool]:
    """Map ``(status, gate_mode, details)`` onto ``(planner_status, hard_stop)``."""
    if details.get(LIFECYCLE_STATE_DETAILS_KEY) == _LIFECYCLE_RUNNING:
        # A dispatch-attempt row. The PASSED on the column means "the
        # dispatch was recorded", not "the stage passed".
        return _RUNNING, False

    raw_status = str(getattr(row, "status", "") or "")

    if raw_status == "GATED":
        gate_mode = getattr(row, "gate_mode", None)
        gate_mode = str(gate_mode) if gate_mode is not None else ""
        mapped = _GATE_MODE_MAP.get(gate_mode)
        if mapped is None:
            # Unknown / absent gate verdict: wait, never guess approved.
            logger.warning(
                "mode_c_history_projection: GATED row with unreadable "
                "gate_mode=%r — projecting %r (the planner waits)",
                gate_mode,
                _PENDING,
            )
            return _PENDING, False
        return mapped, gate_mode == "HARD_STOP"

    mapped = _STATUS_MAP.get(raw_status)
    if mapped is None:
        raise ProjectionError(
            f"unknown stage_log status {raw_status!r}; expected one of "
            f"{sorted({*_STATUS_MAP, 'GATED'})}"
        )
    return mapped, False


def _project_fix_tasks(
    details: Mapping[str, Any],
    *,
    status: str,
    subject_task_id: str | None = None,
) -> tuple[str, ...]:
    """Recover the typed fix-task list from an approved review's output.

    See the module docstring for the full contract; every rejection path
    raises :class:`ProjectionError` so the caller can turn it into the
    loud hard-stop entry.
    """
    if FIX_TASKS_DETAILS_KEY not in details:
        if status == _APPROVED:
            raise ProjectionError(
                f"approved task-review row carries no {FIX_TASKS_DETAILS_KEY!r} "
                "key — an approved review must state its finding, even when "
                "the finding is 'nothing'. Refusing to read it as a clean "
                "review"
            )
        # Non-approved reviews are never read for fix tasks by the planner.
        return ()

    raw = details[FIX_TASKS_DETAILS_KEY]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ProjectionError(
            f"{FIX_TASKS_DETAILS_KEY!r} is {type(raw).__name__}, expected a "
            "JSON array of identifier strings"
        )

    fix_tasks: list[str] = []
    for position, element in enumerate(raw):
        if not isinstance(element, str) or not element.strip():
            raise ProjectionError(
                f"{FIX_TASKS_DETAILS_KEY!r}[{position}] is {element!r}, "
                "expected a non-empty identifier string"
            )
        fix_tasks.append(element.strip())

    duplicates = {t for t in fix_tasks if fix_tasks.count(t) > 1}
    if duplicates:
        raise ProjectionError(
            f"{FIX_TASKS_DETAILS_KEY!r} contains duplicate identifier(s) "
            f"{sorted(duplicates)}; the planner matches dispatched work by "
            "identity, so duplicates make the walk ambiguous"
        )

    if subject_task_id and subject_task_id in fix_tasks:
        # Risk h.3, driven by the C2 coach: a review that re-emits or touches
        # its OWN subject's task artefact must not make the planner fan a
        # /task-work out against the build's own subject. Dropped loudly,
        # never silently ambiguous.
        logger.warning(
            "mode_c_history: task-review listed the build's own subject task "
            "%s as a fix task — dropped (a review's subject is never its own "
            "fix)",
            subject_task_id,
        )
        fix_tasks = [t for t in fix_tasks if t != subject_task_id]

    return tuple(fix_tasks)


def _project_finding_anchors(details: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Recover a review row's finding anchors (LI stage-2 §5).

    Deliberately NOT strict, and deliberately three-valued — the opposite
    posture to :func:`_project_fix_tasks`, for a reason worth stating:

    * ``fix_tasks`` drives a FAN-OUT. Reading it wrong dispatches work at
      the wrong thing, so anything malformed hard-stops the journey.
    * ``finding_anchors`` drives a STOP. Reading it wrong at worst costs a
      journey one extra review cycle (the cap still bounds it), and a
      hard-stop here would take down every legacy row ever written — every
      review row that predates this key.

    So:

    * key absent → ``None`` — "this row states nothing about anchors".
      The no-progress rule reads that as *no baseline* and resets, which
      is the only honest reading: a row that never recorded its findings
      cannot testify that they went unresolved.
    * key present and readable → the tuple, order-preserved, de-duplicated.
      An EMPTY tuple is a real answer ("the review reported no findings"),
      not the same thing as ``None``.
    * key present but malformed → ``None`` with a loud warning. Same
      reading as absent, because a row we cannot parse states nothing
      either — but it is a defect, so it is logged rather than shrugged at.
    """
    if FINDING_ANCHORS_DETAILS_KEY not in details:
        return None

    raw = details[FINDING_ANCHORS_DETAILS_KEY]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        logger.warning(
            "mode_c_history_projection: %r is %s, expected a JSON array of "
            "anchor strings — projecting 'no anchors recorded' (the "
            "no-progress rule resets rather than accuse the journey)",
            FINDING_ANCHORS_DETAILS_KEY,
            type(raw).__name__,
        )
        return None

    anchors: list[str] = []
    for element in raw:
        if not isinstance(element, str) or not element.strip():
            logger.warning(
                "mode_c_history_projection: %r contains %r, expected "
                "non-empty anchor strings — projecting 'no anchors "
                "recorded' for the whole row",
                FINDING_ANCHORS_DETAILS_KEY,
                element,
            )
            return None
        text = element.strip()
        if text not in anchors:
            anchors.append(text)
    return tuple(anchors)


#: ``details_json`` key carrying a dispatch's own account of itself. On a
#: failed subprocess leg it holds the leg's banner and output tail verbatim
#: (composed by ``dispatchers/subprocess.py``), which is the only durable
#: place the leg's own words survive to the terminal.
RATIONALE_DETAILS_KEY: str = "rationale"

#: Upper bound on the carried reason. ``builds.error`` is a one-line column
#: and a run report is read by a human; a 4 KB output tail belongs in the
#: row it was written on, not smeared across every downstream sentence.
_FAILURE_REASON_LIMIT: int = 600


def _project_failure_reason(
    details: Mapping[str, Any], *, status: str
) -> str | None:
    """Carry a FAILED row's own reason forward (leg-result honesty).

    Only for rows the planner reads as failed — an approved row's rationale
    is a success sentence ("task-review completed in 1.2s with 0 artefact
    path(s)") and carrying it would put cheerful text on a terminal.

    Never parsed, never branched on: the reason is threaded through to the
    terminal handler so the stop can NAME what stopped it. Trimmed to
    :data:`_FAILURE_REASON_LIMIT` because ``builds.error`` is one line.
    """
    if status != _FAILED:
        return None
    raw = details.get(RATIONALE_DETAILS_KEY)
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    if len(text) > _FAILURE_REASON_LIMIT:
        return text[: _FAILURE_REASON_LIMIT - 1] + "…"
    return text


def _project_fix_task_id(details: Mapping[str, Any]) -> str:
    """Recover the fix-task identifier a ``task-work`` row was dispatched for."""
    raw = details.get(FIX_TASK_ID_DETAILS_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectionError(
            f"task-work row carries {FIX_TASK_ID_DETAILS_KEY}={raw!r}, expected "
            "a non-empty identifier string — an unattributable /task-work row "
            "would be read as 'never dispatched' and the fix task dispatched "
            "a second time"
        )
    return raw.strip()


class SqliteModeCHistoryReader:
    """SQLite-backed :class:`forge.pipeline.supervisor.ModeCHistoryReader`.

    Composes :func:`project_mode_c_history` over the daemon's persistence
    facade. No second connection is opened — reads go through
    ``read_stages`` like every other adapter.

    Args:
        pool: The daemon's lifecycle persistence facade, or anything
            exposing ``read_stages(build_id)``.
        has_commits_reader: Optional synchronous ``(build_id) -> bool``.
            Defaults to ``None``, which answers ``False`` — the
            conservative default documented in the module docstring. The
            authoritative commit answer is taken by the terminal handler
            via the async commit probe
            (:mod:`forge.pipeline.mode_c_commit_probe`), which is where
            the supervisor already gives it precedence.
    """

    __slots__ = ("_pool", "_has_commits_reader", "_subject_task_id_reader")

    def __init__(
        self,
        pool: _StageLogReader,
        *,
        has_commits_reader: Callable[[str], bool] | None = None,
        subject_task_id_reader: Callable[[str], str | None] | None = None,
    ) -> None:
        self._pool = pool
        self._has_commits_reader = has_commits_reader
        self._subject_task_id_reader = subject_task_id_reader

    def get_mode_c_history(self, build_id: str) -> Sequence[StageEntry]:
        """Return the projected Mode C history for ``build_id``."""
        if not build_id:
            raise ValueError(
                "SqliteModeCHistoryReader.get_mode_c_history: build_id must "
                "be non-empty"
            )
        subject: str | None = None
        if self._subject_task_id_reader is not None:
            subject = self._subject_task_id_reader(build_id)
        return project_mode_c_history(
            self._pool.read_stages(build_id), subject_task_id=subject
        )

    def has_commits(self, build_id: str) -> bool:
        """Return the flag driving the planner's clean-follow-up branch.

        ``False`` unless a synchronous reader was injected. A reader that
        raises degrades to ``False`` with a loud log rather than crashing
        the turn — the terminal handler's probe still gets the real
        answer, so the conservative degrade costs nothing but a tick.
        """
        if self._has_commits_reader is None:
            return False
        try:
            return bool(self._has_commits_reader(build_id))
        except Exception as exc:  # noqa: BLE001 — defensive: never crash a turn
            logger.error(
                "SqliteModeCHistoryReader.has_commits: reader raised %s: %s "
                "for build_id=%s; answering False (the terminal handler's "
                "commit probe remains authoritative)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
