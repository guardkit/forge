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
from typing import TYPE_CHECKING, Any

from forge.adapters.sqlite.connect import connect_writer
from forge.cli._serve_async_task_starter import build_async_task_starter
from forge.cli._serve_config import ServeConfig
from forge.config.models import ForgeConfig
from forge.lifecycle.migrations import apply_at_boot
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle_bridge import (
    LifecycleBridge,
    LifecycleBridgeWireup,  # noqa: F401  (re-exported via wireup parts contract)
    RunStateFetcher,
    StreamEventTranslator,
    StreamSource,
    TerminalPublishLedger,
    build_build_state_recorder,
    langgraph_run_state_fetcher,
    langgraph_stream_source,
)
from forge.lifecycle_bridge import coexistence as _bridge_coexistence
from forge.persistence.migrations import (
    lifecycle_bridge_registry as _bridge_registry_migration,
)
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.dispatchers.autobuild_async import AsyncTaskStarter

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from forge.lifecycle_bridge.wireup import BuildStateRecorder, IdentityProvider

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


@dataclass(frozen=True)
class LifecycleBridgeWireupParts:
    """SQLite-bound dependencies for :class:`LifecycleBridgeWireup`.

    The wireup itself cannot be constructed in
    :func:`bind_production_serve` because it requires the
    :class:`PipelinePublisher`, which is only available inside the
    closure returned by
    :func:`forge.cli.serve.bind_production_dispatch_chain` (where the
    NATS client has been opened). This struct carries the parts that
    DO depend on the SQLite pool / runner_url so they can be threaded
    through ``bind_production_dispatch_chain`` into its closure.

    TASK-FORGE-FRR-PEBR-WIREUP — closes Gap PEBR-WIREUP surfaced by the
    2026-05-08 jarvis runbook walkthrough on GB10
    (correlation_id=5673965b-e302-4a10-89cb-ceb430e64995).
    """

    bridge: LifecycleBridge
    translator: StreamEventTranslator
    stream_source: StreamSource
    identity_provider: "IdentityProvider"
    run_state_fetcher: RunStateFetcher
    terminal_publish_ledger: TerminalPublishLedger
    build_state_recorder: "BuildStateRecorder"
    # TASK-JNB-101 — public registry handle so the approval-gate wiring
    # can bind ``ApprovalSubscriberDeps.bridge_registry_lookup`` to the
    # same registry instance the bridge itself consults (the bridge only
    # keeps it privately; reaching into ``bridge._registry`` from the
    # compose closure would be an encapsulation violation).
    registry: BridgeRegistry


def _build_async_tasks_identity_provider(
    *,
    sqlite_pool: SqliteLifecyclePersistence,
    autobuild_runner_url: str,
) -> "IdentityProvider":
    """Return an :data:`IdentityProvider` resolving ``(thread_id, run_id)``.

    Two-step hybrid resolution (TASK-REV-PEBR-003 §AC-3 — chosen over
    schema-migration because the ``async_tasks`` SQLite mirror does not
    carry a ``run_id`` column and one HTTP round-trip per identity
    poll fits comfortably within the wireup's per-poll budget):

    1. SQLite read: ``SELECT task_id FROM async_tasks WHERE feature_id
       = ? ORDER BY started_at DESC, rowid DESC LIMIT 1``. ``task_id``
       equals ``thread_id`` per
       :mod:`forge.cli._serve_async_task_starter` (line 148-149: the
       state-channel command writes one entry keyed by the
       just-launched task's ``thread_id``). The dispatcher writes
       this row inside :func:`dispatch_autobuild_async` BEFORE
       :meth:`start_async_task` returns; the wireup's
       :meth:`register_ack_handle` runs BEFORE
       :func:`dispatch_autobuild_async`, so the row is not yet present
       at the first poll. Returns ``None`` on miss; the wireup retries
       per its ``identity_resolution_attempts`` budget.

       ``ORDER BY started_at DESC`` is load-bearing: ``async_tasks``
       accumulates one row per dispatched build and rows for dead
       builds are not garbage-collected, so an unordered ``LIMIT 1``
       pins resolution to the *oldest* row — whose thread no longer
       exists on a restarted sidecar, 404ing every poll while the live
       run streams unobserved (observed 2026-07-04 on GB10 for
       FEAT-9E59). Newest-first makes the just-dispatched row win;
       ``rowid DESC`` breaks same-instant ties.

    2. ``langgraph_sdk`` fetch: once ``thread_id`` is known, call
       ``client.runs.list(thread_id, limit=1)`` (verified against
       installed ``langgraph_sdk`` 0.3.13: ``list(thread_id, *,
       limit=10, ...) -> list[Run]``) and return
       ``(thread_id, runs[0].run_id)``. Empty list → ``None``;
       transport errors are caught and downgraded to ``None`` so the
       observer's reconnect loop can exit cleanly to JetStream
       redelivery.

    Args:
        sqlite_pool: Shared writer connection facade for the
            ``async_tasks`` read.
        autobuild_runner_url: URL of the langgraph-runner sidecar.
            Validated by :class:`ServeConfig`'s fail-fast guard so it
            is non-empty here.

    Returns:
        An ``async (feature_id) -> tuple[str, str] | None`` callable
        conforming to :data:`IdentityProvider`.
    """

    async def _provider(feature_id: str) -> tuple[str, str] | None:
        # Step 1 — SQLite lookup against the shared writer connection.
        try:
            row = sqlite_pool.connection.execute(
                "SELECT task_id FROM async_tasks WHERE feature_id = ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (feature_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "_async_tasks_identity_provider: SQLite read failed for "
                "feature_id=%s (%s); treating as None",
                feature_id,
                exc,
            )
            return None
        if row is None:
            return None
        thread_id: str = row[0] if not hasattr(row, "keys") else row["task_id"]

        # Step 2 — langgraph_sdk lookup for run_id.
        try:
            from langgraph_sdk import get_client

            client = get_client(url=autobuild_runner_url)
            runs = await client.runs.list(thread_id, limit=1)
            if not runs:
                return None
            run_id = (
                runs[0].get("run_id")
                if isinstance(runs[0], dict)
                else getattr(runs[0], "run_id", None)
            )
            if run_id is None:
                return None
            return (thread_id, run_id)
        except Exception as exc:  # noqa: BLE001 - downgrade transport / SDK errors
            logger.warning(
                "_async_tasks_identity_provider: failed to resolve "
                "run_id for feature_id=%s thread_id=%s (%s); treating "
                "as None",
                feature_id,
                thread_id,
                exc,
            )
            return None

    return _provider


def _build_lifecycle_bridge_wireup_parts(
    *,
    sqlite_pool: SqliteLifecyclePersistence,
    autobuild_runner_url: str,
) -> LifecycleBridgeWireupParts:
    """Construct the SQLite-bound dependencies for :class:`LifecycleBridgeWireup`.

    Pipeline (TASK-FORGE-FRR-PEBR-WIREUP §1):

    1. :class:`BridgeRegistry` against the writer connection.
    2. :class:`LifecycleBridge` wrapping the registry — no SDK client
       and no deadline handler are wired here (operator-cancel and
       deadline enforcement are out of scope for the PEBR-WIREUP
       fix; the wireup itself is the canonical deadline handler, but
       its construction site is the closure inside
       :func:`bind_production_dispatch_chain`).
    3. :class:`StreamEventTranslator` — zero-arg.
    4. :func:`langgraph_stream_source(runner_url=...)` — production
       SSE adapter.
    5. ``_build_async_tasks_identity_provider(...)`` — hybrid SQLite +
       ``langgraph_sdk`` identity resolver.
    6. :class:`TerminalPublishLedger` against the same writer
       connection (the migration that creates its table is invoked at
       Step 3.5 of :func:`bind_production_serve`).

    Args:
        sqlite_pool: Shared :class:`SqliteLifecyclePersistence`.
        autobuild_runner_url: Validated sidecar URL.

    Returns:
        A frozen :class:`LifecycleBridgeWireupParts`.
    """
    connection = sqlite_pool.connection

    registry = BridgeRegistry(connection=connection)
    bridge = LifecycleBridge(registry=registry)
    translator = StreamEventTranslator()
    stream_source = langgraph_stream_source(runner_url=autobuild_runner_url)
    identity_provider = _build_async_tasks_identity_provider(
        sqlite_pool=sqlite_pool,
        autobuild_runner_url=autobuild_runner_url,
    )
    # TASK-REV-PEBR-005 (FOLLOWUP-C-RACE) — fetch-on-empty fallback for
    # the join_stream race against fast-completing runs. The fetcher is
    # consulted by ``LifecycleBridgeWireup._observer_loop`` after the
    # SSE iterator closes empty; on a terminal run it replays the
    # final state through the existing translator so the canonical
    # envelope shape lands without subscribe-before-dispatch
    # restructuring (which would require modifying deepagents'
    # AsyncSubAgentMiddleware — out of forge's modify-able surface).
    run_state_fetcher = langgraph_run_state_fetcher(runner_url=autobuild_runner_url)
    terminal_publish_ledger = TerminalPublishLedger(connection=connection)
    # Builds-row write-back for published lifecycle envelopes — without
    # it the row stays QUEUED past terminal and exists_active_build
    # wedges the feature's next dispatch (2026-07-04 GB10 gap).
    build_state_recorder = build_build_state_recorder(sqlite_pool)

    return LifecycleBridgeWireupParts(
        bridge=bridge,
        translator=translator,
        stream_source=stream_source,
        identity_provider=identity_provider,
        run_state_fetcher=run_state_fetcher,
        terminal_publish_ledger=terminal_publish_ledger,
        build_state_recorder=build_state_recorder,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Boot reconcile seam bindings (TASK-GATE-D659 §D4.1-2 / §D4.4, "Step 6.8")
# ---------------------------------------------------------------------------
#
# THREE-ACTOR OWNERSHIP BOUNDARY at boot (arch-review C1 + C2):
#
#   1. ``recovery.reconcile_on_boot`` (the SQLite recovery matrix) — owns the
#      PREPARING / RUNNING / FINALISING → INTERRUPTED transitions and their
#      ``build-failed`` emits. Its PAUSED approval re-emit is SUPPRESSED here
#      via a no-op :class:`_NoopApprovalRepublisher` (recovery.py itself is
#      unmodified) because re-emitting the approval request before any response
#      subscriber exists drops the operator's tap on core NATS (C1 tap-drop).
#   2. ``pipeline_consumer.reconcile_on_boot`` (the JetStream twin seam) — owns
#      the in-flight / INTERRUPTED redelivery redispatch (Branch-2). Its PAUSED
#      scan is SUPPRESSED (empty enumerator + no-op re-emit fns) because rearm
#      owns PAUSED. Its redelivery *drain* is deferred to the live consumer +
#      the ``dispatch_build`` three-arm INTERRUPTED→redispatch (single-consumer
#      rule err 10100 — a boot-time pull subscription would collide with the
#      daemon's own durable attach; the third arm heals crash-mid-hop rows on
#      the first post-boot redelivery).
#   3. ``rearm_paused_gates`` (spawned from ``serve._compose``, AFTER the two
#      reconciles + with a LIVE response subscriber) — owns BOTH PAUSED
#      re-emits (approval request + build-paused) and the re-armed round-trip.


class _NoopApprovalRepublisher:
    """No-op :class:`ApprovalRepublisher` for the boot recovery seam (§D4.1).

    ``recovery.reconcile_on_boot`` calls ``publish_request`` once per PAUSED
    row. Swallowing it (INFO log) suppresses recovery's PAUSED approval re-emit
    so it cannot fire before a subscriber exists (C1). ``rearm_paused_gates``
    re-emits for real, arm-before-post, once ``_compose`` has bound a live
    subscriber.
    """

    async def publish_request(self, envelope: Any) -> None:
        build_id: Any = None
        payload = getattr(envelope, "payload", None)
        if isinstance(payload, dict):
            details = payload.get("details")
            if isinstance(details, dict):
                build_id = details.get("build_id")
        logger.info(
            "forge-serve: recovery boot seam SUPPRESSING PAUSED approval "
            "re-emit for build_id=%s — rearm_paused_gates owns it (live "
            "subscriber, arm-before-post)",
            build_id,
        )


def _build_recovery_reconcile_seam(
    sqlite_pool: SqliteLifecyclePersistence,
    forge_config: ForgeConfig,
) -> Any:
    """Return the production ``recovery_reconcile_on_boot`` closure (§D4.1).

    Binds :func:`forge.lifecycle.recovery.reconcile_on_boot` to a
    :class:`PipelineFailurePublisher` built from the boot client and a no-op
    :class:`ApprovalRepublisher` (PAUSED approval re-emit suppressed — rearm
    owns it). recovery.py stays unmodified.
    """

    async def _recovery_reconcile_on_boot(client: Any) -> None:
        from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
        from forge.lifecycle.recovery import reconcile_on_boot as _recovery_reconcile

        publisher, _emitter = build_publisher_and_emitter(
            client, config=forge_config.pipeline
        )
        report = await _recovery_reconcile(
            sqlite_pool, publisher, _NoopApprovalRepublisher()
        )
        logger.info(
            "forge-serve: recovery reconcile complete (interrupted=%d "
            "paused_suppressed=%d skipped=%d failures=%d)",
            report.interrupted_count,
            report.paused_reissued_count,
            report.skipped_count,
            len(report.failures),
        )

    return _recovery_reconcile_on_boot


def _build_consumer_reconcile_seam(
    sqlite_pool: SqliteLifecyclePersistence,
    forge_config: ForgeConfig,
    async_task_starter: AsyncTaskStarter | None,
) -> Any:
    """Return the production ``consumer_reconcile_on_boot`` closure (§D4.4).

    Binds :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot` with:

    * PAUSED scan SUPPRESSED — ``iter_paused_builds`` empty and the two re-emit
      fns are no-ops (rearm owns PAUSED; documented ownership boundary above).
    * redelivery drain DEFERRED — ``fetch_redeliveries`` returns an empty batch
      so no boot-time pull subscription collides with the daemon's own durable
      attach (single-consumer rule, err 10100). Crash-mid-hop INTERRUPTED rows
      heal on the first post-boot redelivery via ``dispatch_build``'s three-arm
      INTERRUPTED→redispatch. The Branch-2 collaborators are wired correctly so
      the machinery is honest and a future change may enable the boot drain.
    """

    async def _consumer_reconcile_on_boot(client: Any) -> None:
        from forge.adapters.nats.pipeline_consumer import (
            ReconcileDeps,
            reconcile_on_boot as _consumer_reconcile,
        )
        from forge.cli._serve_deps import build_pipeline_consumer_deps

        consumer_deps = build_pipeline_consumer_deps(
            client,
            forge_config,
            sqlite_pool,
            async_task_starter=async_task_starter,
        )

        async def _fetch_redeliveries() -> list[Any]:
            # Deferred to the live consumer + the third-arm redispatch (see the
            # closure docstring): a boot-time pull subscription would collide
            # with the daemon's durable attach (single-consumer rule).
            return []

        async def _read_build_state(feature_id: str, correlation_id: str) -> str | None:
            return _read_build_state_by_identity(
                sqlite_pool, feature_id=feature_id, correlation_id=correlation_id
            )

        async def _mark_interrupted_and_reset(
            feature_id: str, correlation_id: str
        ) -> None:
            _interrupt_and_reset_to_preparing(
                sqlite_pool, feature_id=feature_id, correlation_id=correlation_id
            )

        async def _empty_paused() -> list[Any]:
            return []  # PAUSED scan suppressed — rearm owns PAUSED.

        async def _noop_build_paused(_payload: Any) -> None:
            return None

        async def _noop_approval_request(_payload: Any, _subject: str) -> None:
            return None

        deps = ReconcileDeps(
            consumer_deps=consumer_deps,
            fetch_redeliveries=_fetch_redeliveries,
            read_build_state=_read_build_state,
            mark_interrupted_and_reset=_mark_interrupted_and_reset,
            iter_paused_builds=_empty_paused,
            publish_build_paused=_noop_build_paused,
            publish_approval_request=_noop_approval_request,
        )
        report = await _consumer_reconcile(deps)
        logger.info(
            "forge-serve: consumer reconcile complete (restarted=%d "
            "acked_terminal=%d paused_scan_suppressed=%d) — PAUSED owned by "
            "rearm_paused_gates; redelivery drain deferred to live consumer",
            report.restarted_in_flight,
            report.acked_terminal,
            report.paused_scan_re_emitted,
        )

    return _consumer_reconcile_on_boot


def _read_build_state_by_identity(
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> str | None:
    """Return the ``builds.status`` string for an identity, or ``None``."""
    from forge.lifecycle.state_machine import BuildState

    with sqlite_pool._reader() as cx:
        row = cx.execute(
            "SELECT status FROM builds WHERE feature_id = ? AND correlation_id = ?",
            (feature_id, correlation_id),
        ).fetchone()
    if row is None:
        return None
    raw = row[0] if not hasattr(row, "keys") else row["status"]
    return raw.value if isinstance(raw, BuildState) else raw


def _interrupt_and_reset_to_preparing(
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> None:
    """Mark an in-flight row INTERRUPTED then reset it to PREPARING.

    The Branch-2 ``MarkInterruptedAndReset`` collaborator. Composes both hops
    through :meth:`SqliteLifecyclePersistence.apply_transition` (single writer
    of ``builds.status``). Only exercised if a boot-time redelivery drain is
    ever enabled (see :func:`_build_consumer_reconcile_seam`).
    """
    from forge.lifecycle.persistence import Build
    from forge.lifecycle.state_machine import (
        BuildState,
        transition_chain,
    )

    with sqlite_pool._reader() as cx:
        row = cx.execute(
            "SELECT build_id, status FROM builds WHERE feature_id = ? "
            "AND correlation_id = ?",
            (feature_id, correlation_id),
        ).fetchone()
    if row is None:  # pragma: no cover - defensive
        return
    build_id = row[0] if not hasattr(row, "keys") else row["build_id"]
    raw = row[1] if not hasattr(row, "keys") else row["status"]
    current = raw if isinstance(raw, BuildState) else BuildState(raw)

    if current is not BuildState.INTERRUPTED:
        for hop in transition_chain(
            Build(build_id=build_id, status=current),
            BuildState.INTERRUPTED,
            error="crash-mid-hop: reset to PREPARING for redispatch",
        ):
            sqlite_pool.apply_transition(hop)
    for hop in transition_chain(
        Build(build_id=build_id, status=BuildState.INTERRUPTED),
        BuildState.PREPARING,
    ):
        sqlite_pool.apply_transition(hop)


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


def bind_production_serve(config: ServeConfig, forge_config: ForgeConfig) -> None:
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
    logger.info("forge-serve: applied %d SQLite migration(s) at boot", applied)

    # Step 3.5b (TASK-FORGE-FRR-PEBR-WIREUP) — apply the lifecycle-
    # bridge coexistence migration so the
    # ``lifecycle_bridge_terminal_publishes`` table required by
    # :class:`TerminalPublishLedger` exists at boot. Idempotent
    # (``CREATE TABLE IF NOT EXISTS``); the migration is co-located
    # with the bridge code rather than folded into the canonical
    # migration ladder so it travels with the consumer (see
    # ``coexistence.py:140-175`` for the rationale).
    _bridge_coexistence.apply_migration(connection)

    # Also apply the ``lifecycle_bridge_registry`` migration so the
    # registry table required by :class:`BridgeRegistry` exists at
    # boot (TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A). Without this, on
    # a fresh ``FORGE_DB_PATH`` volume the first call to
    # ``register_ack_handle`` raises ``no such table:
    # lifecycle_bridge_registry`` and the wireup silently falls back
    # to the legacy ``ack_callback`` ack-on-dispatch-return path —
    # exactly the redelivery-storm closure the bridge was built to
    # replace. Idempotent (``CREATE TABLE IF NOT EXISTS``); migration
    # is co-located with the bridge code for the same reason as the
    # coexistence migration above.
    _bridge_registry_migration.apply(connection)

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

    # Step 6.5 (TASK-FORGE-FRR-PEBR-WIREUP) — construct the
    # SQLite-bound dependencies for the lifecycle-bridge wireup. The
    # wireup itself is finalised inside the closure returned by
    # ``bind_production_dispatch_chain`` (where the publisher is in
    # scope); this step builds the parts that depend on the SQLite
    # pool / runner_url so they can be threaded through.
    bridge_wireup_parts = _build_lifecycle_bridge_wireup_parts(
        sqlite_pool=sqlite_pool,
        autobuild_runner_url=config.autobuild_runner_url,
    )

    # Step 7 — build the production composer and rebind the seam.
    composer = serve_module.bind_production_dispatch_chain(
        forge_config=forge_config,
        sqlite_pool=sqlite_pool,
        async_task_starter=async_task_starter,
        bridge_wireup_parts=bridge_wireup_parts,
    )
    serve_module.compose_dispatch_chain = composer

    # Step 7.5 (TASK-GATE-D659 §D4.1-2 / §D4.4 "Step 6.8") — bind BOTH boot
    # reconcile seams to production wiring so the receipt-only default no-op
    # stubs are unreachable at boot. See the module-level THREE-ACTOR
    # OWNERSHIP BOUNDARY note: recovery gets a no-op ApprovalRepublisher
    # (PAUSED approval re-emit suppressed) and the consumer twin seam runs
    # with its PAUSED scan suppressed (rearm owns PAUSED).
    serve_module.recovery_reconcile_on_boot = _build_recovery_reconcile_seam(
        sqlite_pool, forge_config
    )
    serve_module.consumer_reconcile_on_boot = _build_consumer_reconcile_seam(
        sqlite_pool, forge_config, async_task_starter
    )

    # Step 8 — close any previous binding's writer connection cleanly.
    previous = _bound_resources
    _bound_resources = _BoundResources(connection=connection)
    if previous is not None:
        _close_previous_connection_quietly(previous)

    logger.info(
        "forge-serve: production composer bound (db_path=%s)",
        config.db_path,
    )


__all__ = ["LifecycleBridgeWireupParts", "bind_production_serve"]
