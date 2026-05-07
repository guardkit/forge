"""``LifecycleBridge`` — structural skeleton (TASK-FRR-PEB-002).

The bridge owns the SSE connection lifecycle to the langgraph-runner
sidecar. T2 establishes the structural foundation only:

* Public method surface: ``attach``, ``detach``, ``recover_in_flight``,
  ``shutdown``.
* SQLite-backed in-flight registry persistence via
  :class:`forge.persistence.repositories.bridge_registry.BridgeRegistry`.

The methods do *not* yet open SSE connections — that wiring is split
across the follow-up tasks:

* T3 — SSE envelope translation layer (consumes the registry).
* T4 — ``forge serve`` startup wiring (calls :meth:`attach`/:meth:`shutdown`).
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

from forge.lifecycle_bridge.version_check import check_langgraph_runner_version
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
)

logger = logging.getLogger(__name__)


__all__ = [
    "AckHandle",
    "BuildContext",
    "LifecycleBridge",
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
        logger.info(
            "lifecycle_bridge.shutdown drained_in_memory_attachments=%d",
            in_flight,
        )
