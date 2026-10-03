"""Forward-propagation context builder for FEAT-FORGE-007 Mode A (TASK-MAG7-006).

This module defines :class:`ForwardContextBuilder`, the *only* place in the
Mode A pipeline that crosses the boundary from "the build's recorded
history" (``stage_log``) to "what gets passed to a downstream subprocess"
(``--context`` flag values on the next stage's dispatch).

It implements forward propagation per the
:data:`forge.pipeline.forward_propagation.PROPAGATION_CONTRACT` defined by
TASK-MAG7-002 and mitigates Risk R-5 ("forward-propagation context builder
leaks unapproved or stale artefacts") by centralising two read-side filters
in one auditable place:

1. *Approved-only*: a downstream stage receives context only from
   ``stage_log`` rows whose ``gate_decision == 'approved'``. In-progress
   and flagged-for-review rows are invisible to the builder by Protocol
   contract — :meth:`StageLogReader.get_approved_stage_entry` returns
   ``None`` for any row that has not reached the terminal-approved state.
2. *Worktree allowlisted*: every artefact path is checked against the
   build's :class:`WorktreeAllowlist` before it is threaded into a
   ``--context`` value. Paths that fall outside the worktree are filtered
   out individually and a structured warning is logged. This is the
   defence-in-depth twin of the FEAT-FORGE-005 allowlist that the build
   subprocess applies on its own side.

Per-feature scoping (FEAT-FORGE-007 ASSUM-001) is handled by passing
``feature_id`` through to the reader for any consumer or producer stage
in :data:`~forge.pipeline.stage_taxonomy.PER_FEATURE_STAGES`. A consumer
that is per-feature but called without a ``feature_id`` is treated as a
misuse and yields an empty context list — the same "refuse rather than
dispatch cross-feature" stance taken by
:class:`forge.pipeline.stage_ordering_guard.StageOrderingGuard`
(TASK-MAG7-003).

Architecture notes
------------------

The builder is I/O-thin: it owns no state across calls and depends only
on the two reader Protocols (:class:`StageLogReader`,
:class:`WorktreeAllowlist`) declared in this module. Production wires the
FEAT-FORGE-001 SQLite adapter for the stage_log reader and the
FEAT-FORGE-005 allowlist for the worktree gate; tests inject in-memory
fakes — the same shape TASK-MAG7-003 and TASK-MAG7-005 use.

References:
    - TASK-MAG7-006 — this task brief.
    - TASK-MAG7-001 — :mod:`forge.pipeline.stage_taxonomy`
      (``StageClass``, ``PER_FEATURE_STAGES``).
    - TASK-MAG7-002 — :mod:`forge.pipeline.forward_propagation`
      (``PROPAGATION_CONTRACT``, ``ContextRecipe``).
    - FEAT-FORGE-007 Group A scenarios — forward propagation of
      product-owner / architect outputs to the next stage's dispatch.
    - FEAT-FORGE-005 — worktree allowlist (production side).
    - FEAT-FORGE-001 — SQLite ``stage_log`` adapter (production reader).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from forge.lifecycle.modes import BuildMode
from forge.pipeline.forward_propagation import PROPAGATION_CONTRACT, ContextRecipe
from forge.pipeline.mode_chains_data import MODE_B_FORBIDDEN_STAGES
from forge.pipeline.stage_taxonomy import PER_FEATURE_STAGES, StageClass

__all__ = [
    "ApprovedStageEntry",
    "ContextEntry",
    "FixTaskRef",
    "ForwardContextBuilder",
    "MODE_B_PROPAGATION_CONTRACT",
    "ModeBoundaryViolation",
    "StageLogReader",
    "WorktreeAllowlist",
]

logger = logging.getLogger(__name__)


#: The two artefact shapes a single :class:`ContextEntry` can carry.
#:
#: A producer recipe of ``"path-list"`` is expanded into one
#: :class:`ContextEntry` per path, each tagged ``"path"``; the only kinds
#: that escape :meth:`ForwardContextBuilder.build_for` are therefore
#: ``"text"`` and ``"path"``.
ContextEntryKind = Literal["text", "path"]


@dataclass(frozen=True, slots=True)
class ApprovedStageEntry:
    """The minimal stage_log row shape the builder consumes.

    Production's FEAT-FORGE-001 ``stage_log`` row carries a great deal
    more (timestamps, coach scores, gate metadata, …); this dataclass
    pins the *only* fields the builder is allowed to read so the
    :class:`StageLogReader` Protocol stays narrow.

    Attributes:
        gate_decision: Always ``"approved"`` — by Protocol contract the
            reader never returns rows in any other gate state. The field
            is preserved on the dataclass so downstream consumers (and
            tests) can introspect it without re-querying SQLite.
        artefact_paths: Filesystem paths the producer stage emitted.
            Empty tuple for ``"text"``-kind producers. Single-element
            tuple for ``"path"``. Multiple elements for ``"path-list"``.
        artefact_text: Inline text payload for ``"text"``-kind
            producers (charters, approved-output blobs, etc.). ``None``
            for path-shaped producers.
    """

    gate_decision: str
    artefact_paths: tuple[str, ...] = ()
    artefact_text: str | None = None


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """A single ``--context`` flag invocation on the downstream dispatch.

    The downstream dispatcher (specialist dispatch or GuardKit
    subprocess) receives a list of these and turns each one into one
    ``--<flag> <value>`` pair on its child-process argv. Inline-text
    payloads (``kind="text"``) are passed verbatim; path payloads
    (``kind="path"``) are passed as filesystem paths the child process
    will open.

    Attributes:
        flag: CLI flag name, e.g. ``"--context"``. Sourced from
            :attr:`ContextRecipe.context_flag`.
        value: The flag value — either an inline text payload or a
            filesystem path.
        kind: Discriminator so the dispatcher knows whether ``value`` is
            inline text or a path it should resolve.
    """

    flag: str
    value: str
    kind: ContextEntryKind


@runtime_checkable
class StageLogReader(Protocol):
    """Read-side Protocol over the FEAT-FORGE-001 ``stage_log`` table.

    The builder needs two queries:

    * :meth:`get_approved_stage_entry` — "give me the *approved* row for
      ``(build_id, stage, feature_id)``, or ``None`` if no such row exists".
    * :meth:`get_all_approved_stage_entries` — "give me every approved
      row for ``(build_id, stage, feature_id)`` in dispatch order".
      Required for Mode C follow-up ``/task-review`` (TASK-MBC8-005), which
      must surface every completed ``/task-work`` artefact in the cycle.

    By Protocol contract both methods filter ``gate_decision == 'approved'``
    *internally* — the builder cannot accidentally widen the filter because
    there is no place to put an ``include_unapproved`` flag.

    Production wires the FEAT-FORGE-001 SQLite adapter; tests use an
    in-memory fake.
    """

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:  # pragma: no cover - protocol stub
        """Return the approved stage_log entry, or ``None``.

        Args:
            build_id: Build the row is scoped to.
            stage: Stage class whose approved row is requested.
            feature_id: ``None`` for non-per-feature stages; the
                feature identifier for stages in
                :data:`~forge.pipeline.stage_taxonomy.PER_FEATURE_STAGES`.

        Returns:
            The :class:`ApprovedStageEntry` if a row exists with
            ``gate_decision='approved'`` for the given scope; ``None``
            otherwise (no row yet, or row exists in any non-approved
            gate state — both look the same to callers, which is the
            point).
        """
        ...

    def get_all_approved_stage_entries(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> Sequence[ApprovedStageEntry]:  # pragma: no cover - protocol stub
        """Return every approved stage_log entry for the given scope.

        The Mode A and Mode B contracts only ever require *one* approved
        row per (build, stage, feature) — :meth:`get_approved_stage_entry`
        is sufficient. Mode C is the first contract that needs a list:
        the follow-up ``/task-review`` must receive the artefact paths
        from *every* completed ``/task-work`` in the cycle (TASK-MBC8-005
        AC-005).

        Args:
            build_id: Build the rows are scoped to.
            stage: Stage class whose approved rows are requested.
            feature_id: ``None`` for non-per-feature stages; the
                feature identifier for stages in
                :data:`~forge.pipeline.stage_taxonomy.PER_FEATURE_STAGES`.

        Returns:
            Sequence of approved entries in dispatch (insertion) order.
            Empty sequence if no approved rows exist for the scope. The
            sequence may be empty even if non-approved rows exist — they
            are filtered out by the same approved-only Protocol contract
            as :meth:`get_approved_stage_entry`.
        """
        ...


@runtime_checkable
class WorktreeAllowlist(Protocol):
    """Defence-in-depth filesystem allowlist for the build's worktree.

    Mirrors the FEAT-FORGE-005 production allowlist surface. The builder
    invokes :meth:`is_allowed` once per artefact path before threading it
    into a ``--context`` value; paths that fail the check are filtered
    out individually with a structured warning rather than raising. This
    matches the AC-004 / AC-008 contract: "Refuses to return any entry
    whose underlying artefact path falls outside the build's worktree
    allowlist".
    """

    def is_allowed(
        self, build_id: str, path: str
    ) -> bool:  # pragma: no cover - protocol stub
        """Return ``True`` iff ``path`` lies inside ``build_id``'s worktree.

        Args:
            build_id: Build whose worktree allowlist to consult.
            path: Filesystem path to gate.

        Returns:
            ``True`` iff the path is safe to thread onto a downstream
            ``--context`` flag, ``False`` otherwise (path escapes the
            worktree, or the build has no recorded worktree root).
        """
        ...


class ModeBoundaryViolation(ValueError):
    """Raised when forward context is requested for a stage forbidden in the mode.

    Mode B (FEAT-FORGE-008 ASSUM-013, ASSUM-014) forbids the four
    pre-``feature-spec`` Mode A stages
    (:data:`~forge.pipeline.mode_chains_data.MODE_B_FORBIDDEN_STAGES`).
    Asking the builder to thread a ``--context`` flag onto a ``/system-arch``
    or ``/architect`` dispatch under Mode B is a planner bug — the planner
    should never have selected such a stage in the first place. Raising
    here (rather than returning ``[]``) gives that bug a loud, attributable
    failure mode instead of a silent empty-context dispatch.

    The exception is also raised by the Mode B chain planner
    (TASK-MBC8-003) for the same forbidden-stage condition; both raise
    sites use this single exception type so callers can ``except`` once.

    Attributes:
        stage: The stage class that triggered the violation.
        mode: The :class:`BuildMode` whose contract forbids the stage.
    """

    def __init__(self, stage: StageClass, mode: BuildMode) -> None:
        self.stage = stage
        self.mode = mode
        super().__init__(
            f"Stage {stage.value!r} is forbidden in {mode.value!r}; "
            "no forward context can be built for it."
        )


@dataclass(frozen=True, slots=True)
class FixTaskRef:
    """Reference to a Mode C fix task being dispatched (TASK-MBC8-005 AC-004).

    Carries the data dependency between a ``/task-review`` decision and
    the ``/task-work`` dispatch that acts on it
    (FEAT-FORGE-008 IMPLEMENTATION-GUIDE.md §4 cross-task data dependency):

    * ``fix_task_id`` — the unique identifier the reviewer assigned to the
      fix task. The ``/task-work`` dispatcher uses it to scope its outputs
      and to emit ``stage_log`` rows tagged with the originating fix task
      (Group L lineage).
    * ``task_review_entry_id`` — back-reference to the ``stage_log``
      ``entry_id`` of the originating ``/task-review`` row. This is the
      audit anchor that lets the lineage scenarios in Group L resolve
      "which review produced this fix task".
    * ``review_artefact_paths`` — the artefact paths the originating
      ``/task-review`` emitted. The Mode C ``ForwardContextBuilder``
      threads each (after allowlist gating) onto a ``--context`` flag
      so the ``/task-work`` dispatch sees the review's findings verbatim.

    The dataclass is frozen + slotted to mirror :class:`ApprovedStageEntry`
    and :class:`ContextEntry` — it is a value object, never mutated.

    Note:
        Per the TASK-MBC8-005 implementation note, the canonical home for
        this dataclass is alongside ``ModeCCyclePlanner`` (TASK-MBC8-004).
        It lives here for now so this task can be merged independently of
        the planner; once TASK-MBC8-004 lands, the planner module should
        re-export this symbol verbatim and downstream callers should import
        it from there. This module retains it under ``__all__`` so the
        re-export migration is purely additive.
    """

    fix_task_id: str
    task_review_entry_id: str
    review_artefact_paths: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Serialise to a stable JSON string for ``--fix-task`` flag values.

        Keys are emitted in sorted order so the same :class:`FixTaskRef`
        always serialises to the same byte string — important for log
        diffing and for the Group L lineage scenarios that may compare
        the rendered flag value across stages.
        """
        return json.dumps(
            {
                "fix_task_id": self.fix_task_id,
                "task_review_entry_id": self.task_review_entry_id,
                "review_artefact_paths": list(self.review_artefact_paths),
            },
            sort_keys=True,
        )


#: Mode B propagation contract (FEAT-FORGE-008 TASK-MBC8-005).
#:
#: A strict subset of :data:`~forge.pipeline.forward_propagation.PROPAGATION_CONTRACT`
#: that covers only Mode B's four stages
#: (:data:`~forge.pipeline.mode_chains_data.MODE_B_CHAIN`):
#:
#:     1. FEATURE_PLAN        ← FEATURE_SPEC   (path)
#:     2. AUTOBUILD           ← FEATURE_PLAN   (path)
#:     3. PULL_REQUEST_REVIEW ← AUTOBUILD      (text)
#:
#: ``FEATURE_SPEC`` is the Mode B entry stage and is intentionally
#: omitted as a key (mirrors the Mode A convention where
#: ``PRODUCT_OWNER`` is omitted because it has no upstream artefact).
#:
#: The recipes themselves are *equivalent* to the matching Mode A rows —
#: Mode B inherits Mode A's ``feature-spec → feature-plan → autobuild →
#: pull-request-review`` suffix verbatim — but the descriptions are
#: tagged ``(Mode B)`` so audit logs make the propagation mode obvious
#: when a build runs in Mode B.
MODE_B_PROPAGATION_CONTRACT: dict[StageClass, ContextRecipe] = {
    StageClass.FEATURE_PLAN: ContextRecipe(
        producer_stage=StageClass.FEATURE_SPEC,
        artefact_kind="path",
        context_flag="--context",
        description="(Mode B) feature-spec artefact path",
    ),
    StageClass.AUTOBUILD: ContextRecipe(
        producer_stage=StageClass.FEATURE_PLAN,
        artefact_kind="path",
        context_flag="--context",
        description="(Mode B) feature-plan artefact path",
    ),
    StageClass.PULL_REQUEST_REVIEW: ContextRecipe(
        producer_stage=StageClass.AUTOBUILD,
        artefact_kind="text",
        context_flag="--context",
        description="(Mode B) autobuild branch ref + commit summary",
    ),
}


#: Per-mode contract dispatch table.
#:
#: ``ForwardContextBuilder.build_for`` consults this map to choose the
#: contract for the requested ``mode``. Mode C is intentionally absent —
#: its ``TASK_WORK`` and follow-up ``TASK_REVIEW`` shapes are not
#: single-producer-recipe driven (they need a :class:`FixTaskRef` and a
#: list of approved ``TASK_WORK`` entries respectively) and are handled
#: by dedicated branches in the builder.
_CONTRACT_BY_MODE: dict[BuildMode, dict[StageClass, ContextRecipe]] = {
    BuildMode.MODE_A: PROPAGATION_CONTRACT,
    BuildMode.MODE_B: MODE_B_PROPAGATION_CONTRACT,
}


class ForwardContextBuilder:
    """Builds the ``--context`` entries for the next stage's dispatch.

    The builder is the single read-side seam between
    :data:`~forge.pipeline.forward_propagation.PROPAGATION_CONTRACT` (the
    declarative producer-to-consumer wiring) and the downstream stage's
    dispatcher (the executable subprocess invocation). All approval and
    allowlist filtering happens here, so adding a new stage is purely a
    matter of registering a :class:`ContextRecipe` row — no new
    enforcement code is required.

    Args:
        stage_log_reader: Reader over the ``stage_log`` table; only ever
            returns rows with ``gate_decision='approved'``.
        worktree_allowlist: Filesystem-path gate over the build's
            worktree. Path artefacts are filtered through it; text
            artefacts bypass it (they have no filesystem path to gate).

    Example:
        >>> builder = ForwardContextBuilder(reader, allowlist)
        >>> builder.build_for(
        ...     stage=StageClass.AUTOBUILD,
        ...     build_id="build-FEAT-X-20260426",
        ...     feature_id="FEAT-1",
        ... )
        [ContextEntry(flag='--context', value='/work/.../plan.md', kind='path')]
    """

    def __init__(
        self,
        stage_log_reader: StageLogReader,
        worktree_allowlist: WorktreeAllowlist,
    ) -> None:
        self._reader = stage_log_reader
        self._allowlist = worktree_allowlist

    def widened(
        self, widen: Callable[[WorktreeAllowlist], WorktreeAllowlist]
    ) -> "ForwardContextBuilder":
        """The same builder with its allowlist widened by ``widen`` — for one
        build whose tree is in a sandbox, at a path the configured roots do
        not name (3 October 2026)."""
        return ForwardContextBuilder(self._reader, widen(self._allowlist))

    def build_for(
        self,
        stage: StageClass,
        build_id: str,
        feature_id: str | None,
        *,
        mode: BuildMode | None = None,
        fix_task: FixTaskRef | None = None,
    ) -> list[ContextEntry]:
        """Return the ``--context`` entries to thread into ``stage``'s dispatch.

        Mode A resolution order (unchanged from TASK-MAG7-006):

        1. Look up :data:`PROPAGATION_CONTRACT[stage]` to find the
           producer stage and the artefact recipe. Stages with no
           recipe entry (only ``PRODUCT_OWNER`` today, the entry stage)
           return an empty list — there is no upstream to propagate.
        2. Refuse early for per-feature consumers called without a
           ``feature_id`` — this is the same safe-default stance
           :class:`StageOrderingGuard` takes for misuse.
        3. Ask the :class:`StageLogReader` for the producer's approved
           row. ``None`` (no approved row yet) → empty list. This is
           how AC-007 ("in-progress prior stage → empty context") is
           enforced: the reader will not surface a non-approved row.
        4. Convert the row into one or more :class:`ContextEntry`
           values according to the recipe's ``artefact_kind``:
           ``"text"`` → one entry from ``artefact_text``;
           ``"path"`` → one entry from ``artefact_paths[0]``;
           ``"path-list"`` → one entry per element of ``artefact_paths``.
        5. For path-shaped entries, run each path through
           :meth:`WorktreeAllowlist.is_allowed`; rejects are dropped
           with a structured ``WARNING`` log line that names the path
           and the build (AC-004 / AC-008).

        Mode B resolution (FEAT-FORGE-008 TASK-MBC8-005):

        * Stages in :data:`~forge.pipeline.mode_chains_data.MODE_B_FORBIDDEN_STAGES`
          raise :class:`ModeBoundaryViolation` — the planner should never
          have selected such a stage in the first place.
        * Otherwise the contract from :data:`MODE_B_PROPAGATION_CONTRACT`
          is consulted. The shape of each row mirrors the matching Mode A
          row, so resolution steps 2–5 above apply unchanged.

        Mode C resolution (FEAT-FORGE-008 TASK-MBC8-005):

        * ``TASK_WORK`` requires a :class:`FixTaskRef` (Group A — each
          ``/task-work`` is dispatched with the fix-task definition
          produced by ``/task-review``). The builder emits one
          ``--fix-task`` text entry carrying ``fix_task.to_json()`` plus
          one ``--context`` path entry per allow-listed
          ``review_artefact_paths`` element (Group L lineage).
        * ``TASK_REVIEW`` (the follow-up review at the end of a Mode C
          cycle) consumes every approved ``/task-work`` row for the
          build via
          :meth:`StageLogReader.get_all_approved_stage_entries`, emitting
          one ``--context`` path entry per allow-listed artefact path.
        * Other Mode C stages currently return ``[]`` — they are not
          covered by the TASK-MBC8-005 acceptance criteria and will be
          wired up by the relevant follow-up tasks
          (e.g. ``PULL_REQUEST_REVIEW`` Mode C handling lives with the
          mode-c terminal handlers in TASK-MBC8-007).

        Args:
            stage: Consumer stage whose dispatch is about to fire.
            build_id: Build identifier
                (``build-{feature_id}-{YYYYMMDDHHMMSS}``).
            feature_id: Feature identifier for per-feature consumers;
                ``None`` for non-per-feature consumers. Misuse
                (per-feature consumer + ``feature_id=None``) yields
                an empty list.
            mode: Build mode whose propagation contract to apply.
                Defaults to ``None`` which is treated as Mode A — this
                preserves the TASK-MAG7-006 callsite signature byte for
                byte so existing Mode A callers see no behaviour change.
            fix_task: Required when ``mode is BuildMode.MODE_C`` and
                ``stage is StageClass.TASK_WORK``. Carries the fix-task
                identifier, originating ``/task-review`` ``entry_id``,
                and review artefact paths to thread onto the dispatch.
                Ignored for every other (mode, stage) combination.

        Raises:
            ModeBoundaryViolation: Mode B builder asked for a stage in
                :data:`~forge.pipeline.mode_chains_data.MODE_B_FORBIDDEN_STAGES`.

        Returns:
            Ordered list of :class:`ContextEntry` values. Order matches
            the producer's ``artefact_paths`` for path-list recipes so
            downstream tools can rely on stable argv ordering.
        """
        effective_mode = BuildMode.MODE_A if mode is None else mode

        if effective_mode is BuildMode.MODE_C:
            return self._build_for_mode_c(
                stage=stage,
                build_id=build_id,
                fix_task=fix_task,
            )

        # Mode A and Mode B share the recipe-driven resolution path; the
        # only differences are the contract map and the forbidden-stage
        # guard for Mode B.
        if effective_mode is BuildMode.MODE_B and stage in MODE_B_FORBIDDEN_STAGES:
            raise ModeBoundaryViolation(stage=stage, mode=BuildMode.MODE_B)

        contract = _CONTRACT_BY_MODE[effective_mode]
        recipe = contract.get(stage)
        if recipe is None:
            # Entry stage (PRODUCT_OWNER for Mode A, FEATURE_SPEC for
            # Mode B) or any other stage without a propagation row —
            # nothing to thread.
            logger.debug(
                "forward_context_builder: no contract entry for stage=%s "
                "under mode=%s; returning empty context",
                stage,
                effective_mode,
            )
            return []

        # Per-feature consumer + missing feature_id is a misuse.
        if stage in PER_FEATURE_STAGES and feature_id is None:
            logger.warning(
                "forward_context_builder: per-feature stage=%s called without "
                "feature_id (build_id=%s, mode=%s); refusing to thread "
                "cross-feature context",
                stage,
                build_id,
                effective_mode,
            )
            return []

        producer_feature_id = (
            feature_id if recipe.producer_stage in PER_FEATURE_STAGES else None
        )
        approved = self._reader.get_approved_stage_entry(
            build_id=build_id,
            stage=recipe.producer_stage,
            feature_id=producer_feature_id,
        )
        if approved is None:
            # Producer not yet approved — AC-007 says empty context.
            logger.debug(
                "forward_context_builder: producer=%s not approved for "
                "build_id=%s feature_id=%s mode=%s; returning empty context",
                recipe.producer_stage,
                build_id,
                producer_feature_id,
                effective_mode,
            )
            return []

        return self._materialise_entries(
            recipe=recipe,
            approved=approved,
            build_id=build_id,
        )

    # ------------------------------------------------------------------
    # Mode C — review/fix cycle handling
    # ------------------------------------------------------------------

    def _build_for_mode_c(
        self,
        stage: StageClass,
        build_id: str,
        fix_task: FixTaskRef | None,
    ) -> list[ContextEntry]:
        """Dispatch Mode C stages to the right specialised builder.

        ``TASK_WORK`` and ``TASK_REVIEW`` are the two Mode C contracts
        TASK-MBC8-005 covers. Every other stage returns ``[]`` — Mode C
        terminal stages (e.g. ``PULL_REQUEST_REVIEW``) are not in scope
        for this task and will be wired up by TASK-MBC8-007.
        """
        if stage is StageClass.TASK_WORK:
            return self._build_mode_c_task_work(
                build_id=build_id,
                fix_task=fix_task,
            )
        if stage is StageClass.TASK_REVIEW:
            return self._build_mode_c_followup_review(build_id=build_id)
        logger.debug(
            "forward_context_builder: Mode C does not currently propagate "
            "context for stage=%s (build_id=%s); returning empty list",
            stage,
            build_id,
        )
        return []

    def _build_mode_c_task_work(
        self,
        build_id: str,
        fix_task: FixTaskRef | None,
    ) -> list[ContextEntry]:
        """Mode C ``TASK_WORK`` dispatch: ``--fix-task`` + review paths.

        Per TASK-MBC8-005 AC-004:

        * One ``--fix-task`` text entry whose value is
          :meth:`FixTaskRef.to_json` (the audit-anchor payload from Group L).
        * One ``--context`` path entry per allow-listed element of
          ``fix_task.review_artefact_paths`` so the ``/task-work``
          dispatch sees the originating review's findings verbatim.

        A ``None`` ``fix_task`` is a planner bug (Mode C TASK_WORK is
        always dispatched in the context of one fix task). We log a
        warning and return ``[]`` rather than raising — symmetrical to
        the per-feature missing-``feature_id`` misuse stance.
        """
        if fix_task is None:
            logger.warning(
                "forward_context_builder: Mode C TASK_WORK called without a "
                "fix_task (build_id=%s); refusing to thread context",
                build_id,
            )
            return []

        entries: list[ContextEntry] = [
            ContextEntry(
                flag="--fix-task",
                value=fix_task.to_json(),
                kind="text",
            )
        ]
        for path in fix_task.review_artefact_paths:
            if not self._allowlist.is_allowed(build_id, path):
                logger.warning(
                    "forward_context_builder: Mode C TASK_WORK review path "
                    "outside worktree allowlist; build_id=%s path=%s "
                    "fix_task_id=%s — filtered from forward context",
                    build_id,
                    path,
                    fix_task.fix_task_id,
                )
                continue
            entries.append(
                ContextEntry(
                    flag="--context",
                    value=path,
                    kind="path",
                )
            )
        return entries

    def _build_mode_c_followup_review(
        self,
        build_id: str,
    ) -> list[ContextEntry]:
        """Mode C follow-up ``/task-review`` dispatch: every completed task-work.

        Per TASK-MBC8-005 AC-005, the follow-up review must see the
        artefact paths from every approved ``/task-work`` in the cycle so
        the reviewer can judge whether the fixes landed cleanly. We query
        :meth:`StageLogReader.get_all_approved_stage_entries` for
        ``TASK_WORK`` (it is not per-feature — see
        :data:`~forge.pipeline.stage_taxonomy.PER_FIX_TASK_STAGES`),
        then emit one allow-listed ``--context`` path entry per artefact.

        Empty result (no fix tasks completed yet) is a legitimate state —
        a Mode C cycle that wraps up immediately because the initial
        review returned no fix tasks would call this with nothing to
        propagate. We return ``[]`` in that case.
        """
        approved_entries = self._reader.get_all_approved_stage_entries(
            build_id=build_id,
            stage=StageClass.TASK_WORK,
            feature_id=None,
        )
        entries: list[ContextEntry] = []
        for approved in approved_entries:
            for path in approved.artefact_paths:
                if not self._allowlist.is_allowed(build_id, path):
                    logger.warning(
                        "forward_context_builder: Mode C follow-up "
                        "TASK_REVIEW path outside worktree allowlist; "
                        "build_id=%s path=%s — filtered from forward context",
                        build_id,
                        path,
                    )
                    continue
                entries.append(
                    ContextEntry(
                        flag="--context",
                        value=path,
                        kind="path",
                    )
                )
        return entries

    # ------------------------------------------------------------------
    # Internal: shape the approved row into ContextEntry values
    # ------------------------------------------------------------------

    def _materialise_entries(
        self,
        recipe: ContextRecipe,
        approved: ApprovedStageEntry,
        build_id: str,
    ) -> list[ContextEntry]:
        """Convert an approved row into ``ContextEntry`` values per the recipe.

        Splits along the recipe's ``artefact_kind``:

        - ``text``: emits one entry from ``approved.artefact_text``.
          Allowlist is bypassed — text payloads have no filesystem path
          to gate. An empty / ``None`` text payload yields no entry
          (defensive: a producer that passes the gate but emits no text
          is a bug upstream, not something we should crash on).
        - ``path``: emits one entry from ``approved.artefact_paths[0]``,
          gated by the allowlist.
        - ``path-list``: emits one entry per allowed path; every path is
          checked individually.

        Rejects are filtered with a structured warning that names the
        path and the build so operators can track allowlist misses
        without spelunking through SQLite.
        """
        if recipe.artefact_kind == "text":
            return self._build_text_entries(recipe, approved)
        # Both "path" and "path-list" go through the same allowlist
        # filter — the only difference is how many paths we read.
        return self._build_path_entries(recipe, approved, build_id)

    def _build_text_entries(
        self,
        recipe: ContextRecipe,
        approved: ApprovedStageEntry,
    ) -> list[ContextEntry]:
        """Emit a single text entry from ``approved.artefact_text``."""
        text = approved.artefact_text
        if not text:
            logger.warning(
                "forward_context_builder: text-kind recipe (%s) produced "
                "no artefact_text — upstream stage approved with empty "
                "payload; returning empty context",
                recipe.description,
            )
            return []
        return [
            ContextEntry(
                flag=recipe.context_flag,
                value=text,
                kind="text",
            )
        ]

    def _build_path_entries(
        self,
        recipe: ContextRecipe,
        approved: ApprovedStageEntry,
        build_id: str,
    ) -> list[ContextEntry]:
        """Emit one path entry per allow-listed path in ``approved``."""
        if recipe.artefact_kind == "path":
            # Single-path recipe — take only the first artefact path.
            # An approved row with no paths is treated the same way as
            # a text-kind recipe with no text: defensive empty list.
            if not approved.artefact_paths:
                logger.warning(
                    "forward_context_builder: path-kind recipe (%s) "
                    "produced no artefact_paths; returning empty context",
                    recipe.description,
                )
                return []
            paths = approved.artefact_paths[:1]
        else:
            paths = approved.artefact_paths

        entries: list[ContextEntry] = []
        for path in paths:
            if not self._allowlist.is_allowed(build_id, path):
                # Structured WARNING — operators monitor this log line
                # to detect allowlist drift. Include both the path and
                # the build_id so the message is self-contained.
                logger.warning(
                    "forward_context_builder: artefact path outside "
                    "worktree allowlist; build_id=%s path=%s recipe=%s — "
                    "filtered from forward context",
                    build_id,
                    path,
                    recipe.description,
                )
                continue
            entries.append(
                ContextEntry(
                    flag=recipe.context_flag,
                    value=path,
                    kind="path",
                )
            )
        return entries


# ---------------------------------------------------------------------------
# Module-level dataclass field default factories
# ---------------------------------------------------------------------------
# (No additional state beyond the ApprovedStageEntry/ContextEntry frozen
# dataclasses defined above. Listed here for grep-ability — if a future
# refactor adds module-level mutable state, it should appear in this
# section so the audit-trail of "what does this module own?" stays
# centralised.)
_ = field  # silence unused-import; kept for future field(default_factory=...) use
