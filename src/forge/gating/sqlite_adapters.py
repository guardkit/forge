"""SQLite-backed gate adapters (TASK-GATE-D659, Wave 1 / plan §D3).

:func:`build_sqlite_gate_adapters` returns a ``(repository, state_machine)``
pair that satisfies the :class:`forge.gating.wrappers.GateRepository` and
:class:`forge.gating.wrappers.StateMachine` Protocols by **composing** the
already-tested SQLite facades in :mod:`forge.lifecycle.persistence` — it
never re-implements SQL. The pair is behaviourally substitutable for the
in-memory fakes (``tests/integration/conftest.py`` ``InMemoryRepository`` /
``InMemoryStateMachine``) so the live ``gate_check`` path runs unchanged
against real SQLite.

Two architectural rules the design leans on (plan §D3, arch-review M1):

* **Single-transition-owner.** The state machine owns *every*
  ``builds.status`` write (through ``apply_transition`` / ``mark_paused`` /
  ``SqliteBuildCanceller``). The repository owns ``stage_log`` only — its
  ``mark_resumed`` and ``mark_cancelled`` are genuine no-ops so the two
  cancel orderings in ``gate_check`` still produce exactly one status
  write.
* **Pause handoff.** ``record_paused_build`` carries the ``request_id`` but
  the state machine's ``transition_to_paused`` does not — a shared
  :class:`_PauseHandoff` bridges them: the repository stashes the id, the
  state machine pops it and hands it to ``mark_paused``.

:class:`StaleTransitionError` is raised by ``transition_to_cancelled`` when
the row is *already* terminal — **before** any wire publish — so a
concurrent CLI-cancel that beat the gate does not produce a second cancel
and a mis-emitted ``build-failed``. ``await_and_dispatch`` (in
:mod:`forge.gating.wrappers`) catches it and softens to a WARNING.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from forge.gating.degraded import degraded_recovery_decision
from forge.gating.identity import parse_request_id
from forge.gating.models import GateDecision
from forge.lifecycle.persistence import (
    Build,
    SqliteBuildCanceller,
    SqliteLifecyclePersistence,
    SqliteStageSkipRecorder,
    StageLogEntry,
)
from forge.lifecycle.state_machine import (
    TERMINAL_STATES,
    BuildState,
    transition as compose_transition,
)
from forge.pipeline.stage_taxonomy import StageClass

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids an import cycle)
    from forge.gating.wrappers import (
        GateRepository,
        PausedBuildSnapshot,
        StateMachine,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "StaleTransitionError",
    "build_sqlite_gate_adapters",
]

#: ``stage_log.details_json`` key that holds the durable
#: ``GateDecision.model_dump`` snapshot (first written by
#: :meth:`_SqliteGateRepository.record_decision`).
_GATE_DETAILS_KEY: str = "gate"

#: ``stage_log.details_json`` key holding the pause bookkeeping
#: (request_id / attempt_count / feature_id) — the durable audit home for
#: the pause event.
_GATE_PAUSE_DETAILS_KEY: str = "gate_pause"


class StaleTransitionError(Exception):
    """Raised when a state transition targets an already-terminal row.

    Grafted from the bridge design. ``transition_to_cancelled`` raises this
    **before** the JNB-102 ``build-cancelled`` publish so a cancel that
    lost the race to a concurrent terminal (CLI-cancel, prior reject) does
    not double-emit. ``await_and_dispatch`` catches it on both cancel legs
    and returns without raising (softening posture).
    """

    def __init__(self, build_id: str, current_state: BuildState) -> None:
        super().__init__(
            f"transition refused: build_id={build_id!r} is already terminal "
            f"({current_state.value})"
        )
        self.build_id = build_id
        self.current_state = current_state


@dataclass
class _PauseHandoff:
    """One-slot-per-build carrier bridging ``record_paused_build`` → SM.

    ``record_paused_build`` receives the ``request_id`` and stashes it here;
    ``transition_to_paused`` pops it and threads it into ``mark_paused`` so
    the PAUSED transition and the ``pending_approval_request_id`` write stay
    atomic (a single ``apply_transition``).
    """

    _pending: dict[str, str] = field(default_factory=dict)

    def stash(self, build_id: str, request_id: str) -> None:
        self._pending[build_id] = request_id

    def pop(self, build_id: str) -> str:
        try:
            return self._pending.pop(build_id)
        except KeyError as exc:  # pragma: no cover - programmer-error guard
            raise RuntimeError(
                "pause handoff empty for build_id="
                f"{build_id!r}: transition_to_paused was called without a "
                "preceding record_paused_build"
            ) from exc


# ---------------------------------------------------------------------------
# Repository — owns stage_log only.
# ---------------------------------------------------------------------------


class _SqliteGateRepository:
    """SQLite-backed :class:`forge.gating.wrappers.GateRepository`.

    Owns ``stage_log`` writes exclusively. Every ``builds.status`` write is
    delegated to the paired state machine, so ``mark_resumed`` and
    ``mark_cancelled`` are deliberately no-ops (the SM's transitions are the
    sole writers — arch-review M1).
    """

    def __init__(
        self,
        pool: SqliteLifecyclePersistence,
        *,
        clock: Callable[[], datetime],
        handoff: _PauseHandoff,
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._handoff = handoff
        self._skip_recorder = SqliteStageSkipRecorder(pool)

    # -- decision persistence ------------------------------------------

    async def record_decision(self, decision: GateDecision) -> None:
        """First-ever writer of ``stage_log.details_json["gate"]``.

        Appends a ``GATED`` stage_log row carrying the full
        ``GateDecision`` snapshot (``model_dump`` round-trip) so a later
        rearm can rehydrate the decision verbatim.
        """
        now = self._clock()
        entry = StageLogEntry(
            build_id=decision.build_id,
            stage_label=decision.stage_label,
            target_kind=decision.target_kind,
            target_identifier=decision.target_identifier,
            status="GATED",
            gate_mode=decision.mode.value,
            coach_score=decision.coach_score,
            threshold_applied=decision.threshold_applied,
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={_GATE_DETAILS_KEY: decision.model_dump(mode="json")},
        )
        self._pool.record_stage(entry)

    async def write_to_graphiti(self, decision: GateDecision) -> None:
        """Best-effort Graphiti mirror — no-op in the SQLite adapter.

        Matches the in-memory fake's capture-only behaviour. A real
        Graphiti write-side adapter later replaces this seam; SQLite stays
        the source of truth (F10), so a missing Graphiti write is never a
        rollback trigger.
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
        * **Defer re-publish** (row already PAUSED): the build never left
          PAUSED, so there is no transition — refresh the pending id in
          place via :meth:`SqliteLifecyclePersistence.refresh_pending_approval_request_id`.

        Either way, append a ``gate_pause`` stage_log row as the durable
        audit home for ``(request_id, attempt_count, feature_id)``.
        """
        current = self._read_status(build_id)
        if current is BuildState.PAUSED:
            # Defer round-trip: status-preserving refresh, no transition.
            self._pool.refresh_pending_approval_request_id(build_id, request_id)
        else:
            self._handoff.stash(build_id, request_id)

        now = self._clock()
        entry = StageLogEntry(
            build_id=build_id,
            stage_label=stage_label,
            target_kind=decision.target_kind,
            target_identifier=decision.target_identifier,
            status="GATED",
            gate_mode=decision.mode.value,
            coach_score=decision.coach_score,
            threshold_applied=decision.threshold_applied,
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={
                _GATE_PAUSE_DETAILS_KEY: {
                    "request_id": request_id,
                    "attempt_count": attempt_count,
                    "feature_id": feature_id,
                }
            },
        )
        self._pool.record_stage(entry)

    async def list_paused_builds(self) -> list[PausedBuildSnapshot]:
        """Reconstruct :class:`PausedBuildSnapshot` rows from status + stage_log.

        Status-backed: ``read_non_terminal_builds`` filtered to PAUSED.
        ``stage_label`` / ``attempt_count`` come from parsing the persisted
        ``pending_approval_request_id`` (the durable home, by design);
        ``decision_snapshot`` is rehydrated from
        ``stage_log.details_json["gate"]`` with a degraded fallback for a
        corrupt / legacy row.
        """
        # Late import breaks the wrappers <-> sqlite_adapters cycle
        # (wrappers imports StaleTransitionError from this module).
        from forge.gating.wrappers import PausedBuildSnapshot

        snapshots: list[PausedBuildSnapshot] = []
        for row in self._pool.read_non_terminal_builds():
            if row.status is not BuildState.PAUSED:
                continue
            request_id = row.pending_approval_request_id
            if not request_id:
                logger.warning(
                    "list_paused_builds: PAUSED build_id=%s has no "
                    "pending_approval_request_id; skipping corrupt row",
                    row.build_id,
                )
                continue
            try:
                _bid, stage_label, attempt_count = parse_request_id(request_id)
            except ValueError:
                logger.error(
                    "list_paused_builds: unparseable request_id=%r for "
                    "build_id=%s; skipping (legacy / corrupt id)",
                    request_id,
                    row.build_id,
                )
                continue

            decision = self._rehydrate_decision(row.build_id, stage_label)
            snapshots.append(
                PausedBuildSnapshot(
                    build_id=row.build_id,
                    feature_id=row.feature_id,
                    stage_label=stage_label,
                    request_id=request_id,
                    attempt_count=attempt_count,
                    decision_snapshot=decision,
                    correlation_id=row.correlation_id,
                )
            )
        return snapshots

    def _rehydrate_decision(self, build_id: str, stage_label: str) -> GateDecision:
        """Return the persisted decision, or a degraded fallback."""
        gate_dump: dict | None = None
        for entry in self._pool.read_stages(build_id):
            snapshot = entry.details.get(_GATE_DETAILS_KEY)
            if isinstance(snapshot, dict):
                # read_stages is chronological (ASC); the last match wins.
                gate_dump = snapshot
        if gate_dump is not None:
            try:
                return GateDecision.model_validate(gate_dump)
            except Exception:  # noqa: BLE001 — corrupt snapshot → degraded
                logger.warning(
                    "list_paused_builds: build_id=%s has a malformed gate "
                    "snapshot; falling back to a degraded decision",
                    build_id,
                )
        return degraded_recovery_decision(
            build_id=build_id,
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
        """Record the stage override as a SKIPPED stage_log row."""
        self._skip_recorder.record_skipped(
            build_id, StageClass(stage_label), reason
        )

    async def mark_cancelled(self, *, build_id: str, reason: str) -> None:
        """Genuine no-op — ``transition_to_cancelled`` is the sole cancel writer.

        Arch-review M1: the single-transition-owner rule holds *by
        construction* rather than by leaning on the canceller's
        idempotency, so the two ``gate_check`` cancel orderings each produce
        exactly one ``apply_transition``.
        """
        return None

    # -- helpers -------------------------------------------------------

    def _read_status(self, build_id: str) -> BuildState:
        return _read_status(self._pool, build_id)


# ---------------------------------------------------------------------------
# State machine — owns builds.status only.
# ---------------------------------------------------------------------------


class _SqliteStateMachine:
    """SQLite-backed :class:`forge.gating.wrappers.StateMachine`.

    Sole writer of ``builds.status`` for the gate flow. Composes
    :meth:`SqliteLifecyclePersistence.mark_paused`,
    :meth:`SqliteLifecyclePersistence.apply_transition`, and
    :class:`SqliteBuildCanceller`.
    """

    def __init__(
        self,
        pool: SqliteLifecyclePersistence,
        *,
        handoff: _PauseHandoff,
    ) -> None:
        self._pool = pool
        self._handoff = handoff
        self._canceller = SqliteBuildCanceller(pool)

    async def transition_to_paused(self, *, build_id: str, stage_label: str) -> None:
        """Pop the handoff ``request_id`` and pause atomically."""
        request_id = self._handoff.pop(build_id)
        self._pool.mark_paused(build_id, request_id)

    async def transition_to_running(self, *, build_id: str) -> None:
        """Resume PAUSED → RUNNING (auto-clears ``pending_approval_request_id``).

        ``apply_transition`` writes ``pending_approval_request_id = ?`` from
        the transition, whose default is ``None`` for a non-PAUSED target,
        so the resume clears the pending id in the same UPDATE. On an
        optimistic-concurrency ``RuntimeError`` the row is re-read: a
        concurrent terminal is softened to a WARNING (a CLI-cancel beat the
        approve); anything else re-raises.
        """
        current = _read_status(self._pool, build_id)
        if current in TERMINAL_STATES:
            logger.warning(
                "transition_to_running: build_id=%s already terminal (%s); "
                "no-op",
                build_id,
                current.value,
            )
            return
        if current is BuildState.RUNNING:
            # Idempotent double-approve — the bridge's later BuildStarted
            # write-back also composes to a no-op here.
            return
        try:
            self._pool.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=current),
                    BuildState.RUNNING,
                )
            )
        except RuntimeError:
            latest = _read_status(self._pool, build_id)
            if latest in TERMINAL_STATES:
                logger.warning(
                    "transition_to_running: build_id=%s went terminal (%s) "
                    "under a concurrent writer; no-op",
                    build_id,
                    latest.value,
                )
                return
            raise

    async def transition_to_failed(self, *, build_id: str, reason: str) -> None:
        """Transition the current state → FAILED (HARD_STOP)."""
        current = _read_status(self._pool, build_id)
        if current in TERMINAL_STATES:
            logger.warning(
                "transition_to_failed: build_id=%s already terminal (%s); "
                "no-op",
                build_id,
                current.value,
            )
            return
        self._pool.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=current),
                BuildState.FAILED,
                error=reason,
            )
        )

    async def transition_to_cancelled(self, *, build_id: str, reason: str) -> None:
        """Cancel the build — the SOLE cancel writer for all four outcomes.

        Raises :class:`StaleTransitionError` when the row is *already*
        terminal, **before** any wire publish, so a cancel that lost the
        race does not double-emit. Otherwise delegates to
        :class:`SqliteBuildCanceller` (the actual ``apply_transition``).
        """
        current = _read_status(self._pool, build_id)
        if current in TERMINAL_STATES:
            raise StaleTransitionError(build_id, current)
        self._canceller.mark_cancelled(build_id, reason)


# ---------------------------------------------------------------------------
# Shared helper + factory.
# ---------------------------------------------------------------------------


def _read_status(pool: SqliteLifecyclePersistence, build_id: str) -> BuildState:
    """Read ``builds.status`` off the writer connection (no transaction)."""
    row = pool.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no build row for build_id={build_id!r}")
    status = row["status"] if isinstance(row, sqlite3.Row) else row[0]
    return BuildState(status)


def build_sqlite_gate_adapters(
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    clock: Callable[[], datetime],
) -> tuple[GateRepository, StateMachine]:
    """Build the ``(repository, state_machine)`` gate-adapter pair.

    Both adapters compose the shared ``sqlite_pool`` facade and a shared
    :class:`_PauseHandoff` so ``record_paused_build`` (repository) and
    ``transition_to_paused`` (state machine) exchange the ``request_id``.

    Args:
        sqlite_pool: The shared :class:`SqliteLifecyclePersistence` facade
            owned by ``forge serve``.
        clock: Injected ``() -> datetime`` (UTC) used to stamp
            ``stage_log`` rows and the degraded-fallback ``decided_at``.
            Clock hygiene — never ``datetime.now()``.

    Returns:
        A ``(repository, state_machine)`` tuple satisfying the
        :class:`forge.gating.wrappers.GateRepository` /
        :class:`forge.gating.wrappers.StateMachine` Protocols.
    """
    handoff = _PauseHandoff()
    repository = _SqliteGateRepository(sqlite_pool, clock=clock, handoff=handoff)
    state_machine = _SqliteStateMachine(sqlite_pool, handoff=handoff)
    return repository, state_machine
