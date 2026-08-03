"""Production binding for ``StageLogRecorder`` (TASK-FW10-004).

This module is one of the four production wirings for
:func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`.
It returns a :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`
Protocol implementation that delegates to the FEAT-FORGE-001 SQLite
writer (:meth:`forge.lifecycle.persistence.SqliteLifecyclePersistence.record_stage`).

Design rules
------------

* **Protocol surface only.** :class:`_AutobuildStageLogRecorder` exposes
  exactly the single method declared on
  :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`
  (``record_running``). The persistence facade carries far more API
  than the dispatcher needs; this adapter narrows the surface so the
  dispatcher cannot accidentally reach into the wider lifecycle write
  path.
* **No second pool.** The factory accepts the same persistence facade
  the rest of FEAT-FORGE-010 wires (the "sqlite_pool" in
  ``IMPLEMENTATION-GUIDE.md`` §5). Writes route through the existing
  connection-scoped ``BEGIN IMMEDIATE`` session pattern in
  :mod:`forge.lifecycle.persistence`. We do not open a new SQLite
  connection here.
* **Mapping → :class:`StageLogEntry`.** The dispatcher's Protocol
  passes the dispatch metadata as ``details_json`` (a
  :class:`~typing.Mapping`); :meth:`record_stage` requires a
  :class:`~forge.lifecycle.persistence.StageLogEntry` Pydantic value
  object. This adapter is the only place that translation happens —
  ``target_kind="subagent"``, ``target_identifier=feature_id`` (the
  most-identifying field for an autobuild row at dispatch time, since
  the ``task_id`` may be ``None`` on the pre-dispatch call), and
  ``status=AUTOBUILD_RUNNING_STATUS`` (a schema-valid status — see the
  constant's docstring). The ``details`` payload preserves every key
  the dispatcher passed plus a ``feature_id`` echo and a ``state``
  marker (``"running"``) so a reader on the same pool can reconstruct
  the dispatch shape without joining against the ``builds`` table and
  can distinguish a dispatch-attempt row from a true terminal-pass
  row.

References:
    - TASK-FW10-004 — this task brief.
    - :mod:`forge.pipeline.dispatchers.autobuild_async` — the
      :class:`StageLogRecorder` Protocol surface.
    - :mod:`forge.lifecycle.persistence` — the
      :class:`SqliteLifecyclePersistence` facade and
      :class:`StageLogEntry` value object.
    - ``IMPLEMENTATION-GUIDE.md`` §4 contract: ``StageLogRecorder``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, Mapping, Protocol

from forge.cli._serve_deps_forward_context import (
    STAGE_LOG_GATE_DECISION_APPROVED,
    STAGE_LOG_GATE_DECISION_KEY,
)
from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.dispatchers.autobuild_async import StageLogRecorder
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)


__all__ = [
    "AUTOBUILD_LIFECYCLE_STATE_KEY",
    "AUTOBUILD_LIFECYCLE_STATE_VALUE",
    "AUTOBUILD_RUNNING_STATUS",
    "AUTOBUILD_TARGET_KIND",
    "FIX_JOURNEY_TARGET_KIND",
    "build_fix_journey_stage_log_writer",
    "build_stage_log_recorder",
    "default_fix_tasks_extractor",
]


#: ``stage_log.target_kind`` value for an autobuild dispatch row.
#:
#: The dispatcher launches a long-running ``AsyncSubAgent``
#: (``autobuild_runner``); the row's natural target is the subagent the
#: dispatcher handed control to. Mirrors the convention used by
#: :class:`forge.lifecycle.persistence.SqliteStageSkipRecorder` (which
#: writes ``target_kind="local_tool"`` for skip rows) — the column
#: encodes *what kind of thing* received the work, not the build itself.
AUTOBUILD_TARGET_KIND: str = "subagent"

#: ``stage_log.status`` value written by :meth:`record_running`.
#:
#: The :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`
#: Protocol's docstring describes the row as carrying
#: ``state="running"``, but the FEAT-FORGE-001 ``stage_log.status``
#: column is constrained by a SQLite ``CHECK`` to one of
#: ``{'PASSED', 'FAILED', 'GATED', 'SKIPPED'}`` — there is no
#: ``RUNNING`` status in the schema. The dispatch action (writing a
#: durable record that a dispatch was attempted) is itself a passed
#: action: the dispatcher successfully recorded its intent. We
#: therefore write ``status="PASSED"`` to satisfy the schema and put
#: the lifecycle marker (``"running"``) on
#: :data:`AUTOBUILD_LIFECYCLE_STATE_KEY` in ``details_json`` so a
#: reader can distinguish a dispatch-attempt row from a stage's
#: terminal pass.
AUTOBUILD_RUNNING_STATUS: str = "PASSED"

#: Key on the ``details_json`` payload that carries the lifecycle
#: state. ``"running"`` for the dispatch-attempt rows the autobuild
#: dispatcher writes; downstream consumers should treat the absence of
#: this key as "not a dispatch row" and interpret ``status`` directly.
AUTOBUILD_LIFECYCLE_STATE_KEY: str = "lifecycle_state"

#: The :data:`AUTOBUILD_LIFECYCLE_STATE_KEY` value written by
#: :meth:`record_running`. Mirrors DDR-006's
#: :class:`AutobuildState.lifecycle` ``"starting"`` literal in spirit,
#: but lives on the ``stage_log`` side rather than the ``async_tasks``
#: channel — the two writes are paired by the dispatcher (see
#: ``dispatch_autobuild_async`` invariant 3).
AUTOBUILD_LIFECYCLE_STATE_VALUE: str = "running"


class _StageLogWriter(Protocol):
    """Duck-typed slice of :class:`SqliteLifecyclePersistence` we need.

    The factory accepts any object exposing :meth:`record_stage`. In
    production the caller passes the full
    :class:`~forge.lifecycle.persistence.SqliteLifecyclePersistence`
    facade (the daemon's "sqlite_pool"); tests can pass an in-memory
    persistence built around an in-memory SQLite database without any
    further wrapping. Keeping this Protocol private to the module
    (``_StageLogWriter``) signals the duck-typed dependency without
    leaking it into the package's public surface.
    """

    def record_stage(self, entry: StageLogEntry) -> None:  # pragma: no cover - protocol stub
        """Append the ``stage_log`` row described by ``entry``."""
        ...


class _AutobuildStageLogRecorder:
    """:class:`StageLogRecorder` adapter that writes via the SQLite facade.

    The class is module-private (leading underscore) — callers should
    construct instances via :func:`build_stage_log_recorder`. The
    factory is the single documented entry point so the wiring stays
    discoverable from ``IMPLEMENTATION-GUIDE.md`` §4 without exposing
    the adapter type to inspection or subclassing.

    Args:
        persistence: The lifecycle persistence facade. Used solely as a
            :class:`_StageLogWriter` (only :meth:`record_stage` is
            invoked); the wider read/write API is intentionally not
            referenced from this adapter.
        clock: Optional zero-arg callable returning a timezone-aware
            :class:`~datetime.datetime`. Defaults to
            ``datetime.now(UTC)``. Tests can inject a deterministic
            clock to assert ``started_at`` / ``completed_at`` values.
    """

    __slots__ = ("_persistence", "_clock")

    def __init__(
        self,
        persistence: _StageLogWriter,
        *,
        clock: "Any" = None,
    ) -> None:
        self._persistence = persistence
        # Default clock returns UTC ``now`` per FEAT-FORGE-001 convention
        # (every timestamp on the stage_log table is UTC).
        self._clock = clock if clock is not None else _utc_now

    def record_running(
        self,
        build_id: str,
        feature_id: str,
        stage: StageClass,
        details_json: Mapping[str, Any],
    ) -> None:
        """Write a ``status="running"`` row to ``stage_log``.

        Mirrors the
        :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`
        Protocol exactly — same argument names, same types, same
        ordering. The body translates the Protocol's ``Mapping``
        payload into a :class:`StageLogEntry` and forwards it to the
        SQLite writer.

        Validation is fail-fast: empty ``build_id`` / ``feature_id``
        raise :class:`ValueError` so a misconfigured caller surfaces a
        clear error rather than writing a row keyed on an empty string.
        :class:`StageLogEntry`'s Pydantic ``min_length=1`` constraint
        on ``build_id`` would catch ``build_id=""`` anyway, but we add
        the explicit guard so the error message names the offending
        argument.

        Args:
            build_id: Build the row is scoped to. Non-empty.
            feature_id: Feature the row is attributed to. Non-empty;
                stored in both ``target_identifier`` and ``details`` so
                a downstream reader can filter by feature without
                joining ``stage_log`` against ``builds``.
            stage: Stage classification. The dispatcher always passes
                :attr:`StageClass.AUTOBUILD`; the recorder accepts any
                :class:`StageClass` so the surface remains general
                (the Protocol declaration does not pin the value).
            details_json: JSON-serialisable mapping persisted onto the
                row's ``details_json`` column. The dispatcher threads
                ``correlation_id`` and the resolved context entries
                through this mapping (and ``task_id`` once
                ``start_async_task`` returns). The adapter copies the
                mapping into a ``dict`` before adding the
                ``feature_id`` echo so the caller's mapping object is
                never mutated.
        """
        if not build_id:
            raise ValueError(
                "_AutobuildStageLogRecorder.record_running: build_id must "
                "be a non-empty string"
            )
        if not feature_id:
            raise ValueError(
                "_AutobuildStageLogRecorder.record_running: feature_id "
                "must be a non-empty string"
            )
        if not isinstance(stage, StageClass):
            raise TypeError(
                "_AutobuildStageLogRecorder.record_running: stage must be a "
                f"StageClass; got {type(stage).__name__}"
            )

        # Copy the mapping into a fresh dict so we never mutate the
        # caller's payload. The dispatcher reuses its ``details`` dict
        # across the pre/post-dispatch calls; mutating it here would
        # bleed cross-call state through the recorder.
        details: dict[str, Any] = dict(details_json)
        # Echo feature_id into details so a stage_log reader filtering
        # on ``details_json`` can identify the feature without a
        # separate column. Use ``setdefault`` so a caller that already
        # threaded feature_id through ``details_json`` wins (the
        # caller's value is the authoritative one).
        details.setdefault("feature_id", feature_id)
        # Stamp the dispatcher's intended ``state="running"`` marker
        # onto ``details``: the schema-allowed ``status`` column does
        # not have a "running" value, so the lifecycle marker lives
        # here. Use ``setdefault`` so an explicit caller-provided
        # marker (e.g. a test seam) wins.
        details.setdefault(
            AUTOBUILD_LIFECYCLE_STATE_KEY, AUTOBUILD_LIFECYCLE_STATE_VALUE
        )

        now: datetime = self._clock()
        entry = StageLogEntry(
            build_id=build_id,
            stage_label=stage.value,
            target_kind=AUTOBUILD_TARGET_KIND,
            target_identifier=feature_id,
            status=AUTOBUILD_RUNNING_STATUS,
            gate_mode=None,
            coach_score=None,
            threshold_applied=None,
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details=details,
        )
        self._persistence.record_stage(entry)
        logger.debug(
            "stage_log_recorder: wrote running row build_id=%s "
            "feature_id=%s stage=%s",
            build_id,
            feature_id,
            stage.value,
        )


def _utc_now() -> datetime:
    """Return the current UTC time as an aware :class:`datetime`.

    Pulled out of :class:`_AutobuildStageLogRecorder` so tests can
    inject an alternate clock without subclassing the adapter — the
    factory's default is the only production caller of this helper.
    """
    return datetime.now(UTC)


def build_stage_log_recorder(sqlite_pool: _StageLogWriter) -> StageLogRecorder:
    """Build the production :class:`StageLogRecorder` for autobuild dispatch.

    The factory is the single documented entry point for wiring the
    :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`
    collaborator on
    :func:`~forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`.
    Composition (TASK-FW10-007) calls this function with the daemon's
    shared :class:`~forge.lifecycle.persistence.SqliteLifecyclePersistence`
    facade; tests can call it with any object exposing
    :meth:`record_stage`.

    The returned object satisfies
    :class:`~forge.pipeline.dispatchers.autobuild_async.StageLogRecorder`'s
    ``runtime_checkable`` Protocol — callers that need a structural
    type check can :func:`isinstance` against the Protocol directly.

    Args:
        sqlite_pool: Object exposing
            :meth:`record_stage(entry: StageLogEntry) -> None`. In
            production this is the daemon's
            :class:`SqliteLifecyclePersistence` facade.

    Returns:
        A :class:`StageLogRecorder` Protocol implementation that
        delegates :meth:`record_running` to ``sqlite_pool.record_stage``.

    Raises:
        TypeError: If ``sqlite_pool`` does not expose a callable
            ``record_stage`` attribute. The check is duck-typed
            (``callable(getattr(...))``) so it does not pin the
            argument to :class:`SqliteLifecyclePersistence` — that
            would defeat the test seam.

    Example:
        >>> from forge.lifecycle.persistence import SqliteLifecyclePersistence
        >>> persistence = SqliteLifecyclePersistence(connection=cx)
        >>> recorder = build_stage_log_recorder(persistence)
        >>> from forge.pipeline.stage_taxonomy import StageClass
        >>> recorder.record_running(
        ...     build_id="build-1",
        ...     feature_id="FEAT-X",
        ...     stage=StageClass.AUTOBUILD,
        ...     details_json={"correlation_id": "corr-1", "task_id": None},
        ... )
    """
    record_stage = getattr(sqlite_pool, "record_stage", None)
    if not callable(record_stage):
        raise TypeError(
            "build_stage_log_recorder: sqlite_pool must expose a callable "
            "record_stage(entry: StageLogEntry) -> None method; got "
            f"{type(sqlite_pool).__name__}"
        )
    return _AutobuildStageLogRecorder(sqlite_pool)


# ---------------------------------------------------------------------------
# The fix journey's stage_log writer — THE ``fix_tasks`` PRODUCER
# ---------------------------------------------------------------------------
#
# Conductor revival Stage 2, shakeout item 5.
#
# ``mode_c_history_reader.FIX_TASKS_DETAILS_KEY`` documents a strict contract:
# an approved ``task-review`` row carries its typed fix-task list in
# ``details_json``, and the projection turns that list into the planner's
# fan-out. Until now **nothing wrote it**. The projection had a reader with no
# producer, so every real review row was "malformed" by its own contract and
# the journey hard-stopped before it ever dispatched a fix.
#
# This is the producer. It is a ``StageLogWriter`` (the write-side Protocol the
# subprocess dispatcher calls once per dispatch) that records the fix journey's
# two stages with the two keys the projection reads back:
#
#   * a ``task-review`` row carries ``fix_tasks`` — ALWAYS, even when the
#     finding is "nothing" (an empty array is the legitimate clean-review
#     answer; an ABSENT key is the malformed one);
#   * a ``task-work`` row carries ``fix_task_id`` — the fix task it was
#     dispatched against, so the planner's walk can tell dispatched work from
#     outstanding work instead of dispatching the same fix twice.
#
# LI stage-2 §5 adds a third key on the review row: ``finding_anchors`` — the
# location identities of what the review found. The fix-task list cannot serve
# that purpose (its ids are prose+position-derived and re-mint every cycle);
# the anchors are what the conductor's review-cycle no-progress stop compares.
#
# Round-trip, not two half-contracts: the reader's constants are imported here
# rather than re-spelled, so a rename cannot leave the writer and the reader
# disagreeing in silence.


#: ``stage_log.target_kind`` for a fix-journey dispatch row. The work went to a
#: GuardKit subprocess — the same convention the skip recorder uses
#: (``local_tool``), because the column encodes *what kind of thing* received
#: the work.
FIX_JOURNEY_TARGET_KIND: str = "local_tool"

#: ``stage_log.status`` values, mapped from the dispatcher's own discriminator.
#: The schema's CHECK allows only PASSED / FAILED / GATED / SKIPPED; the
#: projection maps PASSED → ``approved`` and FAILED → ``failed``, which is
#: exactly the planner vocabulary a completed dispatch should produce.
#: The ``stage_log.status`` a successful fix-journey dispatch writes. Named
#: because two things key off it now: the row's own status column, and the
#: forward-context builder's approved-row filter (see ``record_dispatch``).
_STAGE_LOG_PASSED: str = "PASSED"

_DISPATCH_STATUS_TO_STAGE_LOG: Mapping[str, str] = {
    "success": _STAGE_LOG_PASSED,
    "failed": "FAILED",
    "degraded": "FAILED",
}

#: Canonical fix-task identifier shape. Mirrors the queue's boundary regex
#: (``cli/queue.py::_TASK_ID_REGEX``) widened by the per-fix-task suffix a
#: review assigns (``TASK-ABC123-004`` / ``TASK-ABC123-A``).
_FIX_TASK_ID_RE = re.compile(r"^TASK-[A-Z0-9]{3,12}(?:-[A-Za-z0-9]+)*$")


def default_fix_tasks_extractor(
    *,
    artefact_paths: "tuple[str, ...]",
    rationale: str,
) -> tuple[str, ...]:
    """Recover a review's typed fix-task list from what the dispatch returned.

    ``/task-review`` emits one task artefact per fix it wants done; the
    dispatcher hands those paths back on
    :attr:`~forge.pipeline.dispatchers.subprocess.StageDispatchResult.artefact_paths`
    after allowlist gating. The identifier is the artefact's own file stem —
    the same ``TASK-XXX`` name a developer types into ``/task-work``.

    Deliberately conservative, because risk h.3 (the least-proven seam) is
    exactly "if task-review's output parsing is loose, the planner fans out
    wrong":

    * only stems matching the canonical identifier shape are taken — a
      ``README.md`` or a coach verdict in the artefact list is not a fix task;
    * order is preserved and duplicates are dropped, because the projection
      REFUSES a list with duplicates (the planner matches dispatched work by
      identity, so a repeated id makes the walk ambiguous);
    * an empty result is returned honestly as an empty tuple — a clean review
      is a real outcome, and the writer records the key either way.

    ``rationale`` is accepted (and currently unused) so an operator can swap in
    a richer extractor over the subprocess's own text output without changing
    a call site. This default reads only what the dispatcher already proved:
    paths inside the worktree allowlist.
    """
    seen: list[str] = []
    for raw in artefact_paths or ():
        stem = PurePath(str(raw)).stem
        if not _FIX_TASK_ID_RE.match(stem):
            continue
        if stem in seen:
            continue
        seen.append(stem)
    return tuple(seen)


class _FixJourneyStageLogWriter:
    """``StageLogWriter`` that records the two keys the projection reads.

    Module-private; construct via :func:`build_fix_journey_stage_log_writer`.

    One instance is bound per *dispatch* for ``task-work`` (via
    :meth:`for_fix_task`) because the fix-task identifier is a property of the
    dispatch, not of the writer — and the dispatcher's ``record_dispatch``
    Protocol has no slot for it. Binding rather than widening the Protocol
    keeps the dispatcher's surface untouched.
    """

    __slots__ = ("_persistence", "_extractor", "_clock", "_fix_task_id")

    def __init__(
        self,
        persistence: _StageLogWriter,
        *,
        fix_tasks_extractor: "Any" = None,
        clock: "Any" = None,
        fix_task_id: str | None = None,
    ) -> None:
        self._persistence = persistence
        self._extractor = fix_tasks_extractor or default_fix_tasks_extractor
        self._clock = clock if clock is not None else _utc_now
        self._fix_task_id = fix_task_id

    def for_fix_task(self, fix_task_id: str | None) -> "_FixJourneyStageLogWriter":
        """Return a sibling writer bound to one fix task's dispatch."""
        return _FixJourneyStageLogWriter(
            self._persistence,
            fix_tasks_extractor=self._extractor,
            clock=self._clock,
            fix_task_id=fix_task_id,
        )

    def record_dispatch(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        correlation_id: str,
        status: "Any",
        artefact_paths: "tuple[str, ...]",
        rationale: str,
        exit_code: int,
        duration_secs: float,
        detection_findings: "tuple[dict[str, Any], ...]" = (),
        detection_findings_reported: bool = False,
    ) -> None:
        """Write the fix-journey dispatch row, with the projection's keys."""
        from forge.pipeline.mode_c_history_reader import (
            FINDING_ANCHORS_DETAILS_KEY,
            FIX_TASK_ID_DETAILS_KEY,
            FIX_TASKS_DETAILS_KEY,
            derive_finding_anchors,
        )

        status_value = str(getattr(status, "value", status)).lower()
        row_status = _DISPATCH_STATUS_TO_STAGE_LOG.get(status_value, "FAILED")

        details: dict[str, Any] = {
            "correlation_id": correlation_id,
            "rationale": rationale,
            "exit_code": exit_code,
            "artefact_paths": list(artefact_paths or ()),
        }
        if feature_id:
            details["feature_id"] = feature_id

        # THE ROW HAS TO BE READABLE BY THE FORWARD-CONTEXT BUILDER
        # (shadow-replay item 4). The builder's Mode C follow-up-review
        # branch reads "every APPROVED /task-work row" through
        # ``_SqliteStageLogReader``, whose filter is
        # ``details_json["gate_decision"] == "approved"``. This writer
        # never wrote that key, so every fix-journey row was invisible to
        # it and the branch could only ever return an empty list — the
        # follow-up review dispatched blind to the work it reviews.
        #
        # What counts as "approved" for a fix journey: a stage that
        # DISPATCHED AND SUCCEEDED. The fix journey has no per-stage human
        # gate — the owner's one act is the merge word at the end — so the
        # subprocess's own exit code is the approval, and a FAILED /
        # DEGRADED row stays unreadable, which is the honest reading.
        if row_status == _STAGE_LOG_PASSED:
            details[STAGE_LOG_GATE_DECISION_KEY] = STAGE_LOG_GATE_DECISION_APPROVED

        target_identifier = feature_id or build_id
        if stage is StageClass.TASK_REVIEW:
            # ALWAYS present on a review row. The projection treats an
            # absent key on an approved review as malformed and hard-stops
            # the journey — correctly: "an approved review must state its
            # finding, even when the finding is 'nothing'."
            #
            # LEG-RESULT HONESTY (2026-08-03): a FAILED review leg has no
            # findings to state, so its artefact list is DEBRIS, not a
            # fix-task list. The live refusal exited 2 in a second; a leg
            # that dies mid-run having already touched ``tasks/`` would
            # otherwise have its leftovers fanned out as the review's
            # verdict. The key still rides (the row shape is one shape,
            # whatever the outcome) — it just rides EMPTY, and the log
            # below says "the leg failed", never "clean review".
            leg_failed = row_status != _STAGE_LOG_PASSED
            fix_tasks: tuple[str, ...] = ()
            if not leg_failed:
                fix_tasks = tuple(
                    self._extractor(
                        artefact_paths=tuple(artefact_paths or ()),
                        rationale=rationale,
                    )
                )
            elif artefact_paths:
                logger.error(
                    "fix_journey_stage_log: task-review row for build_id=%s "
                    "is a FAILED leg carrying %d artefact path(s) — DISCARDED "
                    "rather than read as fix tasks; a leg that fell over "
                    "stated no finding",
                    build_id,
                    len(artefact_paths),
                )
            details[FIX_TASKS_DETAILS_KEY] = list(fix_tasks)
            # THE ANCHORS RIDE BESIDE THE FIX TASKS (LI stage-2 §5).
            # The fix-task id is prose+position-derived and re-mints itself
            # every cycle; the finding's location anchor is the identity
            # that survives, and it is what the review-cycle no-progress
            # stop compares.
            #
            # Written only when the leg actually REPORTED a findings block.
            # The key is three-valued by design and the writer must not
            # forge the third value: an empty list means "the review looked
            # and found nothing" (a clean review), while an ABSENT key means
            # "this row says nothing about findings" — which covers both a
            # leg that emitted no readable block and every row written
            # before this key existed. The projection reads absent as "no
            # baseline", never as "everything was resolved".
            finding_anchors: tuple[str, ...] = ()
            if detection_findings_reported:
                finding_anchors = derive_finding_anchors(detection_findings)
                details[FINDING_ANCHORS_DETAILS_KEY] = list(finding_anchors)
            logger.log(
                logging.ERROR if leg_failed else logging.INFO,
                "fix_journey_stage_log: task-review row for build_id=%s "
                "records %d fix task(s): %s | finding anchors: %s",
                build_id,
                len(fix_tasks),
                (
                    ", ".join(fix_tasks)
                    or (
                        "none — THE LEG FAILED, this is not a clean review"
                        if leg_failed
                        else "none (clean review)"
                    )
                ),
                (
                    (", ".join(finding_anchors) or "none (the review found nothing)")
                    if detection_findings_reported
                    else "NOT RECORDED — the leg reported no readable findings block"
                ),
            )
        elif stage is StageClass.TASK_WORK:
            if self._fix_task_id:
                details[FIX_TASK_ID_DETAILS_KEY] = self._fix_task_id
                target_identifier = self._fix_task_id
            else:
                # Unattributable work. The projection refuses it loudly
                # (a row read as "never dispatched" would dispatch the fix
                # a second time), so say so here where it can be fixed.
                logger.error(
                    "fix_journey_stage_log: task-work row for build_id=%s has "
                    "NO fix_task_id bound — the projection will hard-stop the "
                    "journey rather than risk a double dispatch",
                    build_id,
                )

        now: datetime = self._clock()
        entry = StageLogEntry(
            build_id=build_id,
            stage_label=stage.value,
            target_kind=FIX_JOURNEY_TARGET_KIND,
            target_identifier=target_identifier,
            status=row_status,
            gate_mode=None,
            coach_score=None,
            threshold_applied=None,
            started_at=now,
            completed_at=now,
            duration_secs=float(duration_secs),
            details=details,
        )
        self._persistence.record_stage(entry)


def build_fix_journey_stage_log_writer(
    sqlite_pool: _StageLogWriter,
    *,
    fix_tasks_extractor: "Any" = None,
    clock: "Any" = None,
) -> "_FixJourneyStageLogWriter":
    """Build the conductor's ``stage_log`` writer — the ``fix_tasks`` producer.

    Args:
        sqlite_pool: Object exposing ``record_stage(entry)``. Production
            passes the daemon's shared persistence facade — no second pool.
        fix_tasks_extractor: ``(*, artefact_paths, rationale) ->
            Iterable[str]``. Defaults to
            :func:`default_fix_tasks_extractor`.
        clock: Zero-arg UTC ``datetime`` source; defaults to ``now``.

    Returns:
        A ``StageLogWriter`` whose ``task-review`` rows the Mode C history
        projection can actually read back.
    """
    record_stage = getattr(sqlite_pool, "record_stage", None)
    if not callable(record_stage):
        raise TypeError(
            "build_fix_journey_stage_log_writer: sqlite_pool must expose a "
            "callable record_stage(entry: StageLogEntry) -> None method; got "
            f"{type(sqlite_pool).__name__}"
        )
    return _FixJourneyStageLogWriter(
        sqlite_pool, fix_tasks_extractor=fix_tasks_extractor, clock=clock
    )
