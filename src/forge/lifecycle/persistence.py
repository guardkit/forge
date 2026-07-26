"""Concrete SQLite-backed lifecycle persistence layer (TASK-PSM-005).

This module is the **producer side** of the FEAT-FORGE-001
``PERSISTENCE_PROTOCOLS`` integration contract — every Sqlite* class
declared here implements one of the runtime-checkable Protocols already
defined in :mod:`forge.pipeline.cli_steering`, so the CLI steering
handler can be wired against in-memory fakes in tests and against these
Sqlite-backed concrete implementations in production.

It also enforces concern **sc_001** (state-mutation exclusivity) at the
implementation level: :func:`SqliteLifecyclePersistence.apply_transition`
is the *only* public function in the entire ``src/forge`` tree that
issues ``UPDATE builds SET status = ?``. Every other write that needs
to change ``builds.status`` composes a :class:`~forge.lifecycle.state_machine.Transition`
via :func:`forge.lifecycle.state_machine.transition` and routes it
through ``apply_transition``.

Design rules baked into this module:

* **Single SQL writer** of ``builds.status`` — :func:`apply_transition`.
* **BEGIN IMMEDIATE** for every write — avoids busy-retry contention
  under concurrent CLI / agent-runtime activity (review finding F7).
* **read-only connections for reads** — every read path opens a fresh
  ``read_only_connect()`` against the writer's database file so the CLI
  side cannot accidentally hold a write lock while listing status.
* **Strict typing at the boundary** — ``apply_transition`` accepts only
  :class:`Transition` value objects. Raw ``status=`` kwargs are refused
  by signature.
* **Domain-shaped errors** — ``IntegrityError`` on the unique
  ``(feature_id, correlation_id)`` index is translated to
  :class:`DuplicateBuildError` rather than leaking ``sqlite3.IntegrityError``
  to callers.

References:
    - TASK-PSM-005 — this task brief.
    - TASK-PSM-002 — schema + connection helpers (consumed via
      ``SCHEMA_INITIALIZED`` integration contract).
    - TASK-PSM-004 — ``Transition`` value object + ``transition()``
      composer (consumed via ``STATE_TRANSITION_API`` integration
      contract).
    - TASK-PSM-007 — consumer of ``pending_approval_request_id`` for
      PAUSED-recovery idempotency.
    - ``forge/pipeline/cli_steering.py`` — Protocol seams this module
      satisfies.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Final, Iterator

from pydantic import BaseModel, ConfigDict, Field

from forge.adapters.sqlite.connect import read_only_connect
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.state_machine import (
    BuildState,
    Transition,
    transition as compose_transition,
)
from forge.pipeline.cli_steering import BuildLifecycle, BuildSnapshot
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)


__all__ = [
    "ACTIVE_STATES",
    "MAX_HISTORY_LIMIT",
    "AsyncTaskCanceller",
    "AsyncTaskUpdater",
    "Build",
    "BuildMode",
    "BuildRow",
    "BuildStatusView",
    "DuplicateBuildError",
    "SqliteBuildCanceller",
    "SqliteBuildResumer",
    "SqliteBuildSnapshotReader",
    "SqliteLifecyclePersistence",
    "SqlitePauseRejectResolver",
    "SqliteStageLogReader",
    "SqliteStageSkipRecorder",
    "StageLogEntry",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Active (non-terminal, non-INTERRUPTED) lifecycle states. Any build whose
#: status is in this set blocks a new ``record_pending_build`` for the same
#: feature (Group C "active in-flight duplicate" check) and shows up at the
#: top of ``forge status`` (per ``API-cli.md §4.2``).
ACTIVE_STATES: Final[frozenset[BuildState]] = frozenset(
    {
        BuildState.QUEUED,
        BuildState.PREPARING,
        BuildState.RUNNING,
        BuildState.PAUSED,
        BuildState.FINALISING,
    }
)

#: Hard cap on ``read_history(limit=...)`` to prevent unbounded queries
#: from a misbehaving CLI invocation.
MAX_HISTORY_LIMIT: Final[int] = 1000

#: Number of recent terminal builds appended to ``read_status`` output.
_RECENT_TERMINAL_LIMIT: Final[int] = 5


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class DuplicateBuildError(RuntimeError):
    """Raised by :func:`record_pending_build` for a duplicate build row.

    Signals that a build with the same ``(feature_id, correlation_id)``
    already exists. Group B "duplicate refused" — surfaces an
    ``IntegrityError`` on the unique index as a domain-level error so
    the CLI / NATS consumer can issue a structured response without
    importing :mod:`sqlite3`.
    """

    def __init__(self, feature_id: str, correlation_id: str) -> None:
        super().__init__(
            f"duplicate build for feature_id={feature_id!r}, "
            f"correlation_id={correlation_id!r}"
        )
        self.feature_id = feature_id
        self.correlation_id = correlation_id


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class StageLogEntry(BaseModel):
    """One row in the ``stage_log`` table.

    Frozen Pydantic model so callers can compare entries by value and
    pass them across thread boundaries without defensive copies.

    Attributes mirror the ``stage_log`` schema (TASK-PSM-002 §2.2)
    one-for-one. ``details`` is a Python ``dict`` here for ergonomics —
    the persistence layer JSON-encodes it on write and decodes on read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str = Field(min_length=1)
    stage_label: str = Field(min_length=1)
    target_kind: str
    target_identifier: str
    status: str
    gate_mode: str | None = None
    coach_score: float | None = None
    threshold_applied: float | None = None
    started_at: datetime
    completed_at: datetime
    duration_secs: float
    details: dict[str, Any] = Field(default_factory=dict)


class BuildRow(BaseModel):
    """One row from the ``builds`` table.

    The full row form, used by :meth:`SqliteLifecyclePersistence.read_history`
    and by the recovery pass.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str
    feature_id: str
    repo: str
    branch: str
    feature_yaml_path: str
    project: str | None = None
    status: BuildState
    triggered_by: str
    originating_adapter: str | None = None
    originating_user: str | None = None
    correlation_id: str
    parent_request_id: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    error: str | None = None
    max_turns: int = 5
    sdk_timeout_seconds: int = 1800
    pending_approval_request_id: str | None = None
    # FEAT-FORGE-008 / TASK-MBC8-001 — pipeline build mode. Defaults to
    # ``BuildMode.MODE_A`` so pre-FEAT-FORGE-008 callers (and historical
    # rows that backfilled to ``"mode-a"`` via ``schema_v2.sql``) keep
    # working unchanged.
    mode: BuildMode = BuildMode.MODE_A
    # FEAT-UBS-002 / TASK-UBS-002-integration — budget-guard profile
    # selected by ``forge queue --profile`` (``schema_v5.sql``). ``None``
    # means "no per-build profile requested"; the daemon resolves it via
    # ``config.budget.resolve(profile)``, which maps ``None`` to
    # ``default_profile`` (``attended`` = caps off, ASSUM-010). Historical
    # rows that pre-date the column read back as ``None``.
    profile: str | None = None


class BuildStatusView(BaseModel):
    """Narrow projection used by ``forge status``.

    Carries only the columns the CLI status table renders so we don't
    waste bandwidth marshalling the full :class:`BuildRow` for every
    build.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str
    feature_id: str
    status: BuildState
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pr_url: str | None = None
    error: str | None = None
    # FEAT-FORGE-008 / TASK-MBC8-001 — surface ``mode`` to ``forge status``
    # so operators can see at a glance whether a build is Mode A / B / C
    # without joining against the full ``BuildRow``.
    mode: BuildMode = BuildMode.MODE_A


@dataclass(frozen=True, slots=True)
class Build:
    """Minimal build value object compatible with :func:`state_machine.transition`.

    The state machine only reads ``status`` and ``build_id`` off its
    ``build`` argument (TASK-PSM-004); this dataclass exposes exactly
    those two fields so we can compose a :class:`Transition` for an
    existing row without needing the TASK-PSM-003 ``Build`` Pydantic
    model.

    ``mode`` is added by TASK-MBC8-001 so callers that hold a ``Build``
    value object can route through mode-aware planners without
    re-deriving it from the SQLite row. It defaults to
    :attr:`BuildMode.MODE_A` to keep every Mode A call site
    backwards-compatible.
    """

    build_id: str
    status: BuildState
    mode: BuildMode = BuildMode.MODE_A


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_build_row(row: sqlite3.Row | tuple[Any, ...]) -> BuildRow:
    """Hydrate a ``builds`` row into :class:`BuildRow`."""
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
    else:
        # Defensive: the row factory might not be ``sqlite3.Row`` in
        # exotic test wirings. We expect the exact column ordering of
        # ``SELECT *`` against the schema in ``schema.sql``.
        keys = (
            "build_id",
            "feature_id",
            "repo",
            "branch",
            "feature_yaml_path",
            "project",
            "status",
            "triggered_by",
            "originating_adapter",
            "originating_user",
            "correlation_id",
            "parent_request_id",
            "queued_at",
            "started_at",
            "completed_at",
            "worktree_path",
            "pr_url",
            "error",
            "max_turns",
            "sdk_timeout_seconds",
            "pending_approval_request_id",
            "mode",
            "profile",
        )
        data = dict(zip(keys, row, strict=False))

    return BuildRow(
        build_id=data["build_id"],
        feature_id=data["feature_id"],
        repo=data["repo"],
        branch=data["branch"],
        feature_yaml_path=data["feature_yaml_path"],
        project=data.get("project"),
        status=BuildState(data["status"]),
        triggered_by=data["triggered_by"],
        originating_adapter=data.get("originating_adapter"),
        originating_user=data.get("originating_user"),
        correlation_id=data["correlation_id"],
        parent_request_id=data.get("parent_request_id"),
        queued_at=datetime.fromisoformat(data["queued_at"]),
        started_at=(
            datetime.fromisoformat(data["started_at"])
            if data.get("started_at")
            else None
        ),
        completed_at=(
            datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at")
            else None
        ),
        worktree_path=data.get("worktree_path"),
        pr_url=data.get("pr_url"),
        error=data.get("error"),
        max_turns=int(data.get("max_turns") or 5),
        sdk_timeout_seconds=int(data.get("sdk_timeout_seconds") or 1800),
        pending_approval_request_id=data.get("pending_approval_request_id"),
        mode=BuildMode(data.get("mode") or BuildMode.MODE_A.value),
        profile=data.get("profile"),
    )


def _row_to_status_view(row: sqlite3.Row) -> BuildStatusView:
    """Hydrate a narrow status-projection row into :class:`BuildStatusView`."""
    # ``sqlite3.Row`` raises IndexError for unknown keys, so we tolerate
    # legacy rows that pre-date the ``mode`` column by falling back to
    # MODE_A. This keeps ``forge status`` working through the upgrade
    # window even if the additive migration has not yet run.
    try:
        raw_mode = row["mode"]
    except (IndexError, KeyError):
        raw_mode = None
    return BuildStatusView(
        build_id=row["build_id"],
        feature_id=row["feature_id"],
        status=BuildState(row["status"]),
        queued_at=datetime.fromisoformat(row["queued_at"]),
        started_at=(
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        ),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
        pr_url=row["pr_url"],
        error=row["error"],
        mode=BuildMode(raw_mode) if raw_mode else BuildMode.MODE_A,
    )


def _row_to_stage_entry(row: sqlite3.Row) -> StageLogEntry:
    """Hydrate a ``stage_log`` row into :class:`StageLogEntry`."""
    raw_details = row["details_json"]
    try:
        details = json.loads(raw_details) if raw_details else {}
    except json.JSONDecodeError:
        # Malformed legacy rows are surfaced as empty details; callers
        # that need to see the raw bytes can read the table directly.
        logger.warning(
            "stage_log row for build_id=%r has malformed details_json; "
            "returning empty dict",
            row["build_id"],
        )
        details = {}

    return StageLogEntry(
        build_id=row["build_id"],
        stage_label=row["stage_label"],
        target_kind=row["target_kind"],
        target_identifier=row["target_identifier"],
        status=row["status"],
        gate_mode=row["gate_mode"],
        coach_score=row["coach_score"],
        threshold_applied=row["threshold_applied"],
        started_at=datetime.fromisoformat(row["started_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]),
        duration_secs=row["duration_secs"],
        details=details,
    )


# ---------------------------------------------------------------------------
# Main facade
# ---------------------------------------------------------------------------


class SqliteLifecyclePersistence:
    """Concrete SQLite-backed lifecycle persistence.

    Holds a writer connection (the long-lived agent-runtime connection
    from :func:`forge.adapters.sqlite.connect.connect_writer`) and a
    derived ``db_path`` so reads can open fresh ``read_only_connect``
    handles (per ADR-ARCH-013 — CLI never writes).

    The class is the *facade* for every lifecycle write and read. The
    seven ``Sqlite*`` Protocol-implementing classes elsewhere in this
    module compose this facade rather than duplicating SQL.

    Args:
        connection: Writer connection produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`. The
            persistence layer treats this connection as autocommit
            (``isolation_level=None``) and manages transactions via
            explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` statements.
        db_path: Filesystem path to the database file. Optional; when
            omitted, derived from ``PRAGMA database_list`` on the
            writer connection. For in-memory databases the path is
            empty and reads fall back to the writer connection (tests
            only — production always has a real file).
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        db_path: Path | None = None,
    ) -> None:
        self._cx = connection
        # ``sqlite3.Row`` factory is needed by the read-side projections.
        if connection.row_factory is None:
            connection.row_factory = sqlite3.Row
        if db_path is None:
            db_path = self._derive_db_path(connection)
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the underlying writer connection (for tests / diagnostics)."""
        return self._cx

    @property
    def db_path(self) -> Path | None:
        """Return the database path used to open read-only connections."""
        return self._db_path

    # ------------------------------------------------------------------
    # Reader helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_db_path(connection: sqlite3.Connection) -> Path | None:
        """Return the database file path from ``PRAGMA database_list``.

        Returns ``None`` for in-memory connections (file column is the
        empty string in that case).
        """
        try:
            row = connection.execute("PRAGMA database_list;").fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        # PRAGMA database_list rows: (seq, name, file).
        try:
            file_path = row[2] if len(row) >= 3 else None
        except (IndexError, TypeError):
            return None
        if not file_path:
            return None
        return Path(file_path)

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection (or fall back to the writer).

        AC-010: ``All read paths use ``read_only_connect()``. We open a
        fresh ro handle per call so concurrent CLI invocations do not
        contend with the writer's lock. For in-memory test databases
        (path is ``None``) we transparently fall back to the writer
        connection because in-memory SQLite is per-connection.
        """
        if self._db_path is None or str(self._db_path) in ("", ":memory:"):
            # In-memory or unresolvable path: reuse the writer connection.
            yield self._cx
            return

        cx = read_only_connect(self._db_path)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
        finally:
            cx.close()

    # ------------------------------------------------------------------
    # Write API — apply_transition: SOLE writer of ``builds.status``
    # ------------------------------------------------------------------

    def apply_transition(self, transition: Transition) -> None:
        """Persist a :class:`Transition` against the ``builds`` row.

        This is the **sole** function in the entire ``src/forge`` tree
        that issues ``UPDATE builds SET status = ?`` (concern sc_001 /
        AC-002). Every other state-changing write composes a
        :class:`Transition` via
        :func:`forge.lifecycle.state_machine.transition` and routes it
        through this method.

        The signature accepts a single positional/keyword argument named
        ``transition`` whose annotation is :class:`Transition`. Raw
        ``status=`` kwargs are refused by signature (AC-003).

        Field semantics:

        * ``started_at`` is populated from ``occurred_at`` when the
          target state is ``PREPARING`` or ``RUNNING`` and the existing
          column is ``NULL`` (idempotent — a re-entry from
          ``INTERRUPTED`` does not overwrite the original start time).
        * ``completed_at`` is populated from ``transition.completed_at``
          when the target state is terminal (the state machine guarantees
          it is non-``None`` for terminal targets — Group G data
          integrity invariant).
        * ``error`` and ``pr_url`` are written verbatim when present.
        * ``pending_approval_request_id`` is set verbatim — when the
          target is ``PAUSED`` it carries the request id (F4 / AC-004);
          when the target is anything else it is cleared (the
          :class:`Transition` default is ``None``).

        The whole UPDATE happens inside a ``BEGIN IMMEDIATE`` block
        (AC-009). The optimistic-concurrency check on the
        ``WHERE status = ?`` predicate guards against another writer
        having transitioned the row in between the caller composing the
        Transition and this UPDATE — a 0-row update raises a
        :class:`RuntimeError`.

        Args:
            transition: The :class:`Transition` value object produced by
                :func:`forge.lifecycle.state_machine.transition`.

        Raises:
            TypeError: If ``transition`` is not a :class:`Transition`.
            RuntimeError: If the optimistic-concurrency UPDATE matches
                zero rows (the build's state changed underneath us, or
                the row does not exist).
            sqlite3.Error: For any other database error. The transaction
                is rolled back so the row is not partially updated.
        """
        if not isinstance(transition, Transition):
            raise TypeError(
                "apply_transition requires a Transition value object; got "
                f"{type(transition).__name__}"
            )

        # Decide whether this transition writes started_at. We use the
        # SQL ``COALESCE(started_at, ?)`` so an existing started_at is
        # never overwritten — a replay from INTERRUPTED preserves the
        # original wall-clock start.
        writes_started_at = transition.to_state in (
            BuildState.PREPARING,
            BuildState.RUNNING,
        )
        started_at_value = (
            transition.occurred_at.isoformat() if writes_started_at else None
        )
        completed_at_value = (
            transition.completed_at.isoformat()
            if transition.completed_at is not None
            else None
        )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            cursor = self._cx.execute(
                """
                UPDATE builds
                   SET status = ?,
                       started_at = COALESCE(started_at, ?),
                       completed_at = COALESCE(?, completed_at),
                       error = COALESCE(?, error),
                       pr_url = COALESCE(?, pr_url),
                       pending_approval_request_id = ?
                 WHERE build_id = ?
                   AND status = ?
                """,
                (
                    transition.to_state.value,
                    started_at_value,
                    completed_at_value,
                    transition.error,
                    transition.pr_url,
                    transition.pending_approval_request_id,
                    transition.build_id,
                    transition.from_state.value,
                ),
            )
            if cursor.rowcount == 0:
                self._cx.execute("ROLLBACK;")
                raise RuntimeError(
                    "apply_transition: optimistic concurrency check failed "
                    f"for build_id={transition.build_id!r} "
                    f"({transition.from_state.value} -> "
                    f"{transition.to_state.value}); the row's state may "
                    "have changed underneath us, or the row does not exist"
                )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover - rollback failure is rare
                pass
            raise

    # ------------------------------------------------------------------
    # Write API — record_pending_build
    # ------------------------------------------------------------------

    def record_pending_build(
        self,
        payload: Any,
        *,
        mode: BuildMode | str | None = None,
        profile: str | None = None,
    ) -> str:
        """Insert a fresh ``builds`` row in state ``QUEUED``.

        The build_id is derived from ``payload.feature_id`` and
        ``payload.queued_at`` per :func:`forge.lifecycle.identifiers.derive_build_id`
        and returned to the caller for downstream correlation.

        Translates ``IntegrityError`` on the unique
        ``(feature_id, correlation_id)`` index into
        :class:`DuplicateBuildError` (AC-005 / Group B "duplicate
        refused"). The transaction is rolled back so neither the
        primary row nor any side rows become visible.

        Args:
            payload: Pydantic ``BuildQueuedPayload`` from
                :mod:`nats_core.events`. Typed as :class:`Any` here so
                the import does not pin the persistence layer to the
                wire-event package — the persistence layer only reads
                attributes and never instantiates the type. If the
                payload exposes a ``mode`` attribute and the keyword
                argument is omitted, that value is used.
            mode: Pipeline build mode (TASK-MBC8-001). Defaults to
                :attr:`BuildMode.MODE_A` when neither the keyword nor
                ``payload.mode`` carries a value, so existing Mode A
                callers continue to work without modification.
            profile: Budget-guard profile name selected by ``forge queue
                --profile`` (FEAT-UBS-002 / TASK-UBS-002-integration).
                Persisted to ``builds.profile`` so the daemon can resolve
                caps via ``config.budget.resolve(row.profile)``. Defaults
                to ``None`` (no per-build profile → the daemon applies
                ``config.budget.default_profile``); like ``mode`` it also
                falls back to sniffing ``payload.profile`` when the keyword
                is omitted, but ``BuildQueuedPayload`` carries no profile
                field today (option §2(b) is barred) so the CLI passes it
                explicitly.

        Returns:
            The derived ``build_id``.

        Raises:
            DuplicateBuildError: If the ``(feature_id, correlation_id)``
                pair already exists.
            sqlite3.Error: For any other database error.
        """
        feature_id: str = payload.feature_id
        correlation_id: str = payload.correlation_id
        queued_at: datetime = payload.queued_at
        build_id = derive_build_id(feature_id, queued_at)
        # Resolve the build mode: explicit kwarg wins, otherwise sniff
        # ``payload.mode``, otherwise default to MODE_A.
        if mode is None:
            mode = getattr(payload, "mode", None)
        resolved_mode: BuildMode = (
            BuildMode(mode) if mode is not None else BuildMode.MODE_A
        )
        # Resolve the budget profile: explicit kwarg wins, otherwise sniff
        # ``payload.profile`` (absent on today's nats-core payload —
        # §2(b) barred), otherwise NULL = the daemon's default_profile.
        if profile is None:
            profile = getattr(payload, "profile", None)

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            self._cx.execute(
                """
                INSERT INTO builds (
                    build_id, feature_id, repo, branch, feature_yaml_path,
                    status, triggered_by, originating_adapter,
                    originating_user, correlation_id, parent_request_id,
                    queued_at, max_turns, sdk_timeout_seconds, mode, profile
                ) VALUES (
                    ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    build_id,
                    feature_id,
                    payload.repo,
                    payload.branch,
                    payload.feature_yaml_path,
                    payload.triggered_by,
                    payload.originating_adapter,
                    payload.originating_user,
                    correlation_id,
                    payload.parent_request_id,
                    queued_at.isoformat(),
                    int(payload.max_turns),
                    int(payload.sdk_timeout_seconds),
                    resolved_mode.value,
                    profile,
                ),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.IntegrityError as exc:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover
                pass
            # Translate the unique-index violation into the domain error.
            # The IntegrityError message includes the constraint name or
            # column list — we don't parse it; we just look up whether
            # a duplicate row already exists with this (feature, corr).
            raise DuplicateBuildError(feature_id, correlation_id) from exc
        except sqlite3.Error:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise

        return build_id

    # ------------------------------------------------------------------
    # Read API — pick_next_pending (TASK-MBC8-009)
    # ------------------------------------------------------------------

    def pick_next_pending(self, project: str | None = None) -> BuildRow | None:
        """Return the oldest QUEUED build for ``project`` in FIFO order.

        TASK-MBC8-009 / FEAT-FORGE-008 acceptance criterion: the queue
        picker returns builds in their **original FIFO order regardless
        of mode**. There is no mode-based priority — every queued build
        is its own lifecycle (ASSUM-016) and a Mode A build queued at
        ``T0`` is picked before a Mode B build queued at ``T1`` even
        though the Mode B chain is shorter.

        This method is the canonical spelling referenced by the
        FEAT-FORGE-008 task brief; it composes
        :class:`forge.lifecycle.queue.SqliteSequentialQueuePicker`
        which already implements the correct ``ORDER BY queued_at ASC``
        semantics. The wrapper exists so callers asking for "the queue
        picker" via the persistence facade have one entrypoint instead
        of having to import a sibling module.

        The picker still respects sequential discipline (per-project
        blocking states from :data:`BLOCKING_STATES`); it never returns
        a row when an in-flight build for the same project is occupying
        the slot. ``project=None`` is the fleet-wide scope and uses
        ``IS NULL`` SQLite semantics — see
        :class:`SqliteSequentialQueuePicker` for the full contract.

        Args:
            project: Project name (``None`` = fleet-wide scope).

        Returns:
            The oldest QUEUED :class:`BuildRow` for ``project``, or
            ``None`` if the project is busy or has nothing queued.
        """
        # Local import to avoid a top-level circular import between
        # ``persistence`` (which queue.py composes) and ``queue`` (which
        # imports this module's facade).
        from forge.lifecycle.queue import SqliteSequentialQueuePicker

        picker = SqliteSequentialQueuePicker(self)
        return picker.next_build_to_pick(project)

    # ------------------------------------------------------------------
    # Write API — queue_build (TASK-MBC8-001)
    # ------------------------------------------------------------------

    def queue_build(
        self,
        payload: Any,
        *,
        mode: BuildMode | str | None = None,
        profile: str | None = None,
    ) -> str:
        """Mode-aware alias of :meth:`record_pending_build`.

        TASK-MBC8-001 surfaces a ``queue_build`` entry-point that accepts
        an explicit ``mode: BuildMode`` keyword so the supervisor and
        future Mode B / Mode C planners can opt in to a specific chain
        shape without depending on the ``payload.mode`` sniffing
        behaviour. The implementation forwards to
        :meth:`record_pending_build` so there is still exactly one
        ``INSERT INTO builds`` site (sc_001-style "single writer"
        cohesion).

        Args:
            payload: Same duck-typed ``BuildQueuedPayload`` accepted by
                :meth:`record_pending_build`.
            mode: Pipeline build mode. Defaults to
                :attr:`BuildMode.MODE_A` so omitting the keyword
                preserves Mode A behaviour.
            profile: Budget-guard profile name (FEAT-UBS-002). Forwarded
                to :meth:`record_pending_build`; ``None`` persists NULL so
                the daemon applies ``config.budget.default_profile``.

        Returns:
            The derived ``build_id`` string.
        """
        return self.record_pending_build(payload, mode=mode, profile=profile)

    # ------------------------------------------------------------------
    # Write API — record_stage
    # ------------------------------------------------------------------

    def record_stage(self, entry: StageLogEntry) -> None:
        """Append a row to the ``stage_log`` table.

        Used by dispatchers, the cancel/skip handler (via
        :class:`SqliteStageSkipRecorder`) and the per-turn supervisor
        recorder. The ``details`` dict is JSON-encoded into the
        ``details_json`` column.
        """
        if not isinstance(entry, StageLogEntry):
            raise TypeError(
                f"record_stage requires a StageLogEntry; got {type(entry).__name__}"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            self._cx.execute(
                """
                INSERT INTO stage_log (
                    build_id, stage_label, target_kind, target_identifier,
                    status, gate_mode, coach_score, threshold_applied,
                    started_at, completed_at, duration_secs, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.build_id,
                    entry.stage_label,
                    entry.target_kind,
                    entry.target_identifier,
                    entry.status,
                    entry.gate_mode,
                    entry.coach_score,
                    entry.threshold_applied,
                    entry.started_at.isoformat(),
                    entry.completed_at.isoformat(),
                    float(entry.duration_secs),
                    json.dumps(entry.details, sort_keys=True, default=str),
                ),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise

    # ------------------------------------------------------------------
    # Write API — record_last_coach_score
    # ------------------------------------------------------------------

    def record_last_coach_score(self, build_id: str, score: float) -> None:
        """Persist the most recent Coach score onto the ``builds`` row.

        Write side of the coach-score store (``schema_v6.sql``). The
        lifecycle bridge's ``build_state_recorder`` calls this per
        ``stage-complete`` / ``build-paused`` envelope that carries a
        ``coach_score``, so the last such write wins — the value read back
        by :meth:`read_last_coach_score` (the ``min_coach_score`` budget
        floor's reader) reflects the most recent gated stage.

        This is a **status-preserving** UPDATE of ``last_coach_score``
        alone — it never touches ``status``, so it is orthogonal to the
        single-transition-owner rule (``apply_transition`` remains the sole
        ``builds.status`` writer). A terminal row is a legal target: the
        score is a record of what happened, not a lifecycle change. A
        missing ``build_id`` matches zero rows and is a quiet no-op,
        consistent with the module's other concurrent-race handling.

        Args:
            build_id: The build whose latest Coach score is being recorded.
            score: The Coach score (0.0–1.0 in the UBS1C runner).

        Raises:
            ValueError: If ``build_id`` is empty.
            sqlite3.Error: For any database error. The transaction is
                rolled back so the row is not left partially updated.
        """
        if not build_id:
            raise ValueError("record_last_coach_score: build_id must be non-empty")

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            self._cx.execute(
                """
                UPDATE builds
                   SET last_coach_score = ?
                 WHERE build_id = ?
                """,
                (float(score), build_id),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover - rollback failure is rare
                pass
            raise

    # ------------------------------------------------------------------
    # Write API — mark_paused
    # ------------------------------------------------------------------

    def mark_paused(self, build_id: str, request_id: str) -> None:
        """Transition ``build_id`` to ``PAUSED`` and record the request id atomically.

        AC-004: writes ``pending_approval_request_id`` *atomically* with
        the state transition (single ``UPDATE`` statement). This is the
        producer side of the ``PENDING_APPROVAL_REQUEST_ID`` integration
        contract consumed by TASK-PSM-007 (PAUSED-recovery
        idempotency).

        The transition is composed via
        :func:`forge.lifecycle.state_machine.transition` so the
        transition table validates ``RUNNING -> PAUSED`` (or whichever
        from-state currently holds) before any SQL runs.

        Args:
            build_id: Build to pause.
            request_id: The approval-request id to record on the row.

        Raises:
            ValueError: If either argument is empty.
            RuntimeError: If the build does not exist.
            forge.lifecycle.state_machine.InvalidTransitionError: If the
                build's current state cannot transition to ``PAUSED``.
        """
        if not build_id:
            raise ValueError("mark_paused: build_id must be non-empty")
        if not request_id:
            raise ValueError("mark_paused: request_id must be non-empty")

        # Read the current state under the writer connection so the
        # subsequent apply_transition's optimistic check sees the same
        # value. We don't hold a transaction across the read — the
        # apply_transition's BEGIN IMMEDIATE + WHERE status=? clause is
        # the concurrency guard.
        row = self._cx.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"mark_paused: no build row for build_id={build_id!r}")
        current_state = BuildState(
            row["status"] if isinstance(row, sqlite3.Row) else row[0]
        )

        transition = compose_transition(
            Build(build_id=build_id, status=current_state),
            BuildState.PAUSED,
            pending_approval_request_id=request_id,
        )
        self.apply_transition(transition)

    # ------------------------------------------------------------------
    # Write API — refresh_pending_approval_request_id (TASK-GATE-D659)
    # ------------------------------------------------------------------

    def refresh_pending_approval_request_id(
        self, build_id: str, request_id: str
    ) -> None:
        """Update a PAUSED row's ``pending_approval_request_id`` in place.

        The defer round-trip re-publishes with ``attempt_count + 1`` — a
        fresh ``request_id`` — while the build **stays PAUSED**. There is
        no state transition, so :meth:`mark_paused` (RUNNING → PAUSED) is
        illegal here; this method issues a **status-preserving** UPDATE of
        ``pending_approval_request_id`` alone. It deliberately does *not*
        touch ``status``, keeping the single-transition-owner rule intact
        (``apply_transition`` remains the sole ``builds.status`` writer).

        The UPDATE is guarded by ``AND status = 'PAUSED'`` so a concurrent
        terminal transition landing between the caller's PAUSED precheck and
        this write cannot stamp a fresh request_id onto an already-terminal
        row (single-owner discipline). On a 0-row UPDATE the row is re-read:
        a genuinely missing build is a caller bug (``RuntimeError``); a row
        that a concurrent writer moved out of PAUSED is a soft no-op (log +
        return), matching the module's other concurrent-terminal handling.

        Args:
            build_id: The paused build whose pending request id is being
                refreshed.
            request_id: The new (next-attempt) approval-request id.

        Raises:
            ValueError: If either argument is empty.
            RuntimeError: If no row matches ``build_id`` at all — a refresh
                against a non-existent build is a caller bug, not a silent
                no-op. A row that exists but is non-PAUSED (terminal) is NOT
                an error: it is logged and skipped.
        """
        if not build_id:
            raise ValueError(
                "refresh_pending_approval_request_id: build_id must be non-empty"
            )
        if not request_id:
            raise ValueError(
                "refresh_pending_approval_request_id: request_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            cursor = self._cx.execute(
                """
                UPDATE builds
                   SET pending_approval_request_id = ?
                 WHERE build_id = ?
                   AND status = ?
                """,
                (request_id, build_id, BuildState.PAUSED.value),
            )
            if cursor.rowcount == 0:
                # No PAUSED row matched. Re-read (still inside the txn) to
                # tell a missing build from a concurrent-terminal race.
                row = self._cx.execute(
                    "SELECT status FROM builds WHERE build_id = ?",
                    (build_id,),
                ).fetchone()
                self._cx.execute("ROLLBACK;")
                if row is None:
                    raise RuntimeError(
                        "refresh_pending_approval_request_id: no build row for "
                        f"build_id={build_id!r}"
                    )
                current = row["status"] if isinstance(row, sqlite3.Row) else row[0]
                logger.warning(
                    "refresh_pending_approval_request_id: refresh skipped: "
                    "build_id=%s is %s, not PAUSED (a concurrent terminal "
                    "transition is authoritative)",
                    build_id,
                    current,
                )
                return
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            try:
                self._cx.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover - rollback failure is rare
                pass
            raise

    # ------------------------------------------------------------------
    # Read API — read_status
    # ------------------------------------------------------------------

    def read_status(
        self,
        feature_id: str | None = None,
    ) -> list[BuildStatusView]:
        """Return active builds plus the last 5 terminal builds.

        AC-006 / ``API-cli.md §4.2``: the status report combines all
        active builds with the most recent terminal builds, sorted
        newest-first by ``queued_at``.

        Args:
            feature_id: Optional filter — when supplied, only rows for
                that feature are returned.

        Returns:
            List of :class:`BuildStatusView` ordered by ``queued_at DESC``.
        """
        active_values = tuple(s.value for s in ACTIVE_STATES)
        terminal_values = tuple(
            s.value
            for s in (
                BuildState.COMPLETE,
                BuildState.FAILED,
                BuildState.CANCELLED,
                BuildState.SKIPPED,
            )
        )

        # Build placeholder strings that match each tuple length.
        active_placeholders = ",".join(["?"] * len(active_values))
        terminal_placeholders = ",".join(["?"] * len(terminal_values))

        feature_clause_active = " AND feature_id = ?" if feature_id else ""
        feature_clause_terminal = " AND feature_id = ?" if feature_id else ""

        active_sql = f"""
            SELECT build_id, feature_id, status, queued_at, started_at,
                   completed_at, pr_url, error, mode
              FROM builds
             WHERE status IN ({active_placeholders})
                   {feature_clause_active}
             ORDER BY queued_at DESC
        """
        terminal_sql = f"""
            SELECT build_id, feature_id, status, queued_at, started_at,
                   completed_at, pr_url, error, mode
              FROM builds
             WHERE status IN ({terminal_placeholders})
                   {feature_clause_terminal}
             ORDER BY queued_at DESC
             LIMIT ?
        """

        active_params: list[Any] = list(active_values)
        terminal_params: list[Any] = list(terminal_values)
        if feature_id:
            active_params.append(feature_id)
            terminal_params.append(feature_id)
        terminal_params.append(_RECENT_TERMINAL_LIMIT)

        with self._reader() as cx:
            active_rows = cx.execute(active_sql, active_params).fetchall()
            terminal_rows = cx.execute(terminal_sql, terminal_params).fetchall()

        combined = [_row_to_status_view(r) for r in active_rows] + [
            _row_to_status_view(r) for r in terminal_rows
        ]
        # Already ordered DESC within each bucket; re-sort the
        # concatenation so the final list is globally newest-first.
        combined.sort(key=lambda v: v.queued_at, reverse=True)
        return combined

    # ------------------------------------------------------------------
    # Read API — read_history
    # ------------------------------------------------------------------

    def read_history(
        self,
        limit: int = 50,
        feature_id: str | None = None,
    ) -> list[BuildRow]:
        """Return the most recent builds.

        AC-007: ``limit`` is clamped to :data:`MAX_HISTORY_LIMIT` (1000)
        to prevent unbounded queries. A non-positive ``limit`` returns
        an empty list.

        Args:
            limit: Maximum rows to return. Clamped to
                ``[0, MAX_HISTORY_LIMIT]``.
            feature_id: Optional feature filter.

        Returns:
            List of :class:`BuildRow` ordered by ``queued_at DESC``.
        """
        if not isinstance(limit, int):
            raise TypeError(
                f"read_history: limit must be int; got {type(limit).__name__}"
            )
        clamped = max(0, min(limit, MAX_HISTORY_LIMIT))
        if clamped == 0:
            return []

        feature_clause = " WHERE feature_id = ?" if feature_id else ""
        sql = f"""
            SELECT *
              FROM builds
              {feature_clause}
             ORDER BY queued_at DESC
             LIMIT ?
        """
        params: list[Any] = []
        if feature_id:
            params.append(feature_id)
        params.append(clamped)

        with self._reader() as cx:
            rows = cx.execute(sql, params).fetchall()
        return [_row_to_build_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Read API — get_build_row (TASK-JNB-102)
    # ------------------------------------------------------------------

    def get_build_row(self, build_id: str) -> BuildRow | None:
        """Return the full builds row for ``build_id``, or ``None``.

        Read-only accessor added for the TASK-JNB-102 CLI
        build-cancelled notifier, which needs ``feature_id`` (subject
        token) and ``correlation_id`` (payload field) that
        :class:`~forge.pipeline.cli_steering.BuildSnapshot` does not
        carry. General-purpose: any caller holding only a ``build_id``
        may hydrate the row through this method.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError("get_build_row: build_id must be non-empty")
        with self._reader() as cx:
            row = cx.execute(
                "SELECT * FROM builds WHERE build_id = ?",
                (build_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_build_row(row)

    # ------------------------------------------------------------------
    # Read API — read_last_coach_score
    # ------------------------------------------------------------------

    def read_last_coach_score(self, build_id: str) -> float | None:
        """Return the build's most recent Coach score, or ``None``.

        Read side of the coach-score store (``schema_v6.sql``). Returns
        the ``builds.last_coach_score`` value written by
        :meth:`record_last_coach_score`, or ``None`` when the build has no
        recorded score (a historical row that pre-dates the column, or a
        run that never emitted a gated ``coach_score``).

        The bound method matches the Supervisor's
        ``budget_coach_score_reader`` DI shape exactly — a sync
        ``Callable[[str], float | None]`` — so it can be wired directly as
        that reader when the Mode-C path is activated (activation is a
        plan-of-record decision, out of scope here; today the store is
        populated but no production caller reads it).

        Args:
            build_id: The build whose latest Coach score is being read.

        Returns:
            The recorded score as a ``float``, or ``None`` if unset or the
            build does not exist.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError("read_last_coach_score: build_id must be non-empty")
        with self._reader() as cx:
            row = cx.execute(
                "SELECT last_coach_score FROM builds WHERE build_id = ?",
                (build_id,),
            ).fetchone()
        if row is None:
            return None
        value = row["last_coach_score"] if isinstance(row, sqlite3.Row) else row[0]
        return float(value) if value is not None else None

    # ------------------------------------------------------------------
    # Read API — read_stages
    # ------------------------------------------------------------------

    def read_stages(self, build_id: str) -> list[StageLogEntry]:
        """Return all stage_log entries for ``build_id`` in chronological order."""
        if not build_id:
            raise ValueError("read_stages: build_id must be non-empty")
        with self._reader() as cx:
            rows = cx.execute(
                """
                SELECT build_id, stage_label, target_kind, target_identifier,
                       status, gate_mode, coach_score, threshold_applied,
                       started_at, completed_at, duration_secs, details_json
                  FROM stage_log
                 WHERE build_id = ?
                 ORDER BY started_at ASC, id ASC
                """,
                (build_id,),
            ).fetchall()
        return [_row_to_stage_entry(r) for r in rows]

    # ------------------------------------------------------------------
    # Read API — read_non_terminal_builds (TASK-PSM-007)
    # ------------------------------------------------------------------

    def read_non_terminal_builds(self) -> list[BuildRow]:
        """Return every build whose status is **not** terminal.

        Used by the boot-time crash-recovery reconciliation pass
        (TASK-PSM-007 / :func:`forge.lifecycle.recovery.reconcile_on_boot`).
        Terminal states (``COMPLETE``, ``FAILED``, ``CANCELLED``,
        ``SKIPPED``) are filtered out at the SQL layer so the recovery
        pass never iterates them. ``INTERRUPTED`` is non-terminal and is
        included — the recovery handler treats it as a no-op (the build
        is already awaiting re-pickup).

        Rows are ordered by ``queued_at ASC`` so a deterministic
        recovery order is preserved across runs (the oldest in-flight
        build is reconciled first), which keeps the recovery report's
        ``failures`` list stable across re-runs.

        Returns:
            List of :class:`BuildRow` for every non-terminal build.
        """
        terminal_values = tuple(
            s.value
            for s in (
                BuildState.COMPLETE,
                BuildState.FAILED,
                BuildState.CANCELLED,
                BuildState.SKIPPED,
            )
        )
        placeholders = ",".join(["?"] * len(terminal_values))
        sql = f"""
            SELECT *
              FROM builds
             WHERE status NOT IN ({placeholders})
             ORDER BY queued_at ASC
        """
        with self._reader() as cx:
            rows = cx.execute(sql, terminal_values).fetchall()
        return [_row_to_build_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Read API — exists_active_build
    # ------------------------------------------------------------------

    def exists_active_build(self, feature_id: str) -> bool:
        """Return True iff any build for ``feature_id`` is in an active state.

        AC-008 / Group C "active in-flight duplicate" check.
        """
        if not feature_id:
            raise ValueError("exists_active_build: feature_id must be non-empty")

        active_values = tuple(s.value for s in ACTIVE_STATES)
        placeholders = ",".join(["?"] * len(active_values))
        sql = f"""
            SELECT 1
              FROM builds
             WHERE feature_id = ?
               AND status IN ({placeholders})
             LIMIT 1
        """
        params: list[Any] = [feature_id, *active_values]
        with self._reader() as cx:
            row = cx.execute(sql, params).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Read API — find_active_or_recent
    # ------------------------------------------------------------------

    def find_active_or_recent(self, feature_id: str) -> Build | None:
        """Return the most relevant build for ``feature_id``.

        Group C "cancel of unknown" check: returns the active build for
        the feature if one exists, otherwise the most recently queued
        build regardless of status. Returns ``None`` when the feature
        has never produced a build.
        """
        if not feature_id:
            raise ValueError("find_active_or_recent: feature_id must be non-empty")

        active_values = tuple(s.value for s in ACTIVE_STATES)
        active_placeholders = ",".join(["?"] * len(active_values))
        with self._reader() as cx:
            row = cx.execute(
                f"""
                SELECT build_id, status
                  FROM builds
                 WHERE feature_id = ?
                   AND status IN ({active_placeholders})
                 ORDER BY queued_at DESC
                 LIMIT 1
                """,
                (feature_id, *active_values),
            ).fetchone()
            if row is None:
                row = cx.execute(
                    """
                    SELECT build_id, status
                      FROM builds
                     WHERE feature_id = ?
                     ORDER BY queued_at DESC
                     LIMIT 1
                    """,
                    (feature_id,),
                ).fetchone()
        if row is None:
            return None
        return Build(
            build_id=row["build_id"],
            status=BuildState(row["status"]),
        )


# ---------------------------------------------------------------------------
# Protocol implementations — Sqlite* facades
# ---------------------------------------------------------------------------


class SqliteBuildSnapshotReader:
    """SQLite-backed :class:`forge.pipeline.cli_steering.BuildSnapshotReader`.

    Maps a builds row's ``status`` onto the four-way
    :class:`~forge.pipeline.cli_steering.BuildLifecycle` taxonomy. The
    SQLite layer cannot directly observe the DeepAgents async-task
    channel, so :attr:`BuildLifecycle.AUTOBUILD_RUNNING` is *not*
    produced by this reader on its own. Production wiring composes
    this reader with an in-memory ``async_tasks`` reader that overrides
    the lifecycle when an autobuild is in flight; the SqliteBuildSnapshotReader
    is the FEAT-FORGE-001 contribution to that consolidation.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def get_snapshot(self, build_id: str) -> BuildSnapshot:
        """Read the build's coarse lifecycle classification from SQLite."""
        if not build_id:
            raise ValueError(
                "SqliteBuildSnapshotReader.get_snapshot: build_id must be non-empty"
            )
        with self._persistence._reader() as cx:
            row = cx.execute(
                "SELECT status, feature_id FROM builds WHERE build_id = ?",
                (build_id,),
            ).fetchone()
            if row is None:
                # Treat unknown builds as terminal-no-op so the cancel
                # handler short-circuits cleanly rather than dispatching
                # a synthetic reject against a phantom row.
                return BuildSnapshot(
                    build_id=build_id,
                    lifecycle=BuildLifecycle.TERMINAL,
                )
            status = BuildState(row["status"])
            feature_id = row["feature_id"]

            paused_stage: StageClass | None = None
            if status is BuildState.PAUSED:
                # Look up the most recent gated stage_log row to find
                # which stage triggered the pause. The taxonomy enum
                # values are dash-separated strings, so we attempt a
                # round-trip; any unknown label leaves paused_stage None.
                gated_row = cx.execute(
                    """
                    SELECT stage_label
                      FROM stage_log
                     WHERE build_id = ?
                       AND gate_mode IS NOT NULL
                       AND status = 'GATED'
                     ORDER BY started_at DESC, id DESC
                     LIMIT 1
                    """,
                    (build_id,),
                ).fetchone()
                if gated_row is not None:
                    try:
                        paused_stage = StageClass(gated_row["stage_label"])
                    except ValueError:
                        paused_stage = None

        if status in (
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        ):
            lifecycle = BuildLifecycle.TERMINAL
            return BuildSnapshot(build_id=build_id, lifecycle=lifecycle)

        if status is BuildState.PAUSED:
            return BuildSnapshot(
                build_id=build_id,
                lifecycle=BuildLifecycle.PAUSED_AT_GATE,
                paused_stage=paused_stage,
                paused_feature_id=feature_id,
            )

        # QUEUED / PREPARING / RUNNING / FINALISING / INTERRUPTED.
        return BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )


class SqliteBuildCanceller:
    """SQLite-backed :class:`~forge.pipeline.cli_steering.BuildCanceller`.

    Composes a terminal ``CANCELLED`` :class:`Transition` and routes it
    through :meth:`SqliteLifecyclePersistence.apply_transition`. The
    rationale is recorded onto ``builds.error`` so the audit trail
    captures *why* the cancel happened.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def mark_cancelled(self, build_id: str, rationale: str) -> Any:
        """Transition the build to terminal ``CANCELLED`` with ``rationale``."""
        if not build_id:
            raise ValueError(
                "SqliteBuildCanceller.mark_cancelled: build_id must be non-empty"
            )
        # Read the current state under the writer connection.
        row = self._persistence._cx.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"SqliteBuildCanceller.mark_cancelled: no build row for "
                f"build_id={build_id!r}"
            )
        current_state = BuildState(
            row["status"] if isinstance(row, sqlite3.Row) else row[0]
        )
        if current_state in (
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        ):
            # Already terminal — no-op (Group D "cancel on terminal").
            logger.info(
                "SqliteBuildCanceller.mark_cancelled: build_id=%s already "
                "terminal (%s); no-op",
                build_id,
                current_state.value,
            )
            return None

        transition = compose_transition(
            Build(build_id=build_id, status=current_state),
            BuildState.CANCELLED,
            error=rationale,
        )
        self._persistence.apply_transition(transition)
        return None


class SqliteBuildResumer:
    """SQLite-backed :class:`~forge.pipeline.cli_steering.BuildResumer`.

    Re-enters ``RUNNING`` from ``PAUSED`` after a permitted skip. If the
    build is not currently ``PAUSED`` we log a warning and return — the
    caller (the CLI steering handler) is the one that already validated
    the lifecycle, so a non-PAUSED resume is a defensive log point.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def resume_after_skip(
        self,
        build_id: str,
        skipped_stage: StageClass,
    ) -> Any:
        """Transition the build from ``PAUSED`` back to ``RUNNING``."""
        if not build_id:
            raise ValueError(
                "SqliteBuildResumer.resume_after_skip: build_id must be non-empty"
            )
        row = self._persistence._cx.execute(
            "SELECT status FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"SqliteBuildResumer.resume_after_skip: no build row for "
                f"build_id={build_id!r}"
            )
        current_state = BuildState(
            row["status"] if isinstance(row, sqlite3.Row) else row[0]
        )
        if current_state is not BuildState.PAUSED:
            logger.warning(
                "SqliteBuildResumer.resume_after_skip: build_id=%s is in "
                "state %s, not PAUSED; refusing to resume after skip on %s",
                build_id,
                current_state.value,
                skipped_stage.value,
            )
            return None

        transition = compose_transition(
            Build(build_id=build_id, status=current_state),
            BuildState.RUNNING,
        )
        self._persistence.apply_transition(transition)
        return None


class SqliteStageLogReader:
    """Reader-only facade over ``stage_log`` rows.

    Wraps :meth:`SqliteLifecyclePersistence.read_stages` so the CLI
    surface (``forge status --full``) and the per-feature sequencer can
    consume a narrow read interface.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def read_stages(self, build_id: str) -> list[StageLogEntry]:
        """Return all stage_log entries for ``build_id`` in chronological order."""
        return self._persistence.read_stages(build_id)


class SqliteStageSkipRecorder:
    """SQLite-backed :class:`~forge.pipeline.cli_steering.StageSkipRecorder`.

    Two methods because the two skip outcomes write distinct
    ``stage_log`` rows: one carrying ``status='SKIPPED'`` (permitted
    skip) and one carrying ``status='GATED'`` with the constitutional
    refusal rationale embedded in ``details_json`` (refused skip; the
    build remains paused).
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def record_skipped(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:
        """Append a SKIPPED stage_log row for ``stage``."""
        now = self._now()
        entry = StageLogEntry(
            build_id=build_id,
            stage_label=stage.value,
            target_kind="local_tool",
            target_identifier="cli-skip",
            status="SKIPPED",
            gate_mode=None,
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={"rationale": rationale, "source": "cli_steering"},
        )
        self._persistence.record_stage(entry)
        return None

    def record_skip_refused(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:
        """Record a constitutional skip-refusal; the build remains paused."""
        now = self._now()
        entry = StageLogEntry(
            build_id=build_id,
            stage_label=stage.value,
            target_kind="local_tool",
            target_identifier="cli-skip",
            status="GATED",
            gate_mode="HARD_STOP",
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={
                "rationale": rationale,
                "source": "cli_steering",
                "refused": True,
            },
        )
        self._persistence.record_stage(entry)
        return None


class SqlitePauseRejectResolver:
    """SQLite-backed :class:`~forge.pipeline.cli_steering.PauseRejectResolver`.

    Synthesises an :class:`ApprovalResponsePayload` with
    ``decision='reject'`` for the cancel-on-paused pathway
    (FEAT-FORGE-004 ASSUM-005) and forwards it to an injected
    "synthetic response injector" callable. The synthetic injector is
    typed as a plain callable to keep the persistence module decoupled
    from :mod:`forge.adapters.nats`.

    Args:
        persistence: The lifecycle persistence facade. Used to read the
            currently-pending ``pending_approval_request_id`` so the
            synthetic reject carries the original request id.
        synthetic_injector: Callable invoked with the synthetic
            ``ApprovalResponsePayload`` (or a raw dict — production
            wires the
            :class:`forge.adapters.nats.synthetic_response_injector.SyntheticResponseInjector`).
    """

    def __init__(
        self,
        persistence: SqliteLifecyclePersistence,
        synthetic_injector: Callable[[Any], Any],
    ) -> None:
        self._persistence = persistence
        self._injector = synthetic_injector

    def resolve_as_reject(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        rationale: str,
    ) -> Any:
        """Build and inject a synthetic ``ApprovalResponsePayload(reject)``."""
        if not build_id:
            raise ValueError(
                "SqlitePauseRejectResolver.resolve_as_reject: build_id "
                "must be non-empty"
            )
        # Read the pending_approval_request_id off the build row.
        row = self._persistence._cx.execute(
            "SELECT pending_approval_request_id FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"SqlitePauseRejectResolver.resolve_as_reject: no build row "
                f"for build_id={build_id!r}"
            )
        request_id = (
            row["pending_approval_request_id"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        if not request_id:
            raise RuntimeError(
                f"SqlitePauseRejectResolver.resolve_as_reject: build "
                f"{build_id!r} has no pending_approval_request_id; cannot "
                "synthesise a reject for a build that is not awaiting an "
                "approval response"
            )

        # Construct the payload as a plain dict so we don't pin the
        # persistence layer to nats_core; the injector is responsible
        # for validating into ``ApprovalResponsePayload`` if it wishes.
        synthetic = {
            "request_id": request_id,
            "decision": "reject",
            "decided_by": "cli-cancel",
            "notes": rationale,
            "_metadata": {
                "build_id": build_id,
                "stage": stage.value,
                "feature_id": feature_id,
                "source": "cli_steering.cancel",
            },
        }
        return self._injector(synthetic)


# ---------------------------------------------------------------------------
# AsyncTaskCanceller / AsyncTaskUpdater — pass-through wrappers
# ---------------------------------------------------------------------------


class AsyncTaskCanceller:
    """Pass-through implementation of
    :class:`~forge.pipeline.cli_steering.AsyncTaskCanceller`.

    The DeepAgents ``cancel_async_task`` middleware tool lives in the
    LangGraph runtime; this class is a thin wrapper that forwards the
    ``task_id`` to an injected callable. No SQLite involvement — the
    class only lives in this module for cohesion with the other
    Protocol implementations the cli_steering handler is wired against.
    """

    def __init__(self, cancel_callable: Callable[[str], Any]) -> None:
        self._cancel = cancel_callable

    def cancel_async_task(self, task_id: str) -> Any:
        if not task_id:
            raise ValueError(
                "AsyncTaskCanceller.cancel_async_task: task_id must be non-empty"
            )
        return self._cancel(task_id)


class AsyncTaskUpdater:
    """Pass-through implementation of
    :class:`~forge.pipeline.cli_steering.AsyncTaskUpdater`.

    Forwards directive appends to an injected callable that wraps the
    DeepAgents ``update_async_task`` middleware tool. No SQLite
    involvement.
    """

    def __init__(
        self,
        update_callable: Callable[..., Any],
    ) -> None:
        self._update = update_callable

    def update_async_task(
        self,
        task_id: str,
        *,
        append_pending_directive: str,
    ) -> Any:
        if not task_id:
            raise ValueError(
                "AsyncTaskUpdater.update_async_task: task_id must be non-empty"
            )
        if not append_pending_directive:
            raise ValueError(
                "AsyncTaskUpdater.update_async_task: append_pending_directive "
                "must be non-empty"
            )
        return self._update(
            task_id,
            append_pending_directive=append_pending_directive,
        )
