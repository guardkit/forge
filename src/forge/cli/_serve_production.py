"""Production wiring wrapper for ``forge serve`` (TASK-FIX-F010).

This module is the thin ops-only seam that closes the FEAT-DEA8
production-wiring gap surfaced by the jarvis FRR rerun on 2026-05-04
(correlation_id ``18036705-2bb7-4564-8363-315bf7716a48``). FEAT-FORGE-010
shipped :func:`forge.cli.serve.bind_production_dispatch_chain` as a
factory but never wired the closure into ``serve_cmd`` — every inbound
``pipeline.build-queued.*`` envelope was acked by the receipt-only
``_default_dispatch`` stub at
:mod:`forge.cli._serve_daemon` and silently discarded.

The fix lives in this dedicated module rather than inline in
:mod:`forge.cli.serve` because:

* The smoke tests under ``tests/forge/test_cli_serve_skeleton.py`` and
  ``tests/forge/test_cli_serve_logging.py`` need exactly one mock seam to
  bypass the production deps graph (``monkeypatch.setattr(serve_module,
  "bind_production_serve", lambda *a, **kw: None)``). Inlining the
  wiring inside ``serve_cmd`` would force every smoke test to stub the
  whole SQLite + middleware graph (TASK-REV-F010 D5.A).
* Boot-order ownership stays in ``_run_serve``; production-deps wiring
  stays here. Mixing them was explicitly rejected by TASK-FW10-001.

The single public entry-point :func:`bind_production_serve` is
idempotent — safe to call multiple times in the same process — because
``forge serve`` may be re-invoked under the same Python interpreter from
integration tests and ``langgraph dev`` reloads. Each call closes the
previous SQLite writer connection cleanly so handles do not leak.

References:
    - Review: ``.claude/reviews/TASK-REV-F010-review-report.md``
    - Sibling task (regression lock): TASK-FW10-011
    - Original dispatch-chain factory: :func:`forge.cli.serve.bind_production_dispatch_chain`
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from forge.adapters.sqlite.connect import connect_writer
from forge.cli._serve_config import ServeConfig
from forge.config.models import ForgeConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence

logger = logging.getLogger(__name__)


@dataclass
class _BoundResources:
    """Captures the resources owned by the most recent binding.

    Tracked at module level so a re-invocation of
    :func:`bind_production_serve` (e.g. from a long-running test process
    or an ops re-bind) can close the previous SQLite writer connection
    before installing a new one. The dataclass is private — tests reset
    it via :func:`_reset_for_tests` to keep idempotency assertions
    deterministic.
    """

    connection: sqlite3.Connection


_bound_resources: _BoundResources | None = None


def _reset_for_tests() -> None:
    """Reset the module-level binding state. Test-only helper.

    Production code must never call this — it discards the
    book-keeping handle without closing the underlying connection so a
    test that already mocked ``connect_writer`` can rewind cleanly.
    """
    global _bound_resources
    _bound_resources = None


def _close_previous_connection_quietly(previous: _BoundResources) -> None:
    """Best-effort close of the previous SQLite writer connection.

    A stale handle may already be closed (or invalid) if the previous
    process branch crashed; swallowing the exception here matches the
    pattern used by :func:`forge.cli.serve._close_client_quietly` for
    the NATS client. The previous handle is discarded regardless of
    success so the caller cannot accidentally reuse it.
    """
    try:
        previous.connection.close()
    except sqlite3.Error as exc:
        logger.debug(
            "forge-serve: previous SQLite writer connection close error "
            "(%s); discarding handle anyway",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 - defensive on rebind path
        logger.debug(
            "forge-serve: unexpected error closing previous SQLite writer "
            "connection (%s); discarding handle anyway",
            exc,
        )


def _resolve_async_task_starter(middleware: Any) -> Any:
    """Return the ``start_async_task`` tool exposed by ``middleware``.

    The :class:`AsyncSubAgentMiddleware` exposes a ``tools`` attribute
    (sequence of LangChain :class:`StructuredTool`-shaped objects, each
    carrying a ``name``) per the FW10-008 wiring contract. This helper
    looks up the ``start_async_task`` entry by name and returns the tool
    object itself — the same surface
    :class:`forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
    accepts at runtime.

    A missing tool is an explicit fail-fast at boot rather than a
    deferred ``RuntimeError`` on the first inbound envelope (D3.A).
    """
    tools = tuple(getattr(middleware, "tools", ()) or ())
    for tool in tools:
        if getattr(tool, "name", None) == "start_async_task":
            return tool
    available = sorted({getattr(t, "name", "<unnamed>") for t in tools})
    raise RuntimeError(
        "bind_production_serve: AsyncSubAgentMiddleware did not expose a "
        "'start_async_task' tool; available tool names were "
        f"{available!r}. This is a wiring bug — TASK-FW10-008 contract "
        "requires the middleware to surface start_async_task / "
        "check_async_task / update_async_task / cancel_async_task / "
        "list_async_tasks."
    )


def bind_production_serve(
    config: ServeConfig, forge_config: ForgeConfig
) -> None:
    """Wire :data:`forge.cli.serve.compose_dispatch_chain` to production deps.

    Idempotent — safe to call multiple times. On a re-bind the previous
    SQLite writer connection is closed cleanly before the new one is
    installed.

    Pipeline:

    1. Validate ``forge_config`` is non-None (the daemon's allowlists
       and approved_originators rules read from it).
    2. Ensure ``config.db_path.parent`` exists (matches the implicit
       ``forge queue`` assumption — fresh checkouts must not need
       manual ``mkdir``).
    3. Open the SQLite writer connection via :func:`connect_writer`.
    4. Wrap it in :class:`SqliteLifecyclePersistence`.
    5. Eagerly construct the :class:`AsyncSubAgentMiddleware` via the
       existing :func:`forge.cli.serve._build_async_subagent_middleware`
       helper so any DeepAgents import error surfaces at boot rather
       than on the first envelope (TASK-REV-F010 D3.A).
    6. Resolve the ``start_async_task`` tool from ``middleware.tools``.
    7. Build the closure via
       :func:`forge.cli.serve.bind_production_dispatch_chain` and
       rebind ``forge.cli.serve.compose_dispatch_chain`` to it.
    8. Close any previous SQLite writer connection captured by a prior
       binding.

    Args:
        config: Validated :class:`ServeConfig` — supplies ``db_path``.
        forge_config: Validated :class:`ForgeConfig` — passed straight
            through to the consumer's allowlist / approved_originators
            rejection rules. ``None`` raises :class:`ValueError`.

    Raises:
        ValueError: When ``forge_config`` is ``None``.
        RuntimeError: When the middleware does not expose a
            ``start_async_task`` tool (FW10-008 contract violation).
    """
    global _bound_resources

    if forge_config is None:
        raise ValueError(
            "bind_production_serve: 'forge_config' is required; the daemon "
            "reads approved_originators and the filesystem allowlist from "
            "it. Pass --config <path> to ``forge serve`` or run from a "
            "directory containing ./forge.yaml."
        )

    # Step 2 — auto-create parent dir so a fresh checkout does not need
    # ``mkdir -p ~/.forge`` before ``forge serve``.
    config.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 3 + 4 — open the writer connection and wrap it.
    connection = connect_writer(config.db_path)
    sqlite_pool = SqliteLifecyclePersistence(
        connection=connection, db_path=config.db_path
    )

    # Lazy import to avoid an import cycle: ``forge.cli.serve`` imports
    # this module's public API only inside ``serve_cmd``'s body.
    from forge.cli import serve as serve_module

    # Step 5 — eagerly construct the middleware. ImportErrors / wiring
    # bugs raise here, before the daemon attaches its consumer.
    middleware = serve_module._build_async_subagent_middleware()

    # Step 6 — derive the AsyncTaskStarter from the middleware tool
    # surface (per TASK-FW10-008 contract).
    async_task_starter = _resolve_async_task_starter(middleware)

    # Step 7 — build the production composer and rebind the seam.
    composer = serve_module.bind_production_dispatch_chain(
        forge_config=forge_config,
        sqlite_pool=sqlite_pool,
        async_task_starter=async_task_starter,
    )
    serve_module.compose_dispatch_chain = composer

    # Step 8 — close any previous binding's writer connection cleanly.
    previous = _bound_resources
    _bound_resources = _BoundResources(connection=connection)
    if previous is not None:
        _close_previous_connection_quietly(previous)

    logger.info(
        "forge-serve: production composer bound (db_path=%s)",
        config.db_path,
    )


__all__ = ["bind_production_serve"]
