"""Pure-domain data models for the Forge dispatch layer.

This module defines the declarative pydantic schemas consumed by every
other task in FEAT-FORGE-003. It is the **producer** for two §4
Integration Contracts referenced in ``IMPLEMENTATION-GUIDE.md`` §4:

* :class:`DispatchAttempt` — one row per dispatch try (resolution_id +
  correlation_key + attempt counter).
* :data:`DispatchOutcome` — discriminated sum-type over the four
  terminal states (:class:`SyncResult`, :class:`AsyncPending`,
  :class:`Degraded`, :class:`DispatchError`).

The module is **pure domain**: no NATS imports, no transport concerns,
no I/O. ``CorrelationKey`` is intentionally an opaque ``str`` alias —
its 32-lowercase-hex format is validated at the boundary in
``CorrelationRegistry.bind`` (TASK-SAD-003), not in the Pydantic model
itself, because the type is opaque by design.

See ``tasks/backlog/TASK-SAD-001-dispatch-package-skeleton.md`` for the
canonical schema contract.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Opaque type alias for a 32-lowercase-hex correlation key. Format is
# validated at the boundary (CorrelationRegistry.bind, TASK-SAD-003), not
# here — the dispatch domain treats this as an opaque identifier.
CorrelationKey = str


class DispatchAttempt(BaseModel):
    """One dispatch attempt against a resolved capability.

    Attributes:
        resolution_id: The :class:`~forge.discovery.models.CapabilityResolution`
            ``resolution_id`` this attempt is bound to.
        correlation_key: 32-lowercase-hex correlation key used to link
            the request to its eventual outcome. Format validated at the
            boundary (TASK-SAD-003), not on this model.
        matched_agent_id: The agent the dispatch is targeting — copied
            from the resolution at attempt time.
        attempt_no: Monotonic attempt counter, starting at 1.
        retry_of: ``resolution_id`` of the previous attempt this one is
            retrying, or ``None`` for the first attempt.
    """

    model_config = ConfigDict(extra="forbid")

    resolution_id: str = Field(min_length=1, description="Resolution being dispatched")
    correlation_key: CorrelationKey = Field(
        min_length=1, description="Per-attempt correlation key (opaque)",
    )
    matched_agent_id: str = Field(
        min_length=1, description="Agent the dispatch targets",
    )
    attempt_no: int = Field(default=1, ge=1, description="Monotonic attempt counter")
    retry_of: str | None = Field(
        default=None,
        description="resolution_id of the prior attempt, or None for the first",
    )


class SyncResult(BaseModel):
    """Synchronous dispatch outcome — agent returned a result inline.

    Attributes:
        kind: Discriminator literal — always ``"sync_result"``.
        resolution_id: The originating resolution.
        attempt_no: The attempt counter this outcome belongs to.
        coach_score: Optional reviewer score in ``[0.0, 1.0]``. ``None``
            when no coach evaluation was performed.
        criterion_breakdown: Per-criterion scores as a free-form mapping.
        detection_findings: Detector findings emitted during evaluation.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sync_result"] = "sync_result"
    resolution_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    coach_score: float | None = Field(default=None, ge=0.0, le=1.0)
    criterion_breakdown: dict[str, Any] = Field(default_factory=dict)
    detection_findings: list[Any] = Field(default_factory=list)


class AsyncPending(BaseModel):
    """Asynchronous dispatch outcome — agent acknowledged and will reply later.

    Attributes:
        kind: Discriminator literal — always ``"async_pending"``.
        resolution_id: The originating resolution.
        attempt_no: The attempt counter this outcome belongs to.
        run_identifier: Opaque identifier supplied by the remote agent
            for later correlation of the eventual result.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["async_pending"] = "async_pending"
    resolution_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    run_identifier: str = Field(min_length=1)


class Degraded(BaseModel):
    """Degraded dispatch outcome — partial result or fallback path.

    Attributes:
        kind: Discriminator literal — always ``"degraded"``.
        resolution_id: The originating resolution.
        attempt_no: The attempt counter this outcome belongs to.
        reason: Human-readable description of why the outcome was degraded.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["degraded"] = "degraded"
    resolution_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    reason: str = Field(min_length=1)


class DispatchError(BaseModel):
    """Error dispatch outcome — terminal failure for this attempt.

    Attributes:
        kind: Discriminator literal — always ``"error"``.
        resolution_id: The originating resolution.
        attempt_no: The attempt counter this outcome belongs to.
        error_explanation: Human-readable error explanation.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["error"] = "error"
    resolution_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    error_explanation: str = Field(min_length=1)


# Discriminated union over the four terminal dispatch outcomes.
# The ``kind`` field on each variant drives Pydantic's discriminator,
# so ``model_validate`` round-trips back to the correct concrete class.
DispatchOutcome = Annotated[
    Union[SyncResult, AsyncPending, Degraded, DispatchError],
    Field(discriminator="kind"),
]


__all__ = [
    "AsyncPending",
    "CorrelationKey",
    "Degraded",
    "DispatchAttempt",
    "DispatchError",
    "DispatchOutcome",
    "SyncResult",
]
