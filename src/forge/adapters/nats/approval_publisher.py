"""Outbound publisher for Forge approval-request envelopes (TASK-CGCP-006).

Owns the wire-side of the protocol described in
``docs/design/contracts/API-nats-approval-protocol.md`` §2–§3:

- Resolves the canonical subject ``agents.approval.forge.{build_id}`` from
  :data:`nats_core.topics.Topics.Agents.APPROVAL_REQUEST`, optionally
  scoped via :meth:`Topics.for_project` for multi-tenant deployments.
- Serialises a :class:`nats_core.envelope.MessageEnvelope` to bytes and
  hands it to the underlying async NATS client.
- Wraps any transport-level error in :class:`ApprovalPublishError` so
  callers can catch a single typed exception — failure does NOT swallow
  silently and does NOT cause the publisher to mutate any caller-side
  state (the SQLite mirror in TASK-CGCP-010 is recorded *before* the
  publish call; failure here surfaces but never rolls that mirror back).

This module is the **single source of truth** for the eleven-key
``details`` dict shape per §3.2 (AC-008): no other module in
``forge.gating`` or ``forge.adapters.nats.approval_*`` should construct
that dict directly. Callers must invoke :func:`_build_approval_details`
to obtain a structurally-conformant value.

Design parity:

- The transport pattern mirrors ``forge.adapters.nats.pipeline_publisher``
  (TASK-NFI-006): build envelope → ``model_dump_json`` → ``self._nc.publish``.
- The exception shape mirrors :class:`PublishFailure` from the same
  module so downstream log scrapers parse one consistent format.
- The risk-level table is taken **verbatim** from
  ``API-nats-approval-protocol §3.3`` and exported as :func:`_derive_risk_level`
  for direct unit-test coverage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from nats_core.topics import Topics

from forge.gating.models import GateDecision, GateMode

if TYPE_CHECKING:  # pragma: no cover — import-time only
    from nats_core.envelope import MessageEnvelope

    from forge.lifecycle.persistence import BuildRow

logger = logging.getLogger(__name__)

#: Identity stamped onto every envelope this publisher emits. Mirrors
#: :data:`forge.adapters.nats.fleet_publisher.AGENT_ID` so tests can
#: assert against a single source of truth.
AGENT_ID: str = "forge"

#: Subject template lifted from ``nats_core.topics`` so a future rename in
#: nats-core surfaces here as a test failure rather than a silent drift.
APPROVAL_SUBJECT_TEMPLATE: str = Topics.Agents.APPROVAL_REQUEST

#: Risk level alias — kept aligned with
#: :class:`nats_core.events.ApprovalRequestPayload.risk_level`.
RiskLevel = Literal["low", "medium", "high"]

# Threshold that splits ``low`` from ``medium`` for FLAG_FOR_REVIEW
# (API-nats-approval-protocol §3.3). Pulled out as a constant so the
# unit tests assert against the same number the helper consults.
_FLAG_FOR_REVIEW_LOW_RISK_FLOOR: float = 0.65


__all__ = [
    "AGENT_ID",
    "APPROVAL_SUBJECT_TEMPLATE",
    "ApprovalPublishError",
    "ApprovalPublisher",
    "RiskLevel",
    "_build_approval_details",
    "_derive_risk_level",
    "build_recovery_approval_envelope",
]


# Recovery-flavoured constants. Pulled out so unit tests assert against
# the same source of truth the helper consults.
_RECOVERY_STAGE_LABEL: str = "recovery"
_RECOVERY_GATE_MODE: str = "MANDATORY_HUMAN_APPROVAL"
_RECOVERY_RISK_LEVEL: RiskLevel = "medium"
_RECOVERY_RATIONALE: str = (
    "Boot-time recovery: re-issuing approval request after "
    "agent-runtime restart. Original request_id preserved."
)
_RECOVERY_RESUME_OPTIONS: tuple[str, ...] = (
    "approve",
    "reject",
    "defer",
    "override",
)


# ---------------------------------------------------------------------------
# Typed failure
# ---------------------------------------------------------------------------


class ApprovalPublishError(RuntimeError):
    """Raised when a transport-level approval publish fails.

    Mirrors the shape of
    :class:`forge.adapters.nats.pipeline_publisher.PublishFailure` so a
    multi-publisher log scraper sees one consistent error format. The
    originating exception is preserved both as ``__cause__`` (via the
    ``raise ... from exc`` chain) and as :attr:`cause` for callers that
    prefer attribute access.

    Attributes:
        subject: The NATS subject the publisher attempted to write to.
        cause: The underlying exception raised by the NATS client.
    """

    def __init__(self, subject: str, cause: BaseException) -> None:
        super().__init__(f"Failed to publish approval request to {subject!r}: {cause}")
        self.subject = subject
        self.cause = cause


# ---------------------------------------------------------------------------
# Pure helpers — single source of truth for §3.2 / §3.3
# ---------------------------------------------------------------------------


def _build_approval_details(
    decision: GateDecision,
    *,
    feature_id: str,
    artefact_paths: list[str],
    resume_options: list[str],
) -> dict[str, Any]:
    """Build the eleven-key ``details`` dict per ``API-nats-approval-protocol §3.2``.

    The keys are emitted in the order documented in the protocol so the
    resulting JSON is stable across runs (helpful for log diff-ing and
    cache key derivation downstream).

    The ``decision``-derived fields are read directly from the
    :class:`forge.gating.models.GateDecision` instance; the three
    decision-external fields (``feature_id``, ``artefact_paths``,
    ``resume_options``) are required keyword-only so callers cannot
    accidentally drop them.

    Args:
        decision: The gate decision being escalated to a human reviewer.
            Mode-specific fields (``mode``, ``coach_score``,
            ``criterion_breakdown``, ``detection_findings``, ``rationale``,
            ``evidence``) are projected into the details dict.
        feature_id: The ``FEAT-XXXX`` identifier of the build's feature.
            Threaded so notification adapters can render the feature label
            without an extra lookup.
        artefact_paths: Filesystem paths the reviewer should inspect.
            Copied defensively so caller mutations after publish do not
            alter the wire payload.
        resume_options: The exact set of decisions the reviewer may
            choose. Documented set is
            ``["approve", "reject", "defer", "override"]`` but the helper
            does not enforce membership — callers may narrow the set
            (e.g. for HARD_STOP, where only ``override`` is valid).

    Returns:
        A fresh JSON-serialisable ``dict[str, Any]`` containing exactly
        the eleven documented keys.
    """
    return {
        "build_id": decision.build_id,
        "feature_id": feature_id,
        "stage_label": decision.stage_label,
        "gate_mode": decision.mode.value,
        "coach_score": decision.coach_score,
        # Defensive copy — see test_criterion_breakdown_is_a_plain_dict_copy.
        "criterion_breakdown": dict(decision.criterion_breakdown),
        # ``exclude_none=True`` keeps the wire shape aligned with the
        # documented example in §3.2 (which omits the optional ``criterion``
        # link when it is unset). ``mode="json"`` ensures every nested
        # field is a primitive that ``json.dumps`` handles natively.
        "detection_findings": [
            finding.model_dump(mode="json", exclude_none=True)
            for finding in decision.detection_findings
        ],
        "rationale": decision.rationale,
        # Project priors down to the two fields §3.2 documents — entity_id
        # for traceability and summary for human display. Other fields
        # (group_id, relevance_score) are intentionally stripped to keep
        # the payload small and the renderer surface stable.
        "evidence_priors": [
            {"entity_id": prior.entity_id, "summary": prior.summary}
            for prior in decision.evidence
        ],
        "artefact_paths": list(artefact_paths),
        "resume_options": list(resume_options),
    }


def _derive_risk_level(decision: GateDecision) -> RiskLevel:
    """Map a :class:`GateDecision` to a risk level per ``API §3.3``.

    The table:

    +--------------------------------+--------------------------------------+
    | ``decision.mode``              | ``risk_level``                       |
    +================================+======================================+
    | ``FLAG_FOR_REVIEW``            | ``"low"`` if ``coach_score ≥ 0.65``, |
    |                                | else ``"medium"``                    |
    +--------------------------------+--------------------------------------+
    | ``HARD_STOP``                  | ``"high"``                           |
    +--------------------------------+--------------------------------------+
    | ``MANDATORY_HUMAN_APPROVAL``   | ``"medium"``                         |
    +--------------------------------+--------------------------------------+

    ``AUTO_APPROVE`` is intentionally **not** in the table: the publisher
    is never invoked for that mode (no pause). Calling this helper with
    an :class:`AUTO_APPROVE` decision is a programming error and raises
    :class:`ValueError` to fail loud rather than silently coerce.

    Args:
        decision: The gate decision whose mode and (for ``FLAG_FOR_REVIEW``)
            ``coach_score`` drive the risk classification.

    Returns:
        One of ``"low" | "medium" | "high"``.

    Raises:
        ValueError: If ``decision.mode`` is :class:`GateMode.AUTO_APPROVE`,
            which the protocol does not define a risk level for.
    """
    mode = decision.mode

    if mode is GateMode.HARD_STOP:
        return "high"
    if mode is GateMode.MANDATORY_HUMAN_APPROVAL:
        return "medium"
    if mode is GateMode.FLAG_FOR_REVIEW:
        score = decision.coach_score
        if score is not None and score >= _FLAG_FOR_REVIEW_LOW_RISK_FLOOR:
            return "low"
        return "medium"

    # AUTO_APPROVE never reaches the publisher; refuse to fabricate a
    # risk level the protocol table does not specify.
    msg = (
        "AUTO_APPROVE has no documented risk_level mapping in "
        "API-nats-approval-protocol §3.3 — the publisher must not be "
        "invoked for this mode."
    )
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Recovery-flavoured envelope helper (AC-008 single-ownership)
# ---------------------------------------------------------------------------


def _build_recovery_approval_details(build: BuildRow) -> dict[str, Any]:
    """Build the recovery-flavoured ``details`` dict for a PAUSED build.

    Matches the eleven canonical keys produced by
    :func:`_build_approval_details` (per ``API-nats-approval-protocol §3.2``)
    and adds a twelfth ``"recovery": True`` marker. Boot-time recovery
    has no live :class:`forge.gating.models.GateDecision` — only the
    durable :class:`BuildRow` survives a process restart — so the
    decision-derived fields are emitted as their empty/null defaults.

    AC-008: this is the only place outside :func:`_build_approval_details`
    that constructs the canonical eleven-key shape; callers in
    :mod:`forge.lifecycle.recovery` delegate here rather than open-code
    the dict.

    Args:
        build: The PAUSED build whose approval request is being
            re-issued. Only ``build_id`` and ``feature_id`` are read.

    Returns:
        A fresh JSON-serialisable ``dict[str, Any]`` carrying the eleven
        canonical keys plus ``recovery: True``.
    """
    return {
        "build_id": build.build_id,
        "feature_id": build.feature_id,
        "stage_label": _RECOVERY_STAGE_LABEL,
        "gate_mode": _RECOVERY_GATE_MODE,
        "coach_score": None,
        "criterion_breakdown": {},
        "detection_findings": [],
        "rationale": _RECOVERY_RATIONALE,
        "evidence_priors": [],
        "artefact_paths": [],
        "resume_options": list(_RECOVERY_RESUME_OPTIONS),
        "recovery": True,
    }


def build_recovery_approval_envelope(build: BuildRow) -> MessageEnvelope:
    """Build a recovery-flavoured approval envelope (sc_004 verbatim ``request_id``).

    The recovery envelope re-issues the original
    ``builds.pending_approval_request_id`` verbatim and carries a
    stripped details dict with ``recovery=True``. The full details
    shape is constructed by this module so AC-008 single-ownership of
    the eleven-key dict is preserved.

    The original :class:`forge.gating.models.GateDecision` is not
    durable across process restarts, so the recovery payload carries
    only what survives in SQLite plus the ``recovery`` marker.
    Notification adapters that previously rendered a rich approval
    card will see a stripped card on the re-issue; the responder,
    however, only correlates by ``request_id`` and is unaffected.

    Args:
        build: The PAUSED build whose approval request is being
            re-issued. ``build.pending_approval_request_id`` MUST be
            non-empty — :func:`forge.lifecycle.recovery._handle_paused`
            checks this before calling.

    Returns:
        :class:`nats_core.envelope.MessageEnvelope` ready to be passed
        to :meth:`ApprovalPublisher.publish_request`.

    Raises:
        ValueError: If ``build.pending_approval_request_id`` is missing
            — surfaces a corrupt PAUSED row rather than fabricating a
            request_id.
    """
    # Late import — the lifecycle module re-exports this helper without
    # eagerly importing ``nats_core.envelope`` / ``nats_core.events``,
    # so a missing optional dependency surfaces with a clear traceback
    # at call time rather than at module-import time.
    from nats_core.envelope import EventType, MessageEnvelope
    from nats_core.events import ApprovalRequestPayload

    request_id = build.pending_approval_request_id
    if not request_id:
        msg = (
            f"build_recovery_approval_envelope: build {build.build_id!r} "
            "has no pending_approval_request_id; cannot re-issue"
        )
        raise ValueError(msg)

    details = _build_recovery_approval_details(build)
    payload = ApprovalRequestPayload(
        request_id=request_id,
        agent_id=AGENT_ID,
        action_description=(
            f"Recovery: re-issuing pause for build {build.build_id} "
            f"(feature={build.feature_id})"
        ),
        risk_level=_RECOVERY_RISK_LEVEL,
        details=details,
    )

    return MessageEnvelope(
        source_id=AGENT_ID,
        event_type=EventType.APPROVAL_REQUEST,
        # TASK-GATE-D659 correlation landmine: the live pause path stamps
        # ``deps.correlation_id`` on its request envelope
        # (``_build_request_envelope``), but this recovery/rearm builder
        # previously left it unset — so the subscriber's step-2b guard
        # (refuses only when BOTH sides are non-None and mismatched) saw a
        # null on the re-emit and a real value on the responder echo. Stamp
        # the build's ``correlation_id`` verbatim so boot re-emits thread
        # the same correlation context as the original pause.
        correlation_id=build.correlation_id,
        payload=payload.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class ApprovalPublisher:
    """Publishes approval-request envelopes to ``agents.approval.forge.{build_id}``.

    The class is intentionally thin — it owns no scheduling, retry, or
    state-mutation logic. Its single responsibility is to resolve the
    subject, serialise the envelope, and hand the bytes to the NATS
    client. Callers (TASK-CGCP-010 wrapper) decide *when* to publish and
    own the SQLite mirror that records the underlying decision.

    Args:
        nats_client: An async NATS client (typically
            ``nats.aio.client.Client``) with an awaitable ``publish``
            method accepting ``(subject, body_bytes)``. Injected at the
            application boundary so unit tests can substitute a mock.
        project: Optional project scope. When set, the resolved subject
            is prefixed via :meth:`Topics.for_project` to namespace the
            stream for multi-tenant deployments.
    """

    def __init__(
        self,
        nats_client: Any,
        *,
        project: str | None = None,
    ) -> None:
        self._nc = nats_client
        self._project = project

    # ------------------------------------------------------------------
    # Subject resolver
    # ------------------------------------------------------------------

    def _subject_for(self, build_id: str) -> str:
        """Resolve the canonical subject for ``build_id``.

        The resolved string substitutes ``{agent_id}=forge`` and
        ``{task_id}=build_id`` per §2 of the protocol. When the publisher
        was constructed with a ``project`` argument, the subject is
        further wrapped via :meth:`Topics.for_project`.

        Args:
            build_id: The build identifier — also the ``request_id`` on
                the wrapped :class:`ApprovalRequestPayload`.

        Returns:
            The fully-resolved (and optionally project-scoped) subject.
        """
        subject = Topics.resolve(
            APPROVAL_SUBJECT_TEMPLATE,
            agent_id=AGENT_ID,
            task_id=build_id,
        )
        if self._project is not None:
            subject = Topics.for_project(self._project, subject)
        return subject

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish_request(self, envelope: MessageEnvelope) -> None:
        """Publish ``envelope`` to ``agents.approval.forge.{build_id}``.

        The ``build_id`` is read from
        ``envelope.payload["details"]["build_id"]`` — the eleven-key dict
        produced by :func:`_build_approval_details` puts it there. Reading
        it from ``details`` (rather than ``payload["request_id"]``) keeps
        the publisher honest about its dependency on the canonical dict
        shape: a malformed envelope fails loud here, before any wire
        write, rather than producing a subtly wrong subject downstream.

        Args:
            envelope: A wrapped :class:`MessageEnvelope` whose
                ``payload`` is a dumped
                :class:`nats_core.events.ApprovalRequestPayload` carrying
                a ``details`` dict with the documented eleven keys.

        Raises:
            ValueError: If ``envelope.payload['details']['build_id']`` is
                missing or empty — the publisher refuses to publish to a
                malformed subject.
            ApprovalPublishError: If the underlying NATS publish raises
                for any reason. The original exception is preserved as
                ``__cause__`` and as :attr:`ApprovalPublishError.cause`.
        """
        # --- Resolve build_id from the canonical details dict ---------
        payload = envelope.payload
        details = payload.get("details") if isinstance(payload, dict) else None
        if not isinstance(details, dict):
            msg = (
                "envelope.payload['details'] must be a dict produced by "
                "_build_approval_details; got "
                f"{type(details).__name__!r}"
            )
            raise ValueError(msg)

        build_id = details.get("build_id")
        if not isinstance(build_id, str) or not build_id:
            msg = (
                "envelope.payload['details']['build_id'] is required to "
                "resolve the approval subject; got "
                f"{build_id!r}"
            )
            raise ValueError(msg)

        subject = self._subject_for(build_id)

        # --- Serialise + write ----------------------------------------
        body = envelope.model_dump_json().encode("utf-8")

        try:
            await self._nc.publish(subject, body)
        except Exception as exc:  # noqa: BLE001 — we re-raise as ApprovalPublishError
            # Log first so operators see the underlying error even if a
            # caller swallows ApprovalPublishError further up the stack.
            # AC-006: the log + raise here is the *only* side effect on
            # failure; the publisher never reaches into caller state to
            # roll back the GateDecision SQLite mirror.
            logger.warning(
                "approval publish failed subject=%s error=%s",
                subject,
                exc,
            )
            raise ApprovalPublishError(subject, exc) from exc

        logger.debug("approval publish ok subject=%s", subject)
