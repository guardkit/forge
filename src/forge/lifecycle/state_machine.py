"""Lifecycle state machine — transition table and invariants (TASK-PSM-004).

This module is the **sole producer of** :class:`Transition` value objects.
The persistence layer's ``apply_transition()`` (TASK-PSM-005) is the only
caller permitted to write the ``builds.status`` column, and it accepts a
:class:`Transition` produced here. CLI commands (queue / cancel / skip)
and the recovery pass import :func:`transition` from this module to
compose state changes; they never write ``status`` directly.

This is concern **sc_001** in the FEAT-FORGE-001 review — the single most
important architectural rule in the feature. Without a single producer of
state transitions, the cancel/skip handlers, queue command, and recovery
pass could each write status independently and produce illegal states
(e.g. ``COMPLETE → RUNNING``, ``FAILED → COMPLETE``). The transition
table here is the authoritative graph; :func:`transition` enforces it.

Allowed transitions
-------------------

::

    QUEUED       → PREPARING, INTERRUPTED, CANCELLED
    PREPARING    → RUNNING, FAILED, INTERRUPTED, CANCELLED
    RUNNING      → PAUSED, FINALISING, FAILED, INTERRUPTED, CANCELLED, SKIPPED
    PAUSED       → RUNNING, FINALISING, FAILED, CANCELLED, SKIPPED
    FINALISING   → COMPLETE, FAILED, INTERRUPTED
    INTERRUPTED  → QUEUED, PREPARING        # re-pickup after recovery
    COMPLETE     → ()                       # terminal
    FAILED       → ()                       # terminal
    CANCELLED    → ()                       # terminal
    SKIPPED      → ()                       # terminal

Terminal states (COMPLETE / FAILED / CANCELLED / SKIPPED) accept no
outgoing transitions, and any transition *into* a terminal state must
record ``completed_at`` (Group G data-integrity invariant). When a
caller does not pass ``completed_at`` explicitly, :func:`transition`
sets it to the same UTC instant as ``occurred_at``.

References
----------

- TASK-PSM-004 — this task brief.
- TASK-PSM-002 — :mod:`forge.lifecycle.migrations` / ``schema.sql``;
  the ``builds.status CHECK`` mirrors :class:`BuildState`.
- TASK-PSM-005 — ``persistence.apply_transition`` (sole writer).
- TASK-PSM-007 — PAUSED-recovery idempotency consumer of
  ``pending_approval_request_id`` (review finding F4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

# Single source of truth — the BuildState enum lives in pipeline.supervisor.
# This module owns the *transitions*, not the *states*. Re-exporting (rather
# than defining a parallel enum) is the AC "BuildState enum re-exported
# (single source of truth); no parallel definition".
from forge.pipeline.supervisor import BuildState

__all__ = [
    "BuildState",
    "InvalidTransitionError",
    "TERMINAL_STATES",
    "TRANSITION_TABLE",
    "Transition",
    "transition",
    "transition_chain",
]


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

#: The set of terminal states. Any transition into one of these must record
#: ``completed_at`` (Group G data-integrity invariant); no transition out of
#: any of these is permitted (Group C "no resurrection from terminal").
TERMINAL_STATES: Final[frozenset[BuildState]] = frozenset(
    {
        BuildState.COMPLETE,
        BuildState.FAILED,
        BuildState.CANCELLED,
        BuildState.SKIPPED,
    }
)

#: Authoritative state-transition graph. The mapping is built from
#: ``frozenset`` values so the per-state target set cannot be mutated at
#: runtime; the outer mapping is exposed via :func:`MappingProxyType`-free
#: convention (we treat ``Final`` + ``frozenset`` values as the immutability
#: contract, and the property tests assert no state is missing a row).
TRANSITION_TABLE: Final[dict[BuildState, frozenset[BuildState]]] = {
    BuildState.QUEUED: frozenset(
        {
            BuildState.PREPARING,
            BuildState.INTERRUPTED,
            BuildState.CANCELLED,
        }
    ),
    BuildState.PREPARING: frozenset(
        {
            BuildState.RUNNING,
            BuildState.FAILED,
            BuildState.INTERRUPTED,
            BuildState.CANCELLED,
        }
    ),
    BuildState.RUNNING: frozenset(
        {
            BuildState.PAUSED,
            BuildState.FINALISING,
            BuildState.FAILED,
            BuildState.INTERRUPTED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        }
    ),
    BuildState.PAUSED: frozenset(
        {
            BuildState.RUNNING,
            BuildState.FINALISING,
            BuildState.FAILED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        }
    ),
    BuildState.FINALISING: frozenset(
        {
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.INTERRUPTED,
        }
    ),
    BuildState.INTERRUPTED: frozenset(
        {
            BuildState.QUEUED,
            BuildState.PREPARING,
        }
    ),
    BuildState.COMPLETE: frozenset(),
    BuildState.FAILED: frozenset(),
    BuildState.CANCELLED: frozenset(),
    BuildState.SKIPPED: frozenset(),
}


# ---------------------------------------------------------------------------
# Value objects + errors
# ---------------------------------------------------------------------------


class Transition(BaseModel):
    """Immutable record describing a single state change for one build.

    Produced exclusively by :func:`transition` and consumed by the
    persistence layer's ``apply_transition()`` (TASK-PSM-005) — the only
    SQL site allowed to write ``builds.status``.

    Attributes:
        build_id: The build whose state is changing.
        from_state: The build's state prior to the transition. Read off
            the ``Build`` instance at call time and copied here so the
            persistence layer can perform an optimistic-concurrency
            UPDATE keyed on ``(build_id, status = from_state)``.
        to_state: The state being transitioned into. Validated against
            :data:`TRANSITION_TABLE` before the value object is built.
        occurred_at: UTC instant the transition was composed at.
            Recorded verbatim on ``builds.started_at`` /
            ``stage_log.started_at`` depending on caller.
        completed_at: For transitions into a terminal state, the UTC
            instant the build was finalised. Populated automatically by
            :func:`transition` if the caller does not supply one.
        error: Optional error string carried into terminal FAILED
            transitions; recorded on ``builds.error``.
        pr_url: Optional PR URL carried into terminal COMPLETE
            transitions; recorded on ``builds.pr_url``.
        pending_approval_request_id: Set when transitioning into PAUSED
            so that PAUSED-recovery (F4) can re-issue the original
            approval request idempotently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str
    from_state: BuildState
    to_state: BuildState
    occurred_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    pr_url: str | None = None
    pending_approval_request_id: str | None = None


class InvalidTransitionError(ValueError):
    """Raised when a caller attempts a transition not in :data:`TRANSITION_TABLE`.

    Group C invariant: "Invalid lifecycle jump refused". The caller is
    expected to translate this to a structured error response (CLI exit
    code, NATS error reply, etc.); the state machine itself never
    silently corrects an illegal transition.
    """

    def __init__(
        self,
        build_id: str,
        from_state: BuildState,
        to_state: BuildState,
    ) -> None:
        super().__init__(
            f"Invalid transition for {build_id}: "
            f"{from_state.value} -> {to_state.value}"
        )
        self.build_id = build_id
        self.from_state = from_state
        self.to_state = to_state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition(
    build: Any,
    to_state: BuildState,
    **fields: Any,
) -> Transition:
    """Compose and return a validated :class:`Transition` value object.

    Reads the build's current state from ``build.status`` (any object
    with that attribute — typically the ``Build`` Pydantic model from
    TASK-PSM-003), checks the proposed move against
    :data:`TRANSITION_TABLE`, and returns a frozen :class:`Transition`
    on success. Raises :class:`InvalidTransitionError` when the move is
    not in the table.

    Terminal-state transitions populate ``completed_at`` automatically
    when the caller does not pass one; the value mirrors ``occurred_at``
    so a single UTC instant identifies the transition end-to-end.

    Args:
        build: An object exposing ``status: BuildState`` and
            ``build_id: str``. The supervisor's ``Build`` model is the
            production caller.
        to_state: Target :class:`BuildState`. Must appear in
            ``TRANSITION_TABLE[build.status]``.
        **fields: Optional per-transition fields recorded on the
            :class:`Transition` value object — ``error``, ``pr_url``,
            ``pending_approval_request_id``, and an explicit
            ``completed_at`` override. Unknown keys cause Pydantic to
            raise ``ValidationError`` (``extra="forbid"`` on the model).

    Returns:
        The validated :class:`Transition` value object. Pass it directly
        to ``persistence.apply_transition()`` (TASK-PSM-005).

    Raises:
        InvalidTransitionError: ``to_state`` is not reachable from the
            build's current state. The exception carries
            ``(build_id, from_state, to_state)`` for structured logging.
        AttributeError: ``build`` does not expose ``status`` /
            ``build_id``. This is a programmer error — surfaces at the
            boundary rather than silently constructing a bad transition.
    """
    # Boundary validation: required attributes must be present on the
    # build instance. We surface AttributeError at the call site rather
    # than swallowing it so production callers cannot accidentally pass
    # a half-formed object and then write a Transition with empty
    # build_id / placeholder from_state.
    from_state: BuildState = build.status
    build_id: str = build.build_id

    allowed = TRANSITION_TABLE.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidTransitionError(build_id, from_state, to_state)

    now = datetime.now(UTC)
    completed_at = fields.pop("completed_at", None)
    if to_state in TERMINAL_STATES and completed_at is None:
        completed_at = now

    return Transition(
        build_id=build_id,
        from_state=from_state,
        to_state=to_state,
        occurred_at=now,
        completed_at=completed_at,
        **fields,
    )


def transition_chain(
    build: Any,
    to_state: BuildState,
    **fields: Any,
) -> list[Transition]:
    """Compose the shortest legal sequence of Transitions reaching ``to_state``.

    Some observers learn about a build's lifecycle in coarser steps than
    the transition graph permits in one hop — the lifecycle bridge, for
    example, sees ``build-complete`` for a row still in ``QUEUED``
    (legal path: ``QUEUED → PREPARING → RUNNING → FINALISING →
    COMPLETE``). Rather than let such callers hand-roll intermediate
    hops (and drift from :data:`TRANSITION_TABLE`), this helper walks a
    breadth-first shortest path through the authoritative table and
    composes one :class:`Transition` per hop via :func:`transition` —
    preserving the "single producer of Transition value objects"
    invariant (sc_001).

    ``**fields`` (``error``, ``pr_url``, ``completed_at``, …) are
    applied to the **final** hop only; intermediate hops carry no extra
    fields. Apply the returned list in order through
    ``persistence.apply_transition()`` — each hop's ``from_state``
    matches the previous hop's ``to_state``, so the optimistic
    concurrency check composes across the chain.

    Args:
        build: An object exposing ``status: BuildState`` and
            ``build_id: str`` (same contract as :func:`transition`).
        to_state: Target :class:`BuildState`.
        **fields: Optional fields recorded on the final hop's
            :class:`Transition` (see :func:`transition`).

    Returns:
        Transitions in application order. Empty when ``build.status``
        already equals ``to_state`` (idempotent re-delivery no-op).

    Raises:
        InvalidTransitionError: No path exists from ``build.status`` to
            ``to_state`` (e.g. out of a terminal state — Group C "no
            resurrection from terminal").
    """
    from_state: BuildState = build.status
    build_id: str = build.build_id

    if from_state is to_state:
        return []

    # Breadth-first shortest path over TRANSITION_TABLE. Neighbours are
    # expanded in sorted order so the chosen path is deterministic when
    # two paths tie on length.
    predecessor: dict[BuildState, BuildState] = {}
    frontier: list[BuildState] = [from_state]
    seen: set[BuildState] = {from_state}
    while frontier and to_state not in predecessor:
        next_frontier: list[BuildState] = []
        for state in frontier:
            for neighbour in sorted(TRANSITION_TABLE[state]):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                predecessor[neighbour] = state
                next_frontier.append(neighbour)
        frontier = next_frontier

    if to_state not in predecessor:
        raise InvalidTransitionError(build_id, from_state, to_state)

    path: list[BuildState] = [to_state]
    while path[-1] is not from_state:
        path.append(predecessor[path[-1]])
    path.reverse()

    class _Cursor:
        """Minimal ``build``-shaped shim advancing along the path."""

        def __init__(self, status: BuildState) -> None:
            self.build_id = build_id
            self.status = status

    transitions: list[Transition] = []
    for hop_index, hop_target in enumerate(path[1:], start=1):
        hop_fields = fields if hop_index == len(path) - 1 else {}
        transitions.append(
            transition(_Cursor(path[hop_index - 1]), hop_target, **hop_fields)
        )
    return transitions
