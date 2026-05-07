"""Operator-cancel routing for the ``forge serve`` daemon (TASK-FRR-PEB-007).

This module is the daemon-side seam between an operator-issued cancel
(``forge cancel`` over CLI / a NATS ``pipeline.cancel-requested`` envelope
/ a future admin HTTP endpoint) and the
:class:`forge.lifecycle_bridge.LifecycleBridge`. It owns one rule:

    Operator cancel → ``LifecycleBridge.request_cancel(...)``;
    **never** synthesise a ``pipeline.build-cancelled`` envelope here.

Why this matters
----------------

Pre-TASK-FRR-PEB-007, an in-process cancel handler would publish
``pipeline.build-cancelled`` synchronously the moment the operator
issued the cancel. That meant two emit sites for the same envelope —
one optimistic (synchronous, ahead of the sidecar acknowledging the
interrupt) and one observational (the SSE translator, when the sidecar
reported ``terminal=interrupted``). Two sites means the FEAT-FORGE-004
"exactly one envelope per terminal" contract was easy to break: a fast
sidecar would race both publishes and downstream consumers saw two
``build-cancelled`` events for one operator action.

Q7 sub-option (b) of the FRR-PEB scoping doc resolved this by giving
the bridge **sole emit authority** for ``build-cancelled``. The cancel
handler asks the sidecar to interrupt via the SDK, returns immediately,
and the bridge — already attached to the SSE stream — sees the
``terminal=interrupted`` snapshot and asks the translator to emit the
envelope. One emit site, one envelope per terminal, idempotent under
two concurrent cancels.

Idempotency contract (AC-5)
---------------------------

Concurrent cancel requests for the same in-flight build produce
exactly one ``runs.cancel`` SDK call and exactly one envelope. The
bridge holds the cancel-in-flight flag — this handler simply
forwards every request to ``request_cancel`` and returns the
:class:`CancelResult` so the caller can distinguish the freshly issued
cancel from the duplicate-request no-op (e.g. for CLI exit-status
reporting).

No-bridge fallback
------------------

When the daemon is constructed without a wired bridge (some unit-test
setups, the legacy reconcile-only boot path), :func:`cancel_via_bridge`
returns a :class:`CancelResult` with ``invoked=False`` and
``reason="no-bridge"`` rather than raising — this preserves the
"cancel of unknown is non-fatal" stance the CLI cancel command takes
(:mod:`forge.cli.cancel`'s exit-2 behaviour).

The handler is intentionally a single function plus a thin dataclass
result rather than a class. The serve daemon already composes its
dependencies via small functions (see :mod:`forge.cli._serve_deps`);
adding a class here would create a parallel composition path for no
behavioural gain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from forge.lifecycle_bridge.bridge import CancelResult, LifecycleBridge

logger = logging.getLogger(__name__)


__all__ = [
    "CancelHandlerOutcome",
    "cancel_via_bridge",
]


@dataclass(frozen=True, slots=True)
class CancelHandlerOutcome:
    """Result of an operator-cancel routed through :func:`cancel_via_bridge`.

    Wraps the bridge's :class:`CancelResult` with a textual ``reason``
    that the caller can surface in CLI output, structured logs, or
    daemon-side metrics. The ``invoked`` flag mirrors
    :attr:`CancelResult.invoked`: ``True`` when this call issued the
    SDK cancel, ``False`` for the duplicate-request / no-bridge / no-row
    no-ops.

    Attributes:
        feature_id: Build the cancel was directed at.
        invoked: ``True`` when ``runs.cancel(...)`` was issued by this
            call. ``False`` for any no-op outcome.
        reason: Short text label suitable for ``%s``-formatted logs and
            user-facing CLI output. One of ``"invoked"``,
            ``"no-bridge"``, ``"already-cancelling"`` (cancel-in-flight
            duplicate), or ``"unknown-build"`` (no registry row).
        cancel_result: The underlying :class:`CancelResult` from the
            bridge, or ``None`` when the no-bridge fallback short-
            circuits. Exposed for callers that want the
            registry-derived ids (``thread_id`` / ``run_id``).
    """

    feature_id: str
    invoked: bool
    reason: str
    cancel_result: CancelResult | None = None


async def cancel_via_bridge(
    bridge: LifecycleBridge | None,
    feature_id: str,
) -> CancelHandlerOutcome:
    """Route an operator cancel through the lifecycle bridge.

    AC-4: this is the cancel handler that replaces synchronous
    ``pipeline.build-cancelled`` emission. It calls
    :meth:`LifecycleBridge.request_cancel` and returns immediately.
    The bridge does not publish; the SSE translator publishes when the
    sidecar reports ``terminal=interrupted``.

    Args:
        bridge: The wired :class:`LifecycleBridge`, or ``None`` when no
            bridge is available (legacy boot path / unit-test setups).
            ``None`` triggers the no-bridge fallback.
        feature_id: Primary key of the build to cancel. Refused when
            empty — same stance the registry takes against an empty
            primary key.

    Returns:
        :class:`CancelHandlerOutcome` describing whether the SDK cancel
        was issued and a textual reason suitable for CLI output and
        structured logging.

    Raises:
        ValueError: If ``feature_id`` is empty.
    """
    if not feature_id:
        raise ValueError(
            "cancel_via_bridge: feature_id must be a non-empty string"
        )

    if bridge is None:
        # No-bridge fallback (test paths and legacy boot). The CLI cancel
        # command surfaces this via exit-2 "no active or recent build"
        # rather than a hard failure.
        logger.info(
            "_serve_handlers.cancel_via_bridge feature_id=%s status=no-op "
            "reason=no-bridge",
            feature_id,
        )
        return CancelHandlerOutcome(
            feature_id=feature_id,
            invoked=False,
            reason="no-bridge",
            cancel_result=None,
        )

    # Delegate to the bridge — the bridge owns idempotency, SDK
    # invocation, and *crucially* does not publish the envelope. The
    # bridge's :class:`CancelResult.reason` already carries the
    # categorical outcome ("invoked" / "already-cancelling" /
    # "no-registry-row"); we map "no-registry-row" to the
    # operator-facing label "unknown-build" so the CLI surfaces a
    # clearer diagnostic without leaking the registry-internal term.
    result = await bridge.request_cancel(feature_id)

    if result.reason == "no-registry-row":
        reason = "unknown-build"
    else:
        reason = result.reason

    logger.info(
        "_serve_handlers.cancel_via_bridge feature_id=%s status=%s "
        "thread_id=%s run_id=%s",
        feature_id,
        reason,
        result.thread_id,
        result.run_id,
    )

    return CancelHandlerOutcome(
        feature_id=feature_id,
        invoked=result.invoked,
        reason=reason,
        cancel_result=result,
    )
