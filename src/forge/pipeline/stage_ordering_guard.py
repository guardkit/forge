"""Executor-layer guard for the Mode A stage-ordering invariant (TASK-MAG7-003).

This module implements the executor-layer half of the FEAT-FORGE-007 Group B
stage-ordering invariant: a downstream Mode A stage is **never** dispatched
before every prerequisite stage has reached the approved state in the
``stage_log``.

It is the belt-and-braces twin of the reasoning-model prompt — the reasoning
model may *choose* a stage, but :class:`StageOrderingGuard` is what the
supervisor consults before actually dispatching it. Because this module is a
pure function (no async, no I/O behind the injected
:class:`StageLogReader` Protocol), the reasoning model cannot bypass it via
prompt drift (ADR-ARCH-026).

Per-feature semantics (FEAT-FORGE-007 ASSUM-001):

- ``PRODUCT_OWNER``, ``ARCHITECT``, ``SYSTEM_ARCH``, ``SYSTEM_DESIGN`` run
  once per Mode A pipeline. Approval is recorded against the ``build_id``
  with no ``feature_id`` scope.
- ``FEATURE_SPEC``, ``FEATURE_PLAN``, ``AUTOBUILD`` run **once per feature**
  (Group B Scenario Outline rows 5–6, "feature-plan ← feature-spec for
  that feature"). Approval is recorded against ``(build_id, feature_id)``.
- ``PULL_REQUEST_REVIEW`` is the constitutional terminator. Group B row 7
  ("pull-request ← autobuild for every feature") makes it dispatchable
  only when ``AUTOBUILD`` is approved for *every* feature in the
  build's catalogue. An empty catalogue is therefore not dispatchable —
  there is nothing to review.

References:

- Task: TASK-MAG7-003 (this implementation)
- Producer: TASK-MAG7-001 (:mod:`forge.pipeline.stage_taxonomy`)
- Feature: ``features/mode-a-greenfield-end-to-end/`` Group B Scenario
  Outline ("A downstream stage is not dispatched before its prerequisite
  has reached the approved state")
- ADR-ARCH-026 (constitutional rules — belt-and-braces)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Iterable, Protocol, runtime_checkable

from forge.pipeline.stage_taxonomy import (
    PER_FEATURE_STAGES,
    POST_REVIEW_STAGES,
    STAGE_PREREQUISITES,
    StageClass,
)

__all__ = [
    "StageLogReader",
    "StageOrderingGuard",
]


@runtime_checkable
class StageLogReader(Protocol):
    """Read-only view over the FEAT-FORGE-001 ``stage_log`` table.

    The guard depends only on this Protocol so unit tests can substitute
    an in-memory fake without bringing up SQLite. The production
    implementation is provided by FEAT-FORGE-001's SQLite adapter.

    Two methods only — :meth:`is_approved` answers "has this stage been
    approved for this build (and feature, if per-feature)?" and
    :meth:`feature_catalogue` returns the list of feature IDs in scope
    so the multi-feature ``PULL_REQUEST_REVIEW`` check can fan out.
    """

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        """Return ``True`` iff ``stage`` is recorded as approved.

        For per-feature stages the lookup is scoped to ``(build_id,
        feature_id)``. For non-per-feature stages ``feature_id`` is
        ``None`` and the lookup is scoped to ``build_id`` only.
        """
        ...  # pragma: no cover - protocol stub

    def feature_catalogue(self, build_id: str) -> list[str]:
        """Return the ordered list of feature IDs in the build's catalogue.

        Empty list means the catalogue has not been produced yet (or
        ``SYSTEM_DESIGN`` produced zero features — see TASK-MAG7-005's
        boundary scenario). Either way, ``PULL_REQUEST_REVIEW`` is **not**
        dispatchable when this list is empty.
        """
        ...  # pragma: no cover - protocol stub


class StageOrderingGuard:
    """Pure-function guard enforcing the Mode A stage-ordering invariant.

    The guard is intentionally state-free: every method takes the
    :class:`StageLogReader` as an argument so the same guard instance can
    be reused across builds (and across tests with different in-memory
    readers). No async, no I/O — the only side effect is the
    ``stage_log_reader.is_approved`` / ``feature_catalogue`` calls, both
    of which are read-only by Protocol contract.

    See module docstring for the full executor-layer-vs-reasoning-model
    rationale (ADR-ARCH-026 belt-and-braces).
    """

    def is_dispatchable(
        self,
        build_id: str,
        stage: StageClass,
        stage_log_reader: StageLogReader,
        feature_id: str | None = None,
        *,
        prerequisites: Mapping[StageClass, Sequence[StageClass]] | None = None,
    ) -> bool:
        """Return ``True`` iff every prerequisite of ``stage`` is approved.

        The decision tree mirrors the seven Group B Scenario Outline rows:

        1. ``PULL_REQUEST_REVIEW`` is special-cased — it requires
           ``AUTOBUILD`` approved for *every* feature in
           ``stage_log_reader.feature_catalogue(build_id)`` (row 7) when
           the Mode A prerequisite map is in effect; under per-mode maps
           that explicitly list ``PULL_REQUEST_REVIEW`` (Mode B / Mode C
           via :mod:`forge.pipeline.mode_chains_data`) the listed
           prerequisite is consulted instead.
        2. Stages with no prerequisites entry in the active prerequisite
           map (i.e. the entry stage for the mode — ``PRODUCT_OWNER`` for
           Mode A, ``FEATURE_SPEC`` for Mode B, ``TASK_REVIEW`` for
           Mode C) are trivially dispatchable.
        3. Per-feature stages other than ``PULL_REQUEST_REVIEW`` —
           ``FEATURE_SPEC``, ``FEATURE_PLAN``, ``AUTOBUILD`` — require
           ``feature_id`` to be supplied. Without it the guard refuses
           rather than checking against the wrong scope.
        4. Otherwise: every prerequisite must be approved. A
           prerequisite that is itself per-feature is checked at the
           same ``feature_id`` (rows 5 and 6: "feature-plan ←
           feature-spec for that feature"); a non-per-feature
           prerequisite is checked with ``feature_id=None``.

        Args:
            build_id: Build identifier (``build-{feature_id}-{ts}``).
            stage: The stage class the supervisor wants to dispatch.
            stage_log_reader: Injected reader over the ``stage_log`` table.
            feature_id: Required for per-feature stages other than
                ``PULL_REQUEST_REVIEW``; ignored for non-per-feature
                stages. ``None`` by default.
            prerequisites: Optional per-mode prerequisite map. When
                ``None`` (default) the canonical Mode A map
                :data:`~forge.pipeline.stage_taxonomy.STAGE_PREREQUISITES`
                is used, preserving every existing TASK-MAG7-003 caller.
                Mode B / Mode C callers (TASK-MBC8-008) inject the
                matching :data:`~forge.pipeline.mode_chains_data.MODE_B_PREREQUISITES`
                or :data:`~forge.pipeline.mode_chains_data.MODE_C_PREREQUISITES`
                map so the guard's belt-and-braces enforcement matches
                the build's actual chain shape.

        Returns:
            ``True`` if the supervisor may dispatch ``stage`` now,
            ``False`` otherwise.
        """
        prereq_map: Mapping[StageClass, Sequence[StageClass]] = (
            prerequisites if prerequisites is not None else STAGE_PREREQUISITES
        )
        # Mode A treats PULL_REQUEST_REVIEW as a build-wide fan-out (row
        # 7 of the Group B Scenario Outline). Per-mode maps that name
        # PULL_REQUEST_REVIEW explicitly (Mode B, Mode C) opt out of the
        # fan-out and use the listed prerequisite instead.
        if (
            stage is StageClass.PULL_REQUEST_REVIEW
            and prerequisites is None
        ):
            features = stage_log_reader.feature_catalogue(build_id)
            if not features:
                # Empty catalogue → nothing to review → not dispatchable.
                # Defends the boundary that TASK-MAG7-005 covers from the
                # other side (zero-feature system-design output).
                return False
            return all(
                stage_log_reader.is_approved(
                    build_id, StageClass.AUTOBUILD, feature_id=fid
                )
                for fid in features
            )

        prerequisite_stages = list(prereq_map.get(stage, []))
        if not prerequisite_stages:
            # Entry stage for the active mode — no prerequisites by
            # design (PRODUCT_OWNER for Mode A, FEATURE_SPEC for Mode B,
            # TASK_REVIEW for Mode C).
            return True

        # Determine if this dispatch needs a feature scope.
        #
        # Mode A: every per-feature stage (FEATURE_SPEC, FEATURE_PLAN,
        # AUTOBUILD, PULL_REQUEST_REVIEW) requires a feature_id —
        # missing scope would silently leak across features.
        #
        # Per-mode (Mode B / Mode C): the stage's per-feature-ness is
        # *prerequisite-driven*. A stage whose prereqs are all
        # build-scoped (e.g. Mode C ``PULL_REQUEST_REVIEW`` ←
        # ``TASK_WORK``) is itself build-scoped under that mode and
        # does not require ``feature_id``.
        if prerequisites is None:
            feature_scope_required = stage in PER_FEATURE_STAGES
        else:
            feature_scope_required = any(
                prereq in PER_FEATURE_STAGES for prereq in prerequisite_stages
            )
        if feature_scope_required and feature_id is None:
            return False

        for prereq in prerequisite_stages:
            scoped_feature_id = feature_id if prereq in PER_FEATURE_STAGES else None
            if not stage_log_reader.is_approved(
                build_id, prereq, feature_id=scoped_feature_id
            ):
                return False
        return True

    def next_dispatchable(
        self,
        build_id: str,
        stage_log_reader: StageLogReader,
        *,
        prerequisites: Mapping[StageClass, Sequence[StageClass]] | None = None,
        stages: Iterable[StageClass] | None = None,
    ) -> set[StageClass]:
        """Return the set of stages whose prerequisites are all approved.

        Walks every :class:`StageClass` and asks :meth:`is_dispatchable`.
        For per-feature stages other than ``PULL_REQUEST_REVIEW`` a stage
        is included if at least one feature in
        ``stage_log_reader.feature_catalogue(build_id)`` has its
        prerequisites satisfied — i.e. there is *some* dispatch the
        supervisor could legally issue.

        ``PULL_REQUEST_REVIEW`` and the non-per-feature stages are
        checked once against the build scope.

        Args:
            build_id: Build identifier.
            stage_log_reader: Injected reader over the ``stage_log`` table.
            prerequisites: Optional per-mode prerequisite map. When
                ``None`` (default) the canonical Mode A map is used
                (TASK-MAG7-003 contract). Mode B / Mode C callers pass
                their respective per-mode maps so the dispatchable set
                reflects only the chain stages the build is actually
                running (TASK-MBC8-008).
            stages: Optional iterable of :class:`StageClass` to scope
                the walk to. Defaults to every :class:`StageClass`
                member, preserving the Mode A contract. Mode B / Mode C
                callers pass the matching :data:`MODE_B_CHAIN` /
                :data:`MODE_C_CHAIN` so the returned set never includes
                stages forbidden by the active mode.

        Returns:
            The subset of :class:`StageClass` the supervisor is allowed
            to dispatch on the next reasoning turn. May contain stages
            that are already approved — callers are expected to filter
            further if "currently in progress" semantics are required.
        """
        dispatchable: set[StageClass] = set()
        # Snapshot the catalogue once — callers paying for a SQLite
        # round-trip per call should not pay it eight times here.
        features = stage_log_reader.feature_catalogue(build_id)

        prereq_map: Mapping[StageClass, Sequence[StageClass]] = (
            prerequisites if prerequisites is not None else STAGE_PREREQUISITES
        )

        # WS2-B8: the output-side stages (DEPLOY, LIVE_GATE) live in the
        # StageClass enum for canonical naming, but they are dispatched by the
        # standalone DeployStageRunner (config-gated), never by the greenfield
        # reasoning loop. Exclude them from the default walk so adding them to
        # the enum leaves the Mode A/B/C permitted set byte-for-byte unchanged.
        # An explicit ``stages=`` caller (e.g. a future deploy-aware walk) can
        # still opt them in.
        candidate_stages: Iterable[StageClass] = (
            stages
            if stages is not None
            else [s for s in StageClass if s not in POST_REVIEW_STAGES]
        )
        for stage in candidate_stages:
            if prerequisites is None:
                # Mode A behaviour — unchanged from TASK-MAG7-003.
                # PULL_REQUEST_REVIEW is the build-wide fan-out gate
                # (Group B row 7) and is checked at build scope; every
                # other per-feature stage is iterated across the
                # catalogue. Non-per-feature stages are checked once.
                if (
                    stage in PER_FEATURE_STAGES
                    and stage is not StageClass.PULL_REQUEST_REVIEW
                ):
                    if features and any(
                        self.is_dispatchable(
                            build_id,
                            stage,
                            stage_log_reader,
                            feature_id=fid,
                        )
                        for fid in features
                    ):
                        dispatchable.add(stage)
                else:
                    if self.is_dispatchable(
                        build_id, stage, stage_log_reader
                    ):
                        dispatchable.add(stage)
            else:
                # Per-mode (Mode B / Mode C) behaviour — TASK-MBC8-008.
                # Iterate features only when at least one prerequisite
                # is per-feature; otherwise check at build scope.
                prereq_list = list(prereq_map.get(stage, []))
                has_per_feature_prereq = any(
                    prereq in PER_FEATURE_STAGES for prereq in prereq_list
                )
                if has_per_feature_prereq:
                    if features and any(
                        self.is_dispatchable(
                            build_id,
                            stage,
                            stage_log_reader,
                            feature_id=fid,
                            prerequisites=prerequisites,
                        )
                        for fid in features
                    ):
                        dispatchable.add(stage)
                else:
                    if self.is_dispatchable(
                        build_id,
                        stage,
                        stage_log_reader,
                        prerequisites=prerequisites,
                    ):
                        dispatchable.add(stage)

        return dispatchable
