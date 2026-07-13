"""Synthetic ``ApprovalResponsePayload`` injector for forge cancel/skip.

Implements the CLI steering paths for a paused build per
``API-nats-approval-protocol.md §7`` (timeout-and-steering table) and
ASSUM-005 (high). When Rich runs ``forge cancel FEAT-XXX`` or
``forge skip FEAT-XXX`` against a paused build the system needs to
resume the LangGraph ``interrupt()`` waiting on the approval response
mirror subject. Rather than open a parallel resume code path that
silently bypasses the dedup gate (closes risk **F6**), this module
**publishes a typed synthetic** :class:`~nats_core.events.ApprovalResponsePayload`
onto the **same** ``agents.approval.forge.{build_id}.response`` subject
that real Rich responses traverse so the standard subscriber
(TASK-CGCP-007) consumes it through its **first-response-wins**
idempotency gate.

Mappings (ASSUM-005 high; API §7 table):

==========================  ========================================================
CLI command                 Synthetic ``ApprovalResponsePayload``
==========================  ========================================================
``forge cancel FEAT-XXX``   ``decision="reject"``,
                            ``decided_by="rich"``, ``notes="cli cancel"``
``forge skip FEAT-XXX``     ``decision="override"``,
                            ``decided_by="rich"``, ``notes="cli skip"``
==========================  ========================================================

The ``ApprovalResponsePayload`` schema (``nats_core.events._agent``)
declares the responder identity as ``decided_by`` and the human-readable
explanation as ``notes``. The design contract in
``API-nats-approval-protocol.md §4.1`` refers to these same fields by the
ergonomic names ``responder`` and ``reason`` — this module bridges the
two name spaces by populating ``decided_by`` from the design ``responder``
slot and ``notes`` from the design ``reason`` slot. A subscriber that
reads the payload via the ``nats_core`` model will see ``decided_by``
and ``notes``; a subscriber that reads the persisted record via the
gating-layer mirror sees the design-level ``responder``/``reason`` keys.

Idempotency contract
--------------------

Per ``API §6``, responders deduplicate on ``request_id`` with
first-response-wins semantics. The synthetic injector keys on the
**same** deterministic ``request_id`` derived by
:func:`forge.gating.identity.derive_request_id` — guaranteeing that:

* a synthetic CLI cancel/skip arriving **before** any real Rich response
  resumes the build with reason ``"cli cancel"`` / ``"cli skip"``, and
* a synthetic CLI cancel/skip arriving **after** a real Rich response
  has already resumed the build is observed by the dedup buffer and
  silently discarded (no double-resume).

The ``attempt_count`` passed to :meth:`SyntheticResponseInjector.inject_cli_cancel`
and :meth:`SyntheticResponseInjector.inject_cli_skip` MUST be the value
**persisted in SQLite** at first emission for the paused stage, not a
freshly-derived live value. This prevents drift if the timeout-refresh
loop (TASK-CGCP-007) has advanced the persisted ``attempt_count`` since
the inject call was issued.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.gating.identity import derive_request_id

if TYPE_CHECKING:  # pragma: no cover — import-time only
    from nats.aio.client import Client as NATSClient

logger = logging.getLogger(__name__)

#: Identity stamped onto every envelope this injector emits. Matches the
#: pipeline publisher (TASK-NFI-006) so consumers can route by
#: ``source_id`` without distinguishing publisher modules.
SOURCE_ID: Final[str] = "forge"

#: Subject prefix mirroring ``API-nats-approval-protocol.md §2``. The
#: per-build ``.response`` mirror subject is built by appending
#: ``.{build_id}.response`` to this prefix.
APPROVAL_SUBJECT_PREFIX: Final[str] = "agents.approval.forge"

#: Sentinel for the design ``responder`` field (mapped to ``decided_by``
#: on the wire-level :class:`ApprovalResponsePayload`). All synthetic
#: responses are issued on Rich's behalf — the CLI always speaks as
#: Rich since Rich is the human triggering the cancel/skip.
SYNTHETIC_RESPONDER: Final[str] = "rich"

#: Sentinel ``reason`` (``notes``) value for ``forge cancel``. Used by
#: the persisted ``GateDecision`` mirror to distinguish a CLI cancel from
#: a real Rich response that happens to carry ``decision="reject"``.
REASON_CLI_CANCEL: Final[str] = "cli cancel"

#: Sentinel ``reason`` (``notes``) value for ``forge skip``. Used the
#: same way as :data:`REASON_CLI_CANCEL` but for the override path.
REASON_CLI_SKIP: Final[str] = "cli skip"

__all__ = [
    "APPROVAL_SUBJECT_PREFIX",
    "REASON_CLI_CANCEL",
    "REASON_CLI_SKIP",
    "SOURCE_ID",
    "SYNTHETIC_RESPONDER",
    "SyntheticInjectFailure",
    "SyntheticResponseInjector",
]


class SyntheticInjectFailure(RuntimeError):
    """Raised when the underlying NATS publish for a synthetic response fails.

    Mirrors the shape of
    :class:`forge.adapters.nats.pipeline_publisher.PublishFailure` —
    callers (the CLI command in TASK-CGCP-010) catch + log the failure
    but **must not** roll back any SQLite state, since pipeline truth
    lives in SQLite and the NATS stream is a derived projection.

    Attributes:
        subject: The NATS subject the injector attempted to write to.
        cause: The underlying exception raised by the NATS client.
    """

    def __init__(self, subject: str, cause: BaseException) -> None:
        super().__init__(
            f"Failed to inject synthetic approval response on {subject!r}: {cause}",
        )
        self.subject = subject
        self.cause = cause


class SyntheticResponseInjector:
    """Publishes synthetic :class:`ApprovalResponsePayload` envelopes for CLI steering.

    The class is intentionally thin — it owns no scheduling, no SQLite
    reads, and no retry logic. The caller (TASK-CGCP-010 CLI wiring)
    is responsible for fetching the persisted ``attempt_count`` from
    SQLite and passing it to :meth:`inject_cli_cancel` /
    :meth:`inject_cli_skip`. This separation keeps the injector
    domain-pure (no SQLite import) and trivially unit-testable.

    Args:
        nats_client: An async NATS client (typically
            ``nats.aio.client.Client``) with an awaitable ``publish``
            method. Injected at the application boundary so unit tests
            can substitute a mock without monkey-patching.
    """

    def __init__(self, nats_client: NATSClient | Any) -> None:
        self._nc = nats_client

    # ------------------------------------------------------------------
    # Subject helper
    # ------------------------------------------------------------------

    @staticmethod
    def _subject_for(build_id: str) -> str:
        """Build the ``agents.approval.forge.{build_id}.response`` subject.

        Args:
            build_id: Identifier of the paused build whose approval
                response mirror subject is being addressed. Must be
                non-empty.

        Returns:
            The canonical mirror subject for ``build_id``.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError("build_id must be a non-empty string")
        return f"{APPROVAL_SUBJECT_PREFIX}.{build_id}.response"

    # ------------------------------------------------------------------
    # Public injection API
    # ------------------------------------------------------------------

    async def inject_cli_cancel(
        self,
        *,
        build_id: str,
        stage_label: str,
        attempt_count: int,
        correlation_id: str | None = None,
        responder: str | None = None,
    ) -> None:
        """Publish a synthetic ``decision="reject"`` response for ``forge cancel``.

        Per ASSUM-005 (high) and ``API §7``: a CLI cancel maps to a
        rejection so the state machine transitions the build to
        ``CANCELLED``. The persisted ``GateDecision`` response record
        carries the resolved ``responder`` (``decided_by``) AND
        ``reason="cli cancel"`` (``notes``) so the CLI origin is
        distinguishable from a real Rich rejection.

        Args:
            build_id: Identifier of the paused build. Used both for the
                NATS subject and as an input to the deterministic
                ``request_id`` derivation.
            stage_label: Pipeline stage the build is paused at. Combined
                with ``build_id`` and ``attempt_count`` it yields a
                stable ``request_id`` that any concurrent real Rich
                response shares.
            attempt_count: The ``attempt_count`` **persisted in SQLite**
                for the paused stage at first emission. Passing the
                persisted value (not a freshly-incremented one)
                guarantees the synthetic response keys on the same
                ``request_id`` as the real outstanding request — so the
                dedup buffer can recognise duplicates.
            correlation_id: Optional pipeline-level correlation id from
                ``BuildQueuedPayload``. Threaded onto the
                :class:`MessageEnvelope` for trace stitching; ``None``
                when no correlation is available.
            responder: Identity stamped onto the reject's ``decided_by``
                (O-02 / TASK-FWD-005 break #3). MUST equal the gate's
                ``expected_approver`` or the identity-pinned approval
                subscriber refuses the reject and the build stays PAUSED.
                ``None`` keeps the legacy :data:`SYNTHETIC_RESPONDER`
                default for callers that do not resolve an identity.

        Raises:
            ValueError: If ``build_id`` or ``stage_label`` is empty, or
                ``attempt_count`` is negative — all three propagated
                from :func:`derive_request_id`.
            SyntheticInjectFailure: If the underlying NATS publish raises.
        """
        await self._publish_synthetic(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
            decision="reject",
            reason=REASON_CLI_CANCEL,
            correlation_id=correlation_id,
            responder=responder,
        )

    async def inject_cli_skip(
        self,
        *,
        build_id: str,
        stage_label: str,
        attempt_count: int,
        correlation_id: str | None = None,
        responder: str | None = None,
    ) -> None:
        """Publish a synthetic ``decision="override"`` response for ``forge skip``.

        Per ASSUM-005 (high) and ``API §7``: a CLI skip maps to an
        override of the **current stage only**. The state machine
        records the override and the build continues to the next stage
        without re-running the skipped one. The persisted record
        carries ``responder="rich"`` (``decided_by``) AND
        ``reason="cli skip"`` (``notes``) so the CLI origin is
        distinguishable from a real Rich override.

        Args:
            build_id: Identifier of the paused build.
            stage_label: Pipeline stage being overridden.
            attempt_count: The ``attempt_count`` persisted in SQLite for
                the paused stage; see :meth:`inject_cli_cancel` for why
                this MUST be the persisted value rather than re-derived.
            correlation_id: Optional pipeline-level correlation id.
            responder: Identity stamped onto ``decided_by`` (see
                :meth:`inject_cli_cancel`). ``None`` keeps the legacy
                :data:`SYNTHETIC_RESPONDER` default.

        Raises:
            ValueError: If ``build_id`` or ``stage_label`` is empty, or
                ``attempt_count`` is negative.
            SyntheticInjectFailure: If the underlying NATS publish raises.
        """
        await self._publish_synthetic(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
            decision="override",
            reason=REASON_CLI_SKIP,
            correlation_id=correlation_id,
            responder=responder,
        )

    # ------------------------------------------------------------------
    # Internal: build envelope + publish
    # ------------------------------------------------------------------

    async def _publish_synthetic(
        self,
        *,
        build_id: str,
        stage_label: str,
        attempt_count: int,
        decision: str,
        reason: str,
        correlation_id: str | None,
        responder: str | None = None,
    ) -> None:
        """Construct and publish the synthetic :class:`ApprovalResponsePayload`.

        Single shared code path for cancel and skip — the only
        differences between the two are the ``decision`` Literal, the
        ``reason`` sentinel, and the ``responder`` identity. Centralising
        the envelope wrapping here guarantees the two CLI steering paths
        emit envelopes that are byte-for-byte structurally identical apart
        from those fields. ``responder`` defaults to
        :data:`SYNTHETIC_RESPONDER` when the caller passes ``None``.
        """
        decided_by = responder if responder and responder.strip() else SYNTHETIC_RESPONDER
        # request_id must be **deterministic** over (build_id,
        # stage_label, attempt_count) so it matches the value the
        # publisher emitted on first send. derive_request_id is pure;
        # passing the persisted attempt_count yields the persisted
        # request_id without an extra SQLite lookup at this layer.
        request_id = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
        )

        # The ``ApprovalResponsePayload`` schema in ``nats_core`` uses
        # ``decided_by`` for the responder identity and ``notes`` for the
        # reason. See module docstring for the ``responder``/``reason``
        # design-name mapping.
        payload = ApprovalResponsePayload(
            request_id=request_id,
            decision=decision,  # type: ignore[arg-type]  # validated by Pydantic Literal
            decided_by=decided_by,
            notes=reason,
        )

        subject = self._subject_for(build_id)

        envelope = MessageEnvelope(
            source_id=SOURCE_ID,
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        body = envelope.model_dump_json().encode("utf-8")

        try:
            ack = await self._nc.publish(subject, body)
        except Exception as exc:  # noqa: BLE001 — re-raised as typed SyntheticInjectFailure
            # Log first so operators see the underlying error even if a
            # caller swallows SyntheticInjectFailure further up the stack.
            logger.warning(
                "synthetic approval inject failed subject=%s decision=%s reason=%s "
                "request_id=%s error=%s",
                subject,
                decision,
                reason,
                request_id,
                exc,
            )
            raise SyntheticInjectFailure(subject, exc) from exc

        # PubAck is informational only — JetStream may or may not return
        # one depending on stream configuration. Mirror the
        # fire-and-forget semantics of ``pipeline_publisher`` (LES1 parity
        # rule): never treat the ack as proof of delivery.
        if ack is not None:
            logger.debug(
                "synthetic approval inject ack subject=%s ack=%r "
                "(informational only)",
                subject,
                ack,
            )
        else:
            logger.debug(
                "synthetic approval inject ok subject=%s decision=%s "
                "request_id=%s",
                subject,
                decision,
                request_id,
            )
