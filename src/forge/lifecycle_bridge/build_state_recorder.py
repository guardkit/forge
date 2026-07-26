"""Builds-row write-back for published lifecycle envelopes.

Closes the gap observed on the 2026-07-04 GB10 attended run: the wireup
published ``build-started`` and ``build-complete`` envelopes and acked the
inbound ``build-queued`` message, but no component mapped those envelopes
back onto ``builds.status`` — the row stayed ``QUEUED`` past terminal, so
``forge status`` misreported the build and the Group C "active in-flight
duplicate" check (:meth:`SqliteLifecyclePersistence.exists_active_build`)
wedged every subsequent dispatch for the feature.

The factory here returns a
:data:`~forge.lifecycle_bridge.wireup.BuildStateRecorder` that the wireup
invokes after each *successful* publish (see
:meth:`LifecycleBridgeWireup._record_build_state` for the ordering
rationale). The recorder:

* maps the payload type onto a target :class:`BuildState` via
  :data:`_TARGET_STATE_TABLE` — payload types outside the table
  (``stage-complete``, ``paused``/``resumed``, which belong to the
  approval machinery) are ignored;
* reads the row's current status by ``build_id`` (every mapped payload
  carries one per the ``nats_core.events`` schemas);
* composes the shortest legal hop sequence with
  :func:`forge.lifecycle.state_machine.transition_chain` — the bridge
  observes lifecycles coarser than the transition graph's single hops
  (a fast run goes ``QUEUED`` → ``build-complete`` with no intermediate
  envelope) — and applies each hop through
  :meth:`SqliteLifecyclePersistence.apply_transition`, the sole
  ``builds.status`` writer (sc_001).

Idempotency: a redelivered or replayed envelope whose target state the
row already reached is a silent no-op, and a row already in a terminal
state is never moved (Group C "no resurrection from terminal") — the
CLI cancel path may legitimately win the race against a slow terminal
envelope.

Coach-score store (``schema_v6.sql``): additively, and orthogonally to
the status write-back, the recorder lands the ``coach_score`` carried by
``stage-complete`` / ``build-paused`` envelopes on
``builds.last_coach_score`` (last-wins per envelope). This is the durable
sink for the UBS1C score that the ``min_coach_score`` budget floor reads
back via :meth:`SqliteLifecyclePersistence.read_last_coach_score`. The
score write never touches ``builds.status``, so the status write-back's
early-returns and idempotency guarantees above are unaffected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.lifecycle.state_machine import (
    TERMINAL_STATES,
    BuildState,
    InvalidTransitionError,
    transition_chain,
)

if TYPE_CHECKING:
    from forge.lifecycle.persistence import SqliteLifecyclePersistence
    from forge.lifecycle_bridge.translation import PipelineEvent
    from forge.lifecycle_bridge.wireup import BuildStateRecorder

logger = logging.getLogger(__name__)

__all__ = ["build_build_state_recorder"]


#: Payload type → target builds-row state. Only the four states the
#: bridge is authoritative for; PAUSED/RESUMED stay with the approval
#: machinery (which owns ``pending_approval_request_id`` — F4) and
#: ``stage-complete`` never changes ``builds.status``.
_TARGET_STATE_TABLE: dict[type, BuildState] = {
    BuildStartedPayload: BuildState.RUNNING,
    BuildCompletePayload: BuildState.COMPLETE,
    BuildFailedPayload: BuildState.FAILED,
    BuildCancelledPayload: BuildState.CANCELLED,
}

#: Payload types that carry a ``coach_score`` the recorder lands durably on
#: ``builds.last_coach_score`` (``schema_v6.sql``). These are exactly the two
#: envelopes on which the score survives ``lifecycle_bridge.translation``.
#: ``BuildCompletePayload`` deliberately carries no score, so the terminal
#: envelope cannot be the source — the write must happen per stage-complete /
#: pause, mid-build, which is where the ``min_coach_score`` budget floor reads.
_COACH_SCORE_CARRIERS: tuple[type, ...] = (StageCompletePayload, BuildPausedPayload)


class _RowCursor:
    """Minimal ``build``-shaped view satisfying :func:`transition_chain`."""

    def __init__(self, build_id: str, status: BuildState) -> None:
        self.build_id = build_id
        self.status = status


def _final_hop_fields(event: "PipelineEvent") -> dict[str, str]:
    """Extract per-payload fields recorded on the chain's final hop."""
    fields: dict[str, str] = {}
    if isinstance(event, BuildCompletePayload) and event.pr_url:
        fields["pr_url"] = event.pr_url
    if isinstance(event, BuildFailedPayload) and event.failure_reason:
        fields["error"] = event.failure_reason
    if isinstance(event, BuildCancelledPayload) and event.reason:
        fields["error"] = event.reason
    return fields


def build_build_state_recorder(
    persistence: "SqliteLifecyclePersistence",
) -> "BuildStateRecorder":
    """Return the production :data:`BuildStateRecorder` over ``persistence``.

    Args:
        persistence: The shared :class:`SqliteLifecyclePersistence`
            facade (same writer connection as the rest of the serve
            daemon — the recorder runs on the event loop, so its
            ``BEGIN IMMEDIATE`` blocks never interleave with the
            daemon's other writes).

    Returns:
        An ``async (event) -> None`` callable. Expected failure modes
        (unknown build_id, no legal path, optimistic-concurrency clash
        with the CLI cancel path) are logged at WARNING and swallowed;
        the wireup guards against everything else.
    """

    def _record_coach_score(event: "PipelineEvent") -> None:
        """Land a carried ``coach_score`` on ``builds.last_coach_score``.

        Orthogonal to the status write-back below: it fires for the
        stage-complete / build-paused envelopes (which never move
        ``builds.status``) whenever they carry a non-``None`` score, and
        last-wins per envelope so the column reflects the most recent gated
        stage. A missing build_id or row is a quiet no-op consistent with
        the status path's own guards.
        """
        if not isinstance(event, _COACH_SCORE_CARRIERS):
            return
        score = getattr(event, "coach_score", None)
        if score is None:
            return
        build_id = getattr(event, "build_id", None)
        if not build_id:
            logger.warning(
                "build_state_recorder: %s carries a coach_score but no "
                "build_id; skipping score write-back",
                type(event).__name__,
            )
            return
        persistence.record_last_coach_score(build_id, score)

    async def _record(event: "PipelineEvent") -> None:
        # Score write-back is orthogonal to the status write-back: it runs
        # whether or not the payload maps to a status transition, so it
        # happens before the ``target is None`` early-return that the
        # stage-complete / build-paused envelopes take.
        _record_coach_score(event)

        target = _TARGET_STATE_TABLE.get(type(event))
        if target is None:
            return
        build_id = getattr(event, "build_id", None)
        if not build_id:
            logger.warning(
                "build_state_recorder: %s carries no build_id; skipping " "write-back",
                type(event).__name__,
            )
            return

        with persistence._reader() as cx:
            row = cx.execute(
                "SELECT status FROM builds WHERE build_id = ?",
                (build_id,),
            ).fetchone()
        if row is None:
            logger.warning(
                "build_state_recorder: no builds row for build_id=%s "
                "(payload_type=%s); skipping write-back",
                build_id,
                type(event).__name__,
            )
            return

        current = BuildState(row["status"])
        if current is target:
            return
        if current in TERMINAL_STATES:
            # Another writer (CLI cancel, recovery) finalised the row
            # first. Terminal states accept no outgoing transitions —
            # respect the earlier verdict rather than fighting it.
            logger.info(
                "build_state_recorder: builds row %s already terminal "
                "(%s); ignoring published %s",
                build_id,
                current.value,
                type(event).__name__,
            )
            return

        try:
            chain = transition_chain(
                _RowCursor(build_id, current),
                target,
                **_final_hop_fields(event),
            )
        except InvalidTransitionError:
            logger.warning(
                "build_state_recorder: no legal path %s -> %s for "
                "build_id=%s (payload_type=%s); skipping write-back",
                current.value,
                target.value,
                build_id,
                type(event).__name__,
            )
            return

        for hop in chain:
            try:
                persistence.apply_transition(hop)
            except RuntimeError as exc:
                # Optimistic-concurrency miss: another writer moved the
                # row between our read and this hop's UPDATE. Stop —
                # the next envelope (or the operator) re-reads fresh
                # state; partially-applied hops are themselves legal
                # states.
                logger.warning(
                    "build_state_recorder: transition %s -> %s refused "
                    "for build_id=%s (%s); leaving row as-is",
                    hop.from_state.value,
                    hop.to_state.value,
                    build_id,
                    exc,
                )
                return

    return _record
