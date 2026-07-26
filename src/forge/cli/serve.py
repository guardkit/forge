"""``forge serve`` — long-lived daemon subcommand (TASK-F009-001 + TASK-FW10-001).

This module is the public entry-point for the ``forge serve`` subcommand
introduced by FEAT-FORGE-009. It runs the JetStream consumer daemon and
the healthz HTTP readiness probe concurrently via ``asyncio.wait`` with
``FIRST_COMPLETED`` semantics — first task to return cancels the other,
so a daemon failure stops reporting healthy and a healthz failure stops
consuming.

TASK-FW10-001 wiring (Wave 1, foundation)
-----------------------------------------

1. ``_run_serve`` opens **one** NATS client via the daemon's
   :data:`forge.cli._serve_daemon.nats_connect` seam (ASSUM-011). The
   single client is shared with all downstream constructors — the
   dispatcher, the deps factory, the publisher, and the daemon's first
   attach — so the daemon's startup path contains exactly one
   ``nats.connect(...)`` call.
2. Both ``reconcile_on_boot`` routines run synchronously **before** the
   durable consumer is attached:

   - :func:`forge.lifecycle.recovery.reconcile_on_boot` reconciles
     non-terminal SQLite rows (PREPARING / RUNNING / PAUSED / FINALISING).
   - :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot`
     drains JetStream redeliveries against the SQLite truth.

   Both are exposed as module-level rebindable seams
   (:data:`recovery_reconcile_on_boot`, :data:`consumer_reconcile_on_boot`)
   so this task can wire the boot order without dragging in the full
   production deps graph (which is owned by later tasks). Tests rebind
   these to assert the ordering invariant.
3. After both routines complete, ``state.chain_ready`` flips True. The
   healthz endpoint reads this flag and returns 503 / ``chain_not_ready``
   until then (TASK-FW10-001 ASSUM-012; AC for healthz row 1).
4. The daemon and healthz coroutines are then started; the daemon
   receives the shared client via :func:`run_daemon`'s ``client``
   keyword, so it does **not** call ``nats.connect(...)`` on its first
   attach.

Re-exports
----------

The two integration-contract constants live in
:mod:`forge.cli._serve_config` but are also re-exported here so callers
can use the canonical import path documented in the acceptance
criteria::

    from forge.cli.serve import DEFAULT_HEALTHZ_PORT  # 8080
    from forge.cli.serve import DEFAULT_DURABLE_NAME  # "forge-serve"
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import click

from forge.adapters.nats.fleet_publisher import (
    AGENT_ID,
    deregister,
    register_on_boot,
)
from forge.cli import _serve_daemon
from forge.cli._serve_config import (
    DEFAULT_DURABLE_NAME,
    DEFAULT_HEALTHZ_PORT,
    ServeConfig,
)
from forge.cli._serve_daemon import run_daemon
from forge.cli._serve_dispatcher import make_handle_message_dispatcher
from forge.cli._serve_healthz import run_healthz_server
from forge.cli._serve_planning import (
    compose_planning_consumer_and_dispatch,
    rearm_paused_planning_runs,
    sweep_interrupted_planning_runs,
)
from forge.cli._serve_state import SubscriptionState
from forge.pipeline.dispatchers.autobuild_async import (
    AsyncTaskStarter,
    AutobuildStateInitialiser,
    StageLogRecorder,
    dispatch_autobuild_async,
)
from forge.pipeline.supervisor import Supervisor

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from datetime import datetime

    from forge.cli._serve_production import LifecycleBridgeWireupParts
    from forge.config.models import BudgetGuards
    from forge.pipeline import PipelineLifecycleEmitter
    from forge.pipeline.forward_context_builder import ForwardContextBuilder

logger = logging.getLogger(__name__)

# stdlib ``logging`` format chosen for daemon-grep readability across
# replicas: ISO-8601 timestamp, level, logger name, message. If the
# project ever moves to structlog/JSON, ``_configure_logging`` is the
# single swap point — keep that in mind before scattering more
# ``basicConfig`` calls.
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# Reconcile-on-boot seams (TASK-FW10-001)
# ---------------------------------------------------------------------------


ReconcileFn = Callable[[Any], Awaitable[None]]
"""``async (client: nats_client) -> None`` — boot-time reconciliation seam.

Receives the shared NATS client so the routine can construct its NATS-
side dependencies (publishers, redelivery readers) against the same
connection ``_run_serve`` opened. The default implementations are
no-ops; production wiring is filled in by later FW10 tasks. Tests
rebind these to assert ordering, deps sharing, and "ran before
attach".
"""


async def _default_recovery_reconcile_on_boot(client: Any) -> None:
    """Default no-op for the SQLite-side recovery reconcile.

    Production wiring (later FW10 task) constructs the persistence,
    publisher, and approval_publisher and calls
    :func:`forge.lifecycle.recovery.reconcile_on_boot`. Until that
    lands, the seam is a logged no-op so the boot order is observable
    without forcing an empty SQLite reconciliation pass at every
    process start.
    """
    logger.debug(
        "forge-serve: recovery_reconcile_on_boot seam not bound to "
        "production wiring (default no-op)"
    )


async def _default_consumer_reconcile_on_boot(client: Any) -> None:
    """Default no-op for the JetStream-side consumer reconcile.

    Production wiring (later FW10 task) constructs the
    :class:`forge.adapters.nats.pipeline_consumer.ReconcileDeps` and
    calls :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot`.
    The seam stays a logged no-op until then.
    """
    logger.debug(
        "forge-serve: consumer_reconcile_on_boot seam not bound to "
        "production wiring (default no-op)"
    )


#: Module-level rebindable seam: SQLite-side recovery reconcile.
recovery_reconcile_on_boot: ReconcileFn = _default_recovery_reconcile_on_boot

#: Module-level rebindable seam: JetStream-side consumer reconcile.
consumer_reconcile_on_boot: ReconcileFn = _default_consumer_reconcile_on_boot


# ---------------------------------------------------------------------------
# Compose-dispatch-chain seam (TASK-FW10-007)
# ---------------------------------------------------------------------------

ComposeDispatchChainFn = Callable[[Any], Awaitable[None]]
"""``async (client: nats_client) -> None`` — composes the orchestrator chain.

Bound by the production wiring (TASK-FW10-007 + TASK-FW10-008) to a
closure that opens the SQLite pool, calls
:func:`forge.cli._serve_deps.build_pipeline_consumer_deps`,
constructs the dispatcher via
:func:`forge.cli._serve_dispatcher.make_handle_message_dispatcher`,
and rebinds :data:`forge.cli._serve_daemon.dispatch_payload` to the
result. The default implementation is a logged no-op so the daemon
smoke tests (``TestServeCmdSmoke``) and the FW10-001 boot-order tests
keep working without a SQLite pool wiring.
"""


async def _default_compose_dispatch_chain(client: Any) -> None:
    """Default no-op for the dispatch-chain composer seam.

    Production wiring (``serve_cmd`` and ops scripts) rebinds this seam
    to a real composer that builds the
    :class:`PipelineConsumerDeps` and rebinds
    :data:`_serve_daemon.dispatch_payload`. Until that wiring runs the
    daemon falls back to the receipt-only ``_default_dispatch`` stub
    inside ``_serve_daemon`` — that stub still acks every message, so
    a misconfigured deployment can never wedge the JetStream queue
    even when the chain composer is missing.
    """
    logger.debug(
        "forge-serve: compose_dispatch_chain seam not bound to production "
        "wiring (default no-op); _serve_daemon.dispatch_payload remains the "
        "receipt-only stub"
    )


#: Module-level rebindable seam: orchestrator-chain composer.
compose_dispatch_chain: ComposeDispatchChainFn = _default_compose_dispatch_chain


# ---------------------------------------------------------------------------
# Fleet-client seam — opens the dedicated nats_core.NATSClient that
# ``register_on_boot`` / ``deregister`` need. The forge daemon's primary
# NATS handle comes from ``_serve_daemon.nats_connect`` and is a raw
# ``nats.aio.client.Client`` (no ``register_agent`` / ``deregister_agent``
# helpers). Rather than refactor every downstream consumer onto the
# higher-level wrapper, we open a second connection scoped exclusively
# to fleet lifecycle events. The connection cost is one extra TCP
# socket per ``forge serve`` process.
# ---------------------------------------------------------------------------


FleetClientOpenerFn = Callable[[str], Awaitable[Any]]
"""``async (nats_url: str) -> NATSClient`` — opens the fleet lifecycle client.

The default implementation constructs a ``nats_core.client.NATSClient``
with ``name='forge-serve-fleet'`` against the same broker as the daemon
client. Tests rebind this seam to return a stub so they do not need a
live broker.
"""


async def _default_open_fleet_client(nats_url: str) -> Any:
    """Open and connect the dedicated NATSClient used for fleet lifecycle.

    Args:
        nats_url: Broker URL — typically ``config.nats_url``.

    Returns:
        A connected :class:`nats_core.client.NATSClient` exposing
        ``register_agent`` and ``deregister_agent``.
    """
    from nats_core.client import NATSClient
    from nats_core.config import NATSConfig

    fleet_client = NATSClient(
        NATSConfig(url=nats_url, name="forge-serve-fleet"),
    )
    await fleet_client.connect()
    return fleet_client


#: Module-level rebindable seam: fleet lifecycle client opener.
open_fleet_client: FleetClientOpenerFn = _default_open_fleet_client


def bind_production_dispatch_chain(
    *,
    forge_config: Any,
    sqlite_pool: Any,
    async_task_starter: Any | None = None,
    bridge_wireup_parts: "LifecycleBridgeWireupParts | None" = None,
    db_path: Any | None = None,
    nats_url: str | None = None,
) -> ComposeDispatchChainFn:
    """Return a :data:`ComposeDispatchChainFn` bound to the production deps.

    This is the production wiring for the
    :data:`compose_dispatch_chain` seam (TASK-FW10-007). The returned
    closure:

    1. calls :func:`forge.cli._serve_deps.build_publisher_and_emitter`
       to construct the shared :class:`PipelinePublisher` against the
       NATS client (TASK-FORGE-FRR-PEBR-WIREUP — finalised here rather
       than inside ``build_pipeline_consumer_deps`` so the bridge
       wireup shares the same publisher instance);
    2. when ``bridge_wireup_parts`` is provided, finalises the
       :class:`LifecycleBridgeWireup` using that publisher and threads
       its ``register_ack_handle`` plus the parts'
       ``terminal_publish_ledger`` into
       :func:`forge.cli._serve_deps.build_pipeline_consumer_deps`
       (Gap PEBR-WIREUP — closes the ack-bridge / terminal-publish
       ledger composition that was previously deferred);
    3. wraps the resulting :class:`PipelineConsumerDeps` in a
       :func:`make_handle_message_dispatcher` closure; and
    4. rebinds :data:`_serve_daemon.dispatch_payload` to the
       dispatcher before returning. After this returns the
       receipt-only ``_default_dispatch`` stub is no longer reachable
       on the production code path (TASK-FW10-007 AC: "receipt-only
       stub no longer reachable").

    Args:
        forge_config: Validated :class:`ForgeConfig` shared with the
            consumer's allowlist / approved_originators rejection
            rules.
        sqlite_pool: The shared
            :class:`SqliteLifecyclePersistence` facade.
        async_task_starter: Optional
            :class:`AsyncTaskStarter`. Production wiring is provided
            by TASK-FW10-008 via the
            :class:`AsyncSubAgentMiddleware` tool surface; tests pass
            a deterministic fake.
        bridge_wireup_parts: Optional
            :class:`~forge.cli._serve_production.LifecycleBridgeWireupParts`
            carrying the SQLite-bound dependencies for the lifecycle
            bridge wireup (bridge, translator, stream_source,
            identity_provider, terminal_publish_ledger). Production
            wiring (``bind_production_serve``) constructs the parts in
            its Step 6.5 and threads them in. When ``None`` (legacy
            unit-test / smoke-test path), the deps composer logs
            ``ack_bridge=deferred (TASK-FRR-PEB-002)`` and
            ``terminal_publish_ledger=deferred (TASK-FRR-PEB-005)`` —
            the pre-PEBR-WIREUP behaviour, preserved for backwards
            compatibility.

    Returns:
        An ``async (client) -> None`` closure suitable for assignment
        to :data:`compose_dispatch_chain`.
    """

    from forge.cli import _serve_deps_gating, _serve_gate_activation
    from forge.cli._serve_deps import (
        build_pipeline_consumer_deps,
        build_serve_resume_launcher,
    )
    from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
    from forge.gating.sqlite_adapters import build_sqlite_gate_adapters

    def _gate_wall_clock() -> Any:
        """Composition-root wall clock for the TASK-GATE-D659 gate.

        The single injected ``() -> datetime`` seam shared by the gate's
        SQLite adapters (``stage_log`` timestamps) and the mirrored
        publisher's ``build-paused`` ``paused_at`` — clock hygiene keeps
        wall-clock reads at this one boot seam.
        """
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    async def _compose(client: Any) -> None:
        # TASK-FORGE-FRR-PEBR-WIREUP — hoist publisher construction up
        # to the closure so the bridge wireup can share the same
        # publisher instance as ``publish_build_failed`` (no-double-
        # emit invariant). The emitter is rebuilt against the same
        # publisher inside ``build_pipeline_consumer_deps``.
        publisher, emitter = build_publisher_and_emitter(
            client, config=forge_config.pipeline
        )

        register_ack_handle: Any = None
        terminal_publish_ledger: Any = None
        if bridge_wireup_parts is not None:
            from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup

            wireup = LifecycleBridgeWireup(
                bridge=bridge_wireup_parts.bridge,
                translator=bridge_wireup_parts.translator,
                publisher=publisher,
                stream_source=bridge_wireup_parts.stream_source,
                identity_provider=bridge_wireup_parts.identity_provider,
                run_state_fetcher=bridge_wireup_parts.run_state_fetcher,
                build_state_recorder=bridge_wireup_parts.build_state_recorder,
                build_id_resolver=bridge_wireup_parts.build_id_resolver,
            )
            register_ack_handle = wireup.register_ack_handle
            terminal_publish_ledger = bridge_wireup_parts.terminal_publish_ledger

        # TASK-JNB-101 — construct the approval-gate collaborators
        # against the same shared client + emitter and anchor them
        # module-level. This is the production injection seam for
        # ``GateCheckDeps.subscriber`` (the four-step reply-validation
        # chain); ``expected_approver`` comes from forge.yaml
        # ``approval.expected_approver`` (pinned 'rich' — must equal
        # jarvis JARVIS_SLACK_DECIDED_BY verbatim). Soft-fail: a v1.1
        # approval-wiring defect must never brick v1 dispatch boot —
        # the ERROR log line is the operator's probe (TASK-JNB-107).
        # TASK-GATE-D659 — the SQLite gate adapters (repository + state
        # machine) compose the tested lifecycle facades so the live
        # ``gate_check`` path runs against the real DB. Built once per boot
        # (they share a build-keyed pause handoff) and threaded into the
        # dispatch closure so ``maybe_gate_build`` can pause / resume the
        # real builds row.
        gate_repository, gate_state_machine = build_sqlite_gate_adapters(
            sqlite_pool, clock=_gate_wall_clock
        )

        gate_parts: Any = None
        try:
            gate_parts = _serve_deps_gating.bind_gate_parts(
                _serve_deps_gating.build_approval_gate_parts(
                    client,
                    forge_config,
                    emitter=emitter,
                    # TASK-GATE-D659 R1 — static exactly-one-resume-emit
                    # owner: the daemon subscriber owns the build-resumed
                    # emit. A paused build never creates a bridge registry
                    # row (the observer is registered only AFTER approval),
                    # so the subscriber's PEB-006 resume-emit suppression
                    # probe MUST stay disabled — pass ``bridge_registry=None``
                    # unconditionally. Reintroduce a non-None registry only
                    # once a runner-side activation point can pre-create the
                    # registry row before the pause (plan §Future).
                    bridge_registry=None,
                    # WS3-S6 (2026-07-09) — refresh-on-timeout WIRED LIVE.
                    # D659 shipped the real SQLite ``gate_repository`` at
                    # ``build_sqlite_gate_adapters`` above; thread it into the
                    # parts so the subscriber's API §7 refresh loop is live.
                    # Without it ``publish_refresh`` stayed None and a pause
                    # gave up after ONE ``default_wait_seconds`` window (300s)
                    # instead of waiting the full ``max_wait_seconds`` (1h) —
                    # single-window pauses silently expired well before the
                    # operator's phone round-trip and before the 1h JetStream
                    # ack_wait (FWD-003). With it, the subscriber re-derives
                    # + persists + re-publishes the current ``request_id`` each
                    # window up to ``max_wait_seconds``; boot recovery re-emits
                    # the persisted (refreshed) id. The refresh re-publish uses
                    # the raw approval publisher (no build-paused mirror), which
                    # is correct: jarvis JNB-103 joins request→paused on
                    # ``build_id`` (not ``request_id``), so the original pause's
                    # build-paused keeps the prompt rendered while the refresh
                    # only supersedes the request envelope + extends forge's
                    # wait. The prior "keep disabled for v1" note is superseded.
                    repository=gate_repository,
                )
            )
        except Exception as exc:  # noqa: BLE001 — DDR-007 boot protection
            logger.error(
                "forge-serve: approval gate parts construction FAILED "
                "(%s) — daemon continues WITHOUT the approval reply "
                "seam; phone approvals will not resolve until fixed",
                exc,
            )

        # TASK-GATE-D659 §D4.2 — re-arm every PAUSED build's approval
        # round-trip. Spawned AFTER bind_gate_parts so a LIVE response
        # subscriber exists before ANY PAUSED approval is re-emitted
        # (arm-before-post; closes the C1 boot-order tap-drop). rearm owns
        # BOTH PAUSED re-emits; the two boot reconcile seams suppress theirs.
        # Soft-fail: a rearm defect must never brick v1 dispatch boot.
        if gate_parts is not None:
            try:
                resume_launcher = build_serve_resume_launcher(
                    sqlite_pool,
                    forge_config,
                    lifecycle_emitter=emitter,
                    async_task_starter=async_task_starter,
                )
                await _serve_gate_activation.rearm_paused_gates(
                    parts=gate_parts,
                    sqlite_pool=sqlite_pool,
                    gate_repository=gate_repository,
                    gate_state_machine=gate_state_machine,
                    resume_launcher=resume_launcher,
                    client=client,
                    clock=_gate_wall_clock,
                )
            except Exception as exc:  # noqa: BLE001 — DDR-007 boot protection
                logger.error(
                    "forge-serve: rearm_paused_gates FAILED (%s) — paused "
                    "builds were NOT re-armed this boot; they stay PAUSED "
                    "until the next restart",
                    exc,
                )

        # TASK-MP-011/TASK-MP-012 — wire Mode P planning composition into
        # serve boot. Guarded by config.planning.enabled to keep the default
        # path zero-cost. Soft-fail: planning composition failure never
        # bricks daemon boot (DDR-007). Mirrors the gate-parts posture above.
        # Call contract pinned by tests/cli/test_serve_planning_wiring.py
        # with SIGNATURE-BINDING fakes — a kwargs drift here fails CI
        # (the TASK-MP-011 permissive-fake gap, closed).
        if forge_config.planning.enabled:
            try:
                if db_path is None:
                    raise RuntimeError(
                        "planning.enabled=true but no db_path was threaded "
                        "into bind_production_dispatch_chain — planning "
                        "cannot compose"
                    )
                planning_composition = await compose_planning_consumer_and_dispatch(
                    db_path=db_path,
                    nats_client=client,
                    config=forge_config,
                    nats_url=nats_url,
                )
                planning_dispatch = (
                    planning_composition.dispatch_callable
                    if planning_composition is not None
                    else None
                )
                await sweep_interrupted_planning_runs(
                    db_path,
                    dispatch_callable=planning_dispatch,
                )
                await rearm_paused_planning_runs(
                    db_path,
                    client,
                    forge_config.planning,
                    composition=planning_composition,
                )
            except Exception as exc:  # noqa: BLE001 — DDR-007 boot protection
                logger.error(
                    "forge-serve: planning composition FAILED (%s) — daemon "
                    "continues WITHOUT planning consumer; Mode P will not "
                    "process planning-queued messages until fixed",
                    exc,
                )

        # Lane C1a — wire the WS2-B8 deploy-stage runner into serve boot,
        # gated on config.deploy.enabled (the flag's first production reader).
        # Flag OFF (default) = byte-for-byte no-op: nothing is composed and
        # _serve_daemon.deploy_stage_runner stays None (DEPLOY / LIVE_GATE
        # inert). Flag ON composes the runner from the shared client + forge
        # DB and stashes it for the post-review deploy trigger (Lane C4).
        # DDR-007 boot protection: a composition failure never bricks daemon
        # boot — the deploy stage stays inert and the ERROR log is the probe.
        try:
            from forge.cli._serve_deploy import compose_deploy_stage_runner

            _serve_daemon.deploy_stage_runner = compose_deploy_stage_runner(
                forge_config=forge_config,
                nats_client=client,
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001 — DDR-007 boot protection
            _serve_daemon.deploy_stage_runner = None
            logger.error(
                "forge-serve: deploy-stage composition FAILED (%s) — the "
                "deploy stage stays INERT (DEPLOY / LIVE_GATE will not "
                "dispatch) until fixed",
                exc,
            )

        deps = build_pipeline_consumer_deps(
            client,
            forge_config,
            sqlite_pool,
            async_task_starter=async_task_starter,
            register_ack_handle=register_ack_handle,
            terminal_publish_ledger=terminal_publish_ledger,
            publisher=publisher,
            gate_repository=gate_repository,
            gate_state_machine=gate_state_machine,
            gate_clock=_gate_wall_clock,
        )
        dispatcher = make_handle_message_dispatcher(deps)
        # Rebind the daemon's dispatch seam BEFORE the consumer's first
        # fetch. After this assignment the receipt-only
        # ``_default_dispatch`` stub is no longer reachable on the
        # production code path (TASK-FW10-007 AC). The daemon's
        # ``_process_message`` reads ``dispatch_payload`` per call, so
        # the rebind takes effect on the very next pulled message.
        _serve_daemon.dispatch_payload = dispatcher
        logger.info(
            "forge-serve: dispatch chain composed; "
            "_serve_daemon.dispatch_payload rebound to handle_message dispatcher "
            "(receipt-only stub no longer reachable)"
        )

    return _compose


# ---------------------------------------------------------------------------
# Supervisor construction (TASK-FW10-008)
# ---------------------------------------------------------------------------


def _build_async_subagent_middleware(*, autobuild_runner_url: str | None = None) -> Any:
    """Return a configured :class:`AsyncSubAgentMiddleware` for autobuild.

    The middleware exposes the five tools (``start_async_task``,
    ``check_async_task``, ``update_async_task``, ``cancel_async_task``,
    ``list_async_tasks``) the supervisor's reasoning loop uses to
    dispatch the autobuild stage as an :class:`AsyncSubAgent` per
    ADR-ARCH-031. The ``graph_id`` is the
    :data:`forge.pipeline.dispatchers.autobuild_async.AUTOBUILD_RUNNER_NAME`
    constant — the same name TASK-FW10-002 registers under
    ``langgraph.json`` — so the middleware addresses the production
    runner's compiled graph.

    The factory imports ``deepagents`` at call time rather than at
    module import so :mod:`forge.cli.serve` stays importable in
    environments that do not have DeepAgents installed (the BDD oracle,
    static lint runners, etc.).

    Args:
        autobuild_runner_url: URL of the ``langgraph-runner`` sidecar
            serving the autobuild_runner graph (TASK-FORGE-FRR-F010I/J).
            When provided, the ``AsyncSubAgent`` registration includes
            ``url=<url>`` so deepagents'
            ``_ClientCache.get_async()`` constructs an
            ``httpx.AsyncClient`` with a real URL transport (the path
            this whole component was designed for). When ``None`` /
            empty (default), the ``url`` key is omitted from the spec
            so the in-process ASGI fallback applies — that path raises
            ``'NoneType' object is not callable`` on every dispatch in
            the current forge venv (``langgraph_api`` is not installed),
            so production callers MUST pass the URL or
            :func:`forge.cli._serve_production.bind_production_serve`
            will fail-fast at boot. The ``None`` default is preserved
            so non-production callers (BDD oracle, lint runners) can
            still construct the middleware without the env var being
            set.
    """
    from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

    from forge.pipeline.dispatchers.autobuild_async import (
        AUTOBUILD_RUNNER_NAME,
    )

    spec: dict[str, Any] = {
        "name": AUTOBUILD_RUNNER_NAME,
        "description": (
            "Long-running autobuild stage runner (FEAT-FORGE-005, "
            "ADR-ARCH-031). The supervisor dispatches a feature's "
            "autobuild via start_async_task and tracks lifecycle "
            "transitions through the async_tasks state channel."
        ),
        "graph_id": AUTOBUILD_RUNNER_NAME,
    }
    # Truthy check: empty-string values
    # (``FORGE_AUTOBUILD_RUNNER_URL=``) are treated as "no URL" so a
    # misconfigured deploy doesn't register a broken empty URL on the
    # spec.
    if autobuild_runner_url:
        spec["url"] = autobuild_runner_url

    return AsyncSubAgentMiddleware(async_subagents=[spec])


def _make_autobuild_dispatcher_closure(
    *,
    forward_context_builder: ForwardContextBuilder,
    async_task_starter: AsyncTaskStarter,
    stage_log_recorder: StageLogRecorder,
    state_channel: AutobuildStateInitialiser,
    lifecycle_emitter: PipelineLifecycleEmitter,
) -> Callable[..., Any]:
    """Return the supervisor-shaped autobuild dispatcher closure.

    The supervisor calls ``await self.autobuild_dispatcher(build_id=...,
    feature_id=..., rationale=...)`` (see ``Supervisor._dispatch``);
    this closure pre-binds the four wave-2 collaborators
    (TASK-FW10-003/004/005) plus the wave-2 lifecycle emitter
    (TASK-FW10-006) so dispatch-time only needs the per-turn identifiers.
    The ``rationale`` arg is accepted but not threaded into
    :func:`dispatch_autobuild_async` because the autobuild dispatcher
    persists rationale on the per-turn supervisor row, not on the
    per-dispatch ``stage_log`` row.

    The closure feeds ``correlation_id=feature_id`` as a placeholder
    until the eventual TASK-FW10-007 deps factory threads the real
    envelope ``correlation_id`` through. That is sufficient to satisfy
    the FEAT-FORGE-007 Group I @data-integrity check in unit tests; the
    cross-feature integration tests assert the production correlation
    propagation path.

    TASK-FORGE-FRR-F010G: the closure is ``async def`` because
    :func:`dispatch_autobuild_async` is now async — the launch path
    awaits ``StructuredTool.coroutine`` so the autobuild_runner
    registration can stay URL-less (in-process ASGI transport).
    """

    async def dispatcher(
        *,
        build_id: str,
        feature_id: str,
        rationale: str = "",
    ) -> Any:
        return await dispatch_autobuild_async(
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=feature_id,
            forward_context_builder=forward_context_builder,
            async_task_starter=async_task_starter,
            stage_log_recorder=stage_log_recorder,
            state_channel=state_channel,
            lifecycle_emitter=lifecycle_emitter,
        )

    # Tag the closure for diagnostics so test assertions can recover
    # the bound emitter instance without recursing into closure cells.
    dispatcher.__wrapped_emitter__ = lifecycle_emitter  # type: ignore[attr-defined]
    return dispatcher


def build_supervisor(
    *,
    forward_context_builder: ForwardContextBuilder,
    async_task_starter: AsyncTaskStarter,
    stage_log_recorder: StageLogRecorder,
    state_channel: AutobuildStateInitialiser,
    lifecycle_emitter: PipelineLifecycleEmitter,
    ordering_guard: Any,
    per_feature_sequencer: Any,
    constitutional_guard: Any,
    state_reader: Any,
    ordering_stage_log_reader: Any,
    per_feature_stage_log_reader: Any,
    async_task_reader: Any,
    reasoning_model: Any,
    turn_recorder: Any,
    specialist_dispatcher: Callable[..., Awaitable[Any]],
    subprocess_dispatcher: Callable[..., Awaitable[Any]],
    pr_review_gate: Any,
    async_subagent_middleware: Any | None = None,
    budget_guards: "BudgetGuards | None" = None,
    budget_profile_name: str = "attended",
    budget_wall_clock: "Callable[[], datetime] | None" = None,
    budget_started_at_reader: "Callable[[str], datetime | None] | None" = None,
    budget_coach_score_reader: "Callable[[str], float | None] | None" = None,
    budget_pause: "Callable[..., Awaitable[None]] | None" = None,
) -> Supervisor:
    """Construct the production :class:`Supervisor` for ``_run_serve``.

    TASK-FW10-008 — wires the supervisor with:

    * The four wave-2 collaborators (TASK-FW10-003/004/005) plus the
      :class:`PipelineLifecycleEmitter` (TASK-FW10-006). The emitter is
      pre-bound into the autobuild dispatcher closure so
      ``dispatch_autobuild_async`` threads it onto
      ``ctx['lifecycle_emitter']`` (DDR-007 Option A) — the autobuild
      runner subagent reads it back and calls
      ``emitter.on_transition(state)`` from its ``_update_state``
      helper.
    * The :class:`AsyncSubAgentMiddleware` ``start_async_task`` /
      ``check_async_task`` / ``update_async_task`` /
      ``cancel_async_task`` / ``list_async_tasks`` tool surface so the
      reasoning loop stays responsive while autobuild executes in the
      background. The middleware's ``tools`` attribute is exposed on
      the returned :class:`Supervisor` via the
      :attr:`Supervisor.tools` field — the supervisor itself does not
      invoke the tools; it forwards them to the reasoning model wiring
      on the LangGraph side.

    AC-005 invariant: ``dispatch_autobuild_async`` is called with
    exactly five collaborator parameters
    (``forward_context_builder``, ``async_task_starter``,
    ``stage_log_recorder``, ``state_channel``, ``lifecycle_emitter``).

    AC-004 invariant: only one :class:`PipelineLifecycleEmitter` is
    constructed per ``_run_serve`` invocation; this factory does not
    construct a second one — it accepts the emitter as a parameter and
    threads it into both the supervisor field and the dispatcher
    closure.

    FEAT-UBS-002 budget DI (``budget_guards`` / ``budget_profile_name`` /
    ``budget_wall_clock`` / ``budget_started_at_reader`` /
    ``budget_coach_score_reader`` / ``budget_pause``): pass-through kwargs
    threaded onto the :class:`Supervisor`'s budget fields. Each defaults to
    the Supervisor dataclass default (an ``attended`` caps-off /
    unwired-collaborator profile), so every existing caller is unchanged and
    the guard stays a strict no-op (ASSUM-010). These are wired so the
    resolved caps + pause collaborator have a home, but they are **inert
    until activation**: the Mode-C ``next_turn`` enforcement loop that would
    consume them has no production driver today (``build_mode_reader`` is
    ``None`` in production and the Supervisor/Mode-C path has no production
    caller). Activation — wiring these to a next_turn driver — is a
    plan-of-record decision reserved for Rich, out of scope here. The
    serve-side helpers :func:`resolve_budget_for_build`,
    :func:`make_budget_started_at_reader`, :func:`budget_wall_clock`, and
    :func:`make_budget_pause` build the production values a future driver
    would pass here.
    """
    middleware = (
        async_subagent_middleware
        if async_subagent_middleware is not None
        else _build_async_subagent_middleware()
    )
    autobuild_dispatcher = _make_autobuild_dispatcher_closure(
        forward_context_builder=forward_context_builder,
        async_task_starter=async_task_starter,
        stage_log_recorder=stage_log_recorder,
        state_channel=state_channel,
        lifecycle_emitter=lifecycle_emitter,
    )
    return Supervisor(
        ordering_guard=ordering_guard,
        per_feature_sequencer=per_feature_sequencer,
        constitutional_guard=constitutional_guard,
        state_reader=state_reader,
        ordering_stage_log_reader=ordering_stage_log_reader,
        per_feature_stage_log_reader=per_feature_stage_log_reader,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=turn_recorder,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        tools=tuple(getattr(middleware, "tools", ()) or ()),
        lifecycle_emitter=lifecycle_emitter,
        budget_guards=budget_guards,
        budget_profile_name=budget_profile_name,
        budget_wall_clock=budget_wall_clock,
        budget_started_at_reader=budget_started_at_reader,
        budget_coach_score_reader=budget_coach_score_reader,
        budget_pause=budget_pause,
    )


# ---------------------------------------------------------------------------
# FEAT-UBS-002 — production budget wiring (INERT until activation)
# ---------------------------------------------------------------------------
#
# The functions below build the production values a Mode-C ``next_turn`` driver
# would pass to :func:`build_supervisor`'s budget DI. They are complete and
# tested, but nothing consumes them yet: the Supervisor/Mode-C path has no
# production caller (the daemon drives builds via the NATS pipeline consumer)
# and ``build_mode_reader`` is ``None`` in production, so every build is Mode A.
# Activation — wiring a ``build_mode_reader`` + a next_turn driver — is a
# plan-of-record decision reserved for Rich, explicitly out of scope here.


def resolve_budget_for_build(
    pool: Any,
    config: Any,
    build_id: str,
) -> "tuple[BudgetGuards, str]":
    """Resolve the :class:`BudgetGuards` + profile name for ``build_id``.

    Reads ``builds.profile`` off the persisted row (written by
    ``forge queue --profile`` — schema_v5.sql) and resolves the caps via
    ``config.budget.resolve(profile)`` (``models.py``): a ``NULL`` profile maps
    to ``config.budget.default_profile`` (``attended`` — caps off, ASSUM-010).
    A missing row is treated the same as a ``NULL`` profile.

    This is the FIRST production ``BudgetConfig.resolve`` call — until now the
    only caller was ``forge queue`` at CLI echo time (queue.py). It is consumed
    by the future Mode-C ``next_turn`` driver at per-build Supervisor setup;
    inert until that activation lands (a plan-of-record decision).

    Returns:
        ``(guards, resolved_profile_name)`` where ``resolved_profile_name`` is
        the profile the caps came from (the row's profile, or the config
        default when the row carries none).
    """
    row = pool.get_build_row(build_id)
    profile_name = row.profile if row is not None else None
    guards = config.budget.resolve(profile_name)
    resolved_name = (
        profile_name if profile_name is not None else config.budget.default_profile
    )
    return guards, resolved_name


def make_budget_started_at_reader(
    pool: Any,
) -> "Callable[[str], datetime | None]":
    """Build the production ``budget_started_at_reader`` for the wall-clock cap.

    Returns a ``(build_id) -> datetime | None`` reader over
    ``builds.started_at`` (schema.sql:37 — written on the PREPARING/RUNNING
    transition; there is no ``created_at`` column). ``None`` when the row is
    absent or has not started yet, which the Supervisor treats as ``0.0``
    elapsed (the wall-clock cap is fail-open, never a false pause).

    Inert until activation: no production caller wires this reader onto a live
    Supervisor today (the Mode-C path has no production driver).
    """

    def _reader(build_id: str) -> "datetime | None":
        row = pool.get_build_row(build_id)
        return row.started_at if row is not None else None

    return _reader


def budget_wall_clock() -> "datetime":
    """Production ``budget_wall_clock`` — the current UTC time.

    Paired with :func:`make_budget_started_at_reader` to measure a build's
    elapsed wall-clock for the ``max_build_wallclock_seconds`` cap. Absent
    either collaborator the Supervisor treats elapsed as ``0.0`` (fail-open).

    Inert until activation: no production caller wires this onto a live
    Supervisor today.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def make_budget_pause(
    pool: Any,
    publish_approval_request: "Callable[[Any, str], Awaitable[None]]",
    lifecycle_emitter: "PipelineLifecycleEmitter",
) -> "Callable[..., Awaitable[None]]":
    """Build the production ``budget_pause`` collaborator the Supervisor awaits.

    Returns the ``async (build_id, feature_id, payload, verdict, metrics) ->
    None`` callback (supervisor.py budget-pause shape). On a budget breach the
    Supervisor awaits it to publish the escalation and pause the build, in the
    order fixed by ADR-ARCH-021:

    1. **Publish** the risk=high :class:`ApprovalRequestPayload` on
       ``agents.approval.forge.{build_id}`` via ``publish_approval_request``
       (the daemon's re-emit fn shape — pipeline_consumer.py).
    2. **Mark PAUSED** in SQLite via ``pool.mark_paused(build_id, request_id)``
       (persistence.py — an optimistic, status-guarded RUNNING → PAUSED that
       atomically records ``pending_approval_request_id``). ``request_id`` is
       the deterministic ``budget-{build_id}-{review_cycles}`` the Supervisor
       stamped on the payload, so a re-fire is idempotent.
    3. **Emit** the ``build-paused`` lifecycle event via
       ``lifecycle_emitter.emit_paused`` (the PLAIN emit — **not**
       ``emit_paused_then_interrupt``, which awaits a LangGraph ``interrupt()``
       callable this daemon path does not hold; the resume wire is owned by the
       NATS approval subscriber, not by this closure).

    Idempotency / non-fighting with ``cli/cancel.py`` is guaranteed *outside*
    this closure: the Supervisor's already-PAUSED short-circuit means an
    already-paused build never reaches here, and the deterministic
    ``request_id`` makes a re-publish a no-op for idempotent responders. This
    closure's own responsibility is to degrade loudly on a *race* — if
    ``mark_paused`` raises :class:`InvalidTransitionError` (the row left a
    pausable state between the Supervisor's read and this write, e.g. a
    concurrent terminal transition or a cancel), it logs at ERROR and returns
    without emitting a ``build-paused`` for a row that is not PAUSED. It never
    crashes the turn. The approval was already published (idempotent by
    ``request_id``), consistent with the ADR's double-emit tolerance.

    Inert until activation: no production caller wires this onto a live
    Supervisor today (the Mode-C path has no production driver). Activation is a
    plan-of-record decision reserved for Rich.
    """

    async def budget_pause(
        *,
        build_id: str,
        feature_id: str,
        payload: Any,
        verdict: Any,
        metrics: Any,
    ) -> None:
        from datetime import datetime, timezone

        from forge.adapters.nats.approval_publisher import (
            AGENT_ID as _APPROVAL_AGENT_ID,
        )
        from forge.adapters.nats.approval_publisher import (
            APPROVAL_SUBJECT_TEMPLATE,
        )
        from forge.lifecycle.state_machine import InvalidTransitionError
        from forge.pipeline import BuildContext
        from nats_core.topics import Topics

        approval_subject = Topics.resolve(
            APPROVAL_SUBJECT_TEMPLATE,
            agent_id=_APPROVAL_AGENT_ID,
            task_id=build_id,
        )
        request_id = payload.request_id

        # ADR-ARCH-021 step 1: publish the risk=high approval request.
        await publish_approval_request(payload, approval_subject)

        # ADR-ARCH-021 step 2: mark the SQLite row PAUSED (status-guarded).
        try:
            pool.mark_paused(build_id, request_id)
        except InvalidTransitionError as exc:
            # Race: the row left a pausable state between the Supervisor's read
            # and this write. Degrade loudly — the approval is already
            # published (idempotent by request_id) — and skip the build-paused
            # emit for a row that is NOT PAUSED. Never crash the turn.
            logger.error(
                "budget_pause: mark_paused(%s) rejected (%s); the row is not "
                "in a pausable state (concurrent terminal/cancel). Approval "
                "already published on %s; skipping build-paused emit (UBS-002)",
                build_id,
                exc,
                approval_subject,
            )
            return

        # ADR-ARCH-021 step 3: emit the build-paused lifecycle event (PLAIN).
        row = pool.get_build_row(build_id)
        correlation_id = row.correlation_id if row is not None else feature_id
        ctx = BuildContext(
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=correlation_id,
            # wave_total is not consumed by emit_paused; a budget pause is not
            # wave-scoped, so 0 is an honest placeholder here.
            wave_total=0,
        )
        await lifecycle_emitter.emit_paused(
            ctx,
            stage_label=f"budget:{verdict.breached_cap}",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            coach_score=metrics.last_coach_score,
            rationale=verdict.detail,
            approval_subject=approval_subject,
            paused_at=datetime.now(timezone.utc).isoformat(),
        )

    return budget_pause


def _configure_logging(level_name: str) -> None:
    """Attach a stderr handler honouring ``FORGE_LOG_LEVEL``.

    TASK-FORGE-FRR-002. Before this call, every ``logger.info(...)``
    inside ``_serve_daemon`` and ``_serve_healthz`` was silently
    dropped at INFO and below because the root logger had no handler
    — see the 2026-05-01 GB10 first-real-run where ``docker logs
    forge-prod`` was empty despite a successful consume + ack.

    An unrecognised value (``FORGE_LOG_LEVEL=banana``) does not crash
    the daemon: it falls back to INFO with a one-line stderr warning
    so an obvious operator typo never blocks startup.

    ``logging.basicConfig`` is invoked with ``force=False`` (the
    default), which makes re-entrant calls in the same process a
    no-op. Tests that invoke ``serve_cmd`` more than once therefore
    do not pile up duplicate handlers on the root logger.
    """
    resolved = getattr(logging, level_name.upper(), None)
    if not isinstance(resolved, int):
        sys.stderr.write(
            f"unrecognised FORGE_LOG_LEVEL={level_name!r}, defaulting to INFO\n"
        )
        resolved = logging.INFO
    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATEFMT,
        stream=sys.stderr,
    )


async def _close_client_quietly(client: Any) -> None:
    """Close a NATS client, swallowing close errors.

    The shared client lifecycle straddles three coroutines (recovery
    reconcile, consumer reconcile, run_daemon). If any of them already
    closed the client, the second close raises an ``IOError`` /
    ``InvalidStateError`` that we do not want to surface — the process
    is already shutting down.
    """
    if client is None:
        return
    try:
        await asyncio.wait_for(
            client.close(),
            timeout=_serve_daemon.SHUTDOWN_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        logger.debug("forge-serve: shared client close error (%s)", exc)


async def _run_serve(config: ServeConfig, state: SubscriptionState) -> None:
    """Open one NATS client, run reconcile_on_boot, then daemon + healthz.

    TASK-FW10-001 boot order (load-bearing — see §5 of
    IMPLEMENTATION-GUIDE.md):

    1. ``nats_connect(config.nats_url)`` — exactly one connect call on
       the startup path (AC-006). All downstream collaborators share
       this client.
    1.5. ``register_on_boot(client)`` — publish :data:`FORGE_MANIFEST`
       to ``fleet.register`` + ``agent-registry`` KV so the fleet
       supervisor sees forge before any pipeline event is consumed.
       Runs before the reconciles so a registry watcher fired by the
       new entry cannot race the consumer attach in a way that hides
       forge from the fleet view.
    2. ``recovery_reconcile_on_boot(client)`` — SQLite-side recovery
       (PREPARING / RUNNING / PAUSED / FINALISING reconciliation).
    3. ``consumer_reconcile_on_boot(client)`` — JetStream-side redelivery
       reconciliation against the SQLite truth.
    4. ``state.set_chain_ready(True)`` — healthz now reports based on the
       composite gate (live AND chain_ready).
    5. Schedule ``run_daemon(config, state, client=client)`` and
       ``run_healthz_server(config, state)``; first to complete cancels
       the other.

    On teardown, the ``finally`` block calls ``deregister(client,
    reason="shutdown")`` before closing the client so the agent-registry
    KV reflects the shutdown without waiting for staleness eviction.
    ``deregister`` is idempotent and swallows transport errors.

    The daemon receives the shared client so its **first** attach does
    not call ``nats.connect(...)`` (the AC restricts the startup path
    to one connect). Reconnects after a broker drop still open a fresh
    client through the daemon's :data:`_serve_daemon.nats_connect` seam
    — the AC scopes "no second connect" to startup, not to
    runtime-reconnect.

    Args:
        config: Validated :class:`ServeConfig`. Source of NATS URL,
            healthz port, and durable name.
        state: Shared :class:`SubscriptionState`. ``chain_ready`` is
            flipped here; ``live`` is flipped by the daemon. Both are
            read by the healthz handler.
    """
    client: Any = await _serve_daemon.nats_connect(config.nats_url)
    # Open a dedicated NATSClient wrapper for fleet lifecycle ops. The
    # daemon's ``client`` above is the raw ``nats.aio.client.Client`` and
    # does not expose ``register_agent`` / ``deregister_agent``. The
    # fleet client is short-lived from the daemon's perspective — only
    # the register + deregister calls go through it, and it is closed
    # in the finally block alongside the daemon client.
    fleet_client: Any = await open_fleet_client(config.nats_url)
    try:
        # Step 1.5 — fleet self-registration. Publishes FORGE_MANIFEST
        # to ``fleet.register`` and stores it in the ``agent-registry``
        # KV bucket so the fleet supervisor (jarvis) sees forge in its
        # capability index. Registration runs BEFORE the reconciles so
        # any registry watcher that fires on the new entry can race
        # the consumer attach without missing forge's manifest. Per
        # the fleet_publisher docstring, a publish failure here is
        # fatal — the daemon should never start "invisible" to the
        # fleet supervisor.
        await register_on_boot(fleet_client)
        logger.info("forge-serve: fleet registration published agent_id=%s", AGENT_ID)

        # Step 2 + 3 — ASSUM-009 / F1: BOTH reconciliations must run
        # before the durable consumer attaches, so a redelivered
        # envelope cannot land on an unreconciled history view.
        await recovery_reconcile_on_boot(client)
        await consumer_reconcile_on_boot(client)

        # Step 3.5 — compose the orchestrator dispatch chain
        # (TASK-FW10-007). Production wiring rebinds
        # :data:`_serve_daemon.dispatch_payload` to a closure built
        # from :func:`build_pipeline_consumer_deps` +
        # :func:`make_handle_message_dispatcher`. This MUST happen
        # before ``run_daemon`` enters its fetch loop so the receipt-
        # only ``_default_dispatch`` stub is unreachable on the
        # production code path (Group A scenario).
        await compose_dispatch_chain(client)

        # Step 4 — chain composition complete. The daemon may still
        # be bootstrapping its pull subscription, but the lifecycle
        # chain is reconciled and ready to receive dispatches.
        await state.set_chain_ready(True)

        # Step 5 — daemon (with shared client) and healthz concurrently.
        daemon_task: asyncio.Task[None] = asyncio.create_task(
            run_daemon(config, state, client=client),
            name="forge-serve-daemon",
        )
        healthz_task: asyncio.Task[None] = asyncio.create_task(
            run_healthz_server(config, state),
            name="forge-serve-healthz",
        )
        done, pending = await asyncio.wait(
            {daemon_task, healthz_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        # Drain cancellations so the AppRunner.cleanup() finally-block
        # in run_healthz_server actually runs before we return.
        await asyncio.gather(*pending, return_exceptions=True)
        # Surface any non-cancellation exceptions raised by the winner.
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        # Best-effort fleet deregistration so the agent-registry KV
        # reflects shutdown promptly rather than waiting for jarvis to
        # mark forge stale. ``deregister`` is idempotent and swallows
        # transport errors internally, so this call cannot prevent the
        # client-close that follows. Runs even if registration failed
        # earlier — deregister against a non-existent KV entry is a
        # no-op by design.
        await deregister(fleet_client, reason="shutdown")
        await _close_client_quietly(fleet_client)

        # ``run_daemon`` already closes the client on its own
        # iteration's ``finally`` block. This second close is
        # defensive: if the daemon never reached the iteration finally
        # (e.g. cancelled mid-recovery_reconcile), we still release
        # the connection rather than relying on garbage collection.
        await _close_client_quietly(client)


def _resolve_forge_config_for_serve(ctx: click.Context) -> Any:
    """Pick :class:`ForgeConfig` from ``ctx.obj``; fall back to ``./forge.yaml``.

    TASK-FIX-F010 — ``serve_cmd`` needs a validated :class:`ForgeConfig`
    so :func:`forge.cli._serve_production.bind_production_serve` can
    thread the ``approved_originators`` and ``permissions.filesystem``
    rules into the consumer. The Click top-level group already loads
    ``forge.yaml`` into ``ctx.obj`` for the queue / status / history
    subcommands (see :func:`forge.cli.main._resolve_context_object`);
    this helper reaches for that value and only re-loads from
    ``./forge.yaml`` if the group decoration was bypassed (e.g. when
    tests invoke ``serve_cmd`` directly).

    A missing config is a usage error rather than a silent fallback —
    the daemon refuses to start without one because the FEAT-FORGE-002
    rejection rules are not optional.
    """
    # Local imports keep the module-level surface clean; this helper is
    # only invoked at boot.
    from pathlib import Path

    from forge.config.loader import load_config
    from forge.config.models import ForgeConfig

    if isinstance(ctx.obj, ForgeConfig):
        return ctx.obj
    if Path("forge.yaml").exists():
        return load_config(Path("forge.yaml"))
    raise click.UsageError(
        "forge serve requires a forge.yaml — pass --config <path> or run "
        "from a directory containing ./forge.yaml."
    )


@click.command(name="serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Run the long-lived forge daemon (JetStream consumer + healthz)."""
    # Lazy import to keep the module-level surface clean and to avoid an
    # import cycle (the wrapper imports ``forge.cli.serve`` to rebind
    # the seam at runtime).
    from forge.cli._serve_production import bind_production_serve

    config = ServeConfig.from_env()
    # Attach the stderr handler BEFORE _run_serve schedules the daemon
    # / healthz coroutines, so their first ``logger.info`` lines reach
    # ``docker logs`` and ``journalctl`` instead of the silent root
    # logger. TASK-FORGE-FRR-002.
    _configure_logging(config.log_level)
    forge_config = _resolve_forge_config_for_serve(ctx)
    # Bind the production dispatch-chain composer (TASK-FIX-F010)
    # before ``_run_serve`` enters its boot order. The wrapper opens
    # the SQLite writer connection, builds the middleware, and rebinds
    # ``compose_dispatch_chain`` so ``_run_serve``'s Step 3.5 awaits
    # the production closure rather than the no-op default.
    bind_production_serve(config, forge_config)
    state = SubscriptionState()
    asyncio.run(_run_serve(config, state))


__all__ = [
    "AGENT_ID",
    "ComposeDispatchChainFn",
    "DEFAULT_DURABLE_NAME",
    "DEFAULT_HEALTHZ_PORT",
    "FleetClientOpenerFn",
    "ReconcileFn",
    "ServeConfig",
    "SubscriptionState",
    "bind_production_dispatch_chain",
    "budget_wall_clock",
    "build_supervisor",
    "compose_dispatch_chain",
    "consumer_reconcile_on_boot",
    "deregister",
    "make_budget_pause",
    "make_budget_started_at_reader",
    "make_handle_message_dispatcher",
    "open_fleet_client",
    "resolve_budget_for_build",
    "recovery_reconcile_on_boot",
    "register_on_boot",
    "run_daemon",
    "run_healthz_server",
    "serve_cmd",
]
