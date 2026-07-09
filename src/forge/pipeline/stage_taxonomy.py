"""Canonical stage taxonomy for FEAT-FORGE-007 (Mode A) and FEAT-FORGE-008
(Mode B / Mode C extensions).

This module is the single source of truth for the canonical
:class:`StageClass` enum and the Mode A prerequisite map that encodes the
seven prerequisite rows from the FEAT-FORGE-007 Group B Scenario Outline
("A downstream stage is not dispatched before its prerequisite has
reached the approved state").

Per FEAT-FORGE-007 ASSUM-001 (Mode A greenfield assumptions), the first
eight stage classes that drive Mode A are, in dispatch order:

    product-owner → architect → /system-arch → /system-design →
    /feature-spec → /feature-plan → autobuild → pull-request review

The Mode A ``PULL_REQUEST_REVIEW`` stage is constitutional per
ADR-ARCH-026 (constitutional-rules-belt-and-braces): every Mode A run
must terminate at a human-reviewable pull request, regardless of how
many features are in flight.

Per FEAT-FORGE-008, two additional stage classes are appended at the
end of the enum so that Mode A's iteration-order contract is preserved
while Mode C can refer to them by name:

    /task-review → /task-work     (Mode C cyclic chain — see
    :mod:`forge.pipeline.mode_chains_data`)

This module is intentionally free of imports from any other
``forge.pipeline`` submodule so it can be imported from every downstream
guard, dispatcher, and context builder in Waves 2–4 without forming an
import cycle (see TASK-MAG7-001 implementation notes).

References:
    - FEAT-FORGE-007 ASSUM-001 — eight-stage taxonomy assumption.
    - FEAT-FORGE-008 ASSUM-004 — Mode C chain
      (``/task-review`` + ``/task-work`` cycle).
    - ADR-ARCH-026 — constitutional rules (belt-and-braces); the
      ``PULL_REQUEST_REVIEW`` stage is the constitutional gate that
      terminates every Mode A run.
    - ``features/mode-a-greenfield-end-to-end/``
      ``mode-a-greenfield-end-to-end.feature`` — Background and Group B
      Scenario Outline rows that this taxonomy mirrors verbatim.
    - ``features/mode-b-feature-and-mode-c-review-fix/`` — Mode B / C
      assumptions and scenario outlines.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "StageClass",
    "STAGE_PREREQUISITES",
    "CONSTITUTIONAL_STAGES",
    "PER_FEATURE_STAGES",
    "PER_FIX_TASK_STAGES",
    "POST_REVIEW_STAGES",
]


class StageClass(StrEnum):
    """The eight Mode A stage classes, in dispatch order.

    Member order matches the FEAT-FORGE-007 ASSUM-001 dispatch order
    and the ``mode-a-greenfield-end-to-end.feature`` Background. Do
    **not** reorder — downstream guards rely on iteration order to
    reason about wave membership and pipeline progress.

    String values mirror the dash-separated names that appear in the
    Group B Scenario Outline ("architect ← product-owner",
    "system-arch ← architect", …) so callers can round-trip between the
    enum and the feature file without a translation table.
    """

    PRODUCT_OWNER = "product-owner"
    ARCHITECT = "architect"
    SYSTEM_ARCH = "system-arch"
    SYSTEM_DESIGN = "system-design"
    FEATURE_SPEC = "feature-spec"
    FEATURE_PLAN = "feature-plan"
    AUTOBUILD = "autobuild"
    PULL_REQUEST_REVIEW = "pull-request-review"
    # FEAT-FORGE-008 Mode C extensions — appended after the leading eight so
    # the Mode A iteration order above is byte-for-byte preserved
    # (StageOrderingGuard in TASK-MAG7-003 relies on the leading eight members
    # staying put). The per-fix-task fan-out of TASK_WORK is handled by the
    # cycle planner in TASK-MBC8-004, not by this enum.
    TASK_REVIEW = "task-review"
    TASK_WORK = "task-work"
    # WS2-B8 output-side (deploy) stages — the last-mile taxonomy after
    # PULL_REQUEST_REVIEW (scope-design §4). They are appended at the end so
    # the leading eight (and the Mode C tail) keep their positions. They are
    # ordering-expressed via STAGE_PREREQUISITES (DEPLOY ← PULL_REQUEST_REVIEW,
    # LIVE_GATE ← DEPLOY) but are DELIBERATELY excluded from every mode chain,
    # PER_FEATURE_STAGES, and the Mode A/B/C reasoning-loop permitted set
    # (see POST_REVIEW_STAGES below): they are dispatched by the standalone
    # DeployStageRunner (forge.deploy.stage), config-gated on
    # ``deploy.enabled`` (default False, disabled in production until V1), not
    # by the greenfield reasoning loop. This keeps Mode A/B/C dispatch
    # byte-identical while giving V1 canonical stage names for the stage log
    # and the fleet dashboard.
    DEPLOY = "deploy"
    LIVE_GATE = "live-gate"


#: Stage prerequisite map.
#:
#: Encodes the seven prerequisite rows from the FEAT-FORGE-007 Group B
#: Scenario Outline. Each key is a stage; the value lists the stages
#: that must have reached the approved state before the key stage may
#: be dispatched.
#:
#: Rows (verbatim from Group B Scenario Outline):
#:
#:     1. architect           ← product-owner
#:     2. system-arch         ← architect
#:     3. system-design       ← system-arch
#:     4. feature-spec        ← system-design
#:     5. feature-plan        ← feature-spec        (per feature)
#:     6. autobuild           ← feature-plan        (per feature)
#:     7. pull-request-review ← autobuild           (for every feature)
#:
#: ``PRODUCT_OWNER`` is the entry stage and has no prerequisites; it is
#: deliberately omitted from this map so callers can use ``in
#: STAGE_PREREQUISITES`` as a "has prerequisites?" predicate.
STAGE_PREREQUISITES: dict[StageClass, list[StageClass]] = {
    StageClass.ARCHITECT: [StageClass.PRODUCT_OWNER],
    StageClass.SYSTEM_ARCH: [StageClass.ARCHITECT],
    StageClass.SYSTEM_DESIGN: [StageClass.SYSTEM_ARCH],
    StageClass.FEATURE_SPEC: [StageClass.SYSTEM_DESIGN],
    StageClass.FEATURE_PLAN: [StageClass.FEATURE_SPEC],
    StageClass.AUTOBUILD: [StageClass.FEATURE_PLAN],
    StageClass.PULL_REQUEST_REVIEW: [StageClass.AUTOBUILD],
    # FEAT-FORGE-008 / TASK-MBC8-001 — Mode C single-row prerequisite.
    # Each ``/task-work`` is dispatched only after the parent
    # ``/task-review`` has reached the approved state. The per-fix-task
    # fan-out (one ``/task-work`` per fix-task identifier emitted by the
    # review) is enforced by ``ModeCCyclePlanner`` in TASK-MBC8-004, not
    # by this map — the prerequisite expressed here is the *class*-level
    # ordering that ``StageOrderingGuard`` consumes.
    StageClass.TASK_WORK: [StageClass.TASK_REVIEW],
    # WS2-B8 output-side ordering (scope-design §4): DEPLOY follows
    # PULL_REQUEST_REVIEW, LIVE_GATE follows DEPLOY. These rows make the
    # ordering canonical, but the two stages are filtered out of the
    # reasoning-loop permitted set (POST_REVIEW_STAGES) — the DeployStageRunner
    # is their only dispatcher, so this map never causes the greenfield
    # supervisor to offer them.
    StageClass.DEPLOY: [StageClass.PULL_REQUEST_REVIEW],
    StageClass.LIVE_GATE: [StageClass.DEPLOY],
}


#: Constitutional stages — gates that every Mode A run must pass.
#:
#: Per ADR-ARCH-026, the pull-request review stage is the
#: constitutional terminator: a Mode A run is not "complete" until a
#: human-reviewable pull request has been opened for every feature.
#: ``constitutional`` here means the stage is a non-skippable rule of
#: the pipeline, not that it is governed by a separate constitution
#: document.
CONSTITUTIONAL_STAGES: frozenset[StageClass] = frozenset(
    {StageClass.PULL_REQUEST_REVIEW}
)


#: Per-feature stages — stages that fan out across all in-flight features.
#:
#: The first four stages (product-owner through system-design) run once
#: per Mode A pipeline; the remaining four fan out across every feature
#: in scope. ``PULL_REQUEST_REVIEW`` is per-feature even though it is
#: also constitutional — Group B row 7 ("pull-request ← autobuild for
#: every feature") makes this explicit.
PER_FEATURE_STAGES: frozenset[StageClass] = frozenset(
    {
        StageClass.FEATURE_SPEC,
        StageClass.FEATURE_PLAN,
        StageClass.AUTOBUILD,
        StageClass.PULL_REQUEST_REVIEW,
    }
)


#: Per-fix-task stages — stages that fan out across all fix tasks
#: emitted by a single ``/task-review`` in Mode C (FEAT-FORGE-008
#: ASSUM-004 review-fix cycle).
#:
#: ``TASK_REVIEW`` runs **once** per Mode C cycle and emits N fix-task
#: identifiers; ``TASK_WORK`` then fans out one dispatch per fix task.
#: Only ``TASK_WORK`` is in this set — the per-fix-task fan-out is the
#: cycle planner's responsibility (TASK-MBC8-004), and this frozenset
#: is the declarative tag that lets the supervisor and dispatcher
#: distinguish per-fix-task stages from per-feature stages
#: (:data:`PER_FEATURE_STAGES`) and pipeline-wide stages.
PER_FIX_TASK_STAGES: frozenset[StageClass] = frozenset({StageClass.TASK_WORK})


#: Output-side (post-review) stages — the WS2-B8 last-mile taxonomy.
#:
#: ``DEPLOY`` and ``LIVE_GATE`` follow ``PULL_REQUEST_REVIEW`` in
#: :data:`STAGE_PREREQUISITES`, but they are dispatched by the standalone
#: :class:`forge.deploy.stage.DeployStageRunner` (config-gated on
#: ``deploy.enabled``), **never** by the Mode A/B/C greenfield reasoning loop.
#: :meth:`StageOrderingGuard.next_dispatchable` filters this set out of its
#: default walk so the reasoning-loop permitted set is byte-for-byte unchanged
#: by their addition to the enum — a stage in this set is only ever reached
#: through the deploy runner, which checks prerequisites explicitly.
POST_REVIEW_STAGES: frozenset[StageClass] = frozenset(
    {StageClass.DEPLOY, StageClass.LIVE_GATE}
)
