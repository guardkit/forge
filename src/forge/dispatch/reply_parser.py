"""Specialist reply parser for the Forge dispatch layer.

This module converts a specialist reply payload (a ``dict`` arriving over
the transport adapter — see TASK-NFI-005 for the NATS subject contract)
into a :data:`~forge.dispatch.models.DispatchOutcome` discriminated-union
member.

Resolution order (TASK-SAD-005 acceptance criteria):

1. **Envelope validation FIRST.** If the payload fails to validate against
   :class:`SpecialistReplyEnvelope`, the parser produces a
   :class:`~forge.dispatch.models.DispatchError` carrying a
   ``schema_validation`` explanation. *Only field names and Pydantic
   error types* are surfaced — raw payload values are never logged or
   embedded, so sensitive parameters cannot leak via log scraping.

2. **Specialist error result** (``payload.error``) →
   :class:`~forge.dispatch.models.DispatchError` with the specialist's
   own explanation copied verbatim into ``error_explanation``.

3. **Async-mode initial reply** (``payload.run_identifier``) →
   :class:`~forge.dispatch.models.AsyncPending` carrying that opaque
   identifier for later polling.

4. **Synchronous result** — :class:`~forge.dispatch.models.SyncResult`
   built from the Coach fields. Extraction prefers top-level fields over
   the nested ``result`` block (see :func:`_extract_coach_fields`); the
   nested block is retained as fallback evidence only.

The parser is intentionally *pure*: no I/O, no network, no payload-value
logging. The single allowed observability hook emits the resolved
outcome kind (``sync_result``, ``async_pending``, ``error``) — never any
field value from the payload.

See ``tasks/design_approved/TASK-SAD-005-reply-parser.md`` for the
canonical acceptance-criteria contract.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge.dispatch.models import (
    AsyncPending,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Envelope schema
# ---------------------------------------------------------------------------


class SpecialistReplyEnvelope(BaseModel):
    """Boundary contract for a specialist reply payload.

    Only the *structural* identifying fields are required here; per-field
    semantics (Coach extraction, error vs async detection) are applied by
    :func:`parse_reply` against the original payload ``dict`` so that
    "is the field present?" distinctions are preserved (Pydantic defaults
    would erase that information for the optional branching fields).

    A reply is considered well-formed if and only if it identifies the
    responding specialist via ``agent_id``. Everything else is optional.

    ``extra="allow"`` so the dispatch parser does not become a brittle
    chokepoint when specialists evolve their reply shapes.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(
        min_length=1,
        description="Identifier of the specialist that produced the reply",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarise_validation_error(exc: ValidationError) -> str:
    """Render a :class:`ValidationError` as a value-free summary string.

    The summary lists only field paths and Pydantic error types — never
    the offending input values — so the resulting ``DispatchError`` can
    be safely logged without leaking sensitive payload content.
    """

    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        etype = err.get("type", "value_error")
        parts.append(f"{loc}({etype})")
    fields = ", ".join(sorted(set(parts))) if parts else "<unknown>"
    return f"schema validation failed: {fields}"


def _extract_coach_fields(
    payload: dict[str, Any],
) -> tuple[float | None, dict[str, Any], list[Any]]:
    """Return ``(coach_score, criterion_breakdown, detection_findings)``.

    Extraction rule (A.coach-output-top-vs-nested):

    * Top-level fields on ``payload`` are preferred.
    * The nested ``payload["result"]`` block is consulted as a fallback
      *only* when the corresponding top-level field is absent or empty.

    ``coach_score`` is treated specially: ``0.0`` is a legitimate score,
    so we use a presence check (``is None``) rather than truthiness when
    deciding whether to fall back. ``criterion_breakdown`` and
    ``detection_findings`` use truthiness — an empty dict/list at the
    top level is treated as "no useful evidence" and falls back.
    """

    nested_raw = payload.get("result")
    nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else {}

    # coach_score: presence-first preference (0.0 must beat nested fallback).
    top_score = payload.get("coach_score")
    score: float | None
    if top_score is not None:
        score = top_score
    else:
        score = nested.get("coach_score")

    # Collections: empty top-level → nested fallback → empty default.
    breakdown_raw = (
        payload.get("criterion_breakdown")
        or nested.get("criterion_breakdown")
        or {}
    )
    breakdown: dict[str, Any] = (
        breakdown_raw if isinstance(breakdown_raw, dict) else {}
    )

    findings_raw = (
        payload.get("detection_findings")
        or nested.get("detection_findings")
        or []
    )
    findings: list[Any] = findings_raw if isinstance(findings_raw, list) else []

    return score, breakdown, findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_reply(
    payload: dict[str, Any],
    *,
    resolution_id: str,
    attempt_no: int,
) -> DispatchOutcome:
    """Convert a specialist reply payload into a :data:`DispatchOutcome`.

    Args:
        payload: The raw reply ``dict`` as delivered by the transport
            adapter. May carry top-level Coach fields, a nested
            ``result`` block, an ``error`` string, or a ``run_identifier``
            string. The function never mutates this argument.
        resolution_id: The originating ``CapabilityResolution.resolution_id``
            this attempt is bound to.
        attempt_no: Monotonic attempt counter — propagated onto the
            produced outcome so retries are distinguishable.

    Returns:
        Exactly one of :class:`SyncResult`, :class:`AsyncPending`, or
        :class:`DispatchError`. (:class:`Degraded` is owned by the
        gating layer in FEAT-FORGE-004 and is never produced here.)

    Resolution order:
        1. Envelope validation fails → :class:`DispatchError` with
           ``error_explanation`` mentioning ``schema validation``.
        2. ``payload['error']`` truthy → :class:`DispatchError` carrying
           the specialist's own explanation verbatim.
        3. ``payload['run_identifier']`` truthy → :class:`AsyncPending`
           with that identifier.
        4. Otherwise → :class:`SyncResult` with Coach fields extracted
           top-level-first (see :func:`_extract_coach_fields`). When no
           Coach score is present anywhere, ``coach_score=None`` is
           returned so the gating layer's FLAG_FOR_REVIEW rule fires.

    Notes:
        * Order matters: a payload that both carries ``error`` *and*
          fails envelope validation produces a schema-validation error,
          not a specialist-error. The schema is the source of truth.
        * The parser never logs raw payload values. Only the resolved
          outcome ``kind`` is emitted at debug level for tracing.
    """

    # --- Step 1: envelope validation -------------------------------------
    try:
        SpecialistReplyEnvelope.model_validate(payload)
    except ValidationError as exc:
        outcome: DispatchOutcome = DispatchError(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            error_explanation=_summarise_validation_error(exc),
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 2: specialist-reported error ------------------------------
    raw_error = payload.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        outcome = DispatchError(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            error_explanation=raw_error,
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 3: async-mode initial reply -------------------------------
    raw_run_id = payload.get("run_identifier")
    if isinstance(raw_run_id, str) and raw_run_id.strip():
        outcome = AsyncPending(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            run_identifier=raw_run_id,
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 4: synchronous Coach result -------------------------------
    score, breakdown, findings = _extract_coach_fields(payload)
    try:
        outcome = SyncResult(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            coach_score=score,
            criterion_breakdown=breakdown,
            detection_findings=findings,
        )
    except ValidationError as exc:
        # An out-of-range coach_score, etc., is also a schema-validation
        # failure — surface it the same way as envelope errors so the
        # gating layer never sees a half-built SyncResult.
        outcome = DispatchError(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            error_explanation=_summarise_validation_error(exc),
        )

    logger.debug("parse_reply outcome kind=%s", outcome.kind)
    return outcome


__all__ = [
    "SpecialistReplyEnvelope",
    "parse_reply",
]
