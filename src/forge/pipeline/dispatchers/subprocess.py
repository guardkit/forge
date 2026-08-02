"""Subprocess-stage dispatcher for FEAT-FORGE-007 Mode A (TASK-MAG7-008).

This module composes FEAT-FORGE-005's GuardKit subprocess engine
(:mod:`forge.adapters.guardkit.run`) with TASK-MAG7-006's
:class:`~forge.pipeline.forward_context_builder.ForwardContextBuilder` to
dispatch the four subprocess Mode A stages:

- :attr:`~forge.pipeline.stage_taxonomy.StageClass.SYSTEM_ARCH`  → ``/system-arch``
- :attr:`~forge.pipeline.stage_taxonomy.StageClass.SYSTEM_DESIGN` → ``/system-design``
- :attr:`~forge.pipeline.stage_taxonomy.StageClass.FEATURE_SPEC`  → ``/feature-spec``
- :attr:`~forge.pipeline.stage_taxonomy.StageClass.FEATURE_PLAN`  → ``/feature-plan``

…and, since the conductor's revival (Stage 1a, design pass §a.4), the two
stages of the **fix journey** — the conductor's first bounded job:

- :attr:`~forge.pipeline.stage_taxonomy.StageClass.TASK_REVIEW`   → ``/task-review``
- :attr:`~forge.pipeline.stage_taxonomy.StageClass.TASK_WORK`     → ``/task-work``

The fix-journey stages dispatch by *task* subject rather than by feature
(``--task-id``, plus the queue's ``--feature-yaml``); see
:func:`_build_argv_for_stage` for the two argv shapes.

The dispatcher is intentionally a *thin composition layer* (per
TASK-MAG7-008 Implementation Notes). Worktree confinement, allowlist
enforcement, the 600-second timeout, and the universal "never raises"
error contract all live in :func:`forge.adapters.guardkit.run`. This
module:

1. Maps the consumer :class:`StageClass` to its GuardKit slash-command
   subcommand string.
2. Asks the :class:`ForwardContextBuilder` for the
   :class:`~forge.pipeline.forward_context_builder.ContextEntry` list
   (read-side allowlist + approved-only filter applied there).
3. Converts each :class:`ContextEntry` into a ``--context <value>`` pair
   in the subprocess argv.
4. Threads ``correlation_id`` onto the subprocess command line as
   ``--correlation-id <id>`` so the GuardKit subcommand can echo it back
   onto every ``pipeline.*`` event it publishes (Group I @data-integrity
   scenario "correlation_id propagates end-to-end").
5. Awaits the injected subprocess runner — production wires
   :func:`forge.adapters.guardkit.run`; tests inject an in-memory fake.
6. Filters the returned artefact paths through the worktree allowlist
   (defence-in-depth — the GuardKit binary applies the same check on
   its own side; we re-check here so a misbehaving subprocess cannot
   silently sneak an out-of-tree path into ``stage_log``).
7. Writes a :class:`StageLogWriter` row capturing the dispatch outcome,
   artefact paths, ``correlation_id``, and feature attribution
   (Group G @data-integrity "Per-feature artefact paths").
8. Folds every outcome — success, non-zero exit, timeout, internal
   error in the runner, even out-of-tree artefact paths — into a
   :class:`StageDispatchResult`. The function never raises past its
   boundary (universal error contract; mirrors ADR-ARCH-025 for
   :func:`guardkit.run`). The single deliberate exception is
   :class:`asyncio.CancelledError`, which is re-raised so the caller's
   async context unwinds cleanly.

References:
    - TASK-MAG7-008 — this task brief.
    - TASK-MAG7-001 — :class:`StageClass` enum.
    - TASK-MAG7-006 — :class:`ForwardContextBuilder` /
      :class:`ContextEntry`.
    - TASK-GCI-008 / FEAT-FORGE-005 — :func:`guardkit.run` (subprocess
      engine and worktree allowlist enforcement).
    - FEAT-FORGE-007 Group A — full pipeline dispatch order.
    - FEAT-FORGE-007 Group C @negative — ``/feature-spec`` failure halts
      that feature's inner loop; the dispatcher's rationale records the
      failed-spec text so the supervisor can act on it.
    - FEAT-FORGE-007 Group G @data-integrity — per-feature artefact
      paths must be attributed by ``feature_id``.
    - FEAT-FORGE-007 Group I @data-integrity — ``correlation_id`` is
      threaded onto every subprocess envelope and downstream event.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from forge.adapters.guardkit.models import GuardKitResult
from forge.pipeline.forward_context_builder import (
    ContextEntry,
    ForwardContextBuilder,
    WorktreeAllowlist,
)
from forge.pipeline.stage_taxonomy import PER_FEATURE_STAGES, StageClass

logger = logging.getLogger(__name__)


__all__ = [
    "FixTaskReference",
    "MODE_C_STAGES",
    "StageDispatchStatus",
    "StageDispatchResult",
    "StageLogWriter",
    "SubprocessRunner",
    "SUBPROCESS_STAGE_COMMANDS",
    "dispatch_subprocess_stage",
]


# ---------------------------------------------------------------------------
# Stage → GuardKit slash-command map
# ---------------------------------------------------------------------------


#: Maps each subprocess :class:`StageClass` to the GuardKit subcommand
#: token that runs it. The leading ``/`` of the slash-command form is
#: dropped here because :func:`forge.adapters.guardkit.run` invokes the
#: GuardKit binary as ``guardkit <subcommand> ...`` (see
#: :data:`forge.adapters.guardkit.run._GUARDKIT_BINARY` and
#: ``docs/design/contracts/API-subprocess.md`` §3.1).
#:
#: Any :class:`StageClass` *not* in this map is rejected by
#: :func:`dispatch_subprocess_stage` with :class:`ValueError` — calling
#: the subprocess dispatcher with e.g. ``StageClass.PRODUCT_OWNER`` is
#: a programming error, not a runtime condition.
SUBPROCESS_STAGE_COMMANDS: dict[StageClass, str] = {
    StageClass.SYSTEM_ARCH: "system-arch",
    StageClass.SYSTEM_DESIGN: "system-design",
    StageClass.FEATURE_SPEC: "feature-spec",
    StageClass.FEATURE_PLAN: "feature-plan",
    # Conductor revival, Stage 1a (design pass §a.4): the fix journey's
    # two stages. Everything around this map was already Mode-C-aware —
    # the conductor's router lists both stages in ``_SUBPROCESS_STAGES``
    # and the build-system adapter already runs both subcommands with bus
    # streaming (``forge.tools.guardkit`` guardkit_task_review /
    # guardkit_task_work) — but these two rows were absent, so the very
    # first fix-journey dispatch raised ValueError one line below. The
    # rows alone do not dispatch: see ``_build_argv_for_stage`` for the
    # two argv shapes that make them real (risk h.2).
    StageClass.TASK_REVIEW: "task-review",
    StageClass.TASK_WORK: "task-work",
}


#: The fix journey's stages (design pass §a.4). Both take a ``--task-id``
#: subject rather than a ``--feature-id``: the review's subject is the
#: build's own task, and each work dispatch's subject is one fix task the
#: review emitted. Kept local to the dispatcher because the *argv* split
#: is a dispatcher concern — ``stage_taxonomy`` already carries the
#: orthogonal ``PER_FIX_TASK_STAGES`` fan-out tag.
MODE_C_STAGES: frozenset[StageClass] = frozenset(
    {StageClass.TASK_REVIEW, StageClass.TASK_WORK}
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class StageDispatchStatus(StrEnum):
    """Discriminator on :class:`StageDispatchResult`.

    Members:
        SUCCESS: Subprocess returned ``status="success"`` with all
            artefact paths inside the worktree allowlist.
        FAILED: Subprocess returned a non-zero exit, timed out, refused
            to run (cwd outside allowlist), produced an artefact path
            outside the worktree allowlist, or crashed inside the
            wrapper. The :attr:`StageDispatchResult.rationale` field
            carries the reason.
        DEGRADED: Reserved for parity with the specialist dispatcher
            (TASK-MAG7-007). The subprocess engine has no "degraded"
            terminal state — :func:`dispatch_subprocess_stage` never
            emits this value today, but the symbol exists so callers
            can switch on a uniform :class:`StageDispatchStatus`
            surface across both dispatchers.
    """

    SUCCESS = "success"
    FAILED = "failed"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class StageDispatchResult:
    """Structured outcome of a subprocess-stage dispatch.

    Frozen + slotted so callers can store it in dicts/sets without worry
    and so the field set is the contract — no additional state can be
    smuggled in via attribute assignment after the fact.

    Attributes:
        status: :class:`StageDispatchStatus` discriminator.
        stage: Consumer stage that was dispatched.
        build_id: Build identifier the dispatch is scoped to.
        feature_id: ``None`` for non-per-feature stages (``SYSTEM_ARCH``,
            ``SYSTEM_DESIGN``); the feature identifier for per-feature
            stages (``FEATURE_SPEC``, ``FEATURE_PLAN``).
        correlation_id: Originating correlation_id threaded onto the
            subprocess command line and any pipeline events the
            subprocess publishes.
        artefact_paths: Tuple of artefact filesystem paths the subprocess
            produced. Paths outside the worktree allowlist are *removed*
            from this tuple before the result is constructed; if any
            were removed the result is forced to :attr:`StageDispatchStatus.FAILED`
            regardless of the subprocess's own exit status.
        rationale: Human-readable explanation. For ``SUCCESS`` this is a
            short ``"<subcommand> completed in N.NNs"`` summary; for
            ``FAILED`` it carries the failed-spec rationale (Group C
            @negative scenario), the timeout / non-zero exit detail, or
            the allowlist-rejection message — whichever applies.
        exit_code: Subprocess exit code as reported by the runner, or
            ``-1`` for timeouts / internal errors.
        duration_secs: Wall-clock duration of the subprocess invocation.
        subcommand: The GuardKit subcommand token that was invoked
            (e.g. ``"feature-spec"``). Stored for trace-ability without
            forcing callers to re-derive it from ``stage``.
        detection_findings: The parsed ``## Detection Findings`` block, as
            the subprocess emitted it (LI stage-2 design §5). The parser
            has always recovered this block
            (:func:`forge.adapters.guardkit.parser.parse_stdout`) and the
            dispatcher has always dropped it on the floor — so the fix
            journey's only stable dedup key never reached the code that
            needs it. Empty tuple when the block was absent, unparseable,
            or genuinely empty — read
            :attr:`detection_findings_reported` to tell those apart.
        detection_findings_reported: Whether the subprocess actually
            emitted a ``## Detection Findings`` block the parser could
            read. ``True`` alongside an EMPTY
            :attr:`detection_findings` is the honest "the review looked
            and found nothing" — a clean review, which is the fix
            journey's SUCCESS path. ``False`` is "no block at all, or one
            that would not parse".

            The two must not be collapsed. The review-cycle no-progress
            stop fails closed on the second (a leg that stops emitting its
            findings cannot be read as having fixed them); collapsing them
            would fire that stop on the first, which would kill every
            journey at precisely the clean review that was about to end it
            well. The design (§5) states the fail-closed rule for a
            "missing/unparseable findings block" — this flag is what makes
            *missing* mean missing.

    .. note::
       ``detection_findings`` holds ``dict`` elements, so a result carrying
       findings is NOT hashable. Nothing in the pipeline hashes a
       :class:`StageDispatchResult` (the frozen+slots docstring line above
       predates this field and describes the *mutation* guarantee, which is
       unchanged); a caller that wants a set key should use
       :func:`forge.pipeline.finding_anchors.derive_finding_anchors`, which
       is what the driver does.

    Convenience class attributes :attr:`SUCCESS`, :attr:`FAILED`, and
    :attr:`DEGRADED` mirror the :class:`StageDispatchStatus` enum so
    callers can write ``result.status is StageDispatchResult.FAILED``
    without an extra import. They are populated below the class body.
    """

    status: StageDispatchStatus
    stage: StageClass
    build_id: str
    feature_id: str | None
    correlation_id: str
    artefact_paths: tuple[str, ...]
    rationale: str
    exit_code: int
    duration_secs: float
    subcommand: str
    detection_findings: tuple[dict[str, Any], ...] = ()
    detection_findings_reported: bool = False

    # Class-level mirrors of StageDispatchStatus members. These are NOT
    # frozen-dataclass instance fields — they are populated below the
    # class body via setattr(StageDispatchResult, ...) so callers can
    # use the enum-like form ``StageDispatchResult.FAILED`` from the
    # task's acceptance-criteria phrasing without an extra import.


# Populate the class-level mirrors so the AC's `StageDispatchResult.FAILED`
# phrasing resolves without forcing callers to import StageDispatchStatus.
# Done after the class body because frozen dataclasses prohibit mutation
# of *instances*; the class object itself remains a normal type and these
# are class attributes, not instance fields.
for _member in StageDispatchStatus:
    setattr(StageDispatchResult, _member.name, _member)
del _member


# ---------------------------------------------------------------------------
# Injected ports — Protocols (no I/O concrete here)
# ---------------------------------------------------------------------------


@runtime_checkable
class StageLogWriter(Protocol):
    """Write-side Protocol over the FEAT-FORGE-001 ``stage_log`` table.

    Mirrors the read-side Protocols (
    :class:`forge.pipeline.forward_context_builder.StageLogReader`,
    :class:`forge.pipeline.stage_ordering_guard.StageLogReader`)
    deliberately — every dispatcher writes through a Protocol so unit
    tests can substitute an in-memory fake and production wires the
    FEAT-FORGE-001 SQLite adapter.

    The single :meth:`record_dispatch` method captures everything the
    dispatcher knows about an outcome at the moment it lands. Production
    treats the call as an UPSERT keyed by ``(build_id, stage, feature_id,
    correlation_id)``; the Protocol does not surface that detail because
    the dispatcher does not care.
    """

    def record_dispatch(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        correlation_id: str,
        status: StageDispatchStatus,
        artefact_paths: tuple[str, ...],
        rationale: str,
        exit_code: int,
        duration_secs: float,
        detection_findings: tuple[dict[str, Any], ...] = (),
        detection_findings_reported: bool = False,
    ) -> None:  # pragma: no cover - protocol stub
        """Record a stage_log row for the just-completed dispatch.

        ``detection_findings`` carries the parsed ``## Detection Findings``
        block and ``detection_findings_reported`` says whether there WAS
        one (LI stage-2 §5) — see :class:`StageDispatchResult` for why the
        second is not redundant. The dispatcher ALWAYS passes both, so an
        implementation has to accept them; the defaults are here so a
        writer with no use for the block can ignore them without restating
        a value, not as licence to omit the parameters.
        """
        ...


@runtime_checkable
class FixTaskReference(Protocol):
    """Structural view of the fix-task reference the conductor threads in.

    The conductor's Mode C turn puts
    :class:`forge.pipeline.mode_c_planner.FixTaskRef` on the dispatcher
    kwargs as ``fix_task`` (``supervisor.py`` "MODE_C planner chose
    TASK_WORK" branch). The dispatcher only needs two of its fields, and
    typing them structurally keeps this module free of an import from the
    planner (which drags in ``forge.lifecycle.persistence``) — the same
    Protocol-over-collaborator discipline every other seam here follows.

    Attributes:
        fix_task_id: The fix-task identifier the review emitted. Becomes
            the ``--task-id`` subject of the ``task-work`` dispatch.
        review_history_index: Back-reference to the ``/task-review``
            history entry that emitted the identifier. **Not** threaded
            onto argv — the build system has no flag for it; it is the
            audit anchor recorded on the ``stage_log`` row by the Mode C
            terminal handler's attribution helper.
    """

    fix_task_id: str
    review_history_index: int


#: Subprocess-runner callable signature.
#:
#: This is the seam :func:`dispatch_subprocess_stage` calls into. In
#: production the value is :func:`forge.adapters.guardkit.run`; tests
#: inject a coroutine that returns a deterministic
#: :class:`~forge.adapters.guardkit.models.GuardKitResult` without
#: spawning a real process. The narrow keyword-only signature mirrors
#: :func:`guardkit.run` exactly so the production wiring is a one-line
#: ``functools.partial`` (no shape adapter required).
SubprocessRunner = Callable[..., Awaitable[GuardKitResult]]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DispatchPlan:
    """Internal value object — the assembled subprocess invocation.

    Pulled out so :func:`dispatch_subprocess_stage` reads as a five-step
    pipeline (validate → plan → run → filter → record) instead of a
    ten-branch flat function. Not exported.
    """

    subcommand: str
    args: list[str]
    extra_context_paths: list[str]
    text_context_warnings: list[str] = field(default_factory=list)


def _context_entries_to_argv(
    entries: list[ContextEntry],
) -> tuple[list[str], list[str], list[str]]:
    """Split :class:`ContextEntry` values into argv / extra-paths / warnings.

    The dispatcher needs three lists:

    1. ``argv`` — flag/value pairs threaded directly onto the subprocess
       command line. Used for non-default flags and for inline-text
       payloads (``kind="text"``) that the subcommand reads from argv.
    2. ``extra_context_paths`` — paths threaded through
       :func:`forge.adapters.guardkit.run`'s ``extra_context_paths``
       parameter so the resolver merges them with the manifest-derived
       ones (ASSUM-005, retry path).
    3. ``warnings`` — diagnostic strings recorded on the result rationale
       on failure (e.g. when an entry is dropped because it has an
       unsupported flag/kind combination).

    The forward-context builder only emits :class:`ContextEntry` values
    with ``flag="--context"`` today, so the partition is straightforward:
    text entries become argv pairs; path entries become
    ``extra_context_paths``. This function is generalised so a future
    recipe can introduce a new flag without forcing every dispatcher to
    re-partition.
    """
    argv: list[str] = []
    extra_paths: list[str] = []
    warnings: list[str] = []
    for entry in entries:
        if entry.kind == "text":
            argv.extend([entry.flag, entry.value])
        elif entry.kind == "path":
            if entry.flag == "--context":
                extra_paths.append(entry.value)
            else:
                # Non-default flag with a path payload — thread on argv.
                argv.extend([entry.flag, entry.value])
        else:
            warnings.append(
                f"unsupported ContextEntry kind={entry.kind!r} flag={entry.flag!r}; "
                "dropped from subprocess argv"
            )
    return argv, extra_paths, warnings


#: ``--context`` flag for the failure-pack summary entry. The fix journey's
#: forward context carries the failed build's pack index alongside the
#: review→work entries; it rides the same ``--context`` slot as every other
#: text entry so the subprocess needs no new flag to read it.
_FAILURE_PACK_FLAG: str = "--context"


def _entries_from_forward_context(
    forward_context: Mapping[str, Any],
) -> list[ContextEntry]:
    """Rehydrate :class:`ContextEntry` values from the conductor's context.

    The conductor's ``fix_task_context_builder`` returns a JSON-safe
    mapping — ``{"context_entries": [{"flag", "value", "kind"}, …],
    "failure_pack": {...} | None}`` — because it rides a dispatch payload
    and a ``stage_log`` row. This turns it back into the typed entries the
    argv builder already knows how to partition, so the two shapes never
    diverge into two argv assemblers.

    Malformed elements are DROPPED with a warning rather than raising: the
    dispatcher's contract is "never raise past the boundary", and a fix
    journey that dies because one context entry was the wrong shape would
    be a worse failure than one that runs with less context.
    """
    entries: list[ContextEntry] = []
    raw_entries = forward_context.get("context_entries") or ()
    if isinstance(raw_entries, (str, bytes)) or not isinstance(
        raw_entries, (list, tuple)
    ):
        logger.warning(
            "dispatch_subprocess_stage: forward_context['context_entries'] is "
            "%s, expected a list — dispatching with no context entries",
            type(raw_entries).__name__,
        )
        raw_entries = ()
    for element in raw_entries:
        if isinstance(element, ContextEntry):
            entries.append(element)
            continue
        if not isinstance(element, Mapping):
            logger.warning(
                "dispatch_subprocess_stage: forward-context entry %r is not a "
                "mapping; dropped",
                element,
            )
            continue
        flag = element.get("flag")
        value = element.get("value")
        kind = element.get("kind", "text")
        if not isinstance(flag, str) or not isinstance(value, str):
            logger.warning(
                "dispatch_subprocess_stage: forward-context entry %r has no "
                "string flag/value; dropped",
                element,
            )
            continue
        entries.append(
            ContextEntry(flag=flag, value=value, kind=str(kind) or "text")
        )

    pack = forward_context.get("failure_pack")
    if isinstance(pack, Mapping) and pack:
        # The pack index is a small JSON summary, not a path — it rides as
        # an inline text entry so the subprocess reads the evidence the
        # failed build left without a second file-resolution hop.
        import json

        entries.append(
            ContextEntry(
                flag=_FAILURE_PACK_FLAG,
                value=json.dumps({"failure_pack": dict(pack)}, sort_keys=True),
                kind="text",
            )
        )
    return entries


def _filter_artefact_paths(
    *,
    paths: list[str],
    build_id: str,
    allowlist: WorktreeAllowlist,
    subcommand: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Partition ``paths`` into (kept, rejected) by the allowlist gate.

    Each path is checked individually against
    :meth:`WorktreeAllowlist.is_allowed`. Rejects are returned as a
    tuple so the caller can format them into the failure rationale and
    log a structured warning. The allowlist check is defence-in-depth
    over :func:`guardkit.run`'s own ``read_allowlist`` enforcement —
    catching the case where the GuardKit binary itself returns a path
    outside the worktree (a misbehaving subprocess).
    """
    kept: list[str] = []
    rejected: list[str] = []
    for path in paths:
        if allowlist.is_allowed(build_id, path):
            kept.append(path)
        else:
            rejected.append(path)
            logger.warning(
                "dispatch_subprocess_stage: artefact path outside worktree "
                "allowlist; build_id=%s subcommand=%s path=%s — rejecting "
                "dispatch",
                build_id,
                subcommand,
                path,
            )
    return tuple(kept), tuple(rejected)


def _record_safely(
    writer: StageLogWriter,
    *,
    build_id: str,
    stage: StageClass,
    feature_id: str | None,
    correlation_id: str,
    status: StageDispatchStatus,
    artefact_paths: tuple[str, ...],
    rationale: str,
    exit_code: int,
    duration_secs: float,
    detection_findings: tuple[dict[str, Any], ...] = (),
    detection_findings_reported: bool = False,
) -> None:
    """Call the writer; log + swallow any exception.

    The dispatcher's contract is "never raise past the boundary". A
    writer that itself raises (transient SQLite hiccup, programmer
    error in the production adapter) must not turn a successful
    subprocess dispatch into an unhandled exception. We log at ERROR
    so the operator sees the lost row, then continue — the in-memory
    :class:`StageDispatchResult` we return still carries the full
    outcome.
    """
    try:
        writer.record_dispatch(
            build_id=build_id,
            stage=stage,
            feature_id=feature_id,
            correlation_id=correlation_id,
            status=status,
            artefact_paths=artefact_paths,
            rationale=rationale,
            exit_code=exit_code,
            duration_secs=duration_secs,
            detection_findings=detection_findings,
            detection_findings_reported=detection_findings_reported,
        )
    except Exception as exc:  # noqa: BLE001 — by design, see docstring
        logger.error(
            "dispatch_subprocess_stage: stage_log writer raised %s: %s; "
            "build_id=%s stage=%s feature_id=%s — row LOST but result still "
            "returned to caller",
            type(exc).__name__,
            exc,
            build_id,
            stage,
            feature_id,
        )


def _mode_c_subject_refusal(
    *,
    stage: StageClass,
    task_id: str | None,
    fix_task: FixTaskReference | None,
) -> str | None:
    """Return a refusal rationale when a fix-journey dispatch has no subject.

    Both fix-journey stages are dispatched *by subject*, and a subject-less
    dispatch is a wiring bug, not a runtime condition:

    * ``task-review`` reviews the build's own task — its subject is the
      Mode C ``task_id`` the queue put on the wire and on the build row.
    * ``task-work`` works one fix task — its subject is the
      ``fix_task_id`` on the reference the conductor threads through.

    Dispatching either with no subject would run the build system against
    whatever it inferred from the worktree — the cross-attribution risk
    the per-feature ``feature_id`` refusal already guards against. We take
    the same stance: refuse, structured, never guess.

    Returns:
        ``None`` when the dispatch has its subject; otherwise the rationale
        string for a FAILED result.
    """
    if stage is StageClass.TASK_REVIEW:
        if task_id is None or not str(task_id).strip():
            return (
                "fix-journey stage 'task-review' dispatched without task_id; "
                "refusing rather than review an inferred subject"
            )
        return None
    if stage is StageClass.TASK_WORK:
        fix_task_id = getattr(fix_task, "fix_task_id", None) if fix_task else None
        if fix_task_id is None or not str(fix_task_id).strip():
            return (
                "fix-journey stage 'task-work' dispatched without a fix-task "
                "reference; refusing rather than risk cross-fix-task "
                "attribution (the conductor threads it as dispatcher kwarg "
                "'fix_task')"
            )
        return None
    return None


def _build_argv_for_stage(
    *,
    stage: StageClass,
    build_id: str,
    correlation_id: str,
    feature_id: str | None,
    forward_context_builder: ForwardContextBuilder,
    task_id: str | None = None,
    fix_task: FixTaskReference | None = None,
    fix_task_yaml: str | None = None,
    forward_context: "Mapping[str, Any] | None" = None,
) -> _DispatchPlan:
    """Assemble the subprocess argv + extra_context_paths for ``stage``.

    Three argv shapes now live here.

    **The four planning stages** (unchanged, byte-for-byte): a
    ``--build-id`` / ``--correlation-id`` prefix, ``--feature-id`` on the
    per-feature ones, then the forward-context text pairs.

    **task-review** (the fix journey's opening and follow-up legs) — the
    subject is the build's Mode C task, exactly the shape the build-system
    adapter already runs (``guardkit task-review --task-id TASK-XXX``)::

        task-review --build-id <build> --correlation-id <cid>
                    --task-id <task_id>
                    [--feature-yaml <fix-task.yaml>]
                    <forward-context pairs…>

    **task-work** (one dispatch per fix task the review emitted) — the
    same shape with the *fix task* as the subject::

        task-work   --build-id <build> --correlation-id <cid>
                    --task-id <fix_task.fix_task_id>
                    [--feature-yaml <fix-task.yaml>]
                    <forward-context pairs…>

    ``--feature-yaml`` mirrors the queue's Mode C wire shape: ``forge
    queue --mode c TASK-XXX --feature-yaml <fix-task YAML>``. Threading it
    here keeps the dispatched subprocess looking at the same artefact the
    queue resolved instead of re-deriving it. It is optional: absent, the
    argv simply omits it.

    **No ``--parent-feature`` pair.** It was emitted here until Stage 2 and
    nothing in the estate accepts it: neither ``guardkit task-review`` nor
    ``guardkit task-work`` declares the flag, so a dispatch carrying it
    would have died on an unknown argument. The parent feature is resolved
    by the queue *out of the fix-task YAML*, by design
    (``_load_parent_feature_from_fix_task_yaml`` in ``cli/queue.py``) — so
    handing the subprocess ``--feature-yaml`` hands it the parent feature
    too, through the one artefact that owns it. One identifier, one source.

    ``--feature-id`` is deliberately NOT emitted for the fix-journey
    stages even when a ``feature_id`` is supplied — the fix journey
    dispatches by task subject, and a second identifier slot on the same
    argv is exactly the cross-attribution ambiguity the subject refusal
    guards against.

    **The forward context has ONE source per stage class, stated here.**

    * *Planning stages* — the injected ``forward_context_builder``, asked
      exactly as it always was. Byte-for-byte unchanged.
    * *Fix-journey stages* — the ``forward_context`` the CONDUCTOR built
      and threaded in. The conductor's
      :class:`~forge.pipeline.fix_task_context_builder.FixTaskContextBuilder`
      is an adapter over that same :class:`ForwardContextBuilder` which
      additionally reads the failed build's failure-pack index, and it
      resolves in Mode C with the fix-task reference — the only call
      shape that yields the review→work data dependency. Asking the
      builder again from here would resolve it a second time, in Mode A
      shape, and then silently drop the pack half: two sources, one of
      them wrong. So this function never re-derives a fix journey's
      context. With none supplied the dispatch carries no context entries
      and says so at INFO — honestly less context, never a guessed one.
    """
    subcommand = SUBPROCESS_STAGE_COMMANDS[stage]
    if forward_context is not None:
        entries = _entries_from_forward_context(forward_context)
    elif stage in MODE_C_STAGES:
        logger.info(
            "dispatch_subprocess_stage: no forward_context threaded for "
            "fix-journey stage=%s build_id=%s — dispatching with NO context "
            "entries. The conductor's fix_task_context_builder is the one "
            "source for this stage class; the dispatcher does not re-derive it",
            stage.value,
            build_id,
        )
        entries = []
    else:
        entries = forward_context_builder.build_for(
            stage=stage,
            build_id=build_id,
            feature_id=feature_id,
        )
    text_argv, path_args, warnings = _context_entries_to_argv(entries)
    argv: list[str] = [
        "--build-id",
        build_id,
        "--correlation-id",
        correlation_id,
    ]

    if stage in MODE_C_STAGES:
        # The fix journey's subject: the build's task for a review, the
        # fix task for a work dispatch. ``_mode_c_subject_refusal`` has
        # already guaranteed the subject is present by the time we get
        # here, so the lookups below cannot yield None.
        if stage is StageClass.TASK_WORK:
            subject = str(getattr(fix_task, "fix_task_id"))
        else:
            subject = str(task_id)
        argv.extend(["--task-id", subject])
        if fix_task_yaml is not None:
            argv.extend(["--feature-yaml", str(fix_task_yaml)])
    elif feature_id is not None:
        argv.extend(["--feature-id", feature_id])

    argv.extend(text_argv)
    return _DispatchPlan(
        subcommand=subcommand,
        args=argv,
        extra_context_paths=path_args,
        text_context_warnings=warnings,
    )


def _failed_result(
    *,
    stage: StageClass,
    build_id: str,
    feature_id: str | None,
    correlation_id: str,
    rationale: str,
    subcommand: str,
    duration_secs: float,
    exit_code: int = -1,
    artefact_paths: tuple[str, ...] = (),
) -> StageDispatchResult:
    """Construct a :class:`StageDispatchResult` in the FAILED state."""
    return StageDispatchResult(
        status=StageDispatchStatus.FAILED,
        stage=stage,
        build_id=build_id,
        feature_id=feature_id,
        correlation_id=correlation_id,
        artefact_paths=artefact_paths,
        rationale=rationale,
        exit_code=exit_code,
        duration_secs=duration_secs,
        subcommand=subcommand,
    )


def _truncate(text: str, *, limit: int = 4096) -> str:
    """Cap rationale strings so a runaway subprocess cannot blow stage_log.

    GuardKit subprocesses can emit megabytes of stderr on a hard failure;
    threading that verbatim into ``stage_log`` would balloon the SQLite
    row and make the audit trail unreadable. The first 4 KB carries
    enough context for diagnosis; the truncation marker preserves the
    tail-was-dropped fact so reviewers know to fetch the full log
    elsewhere if needed.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated; {len(text) - limit} more chars]"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def dispatch_subprocess_stage(
    stage: StageClass,
    build_id: str,
    *,
    correlation_id: str,
    repo_path: Path,
    read_allowlist: list[Path],
    forward_context_builder: ForwardContextBuilder,
    worktree_allowlist: WorktreeAllowlist,
    stage_log_writer: StageLogWriter,
    subprocess_runner: SubprocessRunner,
    feature_id: str | None = None,
    task_id: str | None = None,
    fix_task: FixTaskReference | None = None,
    fix_task_yaml: str | None = None,
    forward_context: Mapping[str, Any] | None = None,
    timeout_seconds: int = 600,
    with_nats_streaming: bool = True,
    extra_args: list[str] | None = None,
) -> StageDispatchResult:
    """Dispatch a Mode A subprocess stage and return a structured outcome.

    See module docstring for the full design rationale; this is the
    single public entry point. Every code path either returns a
    :class:`StageDispatchResult` or re-raises
    :class:`asyncio.CancelledError` — no other exception escapes.

    Args:
        stage: Consumer stage to dispatch. Must be a key of
            :data:`SUBPROCESS_STAGE_COMMANDS` — anything else is a
            programming error and raises :class:`ValueError` (the one
            documented exception to "never raises", and even that fires
            *before* any I/O so the caller can catch it locally).
        build_id: Build identifier. Threaded onto the subprocess command
            line and onto every ``stage_log`` row.
        correlation_id: Originating correlation ID. Threaded onto the
            subprocess command line as ``--correlation-id`` so the
            child process can echo it onto every pipeline event it
            publishes (Group I @data-integrity).
        repo_path: Working directory for the subprocess (the build's
            worktree root). Forwarded verbatim to ``subprocess_runner``;
            the runner enforces "absolute and inside ``read_allowlist``"
            on its own side.
        read_allowlist: Filesystem allowlist forwarded to the runner.
            Used by :func:`guardkit.run` for both ``cwd`` confinement
            and ``--context`` resolver filtering.
        forward_context_builder: TASK-MAG7-006 builder. Asked once per
            dispatch for the ``--context`` entries; all approval and
            input-side allowlist filtering happens inside it.
        worktree_allowlist: TASK-MAG7-006 allowlist. Used here only for
            *output*-side defence-in-depth — re-checking artefact paths
            the subprocess returns. Production typically wires the same
            instance threaded into ``forward_context_builder``; the
            dispatcher accepts it explicitly so the two checks remain
            independent in unit tests.
        stage_log_writer: Sink for the dispatch row.
        subprocess_runner: Async runner with the
            :func:`forge.adapters.guardkit.run` shape. Tests inject a
            fake; production wires the real coroutine.
        feature_id: ``None`` for non-per-feature stages
            (``SYSTEM_ARCH``, ``SYSTEM_DESIGN``); the feature ID for
            per-feature stages (``FEATURE_SPEC``, ``FEATURE_PLAN``).
            Per-feature stages called without a ``feature_id`` are
            refused with a structured FAILED result — the same
            safe-default stance :class:`StageOrderingGuard` takes.
            The two fix-journey stages carry ``None`` here: their
            identifier is ``task_id`` / ``fix_task``.
        task_id: Fix journey only — the build's Mode C task identifier
            (the ``TASK-XXX`` positional the queue put on the wire).
            It is the ``--task-id`` subject of every ``task-review``
            dispatch, initial and follow-up. Required for
            ``TASK_REVIEW``; ignored by every other stage.
        fix_task: Fix journey only — the :class:`FixTaskReference` the
            conductor threads through as the ``fix_task`` dispatcher
            kwarg. Its ``fix_task_id`` is the ``--task-id`` subject of a
            ``task-work`` dispatch. Required for ``TASK_WORK``; ignored
            by every other stage. A ``TASK_WORK`` dispatch without one is
            refused with a structured FAILED result rather than run
            against an inferred subject.
        fix_task_yaml: Fix journey only — path to the fix-task YAML the
            build was queued with (``--feature-yaml`` on the queue
            surface). Emitted as ``--feature-yaml`` when supplied. This is
            also how the parent feature reaches the subprocess: the queue
            resolves ``parent_feature`` *out of this YAML* by design, so
            the artefact carries it and no second flag is needed (there is
            no ``--parent-feature`` — nothing in the estate accepts one).
        forward_context: Fix journey only — the context the conductor
            already built for this dispatch
            (:class:`~forge.pipeline.fix_task_context_builder.FixTaskContextBuilder`:
            the review→work entries PLUS the failed build's failure-pack
            index). When supplied it is THE source of context for this
            dispatch and ``forward_context_builder`` is not consulted —
            one source of truth, because the conductor's builder is an
            adapter over that very builder and asking both would drop the
            pack half on the floor.
        timeout_seconds: Forwarded to the runner. Defaults to the
            FEAT-FORGE-005 600-second contract (ASSUM-001).
        with_nats_streaming: Forwarded to the runner. Defaults to
            ``True`` so the GuardKit subprocess publishes
            ``pipeline.stage-complete.*`` progress (ASSUM-005, AC-005).
        extra_args: Caller-supplied positional args / flags appended
            after the dispatcher's own boilerplate args. Defaults to
            ``None`` (no extras). Useful for callers that need to thread
            through optional flags (``--retry``, etc.) without forcing
            the dispatcher to know about them.

    Returns:
        :class:`StageDispatchResult` capturing the outcome. The
        ``status`` discriminator tells the supervisor what to do next;
        the ``rationale`` carries the human-readable detail.

    Raises:
        ValueError: If ``stage`` is not in
            :data:`SUBPROCESS_STAGE_COMMANDS`. This is a programming
            error, not a runtime condition — the six-stage contract is
            part of the dispatcher's API surface, not data-driven.
        asyncio.CancelledError: Re-raised so the caller's async context
            unwinds correctly. Mirrors the
            :func:`forge.adapters.guardkit.run` contract.
    """
    if stage not in SUBPROCESS_STAGE_COMMANDS:
        raise ValueError(
            f"dispatch_subprocess_stage: stage={stage!r} is not a subprocess "
            f"stage; expected one of {sorted(SUBPROCESS_STAGE_COMMANDS)}"
        )
    subcommand = SUBPROCESS_STAGE_COMMANDS[stage]

    # Per-feature consumer + missing feature_id is a misuse. We refuse
    # rather than dispatch with feature_id=None and risk cross-feature
    # bleed — same stance ForwardContextBuilder and StageOrderingGuard
    # take. Surface it as a structured FAILED so the supervisor sees a
    # uniform shape across "the dispatcher refused" and "the subprocess
    # itself failed".
    # Two subject refusals, one shape. The per-feature one has always
    # been here; the fix-journey one is its Stage-1a twin (design pass
    # §a.4) — ``PER_FEATURE_STAGES`` deliberately excludes TASK_REVIEW /
    # TASK_WORK, so without this check a subject-less fix-journey
    # dispatch would sail straight through.
    subject_refusal: str | None = None
    if stage in PER_FEATURE_STAGES and feature_id is None:
        subject_refusal = (
            f"per-feature stage {stage.value!r} dispatched without feature_id; "
            "refusing rather than risk cross-feature artefact attribution"
        )
    else:
        subject_refusal = _mode_c_subject_refusal(
            stage=stage,
            task_id=task_id,
            fix_task=fix_task,
        )

    if subject_refusal is not None:
        rationale = subject_refusal
        result = _failed_result(
            stage=stage,
            build_id=build_id,
            feature_id=None,
            correlation_id=correlation_id,
            rationale=rationale,
            subcommand=subcommand,
            duration_secs=0.0,
        )
        _record_safely(
            stage_log_writer,
            build_id=build_id,
            stage=stage,
            feature_id=None,
            correlation_id=correlation_id,
            status=result.status,
            artefact_paths=result.artefact_paths,
            rationale=rationale,
            exit_code=result.exit_code,
            duration_secs=result.duration_secs,
        )
        logger.warning("dispatch_subprocess_stage: %s", rationale)
        return result

    started_at = time.monotonic()

    try:
        plan = _build_argv_for_stage(
            stage=stage,
            build_id=build_id,
            correlation_id=correlation_id,
            feature_id=feature_id,
            forward_context_builder=forward_context_builder,
            task_id=task_id,
            fix_task=fix_task,
            fix_task_yaml=fix_task_yaml,
            forward_context=forward_context,
        )
        full_args = list(plan.args)
        if extra_args:
            full_args.extend(extra_args)

        runner_kwargs: dict[str, Any] = {
            "subcommand": plan.subcommand,
            "args": full_args,
            "repo_path": repo_path,
            "read_allowlist": read_allowlist,
            "timeout_seconds": timeout_seconds,
            "with_nats_streaming": with_nats_streaming,
            "extra_context_paths": plan.extra_context_paths or None,
        }

        guardkit_result = await subprocess_runner(**runner_kwargs)

    except asyncio.CancelledError:
        # Single deliberate exception — propagate so the caller's task
        # unwinds. Do NOT write a stage_log row here: the build is
        # being torn down and the row would be misleading.
        raise

    except Exception as exc:  # noqa: BLE001 — universal error contract
        # Any runner-shape mismatch or pre-runner programming error
        # surfaces here. Convert to FAILED with a structured rationale
        # rather than letting it escape — the dispatcher's contract is
        # "never raise past the boundary" (TASK-MAG7-008 AC).
        duration_secs = time.monotonic() - started_at
        rationale = _truncate(
            f"internal error before/during subprocess dispatch: "
            f"{type(exc).__name__}: {exc}"
        )
        result = _failed_result(
            stage=stage,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            rationale=rationale,
            subcommand=subcommand,
            duration_secs=duration_secs,
        )
        logger.exception(
            "dispatch_subprocess_stage: internal error build_id=%s stage=%s",
            build_id,
            stage,
        )
        _record_safely(
            stage_log_writer,
            build_id=build_id,
            stage=stage,
            feature_id=feature_id,
            correlation_id=correlation_id,
            status=result.status,
            artefact_paths=(),
            rationale=rationale,
            exit_code=-1,
            duration_secs=duration_secs,
        )
        return result

    # ---------------------------------------------------------------------
    # Post-runner: filter artefact paths, record stage_log, build result
    # ---------------------------------------------------------------------
    duration_secs = guardkit_result.duration_secs
    kept, rejected = _filter_artefact_paths(
        paths=list(guardkit_result.artefacts),
        build_id=build_id,
        allowlist=worktree_allowlist,
        subcommand=plan.subcommand,
    )

    # Decide the final status.
    runner_failed = guardkit_result.status != "success"
    allowlist_failure = bool(rejected)

    if runner_failed or allowlist_failure:
        status = StageDispatchStatus.FAILED
        rationale_parts: list[str] = []

        if runner_failed:
            # Group C @negative: surface the failed-spec rationale on
            # /feature-spec failure so the supervisor halts the inner
            # loop for that feature. We thread guardkit's stderr +
            # status verbatim — that IS the rationale.
            stderr_tail = guardkit_result.stderr or ""
            stdout_tail = guardkit_result.stdout_tail or ""
            failure_summary = (
                f"subprocess {plan.subcommand} returned status="
                f"{guardkit_result.status!r} exit_code="
                f"{guardkit_result.exit_code}"
            )
            if stage is StageClass.FEATURE_SPEC:
                # Feature-spec failure — flag explicitly so the
                # supervisor can pattern-match the rationale.
                failure_summary = (
                    "feature-spec failure: " + failure_summary
                )
            detail_blocks: list[str] = [failure_summary]
            if stderr_tail.strip():
                detail_blocks.append(f"stderr: {stderr_tail.strip()}")
            elif stdout_tail.strip():
                detail_blocks.append(f"stdout: {stdout_tail.strip()}")
            for w in guardkit_result.warnings:
                detail_blocks.append(f"warning[{w.code}]: {w.message}")
            rationale_parts.append("\n".join(detail_blocks))

        if allowlist_failure:
            rationale_parts.append(
                "rejected artefact paths outside worktree allowlist: "
                + ", ".join(rejected)
            )

        if plan.text_context_warnings:
            rationale_parts.extend(plan.text_context_warnings)

        rationale = _truncate("\n".join(rationale_parts))
    else:
        status = StageDispatchStatus.SUCCESS
        rationale = (
            f"{plan.subcommand} completed in {duration_secs:.2f}s "
            f"with {len(kept)} artefact path(s)"
        )

    # THE FINDINGS SURVIVE THE DISPATCH (LI stage-2 §5). The parser has
    # always recovered the ``## Detection Findings`` block; every consumer
    # of a dispatch result has always received a result that had thrown it
    # away. That is the whole reason the fix journey had no dedup key.
    # Copied into plain dicts so a runner double cannot hand back a live
    # mutable view into its own state.
    #
    # ``guardkit_result.detection_findings`` is three-valued and stays that
    # way across this seam: ``None`` = the block was absent or would not
    # parse (the parser folds a JSONDecodeError into a warning and answers
    # None), ``[]`` = the block was there and empty, which is a CLEAN
    # REVIEW. Flattening those two into "no findings" is what would turn
    # the journey's success path into a no-progress stop.
    raw_findings = guardkit_result.detection_findings
    detection_findings_reported = raw_findings is not None
    detection_findings = tuple(
        dict(item) for item in (raw_findings or ()) if isinstance(item, Mapping)
    )

    result = StageDispatchResult(
        status=status,
        stage=stage,
        build_id=build_id,
        feature_id=feature_id,
        correlation_id=correlation_id,
        artefact_paths=kept,
        rationale=rationale,
        exit_code=guardkit_result.exit_code,
        duration_secs=duration_secs,
        subcommand=plan.subcommand,
        detection_findings=detection_findings,
        detection_findings_reported=detection_findings_reported,
    )

    _record_safely(
        stage_log_writer,
        build_id=build_id,
        stage=stage,
        feature_id=feature_id,
        correlation_id=correlation_id,
        status=result.status,
        artefact_paths=result.artefact_paths,
        rationale=result.rationale,
        exit_code=result.exit_code,
        duration_secs=result.duration_secs,
        detection_findings=result.detection_findings,
        detection_findings_reported=result.detection_findings_reported,
    )

    return result
