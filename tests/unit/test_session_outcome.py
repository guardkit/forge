"""Unit tests for ``forge.memory.session_outcome`` (TASK-IC-007).

Each test class maps to one or more acceptance criteria from
``tasks/design_approved/TASK-IC-007-session-outcome-writer.md``:

* :class:`TestPreWriteExistenceCheck`        — AC-001/AC-002 (pre-write
                                                existence check + no-op return).
* :class:`TestGateDecisionCollection`        — AC-003 (decisions collected
                                                from the SQLite repo).
* :class:`TestOrderingByDecidedAt`           — AC-004 (ASSUM-008 resolution:
                                                sort ASC by decided_at,
                                                deterministic tiebreaker).
* :class:`TestSynchronousWrite`              — AC-005 (writes via
                                                ``write_entity``, awaits
                                                completion, NOT fire-and-
                                                forget).
* :class:`TestTerminalOnly`                  — AC-006 (``@edge-case
                                                no-in-progress-session-outcome``).
* :class:`TestConcurrentDedupeContract`      — AC-007 (``@concurrency
                                                gate-decisions-in-close-
                                                succession`` + split-brain
                                                safety from TASK-IC-001
                                                ``entity_id`` contract).

Style: each test follows AAA (arrange / act / assert) and uses
deterministic timestamps via :func:`_ts`. Async paths run via
:func:`asyncio.run` so no pytest-asyncio plugin / fixture is required —
matches the convention already established in
``tests/unit/test_writer.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Sequence
from uuid import UUID, uuid4

import pytest

from forge.memory.models import GateDecision, SessionOutcome
from forge.memory.session_outcome import (
    PIPELINE_HISTORY_GROUP_ID,
    _is_terminal,
    _session_outcome_entity_id,
    _sort_gate_decisions,
    write_session_outcome,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ts(minute: int = 0, microsecond: int = 0) -> datetime:
    """Deterministic UTC timestamp keyed on minute / microsecond."""
    return datetime(2026, 4, 26, 12, minute, 0, microsecond, tzinfo=UTC)


def _make_gate_decision(
    *,
    decided_at: datetime,
    entity_id: UUID | None = None,
    stage_name: str = "planning",
) -> GateDecision:
    """Build a valid :class:`GateDecision` for tests."""
    return GateDecision(
        entity_id=entity_id or uuid4(),
        stage_name=stage_name,
        decided_at=decided_at,
        score=0.9,
        criterion_breakdown={"completeness": 1.0},
        rationale="ok",
    )


class _FakeRepo:
    """In-memory :class:`PipelineHistoryRepository` test double."""

    def __init__(self, decisions: Sequence[GateDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[str] = []

    def get_gate_decisions_for_build(
        self, build_id: str
    ) -> Sequence[GateDecision]:
        self.calls.append(build_id)
        # Return the list as-is, intentionally *unsorted* in some
        # tests, so the writer's ordering guarantee is exercised.
        return list(self._decisions)


class _RecordingWrite:
    """Callable that records ``write_entity`` invocations."""

    def __init__(self, *, raise_with: BaseException | None = None) -> None:
        self.calls: list[tuple[SessionOutcome, str]] = []
        self._raise_with = raise_with

    async def __call__(self, entity: SessionOutcome, group_id: str) -> None:
        self.calls.append((entity, group_id))
        if self._raise_with is not None:
            raise self._raise_with


def _make_exists_check(answer: bool) -> "_RecordingExistsCheck":
    """Build a recording exists-check returning ``answer``."""
    return _RecordingExistsCheck(answer=answer)


class _RecordingExistsCheck:
    """Async exists-check that records every ``build_id`` queried."""

    def __init__(self, *, answer: bool) -> None:
        self.calls: list[str] = []
        self._answer = answer

    async def __call__(self, build_id: str) -> bool:
        self.calls.append(build_id)
        return self._answer


# ---------------------------------------------------------------------------
# AC-001 / AC-002 — pre-write existence check + no-op return
# ---------------------------------------------------------------------------


class TestPreWriteExistenceCheck:
    """The writer queries Graphiti before writing and no-ops on hit (AC-001/002)."""

    def test_existence_check_called_with_build_id(self) -> None:
        """The ``build_id`` is passed verbatim to the exists-check."""
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert exists.calls == ["build-42"]

    def test_existing_outcome_returns_none_no_op(self) -> None:
        """If the outcome already exists, the writer returns ``None``."""
        repo = _FakeRepo([_make_gate_decision(decided_at=_ts())])
        exists = _make_exists_check(answer=True)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert result is None

    def test_existing_outcome_does_not_write(self) -> None:
        """No write happens when existence check returns ``True``."""
        repo = _FakeRepo([_make_gate_decision(decided_at=_ts())])
        exists = _make_exists_check(answer=True)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert write.calls == []

    def test_existing_outcome_does_not_query_repo(self) -> None:
        """Idempotent path skips the SQLite read entirely.

        Avoids a useless query against the SQLite ledger when the
        Graphiti row already exists — the saved I/O is small per call
        but adds up across reconcile-backfill replays.
        """
        repo = _FakeRepo([_make_gate_decision(decided_at=_ts())])
        exists = _make_exists_check(answer=True)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert repo.calls == []


# ---------------------------------------------------------------------------
# AC-003 — gate decisions are collected from the SQLite repo
# ---------------------------------------------------------------------------


class TestGateDecisionCollection:
    """The writer reads :class:`GateDecision` rows from the SQLite repo (AC-003)."""

    def test_repo_queried_with_build_id(self) -> None:
        """``get_gate_decisions_for_build`` receives the same ``build_id``."""
        repo = _FakeRepo([_make_gate_decision(decided_at=_ts())])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert repo.calls == ["build-42"]

    def test_empty_decision_list_still_writes_outcome(self) -> None:
        """A build with zero decisions produces an empty list, not an error.

        ``SessionOutcome.gate_decision_ids`` defaults to ``[]``; an
        aborted-pre-first-stage build legitimately has no decisions.
        """
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "aborted",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert isinstance(result, SessionOutcome)
        assert result.gate_decision_ids == []
        assert len(write.calls) == 1

    def test_repo_without_required_method_raises_typeerror(self) -> None:
        """A ``sqlite_repo`` missing the required method is rejected up-front."""
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        class _BadRepo:
            pass

        with pytest.raises(TypeError, match="get_gate_decisions_for_build"):
            asyncio.run(
                write_session_outcome(
                    "build-42",
                    "success",
                    _BadRepo(),  # type: ignore[arg-type]
                    exists_check=exists,
                    write=write,
                )
            )


# ---------------------------------------------------------------------------
# AC-004 — sort ASC by decided_at (ASSUM-008 resolution)
# ---------------------------------------------------------------------------


class TestOrderingByDecidedAt:
    """``gate_decision_ids`` is sorted ASC by ``decided_at`` (ASSUM-008)."""

    def test_out_of_order_decisions_are_sorted_on_output(self) -> None:
        """Inserting decisions late→early produces an early→late list."""
        a = _make_gate_decision(decided_at=_ts(minute=0))
        b = _make_gate_decision(decided_at=_ts(minute=1))
        c = _make_gate_decision(decided_at=_ts(minute=2))
        # Repo returns them out of order.
        repo = _FakeRepo([c, a, b])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert result is not None
        assert result.gate_decision_ids == [a.entity_id, b.entity_id, c.entity_id]

    def test_microsecond_tied_timestamps_use_entity_id_tiebreaker(self) -> None:
        """Two decisions at the same instant order by ``entity_id`` ASC.

        Stable sort would otherwise preserve repository insertion
        order, which is implementation-defined for the SQLite layer.
        """
        same_ts = _ts(minute=0, microsecond=500_000)
        # Pin two entity_ids in a known order (lexically a < b).
        id_a = UUID("00000000-0000-0000-0000-000000000001")
        id_b = UUID("ffffffff-ffff-ffff-ffff-fffffffffffe")
        gate_a = _make_gate_decision(decided_at=same_ts, entity_id=id_a)
        gate_b = _make_gate_decision(decided_at=same_ts, entity_id=id_b)
        # Insert in reverse order to prove the tiebreaker is at work.
        repo = _FakeRepo([gate_b, gate_a])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert result is not None
        assert result.gate_decision_ids == [id_a, id_b]

    def test_pure_helper_does_not_mutate_input(self) -> None:
        """``_sort_gate_decisions`` returns a new list."""
        a = _make_gate_decision(decided_at=_ts(minute=2))
        b = _make_gate_decision(decided_at=_ts(minute=1))
        original = [a, b]

        result = _sort_gate_decisions(original)

        assert original == [a, b]  # input untouched
        assert [d.entity_id for d in result] == [b.entity_id, a.entity_id]


# ---------------------------------------------------------------------------
# AC-005 — synchronous write via write_entity (NOT fire-and-forget)
# ---------------------------------------------------------------------------


class TestSynchronousWrite:
    """The writer awaits the underlying ``write_entity`` (AC-005)."""

    def test_write_invoked_with_session_outcome_and_group_id(self) -> None:
        """The pipeline-history group_id and a built entity reach the writer."""
        gate = _make_gate_decision(decided_at=_ts())
        repo = _FakeRepo([gate])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "failure",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert isinstance(result, SessionOutcome)
        assert len(write.calls) == 1
        entity, group_id = write.calls[0]
        assert group_id == PIPELINE_HISTORY_GROUP_ID
        assert entity.build_id == "build-42"
        assert entity.outcome == "failure"
        assert entity.gate_decision_ids == [gate.entity_id]

    def test_write_failure_propagates_to_caller(self) -> None:
        """Synchronous-failure variant — terminal-state caller wants confirmation."""
        gate = _make_gate_decision(decided_at=_ts())
        repo = _FakeRepo([gate])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite(raise_with=RuntimeError("graphiti boom"))

        with pytest.raises(RuntimeError, match="graphiti boom"):
            asyncio.run(
                write_session_outcome(
                    "build-42",
                    "success",
                    repo,
                    exists_check=exists,
                    write=write,
                )
            )

    def test_closed_at_default_is_recent_utc(self) -> None:
        """Default ``closed_at`` is "now (UTC)" — never naive, never far past.

        Asserts within a generous wall-clock window because the test
        itself takes some milliseconds to run.
        """
        before = datetime.now(tz=UTC) - timedelta(seconds=5)
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )
        after = datetime.now(tz=UTC) + timedelta(seconds=5)

        assert result is not None
        assert result.closed_at.tzinfo is not None
        assert before <= result.closed_at <= after

    def test_caller_supplied_closed_at_is_preserved(self) -> None:
        """An explicit ``closed_at`` is threaded through verbatim."""
        explicit = _ts(minute=42)
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
                closed_at=explicit,
            )
        )

        assert result is not None
        assert result.closed_at == explicit


# ---------------------------------------------------------------------------
# AC-006 — terminal-only (@edge-case no-in-progress-session-outcome)
# ---------------------------------------------------------------------------


class TestTerminalOnly:
    """Non-terminal outcomes do not produce a SessionOutcome (AC-006)."""

    @pytest.mark.parametrize(
        "non_terminal",
        ["in_progress", "running", "pending", "queued", "", "SUCCESS"],
    )
    def test_non_terminal_outcome_returns_none(self, non_terminal: str) -> None:
        """The writer no-ops when called with a non-terminal outcome string.

        Includes ``"SUCCESS"`` (uppercase) to prove the comparison is
        case-sensitive — the canonical terminal kinds are lowercase
        per :data:`SessionOutcomeKind`.
        """
        repo = _FakeRepo([_make_gate_decision(decided_at=_ts())])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        result = asyncio.run(
            write_session_outcome(
                "build-42",
                non_terminal,
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert result is None
        assert write.calls == []

    def test_non_terminal_does_not_query_existence(self) -> None:
        """Non-terminal short-circuit happens before the existence check.

        Saves an unnecessary Graphiti round-trip for callers that
        accidentally invoke the writer mid-pipeline.
        """
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "in_progress",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert exists.calls == []

    def test_non_terminal_logs_warning_with_build_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A WARNING log line surfaces the integration bug to dashboards."""
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()
        caplog.set_level(logging.WARNING, logger="forge.memory.session_outcome")

        asyncio.run(
            write_session_outcome(
                "build-42",
                "running",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        records = [
            r
            for r in caplog.records
            if r.name == "forge.memory.session_outcome"
            and "non_terminal" in r.message
        ]
        assert records, "expected a non-terminal warning log line"
        assert getattr(records[0], "build_id", None) == "build-42"
        assert getattr(records[0], "outcome", None) == "running"

    @pytest.mark.parametrize("terminal", ["success", "failure", "aborted"])
    def test_terminal_predicate_accepts_canonical_kinds(self, terminal: str) -> None:
        """``_is_terminal`` returns ``True`` for every canonical kind."""
        assert _is_terminal(terminal) is True

    @pytest.mark.parametrize("non_terminal", ["in_progress", "", "SUCCESS"])
    def test_terminal_predicate_rejects_others(self, non_terminal: str) -> None:
        """``_is_terminal`` returns ``False`` for everything else."""
        assert _is_terminal(non_terminal) is False


# ---------------------------------------------------------------------------
# AC-007 — concurrent dedupe + entity_id contract from TASK-IC-001
# ---------------------------------------------------------------------------


class TestConcurrentDedupeContract:
    """Concurrent writers share an ``entity_id`` for the same build (AC-007)."""

    def test_entity_id_is_deterministic_for_build_id(self) -> None:
        """Same ``build_id`` → same ``entity_id`` (Graphiti upsert key)."""
        first = _session_outcome_entity_id("build-42")
        second = _session_outcome_entity_id("build-42")
        assert first == second

    def test_entity_id_differs_for_distinct_build_ids(self) -> None:
        """Different ``build_id`` → different ``entity_id``."""
        a = _session_outcome_entity_id("build-1")
        b = _session_outcome_entity_id("build-2")
        assert a != b

    def test_entity_id_factory_rejects_empty_build_id(self) -> None:
        """Empty / non-string ``build_id`` is rejected at the factory boundary."""
        with pytest.raises(ValueError, match="non-empty"):
            _session_outcome_entity_id("")

    def test_concurrent_callers_one_writes_one_no_ops(self) -> None:
        """Exactly one of two concurrent calls writes; the other no-ops.

        Simulates the two-Forge-instances scenario: the first caller
        sees ``exists=False`` and writes; the second caller (running
        a moment later) sees ``exists=True`` because the first
        already populated Graphiti.
        """
        gate = _make_gate_decision(decided_at=_ts())
        repo = _FakeRepo([gate])

        # Stateful exists-check: returns ``False`` once, then ``True``
        # forever — mirroring Graphiti's view after the first write.
        class _StatefulExists:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, build_id: str) -> bool:
                exists = self.calls > 0
                self.calls += 1
                return exists

        exists = _StatefulExists()
        write = _RecordingWrite()

        first = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )
        second = asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert isinstance(first, SessionOutcome)
        assert second is None
        assert len(write.calls) == 1

    def test_split_brain_writes_share_entity_id(self) -> None:
        """If the existence check races, both writes share ``entity_id``.

        Graphiti's storage-layer upsert collapses the two writes into
        one row because the ``entity_id`` is identical. We verify
        that two writes generated for the same ``build_id`` carry
        the same id — that's the dedupe contract from TASK-IC-001.
        """
        repo = _FakeRepo([])
        # Both callers see ``False`` — the race condition.
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )
        asyncio.run(
            write_session_outcome(
                "build-42",
                "success",
                repo,
                exists_check=exists,
                write=write,
            )
        )

        assert len(write.calls) == 2
        first_entity, _ = write.calls[0]
        second_entity, _ = write.calls[1]
        assert first_entity.entity_id == second_entity.entity_id


# ---------------------------------------------------------------------------
# Boundary checks — input validation
# ---------------------------------------------------------------------------


class TestBoundaryInputValidation:
    """Input validation at the public boundary."""

    def test_empty_build_id_raises_value_error(self) -> None:
        """Empty string is rejected by the entry point."""
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(
                write_session_outcome(
                    "",
                    "success",
                    repo,
                    exists_check=exists,
                    write=write,
                )
            )

    def test_non_string_build_id_raises_value_error(self) -> None:
        """Non-string ``build_id`` is rejected with ``ValueError``."""
        repo = _FakeRepo([])
        exists = _make_exists_check(answer=False)
        write = _RecordingWrite()

        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(
                write_session_outcome(
                    123,  # type: ignore[arg-type]
                    "success",
                    repo,
                    exists_check=exists,
                    write=write,
                )
            )
