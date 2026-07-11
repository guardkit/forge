"""Specialist reply parser for the Forge dispatch layer.

This module converts a specialist reply payload (the inner
:class:`nats_core.events.ResultPayload` ``dict`` — the transport adapter
has already validated and unwrapped the outer ``MessageEnvelope`` and read
source identity from ``MessageEnvelope.source_id``, D2) into a
:data:`~forge.dispatch.models.DispatchOutcome` discriminated-union member.

The DEPLOYED reply shape (DISPATCHFMT+ S4, verified in-container) is a
``ResultPayload`` — ``command`` / ``result`` / ``correlation_id`` /
``success`` — whose ``result`` block on success is the
``wrap_role_output`` dict: ``role_id``, ``coach_score``,
``criterion_breakdown`` (a **list** of
``{criterion, score, weight, rationale}``), ``detection_findings``, and the
REAL role document under ``role_output``. On failure the reply is
``result={"error": <message>}`` with ``success=False``.

Resolution order:

1. **Envelope validation FIRST.** If the payload fails to validate against
   :class:`SpecialistReplyEnvelope`, the parser produces a
   :class:`~forge.dispatch.models.DispatchError` carrying a
   ``schema_validation`` explanation. *Only field names and Pydantic
   error types* are surfaced — raw payload values are never logged or
   embedded, so sensitive parameters cannot leak via log scraping. The
   deployed reply carries no top-level ``agent_id`` (that identity lives on
   the envelope ``source_id``), so requiring one here is NOT done — that
   was the M6 blocker.

2. **Deployed failure reply** (``payload.success is False``) →
   :class:`~forge.dispatch.models.DispatchError` with the specialist's own
   ``result['error']`` explanation (M6).

3. **Specialist error result** (top-level ``payload.error``) →
   :class:`~forge.dispatch.models.DispatchError` with the specialist's
   own explanation copied verbatim (alternate/legacy shapes).

4. **Async-mode initial reply** (``payload.run_identifier``) →
   :class:`~forge.dispatch.models.AsyncPending` carrying that opaque
   identifier for later polling.

5. **Synchronous result** — :class:`~forge.dispatch.models.SyncResult`
   built from the Coach fields PLUS the real ``role_output`` document
   (M7 + M10). Extraction prefers top-level fields over the nested
   ``result`` block (see :func:`_extract_coach_fields` /
   :func:`_extract_role_output`); the nested block is the deployed home for
   all of it.

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
    """Boundary contract for a DEPLOYED specialist reply payload.

    The payload handed to :func:`parse_reply` is the inner
    :class:`nats_core.events.ResultPayload` ``dict`` — the transport
    adapter (:mod:`forge.adapters.nats.specialist_dispatch`) has already
    validated and unwrapped the outer ``MessageEnvelope``, reading source
    identity from ``MessageEnvelope.source_id`` and forwarding
    ``envelope.payload`` here (D2). The deployed ``ResultPayload`` surface
    is ``command`` / ``result`` / ``correlation_id`` / ``success`` (see the
    in-container ``nats_core.events._agent.ResultPayload`` — byte-identical
    0.4.0↔0.7.0).

    None of these are *required* to identify the reply: the responding
    specialist is already known from the envelope ``source_id`` at the
    adapter, and a ``role_id`` inside ``result`` names the role. Requiring a
    top-level ``agent_id`` here was the M6 blocker — the deployed reply
    carries none, so every real reply produced an instant
    ``agent_id(missing)`` DispatchError. All fields are therefore optional;
    branching semantics (``success`` / error / async / sync) are applied by
    :func:`parse_reply` against the raw ``dict`` so field-presence
    distinctions are preserved.

    ``result`` is typed as an optional ``dict`` so a structurally wrong
    payload (e.g. ``result`` arriving as a bare string) is still surfaced as
    a schema-validation :class:`DispatchError` rather than silently parsed.

    ``extra="allow"`` so the dispatch parser does not become a brittle
    chokepoint when specialists evolve their reply shapes.
    """

    model_config = ConfigDict(extra="allow")

    command: str | None = Field(
        default=None, description="The command the specialist executed"
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Result block — the ``wrap_role_output`` dict on success, "
        "or ``{'error': ...}`` on failure",
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation id echoed by the specialist"
    )
    success: bool | None = Field(
        default=None,
        description="Deployed success flag; ``False`` marks a failure reply",
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
) -> tuple[float | None, list[Any] | dict[str, Any], list[Any]]:
    """Return ``(coach_score, criterion_breakdown, detection_findings)``.

    Extraction rule (A.coach-output-top-vs-nested):

    * Top-level fields on ``payload`` are preferred.
    * The nested ``payload["result"]`` block is consulted as a fallback
      *only* when the corresponding top-level field is absent or empty.
      For the DEPLOYED reply this nested block is the ``wrap_role_output``
      dict — Coach evidence lives there, so the fallback is the normal path.

    ``coach_score`` is treated specially: ``0.0`` is a legitimate score,
    so we use a presence check (``is None``) rather than truthiness when
    deciding whether to fall back. ``criterion_breakdown`` and
    ``detection_findings`` use truthiness — an empty collection at the
    top level is treated as "no useful evidence" and falls back.

    ``criterion_breakdown`` is preserved as-is when it is a **list**
    (the deployed ``wrap_role_output`` shape: a list of
    ``{criterion, score, weight, rationale}`` records) OR a mapping (older
    reply shapes). Any other type degrades to an empty dict (M7).
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
    # Accept both the deployed list shape and the legacy dict shape (M7).
    breakdown_raw = (
        payload.get("criterion_breakdown")
        or nested.get("criterion_breakdown")
        or {}
    )
    breakdown: list[Any] | dict[str, Any] = (
        breakdown_raw if isinstance(breakdown_raw, (list, dict)) else {}
    )

    findings_raw = (
        payload.get("detection_findings")
        or nested.get("detection_findings")
        or []
    )
    findings: list[Any] = findings_raw if isinstance(findings_raw, list) else []

    return score, breakdown, findings


def _extract_role_output(
    payload: dict[str, Any],
) -> dict[str, Any] | list[Any]:
    """Return the role's actual product document (M10).

    The deployed ``wrap_role_output`` reply carries the REAL PO / architect
    document under ``result["role_output"]``. Extraction mirrors the Coach
    fields: prefer a top-level ``role_output`` (alternate shapes), fall back
    to the nested ``result`` block. A missing or non-collection value
    degrades to an empty dict so the checkpoint / handoff never crashes —
    it simply carries no document.
    """

    nested_raw = payload.get("result")
    nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else {}

    top = payload.get("role_output")
    raw = top if top else nested.get("role_output")
    if isinstance(raw, (dict, list)):
        return raw
    return {}


def _failure_explanation(payload: dict[str, Any]) -> str:
    """Extract a specialist failure explanation from a ``success=False`` reply.

    The deployed error path publishes ``result={"error": <message>}`` with
    ``success=False``. Prefer that nested ``error`` string; fall back to a
    top-level ``error`` (alternate shapes); default to a value-free sentinel
    so the ``DispatchError`` ``min_length=1`` constraint is always satisfied.
    """

    result_raw = payload.get("result")
    if isinstance(result_raw, dict):
        nested_error = result_raw.get("error")
        if isinstance(nested_error, str) and nested_error.strip():
            return nested_error

    top_error = payload.get("error")
    if isinstance(top_error, str) and top_error.strip():
        return top_error

    return "specialist reported failure without an error detail"


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
        2. ``payload['success'] is False`` → :class:`DispatchError` carrying
           the specialist's own explanation (``result['error']``, M6).
        3. ``payload['error']`` truthy → :class:`DispatchError` carrying
           the specialist's own explanation verbatim (alternate shapes).
        4. ``payload['run_identifier']`` truthy → :class:`AsyncPending`
           with that identifier.
        5. Otherwise → :class:`SyncResult` with Coach fields + the real
           ``role_output`` document extracted top-level-first (see
           :func:`_extract_coach_fields` / :func:`_extract_role_output`).
           When no Coach score is present anywhere, ``coach_score=None`` is
           returned so the gating layer's FLAG_FOR_REVIEW rule fires.

    Notes:
        * Order matters: a payload that both carries an error *and*
          fails envelope validation produces a schema-validation error,
          not a specialist-error. The schema is the source of truth.
        * ``success`` is the DEPLOYED failure signal — a ``False`` here is
          authoritative even when a partial ``result`` block is present.
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

    # --- Step 2: deployed failure reply (success=False) -----------------
    # The deployed router publishes ``ResultPayload(result={"error": ...},
    # success=False)`` for every failure mode (M6). ``success`` is the
    # authoritative signal — branch on it before the sync path so a
    # failure carrying only ``result['error']`` (no top-level ``error``)
    # is still surfaced as a DispatchError.
    if payload.get("success") is False:
        outcome = DispatchError(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            error_explanation=_failure_explanation(payload),
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 3: specialist-reported error (top-level, alternate shapes) -
    raw_error = payload.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        outcome = DispatchError(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            error_explanation=raw_error,
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 4: async-mode initial reply -------------------------------
    raw_run_id = payload.get("run_identifier")
    if isinstance(raw_run_id, str) and raw_run_id.strip():
        outcome = AsyncPending(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            run_identifier=raw_run_id,
        )
        logger.debug("parse_reply outcome kind=%s", outcome.kind)
        return outcome

    # --- Step 5: synchronous Coach result + real role_output document ----
    score, breakdown, findings = _extract_coach_fields(payload)
    role_output = _extract_role_output(payload)
    try:
        outcome = SyncResult(
            resolution_id=resolution_id,
            attempt_no=attempt_no,
            coach_score=score,
            criterion_breakdown=breakdown,
            detection_findings=findings,
            role_output=role_output,
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
