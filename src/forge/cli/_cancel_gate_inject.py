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
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["try_inject_paused_cancel"]


def try_inject_paused_cancel(runtime: Any, *, build_id: str, reason: str) -> bool:
    """Inject a synthetic reject for a gate-paused build; return True on success.

    Reads the persisted ``pending_approval_request_id`` (the durable home of the
    paused ``(stage_label, attempt_count)`` pair), parses it, and publishes a
    synthetic reject onto the response mirror subject via the one-shot NATS
    connect pattern (mirrors ``forge.cli.queue.publish``). Returns ``False``
    (caller falls back to ``handle_cancel``) when the row carries no pending
    request id or the id is a legacy / unparseable value — there is nothing for
    the daemon's live frame to correlate a synthetic reject against.
    """
    from forge.gating.identity import parse_request_id

    row = runtime.persistence.get_build_row(build_id)
    if row is None or not row.pending_approval_request_id:
        return False
    try:
        _bid, stage_label, attempt_count = parse_request_id(
            row.pending_approval_request_id
        )
    except ValueError:
        logger.warning(
            "forge cancel: build %s is PAUSED but its "
            "pending_approval_request_id is unparseable; falling back to a "
            "direct cancel",
            build_id,
        )
        return False

    _inject_synthetic_reject(
        build_id=build_id,
        stage_label=stage_label,
        attempt_count=attempt_count,
        correlation_id=row.correlation_id,
    )
    logger.info(
        "forge cancel: build %s paused at the approval gate; injected a "
        "synthetic reject (%r) onto its approval response subject — the "
        "daemon's live frame owns the CANCELLED transition + build-cancelled",
        build_id,
        reason,
    )
    return True


def _inject_synthetic_reject(
    *,
    build_id: str,
    stage_label: str,
    attempt_count: int,
    correlation_id: str | None,
) -> None:
    """One-shot connect → inject synthetic reject → flush → close.

    Mirrors ``forge.cli.queue.publish``'s fire-and-forget sync-over-async
    bridge. The synthetic reject keys on the persisted ``request_id`` (via
    ``attempt_count``), so the daemon subscriber's dedup + correlation guards
    treat it as a first-response reject for the outstanding request.
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
            )
            await client.flush()
        finally:
            await client.close()

    asyncio.run(_inject_once())
