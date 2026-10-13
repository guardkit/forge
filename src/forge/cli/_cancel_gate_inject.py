"""CLI-cancel synthetic-reject injector for gate-paused builds (TASK-GATE-D659 §D6).

Extracted from :mod:`forge.cli.cancel` so the ``forge cancel`` wrapper stays under
its thin-wrapper ceiling (TASK-PSM-011 AC-007). A build **paused at the daemon-
side approval gate** is cancelled by publishing a synthetic ``decision="reject"``
onto its ``agents.approval.forge.{build_id}.response`` subject, so the cancel
flows through the SAME live ``gate_check`` frame the daemon is awaiting. The
daemon's reject branch then owns the CANCELLED transition **and** the single
``build-cancelled`` emit — the CLI does NOT run ``handle_cancel`` on this path,
avoiding the double-emit the ``SqliteRowCancelledNotifier`` docstring warned
about. If the daemon is down (no live frame / no subscriber), the synthetic
reject is dropped on core NATS and the build stays PAUSED until the next restart
re-arms it (accepted limitation — mirrors the phone flow).
"""

from __future__ import annotations

import logging
import os
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

__all__ = [
    "PausedCardChanged",
    "PendingGateRequest",
    "pending_gate_request",
    "send_paused_cancel",
]


class PausedCardChanged(Exception):
    """The build is no longer waiting at the card this cancel would answer."""


class PendingGateRequest(NamedTuple):
    """The card a paused build is waiting at, read back from its row."""

    stage_label: str
    attempt_count: int
    correlation_id: str | None


def pending_gate_request(runtime: Any, *, build_id: str) -> PendingGateRequest | None:
    """Re-read the build row and return the exact card it is waiting at.

    Raises :class:`PausedCardChanged` when the row has left PAUSED or its
    ``pending_approval_request_id`` names another build: a rejection sent
    then would answer a card this cancel was not asked about. Returns
    ``None`` — the caller keeps today's direct-cancel fallback — when there
    is no row, or a PAUSED row carries no pending request id or a legacy one
    that does not parse.
    """
    from forge.gating.identity import parse_request_id
    from forge.lifecycle.state_machine import BuildState

    row = runtime.persistence.get_build_row(build_id)
    if row is None:
        return None
    if row.status is not BuildState.PAUSED:
        raise PausedCardChanged(
            f"the build is no longer waiting at its card (it is now "
            f"{row.status.value})."
        )
    # No pending id: mark_paused refuses an empty one and recovery treats such
    # a row as corrupt, so no live card can be waiting on it; cancel directly.
    if not row.pending_approval_request_id:
        return None
    try:
        card_build_id, stage_label, attempt_count = parse_request_id(
            row.pending_approval_request_id
        )
    except ValueError:
        logger.warning(
            "forge cancel: build %s is PAUSED but its "
            "pending_approval_request_id is unparseable; falling back to a "
            "direct cancel",
            build_id,
        )
        return None
    if card_build_id != build_id:
        raise PausedCardChanged(
            f"the card it is waiting at belongs to another build "
            f"({card_build_id})."
        )
    return PendingGateRequest(stage_label, attempt_count, row.correlation_id)


def send_paused_cancel(
    *, build_id: str, request: PendingGateRequest, reason: str, responder: str
) -> None:
    """Send the synthetic reject for ``request``, signed by ``responder``."""
    _inject_synthetic_reject(
        build_id=build_id,
        stage_label=request.stage_label,
        attempt_count=request.attempt_count,
        correlation_id=request.correlation_id,
        responder=responder,
    )
    logger.info(
        "forge cancel: build %s paused at the approval gate; sent a synthetic "
        "reject (%r, responder=%r)",
        build_id,
        reason,
        responder,
    )


def _inject_synthetic_reject(
    *,
    build_id: str,
    stage_label: str,
    attempt_count: int,
    correlation_id: str | None,
    responder: str | None = None,
) -> None:
    """One-shot connect → inject synthetic reject → flush → close.

    Mirrors ``forge.cli.queue.publish``'s fire-and-forget sync-over-async
    bridge. The synthetic reject keys on the persisted ``request_id`` (via
    ``attempt_count``), so the daemon subscriber's dedup + correlation guards
    treat it as a first-response reject for the outstanding request.
    ``responder`` is forwarded to the injector as the reject's ``decided_by``
    identity (O-02 / TASK-FWD-005 break #3).
    """
    import asyncio

    from forge.adapters.nats.synthetic_response_injector import (
        SyntheticResponseInjector,
    )

    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")

    async def _inject_once() -> None:
        import nats  # type: ignore[import-not-found]

        client = await nats.connect(servers=servers)
        try:
            injector = SyntheticResponseInjector(nats_client=client)
            await injector.inject_cli_cancel(
                build_id=build_id,
                stage_label=stage_label,
                attempt_count=attempt_count,
                correlation_id=correlation_id,
                responder=responder,
            )
            await client.flush()
        finally:
            await client.close()

    asyncio.run(_inject_once())
