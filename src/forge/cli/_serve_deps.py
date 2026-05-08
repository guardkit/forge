"""Composition of :class:`PipelineConsumerDeps` for ``forge serve`` (TASK-FW10-007).

This module is the Wave-3 composition step that turns the five Wave-2
collaborator factories
(:mod:`forge.cli._serve_deps_forward_context`,
:mod:`forge.cli._serve_deps_stage_log`,
:mod:`forge.cli._serve_deps_state_channel`,
:mod:`forge.cli._serve_deps_lifecycle`)
plus the SQLite duplicate-detection helper into the single
:class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`
container the inbound consumer state machine consumes.

What this module wires
----------------------

* ``forge_config`` — passed straight through; the consumer reads
  ``forge_config.pipeline.approved_originators`` and
  ``forge_config.permissions.filesystem.allowlist`` for its rejection
  rules (FEAT-FORGE-002 §2 + §3).
* ``is_duplicate_terminal`` — bound to a SQLite ``SELECT status`` against
  the unique ``(feature_id, correlation_id)`` index on the ``builds``
  table (per ASSUM-014). Returns ``True`` only when the row's
  :class:`~forge.lifecycle.state_machine.BuildState` is one of
  :data:`~forge.lifecycle.state_machine.TERMINAL_STATES`
  (``COMPLETE``/``FAILED``/``CANCELLED``/``SKIPPED``).
* ``dispatch_build`` — a thin closure that records the pending
  ``builds`` row, then calls
  :func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`
  with the three Wave-2 Protocol collaborators
  (:class:`ForwardContextBuilder`, :class:`StageLogRecorder`,
  :class:`AutobuildStateInitialiser`) plus the injected
  :class:`AsyncTaskStarter`. Terminal-only ack of the JetStream
  message is owned by ``pipeline_consumer.handle_message``'s
  ``ack_callback`` — the closure does **not** ack itself (see
  TASK-FW10-001 AC-002).
* ``publish_build_failed`` — bound to
  :meth:`forge.adapters.nats.PipelinePublisher.publish_build_failed`
  via the publisher constructed by
  :func:`forge.cli._serve_deps_lifecycle.build_publisher_and_emitter`.
  The wrapper swallows the ``feature_id`` argument the consumer
  Protocol passes (the publisher derives the subject from
  ``payload.feature_id`` itself).

Single-client invariant (ASSUM-011)
-----------------------------------

Per the IMPLEMENTATION-GUIDE.md §5 boot order, ``_run_serve`` opens
exactly one NATS client and shares it across the daemon, the
publisher/emitter, and this deps factory. We accept the pre-opened
``client`` and pass it to
:func:`build_publisher_and_emitter` rather than dialling a second
connection here.

Per-build ``AsyncTaskStarter`` is supervisor-owned (TASK-FW10-008)
-----------------------------------------------------------------

The :class:`~forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
Protocol is the LangGraph ``AsyncSubAgentMiddleware`` ``start_async_task``
seam (per ADR-ARCH-031). Wiring of the Supervisor and middleware is
TASK-FW10-008's responsibility. Until that lands the deps factory
accepts ``async_task_starter`` as an optional kwarg — production
callers will pass the middleware-backed starter, while unit tests pass
a deterministic fake. When ``None``, the closure raises a clear
``RuntimeError`` rather than silently no-oping; this surfaces the
missing wiring loudly during integration rather than letting a build
disappear into a queue that has no runner attached.

References:
    - TASK-FW10-007 — this module's brief.
    - TASK-FW10-001 — boot order; ``_run_serve`` calls this factory.
    - TASK-FW10-002 — ``autobuild_runner`` AsyncSubAgent.
    - TASK-FW10-008 — supervisor + AsyncSubAgentMiddleware wiring
      (provides the production ``async_task_starter``).
    - ADR-SP-013 — terminal-only ack semantics.
    - ASSUM-011 — single shared NATS client.
    - ASSUM-014 — ``(feature_id, correlation_id)`` unique index.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps
from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.cli._serve_deps_forward_context import (
    build_forward_context_builder,
    build_stage_log_reader,
)
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_deps_stage_log import build_stage_log_recorder
from forge.cli._serve_deps_state_channel import build_autobuild_state_initialiser
from forge.config.models import ForgeConfig, PipelineConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import TERMINAL_STATES, BuildState
from forge.lifecycle_bridge.coexistence import (
    CLAIMER_F010F_SAFETY_NET,
    TerminalPublishLedger,
)
from forge.pipeline import PipelineLifecycleEmitter
from forge.pipeline.build_ack_handle import InFlightAckRegistry
from forge.pipeline.dispatchers.autobuild_async import (
    AsyncTaskStarter,
    dispatch_autobuild_async,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats_core.events import BuildFailedPayload, BuildQueuedPayload

logger = logging.getLogger(__name__)


__all__ = ["build_pipeline_consumer_deps", "is_terminal_status"]


#: Set of canonical ``builds.status`` string values that count as
#: terminal for the duplicate-detection helper. Mirrors
#: :data:`forge.lifecycle.state_machine.TERMINAL_STATES` but stored as
#: the raw string column values used in SQLite so the SQL ``IN`` clause
#: can compare directly without re-hydrating the enum.
_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_STATES)


def is_terminal_status(status: str | None) -> bool:
    """Return True when ``status`` names a terminal :class:`BuildState`.

    Pulled out as a small helper so the duplicate-detection closure
    body stays one assertion long and the membership check is unit-
    testable in isolation. ``None`` (no row) is the legitimate "fresh
    build" signal and returns ``False``.
    """
    return status is not None and status in _TERMINAL_STATUS_VALUES


def _build_is_duplicate_terminal(
    sqlite_pool: SqliteLifecyclePersistence,
):
    """Return an ``async (feature_id, correlation_id) -> bool`` closure.

    The closure issues a single ``SELECT status FROM builds WHERE
    feature_id = ? AND correlation_id = ?`` against a fresh read-only
    SQLite connection (per ADR-ARCH-013) and translates the result:

    * **No row** → ``False`` (fresh build; the consumer continues with
      validation + dispatch).
    * **Non-terminal status** (``QUEUED``/``PREPARING``/``RUNNING``/
      ``PAUSED``/``FINALISING``) → ``False``. The build is in flight;
      the consumer's normal flow handles it (a redelivered envelope
      against an in-flight build is reconciled by
      :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot`,
      not by this duplicate-detection helper).
    * **Terminal status** (``COMPLETE``/``FAILED``/``CANCELLED``/
      ``SKIPPED``) → ``True`` (idempotent ack-and-skip).

    The closure is ``async def`` to honour the
    :data:`~forge.adapters.nats.pipeline_consumer.IsDuplicateTerminal`
    type alias even though the underlying SQLite read is synchronous;
    SQLite reads against the daemon's pool are short and we keep the
    daemon's event loop responsive by holding the writer connection's
    lock for the read alone (no transaction).
    """

    async def is_duplicate_terminal(feature_id: str, correlation_id: str) -> bool:
        """Return True when a terminal ``builds`` row matches the pair."""
        if not feature_id or not correlation_id:
            # The consumer should not call this with empty identifiers
            # (its envelope validation rejects them upstream). Still
            # guard here so a regression in the validator surfaces as a
            # clean ``False`` rather than a wide-open SQL query.
            return False

        try:
            with sqlite_pool._reader() as cx:
                row = cx.execute(
                    """
                    SELECT status FROM builds
                     WHERE feature_id = ? AND correlation_id = ?
                    """,
                    (feature_id, correlation_id),
                ).fetchone()
        except sqlite3.Error as exc:
            # Read failure is not load-bearing for correctness — the
            # consumer treats False as "process the build", which means
            # at worst we re-dispatch a known-terminal build. SQLite
            # surfaces the actual failure for ops via the warning.
            logger.warning(
                "is_duplicate_terminal: SQLite read failed for "
                "feature_id=%s correlation_id=%s (%s); treating as "
                "non-duplicate",
                feature_id,
                correlation_id,
                exc,
            )
            return False

        if row is None:
            return False
        # ``sqlite3.Row`` supports both index and key access; we used
        # ``SELECT status`` so column 0 is the status string. Coerce
        # explicitly so a future schema migration that adds columns
        # cannot quietly shift the index.
        status: Any = row[0] if not hasattr(row, "keys") else row["status"]
        if isinstance(status, BuildState):
            status = status.value
        result = is_terminal_status(status)
        if result:
            logger.debug(
                "is_duplicate_terminal: matched terminal row "
                "feature_id=%s correlation_id=%s status=%s",
                feature_id,
                correlation_id,
                status,
            )
        return result

    return is_duplicate_terminal


def _build_dispatch_build(
    sqlite_pool: SqliteLifecyclePersistence,
    forward_context_builder: Any,
    stage_log_recorder: Any,
    state_channel: Any,
    lifecycle_emitter: Any,
    async_task_starter: AsyncTaskStarter | None,
):
    """Return the production ``dispatch_build`` closure.

    The closure persists a ``QUEUED`` ``builds`` row (so downstream
    crash-recovery has a durable record of the dispatch attempt) and
    then calls :func:`dispatch_autobuild_async` with the three Wave-2
    Protocol collaborators. The ack callback handed to the closure is
    threaded onto the ``async_tasks`` state-channel via
    :class:`AutobuildStateInitialiser` (DDR-006); terminal-only ack of
    the JetStream message itself is the responsibility of the runner's
    terminal lifecycle transition, not the dispatcher.
    """

    async def dispatch_build(payload: "BuildQueuedPayload", ack_callback):
        """Persist + dispatch one accepted ``BuildQueuedPayload``.

        Workflow:

        1. ``record_pending_build(payload)`` — durable QUEUED row;
           translates the unique-index violation on
           ``(feature_id, correlation_id)`` into
           :class:`~forge.lifecycle.persistence.DuplicateBuildError`.
           A duplicate here implies a benign race against
           ``is_duplicate_terminal`` for an in-flight build; we log
           and return without re-dispatching.
        2. :func:`dispatch_autobuild_async` — launches the long-
           running :data:`~forge.pipeline.dispatchers.autobuild_async.AUTOBUILD_RUNNER_NAME`
           AsyncSubAgent with the three Wave-2 collaborators.

        ``ack_callback`` is intentionally *not* called here; it is
        invoked by the terminal-state transition inside
        ``autobuild_runner`` (DDR-007 wiring lands in TASK-FW10-008).
        """
        # Local import to avoid pinning this module's import surface to
        # nats_core when the deps factory is imported during CLI
        # ``--help`` paths (the dispatch closure is the only place the
        # payload type is exercised).
        from forge.lifecycle.persistence import DuplicateBuildError

        if async_task_starter is None:
            raise RuntimeError(
                "build_pipeline_consumer_deps: dispatch_build was invoked "
                "but no async_task_starter was wired. Production wiring "
                "lives in TASK-FW10-008 (Supervisor + AsyncSubAgentMiddleware); "
                "tests should pass a fake starter via the kwarg."
            )

        try:
            build_id = sqlite_pool.record_pending_build(payload)
        except DuplicateBuildError as exc:
            # Duplicate means a row already exists for this
            # ``(feature_id, correlation_id)``. The consumer's
            # ``is_duplicate_terminal`` filter already screens out the
            # terminal half; reaching here means the row is in flight,
            # so we let the live build run rather than spawn a second
            # one. Log so the race is visible to operators.
            logger.warning(
                "dispatch_build: duplicate active build for "
                "feature_id=%s correlation_id=%s (%s); skipping dispatch",
                payload.feature_id,
                payload.correlation_id,
                exc,
            )
            return

        logger.info(
            "dispatch_build: persisted QUEUED row build_id=%s "
            "feature_id=%s correlation_id=%s; dispatching autobuild",
            build_id,
            payload.feature_id,
            payload.correlation_id,
        )

        await dispatch_autobuild_async(
            build_id=build_id,
            feature_id=payload.feature_id,
            correlation_id=payload.correlation_id,
            forward_context_builder=forward_context_builder,
            async_task_starter=async_task_starter,
            stage_log_recorder=stage_log_recorder,
            state_channel=state_channel,
            lifecycle_emitter=lifecycle_emitter,
        )

    return dispatch_build


def _build_publish_build_failed(
    publisher,
    *,
    terminal_publish_ledger: TerminalPublishLedger | None = None,
):
    """Return an ``async (failure_payload, feature_id, *, correlation_id)`` wrapper.

    The consumer's
    :data:`~forge.adapters.nats.pipeline_consumer.PublishBuildFailed`
    type alias passes ``feature_id`` separately for symmetry with the
    other failure subjects in the API contract; the publisher derives
    the subject from ``payload.feature_id`` itself, so the wrapper
    swallows the second positional argument after asserting the two
    agree (defence-in-depth — a mismatched pair is a contract bug
    upstream rather than a publish error).

    DDR-029 — the inbound envelope's ``correlation_id`` is threaded
    through to the outbound envelope by attaching it to the v1
    ``BuildFailedPayload`` via :func:`attach_correlation_id` before the
    publisher reads it back through ``getattr(payload, "correlation_id")``.
    The publisher's central ``_publish_envelope`` then writes it onto the
    outbound :class:`MessageEnvelope`. ``correlation_id=None`` is only
    accepted on the malformed-envelope path where no source value is
    available; every other rejection path threads the inbound value
    explicitly.

    TASK-FRR-PEB-005 — F010F coexistence boundary
    ---------------------------------------------

    When a :class:`TerminalPublishLedger` is wired (production boot path
    for ``forge serve``), the wrapper consults
    :meth:`TerminalPublishLedger.claim` **before** invoking the
    publisher. If the bridge's async-terminal observation already
    claimed the slot for the same ``(feature_id, correlation_id)``,
    ``claim`` returns ``False`` and the wrapper short-circuits without
    publishing. This pins the no-double-emit invariant for every
    ordering: bridge-first, F010F-first, and concurrent.

    The ledger is **optional** so paths that never wire the bridge
    (legacy unit tests, ``forge dispatch`` shell-out) keep their
    F010F-only semantics: the wrapper publishes unconditionally and
    F010F's existing test suite passes unchanged (AC-4).

    The ``correlation_id=None`` malformed-envelope path is exempt from
    the claim check — without a real correlation_id there is nothing
    to coordinate against, and the bridge would never have observed
    such an envelope to begin with.
    """

    from forge.pipeline import attach_correlation_id

    async def publish_build_failed(
        failure_payload: "BuildFailedPayload",
        feature_id: str,
        *,
        correlation_id: str | None,
    ) -> None:
        """Publish ``pipeline.build-failed.{feature_id}`` via the shared publisher.

        Args:
            failure_payload: The :class:`BuildFailedPayload` describing the
                rejection.
            feature_id: Subject-construction key. Must equal
                ``failure_payload.feature_id``; a mismatch is logged and
                the publisher's payload-derived subject wins.
            correlation_id: Inbound envelope ``correlation_id`` (DDR-029).
                Attached to the v1 payload via
                :func:`attach_correlation_id` so the publisher's
                envelope-construction path threads it onto the outbound
                envelope. ``None`` only on the malformed-envelope rejection
                path where no source value is available.
        """
        if failure_payload.feature_id != feature_id:
            # Surface contract bug rather than publish to a subject the
            # caller did not intend; the publisher will derive
            # ``feature_id`` from ``failure_payload`` regardless.
            logger.warning(
                "publish_build_failed: feature_id mismatch payload=%s arg=%s; "
                "publishing on payload.feature_id (publisher-derived)",
                failure_payload.feature_id,
                feature_id,
            )

        # TASK-FRR-PEB-005 AC-2 / AC-3 — first-wins terminal-publish
        # claim. Skip the publish only when a ledger is wired AND the
        # caller has a real ``correlation_id`` (the malformed-envelope
        # path threads ``None`` and is exempt: there is no
        # ``(feature_id, correlation_id)`` pair to coordinate against
        # so the bridge could not possibly have observed it).
        if terminal_publish_ledger is not None and correlation_id is not None:
            won = terminal_publish_ledger.claim(
                feature_id=failure_payload.feature_id,
                correlation_id=correlation_id,
                claimed_by=CLAIMER_F010F_SAFETY_NET,
            )
            if not won:
                logger.info(
                    "publish_build_failed: terminal-publish slot already "
                    "claimed for feature_id=%s correlation_id=%s; "
                    "skipping F010F safety-net emit (TASK-FRR-PEB-005)",
                    failure_payload.feature_id,
                    correlation_id,
                )
                return

        if correlation_id is not None:
            attach_correlation_id(failure_payload, correlation_id)
        await publisher.publish_build_failed(failure_payload)

    return publish_build_failed


def build_pipeline_consumer_deps(
    client: Any,
    forge_config: ForgeConfig,
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    async_task_starter: AsyncTaskStarter | None = None,
    register_ack_handle: InFlightAckRegistry | None = None,
    terminal_publish_ledger: TerminalPublishLedger | None = None,
    publisher: PipelinePublisher | None = None,
) -> PipelineConsumerDeps:
    """Compose the production :class:`PipelineConsumerDeps` for ``forge serve``.

    Wires the four fields of
    :class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`
    against the daemon's shared collaborators:

    * ``forge_config`` — passed straight through.
    * ``is_duplicate_terminal`` — SQLite read closure built by
      :func:`_build_is_duplicate_terminal`.
    * ``dispatch_build`` — autobuild dispatch closure built by
      :func:`_build_dispatch_build`. Internally composes the three
      Wave-2 Protocol collaborators and the (caller-injected)
      :class:`AsyncTaskStarter`.
    * ``publish_build_failed`` — wrapper around the
      :class:`PipelinePublisher` constructed via
      :func:`build_publisher_and_emitter` against the shared NATS
      client.

    Args:
        client: The pre-opened NATS client owned by ``_run_serve``
            (ASSUM-011: exactly one connection per daemon).
        forge_config: Validated :class:`ForgeConfig`. Used by the
            consumer for ``approved_originators`` /
            ``permissions.filesystem.allowlist`` and by the
            forward-context builder for the worktree allowlist.
        sqlite_pool: The shared
            :class:`SqliteLifecyclePersistence` facade. Provides:

            - ``record_pending_build`` for the dispatch closure;
            - ``record_stage`` (via the stage-log recorder factory)
              for the FW10-004 collaborator;
            - the ``async_tasks`` SQLite mirror (via the FW10-005
              factory);
            - read-only ``builds`` reads for duplicate detection.
        async_task_starter: Optional
            :class:`AsyncTaskStarter` used by the autobuild dispatch
            closure. Production wiring is provided by TASK-FW10-008
            (Supervisor + AsyncSubAgentMiddleware); tests pass a
            deterministic fake. When ``None``, calling
            ``deps.dispatch_build`` raises :class:`RuntimeError` so a
            missing wiring surfaces during the first dispatch rather
            than silently dropping the build.

    Returns:
        A fully wired
        :class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`.

    Raises:
        ValueError: When ``client`` is ``None`` (the daemon is
            responsible for opening exactly one client and sharing it).
    """
    if client is None:
        raise ValueError(
            "build_pipeline_consumer_deps: 'client' must be a connected "
            "NATS client; got None. The daemon owns exactly one client "
            "(ASSUM-011) and shares it with this factory; never call "
            "with None."
        )
    if forge_config is None:
        raise ValueError("build_pipeline_consumer_deps: 'forge_config' is required")
    if sqlite_pool is None:
        raise ValueError("build_pipeline_consumer_deps: 'sqlite_pool' is required")

    # 1. Compose the three Wave-2 Protocol collaborators against the
    #    shared SQLite pool + ForgeConfig. Each factory is idempotent
    #    and side-effect free apart from the ``async_tasks`` schema
    #    DDL applied by the state-channel initialiser (which uses
    #    ``CREATE TABLE IF NOT EXISTS``).
    #
    #    ``build_stage_log_reader`` wraps the shared pool in the narrow
    #    :class:`StageLogReader` Protocol surface the forward-context
    #    builder consumes (TASK-FORGE-FRR-F010B). Before this wrapper
    #    existed, the bare facade was handed to the builder and the
    #    first ``build_for`` call raised AttributeError because
    #    :class:`SqliteLifecyclePersistence` does not expose
    #    ``get_approved_stage_entry`` itself.
    stage_log_reader = build_stage_log_reader(sqlite_pool)
    forward_context_builder = build_forward_context_builder(
        stage_log_reader, forge_config
    )
    stage_log_recorder = build_stage_log_recorder(sqlite_pool)
    state_channel = build_autobuild_state_initialiser(sqlite_pool)

    # 2. Build the publisher + emitter pair against the shared NATS
    #    client. The publisher backs ``publish_build_failed``; the
    #    emitter is threaded onto the autobuild dispatch closure
    #    (DDR-007 Option A — in-process Python object via the
    #    ``start_async_task`` context payload) so the runner's
    #    lifecycle transitions (starting → planning_waves → ...) emit
    #    on the same NATS connection that wrote the ``stage_log`` row.
    #
    #    TASK-FORGE-FRR-PEBR-WIREUP — when the caller injects a
    #    ``publisher`` (the production path: ``bind_production_dispatch_chain``
    #    constructs the publisher inside its closure so the
    #    :class:`LifecycleBridgeWireup` shares the SAME publisher
    #    instance as ``publish_build_failed`` — necessary for the
    #    no-double-emit invariant), use the injected publisher and
    #    construct an emitter wrapping it. When ``publisher is None``
    #    (legacy / unit-test path), fall back to the original factory
    #    for both.
    if publisher is not None:
        pipeline_config = (
            forge_config.pipeline
            if forge_config.pipeline is not None
            else PipelineConfig()
        )
        emitter = PipelineLifecycleEmitter(
            publisher=publisher,
            config=pipeline_config,
        )
    else:
        publisher, emitter = build_publisher_and_emitter(
            client, config=forge_config.pipeline
        )

    # 3. Build the four field closures.
    is_duplicate_terminal = _build_is_duplicate_terminal(sqlite_pool)
    dispatch_build = _build_dispatch_build(
        sqlite_pool=sqlite_pool,
        forward_context_builder=forward_context_builder,
        stage_log_recorder=stage_log_recorder,
        state_channel=state_channel,
        lifecycle_emitter=emitter,
        async_task_starter=async_task_starter,
    )
    publish_build_failed = _build_publish_build_failed(
        publisher,
        terminal_publish_ledger=terminal_publish_ledger,
    )

    deps = PipelineConsumerDeps(
        forge_config=forge_config,
        is_duplicate_terminal=is_duplicate_terminal,
        dispatch_build=dispatch_build,
        publish_build_failed=publish_build_failed,
        register_ack_handle=register_ack_handle,
    )
    logger.info(
        "build_pipeline_consumer_deps: composed PipelineConsumerDeps "
        "(async_task_starter=%s, ack_bridge=%s, terminal_publish_ledger=%s)",
        "wired" if async_task_starter is not None else "deferred (TASK-FW10-008)",
        "wired" if register_ack_handle is not None else "deferred (TASK-FRR-PEB-002)",
        (
            "wired"
            if terminal_publish_ledger is not None
            else "deferred (TASK-FRR-PEB-005)"
        ),
    )
    return deps
