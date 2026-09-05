"""Pydantic v2 models describing the ``forge.yaml`` configuration surface.

These models are the **declarative producer** for the NATS Fleet Integration
feature (FEAT-FORGE-002). The defaults below are anchored to the assumptions
manifest (see ``features/nats-fleet-integration/nats-fleet-integration_assumptions.yaml``):

- ASSUM-001: ``FleetConfig.heartbeat_interval_seconds`` = 30
- ASSUM-002: ``FleetConfig.stale_heartbeat_seconds`` = 90
- ASSUM-003: ``FleetConfig.cache_ttl_seconds`` = 30
- ASSUM-004: ``FleetConfig.intent_min_confidence`` = 0.7
- ASSUM-005: ``PipelineConfig.progress_interval_seconds`` = 60

This module is also the declarative producer for the
Confidence-Gated Checkpoint Protocol feature (FEAT-FORGE-004) — see the
``ApprovalConfig`` model below, whose defaults are anchored to that
feature's assumptions manifest
(``features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_assumptions.yaml``):

- ASSUM-001 (CGCP): ``ApprovalConfig.default_wait_seconds`` = 300
- ASSUM-002 (CGCP): ``ApprovalConfig.max_wait_seconds`` = 3600

Downstream consumers (TASK-NFI-004/005/007, TASK-CGCP-006/007/010) import
these models from ``forge.config`` and must not duplicate any of the
defaults.

Per the project boundary rules for ``forge.config.models``, this module
must not import from ``nats_core``, ``nats-py``, or ``langgraph``: it is a
pure declarative schema layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Default values (anchored to ASSUM-001..005)
# ---------------------------------------------------------------------------

#: ASSUM-001 — heartbeat publish cadence (seconds).
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30

#: ASSUM-002 — agent excluded from primary resolution after this many seconds
#: without a heartbeat.
DEFAULT_STALE_HEARTBEAT_SECONDS = 90

#: ASSUM-003 — TTL of the live discovery cache (seconds).
DEFAULT_CACHE_TTL_SECONDS = 30

#: ASSUM-004 — minimum intent-resolution confidence for fallback selection.
DEFAULT_INTENT_MIN_CONFIDENCE = 0.7

#: ASSUM-005 — minimum cadence at which a long-running stage must publish
#: progress while in RUNNING state (seconds).
DEFAULT_PROGRESS_INTERVAL_SECONDS = 60

#: Default subject pattern that ``pipeline_consumer`` subscribes to for
#: build-queued events. The trailing ``>`` is a NATS wildcard.
DEFAULT_BUILD_QUEUE_SUBJECT = "pipeline.build-queued.>"

#: Default originator allowlist accepted by ``pipeline_consumer``. Anything
#: not in this list is rejected before the pipeline state machine sees it.
#:
#: The six human adapters plus ``forge-internal``: the last is the
#: ``triggered_by`` layer the Lane B planning target-terminal uses for a
#: machine-made build dispatch (no user-facing adapter). It is the effective
#: originator identity when ``originating_adapter is None`` (see
#: ``pipeline_consumer.handle_message`` gate 2). Deliberately NOT widened to
#: ``cli`` (the CLI already carries ``cli-wrapper``) or ``notification-adapter``
#: (nothing live uses it) — the list widens by exactly the one literal the
#: B4 round-14 machine-dispatch defect needs.
DEFAULT_APPROVED_ORIGINATORS: tuple[str, ...] = (
    "terminal",
    "voice-reachy",
    "telegram",
    "slack",
    "dashboard",
    "cli-wrapper",
    "forge-internal",
)

#: ASSUM-001 (CGCP / FEAT-FORGE-004) — initial wait time published on an
#: approval request when the caller does not specify one (seconds). Anchored
#: to ``API-nats-approval-protocol §3.1`` (``timeout_seconds`` default = 300).
DEFAULT_APPROVAL_WAIT_SECONDS = 300

#: ASSUM-002 (CGCP / FEAT-FORGE-004) — ceiling on the *total* approval wait
#: a paused build may accumulate by refreshing its wait. Anchored to
#: ``API-nats-approval-protocol §7`` ("refresh up to
#: forge.yaml.approval.max_wait_seconds ≈ 3600").
DEFAULT_APPROVAL_MAX_WAIT_SECONDS = 3600

#: The build gate's own approval-wait ceiling (2026-08-26). 0 means the gate
#: waits indefinitely for a human answer — the approval card stays live until
#: someone answers, exactly like the spec digest pause. This deliberately
#: replaces the old behaviour where ``approval.max_wait_seconds`` (1 hour)
#: cancelled the whole build when nobody tapped the card in time; with the
#: queue verb retired, a cancelled gate had no re-offer path and the only
#: recovery was re-planning the whole feature. Operators who want a hard
#: ceiling back set ``autobuild_gate.approval_max_wait_seconds`` in forge.yaml.
DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS = 0

#: FEAT-UBS-002 — the reserved profile name whose caps must all be unset. It
#: encodes FEAT-FORGE-008 ASSUM-010 (attended mode = reviewer-driven, no numeric
#: cap). ``BudgetConfig`` rejects any config that puts a cap on this profile.
ATTENDED_PROFILE_NAME = "attended"

#: FEAT-UBS-002 — conservative default caps for the ``unattended`` profile. Kept
#: deliberately tight at launch per scope §3 constraint 1 ("autonomy follows
#: verification quality"); loosened only as the QA-Verifier Phase-0 gates come
#: online on the features being built.
DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES = 2
DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS = 5400  # 90 minutes

#: The conductor's fix-journey profile name (revival design pass §d Stage 3).
FIX_JOURNEY_PROFILE_NAME = "fix-journey"

#: THE CAP-MAPPING LAW (design pass §h.7) — read this before changing it.
#:
#: The bound Rich ruled is "**ONE follow-up review** per fix build". The cap
#: that enforces it is NOT 1. ``count_review_cycles``
#: (``pipeline/budget_guard.py``) counts **every** review entry in the
#: build's history, and a bounded fix journey has TWO: the initial
#: ``/task-review`` that finds the fix tasks, and the ONE follow-up
#: ``/task-review`` that confirms they landed. The guard is consulted
#: *before* the step it would allow, with ``>=`` semantics, so:
#:
#:     max_review_cycles = 2  ->  initial review, fixes, ONE follow-up. ✅
#:     max_review_cycles = 1  ->  breach at the MANDATORY follow-up. ❌
#:
#: Writing ``1`` here therefore guarantees a false pause on every single fix
#: build — the build would escalate risk-high and pause before doing the one
#: thing that proves the fix worked. The mapping is pinned by a test that
#: shows a profile of ``1`` breaching at the follow-up while ``2`` passes;
#: that test pins the MAPPING, not the mistake.
FIX_JOURNEY_MAX_REVIEW_CYCLES = 2

#: Wall-clock cap for the fix journey — "on from day one" (design pass §d
#: Stage 3). A fix journey is a bounded repair, not a feature build, so it
#: is held tighter than the ``unattended`` build profile.
FIX_JOURNEY_MAX_BUILD_WALLCLOCK_SECONDS = 3600  # 60 minutes


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FleetConfig(BaseModel):
    """Configuration for Forge's participation on the shared NATS fleet.

    Defaults are pinned to ASSUM-001..004. Operators may override any field in
    ``forge.yaml`` but the defaults must continue to match the assumptions
    manifest so the in-memory schema is the canonical source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    heartbeat_interval_seconds: int = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        description="ASSUM-001 — cadence of fleet heartbeats published by Forge.",
    )
    stale_heartbeat_seconds: int = Field(
        default=DEFAULT_STALE_HEARTBEAT_SECONDS,
        description=(
            "ASSUM-002 — agents whose last heartbeat is older than this are "
            "excluded from primary resolution."
        ),
    )
    cache_ttl_seconds: int = Field(
        default=DEFAULT_CACHE_TTL_SECONDS,
        description="ASSUM-003 — TTL of the live discovery cache.",
    )
    intent_min_confidence: float = Field(
        default=DEFAULT_INTENT_MIN_CONFIDENCE,
        description=(
            "ASSUM-004 — minimum confidence for intent-fallback agent "
            "selection. Agents at exactly this confidence are eligible."
        ),
    )


class PipelineConfig(BaseModel):
    """Configuration for the outbound lifecycle stream and inbound build queue."""

    model_config = ConfigDict(extra="forbid")

    progress_interval_seconds: int = Field(
        default=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        description=(
            "ASSUM-005 — minimum cadence at which a long-running stage must "
            "publish progress events while in RUNNING."
        ),
    )
    build_queue_subject: str = Field(
        default=DEFAULT_BUILD_QUEUE_SUBJECT,
        description="NATS subject pattern subscribed to by pipeline_consumer.",
    )
    approved_originators: list[str] = Field(
        default_factory=lambda: list(DEFAULT_APPROVED_ORIGINATORS),
        description=(
            "Originator identifiers accepted by pipeline_consumer. Build-queued "
            "events from any other originator are rejected."
        ),
    )


class ApprovalConfig(BaseModel):
    """Configuration for the approval / pause-resume protocol (FEAT-FORGE-004).

    Defaults are pinned to ASSUM-001 / ASSUM-002 of the
    Confidence-Gated Checkpoint Protocol assumptions manifest. Operators may
    override either field in ``forge.yaml`` but the defaults must continue to
    match the assumptions manifest so this in-memory schema stays the
    canonical source of truth for both the publisher (TASK-CGCP-006) and the
    state machine (TASK-CGCP-010).

    Note (ASSUM-003 deferral): The terminal behaviour applied when an
    approval pause reaches ``max_wait_seconds`` without a response —
    cancel / escalate / fail-open — is **explicitly out of scope** for this
    model and is deferred to the ``forge-pipeline-config`` feature. Do not
    add a ceiling-fallback field here; that decision belongs with the
    state-machine configuration, not with the wait-time settings.
    """

    model_config = ConfigDict(extra="forbid")

    default_wait_seconds: int = Field(
        default=DEFAULT_APPROVAL_WAIT_SECONDS,
        ge=0,
        description=(
            "ASSUM-001 (CGCP) — initial wait time published on an approval "
            "request when the caller does not specify one. Must be "
            "non-negative and not exceed ``max_wait_seconds``."
        ),
    )
    max_wait_seconds: int = Field(
        default=DEFAULT_APPROVAL_MAX_WAIT_SECONDS,
        ge=0,
        description=(
            "ASSUM-002 (CGCP) — ceiling on the *total* approval wait a "
            "paused build may accumulate by refreshing. Behaviour at the "
            "ceiling (ASSUM-003) is deferred to ``forge-pipeline-config``."
        ),
    )
    expected_approver: str | None = Field(
        default="rich",
        description=(
            "APPROVER_IDENTITY contract (TASK-JNB-101/104) — the only "
            "``decided_by`` value the production ApprovalSubscriber "
            "accepts. Must string-equal the jarvis "
            "``JARVIS_SLACK_DECIDED_BY`` setting VERBATIM (no trimming, "
            "no case folding — jarvis publishes it untouched and forge "
            "compares with ``!=``); a mismatch silently refuses every "
            "phone approval with only a WARNING log on the forge side. "
            "Pinned shared value: 'rich' (operator-chosen 2026-07-04). "
            "``None`` = permissive mode (any responder accepted — dev "
            "only, never production). NOTE: this default flips "
            "deployments that omit the ``approval:`` block from "
            "permissive to enforcing; deploying a forge.yaml carrying "
            "this key before the image that defines it fails boot "
            "loudly (extra='forbid')."
        ),
    )

    @model_validator(mode="after")
    def _validate_default_not_above_max(self) -> ApprovalConfig:
        """Reject configurations where ``default_wait_seconds`` exceeds
        ``max_wait_seconds``.

        A default initial wait that is already larger than the configured
        ceiling can never refresh meaningfully — the very first publish would
        already be over budget. We surface this at config-load time rather
        than letting the publisher (TASK-CGCP-006) discover it at runtime.
        """
        if self.default_wait_seconds > self.max_wait_seconds:
            raise ValueError(
                "approval.default_wait_seconds "
                f"({self.default_wait_seconds}) must not exceed "
                f"approval.max_wait_seconds ({self.max_wait_seconds})"
            )
        return self


class AutobuildGateConfig(BaseModel):
    """Configuration for the build gate's human-approval wait (2026-08-26).

    The build gate is the pause that asks a human to approve a build before
    it runs (and the merge-ready card that reuses the same machinery). This
    model governs how long that pause may wait for an answer. It is a
    separate surface from :class:`ApprovalConfig` on purpose:
    ``approval.default_wait_seconds`` / ``approval.max_wait_seconds`` remain
    the wire protocol's per-window and refresh-budget numbers, still read by
    the planning doors and the conductor's wait windows. This knob overrides
    only the gate's TOTAL wait.

    Default is 0 = wait indefinitely. A build gate that nobody answers should
    keep waiting — like the spec digest pause — not cancel the build, because
    a cancelled gate has no re-offer path and the only recovery is re-planning
    the whole feature.
    """

    model_config = ConfigDict(extra="forbid")

    approval_max_wait_seconds: int = Field(
        default=DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS,
        ge=0,
        description=(
            "How long the build gate may wait for a human to answer its "
            "approval card, in seconds. 0 (the default) means wait "
            "indefinitely — the card stays live until someone answers. A "
            "positive number restores a hard ceiling: a build whose card is "
            "not answered within that many seconds is cancelled."
        ),
    )


class FilesystemPermissions(BaseModel):
    """Filesystem permissions enforced by ``pipeline_consumer``.

    ``allowlist`` is **required** — the system intentionally has no implicit
    default so that an operator misconfiguration cannot accidentally widen
    Forge's authorised filesystem footprint. All entries must be absolute
    paths (validator below).
    """

    model_config = ConfigDict(extra="forbid")

    allowlist: list[Path] = Field(
        ...,
        description=(
            "Absolute filesystem paths the pipeline consumer may read or "
            "write. Builds targeting any path outside the allowlist are "
            "rejected before reaching the state machine."
        ),
    )

    @field_validator("allowlist")
    @classmethod
    def _validate_absolute(cls, value: list[Path]) -> list[Path]:
        """Reject relative paths in ``allowlist``.

        Pydantic happily accepts a string like ``"./builds"`` and turns it
        into a ``Path``. That value would silently resolve relative to the
        process CWD at runtime, which is exactly the kind of authorisation
        ambiguity the allowlist exists to prevent. We raise here so the
        misconfiguration is caught at config-load time.
        """
        offenders = [str(p) for p in value if not p.is_absolute()]
        if offenders:
            joined = ", ".join(offenders)
            raise ValueError(
                "filesystem.allowlist entries must be absolute paths; "
                f"got relative path(s): {joined}"
            )
        return value


class PermissionsConfig(BaseModel):
    """Top-level permissions block. Currently only filesystem permissions exist."""

    model_config = ConfigDict(extra="forbid")

    filesystem: FilesystemPermissions = Field(
        ...,
        description="Filesystem allowlist enforced by pipeline_consumer.",
    )


class QueueConfig(BaseModel):
    """Configuration for the ``forge queue`` lifecycle (FEAT-FORGE-001 / PSM).

    Defaults are anchored to the Pipeline State Machine assumptions manifest
    (ASSUM-001 — minimum turn budget = 1). Operators may override any field
    in ``forge.yaml`` but the defaults must continue to match the assumptions
    manifest so this in-memory schema stays the canonical source of truth for
    downstream consumers (TASK-PSM-008/009/010/011).

    The ``ge=1`` validator on ``default_max_turns`` gives the CLI's
    "turn budget < 1 rejected" rejection branch automatically — no extra
    branch is required at the call site.
    """

    model_config = ConfigDict(extra="forbid")

    default_max_turns: int = Field(
        default=5,
        ge=1,
        description=(
            "ASSUM-001 (PSM) — default per-build turn budget. Must be at "
            "least 1; values below 1 are rejected at config-load time."
        ),
    )
    default_sdk_timeout_seconds: int = Field(
        default=1800,
        ge=1,
        description=(
            "Default SDK timeout (seconds) applied to a build when the "
            "caller does not specify one."
        ),
    )
    default_history_limit: int = Field(
        default=50,
        ge=1,
        description=(
            "Default history-row limit applied when listing past builds "
            "via ``forge queue history``."
        ),
    )
    repo_allowlist: list[Path] = Field(
        default_factory=list,
        description=(
            "Repository paths matched by ``forge queue --repo``. An empty "
            "list (the default) means no repository restriction is applied."
        ),
    )

    # -- the work queue the factory keeps for itself (Lane B stage one) ----
    # These three describe the queue of SENTENCES waiting to become planning
    # runs, not the build queue above. They are optional with defaults that
    # reproduce today's behaviour exactly, so a forge.yaml written before this
    # lane loads unchanged (this model is extra="forbid"; adding OPTIONAL
    # fields is safe, removing or renaming one is not).
    max_in_flight: int = Field(
        default=1,
        ge=1,
        description=(
            "How many pieces of work the factory may have running at once. "
            "One is today's behaviour: the take-next loop admits a queued "
            "sentence only when nothing is in flight."
        ),
    )
    order: Literal["shadow"] = Field(
        default="shadow",
        description=(
            "How the queue chooses what to take next. 'shadow' — the only "
            "value this stage accepts — takes the oldest waiting row and "
            "only SAYS which row a class order would have picked."
        ),
    )
    stale_after_days: int = Field(
        default=7,
        ge=1,
        description=(
            "How long a sentence may sit in the queue before the forge asks "
            "once, in the channel, whether to keep it or drop it."
        ),
    )


class BudgetGuards(BaseModel):
    """Per-profile build budget caps (FEAT-UBS-002) — and the leg knobs.

    Every cap is optional. ``None`` means *no cap* — the attended-mode
    semantics preserved from FEAT-FORGE-008 ASSUM-010 (reviewer-driven Mode C
    termination, no numeric iteration cap). A cap constrains a build only when
    it is set to a positive value in an *unattended* profile. This model is the
    declarative half; enforcement lives in
    :mod:`forge.pipeline.budget_guard` (a profile layered *on top* of the Mode C
    planner, never a rewrite of it).

    The ``leg_*`` fields: budgets that are NOT caps
    -----------------------------------------------

    The four caps above bound a **build**. The ``leg_*`` fields bound one
    **leg** — a single ``guardkit task-review`` / ``guardkit task-work``
    subprocess — and the pipeline threads them onto that subprocess's argv
    (``--max-turns`` / ``--sdk-timeout`` / ``--leg-budget``). Until this
    field group existed the pipeline could not name them at all: the
    conductor dispatcher's only extra argv was ``--model <seat>``, so the
    build system's hardcoded defaults (2 turns / 420s / 1620s) governed
    production and moving them was an image-level change. The experiment
    round needs them turnable from ``forge.yaml``, so here they are.

    They are deliberately **not** members of :attr:`caps_enabled`. That
    property answers one question — "is this an unattended-style profile,
    so the budget guard and the lifecycle budget observer arm?" — and a
    per-leg turn budget does not make a journey unattended. Folding them in
    would (a) arm the whole guard/observer machinery for a profile whose
    every *build* cap is ``None``, and (b) make the reserved ``attended``
    profile unable to carry a leg budget at all, since ``BudgetConfig``
    rejects an armed ``attended`` (ASSUM-010). The cap law's posture —
    including :func:`forge.config.conductor.mode_c_cap_refusal`, which reads
    ``max_review_cycles`` and nothing else — is untouched by this group.

    **Absent means byte-identical.** Every ``leg_*`` field defaults to
    ``None``, and ``None`` emits no argv token whatsoever; the leg's own
    default then governs, exactly as it did before this group existed.

    **Deploy-order law, reverse direction** (the conductor-activation design
    pass states the forward one for ``conductor.seat``, §"the deploy-order
    law"). Adding OPTIONAL fields to an ``extra=forbid`` model is safe in
    ONE direction only: an old ``forge.yaml`` stays valid against this new
    schema. The reverse is the hazard — a ``forge.yaml`` that has grown a
    ``leg_max_turns:`` key, read by a process still running the OLD schema,
    is refused **whole**, because the loader propagates the
    ``ValidationError`` unwrapped. That binds the langgraph sidecar too: it
    lazily re-reads the SAME yaml per invocation and degrades to a
    PERMISSIVE base-dir-only filesystem check on any load failure, so a
    pre-schema sidecar meeting a new key silently weakens the routine path's
    worktree confinement. Order is: merge → daemon image rebuild + recreate
    AND sidecar stop-wait-start → THEN the yaml gains the ``leg_*`` keys.
    """

    model_config = ConfigDict(extra="forbid")

    max_review_cycles: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Cap on Mode C follow-up review cycles. ``None`` = no cap "
            "(ASSUM-010). On breach the build pauses and escalates rather "
            "than running further reviews."
        ),
    )
    max_build_wallclock_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Cap on total build wall-clock (seconds). ``None`` = no cap. "
            "Distinct from the runner's per-subprocess timeout — this bounds "
            "the whole build."
        ),
    )
    max_build_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional cap on tokens consumed by a build (LangSmith-tagged or "
            "parsed from harness output). ``None`` = no cap / not measured."
        ),
    )
    min_coach_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Optional Coach-score floor. STUB (ADR-ARCH-033): enforcement is "
            "inert until the runner populates ``last_coach_score`` — the "
            "coach-score gap that is itself a UBS-002 prerequisite. Set here "
            "so the guard activates automatically once the score flows."
        ),
    )
    leg_max_turns: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Player-Coach turns for ONE fix-journey work leg, threaded as "
            "'--max-turns <n>'. ``None`` (the default) emits nothing and the "
            "leg keeps its own default of 2. WORK LEGS ONLY: 'guardkit "
            "task-review' does not declare --max-turns, and an undeclared "
            "flag is a parse-time exit 2 — a dead journey, not a slow one."
        ),
    )
    leg_sdk_timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-invocation model budget for ONE fix-journey leg (seconds), "
            "threaded as '--sdk-timeout <n>'. ``None`` (the default) emits "
            "nothing and each leg keeps its own default (480s review / 420s "
            "work). BOTH leg kinds declare this flag. Keep it under the "
            "leg's outer subprocess tripwire (600s review / 1800s work) or "
            "the inner-under-outer margin that lets a timed-out leg write an "
            "honest receipt instead of being SIGKILLed is gone."
        ),
    )
    leg_budget_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The loop-level clock for ONE fix-journey work leg (seconds), "
            "threaded as '--leg-budget <n>'. ``None`` (the default) emits "
            "nothing and the leg keeps its own default of 1620s. WORK LEGS "
            "ONLY — 'guardkit task-review' does not declare --leg-budget. "
            "Keep it under the 1800s work-stage tripwire for the same "
            "inner-under-outer reason as the SDK timeout."
        ),
    )

    @property
    def caps_enabled(self) -> bool:
        """Whether any cap is configured (i.e. this is an unattended-style profile).

        Deliberately includes ``min_coach_score`` even though its enforcement is a
        STUB (ADR-ARCH-033): a configured floor is still a cap for ASSUM-010
        purposes, so ``BudgetConfig`` correctly rejects arming the reserved
        ``attended`` profile with only a floor. The CLI annotates the floor as
        dormant when it echoes the caps, so an inert stub is not misrepresented as
        an active cap. Do not drop ``min_coach_score`` here without also moving the
        attended-arming guard, or ASSUM-010 leaks.

        And deliberately EXCLUDES the ``leg_*`` group: those bound one
        subprocess, not the build, and nothing this property gates — the
        budget guard, the lifecycle budget observer, the attended-arming
        validator — has any business firing because a leg was given fewer
        turns. See the class docstring for the full statement.
        """
        return any(
            value is not None
            for value in (
                self.max_review_cycles,
                self.max_build_wallclock_seconds,
                self.max_build_tokens,
                self.min_coach_score,
            )
        )


def _default_budget_profiles() -> dict[str, BudgetGuards]:
    """Three built-in profiles: ``attended``, ``unattended``, ``fix-journey``.

    ``fix-journey`` is the conductor's bounded repair profile (revival
    design pass §d Stage 3). Its ``max_review_cycles`` is **2, and must
    stay 2** — see :data:`FIX_JOURNEY_MAX_REVIEW_CYCLES` for the
    cap-mapping law: "one follow-up review" counts the initial review
    too, so a ``1`` here false-pauses every fix build before the
    mandatory follow-up.
    """
    return {
        ATTENDED_PROFILE_NAME: BudgetGuards(),  # all None — ASSUM-010 preserved
        "unattended": BudgetGuards(
            max_review_cycles=DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES,
            max_build_wallclock_seconds=(
                DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS
            ),
        ),
        FIX_JOURNEY_PROFILE_NAME: BudgetGuards(
            # = the initial /task-review + ONE follow-up. NEVER 1.
            max_review_cycles=FIX_JOURNEY_MAX_REVIEW_CYCLES,
            max_build_wallclock_seconds=(
                FIX_JOURNEY_MAX_BUILD_WALLCLOCK_SECONDS
            ),
        ),
    }


class BudgetConfig(BaseModel):
    """Named budget-guard profiles for the Unattended Build Service (UBS-002).

    ``forge queue --profile <name>`` selects one profile; the daemon resolves
    the caps for the build. The ``attended`` profile is reserved and must keep
    every cap unset (ASSUM-010) — the model_validator rejects any config that
    arms it, so an operator cannot silently turn attended builds into capped
    ones by editing the wrong block.
    """

    model_config = ConfigDict(extra="forbid")

    default_profile: str = Field(
        default=ATTENDED_PROFILE_NAME,
        description=(
            "Profile applied when a build does not request one. Defaults to "
            "``attended`` (caps off) so unattended caps are strictly opt-in."
        ),
    )
    profiles: dict[str, BudgetGuards] = Field(
        default_factory=_default_budget_profiles,
        description="Map of profile name → budget caps.",
    )

    @model_validator(mode="after")
    def _validate_profiles(self) -> BudgetConfig:
        """Reject a missing default profile or an armed ``attended`` profile."""
        if self.default_profile not in self.profiles:
            raise ValueError(
                f"budget.default_profile {self.default_profile!r} is not one "
                f"of the defined profiles {sorted(self.profiles)!r}"
            )
        attended = self.profiles.get(ATTENDED_PROFILE_NAME)
        if attended is not None and attended.caps_enabled:
            raise ValueError(
                f"budget.profiles[{ATTENDED_PROFILE_NAME!r}] must have all caps "
                "unset (FEAT-FORGE-008 ASSUM-010 — attended mode is "
                "reviewer-driven with no numeric cap); use a differently-named "
                "profile for capped builds"
            )
        return self

    def resolve(self, name: str | None) -> BudgetGuards:
        """Return the caps for ``name`` (or ``default_profile`` when ``None``).

        Raises:
            KeyError: If ``name`` is not a defined profile — surfaced so the
                CLI can list the known profiles rather than silently applying
                the default.
        """
        key = name if name is not None else self.default_profile
        try:
            return self.profiles[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown budget profile {key!r}; known profiles: "
                f"{sorted(self.profiles)!r}"
            ) from exc


class PlanningModelResolution(BaseModel):
    """Model resolution configuration for Mode P planning (FEAT-SPL-002).

    DF-004 (fleet REGISTER): planning model resolution can never silently
    escalate to cloud. The ``fallbacks`` list must remain empty; any non-empty
    value is flagged by ``audit_planning_model_resolution`` in
    ``src/forge/planning/audit.py``.

    The audit is deliberately NOT a Pydantic validator — a validator would
    brick the whole daemon on violation, contradicting ASSUM-011's "build
    intake unaffected" (DDR-007 soft-fail posture).
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description=(
            "Primary planning model identifier. None means use the "
            "specialist-agent default."
        ),
    )
    fallbacks: list[str] = Field(
        default_factory=list,
        description=(
            "FORBIDDEN per DF-004 — must remain empty. Cloud escalation is not "
            "permitted for planning models. See audit_planning_model_resolution."
        ),
    )


class TargetTerminalConfig(BaseModel):
    """Configuration for the Lane B / Phase E1 forge target terminal.

    The target terminal is the machine chain that runs *after*
    PLANNED_HANDOFF — it automates the Factory-2 coordinator sequence
    (handoff → spec leg → plan leg → build queue) so the planning run no
    longer terminates at PLANNED_HANDOFF but chains
    FEATURE_SPEC → FEATURE_PLAN → BUILD_QUEUED.

    Defaults are chosen for safe opt-in: ``enabled=False`` means the extra
    transitions are unreachable and PLANNED_HANDOFF keeps its current
    terminal behaviour. Flag OFF is a byte-for-byte no-op — the planning
    state machine is byte-identical to the shipped table (proven by the
    state-table regression test). Flag ON only ADDS transitions; it never
    removes PLANNED_HANDOFF as a reachable fallback terminal.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the Lane B target terminal. False = the "
            "planning chain terminates at PLANNED_HANDOFF exactly as it does "
            "today (byte-for-byte no-op). True = the chain continues into "
            "FEATURE_SPEC -> FEATURE_PLAN -> BUILD_QUEUED."
        ),
    )


class PlanningDigestReviewConfig(BaseModel):
    """The machine chain's ONE pause: the spec digest review.

    After the spec is written the chain shows the owner a plain-language list —
    one sentence per worked example, mechanically checked against the examples
    themselves — and waits for their word. This config governs the single place
    where the recommendation and the letter of the "a person's taps go DOWN,
    never up" rule genuinely disagree.

    ``always_ask`` (the default, and the recommendation): the card always asks.
    A feature with no assumptions still has worked examples, and it is the
    examples, not the assumptions, that say what will be built. Auto-approving
    them would mean the machine can specify and queue a build no person ever
    saw, which is the exact hole this pause exists to close.

    ``always_ask=False``: skip the card on a THIN feature only — no assumptions
    at all AND no more than ``skip_max_scenarios`` worked examples. Mechanically
    decidable, no judgement, no model, and the skip is recorded durably when it
    happens. Both paths are built and tested so the choice costs a value here
    rather than a rebuild.
    """

    model_config = ConfigDict(extra="forbid")

    always_ask: bool = Field(
        default=True,
        description=(
            "True (default) = the spec digest card always asks. False = skip "
            "the card on a thin feature (no assumptions and at most "
            "skip_max_scenarios worked examples); every other feature still "
            "asks."
        ),
    )
    skip_max_scenarios: int = Field(
        default=3,
        ge=0,
        description=(
            "Only consulted when always_ask is False: the most worked examples "
            "a feature may have and still skip the card."
        ),
    )


class PlanningConfig(BaseModel):
    """Configuration for Mode P planning approval-routing (FEAT-SPL-002).

    This config surface is deliberately separate from ``ApprovalConfig`` —
    ``ApprovalConfig`` is closed (extra='forbid', docstring forbids escalation
    fields) and governs the build-gating approval protocol (FEAT-FORGE-004).
    Mode P planning has different routing needs and must not pollute that
    surface.

    Defaults are chosen for safe opt-in: ``enabled=False`` ensures planning
    intake is deliberate; ``frontier_enabled=False`` keeps DF-006 frontier
    client gated until explicitly configured.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for Mode P planning intake. False = planning "
            "requests are rejected at the boundary."
        ),
    )
    escalation_approver: str | None = Field(
        default=None,
        description=(
            "Identity of the human approver for escalated planning requests. "
            "None = no escalation routing configured."
        ),
    )
    originator_wait_seconds: int = Field(
        default=3600,
        ge=0,
        description=(
            "Wait time for originator approval (seconds, non-negative). "
            "1h ratified by Rich 2026-07-06 (ASSUM-004 amendment, "
            "TASK-MP-012): long enough for a human answering a phone ping, "
            "so escalation stays the exception, not the common path."
        ),
    )
    escalated_wait_seconds: int = Field(
        default=14400,
        ge=0,
        description=(
            "Wait time for escalated approval (seconds, non-negative). "
            "4h ratified by Rich 2026-07-06 (ASSUM-004 amendment, "
            "TASK-MP-012): bounds the escalated window inside a working "
            "day; TIMED_OUT is cheap (resubmission is one Slack message)."
        ),
    )
    defer_cap: int = Field(
        default=3,
        ge=1,
        description=(
            "Maximum number of times a planning request can be deferred back "
            "to the originator before terminal action is taken."
        ),
    )
    default_target_repo: str | None = Field(
        default=None,
        description=(
            "Default target repository for planning requests that don't "
            "specify one. Format: 'org/name' (validated when set)."
        ),
    )
    target_repo_paths: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapping of 'org/name' repository identifiers to absolute local "
            "working-copy paths. Used for handoff to local build."
        ),
    )
    terminal: str = Field(
        default="planned-handoff",
        description=(
            "Terminal state name for successfully completed planning workflow."
        ),
    )
    frontier_enabled: bool = Field(
        default=False,
        description=(
            "Enable DF-006 frontier client for planning model access. "
            "False = frontier disabled."
        ),
    )
    frontier_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="Timeout for frontier client requests (seconds, >= 1).",
    )
    model_resolution: PlanningModelResolution = Field(
        default_factory=PlanningModelResolution,
        description=(
            "Planning model resolution configuration. See DF-004 audit for "
            "fallback restrictions."
        ),
    )
    target_terminal: TargetTerminalConfig = Field(
        default_factory=TargetTerminalConfig,
        description=(
            "Lane B / Phase E1 forge target-terminal configuration. Defaults "
            "to disabled (enabled=False) — the planning chain terminates at "
            "PLANNED_HANDOFF exactly as today (byte-for-byte no-op)."
        ),
    )
    digest_review: PlanningDigestReviewConfig = Field(
        default_factory=PlanningDigestReviewConfig,
        description=(
            "The machine chain's one pause — the spec digest review. Defaults "
            "to always_ask=True: the card always asks."
        ),
    )

    @field_validator("default_target_repo")
    @classmethod
    def _validate_repo_format(cls, v: str | None) -> str | None:
        """Validate repository format matches org/name pattern."""
        if v is None:
            return v

        import re

        pattern = r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
        if not re.match(pattern, v):
            raise ValueError(
                f"default_target_repo must match pattern 'org/name', got: {v!r}"
            )
        return v


class DeployStageConfig(BaseModel):
    """Configuration for the WS2-B8 output-side deploy + live-gate stages.

    Deliberately separate from ``ApprovalConfig`` (build-gating) and
    ``PlanningConfig`` (Mode P) — the deploy stage is a distinct concern with
    its own opt-in switch. Defaults are chosen for safe opt-in: ``enabled=False``
    means the DEPLOY / LIVE_GATE stages are DISABLED in production until V1
    (scope-design §4 + DF-017 note: the go-live gate is discharged, but the
    stages still default off until validation). Flag OFF is a byte-for-byte
    no-op — nothing in the pipeline dispatches a deploy.

    The reservation backend is swappable (scope Q2: NATS KV lease vs a DF-002
    ledger extension) — v1 ships the ``none`` (in-process) backend; ``kv`` is
    reserved for the real GB10-GPU lease and is not wired here.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the output-side DEPLOY + LIVE_GATE stages. "
            "False = the stages are inert (disabled in production until V1); "
            "flag OFF is a byte-for-byte no-op."
        ),
    )
    run_live_gate: bool = Field(
        default=True,
        description=(
            "Whether the LIVE_GATE stage runs after a successful DEPLOY. "
            "When False, a deploy completes without a live-gate verdict "
            "(deploy-only profiles, e.g. a headless service with no walk)."
        ),
    )
    reservation_backend: Literal["none", "kv"] = Field(
        default="none",
        description=(
            "Reservation-lease backend (scope Q2, swappable): 'none' = the "
            "in-process lease (v1); 'kv' = a NATS KV lease (reserved, not wired "
            "here). The DeployStageRunner takes/releases the profile's "
            "reservation.resource through this backend."
        ),
    )
    deploy_record_dir: str = Field(
        default="docs/state",
        description=(
            "Directory (repo-relative) under which F7 deploy records are "
            "written: <deploy_record_dir>/<task>/deploy-record-<date>.md "
            "(the MP-012 addenda pattern)."
        ),
    )
    execution_surface: Literal["local", "sidecar"] = Field(
        default="local",
        description=(
            "Where the deploy stage's docker-touching scripts (deploy_compose, "
            "health_check) physically run. 'local' (default) = today's in-process "
            "subprocess via forge.executor.shell_steps — a byte-identical no-op "
            "for the attended host CLI. 'sidecar' routes those scripts over "
            "loopback HTTP to the forge-deploy-sidecar (S1, C4 residue #24), which "
            "is what lets the in-container daemon dispatch deploys without a docker "
            "socket. All other subprocess steps (seed/warm/import/smoke) stay "
            "in-process regardless of this switch."
        ),
    )
    sidecar_url: str = Field(
        default="http://127.0.0.1:8125",
        description=(
            "Base URL of the forge-deploy-sidecar, used only when "
            "execution_surface='sidecar'. Loopback-only by default (the sidecar "
            "binds 127.0.0.1); a remote value is a deliberate, reviewed choice."
        ),
    )


class ReviewGateConfig(BaseModel):
    """Configuration for the WS3-S5 adversarial merge-review gate.

    The gate formalizes the practiced N-reviewers / ≥2-refuters /
    refuted-by-default / executed-reproduction workflow (LPA-14/15) as an
    **attended checkpoint** (Q2 = attended-v1, Rich 2026-07-09): the stage
    assembles the review packet, dispatches the reviewer fan-out, enforces
    refuted-by-default (a finding without an executed reproduction is
    structurally unable to reach ``confirmed``), emits the F14
    review-findings record, and pauses for the human checkpoint's disposition.
    The pause routes through the EXISTING approval-gate machinery (Gate
    G1-proven; DF-001/DF-003/DF-009 — identity-pinned attended checkpoint,
    reviewer-seat SLM localisation is WS4's) via an injected seam that is
    present but UNWIRED in v1 — the attended operator dispositions the emitted
    record directly.

    Deliberately separate from ``ApprovalConfig`` (build-gating),
    ``PlanningConfig`` (Mode P) and ``DeployStageConfig`` (output-side deploy)
    — the merge gate is a distinct concern with its own opt-in switch. Defaults
    are chosen for safe opt-in: ``enabled=False`` means the gate is INERT in
    production (same rollout pattern as ``deploy.enabled``) — nothing dispatches
    a review and the attended CLI refuses to run until an operator opts in. Flag
    OFF is a byte-for-byte no-op.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the WS3-S5 adversarial merge gate. False = the "
            "gate is inert (disabled in production until reviewer-seat SLMs "
            "land in WS4); flag OFF is a byte-for-byte no-op — nothing "
            "dispatches a review and the attended CLI refuses to run."
        ),
    )
    dimensions: list[str] = Field(
        default_factory=lambda: [
            "spec-fidelity",
            "correctness",
            "wire-topology",
            "assumptions",
            "tracker-consistency",
        ],
        description=(
            "The review dimensions dispatched in the reviewer fan-out (one "
            "reviewer per dimension). Default mirrors the DD4F post-merge "
            "review's five dimensions (LPA-14). Every critical/high finding "
            "additionally gets ≥2 independent refuters."
        ),
    )
    min_refuters: int = Field(
        default=2,
        ge=2,
        description=(
            "Minimum independent refuters per critical/high finding (LPA-14). "
            "Floor is 2 — a finding survives only by surviving refutation."
        ),
    )
    record_dir: str = Field(
        default="qa",
        description=(
            "Directory (repo-relative) under which F14 review-findings records "
            "are written: <record_dir>/review-<review_id>.yaml (the structured "
            "form of the docs/reviews/<id>.md review block)."
        ),
    )


#: Conservative default floors for the pre-run resource preflight (O-27/O-29).
#: The co-resident 4-model seat stack sits at ~14 GB steady-state headroom, so an
#: 8 GB memory floor refuses only a genuinely starved box; the 20 GB disk floor
#: clears the 10 GB JetStream store plus rollback-image churn.
DEFAULT_PREFLIGHT_MEMORY_FLOOR_GB = 8.0
DEFAULT_PREFLIGHT_DISK_FLOOR_GB = 20.0


class ResourcePreflightConfig(BaseModel):
    """Pre-run memory/disk headroom preflight (O-27 / O-29 — E2-S4).

    A run must fail CLEANLY *before* it starts when the box is already under its
    memory or disk floor — never mid-run with a kernel OOM-kill (O-27) or an
    ENOSPC-wedged JetStream/worktree write (O-29). The run-entry path consults
    :func:`forge.preflight.run_resource_preflight`; on a breach the run is
    refused into a loud FAILED terminal with a route-and-notify to the
    originator (never a mid-run kill).

    Unlike the other stage switches this defaults **enabled=True**: the check
    only ever refuses BEFORE work starts, so leaving it on is safe (it can never
    kill a run in flight). An *unreadable* resource (non-Linux dev host, etc.)
    is treated as UNCHECKED, never a fabricated breach — the preflight fails open
    per-resource.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the pre-run resource preflight. True (default) = "
            "refuse a run BEFORE it starts if memory/disk is below floor; a "
            "breach is a loud FAILED terminal + originator notification, never a "
            "mid-run kill. False = the check is skipped entirely (no readings)."
        ),
    )
    min_available_memory_gb: float = Field(
        default=DEFAULT_PREFLIGHT_MEMORY_FLOOR_GB,
        gt=0,
        description=(
            "Minimum available system memory (GiB, from /proc/meminfo "
            "MemAvailable) required to START a run. Below this the run is "
            "refused before any seat-holding dispatch."
        ),
    )
    min_available_disk_gb: float = Field(
        default=DEFAULT_PREFLIGHT_DISK_FLOOR_GB,
        gt=0,
        description=(
            "Minimum free space (GiB) on the working filesystem required to "
            "START a run. Below this the run is refused before any deploy / "
            "worktree / JetStream write can hit ENOSPC."
        ),
    )
    working_path: str | None = Field(
        default=None,
        description=(
            "Filesystem whose free space is checked. None = the forge process "
            "working directory (where worktrees, deploy compose and the "
            "JetStream store live on the single GB10 box)."
        ),
    )


class ConductorConfig(BaseModel):
    """The conductor's activation switch (revival design pass §a.5 / §h.8).

    The conductor (codename: the Supervisor) is the pipeline's full brain —
    it can walk a whole journey rather than a single build. It is fully
    built and, in production, completely unplugged: no mode reader, no
    driver loop. This section is the single switch that activates it.

    ``enabled`` defaults to **False** and is the *only* field Stage 1
    introduces. With the switch off the tree is byte-for-byte today's
    behaviour: every build falls through to the routine path (Mode A),
    and ``forge queue --mode c`` is refused at queue time rather than
    writing a build row nothing would ever pick up.

    Deliberately its own section rather than a field on ``PipelineConfig``:
    activation is a plan-of-record decision reserved to the owner, and it
    must be readable at a glance in ``forge.yaml``.

    ``seat`` is the fix journey's SEAT — the local model every fix-journey
    leg runs on (conductor-activation design pass §2). It landed here as
    config-as-code and DELETED the ``FORGE_CONDUCTOR_LEG_MODEL`` stopgap
    env read the composition root used to carry: two statements of one
    rule is a future lie. Switching the conductor on without naming a seat
    is REFUSED at config load — see :meth:`_enabled_requires_a_seat`.

    **Deploy-order law** (this model is ``extra=forbid`` and the loader
    propagates the ValidationError unwrapped): writing ``conductor.seat``
    into a deployed ``forge.yaml`` before the image that defines the field
    is running refuses the WHOLE config at load — and that binds the
    langgraph sidecar too, which lazily re-reads the same file and
    degrades to a permissive filesystem check on any load failure. Merge
    and redeploy BOTH surfaces first, then add the ``conductor:`` section.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the conductor (the full-journey brain). "
            "False (the default) = the conductor is inert: every build runs "
            "the routine path and 'forge queue --mode c' is refused at queue "
            "time. True = the fix journey is activated."
        ),
    )
    seat: str | None = Field(
        default=None,
        description=(
            "The fix journey's seat: the local model every fix-journey leg "
            "runs on, named on the leg's argv as '--model <seat>'. A bare "
            "fleet alias (the workhorse seat is the evidenced default). "
            "REQUIRED whenever enabled is true — an unnamed seat would ride "
            "None down to a frontier default, and only the build system's "
            "own chokepoint would stop it. Blank is read as absent, and a "
            "value starting with '-' is refused (it would land on the leg's "
            "argv as an option, not as a model name)."
        ),
    )

    @model_validator(mode="after")
    def _enabled_requires_a_seat(self) -> ConductorConfig:
        """Refuse an activated conductor that names no seat — or a bad one.

        The cap law's posture, applied to the seat: an unset seat is
        REFUSED, never silently read as "the leg picks its own". The build
        system's chokepoint refuses ``model=None`` AND the empty string at
        leg runtime anyway (``m0_fence``) — failing here means the daemon
        refuses to BOOT on a half-activated config rather than dying on
        the first leg of a journey an owner already approved.

        A DISABLED conductor with no seat stays valid: that is every
        deployed ``forge.yaml`` today.

        Blank normalises to ``None`` before the check, matching the
        composition root's long-standing strip-to-None posture — so
        ``config.conductor.seat`` is either absent or a real name, never a
        named nothing.

        **And the seat must be SHAPED like a name.** The value lands
        verbatim on the leg's argv as ``--model <seat>``, so a seat that
        starts with ``-`` is not a seat at all: the build system's own
        argument parser reads the next token as another OPTION, and the
        result is either an unknown-flag error deep inside a spawned leg or
        — worse — a real flag the operator never meant to pass. That is a
        config mistake, and it is refused HERE, at load, for the same
        reason the missing seat is: the daemon should refuse to boot on a
        malformed activation rather than die on the first leg of a journey
        an owner already approved. The check runs whenever a seat is named,
        enabled or not, because pre-loading the seat and flipping the flag
        later is a supported order and the flip must not be the moment the
        typo is discovered.
        """
        normalised = (self.seat or "").strip() or None
        if normalised != self.seat:
            self.seat = normalised
        if self.enabled and normalised is None:
            raise ValueError(
                "conductor.enabled is true but conductor.seat names no seat. "
                "The fix journey's legs run on a local model and the pipeline "
                "must name it on every dispatch; an unnamed seat falls through "
                "to a frontier default. Add 'seat: <local model>' to the "
                "conductor section, or set 'enabled: false'."
            )
        if normalised is not None and normalised.startswith("-"):
            raise ValueError(
                f"conductor.seat is {normalised!r}, which starts with a dash. "
                "The seat is passed to every fix-journey leg as "
                "'--model <seat>', so a leading dash makes it read as another "
                "command-line option rather than as the name of a model. Name "
                "the local model with no leading dash (for example "
                "'seat: qwen3-coder-30b')."
            )
        return self


class MergeExecutorConfig(BaseModel):
    """The merge word's activation switch (make-merge-work build spec 2026-08-24).

    The merge-and-deploy executor turns the build-complete card's "the merge
    word is yours" sentence into a mechanism: with the switch on, a clean
    routine build offers a [Merge & deploy] card and a server-side executor
    acts on the approval (merge -> re-check -> sandbox deploy -> live checks
    -> one-line report). ``enabled`` defaults to **False**, and with it off
    the tree is byte-for-byte today's behaviour: no card is offered after a
    clean build and no approval response is consumed.

    **Deploy-order law** (mirrors :class:`ConductorConfig` — this model is
    ``extra="forbid"`` and the loader propagates the ValidationError
    unwrapped): writing a ``merge_executor:`` section into a deployed
    ``forge.yaml`` before the image that defines the field is running refuses
    the WHOLE config at load — and that binds the langgraph sidecar too,
    which lazily re-reads the same file and degrades to a permissive
    filesystem check on any load failure. Merge and redeploy BOTH surfaces
    first, then add the ``merge_executor:`` section. This lane writes NO yaml
    key anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the merge-and-deploy executor (the merge word "
            "as a mechanism). False (the default) = byte-for-byte today's "
            "behaviour: no merge card is offered after a clean build and no "
            "approval response is consumed. True = a clean routine build "
            "offers the merge card and the executor acts on the approval."
        ),
    )
    response_wait_seconds: int = Field(
        default=86400,
        ge=0,
        description=(
            "timeout_seconds published on the merge approval request — how "
            "long the card presents itself as valid. Default one day: the "
            "merge word is an owner's act and may come long after the build "
            "finishes."
        ),
    )


class ForgeConfig(BaseModel):
    """Root model for ``forge.yaml``.

    ``fleet``, ``pipeline``, ``approval``, ``queue`` and ``budget`` are optional
    with sensible defaults so that a minimal ``forge.yaml`` only needs to
    declare the required ``permissions`` section. ``permissions`` itself is
    required because there is no safe default filesystem allowlist.
    """

    model_config = ConfigDict(extra="forbid")

    fleet: FleetConfig = Field(default_factory=FleetConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    budget: BudgetConfig = Field(
        default_factory=BudgetConfig,
        description=(
            "FEAT-UBS-002 budget-guard profiles. Defaults to attended "
            "(caps off); operators opt into unattended caps per profile."
        ),
    )
    planning: PlanningConfig = Field(
        default_factory=PlanningConfig,
        description=(
            "FEAT-SPL-002 Mode P planning approval-routing configuration. "
            "Defaults to disabled (enabled=False) for safe opt-in."
        ),
    )
    deploy: DeployStageConfig = Field(
        default_factory=DeployStageConfig,
        description=(
            "WS2-B8 output-side deploy + live-gate stage configuration. "
            "Defaults to disabled (enabled=False) — inert in production "
            "until V1 (scope-design §4)."
        ),
    )
    review_gate: ReviewGateConfig = Field(
        default_factory=ReviewGateConfig,
        description=(
            "WS3-S5 adversarial merge-gate configuration. Defaults to disabled "
            "(enabled=False) — the attended checkpoint is inert in production "
            "until reviewer-seat SLMs land in WS4 (Q2 = attended-v1)."
        ),
    )
    conductor: ConductorConfig = Field(
        default_factory=ConductorConfig,
        description=(
            "The conductor's activation switch (revival design pass §a.5). "
            "Defaults to disabled (enabled=False) — with it off the tree is "
            "byte-for-byte today's behaviour and '--mode c' is refused at "
            "queue time."
        ),
    )
    merge_executor: MergeExecutorConfig = Field(
        default_factory=MergeExecutorConfig,
        description=(
            "The merge word's activation switch (make-merge-work build spec "
            "2026-08-24). Defaults to disabled (enabled=False) — with it off "
            "no merge card is offered after a clean build and no approval "
            "response is consumed; today's behaviour byte-for-byte."
        ),
    )
    resource_preflight: ResourcePreflightConfig = Field(
        default_factory=ResourcePreflightConfig,
        description=(
            "O-27/O-29 pre-run memory/disk headroom preflight. Defaults to "
            "enabled=True — it only ever refuses a run BEFORE it starts, so it "
            "is safe on by default (never a mid-run kill)."
        ),
    )
    autobuild_gate: AutobuildGateConfig = Field(
        default_factory=AutobuildGateConfig,
        description=(
            "The build gate's human-approval wait (2026-08-26). Defaults to "
            "approval_max_wait_seconds=0 — the gate waits indefinitely for a "
            "human answer instead of cancelling the build after "
            "approval.max_wait_seconds."
        ),
    )
    permissions: PermissionsConfig = Field(
        ...,
        description=(
            "Required. Operators must explicitly declare permissions — there "
            "is no safe default filesystem allowlist."
        ),
    )


__all__ = [
    "ATTENDED_PROFILE_NAME",
    "DEFAULT_APPROVAL_MAX_WAIT_SECONDS",
    "DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS",
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "DEFAULT_APPROVED_ORIGINATORS",
    "DEFAULT_BUILD_QUEUE_SUBJECT",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_INTENT_MIN_CONFIDENCE",
    "DEFAULT_PREFLIGHT_DISK_FLOOR_GB",
    "DEFAULT_PREFLIGHT_MEMORY_FLOOR_GB",
    "DEFAULT_PROGRESS_INTERVAL_SECONDS",
    "DEFAULT_STALE_HEARTBEAT_SECONDS",
    "DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS",
    "DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES",
    "FIX_JOURNEY_MAX_BUILD_WALLCLOCK_SECONDS",
    "FIX_JOURNEY_MAX_REVIEW_CYCLES",
    "FIX_JOURNEY_PROFILE_NAME",
    "ApprovalConfig",
    "AutobuildGateConfig",
    "BudgetConfig",
    "BudgetGuards",
    "ConductorConfig",
    "DeployStageConfig",
    "FilesystemPermissions",
    "FleetConfig",
    "ForgeConfig",
    "PermissionsConfig",
    "PipelineConfig",
    "PlanningConfig",
    "PlanningModelResolution",
    "QueueConfig",
    "ResourcePreflightConfig",
    "ReviewGateConfig",
]


# Re-bind ``Any`` to silence unused-import warnings under linters that don't
# notice forward annotations introduced by ``from __future__ import annotations``.
_ = Any
