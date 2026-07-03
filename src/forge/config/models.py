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
from typing import Any

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
DEFAULT_APPROVED_ORIGINATORS: tuple[str, ...] = (
    "terminal",
    "voice-reachy",
    "telegram",
    "slack",
    "dashboard",
    "cli-wrapper",
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


class BudgetGuards(BaseModel):
    """Per-profile build budget caps (FEAT-UBS-002).

    Every cap is optional. ``None`` means *no cap* — the attended-mode
    semantics preserved from FEAT-FORGE-008 ASSUM-010 (reviewer-driven Mode C
    termination, no numeric iteration cap). A cap constrains a build only when
    it is set to a positive value in an *unattended* profile. This model is the
    declarative half; enforcement lives in
    :mod:`forge.pipeline.budget_guard` (a profile layered *on top* of the Mode C
    planner, never a rewrite of it).
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
    """Two built-in profiles: ``attended`` (caps off) and ``unattended``."""
    return {
        ATTENDED_PROFILE_NAME: BudgetGuards(),  # all None — ASSUM-010 preserved
        "unattended": BudgetGuards(
            max_review_cycles=DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES,
            max_build_wallclock_seconds=(
                DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS
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
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "DEFAULT_APPROVED_ORIGINATORS",
    "DEFAULT_BUILD_QUEUE_SUBJECT",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_INTENT_MIN_CONFIDENCE",
    "DEFAULT_PROGRESS_INTERVAL_SECONDS",
    "DEFAULT_STALE_HEARTBEAT_SECONDS",
    "DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS",
    "DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES",
    "ApprovalConfig",
    "BudgetConfig",
    "BudgetGuards",
    "FilesystemPermissions",
    "FleetConfig",
    "ForgeConfig",
    "PermissionsConfig",
    "PipelineConfig",
    "QueueConfig",
]


# Re-bind ``Any`` to silence unused-import warnings under linters that don't
# notice forward annotations introduced by ``from __future__ import annotations``.
_ = Any
