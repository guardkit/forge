"""Async autobuild dispatcher (TASK-MAG7-009, FEAT-FORGE-007).

Dispatches a feature's autobuild as a long-running ``AsyncSubAgent`` via the
DeepAgents ``start_async_task`` middleware tool (per ADR-ARCH-031). The call
returns immediately with the assigned ``task_id`` so the supervisor's
reasoning loop stays responsive while autobuild executes in the background
(FEAT-FORGE-007 Group A: "Autobuild runs as an asynchronous subagent so the
supervisor remains responsive during long runs").

Design invariants
-----------------

1. *Forward context* — the autobuild subagent is launched with the
   approved feature-plan artefact path resolved via
   :class:`forge.pipeline.forward_context_builder.ForwardContextBuilder`
   (TASK-MAG7-006). The dispatcher does not read ``stage_log`` directly;
   it threads whatever the builder returns, so the approval and
   worktree-allowlist filters live in exactly one place.

2. *Crash-recovery durability* — the ``stage_log`` row is written
   **before** ``start_async_task`` is awaited. If the process dies
   between submit and ack, durable history (FEAT-FORGE-001 SQLite)
   reflects the dispatch attempt even when the live ``async_tasks``
   state channel does not. This satisfies FEAT-FORGE-007 Group D
   @edge-case "After a crash mid-autobuild the build's authoritative
   status comes from durable history not the live state channel" —
   the dispatcher's contribution is to never leave a state-channel
   entry without a paired ``stage_log`` row.

3. *Single live entry per (build_id, feature_id, autobuild)* — both the
   ``stage_log`` recorder and the state-channel initialiser are upsert
   shaped (idempotent on the natural key). The dispatcher writes the
   ``stage_log`` row twice on the happy path: once before
   ``start_async_task`` (no ``task_id`` yet) and once after, with the
   assigned ``task_id`` threaded into ``details_json``.

4. *correlation_id is threaded everywhere* — the originating
   ``correlation_id`` lands on (a) the ``stage_log`` row's
   ``details_json``, (b) the launched task's context payload, and (c)
   the ``async_tasks`` state-channel ``AutobuildState`` entry. This is
   the FEAT-FORGE-007 Group I @data-integrity contract: every
   downstream observation of the autobuild can be correlated back to
   the build that triggered it.

5. *No lifecycle progression here* — the dispatcher writes the initial
   ``lifecycle="starting"`` state-channel entry and exits. The
   subsequent transitions through ``planning_waves``, ``running_wave``,
   ``awaiting_approval``, ``completed`` (etc.) are written by
   ``autobuild_runner`` itself (FEAT-FORGE-005 + ADR-ARCH-031).

Concurrency
-----------

Two concurrent builds dispatching autobuild at the same time get
distinct ``task_id`` values because the ``AsyncTaskStarter`` Protocol
contract requires ``start_async_task`` to mint a fresh identifier per
call. The dispatcher is otherwise stateless — every dependency is
injected, so the same instance can be called from concurrent supervisors
without ownership ambiguity (Group F @concurrency).

References:
    - TASK-MAG7-009 — this task brief.
    - TASK-MAG7-001 — :mod:`forge.pipeline.stage_taxonomy`.
    - TASK-MAG7-006 — :class:`ForwardContextBuilder`.
    - TASK-MAG7-013 — integration-level crash-recovery test.
    - DDR-006 — ``AutobuildState`` Pydantic model (lifecycle literals).
    - ADR-ARCH-031 — ``AsyncSubAgent`` / ``start_async_task`` decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

from forge.pipeline.forward_context_builder import (
    ContextEntry,
    ForwardContextBuilder,
)
from forge.pipeline.stage_taxonomy import StageClass

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from forge.pipeline import PipelineLifecycleEmitter

__all__ = [
    "AUTOBUILD_RUNNER_NAME",
    "AUTOBUILD_STARTING_LIFECYCLE",
    "AsyncTaskStarter",
    "AutobuildDispatchHandle",
    "AutobuildStateInitialiser",
    "StageLogRecorder",
    "dispatch_autobuild_async",
]

logger = logging.getLogger(__name__)


#: Subagent name for the long-running autobuild ``AsyncSubAgent``.
#:
#: This is the name DeepAgents' ``AsyncSubAgentMiddleware`` looks up when
#: ``start_async_task`` is invoked. It is owned by FEAT-FORGE-005 and
#: ADR-ARCH-031; this dispatcher only references it.
AUTOBUILD_RUNNER_NAME: str = "autobuild_runner"


#: Lifecycle string the dispatcher writes onto the initial
#: ``AutobuildState`` entry.
#:
#: Verbatim from DDR-006's ``AutobuildState.lifecycle`` ``Literal``. The
#: subsequent lifecycle transitions are owned by ``autobuild_runner``
#: itself; the dispatcher is responsible for ``"starting"`` only.
AUTOBUILD_STARTING_LIFECYCLE: str = "starting"


# ---------------------------------------------------------------------------
# Public dataclass returned to callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutobuildDispatchHandle:
    """The handle returned to a caller of :func:`dispatch_autobuild_async`.

    The handle is intentionally minimal — it carries the four identifiers
    a supervisor needs to reason about the in-flight autobuild
    (``task_id``, ``build_id``, ``feature_id``, ``correlation_id``).
    Anything else is read back off the ``async_tasks`` state channel or
    the ``stage_log`` row.

    Attributes:
        task_id: Identifier minted by the ``start_async_task`` middleware
            tool. Per-feature unique within a build; per
            FEAT-FORGE-007 Group F @concurrency, two concurrent builds'
            dispatches receive distinct ``task_id`` values.
        feature_id: The feature whose autobuild was dispatched.
        build_id: The build the dispatch is scoped to.
        correlation_id: The originating correlation ID, threaded onto
            the launched task's context, the ``stage_log`` row's
            ``details_json``, and the ``async_tasks`` state-channel
            entry.
    """

    task_id: str
    feature_id: str
    build_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Injected Protocols — the only I/O surface the dispatcher is allowed
# ---------------------------------------------------------------------------


@runtime_checkable
class AsyncTaskStarter(Protocol):
    """Protocol over the DeepAgents ``start_async_task`` middleware tool.

    Production wires the ``AsyncSubAgentMiddleware`` ``start_async_task``
    hook (per ADR-ARCH-031). Tests inject an in-memory fake that mints
    deterministic ``task_id`` values so the dispatcher's behaviour can be
    asserted without standing up a LangGraph runtime.

    The Protocol exposes both a sync and an async launch method. The
    sync ``start_async_task`` is preserved for in-memory test fakes and
    pre-F010G callers; the async ``astart_async_task`` is the production
    path because the deepagents middleware's sync ``_ClientCache.get_sync``
    raises on ``url=None`` while ``get_async`` falls back to in-process
    ASGI transport — see TASK-FORGE-FRR-F010G investigation §5 and
    ``deepagents/middleware/async_subagents.py:239-262``.
    """

    def start_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:  # pragma: no cover - protocol stub
        """Launch ``subagent_name`` with ``context`` and return the task_id.

        Args:
            subagent_name: Name of the registered ``AsyncSubAgent`` to
                launch. For autobuild dispatch this is always
                :data:`AUTOBUILD_RUNNER_NAME`.
            context: JSON-serialisable mapping threaded onto the launched
                task. Must include ``correlation_id`` so downstream
                events on the launched task can be correlated back.

        Returns:
            The freshly-minted ``task_id``. Per-call unique — concurrent
            calls (even with identical context) must return distinct
            values.
        """
        ...

    async def astart_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:  # pragma: no cover - protocol stub
        """Async sibling of :meth:`start_async_task` — production path.

        TASK-FORGE-FRR-F010G: the autobuild_runner registration is shipped
        without a ``url`` field (in-process ASGI transport). The deepagents
        sync launch path raises ``ValueError`` on that shape; only the
        async path's ``_ClientCache.get_async`` tolerates ``url=None`` and
        falls back to in-process ASGI transport via the LangGraph SDK's
        ``get_client(url=None)``. Production wires the
        :class:`_StructuredToolAsyncTaskStarter` adapter which awaits
        the underlying ``StructuredTool.coroutine``; the dispatcher
        :func:`dispatch_autobuild_async` is ``async def`` for this
        reason.

        Args:
            subagent_name: As :meth:`start_async_task`.
            context: As :meth:`start_async_task`.

        Returns:
            The freshly-minted ``task_id``.
        """
        ...


@runtime_checkable
class StageLogRecorder(Protocol):
    """Protocol over the FEAT-FORGE-001 ``stage_log`` writer.

    The recorder is upsert-shaped on ``(build_id, feature_id, stage)``
    so the dispatcher can call it twice on the happy path: once before
    :meth:`AsyncTaskStarter.start_async_task` (no ``task_id`` yet, but
    durable evidence that a dispatch was attempted) and once after, with
    the assigned ``task_id`` threaded into ``details_json``.

    Production wires the FEAT-FORGE-001 SQLite adapter. Tests use an
    in-memory fake — the same shape TASK-MAG7-006 / TASK-MAG7-005 use
    for their reader Protocols.
    """

    def record_running(
        self,
        build_id: str,
        feature_id: str,
        stage: StageClass,
        details_json: Mapping[str, Any],
    ) -> None:  # pragma: no cover - protocol stub
        """Upsert a ``state="running"`` ``stage_log`` row.

        Args:
            build_id: Build the row is scoped to.
            feature_id: Feature the row is attributed to (per-feature
                stages only — ``StageClass.AUTOBUILD`` is per-feature).
            stage: Stage class of the row. For this dispatcher always
                :attr:`StageClass.AUTOBUILD`.
            details_json: JSON-serialisable mapping persisted onto the
                row. Always includes ``correlation_id``; once
                ``start_async_task`` returns, also includes ``task_id``.
        """
        ...


@runtime_checkable
class AutobuildStateInitialiser(Protocol):
    """Protocol over the ``async_tasks`` state-channel writer.

    Production is the LangGraph ``AsyncSubAgentMiddleware`` ``async_tasks``
    state channel, written via the supervisor's state-graph reducer.
    Tests use an in-memory fake — the same shape TASK-MAG7-005 uses for
    its ``AsyncTaskReader`` Protocol on the read side.

    The dispatcher writes exactly one entry per dispatch, with
    ``lifecycle=AUTOBUILD_STARTING_LIFECYCLE``. Lifecycle progression
    beyond ``"starting"`` is owned by ``autobuild_runner`` (DDR-006).
    """

    def initialise_autobuild_state(
        self,
        build_id: str,
        feature_id: str,
        task_id: str,
        correlation_id: str,
        lifecycle: str,
        wave_index: int,
        task_index: int,
    ) -> None:  # pragma: no cover - protocol stub
        """Write the initial ``AutobuildState`` entry for the launched task.

        Args:
            build_id: Build the autobuild belongs to.
            feature_id: Feature the autobuild belongs to.
            task_id: Identifier returned by ``start_async_task``.
            correlation_id: Correlation ID threaded through the dispatch.
            lifecycle: Initial lifecycle string. Always
                :data:`AUTOBUILD_STARTING_LIFECYCLE` from this dispatcher.
            wave_index: Initial wave index. Always ``0`` on dispatch.
            task_index: Initial task index. Always ``0`` on dispatch.
        """
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialise_context_entries(
    entries: list[ContextEntry] | tuple[ContextEntry, ...],
) -> list[dict[str, str]]:
    """Convert :class:`ContextEntry` objects into JSON-serialisable dicts.

    ``stage_log.details_json`` and the launched task's context payload
    are both stored as JSON, so :class:`ContextEntry` (a frozen
    dataclass) needs to be flattened. Order is preserved — the
    :class:`ForwardContextBuilder` already orders entries to match the
    producer recipe, and downstream tooling can rely on the order.
    """
    return [
        {"flag": entry.flag, "value": entry.value, "kind": entry.kind}
        for entry in entries
    ]


# ---------------------------------------------------------------------------
# Public dispatch function
# ---------------------------------------------------------------------------


async def dispatch_autobuild_async(
    build_id: str,
    feature_id: str,
    correlation_id: str,
    *,
    forward_context_builder: ForwardContextBuilder,
    async_task_starter: AsyncTaskStarter,
    stage_log_recorder: StageLogRecorder,
    state_channel: AutobuildStateInitialiser,
    lifecycle_emitter: "PipelineLifecycleEmitter | None" = None,
    branch: str | None = None,
    repo: str | None = None,
) -> AutobuildDispatchHandle:
    """Dispatch ``feature_id``'s autobuild as a long-running async subagent.

    Workflow (in execution order — the order is part of the contract,
    not an implementation detail):

    1. **Resolve forward context.** Call
       :meth:`ForwardContextBuilder.build_for` for
       :attr:`StageClass.AUTOBUILD` to get the approved feature-plan
       artefact path (and any other entries the propagation contract
       declares for the autobuild stage). The builder enforces the
       approval and worktree-allowlist filters internally.

    2. **Record ``stage_log`` BEFORE submit.** Upsert a ``running`` row
       with ``correlation_id`` and the resolved context entries in
       ``details_json`` — but no ``task_id`` yet. This is the
       durability invariant: even if the process dies between this
       write and the ``start_async_task`` ack, the SQLite row records
       that a dispatch was attempted.

    3. **Invoke ``astart_async_task``.** Call the middleware tool with
       :data:`AUTOBUILD_RUNNER_NAME` and a context payload that carries
       ``build_id``, ``feature_id``, ``correlation_id``, and the
       serialised context entries. The await resolves with the assigned
       ``task_id``; the actual subagent work runs in the background.
       TASK-FORGE-FRR-F010G drove the switch from the sync to the async
       path because the deepagents sync ``_ClientCache.get_sync`` raises
       on ``url=None`` while the async path tolerates it.

    4. **Re-record ``stage_log``** with the ``task_id`` threaded into
       ``details_json``. The upsert semantics of the recorder mean we
       end up with a single live row whose ``details_json`` reflects
       the assigned ``task_id`` (Group I @data-integrity).

    5. **Initialise ``async_tasks`` state-channel entry.** Write the
       :class:`AutobuildStateLike` row with
       ``lifecycle=AUTOBUILD_STARTING_LIFECYCLE``, ``wave_index=0``,
       ``task_index=0``, and ``correlation_id`` threaded.

    6. **Return the handle.** The supervisor uses the handle to track
       the in-flight autobuild and (eventually) reconcile it with the
       ``async_tasks`` channel reads.

    Args:
        build_id: The build the dispatch is scoped to. Used as the
            primary key on every downstream write.
        feature_id: The feature whose autobuild to dispatch. Required
            because :attr:`StageClass.AUTOBUILD` is per-feature
            (FEAT-FORGE-007 Group B).
        correlation_id: The originating correlation ID. Threaded through
            every downstream observation; per FEAT-FORGE-002, this is
            the only way to correlate published events across the
            stack.
        forward_context_builder: Builder that resolves
            ``--context`` entries for the autobuild stage. Approval and
            allowlist filters live inside the builder, not here.
        async_task_starter: The DeepAgents ``start_async_task`` middleware
            tool, satisfying :class:`AsyncTaskStarter`.
        stage_log_recorder: Upsert-shaped ``stage_log`` writer,
            satisfying :class:`StageLogRecorder`.
        state_channel: Upsert-shaped ``async_tasks`` state-channel
            writer, satisfying :class:`AutobuildStateInitialiser`.
        lifecycle_emitter: Optional :class:`PipelineLifecycleEmitter`
            (TASK-FW10-006) threaded into the launched task's context
            payload as ``ctx['lifecycle_emitter']`` per DDR-007 §Decision
            (Option A — in-process Python object via the
            ``start_async_task`` context). The
            :data:`AUTOBUILD_RUNNER_NAME` subagent reads it from
            ``ctx['lifecycle_emitter']`` and calls
            ``emitter.on_transition(new_state)`` from its
            ``_update_state`` helper so every lifecycle progression
            ``(starting → planning_waves → running_wave → ...)`` is
            published on the same NATS connection that wrote the
            ``stage_log`` row. Defaults to ``None`` for backwards
            compatibility with the wave-2 dispatcher tests; production
            callers (TASK-FW10-008) MUST pass an emitter so the
            five-collaborator wiring contract is honoured.
        branch: The base branch the build is scoped to, forwarded verbatim
            from the consumed :class:`BuildQueuedPayload.branch`. When set it
            is threaded into the launch payload as ``payload["branch"]`` so the
            autobuild_runner's DEFECT #19 branch-aware isolated-worktree path
            engages. ``None`` (legacy CLI / boot-rearm launch, no payload in
            scope) keeps the launch bytes byte-compatible with the pre-fix
            shape — the key is omitted entirely.
        repo: The ``org/repo`` the build targets, forwarded verbatim from
            :class:`BuildQueuedPayload.repo`. Threaded into the launch payload
            as ``payload["repo"]`` when set (same one-hop provenance as
            ``branch``); omitted when ``None`` so the runner's existing
            FORGE_DEFAULT_REPO fallback still governs the legacy path.

    Returns:
        :class:`AutobuildDispatchHandle` carrying the minted ``task_id``
        and the four identifiers the supervisor needs.

    Raises:
        ValueError: If any of ``build_id``, ``feature_id``, or
            ``correlation_id`` is empty. The dispatcher refuses to
            launch an autobuild it cannot correlate; this is the same
            stance the FEAT-FORGE-002 publisher takes for
            ``correlation_id`` validation.

    Example:
        >>> handle = dispatch_autobuild_async(
        ...     build_id="build-FEAT-X-20260426",
        ...     feature_id="FEAT-X",
        ...     correlation_id="corr-001",
        ...     forward_context_builder=builder,
        ...     async_task_starter=middleware_tool,
        ...     stage_log_recorder=sqlite_recorder,
        ...     state_channel=async_tasks_writer,
        ... )
        >>> handle.task_id
        'autobuild-task-001'
    """
    # Reject empty identifiers up front. ``start_async_task`` may not
    # guard this — failing fast here gives operators a clear error
    # rather than a confusing "task launched with empty correlation_id"
    # downstream.
    if not build_id:
        raise ValueError(
            "dispatch_autobuild_async: build_id must be a non-empty string"
        )
    if not feature_id:
        raise ValueError(
            "dispatch_autobuild_async: feature_id must be a non-empty string"
        )
    if not correlation_id:
        raise ValueError(
            "dispatch_autobuild_async: correlation_id must be a non-empty string"
        )

    # 1. Resolve forward context. The builder filters approval and
    #    allowlist internally; if the feature-plan is not yet approved
    #    the builder returns an empty list and we still proceed —
    #    refusing to dispatch on an empty context is the
    #    StageOrderingGuard's job (TASK-MAG7-003), not ours.
    context_entries = forward_context_builder.build_for(
        stage=StageClass.AUTOBUILD,
        build_id=build_id,
        feature_id=feature_id,
    )
    serialised_context = _serialise_context_entries(context_entries)

    # 2. Record stage_log BEFORE start_async_task. ``task_id`` is not
    #    yet known; the row carries enough metadata to reconstruct the
    #    dispatch attempt on crash recovery.
    pre_dispatch_details: dict[str, Any] = {
        "subagent": AUTOBUILD_RUNNER_NAME,
        "correlation_id": correlation_id,
        "context_entries": serialised_context,
        "task_id": None,
    }
    stage_log_recorder.record_running(
        build_id=build_id,
        feature_id=feature_id,
        stage=StageClass.AUTOBUILD,
        details_json=pre_dispatch_details,
    )
    logger.debug(
        "dispatch_autobuild_async: recorded pre-dispatch stage_log row "
        "build_id=%s feature_id=%s correlation_id=%s",
        build_id,
        feature_id,
        correlation_id,
    )

    # 3. Invoke start_async_task. Returns synchronously with the
    #    minted task_id; the runner's actual work happens in the
    #    background. DDR-007 §Decision Option A: the
    #    ``lifecycle_emitter`` is threaded onto the launched task's
    #    context payload as an in-process Python object so the
    #    autobuild_runner subagent (TASK-FW10-002) can read it back as
    #    ``ctx['lifecycle_emitter']`` and invoke
    #    ``emitter.on_transition(state)`` from its single transition
    #    site.
    launch_payload: dict[str, Any] = {
        "build_id": build_id,
        "feature_id": feature_id,
        "correlation_id": correlation_id,
        "context_entries": serialised_context,
        "lifecycle_emitter": lifecycle_emitter,
    }
    # DEFECT #19 activation (B4 round-17): thread ``branch`` (and ``repo`` when
    # equally available) from the ALREADY-VALIDATED BuildQueuedPayload the
    # consumer's dispatch closure accepted into the launch payload. Without
    # these the round-17 launch bytes carried only
    # {build_id, context_entries, correlation_id, feature_id}, so the runner's
    # branch-aware isolated-worktree path never engaged (payload["branch"]
    # absent → legacy shared-checkout mode). We add each key ONLY when a value
    # was threaded so the legacy CLI / boot-rearm launch (no branch/repo) stays
    # byte-compatible with the pre-fix payload the F2 path proved. Values are
    # forwarded verbatim (no reformatting) — same one-hop provenance as
    # build_id/feature_id/correlation_id.
    if branch:
        launch_payload["branch"] = branch
    if repo:
        launch_payload["repo"] = repo
    # TASK-FORGE-FRR-F010G: prefer the async launch path. The deepagents
    # middleware's sync path raises on ``url=None`` (the autobuild_runner
    # registration shape) while the async path tolerates ``url=None`` and
    # falls back to in-process ASGI transport via the LangGraph SDK
    # ``get_client(url=None)``.
    task_id = await async_task_starter.astart_async_task(
        subagent_name=AUTOBUILD_RUNNER_NAME,
        context=launch_payload,
    )
    if not task_id:
        # Defensive: a starter that returns an empty task_id is in
        # contract violation. Surface the bug rather than write a
        # state-channel entry keyed on "" (which would alias every
        # subsequent dispatch).
        raise ValueError(
            "dispatch_autobuild_async: start_async_task returned an empty "
            "task_id (contract violation); refusing to initialise "
            f"async_tasks for build_id={build_id!r} feature_id={feature_id!r}"
        )

    # 4. Re-record stage_log with the assigned task_id threaded into
    #    details_json. Upsert semantics on (build_id, feature_id,
    #    stage) keep this to a single logical row.
    post_dispatch_details: dict[str, Any] = {
        "subagent": AUTOBUILD_RUNNER_NAME,
        "correlation_id": correlation_id,
        "context_entries": serialised_context,
        "task_id": task_id,
    }
    stage_log_recorder.record_running(
        build_id=build_id,
        feature_id=feature_id,
        stage=StageClass.AUTOBUILD,
        details_json=post_dispatch_details,
    )

    # 5. Initialise the async_tasks state-channel entry. Lifecycle is
    #    "starting"; subsequent transitions are autobuild_runner's
    #    responsibility (DDR-006).
    state_channel.initialise_autobuild_state(
        build_id=build_id,
        feature_id=feature_id,
        task_id=task_id,
        correlation_id=correlation_id,
        lifecycle=AUTOBUILD_STARTING_LIFECYCLE,
        wave_index=0,
        task_index=0,
    )

    logger.info(
        "dispatch_autobuild_async: launched task_id=%s build_id=%s "
        "feature_id=%s correlation_id=%s",
        task_id,
        build_id,
        feature_id,
        correlation_id,
    )

    # 6. Return the handle.
    return AutobuildDispatchHandle(
        task_id=task_id,
        feature_id=feature_id,
        build_id=build_id,
        correlation_id=correlation_id,
    )
