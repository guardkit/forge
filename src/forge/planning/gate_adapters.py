"""Planning-backed gate protocol adapters (TASK-MP-004A).

Implementations of :class:`forge.gating.wrappers.GateRepository` and
:class:`forge.gating.wrappers.StateMachine` Protocols over
:class:`SqlitePlanningRunStore`, mirroring the architecture of
:mod:`forge.gating.sqlite_adapters`.

These adapters allow gate primitives from :mod:`forge.gating` to operate on
planning runs without requiring a builds row — the gating module itself remains
unchanged.

Architecture
------------

* **Run ID namespacing**: Run IDs on the wire are namespaced ``plan-{correlation_id}``
  (ARCH-007) so approval subjects (``agents.approval.forge.{run_id}``) and request IDs
  never collide with build IDs.

* **Pause handoff**: Similar to sqlite_adapters.py, a shared ``_PauseHandoff`` bridges
  ``record_paused_build`` (repository) and ``transition_to_paused`` (state machine)
  so the ``request_id`` and ``PAUSED`` transition stay atomic.

* **CAS transitions**: All state changes delegate to the store's compare-and-swap
  transitions; refused transitions are softened to no-ops (never raise).

References
----------
- TASK-MP-004A — this task brief
- TASK-MP-002 — SqlitePlanningRunStore foundation
- ARCH-007 — run ID namespacing convention
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from forge.gating.degraded import degraded_recovery_decision
from forge.gating.identity import parse_request_id
from forge.gating.models import GateDecision
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.gating.wrappers import (
        GateRepository,
        PausedBuildSnapshot,
        StateMachine,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "PlanningGateRepository",
    "PlanningStateMachine",
    "build_planning_gate_adapters",
]

# Terminal states that accept no outgoing transitions
_TERMINAL_STATES = {
    PlanningState.FAILED,
    PlanningState.CANCELLED,
    PlanningState.TIMED_OUT,
    PlanningState.PLANNED_HANDOFF,
}


@dataclass
class _PauseHandoff:
    """One-slot-per-run carrier bridging ``record_paused_build`` → SM.

    Mirrors the pattern from :class:`forge.gating.sqlite_adapters._PauseHandoff`.
    ``record_paused_build`` receives the ``request_id`` and stashes it here;
    ``transition_to_paused`` pops it and threads it into the pause logic so
    the PAUSED transition and the ``pending_approval_request_id`` write stay
    atomic.
    """

    _pending: dict[str, str] = field(default_factory=dict)

    def stash(self, run_id: str, request_id: str) -> None:
        """Stash a request_id for later retrieval during pause transition."""
        self._pending[run_id] = request_id

    def pop(self, run_id: str) -> str:
        """Pop the stashed request_id for this run_id."""
        try:
            return self._pending.pop(run_id)
        except KeyError as exc:  # pragma: no cover - programmer-error guard
            raise RuntimeError(
                f"pause handoff empty for run_id={run_id!r}: "
                "transition_to_paused was called without a preceding "
                "record_paused_build"
            ) from exc


def _extract_correlation_id(run_id: str) -> str:
    """Extract correlation_id from namespaced run_id.

    Run IDs on the wire are ``plan-{correlation_id}`` per ARCH-007.
    This function strips the ``plan-`` prefix to get the underlying
    correlation_id.

    Args:
        run_id: Wire-format run ID (e.g., ``plan-abc-123``).

    Returns:
        Underlying correlation_id (e.g., ``abc-123``).

    Raises:
        ValueError: If run_id doesn't start with ``plan-``.
    """
    if not run_id.startswith("plan-"):
        raise ValueError(
            f"run_id must be namespaced with 'plan-' prefix, got: {run_id!r}"
        )
    return run_id[5:]  # Strip "plan-" prefix


def _build_run_id(correlation_id: str) -> str:
    """Build wire-format run_id from correlation_id.

    Args:
        correlation_id: Internal correlation ID.

    Returns:
        Namespaced run ID (``plan-{correlation_id}``).
    """
    return f"plan-{correlation_id}"


# ---------------------------------------------------------------------------
# Repository — owns planning_run_events only.
# ---------------------------------------------------------------------------


class PlanningGateRepository:
    """Planning-backed :class:`forge.gating.wrappers.GateRepository`.

    Owns ``planning_run_events`` writes exclusively. Every
    ``planning_runs.state`` write is delegated to the paired state machine.
    """

    def __init__(
        self,
        store: SqlitePlanningRunStore,
        *,
        clock: Callable[[], datetime],
        handoff: _PauseHandoff | None = None,
    ) -> None:
        """Initialize repository.

        Args:
            store: The SqlitePlanningRunStore for persistence.
            clock: Injected ``() -> datetime`` (UTC) for timestamps.
            handoff: Optional pause handoff for coordinating with state machine.
                If None, creates a new one (used by factory).
        """
        self._store = store
        self._clock = clock
        self._handoff = handoff or _PauseHandoff()

    # -- decision persistence ------------------------------------------

    async def record_decision(self, decision: GateDecision) -> None:
        """Record a gate decision as a planning_run_events row.

        Writes an event row carrying the full ``GateDecision`` snapshot
        so a later rearm can rehydrate the decision verbatim.

        Args:
            decision: The gate decision to record.
        """
        correlation_id = _extract_correlation_id(decision.build_id)

        # Write event row with gate metadata
        self._store._record_event(
            correlation_id=correlation_id,
            stage_label=decision.stage_label,
            status="GATED",
            gate_mode=decision.mode.value,
            coach_score=decision.coach_score,
            actor_identity="gate-check",
            details_json=json.dumps({"gate": decision.model_dump(mode="json")}),
        )

    async def write_to_graphiti(self, decision: GateDecision) -> None:
        """Best-effort Graphiti mirror — no-op in planning adapter.

        Matches the sqlite_adapters behaviour. A real Graphiti write-side
        adapter may replace this seam later.
        """
        return None

    # -- pause bookkeeping ---------------------------------------------

    async def record_paused_build(
        self,
        *,
        build_id: str,
        feature_id: str,
        stage_label: str,
        request_id: str,
        attempt_count: int,
        decision: GateDecision,
    ) -> None:
        """Record the pause event and route the ``request_id`` correctly.

        * **Initial pause** (row not yet PAUSED): stash the ``request_id``
          on the handoff for the imminent ``transition_to_paused``.
        * **Defer re-publish** (row already PAUSED): refresh the pending id
          in place (status-preserving refresh, no transition).

        Either way, append a ``gate_pause`` event row as the durable audit
        home for ``(request_id, attempt_count, feature_id, paused_at)``.

        Args:
            build_id: Namespaced run ID (``plan-{correlation_id}``).
            feature_id: Feature identifier for tracing.
            stage_label: Stage label for this gate.
            request_id: Deterministic request ID for wire dedup.
            attempt_count: Defer counter for this pause.
            decision: The gate decision that motivated the pause.
        """
        correlation_id = _extract_correlation_id(build_id)
        current_state = self._read_state(correlation_id)

        if current_state is PlanningState.PAUSED:
            # Defer round-trip: status-preserving refresh, no transition
            self._refresh_pending_approval_request_id(correlation_id, request_id)
        else:
            # Initial pause: stash for handoff
            self._handoff.stash(build_id, request_id)

        # Update paused_at timestamp and pending_approval_request_id
        now = self._clock()
        conn = self._store._connection
        conn.execute(
            """
            UPDATE planning_runs
            SET paused_at = ?,
                pending_approval_request_id = ?
            WHERE correlation_id = ?
            """,
            (now.isoformat(), request_id, correlation_id),
        )
        conn.commit()

        # Write audit event
        self._store._record_event(
            correlation_id=correlation_id,
            stage_label=stage_label,
            status="GATED",
            gate_mode=decision.mode.value,
            coach_score=decision.coach_score,
            actor_identity="gate-check",
            details_json=json.dumps(
                {
                    "gate_pause": {
                        "request_id": request_id,
                        "attempt_count": attempt_count,
                        "feature_id": feature_id,
                    }
                }
            ),
        )

    async def list_paused_builds(self) -> list[PausedBuildSnapshot]:
        """Reconstruct :class:`PausedBuildSnapshot` rows from planning_runs.

        Queries planning_runs filtered to PAUSED state. ``stage_label`` /
        ``attempt_count`` come from parsing the persisted
        ``pending_approval_request_id``; ``decision_snapshot`` is rehydrated
        from event details with a degraded fallback for corrupt/legacy rows.

        Returns:
            List of PausedBuildSnapshot objects for all paused planning runs.
        """
        # Late import breaks the wrappers <-> gate_adapters cycle
        from forge.gating.wrappers import PausedBuildSnapshot

        snapshots: list[PausedBuildSnapshot] = []
        conn = self._store._connection
        rows = conn.execute(
            """
            SELECT correlation_id, expected_approver, pending_approval_request_id,
                   paused_at, escalated_at
            FROM planning_runs
            WHERE state = ?
            """,
            (PlanningState.PAUSED.value,),
        ).fetchall()

        for row in rows:
            correlation_id = row[0]
            request_id = row[2]

            if not request_id:
                logger.warning(
                    "list_paused_builds: PAUSED run %s has no "
                    "pending_approval_request_id; skipping corrupt row",
                    correlation_id,
                )
                continue

            try:
                _rid, stage_label, attempt_count = parse_request_id(request_id)
            except ValueError:
                logger.error(
                    "list_paused_builds: unparseable request_id=%r for "
                    "correlation_id=%s; skipping (legacy / corrupt id)",
                    request_id,
                    correlation_id,
                )
                continue

            decision = self._rehydrate_decision(correlation_id, stage_label)
            feature_id = self._extract_feature_id(correlation_id)
            snapshots.append(
                PausedBuildSnapshot(
                    build_id=_build_run_id(correlation_id),
                    feature_id=feature_id,
                    stage_label=stage_label,
                    request_id=request_id,
                    attempt_count=attempt_count,
                    decision_snapshot=decision,
                    correlation_id=correlation_id,
                )
            )

        return snapshots

    def _extract_feature_id(self, correlation_id: str) -> str:
        """Extract feature_id from gate_pause details.

        Args:
            correlation_id: The planning run ID.

        Returns:
            The feature_id from gate_pause details, or "FEAT-SPL-002" as fallback.
        """
        conn = self._store._connection
        events = conn.execute(
            """
            SELECT details_json FROM planning_run_events
            WHERE correlation_id = ?
            ORDER BY recorded_at ASC
            """,
            (correlation_id,),
        ).fetchall()

        for (details_json,) in events:
            if not details_json:
                continue
            try:
                details = json.loads(details_json)
                gate_pause = details.get("gate_pause")
                if isinstance(gate_pause, dict):
                    feature_id = gate_pause.get("feature_id")
                    if feature_id:
                        return feature_id
            except (json.JSONDecodeError, ValueError):
                continue

        # Fallback to planning feature ID
        return "FEAT-SPL-002"

    def _rehydrate_decision(
        self, correlation_id: str, stage_label: str
    ) -> GateDecision:
        """Return the persisted decision, or a degraded fallback.

        Args:
            correlation_id: The planning run ID.
            stage_label: The stage label for degraded fallback.

        Returns:
            Rehydrated GateDecision or degraded fallback.
        """
        conn = self._store._connection
        events = conn.execute(
            """
            SELECT details_json FROM planning_run_events
            WHERE correlation_id = ?
            ORDER BY recorded_at ASC
            """,
            (correlation_id,),
        ).fetchall()

        gate_dump: dict | None = None
        for (details_json,) in events:
            if not details_json:
                continue
            try:
                details = json.loads(details_json)
                snapshot = details.get("gate")
                if isinstance(snapshot, dict):
                    gate_dump = snapshot  # Last match wins
            except (json.JSONDecodeError, ValueError):
                continue

        if gate_dump is not None:
            try:
                return GateDecision.model_validate(gate_dump)
            except Exception:  # noqa: BLE001 — corrupt snapshot → degraded
                logger.warning(
                    "list_paused_builds: correlation_id=%s has a malformed gate "
                    "snapshot; falling back to a degraded decision",
                    correlation_id,
                )

        return degraded_recovery_decision(
            build_id=_build_run_id(correlation_id),
            stage_label=stage_label,
            decided_at=self._clock(),
        )

    # -- resume / override / cancel ------------------------------------

    async def mark_resumed(self, *, build_id: str, stage_label: str) -> None:
        """No-op — the state machine's ``transition_to_running`` owns resume."""
        return None

    async def mark_overridden(
        self, *, build_id: str, stage_label: str, reason: str
    ) -> None:
        """Record the stage override as an event row.

        Args:
            build_id: Namespaced run ID.
            stage_label: Stage being overridden.
            reason: Override reason.
        """
        correlation_id = _extract_correlation_id(build_id)
        self._store._record_event(
            correlation_id=correlation_id,
            stage_label=stage_label,
            status="OVERRIDDEN",
            actor_identity="user-override",
            details_json=json.dumps({"override_reason": reason}),
        )

    async def mark_cancelled(self, *, build_id: str, reason: str) -> None:
        """Genuine no-op — ``transition_to_cancelled`` is the sole cancel writer.

        Args:
            build_id: Namespaced run ID.
            reason: Cancellation reason (unused).
        """
        return None

    # -- helpers -------------------------------------------------------

    def _read_state(self, correlation_id: str) -> PlanningState:
        """Read current state for a planning run.

        Args:
            correlation_id: The planning run ID.

        Returns:
            Current PlanningState.

        Raises:
            RuntimeError: If no row found.
        """
        conn = self._store._connection
        row = conn.execute(
            "SELECT state FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"no planning_runs row for correlation_id={correlation_id!r}"
            )
        return PlanningState(row[0])

    def _refresh_pending_approval_request_id(
        self, correlation_id: str, request_id: str
    ) -> None:
        """Refresh pending_approval_request_id for a PAUSED run.

        Args:
            correlation_id: The planning run ID.
            request_id: New request ID to store.
        """
        conn = self._store._connection
        conn.execute(
            """
            UPDATE planning_runs
            SET pending_approval_request_id = ?
            WHERE correlation_id = ? AND state = ?
            """,
            (request_id, correlation_id, PlanningState.PAUSED.value),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# State machine — owns planning_runs.state only.
# ---------------------------------------------------------------------------


class PlanningStateMachine:
    """Planning-backed :class:`forge.gating.wrappers.StateMachine`.

    Sole writer of ``planning_runs.state`` for the gate flow. Composes
    :meth:`SqlitePlanningRunStore.transition` for all state changes.
    """

    def __init__(
        self,
        store: SqlitePlanningRunStore,
        *,
        handoff: _PauseHandoff | None = None,
    ) -> None:
        """Initialize state machine.

        Args:
            store: The SqlitePlanningRunStore for persistence.
            handoff: Optional pause handoff for coordinating with repository.
                If None, creates a new one (used by factory).
        """
        self._store = store
        self._handoff = handoff or _PauseHandoff()

    async def transition_to_paused(self, *, build_id: str, stage_label: str) -> None:
        """Pop the handoff ``request_id`` and pause atomically.

        Args:
            build_id: Namespaced run ID (``plan-{correlation_id}``).
            stage_label: Stage label for the pause event.
        """
        correlation_id = _extract_correlation_id(build_id)
        # Pop the request_id from handoff (validates handoff was called)
        _request_id = self._handoff.pop(build_id)

        result = self._store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.PAUSED,
            actor_identity="gate-check",
            stage_label=stage_label,
        )

        if isinstance(result, TransitionRefused):
            logger.warning(
                "transition_to_paused: transition refused for %s "
                "(current=%s, requested=%s); no-op",
                correlation_id,
                result.current_state,
                result.requested_state,
            )

    async def transition_to_running(self, *, build_id: str) -> None:
        """Resume PAUSED → RUNNING.

        Args:
            build_id: Namespaced run ID (``plan-{correlation_id}``).
        """
        correlation_id = _extract_correlation_id(build_id)
        current = self._read_state(correlation_id)

        if current in _TERMINAL_STATES:
            logger.warning(
                "transition_to_running: %s already terminal (%s); no-op",
                correlation_id,
                current.value,
            )
            return

        if current is PlanningState.RUNNING:
            # Idempotent double-approve
            return

        result = self._store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="approval-system",
        )

        if isinstance(result, TransitionRefused):
            # Check if concurrent writer made it terminal
            latest = self._read_state(correlation_id)
            if latest in _TERMINAL_STATES:
                logger.warning(
                    "transition_to_running: %s went terminal (%s) "
                    "under concurrent writer; no-op",
                    correlation_id,
                    latest.value,
                )
                return
            # Otherwise log and no-op
            logger.warning(
                "transition_to_running: transition refused for %s "
                "(current=%s, requested=%s)",
                correlation_id,
                result.current_state,
                result.requested_state,
            )

    async def transition_to_failed(self, *, build_id: str, reason: str) -> None:
        """Transition the current state → FAILED.

        Args:
            build_id: Namespaced run ID (``plan-{correlation_id}``).
            reason: Failure reason.
        """
        correlation_id = _extract_correlation_id(build_id)
        current = self._read_state(correlation_id)

        if current in _TERMINAL_STATES:
            logger.warning(
                "transition_to_failed: %s already terminal (%s); no-op",
                correlation_id,
                current.value,
            )
            return

        result = self._store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FAILED,
            actor_identity="gate-check",
            error=reason,
        )

        if isinstance(result, TransitionRefused):
            logger.warning(
                "transition_to_failed: transition refused for %s "
                "(current=%s, requested=%s)",
                correlation_id,
                result.current_state,
                result.requested_state,
            )

    async def transition_to_cancelled(self, *, build_id: str, reason: str) -> None:
        """Cancel the run — the SOLE cancel writer.

        Does not raise on stale transitions (already terminal), instead
        softens to a warning.

        Args:
            build_id: Namespaced run ID (``plan-{correlation_id}``).
            reason: Cancellation reason.
        """
        correlation_id = _extract_correlation_id(build_id)
        current = self._read_state(correlation_id)

        if current in _TERMINAL_STATES:
            logger.warning(
                "transition_to_cancelled: %s already terminal (%s); no-op",
                correlation_id,
                current.value,
            )
            return

        result = self._store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.CANCELLED,
            actor_identity="user-cancel",
            error=reason,
        )

        if isinstance(result, TransitionRefused):
            logger.warning(
                "transition_to_cancelled: transition refused for %s "
                "(current=%s, requested=%s)",
                correlation_id,
                result.current_state,
                result.requested_state,
            )

    # -- helpers -------------------------------------------------------

    def _read_state(self, correlation_id: str) -> PlanningState:
        """Read current state for a planning run.

        Args:
            correlation_id: The planning run ID.

        Returns:
            Current PlanningState.

        Raises:
            RuntimeError: If no row found.
        """
        conn = self._store._connection
        row = conn.execute(
            "SELECT state FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"no planning_runs row for correlation_id={correlation_id!r}"
            )
        return PlanningState(row[0])


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------


def build_planning_gate_adapters(
    store: SqlitePlanningRunStore,
    *,
    clock: Callable[[], datetime],
) -> tuple[GateRepository, StateMachine]:
    """Build the ``(repository, state_machine)`` gate-adapter pair.

    Both adapters compose the shared ``store`` and a shared
    :class:`_PauseHandoff` so ``record_paused_build`` (repository) and
    ``transition_to_paused`` (state machine) exchange the ``request_id``.

    Args:
        store: The shared :class:`SqlitePlanningRunStore` for persistence.
        clock: Injected ``() -> datetime`` (UTC) used to stamp event
            rows and degraded-fallback ``decided_at``. Clock hygiene —
            never ``datetime.now()``.

    Returns:
        A ``(repository, state_machine)`` tuple satisfying the
        :class:`forge.gating.wrappers.GateRepository` /
        :class:`forge.gating.wrappers.StateMachine` Protocols.
    """
    handoff = _PauseHandoff()
    repository = PlanningGateRepository(store, clock=clock, handoff=handoff)
    state_machine = PlanningStateMachine(store, handoff=handoff)
    return repository, state_machine
