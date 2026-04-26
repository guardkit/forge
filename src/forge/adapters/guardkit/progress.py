"""Pydantic v2 DTO for GuardKit progress-stream NATS messages.

This module is the **declarative producer** for the GuardKit Command
Invocation Engine feature (FEAT-FORGE-005) progress-stream subscriber
implemented in TASK-GCI-005. It describes the typed shape of a single
``pipeline.stage-complete.*`` NATS message surfaced to ``forge status``
consumers and the live-progress view.

Authoritative completion still flows through ``GuardKitResult`` (see
:mod:`forge.adapters.guardkit.models` / TASK-GCI-001); this event is
**telemetry only**. A missing or slow progress stream must therefore
never fail an invocation — the BDD scenario "The authoritative result
still returns when progress streaming is unavailable" is the oracle for
that contract.

Notes
-----

- Pydantic v2 — keep declarative, no validators or business logic.
- ``timestamp`` is a ``str`` (ISO 8601), matching the nats-core
  convention used elsewhere in the project (no ``datetime`` field).
- Optional fields explicitly default to ``None`` so JSON round-tripping
  via ``model_dump_json()`` / ``model_validate_json()`` is symmetric.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GuardKitProgressEvent(BaseModel):
    """Typed shape of a single ``pipeline.stage-complete.*`` NATS message.

    The event is surfaced to ``forge status`` consumers and the
    live-progress view. Authoritative completion still flows through
    :class:`forge.adapters.guardkit.models.GuardKitResult`; this is
    telemetry only — the missing/slow stream must never fail an
    invocation (BDD scenario "The authoritative result still returns
    when progress streaming is unavailable").
    """

    build_id: str = Field(
        ...,
        description="Build identifier shared with the originating GuardKitResult.",
    )
    subcommand: str = Field(
        ...,
        description="GuardKit subcommand label (e.g. '/feature-spec', 'autobuild').",
    )
    stage_label: str = Field(
        ...,
        description="Human-readable label of the completed pipeline stage.",
    )
    seq: int = Field(
        ...,
        description="Monotonic sequence number per invocation (gap = lost event).",
    )
    coach_score: float | None = Field(
        default=None,
        description="Coach quality score for this stage (None when not scored).",
    )
    artefact: str | None = Field(
        default=None,
        description="Path or URI of the artefact produced by this stage.",
    )
    timestamp: str = Field(
        ...,
        description=(
            "ISO 8601 timestamp emitted by the publisher (string, not datetime)."
        ),
    )
