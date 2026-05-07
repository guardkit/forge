"""``LifecycleBridge`` — SSE-attached lifecycle owner (TASK-FRR-PEB-002 / -007).

The bridge owns the SSE connection lifecycle to the langgraph-runner
sidecar. T2 established the structural foundation; later waves layer in
the active behaviours:

* Public method surface: ``attach``, ``detach``, ``recover_in_flight``,
  ``shutdown``, ``request_cancel`` (TASK-FRR-PEB-007).
* SQLite-backed in-flight registry persistence via
  :class:`forge.persistence.repositories.bridge_registry.BridgeRegistry`.

The methods do *not* yet open SSE connections — that wiring is split
across the follow-up tasks:

* T3 — SSE envelope translation layer (consumes the registry).
* T4 — ``forge serve`` startup wiring (calls :meth:`attach`/:meth:`shutdown`).
* T7 — operator cancellation: :meth:`request_cancel` calls
  ``runs.cancel(thread_id, run_id, action="interrupt")`` on the
  langgraph-runner sidecar via the SDK and returns immediately. The
  bridge does **not** synthesise the ``pipeline.build-cancelled``
  envelope from this method — it waits for the sidecar to confirm
  ``terminal=interrupted`` over SSE and lets the translator emit the
  envelope (single emit site, FEAT-FORGE-004 contract extended to the
  cancel path).
* T9 — crash-recovery handshake (extends :meth:`recover_in_flight`).

The registry doubles as the source for ``forge status --in-flight``
(T12), so the bridge's reads (:meth:`recover_in_flight`) deliberately
return registry-only fields and never expose live SSE connection
metadata. This matches AC-4: "list_active() returns rows for
forge status --in-flight (T12) with no SSE connection metadata
leaking".

F010C correlation-id contract (AC-5)
-------------------------------------

Every BridgeRegistry call site in this module passes ``correlation_id=``
as a keyword argument. The matching AST guard lives in
``tests/forge/test_pipeline_consumer_correlation_id.py`` so a future
refactor that adds a registry call site without threading the field
fails the test suite at lint time, before any runtime regression.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from forge.lifecycle_bridge.version_check import check_langgraph_runner_version
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
)

logger = logging.getLogger(__name__)


__all__ = [
    "AckHandle",
    "BuildContext",
    "CancelResult",
    "LangGraphCancelClient",
    "LifecycleBridge",
    "RunsCancelClient",
]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Inputs to :meth:`LifecycleBridge.attach`.

    A frozen dataclass so callers can pass it across thread boundaries
    without defensive copies. Mirrors the registry row's identifying
    columns plus the deadline so the bridge can hand the context
    straight to the registry without restructuring.

    Attributes:
        feature_id: Primary key of the in-flight build.
        thread_id: DeepAgents thread id.
        run_id: LangGraph run id.
        correlation_id: F010C correlation-id of the inbound build-queued
            envelope.
        deadline_at: 300s per-build deadline (ASSUM-003); T8 reads this
            from the registry for deadline enforcement.
    """

    feature_id: str
    thread_id: str
    run_id: str
    correlation_id: str
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Outcome of :meth:`LifecycleBridge.request_cancel`.

    The serve-side cancel handler uses this to distinguish a freshly
    issued SDK cancel ("we just told the sidecar to interrupt the run")
    from an idempotent no-op ("a cancel for this feature is already in
    flight; the sidecar has been told already and we won't double-up")
    from a no-registry-row no-op ("there is no live build for this id").

    Attributes:
        feature_id: Build the cancel was directed at.
        invoked: ``True`` when this call issued the SDK
            ``runs.cancel(...)``; ``False`` for any short-circuit path.
        reason: Short categorical label suitable for structured
            logging. One of ``"invoked"`` (SDK issued),
            ``"already-cancelling"`` (cancel-in-flight idempotency
            guard short-circuited), ``"no-registry-row"`` (no live
            build found for this feature_id).
        thread_id: DeepAgents thread id pulled from the registry row,
            or ``None`` when no registry row was found at request time.
        run_id: LangGraph run id pulled from the registry row, or
            ``None`` when no registry row was found at request time.
    """

    feature_id: str
    invoked: bool
    reason: str = "invoked"
    thread_id: str | None = None
    run_id: str | None = None


# ---------------------------------------------------------------------------
# SDK Protocol — kept narrow so production wires the real
# ``langgraph_sdk.client.LangGraphClient`` while tests pass a fake.
# ---------------------------------------------------------------------------


class RunsCancelClient(Protocol):
    """Subset of ``langgraph_sdk.client.RunsClient`` the bridge depends on.

    Only :meth:`cancel` is consumed in the cancel path — keeping the
    Protocol narrow means tests can pass a one-method fake without
    constructing a full SDK client, and a future SDK addition does not
    silently widen the bridge's surface.
    """

    async def cancel(
        self,
        thread_id: str,
        run_id: str,
        *,
        action: str = "interrupt",
    ) -> None: ...


class LangGraphCancelClient(Protocol):
    """Subset of ``langgraph_sdk.client.LangGraphClient`` for cancel.

    Production wires the real
    :class:`langgraph_sdk.client.LangGraphClient`; tests substitute a
    fake whose ``runs`` attribute exposes the
    :class:`RunsCancelClient` Protocol. This keeps the bridge's
    constructor signature stable across both paths.
    """

    @property
    def runs(self) -> RunsCancelClient: ...


@dataclass(frozen=True, slots=True)
class AckHandle:
    """Opaque ack-handle wrapper.

    The bridge stores only ``token`` in the SQLite registry — the in-
    memory ack callback that the consumer (T1) maps the token back to
    is not pickleable, so persisting it would create a cross-process
    deserialisation hazard. Keeping the indirection here means a
    crash-recovery boot can read the token and re-establish the
    callback mapping without serialising un-pickleable closures.
    """

    token: str


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class LifecycleBridge:
    """Owns the SSE-to-registry lifecycle for in-flight builds.

    Args:
        registry: The
            :class:`forge.persistence.repositories.bridge_registry.BridgeRegistry`
            backing the in-flight registry. Injected at construction so
            tests can substitute a real or fake repository against an
            in-memory SQLite database.
        sidecar_url: Optional base URL of the ``langgraph-runner``
            sidecar. When supplied, the constructor invokes
            :func:`forge.lifecycle_bridge.version_check.check_langgraph_runner_version`
            **before** any registry interaction (and therefore before
            :meth:`recover_in_flight`) so a version-mismatched sidecar
            fails the daemon at boot with a clear diagnostic rather
            than emitting malformed envelopes at runtime
            (TASK-FRR-PEB-010, AC-2/AC-3). When ``None`` (the
            default), the check is skipped — useful for unit tests
            and for the T2 skeleton callers that do not yet wire a
            sidecar URL.

    The constructor accepts no SSE client — that arrives in T3/T4.
    """

    def __init__(
        self,
        *,
        registry: BridgeRegistry,
        sidecar_url: str | None = None,
        sdk_client: LangGraphCancelClient | None = None,
    ) -> None:
        if not isinstance(registry, BridgeRegistry):
            raise TypeError(
                "LifecycleBridge: registry must be a BridgeRegistry; got "
                f"{type(registry).__name__}"
            )
        # AC-2/AC-3 (TASK-FRR-PEB-010): version-skew diagnostic. Runs
        # *before* the registry handle is stored so a mismatched sidecar
        # cannot leave the bridge in a half-initialised state. The check
        # itself is silent on transport errors (slow / unreachable
        # sidecar) — only a confirmed out-of-range version raises and
        # propagates out of this constructor to fail daemon boot.
        if sidecar_url is not None:
            check_langgraph_runner_version(sidecar_url)
        self._registry = registry
        # Internal book-keeping for the SSE-attached features. T3 will
        # populate this map with live SSE clients keyed by feature_id.
        self._attached: dict[str, AckHandle] = {}
        # SDK client for ``runs.cancel`` — TASK-FRR-PEB-007. Optional so
        # T2-era callers (and unit tests that exercise attach/detach
        # only) can construct the bridge without a live SDK.
        self._sdk_client: LangGraphCancelClient | None = sdk_client
        # Cancel-in-flight idempotency guard (AC-5). Tracked in-memory
        # rather than as a registry column because the only consumer is
        # ``request_cancel`` and the in-memory set is reset on process
        # restart anyway — a cancel that was issued before a crash is
        # re-issued by the operator on the new daemon, and the SDK side
        # is already idempotent against repeat ``runs.cancel`` calls
        # against an already-interrupted run.
        self._cancel_in_flight: set[str] = set()

    # ------------------------------------------------------------------
    # Public API — attach
    # ------------------------------------------------------------------

    def attach(
        self,
        build_context: BuildContext,
        ack_handle: AckHandle,
    ) -> None:
        """Persist a registry row for ``build_context`` and remember the ack handle.

        AC-4: this method writes the row. The actual SSE connection is
        opened by T3; T2 records the metadata so a crashed-then-rebooted
        ``forge serve`` instance can recover the in-flight set.

        Args:
            build_context: The :class:`BuildContext` to attach.
            ack_handle: The :class:`AckHandle` whose ``token`` field is
                persisted in the registry. The in-memory ack callback
                stays in the consumer (T1) — only the opaque token
                is serialised.

        Raises:
            TypeError: If ``build_context`` or ``ack_handle`` are the
                wrong types.
        """
        if not isinstance(build_context, BuildContext):
            raise TypeError(
                "LifecycleBridge.attach: build_context must be a BuildContext; "
                f"got {type(build_context).__name__}"
            )
        if not isinstance(ack_handle, AckHandle):
            raise TypeError(
                "LifecycleBridge.attach: ack_handle must be an AckHandle; "
                f"got {type(ack_handle).__name__}"
            )

        now = datetime.now(UTC)
        entry = BridgeRegistryEntry(
            feature_id=build_context.feature_id,
            thread_id=build_context.thread_id,
            run_id=build_context.run_id,
            correlation_id=build_context.correlation_id,
            ack_handle_token=ack_handle.token,
            deadline_at=build_context.deadline_at,
            attached_at=now,
            # T3 will overwrite this once the SSE stream emits its first
            # lifecycle event. Until then "queued" is the safe default —
            # it matches the build-queued envelope that triggered attach.
            current_lifecycle="queued",
            updated_at=now,
            last_event_id=None,
        )
        self._registry.record(entry, correlation_id=build_context.correlation_id)
        self._attached[build_context.feature_id] = ack_handle
        logger.info(
            "lifecycle_bridge.attach feature_id=%s correlation_id=%s "
            "thread_id=%s run_id=%s",
            build_context.feature_id,
            build_context.correlation_id,
            build_context.thread_id,
            build_context.run_id,
        )

    # ------------------------------------------------------------------
    # Public API — detach
    # ------------------------------------------------------------------

    def detach(self, feature_id: str, *, correlation_id: str) -> None:
        """Remove the registry row for ``feature_id`` and forget the ack handle.

        AC-4: this method deletes the row. The SSE disconnect itself
        is wired by T3/T4 — this skeleton frees the in-memory bookkeeping
        and the persisted registry entry so a re-attach for the same
        feature does not see stale state.

        Args:
            feature_id: Primary key of the row to remove.
            correlation_id: F010C correlation-id of the calling envelope
                (typically the terminal lifecycle event); threaded
                through to the registry for traceability.

        Raises:
            ValueError: If either argument is empty.
        """
        if not feature_id:
            raise ValueError(
                "LifecycleBridge.detach: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "LifecycleBridge.detach: correlation_id must be non-empty"
            )

        self._registry.delete(feature_id, correlation_id=correlation_id)
        self._attached.pop(feature_id, None)
        # Clear cancel-in-flight tracking so a future re-attach of the
        # same feature_id is not treated as still-being-cancelled.
        self._cancel_in_flight.discard(feature_id)
        logger.info(
            "lifecycle_bridge.detach feature_id=%s correlation_id=%s",
            feature_id,
            correlation_id,
        )

    # ------------------------------------------------------------------
    # Public API — recover_in_flight
    # ------------------------------------------------------------------

    def recover_in_flight(
        self,
        *,
        correlation_id: str,
    ) -> list[BridgeRegistryEntry]:
        """Return every persisted in-flight registry entry.

        The return value is the source for the ``forge status --in-flight``
        CLI surface (T12). T9 will extend this method to also re-attach
        the SSE connection for each returned entry — for T2 we expose the
        registry contents so callers can render the status table without
        contacting the langgraph-runner sidecar.

        AC-4 — none of the returned entries contains live SSE connection
        metadata; :class:`BridgeRegistryEntry` is the canonical
        registry-only projection.

        Args:
            correlation_id: F010C correlation-id of the calling context
                (typically the boot-time recovery sweep's correlation
                id); threaded through to the registry for traceability.

        Returns:
            List of :class:`BridgeRegistryEntry` ordered by
            ``attached_at`` ascending.
        """
        if not correlation_id:
            raise ValueError(
                "LifecycleBridge.recover_in_flight: correlation_id must be "
                "non-empty"
            )
        active = self._registry.list_active(correlation_id=correlation_id)
        logger.info(
            "lifecycle_bridge.recover_in_flight correlation_id=%s "
            "in_flight_count=%d",
            correlation_id,
            len(active),
        )
        return active

    # ------------------------------------------------------------------
    # Public API — request_cancel  (TASK-FRR-PEB-007)
    # ------------------------------------------------------------------

    async def request_cancel(self, feature_id: str) -> CancelResult:
        """Ask the langgraph-runner sidecar to interrupt the in-flight build.

        This implements AC-1 of TASK-FRR-PEB-007: it calls
        ``runs.cancel(thread_id, run_id, action="interrupt")`` on the
        injected SDK client and returns immediately. It does **not**
        publish a ``pipeline.build-cancelled`` envelope synchronously —
        envelope emission is deferred to the SSE translator (T3) when
        the sidecar reports ``terminal=interrupted``. This keeps the
        cancel emit site singular and idempotent: two concurrent cancel
        requests for the same build always produce exactly one
        ``build-cancelled`` envelope (FEAT-FORGE-004 contract extended
        to the cancel path).

        Idempotency (AC-5): the bridge tracks a per-feature
        cancel-in-flight set; the second ``request_cancel`` for the same
        ``feature_id`` is a no-op (logged at INFO, no SDK call) and
        returns ``CancelResult(invoked=False)`` so the caller can
        distinguish the freshly-issued cancel from the duplicate.

        Args:
            feature_id: Primary key of the build to cancel. Must match
                a row currently held by the registry — when no row is
                found we still record the cancel-in-flight flag and
                return ``invoked=False`` so a second request races
                against the same no-op state. The serve handler treats
                "no registry row" as "operator-cancel of an unknown
                build" and surfaces the diagnostic separately.

        Returns:
            :class:`CancelResult` describing whether the SDK call was
            issued and the registry-derived ``thread_id`` /``run_id``
            (when available — useful for structured logging on the
            cancelling side).

        Raises:
            ValueError: If ``feature_id`` is empty — same stance as
                :meth:`detach` against an empty primary key.
            RuntimeError: If no SDK client has been wired into the
                bridge but a cancel is requested. Surfaced as a
                ``RuntimeError`` rather than silently no-opping because
                an unwired SDK is a daemon misconfiguration; the serve
                handler logs it and exits non-zero.
        """
        if not feature_id:
            raise ValueError(
                "LifecycleBridge.request_cancel: feature_id must be non-empty"
            )

        # AC-5: idempotency. The second request for the same feature is
        # a no-op. We check *before* the registry lookup so a missing
        # row on the second request can't trip the "no SDK client"
        # branch below — once we've sent one cancel for a feature we
        # are committed to that single emit, even if the registry row
        # has been swept by detach in between.
        if feature_id in self._cancel_in_flight:
            logger.info(
                "lifecycle_bridge.request_cancel feature_id=%s status=no-op "
                "reason=cancel-in-flight",
                feature_id,
            )
            return CancelResult(
                feature_id=feature_id,
                invoked=False,
                reason="already-cancelling",
            )

        # Use the registry's correlation_id for traceability — the
        # registry is the canonical source of truth for the ids tied to
        # this build. We use a synthetic correlation id for the read
        # itself (the cancel handler's correlation context) and then
        # surface the row's stored correlation_id in the structured log.
        cancel_correlation = f"cancel:{feature_id}"
        entry = self._registry.get(feature_id, correlation_id=cancel_correlation)

        if entry is None:
            # No row → there is no live SDK run to cancel. We still
            # mark the feature as cancel-in-flight so a follow-up
            # cancel for the same id is a recognised no-op rather than
            # bouncing off the registry twice.
            self._cancel_in_flight.add(feature_id)
            logger.info(
                "lifecycle_bridge.request_cancel feature_id=%s status=no-op "
                "reason=no-registry-row",
                feature_id,
            )
            return CancelResult(
                feature_id=feature_id,
                invoked=False,
                reason="no-registry-row",
            )

        if self._sdk_client is None:
            raise RuntimeError(
                "LifecycleBridge.request_cancel: no SDK client wired; "
                "construct the bridge with sdk_client= to enable operator "
                f"cancel (feature_id={feature_id!r})"
            )

        # Mark cancel-in-flight *before* the SDK call so a concurrent
        # second request that wins the race short-circuits without
        # duplicating the SDK side. The SDK call itself is idempotent
        # against an already-interrupted run, but we still avoid the
        # extra round-trip.
        self._cancel_in_flight.add(feature_id)
        logger.info(
            "lifecycle_bridge.request_cancel feature_id=%s status=invoking "
            "thread_id=%s run_id=%s correlation_id=%s",
            feature_id,
            entry.thread_id,
            entry.run_id,
            entry.correlation_id,
        )
        try:
            await self._sdk_client.runs.cancel(
                entry.thread_id,
                entry.run_id,
                action="interrupt",
            )
        except Exception:
            # SDK transport / sidecar errors must not leave the
            # cancel-in-flight flag set, otherwise the operator cannot
            # retry the cancel after fixing the sidecar. Roll the flag
            # back and let the exception propagate to the caller.
            self._cancel_in_flight.discard(feature_id)
            logger.exception(
                "lifecycle_bridge.request_cancel feature_id=%s "
                "sdk_cancel_failed", feature_id,
            )
            raise

        return CancelResult(
            feature_id=feature_id,
            invoked=True,
            reason="invoked",
            thread_id=entry.thread_id,
            run_id=entry.run_id,
        )

    # ------------------------------------------------------------------
    # Public API — shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Release the bridge's in-memory bookkeeping.

        T2 deliberately keeps this a clean no-op against the SSE layer:
        ``forge serve`` startup tests can exercise the bridge without
        contacting a live langgraph-runner sidecar. T4 will extend this
        method to close the SSE clients held in :attr:`_attached`.

        The registry rows are NOT deleted here — they are needed across
        process restarts for the recovery sweep. Only the in-memory
        ack-handle map is cleared.
        """
        in_flight = len(self._attached)
        self._attached.clear()
        self._cancel_in_flight.clear()
        logger.info(
            "lifecycle_bridge.shutdown drained_in_memory_attachments=%d",
            in_flight,
        )
