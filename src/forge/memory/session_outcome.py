"""Terminal-state :class:`SessionOutcome` writer (TASK-IC-007).

This module is the **terminal-state side** of FEAT-FORGE-006 — it is
invoked exactly once per build_id (by the FEAT-FORGE-001 terminal-state
callback) when the pipeline reaches a terminal outcome (``success``,
``failure``, or ``aborted``). It collects all :class:`GateDecision`
references for the build, sorts them ASC by ``decided_at``, and writes
a single :class:`SessionOutcome` entity into the
``forge_pipeline_history`` Graphiti group via :func:`write_entity`.

Design contract
---------------

* **Idempotency** — Before writing, the module performs a pre-write
  existence check by ``build_id`` against ``forge_pipeline_history``.
  If a :class:`SessionOutcome` for the build already exists, the
  function no-ops and returns ``None``. This satisfies the
  ``@edge-case @data-integrity session-outcome-retry-idempotency`` rule
  and the ``@concurrency gate-decisions-in-close-succession`` scenario.

* **Ordering (ASSUM-008 resolution)** — :class:`GateDecision`
  references are collected from the SQLite ledger (the source of truth
  per FEAT-FORGE-001) and sorted ASCending by ``decided_at`` before
  the ``gate_decision_ids`` list is constructed. When two decisions
  share a microsecond-level timestamp, the secondary sort key is the
  decision's ``entity_id`` so the ordering is fully deterministic
  across re-runs.

* **Synchronous write** — Unlike stage-completion hooks (which use
  :func:`forge.memory.writer.fire_and_forget_write`), this writer
  awaits :func:`write_entity` directly. The terminal-state callback
  wants confirmation that the outcome was persisted; resilience is
  provided by the pre-write existence check, not by silent swallow.

* **Terminal-only** — The function refuses to write for non-terminal
  outcome strings (``"in_progress"``, ``"running"``, etc.). This is a
  defence-in-depth check on top of the ``Literal["success", "failure",
  "aborted"]`` type hint: callers occasionally bypass the type by
  reading the outcome from a config file or external API, and a silent
  no-op is preferable to writing a malformed entity. The ``@edge-case
  no-in-progress-session-outcome`` rule.

* **Split-brain safety** — Concurrent calls from two Forge instances
  both perform the same pre-write existence check. If the
  Graphiti-side query is fast enough, exactly one writer wins and the
  other no-ops. If the queries race past each other, both writers
  reach :func:`write_entity` — but Graphiti's upsert semantics on the
  shared ``entity_id`` (derived deterministically from ``build_id``
  via :func:`_session_outcome_entity_id`) collapse the two writes into
  one row at the storage layer. This is the dedupe contract carried
  over from TASK-IC-001's ``entity_id`` rule.

Repository abstraction
----------------------

The SQLite ledger is owned by FEAT-FORGE-001. This module depends on
the **read** surface only, expressed as the
:class:`PipelineHistoryRepository` :class:`~typing.Protocol` below.
FEAT-FORGE-001 is responsible for providing a concrete implementation;
tests provide an in-memory fake. Decoupling via Protocol keeps this
module I/O-free except for the Graphiti dispatch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Awaitable, Callable, Literal, Protocol, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import GateDecision, SessionOutcome, SessionOutcomeKind
from .writer import write_entity

logger = logging.getLogger("forge.memory.session_outcome")

#: Graphiti group_id for pipeline-history entities — kept in sync with
#: :mod:`forge.memory.writer`. Centralised here so a future group
#: rename only requires editing this constant.
PIPELINE_HISTORY_GROUP_ID = "forge_pipeline_history"

#: UUID5 namespace prefix for the deterministic ``entity_id`` derived
#: from ``build_id``. Using a namespaced prefix means two Forge
#: deployments writing to the same Graphiti backend with the same
#: ``build_id`` will produce the same ``entity_id`` (intentional —
#: that is the dedupe contract), but a build_id like ``"42"`` will
#: not collide with an unrelated UUID5 derived from the same string
#: under a different namespace.
_SESSION_OUTCOME_URI_PREFIX = "forge:session-outcome:"

#: Set of outcome strings that count as terminal. Defined as a frozenset
#: so it can participate in ``in`` checks at any boundary without
#: rebuilding the literal each call. Kept in sync with
#: :data:`SessionOutcomeKind` — adding a terminal kind requires editing
#: both.
_TERMINAL_OUTCOMES: frozenset[str] = frozenset({"success", "failure", "aborted"})


# ---------------------------------------------------------------------------
# Repository / existence-check protocols
# ---------------------------------------------------------------------------


class PipelineHistoryRepository(Protocol):
    """Read-only view onto the SQLite pipeline-history ledger.

    Owned by FEAT-FORGE-001; this module consumes only the read
    surface. Callers MUST guarantee that the returned
    :class:`GateDecision` instances carry their final SQLite-assigned
    ``entity_id`` values (ASSUM-007 resolution).
    """

    def get_gate_decisions_for_build(
        self, build_id: str
    ) -> Sequence[GateDecision]:
        """Return every :class:`GateDecision` row recorded for ``build_id``.

        Order is **not** assumed by this writer — the caller-side
        ordering is normalised here via
        :func:`_sort_gate_decisions`. Implementations are free to
        return rows in insertion order, primary-key order, or any
        other order convenient to the underlying storage.
        """
        ...


#: Pre-write existence-check callable.
#:
#: Returns ``True`` when a :class:`SessionOutcome` for the given
#: ``build_id`` already exists in ``forge_pipeline_history``. The
#: production implementation queries Graphiti; tests inject a fake.
SessionOutcomeExistsCheck = Callable[[str], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _session_outcome_entity_id(build_id: str) -> UUID:
    """Derive the deterministic ``entity_id`` for a build's outcome.

    The derivation is :func:`uuid.uuid5` over a namespaced URI built
    from the build_id — the namespace prefix prevents accidental
    collisions with UUID5 derivations performed elsewhere in the
    system from the same input string.

    The function is pure: same input → same output, no I/O. This is
    the dedupe contract that makes Graphiti's storage-layer upsert
    safe under concurrent writes (see module docstring,
    "Split-brain safety").
    """
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("build_id must be a non-empty string")
    return uuid5(NAMESPACE_URL, _SESSION_OUTCOME_URI_PREFIX + build_id)


def _sort_gate_decisions(
    decisions: Sequence[GateDecision],
) -> list[GateDecision]:
    """Return ``decisions`` sorted ASC by ``decided_at`` (ASSUM-008).

    Secondary sort by ``entity_id`` keeps ordering deterministic when
    two decisions share a microsecond-level timestamp — without the
    secondary key, Python's stable sort would preserve insertion
    order, which is implementation-defined for the upstream
    repository and would make the resulting list non-deterministic
    across re-runs.

    The function returns a new list; the input is not mutated. This
    matters because the repository may share the underlying list
    with other callers.
    """
    return sorted(decisions, key=lambda d: (d.decided_at, str(d.entity_id)))


def _is_terminal(outcome: str) -> bool:
    """Return ``True`` when ``outcome`` is one of the terminal kinds."""
    return outcome in _TERMINAL_OUTCOMES


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def write_session_outcome(
    build_id: str,
    outcome: Literal["success", "failure", "aborted"] | str,
    sqlite_repo: PipelineHistoryRepository,
    *,
    exists_check: SessionOutcomeExistsCheck,
    closed_at: datetime | None = None,
    write: Callable[
        [SessionOutcome, str], Awaitable[None]
    ] = write_entity,
) -> SessionOutcome | None:
    """Write a :class:`SessionOutcome` for ``build_id``, idempotently.

    The terminal-state callback (FEAT-FORGE-001 owns the trigger)
    invokes this function once on first terminal-state transition.
    Sequencing:

    1. **Terminal check** — Reject non-terminal ``outcome`` strings
       early. Logs a warning and returns ``None`` (defence in depth
       on top of the ``Literal`` type hint; see ``@edge-case
       no-in-progress-session-outcome``).
    2. **Pre-write existence check** — Query
       ``forge_pipeline_history`` for an existing outcome with this
       ``build_id``. If one exists, return ``None`` (the
       ``@edge-case session-outcome-retry-idempotency`` rule).
    3. **Collect** — Read every :class:`GateDecision` for the build
       from the SQLite ledger via ``sqlite_repo``.
    4. **Sort** — Order the decisions ASC by ``decided_at`` (with
       ``entity_id`` as the deterministic tiebreaker), per the
       ASSUM-008 resolution.
    5. **Build** — Construct the :class:`SessionOutcome` with a
       deterministic ``entity_id`` derived from ``build_id``, the
       ordered ``gate_decision_ids``, and a ``closed_at`` timestamp.
    6. **Write** — Await :func:`write_entity` synchronously. The
       caller wants confirmation; failures propagate.

    Args:
        build_id: Non-empty pipeline build identifier. Used both as
            the natural key for the existence check and as the seed
            for the deterministic ``entity_id``.
        outcome: One of ``"success"``, ``"failure"``, ``"aborted"``.
            Other strings are rejected with a logged warning and a
            ``None`` return.
        sqlite_repo: Read-only view of the SQLite ledger (FEAT-
            FORGE-001 ownership). Provides the gate-decision rows.
        exists_check: Async callable returning ``True`` when a
            :class:`SessionOutcome` for ``build_id`` is already
            present in ``forge_pipeline_history``. Production wires
            this to the Graphiti read path; tests inject a fake.
        closed_at: Timestamp at which the pipeline closed. Defaults
            to :func:`datetime.now` in UTC when omitted, so call
            sites that don't track close time still produce a
            valid entity.
        write: Override for the underlying :func:`write_entity`
            dispatcher. The default is the production writer; tests
            inject a recording fake. The seam exists so the writer
            can be exercised without patching at module scope.

    Returns:
        The persisted :class:`SessionOutcome`, or ``None`` when the
        write was a no-op (existing entity OR non-terminal outcome).

    Raises:
        ValueError: ``build_id`` is not a non-empty string.
        TypeError: ``sqlite_repo`` does not expose
            ``get_gate_decisions_for_build``.
        Exception: Any exception from the underlying ``write``
            callable is propagated verbatim — the caller (terminal-
            state callback) wants to see the failure.
    """
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("build_id must be a non-empty string")
    if not hasattr(sqlite_repo, "get_gate_decisions_for_build"):
        raise TypeError(
            "sqlite_repo must implement PipelineHistoryRepository "
            "(missing get_gate_decisions_for_build)"
        )

    # Step 1 — Terminal-only guard. Logged at WARNING because reaching
    # this branch means a caller bypassed the Literal type — that is
    # an integration bug worth surfacing in operator dashboards.
    if not _is_terminal(outcome):
        logger.warning(
            "session_outcome_non_terminal_outcome_skipped",
            extra={
                "build_id": build_id,
                "outcome": outcome,
                "allowed": sorted(_TERMINAL_OUTCOMES),
            },
        )
        return None

    # Step 2 — Pre-write existence check. The ``await`` is required
    # because the production implementation does I/O (Graphiti
    # query); the test fake is also async to keep the call site
    # uniform. A failure inside ``exists_check`` propagates — the
    # terminal-state callback would rather see the read failure than
    # silently double-write.
    if await exists_check(build_id):
        logger.info(
            "session_outcome_already_exists_skipping_write",
            extra={"build_id": build_id, "outcome": outcome},
        )
        return None

    # Step 3 — Collect gate decisions from the SQLite ledger. The
    # repository is the source of truth (per FEAT-FORGE-001); no
    # cross-checking against Graphiti happens here.
    decisions = sqlite_repo.get_gate_decisions_for_build(build_id)

    # Step 4 — Sort ASC by ``decided_at`` with ``entity_id`` as
    # tiebreaker. ASSUM-008 resolution; downstream consumers depend
    # on this ordering for timeline reconstruction.
    ordered = _sort_gate_decisions(decisions)
    gate_decision_ids = [d.entity_id for d in ordered]

    # Step 5 — Build the entity. ``closed_at`` defaults to "now (UTC)"
    # when not supplied; the tz-aware default avoids a naive datetime
    # leaking into pydantic validation.
    if closed_at is None:
        closed_at = datetime.now(tz=UTC)
    entity = SessionOutcome(
        entity_id=_session_outcome_entity_id(build_id),
        build_id=build_id,
        outcome=outcome,  # type: ignore[arg-type]  # narrowed by _is_terminal above
        gate_decision_ids=gate_decision_ids,
        closed_at=closed_at,
    )

    # Step 6 — Synchronous write. Failures propagate; this is the
    # confirm-on-completion variant called out in the AC list.
    await write(entity, PIPELINE_HISTORY_GROUP_ID)
    return entity


__all__ = [
    "PIPELINE_HISTORY_GROUP_ID",
    "PipelineHistoryRepository",
    "SessionOutcomeExistsCheck",
    "SessionOutcomeKind",
    "write_session_outcome",
]
