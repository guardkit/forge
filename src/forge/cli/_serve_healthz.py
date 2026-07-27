"""``forge serve`` healthz HTTP server (TASK-F009-004 implementation).

This module owns the boundary surface for the readiness-probe side of
``forge serve``. It binds an :mod:`aiohttp` HTTP server to
``ServeConfig.healthz_port`` (Integration Contract B; default ``8080``)
and exposes a single endpoint:

- ``GET /healthz`` -> ``200`` with ``{"status": "healthy"}`` when
  ``SubscriptionState.is_live()`` returns ``True``.
- ``GET /healthz`` -> ``503`` with
  ``{"status": "unhealthy", "reason": "subscription_not_live"}``
  otherwise.
- All other paths and verbs return ``404``/``405`` (E1.3 — no
  remote-access endpoints).

The split between :mod:`forge.cli._serve_daemon` and
:mod:`forge.cli._serve_healthz` exists so T3 (daemon body) and T4 (this
file) could land independently in Wave 2 without colliding on a single
``serve.py``. Do not consolidate later.

Runtime model
-------------

``run_healthz_server`` is a coroutine designed to live forever inside
``asyncio.gather(run_daemon(...), run_healthz_server(...))``. It returns
only by being cancelled — typically when ``serve_cmd`` propagates a
``SIGTERM`` cancellation or when the daemon coroutine raises and
``gather`` cancels its sibling. The ``finally`` block always invokes
``runner.cleanup()`` so the listening socket is released cleanly with no
port leak across restarts.

Implementation notes from the task:

- The :mod:`aiohttp` package is already a transitive dependency of
  ``langchain`` / ``langgraph`` / ``langchain-anthropic`` (verified at
  install time), so we deliberately avoid adding a new HTTP framework.
- ``SubscriptionState`` is mutated by the daemon side of
  ``forge serve`` and read here. Both coroutines share a single
  ``SubscriptionState`` instance built by ``serve_cmd`` so producer and
  consumer cannot drift.
- The :func:`build_healthz_app` factory exists so unit tests can
  exercise the request handler with ``aiohttp.test_utils.TestClient``
  without binding a real TCP socket.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from forge.cli._serve_config import ServeConfig
from forge.cli._serve_state import SubscriptionState

logger = logging.getLogger(__name__)

#: Typed key under which the shared :class:`SubscriptionState` is stashed on
#: the aiohttp ``Application`` so the request handler can reach it without
#: needing a closure over a module-level global. Using :class:`web.AppKey`
#: (rather than a bare string) silences ``NotAppKeyWarning`` and gives
#: type-checkers a precise type for ``request.app[STATE_KEY]``.
STATE_KEY: web.AppKey[SubscriptionState] = web.AppKey(
    "subscription_state", SubscriptionState
)


async def _healthz_handler(request: web.Request) -> web.Response:
    """Handle ``GET /healthz`` — read shared state, return JSON.

    Returns ``200``/``healthy`` only when **both** readiness flags are
    True (TASK-FW10-001 ASSUM-012):

    * ``state.live`` — JetStream subscription is bound (Acceptance
      Criterion B5).
    * ``state.chain_ready`` — orchestrator dispatch chain has been
      composed and both ``reconcile_on_boot`` routines have completed.

    Otherwise the endpoint returns ``503``/``unhealthy`` and the
    ``reason`` field distinguishes which gate is closed:

    * ``chain_not_ready`` — chain composition / reconcile_on_boot has
      not finished (kubelet should keep the pod out of the Service
      until reconcile completes).
    * ``subscription_not_live`` — chain is ready but the broker is
      gone (Group E scenario row 3 — the daemon is composed but the
      pull subscription has dropped).

    We do not take the lock on the read side: the booleans are atomic
    attribute reads under Python's GIL, and a one-tick-stale read is
    the explicit contract for the readiness probe (TASK-F009-001
    AC-006). ``chain_ready`` order matters in the response: it is
    checked first because an unready chain is a stronger signal — if
    the chain has not been composed, the ``live`` flag is meaningless
    (the daemon may not have attached yet).
    """
    state: SubscriptionState = request.app[STATE_KEY]
    # FEAT-PAC healthz seam (this file:_healthz_handler): every response
    # gains an additive ``ack_slot`` field carrying the last boot/watchdog
    # ack-slot reading (``healthy`` / ``held`` / ``phantom`` / ``unknown``),
    # so a standing ``curl :8088/healthz`` makes the phantom-ack wedge
    # visible. It is advisory-only — the wedge is alarm-only in v1 and does
    # NOT change the readiness verdict below or any existing key.
    ack_slot = state.ack_slot
    if not state.is_chain_ready():
        return web.json_response(
            {
                "status": "unhealthy",
                "reason": "chain_not_ready",
                "ack_slot": ack_slot,
            },
            status=503,
        )
    if not state.is_live():
        return web.json_response(
            {
                "status": "unhealthy",
                "reason": "subscription_not_live",
                "ack_slot": ack_slot,
            },
            status=503,
        )
    return web.json_response(
        {"status": "healthy", "ack_slot": ack_slot}, status=200
    )


def build_healthz_app(state: SubscriptionState) -> web.Application:
    """Build the :class:`aiohttp.web.Application` backing the healthz endpoint.

    Factored out of :func:`run_healthz_server` so unit tests can use
    :class:`aiohttp.test_utils.TestClient` against a freshly-built app
    without binding a real socket.

    Args:
        state: Shared :class:`SubscriptionState` instance owned by
            ``serve_cmd``. Stashed on the application object under
            :data:`_STATE_KEY` so the request handler can reach it.

    Returns:
        A configured :class:`aiohttp.web.Application` exposing exactly
        one route — ``GET /healthz`` — and nothing else.
    """
    app = web.Application()
    app[STATE_KEY] = state
    app.router.add_get("/healthz", _healthz_handler)
    return app


async def run_healthz_server(
    config: ServeConfig, state: SubscriptionState
) -> None:
    """Run the healthz HTTP server until cancelled.

    Binds to ``config.healthz_port`` (Integration Contract B; default
    ``8080``, overridable via ``FORGE_HEALTHZ_PORT`` since
    :meth:`ServeConfig.from_env` honours that env var). Listens on all
    interfaces so the kubelet's readiness probe — which contacts the
    pod IP, not ``127.0.0.1`` — can reach the endpoint.

    The coroutine blocks forever on an unset :class:`asyncio.Event`. The
    only intended exit path is cancellation: either a ``SIGTERM``
    propagates through ``serve_cmd`` -> ``asyncio.gather`` and arrives
    here as :class:`asyncio.CancelledError`, or the sibling daemon
    coroutine fails and ``gather`` cancels us. In both cases the
    ``finally`` clause runs :meth:`AppRunner.cleanup` to release the
    listening socket so a process restart does not bump into a stale
    bind (R7 — no port-leak on restart).

    Args:
        config: Daemon configuration; only ``healthz_port`` is read
            here. Constructed by ``serve_cmd`` via
            :meth:`ServeConfig.from_env`, which already resolves
            ``FORGE_HEALTHZ_PORT``.
        state: Shared readiness flag mutated by the daemon side and
            read by the request handler.

    Raises:
        asyncio.CancelledError: Re-raised after cleanup so callers
            (``asyncio.gather`` / ``serve_cmd``) observe the
            cancellation and can exit deterministically.
    """
    app = build_healthz_app(state)
    runner = web.AppRunner(app)
    await runner.setup()

    # ``host=0.0.0.0`` matches the Dockerfile EXPOSE directive (T5) and
    # the kubelet's readiness-probe contract — the probe contacts the
    # pod IP, not ``127.0.0.1``.
    site = web.TCPSite(runner, host="0.0.0.0", port=config.healthz_port)
    try:
        await site.start()
        logger.info(
            "healthz server listening on 0.0.0.0:%d (durable=%s)",
            config.healthz_port,
            config.durable_name,
        )
        # Block until cancelled. ``asyncio.Event().wait()`` is the
        # cheapest "sleep forever" primitive in stdlib asyncio.
        await asyncio.Event().wait()
    finally:
        # Always release the socket, even if cancellation is mid-flight.
        # ``AppRunner.cleanup`` is idempotent and tolerant of being
        # called before ``site.start`` succeeded, so this is safe in
        # both happy and failed-bind paths.
        await runner.cleanup()


__all__ = ["STATE_KEY", "build_healthz_app", "run_healthz_server"]
