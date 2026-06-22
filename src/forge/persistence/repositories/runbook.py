"""``RunbookRepository`` — runbooks/runbook_steps repository (TASK-RSP-003).

The :class:`RunbookRepository` is the SQL-aware writer and reader for the
``runbooks`` and ``runbook_steps`` tables introduced by
:mod:`forge.persistence.migrations.runbook`. Provides the core read/write
foundation: ``create_runbook`` (insert) and ``load_runbook`` (query).

F010C correlation-id contract
-----------------------------

Every public method takes ``correlation_id`` explicitly. The value is used
for structured logging so writes and reads can be traced end-to-end.

Transactional discipline
------------------------

Every write uses ``BEGIN IMMEDIATE`` so concurrent writes serialise correctly
under SQLite's busy-timeout window. The ``_safe_rollback()`` helper ensures
a failed write leaves no half-written state (atomicity).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Mapping

from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepResult,
    StepStatus,
)

logger = logging.getLogger(__name__)


__all__ = [
    "RunbookRepository",
    "RunbookDuplicateError",
    "RunbookNotFoundError",
    "RunbookStepNotFoundError",
    "RunbookAdvanceError",
]


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class RunbookDuplicateError(RuntimeError):
    """Raised by :meth:`RunbookRepository.create_runbook` for duplicate runbook_id.

    The ``RunbookRepository`` does not auto-update on duplicate writes —
    a second create with the same runbook_id is a programming error worth
    surfacing. The original runbook is left untouched.
    """

    def __init__(self, runbook_id: str) -> None:
        super().__init__(
            f"runbook_id={runbook_id!r} already exists; cannot create duplicate"
        )
        self.runbook_id = runbook_id


class RunbookNotFoundError(RuntimeError):
    """Raised when a requested runbook does not exist.

    Used by update/advance operations (TASK-RSP-004).
    """

    def __init__(self, runbook_id: str) -> None:
        super().__init__(f"no runbook found for runbook_id={runbook_id!r}")
        self.runbook_id = runbook_id


class RunbookStepNotFoundError(RuntimeError):
    """Raised when updating a step at an invalid sequence_index.

    The sequence_index must be within the range of existing steps. An
    out-of-range index leaves all steps unchanged (TASK-RSP-004).
    """

    def __init__(self, runbook_id: str, sequence_index: int) -> None:
        super().__init__(
            f"no step at sequence_index={sequence_index} for runbook_id={runbook_id!r}"
        )
        self.runbook_id = runbook_id
        self.sequence_index = sequence_index


class RunbookAdvanceError(RuntimeError):
    """Raised when attempting to advance beyond the terminal position.

    Per ASSUM-004 (R1), current_step_index may rest at the terminal position
    len(steps) (the completion marker); advancing beyond it is refused
    (TASK-RSP-004, reconciled with FEAT-RBX).
    """

    def __init__(self, runbook_id: str, current_index: int) -> None:
        super().__init__(
            f"cannot advance runbook_id={runbook_id!r} beyond terminal position "
            f"(current_step_index={current_index})"
        )
        self.runbook_id = runbook_id
        self.current_index = current_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_params(params: Mapping[str, Any]) -> str:
    """Encode step params as JSON TEXT.

    An empty mapping becomes '{}' (not NULL) per the schema default.
    """
    return json.dumps(params if params else {})


def _decode_params(raw: str | None) -> Mapping[str, Any]:
    """Decode JSON-encoded params column.

    A missing or empty value yields an empty dict.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("runbook: params column contained invalid JSON (%r)", raw)
        return {}
    if not isinstance(decoded, dict):
        logger.warning("runbook: params JSON not a dict (%r)", decoded)
        return {}
    return decoded


def _encode_result(result: StepResult | None) -> str | None:
    """Encode step result as JSON TEXT, or None if no result yet."""
    if result is None:
        return None
    return json.dumps(
        {
            "exit_code": result.exit_code,
            "captured_output": result.captured_output,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
        }
    )


def _decode_result(raw: str | None) -> StepResult | None:
    """Decode JSON-encoded result column, or None if NULL."""
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("runbook: result column contained invalid JSON (%r)", raw)
        return None
    if not isinstance(decoded, dict):
        logger.warning("runbook: result JSON not a dict (%r)", decoded)
        return None
    from datetime import datetime

    return StepResult(
        exit_code=decoded["exit_code"],
        captured_output=decoded["captured_output"],
        started_at=datetime.fromisoformat(decoded["started_at"]),
        completed_at=datetime.fromisoformat(decoded["completed_at"]),
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class RunbookRepository:
    """SQLite-backed repository over the ``runbooks`` and ``runbook_steps`` tables.

    Args:
        connection: Writer ``sqlite3.Connection`` produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`. The
            repository assumes autocommit isolation and manages
            transactions via explicit ``BEGIN IMMEDIATE`` / ``COMMIT``.
    """

    def __init__(self, *, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError(
                "RunbookRepository: connection must be a sqlite3.Connection; "
                f"got {type(connection).__name__}"
            )
        self._cx = connection
        if connection.row_factory is None:
            connection.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Write API — create_runbook (INSERT)
    # ------------------------------------------------------------------

    def create_runbook(
        self,
        runbook: Runbook,
        *,
        correlation_id: str,
    ) -> None:
        """Persist a new runbook with all its steps atomically.

        Writes the runbook row and all step rows in a single transaction.
        A duplicate runbook_id raises ``RunbookDuplicateError``; the
        original runbook is left untouched.

        Args:
            runbook: The :class:`Runbook` to persist.
            correlation_id: Correlation ID for tracing this write.

        Raises:
            TypeError: If ``runbook`` is not a :class:`Runbook`.
            ValueError: If ``correlation_id`` is empty.
            ValueError: If the runbook has no steps (enforced by the
                model's ``__post_init__`` via RunbookValidationError).
            RunbookDuplicateError: If a runbook with the same
                ``runbook_id`` already exists.
            sqlite3.Error: For any database error. The transaction is
                rolled back so no partial writes remain.
        """
        if not isinstance(runbook, Runbook):
            raise TypeError(
                "RunbookRepository.create_runbook: runbook must be a Runbook; "
                f"got {type(runbook).__name__}"
            )
        if not correlation_id:
            raise ValueError(
                "RunbookRepository.create_runbook: correlation_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")

            # Insert runbook row
            self._cx.execute(
                """
                INSERT INTO runbooks (
                    runbook_id, target, current_step_index, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    runbook.runbook_id,
                    runbook.target,
                    runbook.current_step_index,
                    runbook.status.value,
                    runbook.created_at.isoformat(),
                ),
            )

            # Insert all step rows
            for step in runbook.steps:
                params_json = _encode_params(step.params)
                result_json = _encode_result(step.result)
                self._cx.execute(
                    """
                    INSERT INTO runbook_steps (
                        runbook_id, sequence_index, step_type, params, status, result
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        runbook.runbook_id,
                        step.sequence_index,
                        step.step_type,
                        params_json,
                        step.status.value,
                        result_json,
                    ),
                )

            self._cx.execute("COMMIT;")
        except sqlite3.IntegrityError as exc:
            self._safe_rollback()
            # Check if it's a duplicate runbook_id (PRIMARY KEY violation)
            if "PRIMARY KEY" in str(exc) or "UNIQUE" in str(exc).upper():
                raise RunbookDuplicateError(runbook.runbook_id) from exc
            raise
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "runbook.create_runbook runbook_id=%s correlation_id=%s step_count=%d",
            runbook.runbook_id,
            correlation_id,
            len(runbook.steps),
        )

    # ------------------------------------------------------------------
    # Read API — load_runbook (SELECT)
    # ------------------------------------------------------------------

    def load_runbook(
        self,
        runbook_id: str,
        *,
        correlation_id: str,
    ) -> Runbook | None:
        """Load a runbook by ID, returning None if it doesn't exist.

        Reconstructs the full :class:`Runbook` object with all steps
        ordered by ``sequence_index``.

        Args:
            runbook_id: Primary key of the runbook to load.
            correlation_id: Correlation ID for tracing this read.

        Returns:
            The :class:`Runbook` if found, or ``None`` if no such
            runbook exists.

        Raises:
            ValueError: If ``runbook_id`` or ``correlation_id`` is empty.
            sqlite3.Error: For any database error.
        """
        if not runbook_id:
            raise ValueError(
                "RunbookRepository.load_runbook: runbook_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "RunbookRepository.load_runbook: correlation_id must be non-empty"
            )

        # Load runbook row
        runbook_row = self._cx.execute(
            """
            SELECT runbook_id, target, current_step_index, status, created_at
            FROM runbooks
            WHERE runbook_id = ?
            """,
            (runbook_id,),
        ).fetchone()

        if runbook_row is None:
            logger.debug(
                "runbook.load_runbook runbook_id=%s correlation_id=%s result=not_found",
                runbook_id,
                correlation_id,
            )
            return None

        # Load step rows, ordered by sequence_index
        step_rows = self._cx.execute(
            """
            SELECT sequence_index, step_type, params, status, result
            FROM runbook_steps
            WHERE runbook_id = ?
            ORDER BY sequence_index ASC
            """,
            (runbook_id,),
        ).fetchall()

        # Reconstruct steps
        steps = []
        for row in step_rows:
            params = _decode_params(row[2])
            status = StepStatus(row[3])
            result = _decode_result(row[4])
            steps.append(
                Step(
                    step_type=row[1],
                    params=params,
                    status=status,
                    sequence_index=row[0],
                    result=result,
                )
            )

        # Reconstruct runbook
        from datetime import datetime

        runbook = Runbook(
            runbook_id=runbook_row[0],
            target=runbook_row[1],
            current_step_index=runbook_row[2],
            status=StepStatus(runbook_row[3]),
            created_at=datetime.fromisoformat(runbook_row[4]),
            steps=tuple(steps),
        )

        logger.debug(
            "runbook.load_runbook runbook_id=%s correlation_id=%s step_count=%d",
            runbook_id,
            correlation_id,
            len(steps),
        )

        return runbook

    # ------------------------------------------------------------------
    # Write API — update_step_status (UPDATE runbook_steps)
    # ------------------------------------------------------------------

    def update_step_status(
        self,
        runbook_id: str,
        sequence_index: int,
        status: StepStatus,
        *,
        correlation_id: str,
        result: StepResult | None = None,
    ) -> None:
        """Update a step's status and optionally its result.

        Updates the status and result columns for the step at the given
        sequence_index. The update is atomic via BEGIN IMMEDIATE transaction.

        Args:
            runbook_id: Primary key of the runbook.
            sequence_index: 0-based position of the step to update.
            status: New status to set (must be a valid StepStatus member).
            correlation_id: Correlation ID for tracing this write.
            result: Optional execution result to persist.

        Raises:
            ValueError: If correlation_id is empty or status is not a StepStatus.
            RunbookValidationError: If status is not a valid StepStatus member.
            RunbookStepNotFoundError: If sequence_index is out of range.
            sqlite3.Error: For any database error. The transaction is
                rolled back so no partial writes remain.
        """
        if not correlation_id:
            raise ValueError(
                "RunbookRepository.update_step_status: correlation_id must be non-empty"
            )

        # Validate status is a StepStatus member BEFORE the write
        if not isinstance(status, StepStatus):
            from forge.persistence.repositories.runbook_models import (
                RunbookValidationError,
            )

            raise RunbookValidationError(
                f"status must be a StepStatus member; got {status!r}"
            )

        result_json = _encode_result(result)

        try:
            self._cx.execute("BEGIN IMMEDIATE;")

            cursor = self._cx.execute(
                """
                UPDATE runbook_steps
                SET status = ?, result = ?
                WHERE runbook_id = ? AND sequence_index = ?
                """,
                (status.value, result_json, runbook_id, sequence_index),
            )

            if cursor.rowcount == 0:
                self._safe_rollback()
                raise RunbookStepNotFoundError(runbook_id, sequence_index)

            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "runbook.update_step_status runbook_id=%s sequence_index=%d status=%s correlation_id=%s",
            runbook_id,
            sequence_index,
            status.value,
            correlation_id,
        )

    # ------------------------------------------------------------------
    # Write API — try_claim_step_for_execution (atomic claim of a runnable step)
    # ------------------------------------------------------------------

    def try_claim_step_for_execution(
        self,
        runbook_id: str,
        sequence_index: int,
        *,
        correlation_id: str,
    ) -> bool:
        """Atomically claim a runnable step for execution (TASK-RBX-007 concurrency).

        Transitions the step at ``sequence_index`` from a *runnable* status
        (``pending``; ``failed`` — retry on resume; ``awaiting_approval`` —
        re-attempt the gate) to ``running`` in a single ``BEGIN IMMEDIATE``
        transaction, and reports whether *this* caller won the claim. Two
        executors racing on the same runbook serialise: exactly one observes
        ``rowcount == 1`` (claimed) while the other observes ``rowcount == 0``
        and must skip the step. This is the no-double-run guarantee the executor
        relies on — it never runs a handler for a step it did not claim.

        A ``passed`` step (already done — the executor advances past it via its
        recovery shortcut) and a ``running`` step (already claimed by a
        concurrent executor) are NOT claimable.

        Args:
            runbook_id: Primary key of the runbook.
            sequence_index: 0-based position of the step to claim.
            correlation_id: Correlation ID for tracing this write.

        Returns:
            True if this caller transitioned the step to ``running``; False if
            the step was ``passed``/``running`` or does not exist. ``failed`` and
            ``awaiting_approval`` ARE claimable so a resumed run retries them.

        Raises:
            ValueError: If correlation_id is empty.
            sqlite3.Error: For any database error. The transaction is rolled
                back so no partial writes remain.
        """
        if not correlation_id:
            raise ValueError(
                "RunbookRepository.try_claim_step_for_execution: "
                "correlation_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            cursor = self._cx.execute(
                """
                UPDATE runbook_steps
                SET status = ?
                WHERE runbook_id = ? AND sequence_index = ?
                  AND status IN (?, ?, ?)
                """,
                (
                    StepStatus.running.value,
                    runbook_id,
                    sequence_index,
                    StepStatus.pending.value,
                    StepStatus.failed.value,
                    StepStatus.awaiting_approval.value,
                ),
            )
            claimed = cursor.rowcount == 1
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "runbook.try_claim_step_for_execution runbook_id=%s sequence_index=%d "
            "claimed=%s correlation_id=%s",
            runbook_id,
            sequence_index,
            claimed,
            correlation_id,
        )
        return claimed

    # ------------------------------------------------------------------
    # Write API — advance (UPDATE runbooks.current_step_index)
    # ------------------------------------------------------------------

    def advance(
        self,
        runbook_id: str,
        *,
        correlation_id: str,
    ) -> None:
        """Advance the runbook's resume pointer to the next step.

        Increments current_step_index by one. Advancing from the final step to
        the terminal position (current_step_index == step_count, the completion
        marker) is allowed; advancing beyond the terminal position is refused
        (ASSUM-004, R1). The advance is atomic via BEGIN IMMEDIATE.

        Args:
            runbook_id: Primary key of the runbook.
            correlation_id: Correlation ID for tracing this write.

        Raises:
            ValueError: If correlation_id is empty.
            RunbookNotFoundError: If the runbook does not exist.
            RunbookAdvanceError: If already at the terminal position.
            sqlite3.Error: For any database error. The transaction is
                rolled back so no partial writes remain.
        """
        if not correlation_id:
            raise ValueError(
                "RunbookRepository.advance: correlation_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")

            # Read current state
            row = self._cx.execute(
                """
                SELECT current_step_index,
                       (SELECT COUNT(*) FROM runbook_steps WHERE runbook_id = ?) AS step_count
                FROM runbooks
                WHERE runbook_id = ?
                """,
                (runbook_id, runbook_id),
            ).fetchone()

            if row is None:
                self._safe_rollback()
                raise RunbookNotFoundError(runbook_id)

            current_index = row[0]
            step_count = row[1]

            # Refuse only at the terminal position — advancing from the final
            # step to current_step_index == step_count (the completion marker)
            # is allowed (ASSUM-004, R1, reconciled with FEAT-RBX).
            if current_index >= step_count:
                self._safe_rollback()
                raise RunbookAdvanceError(runbook_id, current_index)

            # Increment the pointer
            self._cx.execute(
                """
                UPDATE runbooks
                SET current_step_index = current_step_index + 1
                WHERE runbook_id = ?
                """,
                (runbook_id,),
            )

            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "runbook.advance runbook_id=%s new_index=%d correlation_id=%s",
            runbook_id,
            current_index + 1,
            correlation_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_rollback(self) -> None:
        """Roll back swallowing secondary errors so the original raise survives."""
        try:
            self._cx.execute("ROLLBACK;")
        except sqlite3.Error:  # pragma: no cover - rollback failure is rare
            logger.exception("runbook rollback failed")
