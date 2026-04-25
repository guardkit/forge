"""Pydantic v2 models describing the ``forge.yaml`` configuration surface.

These models are the **declarative producer** for the NATS Fleet Integration
feature (FEAT-FORGE-002). The defaults below are anchored to the assumptions
manifest (see ``features/nats-fleet-integration/nats-fleet-integration_assumptions.yaml``):

- ASSUM-001: ``FleetConfig.heartbeat_interval_seconds`` = 30
- ASSUM-002: ``FleetConfig.stale_heartbeat_seconds`` = 90
- ASSUM-003: ``FleetConfig.cache_ttl_seconds`` = 30
- ASSUM-004: ``FleetConfig.intent_min_confidence`` = 0.7
- ASSUM-005: ``PipelineConfig.progress_interval_seconds`` = 60

Downstream consumers (TASK-NFI-004/005/007) import these models from
``forge.config`` and must not duplicate any of the defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class ForgeConfig(BaseModel):
    """Root model for ``forge.yaml``.

    ``fleet`` and ``pipeline`` are optional with sensible defaults so that a
    minimal ``forge.yaml`` only needs to declare the required ``permissions``
    section. ``permissions`` itself is required because there is no safe
    default filesystem allowlist.
    """

    model_config = ConfigDict(extra="forbid")

    fleet: FleetConfig = Field(default_factory=FleetConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    permissions: PermissionsConfig = Field(
        ...,
        description=(
            "Required. Operators must explicitly declare permissions — there "
            "is no safe default filesystem allowlist."
        ),
    )


__all__ = [
    "DEFAULT_APPROVED_ORIGINATORS",
    "DEFAULT_BUILD_QUEUE_SUBJECT",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_INTENT_MIN_CONFIDENCE",
    "DEFAULT_PROGRESS_INTERVAL_SECONDS",
    "DEFAULT_STALE_HEARTBEAT_SECONDS",
    "FilesystemPermissions",
    "FleetConfig",
    "ForgeConfig",
    "PermissionsConfig",
    "PipelineConfig",
]


# Re-bind ``Any`` to silence unused-import warnings under linters that don't
# notice forward annotations introduced by ``from __future__ import annotations``.
_ = Any
