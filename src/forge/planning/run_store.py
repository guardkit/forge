"""SQLite-backed durable store for planning runs (TASK-MP-002).

Public surface
==============

- :class:`SqlitePlanningRunStore` — primary adapter for planning run persistence
- :class:`DuplicateRun` — sentinel returned when correlation_id already exists
- :class:`TransitionRefused` — sentinel returned when state transition is invalid

Design notes
------------

The store enforces state machine transitions via **CAS (compare-and-swap)**:
every ``transition()`` call uses ``UPDATE ... WHERE correlation_id=? AND state=?``
and checks affected rows. If affected==1, the transition succeeded; if ==0,
the transition was refused (either because current state doesn't allow the
requested next state, or because another worker already transitioned).

This is the approve-vs-escalation race arbitration primitive consumed by
TASK-MP-005's PS-005 scenario. The CAS discipline ensures exactly one winner
in concurrent transition attempts.

Every state transition writes a ``planning_run_events`` row carrying the
acting identity (originator on QUEUED, decided_by on checkpoint decisions).
This satisfies AC-003 and provides the history substrate for FEAT-SPL-005
trace-capture continuity.

References
----------
- TASK-MP-002 — this task brief
- TASK-MP-005 — approve-vs-escalation race consumer
- FEAT-SPL-002 — parent feature (Mode P planning chain)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from forge.planning.states import PlanningState, PLANNING_TRANSITIONS


@dataclass(frozen=True)
class DuplicateRun:
    """Sentinel returned when record_queued finds existing correlation_id.

    Distinguishes terminal vs non-terminal existing states for
    TASK-MP-008's RT-10 notification routing.
    """

    existing_state: str
    is_terminal: bool


@dataclass(frozen=True)
class TransitionRefused:
    """Sentinel returned when transition is invalid for current state."""

    current_state: str
    requested_state: str


class SqlitePlanningRunStore:
    """Durable store for planning runs backed by SQLite.

    Thread-safety: Instances are NOT thread-safe. Each thread should
    create its own store instance with its own connection.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Initialize store with a SQLite connection.

        Parameters
        ----------
        connection:
            A writable ``sqlite3.Connection`` with schema v3+ applied.
        """
        self._connection = connection
        # Enable dict-like row access
        self._connection.row_factory = sqlite3.Row

    def record_queued(
        self,
        correlation_id: str,
        originating_user: str,
        expected_approver: str,
        request_text: str,
        triggered_by: str,
        originating_adapter: str | None = None,
        parent_request_id: str | None = None,
        target_repo: str | None = None,
    ) -> DuplicateRun | None:
        """Record a new planning run in QUEUED state.

        Idempotent on correlation_id: if the correlation_id already exists,
        returns a :class:`DuplicateRun` sentinel instead of creating a
        second row.

        Parameters
        ----------
        correlation_id:
            Unique identifier for this planning request.
        originating_user:
            User who initiated the request.
        expected_approver:
            Initial approver designation (updatable via escalation).
        request_text:
            The planning request description.
        triggered_by:
            Source of the request (cli, jarvis, forge-internal, notification-adapter).
        originating_adapter:
            Optional adapter identifier.
        parent_request_id:
            Optional Jarvis dispatch ID.
        target_repo:
            Optional target repository.

        Returns
        -------
        DuplicateRun or None:
            :class:`DuplicateRun` if correlation_id exists, None on success.
        """
        # Check if correlation_id already exists
        existing = self._get_run(correlation_id)
        if existing is not None:
            existing_state = existing["state"]
            is_terminal = existing_state in {
                PlanningState.FAILED.value,
                PlanningState.CANCELLED.value,
                PlanningState.TIMED_OUT.value,
                PlanningState.PLANNED_HANDOFF.value,
            }
            return DuplicateRun(
                existing_state=existing_state,
                is_terminal=is_terminal,
            )

        # Insert new row
        queued_at = datetime.now(timezone.utc).isoformat()
        try:
            self._connection.execute(
                """
                INSERT INTO planning_runs (
                    correlation_id, state, originating_user, expected_approver,
                    request_text, target_repo, triggered_by, originating_adapter,
                    parent_request_id, queued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    PlanningState.QUEUED.value,
                    originating_user,
                    expected_approver,
                    request_text,
                    target_repo,
                    triggered_by,
                    originating_adapter,
                    parent_request_id,
                    queued_at,
                ),
            )
            self._connection.commit()

            # Record the initial QUEUED event
            self._record_event(
                correlation_id=correlation_id,
                stage_label="planning-queued",
                status=PlanningState.QUEUED.value,
                actor_identity=originating_user,
            )

            return None
        except sqlite3.IntegrityError:
            # Race: another worker inserted between our check and insert
            self._connection.rollback()
            existing = self._get_run(correlation_id)
            if existing is not None:
                existing_state = existing["state"]
                is_terminal = existing_state in {
                    PlanningState.FAILED.value,
                    PlanningState.CANCELLED.value,
                    PlanningState.TIMED_OUT.value,
                    PlanningState.PLANNED_HANDOFF.value,
                }
                return DuplicateRun(
                    existing_state=existing_state,
                    is_terminal=is_terminal,
                )
            # Unexpected state — re-raise
            raise

    def transition(
        self,
        correlation_id: str,
        to_state: PlanningState,
        actor_identity: str,
        stage_label: str | None = None,
        gate_mode: str | None = None,
        coach_score: float | None = None,
        details_json: str | None = None,
        error: str | None = None,
        handoff_branch: str | None = None,
        handoff_path: str | None = None,
        expected_from_state: PlanningState | None = None,
    ) -> TransitionRefused | None:
        """Execute a state transition via CAS (compare-and-swap).

        Validates that the transition is allowed by :const:`PLANNING_TRANSITIONS`,
        updates the planning_runs row, and records a planning_run_events entry.

        Terminal states (FAILED, CANCELLED, TIMED_OUT, PLANNED_HANDOFF) set
        ``completed_at`` to the current timestamp.

        Parameters
        ----------
        correlation_id:
            The planning run to transition.
        to_state:
            The target state.
        actor_identity:
            Who/what is performing this transition.
        stage_label:
            Optional stage label for the event row.
        gate_mode:
            Optional gate mode for the event row.
        coach_score:
            Optional coach score for the event row.
        details_json:
            Optional JSON details for the event row.
        error:
            Optional error message (for FAILED transitions).
        handoff_branch:
            Optional handoff branch (for PLANNED_HANDOFF transitions).
        handoff_path:
            Optional handoff path (for PLANNED_HANDOFF transitions).
        expected_from_state:
            Optional expected current state (for explicit CAS checks).

        Returns
        -------
        TransitionRefused or None:
            :class:`TransitionRefused` if transition invalid, None on success.
        """
        # Get current state
        current_row = self._get_run(correlation_id)
        if current_row is None:
            # Run doesn't exist — refuse
            return TransitionRefused(
                current_state="<not found>",
                requested_state=to_state.value,
            )

        current_state = PlanningState(current_row["state"])

        # If expected_from_state is provided, enforce it
        if expected_from_state is not None and current_state != expected_from_state:
            return TransitionRefused(
                current_state=current_state.value,
                requested_state=to_state.value,
            )

        # Check if transition is allowed
        allowed_next_states = PLANNING_TRANSITIONS.get(current_state, set())
        if to_state not in allowed_next_states:
            return TransitionRefused(
                current_state=current_state.value,
                requested_state=to_state.value,
            )

        # CAS update: only succeeds if state hasn't changed
        now = datetime.now(timezone.utc).isoformat()
        update_fields = ["state = ?"]
        update_values: list[Any] = [to_state.value]

        # Set started_at on first transition to RUNNING
        if to_state == PlanningState.RUNNING and current_row["started_at"] is None:
            update_fields.append("started_at = ?")
            update_values.append(now)

        # Set completed_at on terminal transitions
        terminal_states = {
            PlanningState.FAILED,
            PlanningState.CANCELLED,
            PlanningState.TIMED_OUT,
            PlanningState.PLANNED_HANDOFF,
        }
        if to_state in terminal_states:
            update_fields.append("completed_at = ?")
            update_values.append(now)

        # Set error field if provided
        if error is not None:
            update_fields.append("error = ?")
            update_values.append(error)

        # Set handoff fields if provided
        if handoff_branch is not None:
            update_fields.append("handoff_branch = ?")
            update_values.append(handoff_branch)
        if handoff_path is not None:
            update_fields.append("handoff_path = ?")
            update_values.append(handoff_path)

        update_values.extend([correlation_id, current_state.value])

        cursor = self._connection.execute(
            f"""
            UPDATE planning_runs
            SET {', '.join(update_fields)}
            WHERE correlation_id = ? AND state = ?
            """,
            update_values,
        )

        affected = cursor.rowcount
        if affected == 0:
            # CAS failed — state changed between our read and write
            self._connection.rollback()
            refreshed_row = self._get_run(correlation_id)
            refreshed_state = refreshed_row["state"] if refreshed_row else "<not found>"
            return TransitionRefused(
                current_state=refreshed_state,
                requested_state=to_state.value,
            )

        self._connection.commit()

        # Record the transition event
        self._record_event(
            correlation_id=correlation_id,
            stage_label=stage_label or f"transition-to-{to_state.value.lower()}",
            status=to_state.value,
            gate_mode=gate_mode,
            coach_score=coach_score,
            actor_identity=actor_identity,
            details_json=details_json,
        )

        return None

    def update_escalation(
        self,
        correlation_id: str,
        defer_count: int | None = None,
        paused_at: str | None = None,
        escalated_at: str | None = None,
        expected_approver: str | None = None,
    ) -> None:
        """Update escalation-related fields (AC-006).

        Used by RT-04 durable escalation state tracking.

        Parameters
        ----------
        correlation_id:
            The planning run to update.
        defer_count:
            New defer count.
        paused_at:
            Timestamp when run was paused.
        escalated_at:
            Timestamp when run was escalated.
        expected_approver:
            New expected approver designation.
        """
        update_fields = []
        update_values: list[Any] = []

        if defer_count is not None:
            update_fields.append("defer_count = ?")
            update_values.append(defer_count)
        if paused_at is not None:
            update_fields.append("paused_at = ?")
            update_values.append(paused_at)
        if escalated_at is not None:
            update_fields.append("escalated_at = ?")
            update_values.append(escalated_at)
        if expected_approver is not None:
            update_fields.append("expected_approver = ?")
            update_values.append(expected_approver)

        if not update_fields:
            return  # Nothing to update

        update_values.append(correlation_id)

        self._connection.execute(
            f"""
            UPDATE planning_runs
            SET {', '.join(update_fields)}
            WHERE correlation_id = ?
            """,
            update_values,
        )
        self._connection.commit()

    def _get_run(self, correlation_id: str) -> sqlite3.Row | None:
        """Fetch a planning run by correlation_id.

        Returns
        -------
        sqlite3.Row or None:
            The row if found, None otherwise.
        """
        cursor = self._connection.execute(
            "SELECT * FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        )
        return cursor.fetchone()

    def _record_event(
        self,
        correlation_id: str,
        stage_label: str,
        status: str,
        gate_mode: str | None = None,
        coach_score: float | None = None,
        actor_identity: str | None = None,
        details_json: str | None = None,
    ) -> None:
        """Write a planning_run_events row (AC-003).

        Parameters
        ----------
        correlation_id:
            The planning run this event belongs to.
        stage_label:
            Stage label for this event.
        status:
            Status/state value.
        gate_mode:
            Optional gate mode.
        coach_score:
            Optional coach score.
        actor_identity:
            Who/what performed this action.
        details_json:
            Optional JSON details.
        """
        recorded_at = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO planning_run_events (
                correlation_id, stage_label, status, gate_mode, coach_score,
                actor_identity, details_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correlation_id,
                stage_label,
                status,
                gate_mode,
                coach_score,
                actor_identity,
                details_json,
                recorded_at,
            ),
        )
        self._connection.commit()


__all__ = [
    "SqlitePlanningRunStore",
    "DuplicateRun",
    "TransitionRefused",
]
