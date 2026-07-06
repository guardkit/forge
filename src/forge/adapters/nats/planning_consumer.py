"""Planning intake consumer for Mode P (TASK-MP-008).

The planning front door — a SEPARATE handler + durable consumer that never calls
dispatch_build/maybe_gate_build. Validates PlanningQueuedPayload, validates
correlation_id at the trust boundary (RT-03), records the run, and acks after
persist (ASSUM-015).

Key differences from pipeline_consumer:
- Ack-after-persist (not held-slot)
- Trust boundary validation on correlation_id
- No path allowlist (no feature_yaml_path)
- No dispatch to state machine (just store)
- Terminal duplicate → notification (RT-10)

References:
- TASK-MP-008 — this task brief
- TASK-MP-002 — SqlitePlanningRunStore
- RT-03 — correlation_id trust boundary validation
- RT-10 — terminal duplicate notification
- ASSUM-015 — ack-after-persist rationale
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

from nats_core.envelope import MessageEnvelope
from nats_core.events import PlanningQueuedPayload
from pydantic import ValidationError

from forge.planning.run_store import DuplicateRun, SqlitePlanningRunStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the planning intake contract
# ---------------------------------------------------------------------------

#: NATS subject filter for planning-queued messages.
#: The trailing * matches the correlation_id token.
PLANNING_QUEUED_SUBJECT_FILTER: str = "pipeline.planning-queued.*"

#: Durable consumer name for planning intake.
#: Survives Forge restart so unacked messages are redelivered.
PLANNING_DURABLE_NAME: str = "forge-serve-planning"

#: Correlation ID validation pattern (RT-03 trust boundary).
#: - Alphanumeric, underscore, hyphen, dot only
#: - No forward slash (path traversal)
#: - No consecutive dots (parent traversal)
#: - No NATS subject-breaking chars (~^:?*[)
#: - No whitespace
#: - 1-128 characters
CORRELATION_ID_PATTERN: str = r"^[A-Za-z0-9._-]{1,128}$"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

PublishNotification = Callable[[str, str], Awaitable[None]]
"""``async (correlation_id, message) -> None`` — publish notification for
terminal duplicate retry (RT-10)."""


@runtime_checkable
class _MsgLike(Protocol):
    """Minimal slice of nats.aio.msg.Msg we depend on."""

    data: bytes

    async def ack(self) -> None:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningConsumerDeps:
    """Injected collaborators for planning message processing.

    Attributes:
        store: SqlitePlanningRunStore for durable run persistence.
        publish_notification: Optional notification publisher for terminal
            duplicate retries (RT-10). If None, terminal duplicates are
            logged but no notification is sent.
    """

    store: SqlitePlanningRunStore
    publish_notification: PublishNotification | None = None


# ---------------------------------------------------------------------------
# Correlation ID validation
# ---------------------------------------------------------------------------


def _is_valid_correlation_id(correlation_id: str) -> bool:
    """Validate correlation_id against trust boundary rules (RT-03).

    Returns:
        True if valid, False otherwise.
    """
    if not correlation_id:
        return False

    if len(correlation_id) > 128:
        return False

    # Check pattern
    if not re.fullmatch(CORRELATION_ID_PATTERN, correlation_id):
        return False

    # Additional check: no consecutive dots (.. = parent traversal)
    if ".." in correlation_id:
        return False

    return True


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------


async def handle_planning_message(msg: _MsgLike, deps: PlanningConsumerDeps) -> None:
    """Validate one planning-queued message and persist to the store.

    Outcomes (mutually exclusive):

    1. *Malformed envelope or payload* → ack + log rejection.
       No store write, no wedge.
    2. *Invalid correlation_id* → ack + log rejection (RT-03).
       No store write, no wedge.
    3. *Duplicate non-terminal run* → ack + idempotent skip.
       No second row, no notification.
    4. *Duplicate terminal run* → ack + notification (RT-10).
       No second row, notification published to originator.
    5. *Accepted planning request* → store write + ack AFTER persist.
       Creates planning_runs row in QUEUED state.

    The function never raises on bad input — every failure is captured and
    translated into outcomes (1)–(4). It still propagates asyncio.CancelledError.

    Args:
        msg: NATS message with PlanningQueuedPayload envelope.
        deps: Injected dependencies (store, notification publisher).
    """

    # --- 1. Parse envelope + payload -------------------------------------
    try:
        envelope = MessageEnvelope.model_validate_json(msg.data)
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "planning_consumer: could not parse MessageEnvelope (%s); acking",
            exc,
        )
        await msg.ack()
        return

    try:
        payload = PlanningQueuedPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        logger.warning(
            "planning_consumer: PlanningQueuedPayload validation failed (%s); acking",
            exc,
        )
        await msg.ack()
        return

    # --- 2. Correlation ID validation (RT-03) ----------------------------
    correlation_id = payload.correlation_id
    if not _is_valid_correlation_id(correlation_id):
        logger.warning(
            "planning_consumer: invalid correlation_id=%r (fails trust boundary "
            "validation); acking and rejecting",
            correlation_id,
        )
        await msg.ack()
        return

    # --- 3. Tolerate missing originating_adapter (digest fact 6) ---------
    if payload.originating_adapter is None and payload.triggered_by == "jarvis":
        logger.info(
            "planning_consumer: originating_adapter missing for triggered_by=jarvis "
            "correlation_id=%s; accepting with log",
            correlation_id,
        )

    # --- 4. Record queued run (idempotent on correlation_id) -------------
    try:
        result = deps.store.record_queued(
            correlation_id=correlation_id,
            originating_user=payload.originating_user,
            expected_approver=payload.originating_user,  # RT-04: initialize to originator
            request_text=payload.request_text,
            triggered_by=payload.triggered_by,
            originating_adapter=payload.originating_adapter,
            parent_request_id=payload.parent_request_id,
            target_repo=payload.target_repo,
        )
    except Exception as exc:
        # Store write failed — log and ack to prevent wedge
        logger.error(
            "planning_consumer: store.record_queued raised (%s) for "
            "correlation_id=%s; acking to prevent wedge",
            exc,
            correlation_id,
        )
        await msg.ack()
        return

    # --- 5. Handle duplicate (terminal vs non-terminal) ------------------
    if isinstance(result, DuplicateRun):
        if result.is_terminal:
            # RT-10: terminal duplicate → notification
            logger.info(
                "planning_consumer: duplicate terminal run correlation_id=%s "
                "existing_state=%s; acking and sending notification",
                correlation_id,
                result.existing_state,
            )
            if deps.publish_notification is not None:
                try:
                    await deps.publish_notification(
                        correlation_id,
                        f"Planning request {correlation_id} already completed "
                        f"with state {result.existing_state}",
                    )
                except Exception as notify_exc:
                    # Best-effort notification; don't block ack
                    logger.warning(
                        "planning_consumer: publish_notification raised (%s) for "
                        "correlation_id=%s; continuing with ack",
                        notify_exc,
                        correlation_id,
                    )
        else:
            # Non-terminal duplicate → idempotent skip
            logger.info(
                "planning_consumer: duplicate non-terminal run correlation_id=%s "
                "existing_state=%s; acking and skipping",
                correlation_id,
                result.existing_state,
            )

        await msg.ack()
        return

    # --- 6. Success: ack AFTER persist (ASSUM-015) -----------------------
    logger.info(
        "planning_consumer: recorded planning run correlation_id=%s "
        "originating_user=%s triggered_by=%s; acking",
        correlation_id,
        payload.originating_user,
        payload.triggered_by,
    )
    await msg.ack()


__all__ = [
    "CORRELATION_ID_PATTERN",
    "PLANNING_DURABLE_NAME",
    "PLANNING_QUEUED_SUBJECT_FILTER",
    "PlanningConsumerDeps",
    "handle_planning_message",
    "_is_valid_correlation_id",  # Exported for testing
]
