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
from forge.cli._serve_async_task_starter import build_async_task_starter
from forge.cli._serve_config import ServeConfig
from forge.config.models import ForgeConfig
from forge.lifecycle.migrations import apply_at_boot
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.dispatchers.autobuild_async import AsyncTaskStarter

logger = logging.getLogger(__name__)


def _current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if uninitialised.

    Mirrors :func:`forge.lifecycle.migrations._current_version` so the
    boot log line can report the *count* of newly-applied migrations
    (``after - before``) — :func:`apply_at_boot` itself returns the
    schema version after the run, not the delta. A brand-new DB has no
    ``schema_version`` table, so an :class:`sqlite3.OperationalError`
    falls back to 0 (TASK-FORGE-FRR-F010A).
    """
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version;"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


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


def _resolve_async_task_starter(middleware: Any) -> AsyncTaskStarter:
    """Return an :class:`AsyncTaskStarter` adapter over ``middleware.tools``.

    The :class:`AsyncSubAgentMiddleware` exposes a ``tools`` attribute
    (sequence of LangChain :class:`StructuredTool`-shaped objects, each
    carrying a ``name``) per the FW10-008 wiring contract. This helper
    looks up the ``start_async_task`` entry by name and wraps it in the
    :func:`build_async_task_starter` adapter so the autobuild dispatcher
    sees the purpose-shaped
    :class:`forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
    Protocol surface (``start_async_task(subagent_name, context) -> str``)
    rather than the raw LangChain tool-invocation shape.

    The adapter is necessary because the middleware's
    :class:`StructuredTool` exposes ``invoke({...})`` / ``ainvoke({...})``
    against a ``(description, subagent_type, runtime)`` schema — not the
    named-method ``start_async_task(...)`` surface the dispatcher's
    Protocol declares. Returning the raw tool here was the cause of the
    GB10 ``AttributeError: 'StructuredTool' object has no attribute
    'start_async_task'`` regression on correlation_id
    ``dfad8e7f-92af-4b5f-896f-ca75ad8343bf`` — see TASK-FORGE-FRR-F010E
    for the investigation.

    A missing tool is an explicit fail-fast at boot rather than a
    deferred ``RuntimeError`` on the first inbound envelope (D3.A).
    """
    tools = tuple(getattr(middleware, "tools", ()) or ())
    for tool in tools:
        if getattr(tool, "name", None) == "start_async_task":
            return build_async_task_starter(tool)
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
    3.5. Apply any pending SQLite migrations via :func:`apply_at_boot`
       so a fresh ``FORGE_DB_PATH`` volume gets the canonical 5 tables
       (``async_tasks`` / ``builds`` / ``stage_log`` /
       ``sqlite_sequence`` / ``schema_version``) before the first
       inbound envelope reaches ``dispatch_build``
       (TASK-FORGE-FRR-F010A). Idempotent.
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
        ValueError: When ``forge_config`` is ``None`` or when
            ``config.autobuild_runner_url`` is missing/empty
            (TASK-FORGE-FRR-F010I/J — fail-fast at boot rather than
            failing at first build dispatch with the in-process ASGI
            ``'NoneType' object is not callable`` error).
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

    # Step 1.5 — validate ``autobuild_runner_url`` is set
    # (TASK-FORGE-FRR-F010I/J). The in-process ASGI fallback path
    # (``langgraph_sdk.get_client(url=None)`` →
    # ``ASGITransport(app=None)``) raises ``'NoneType' object is not
    # callable`` on every dispatch in the current forge venv (the
    # F010H investigation confirmed ``langgraph_api`` is not installed,
    # so ``get_client(url=None)``'s first branch falls through to the
    # broken loopback fallback). F010I picked Option B.1 (sidecar
    # URL); this guard makes the missing-URL case fail at boot — with
    # an actionable error message naming the env var — instead of
    # failing at first build dispatch with a low-level transport
    # exception. Fires BEFORE any filesystem / database / writer-
    # connection resource is allocated, so a missing URL never leaves
    # a partially-initialised daemon with an orphan SQLite handle.
    if not config.autobuild_runner_url:
        raise ValueError(
            "bind_production_serve: 'autobuild_runner_url' is required "
            "but missing/empty. The in-process ASGI fallback path "
            "(langgraph_sdk.get_client(url=None) → "
            "ASGITransport(app=None)) raises 'NoneType' object is not "
            "callable on every dispatch (TASK-FORGE-FRR-F010I/J). Set "
            "FORGE_AUTOBUILD_RUNNER_URL to the langgraph-runner sidecar "
            "URL (e.g. http://forge-autobuild-runner:8124 for compose "
            "service-name resolution, or http://localhost:8124 for an "
            "in-pod sidecar) and restart."
        )

    # Step 2 — auto-create parent dir so a fresh checkout does not need
    # ``mkdir -p ~/.forge`` before ``forge serve``.
    config.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 3 — open the writer connection.
    connection = connect_writer(config.db_path)

    # Step 3.5 — apply pending SQLite migrations idempotently
    # (TASK-FORGE-FRR-F010A). A fresh ``FORGE_DB_PATH`` volume on a
    # daemon-only deploy ships without the canonical 5 tables
    # (``async_tasks`` / ``builds`` / ``stage_log`` / ``sqlite_sequence``
    # / ``schema_version``), so the first inbound
    # ``pipeline.build-queued.*`` envelope would fail with
    # ``no such table: builds``. Mirrors the call pattern in
    # :mod:`forge.cli.queue` (the operator-facing CLI seam). Idempotent
    # — a re-bind in the same process emits ``applied 0`` so operators
    # can ``grep`` for it on subsequent boots.
    schema_version_before = _current_schema_version(connection)
    schema_version_after = apply_at_boot(connection)
    applied = max(0, schema_version_after - schema_version_before)
    logger.info(
        "forge-serve: applied %d SQLite migration(s) at boot", applied
    )

    # Step 4 — wrap the connection.
    sqlite_pool = SqliteLifecyclePersistence(
        connection=connection, db_path=config.db_path
    )

    # Lazy import to avoid an import cycle: ``forge.cli.serve`` imports
    # this module's public API only inside ``serve_cmd``'s body.
    from forge.cli import serve as serve_module

    # Step 5 — eagerly construct the middleware. ImportErrors / wiring
    # bugs raise here, before the daemon attaches its consumer.
    # TASK-FORGE-FRR-F010J: thread the langgraph-runner sidecar URL
    # into the ``AsyncSubAgent`` registration so deepagents'
    # ``_ClientCache.get_async()`` constructs an ``httpx.AsyncClient``
    # with a real URL transport instead of the broken
    # ``ASGITransport(app=None)`` fallback. The Step 1.5 guard above
    # has already validated the URL is non-empty.
    middleware = serve_module._build_async_subagent_middleware(
        autobuild_runner_url=config.autobuild_runner_url,
    )

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
