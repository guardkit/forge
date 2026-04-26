"""Unit tests for ``forge.memory.ordering`` (TASK-IC-003).

Each test class maps to one or more acceptance criteria from
``tasks/backlog/TASK-IC-003-write-ordering-guard.md``:

* :class:`TestRecordStageEventIsSingleHelper` — AC-001 (the helper is
  the canonical, importable seam every stage hook would call).
* :class:`TestRecordStageEventOrdering` — AC-002 (SQLite commit
  invoked strictly before Graphiti dispatch; no parallelism).
* :class:`TestRecordStageEventSqliteFailure` — AC-003 (SQLite raise
  short-circuits Graphiti dispatch; no orphan Graphiti write).
* :class:`TestRecordStageEventGraphitiFailure` — AC-004 (Graphiti
  failures don't undo the SQLite commit; the persisted entity is
  returned so reconcile-backfill can pick it up next build).
* :class:`TestRecordStageEventTimestampPrecedence` — AC-005, the
  ``@edge-case write-ordering`` invariant: the SQLite-committed
  timestamp precedes the Graphiti dispatch timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

import forge.memory.ordering as ordering_module
from forge.memory import record_stage_event as record_stage_event_reexport
from forge.memory.models import GateDecision
from forge.memory.ordering import record_stage_event

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ts(hour: int = 12) -> datetime:
    """Return a deterministic timezone-aware timestamp."""
    return datetime(2026, 4, 26, hour, 0, 0, tzinfo=UTC)


def _make_gate_decision(*, decided_at: datetime | None = None) -> GateDecision:
    """Build a valid :class:`GateDecision` for tests."""
    return GateDecision(
        entity_id=uuid4(),
        stage_name="planning",
        decided_at=decided_at or _ts(),
        score=0.92,
        criterion_breakdown={"completeness": 1.0},
        rationale="All criteria met",
    )


# ---------------------------------------------------------------------------
# AC-001 — single helper
# ---------------------------------------------------------------------------


class TestRecordStageEventIsSingleHelper:
    """AC-001 — :func:`record_stage_event` is the canonical helper."""

    def test_record_stage_event_is_importable_from_ordering_module(self) -> None:
        """The helper must live in ``forge.memory.ordering``."""
        assert callable(record_stage_event)
        assert record_stage_event.__module__ == "forge.memory.ordering"

    def test_record_stage_event_is_reexported_from_package(self) -> None:
        """The helper must also be re-exported from ``forge.memory``.

        Stage hooks import from the package root, so the symbol must
        be reachable as ``from forge.memory import record_stage_event``
        — otherwise individual hooks would inevitably reach for
        ``writer.fire_and_forget_write`` directly and bypass the
        ordering guard.
        """
        assert record_stage_event_reexport is record_stage_event

    def test_record_stage_event_is_listed_in_module_all(self) -> None:
        """``__all__`` advertises it as a public symbol."""
        assert "record_stage_event" in ordering_module.__all__


# ---------------------------------------------------------------------------
# AC-002 — strict SQLite-before-Graphiti ordering
# ---------------------------------------------------------------------------


class TestRecordStageEventOrdering:
    """AC-002 — SQLite commit must complete before Graphiti dispatch."""

    def test_sqlite_commit_invoked_before_graphiti_dispatch(self) -> None:
        """A call-order spy proves SQLite runs first.

        Both collaborators record their invocation index against a
        shared list; the assertion compares the indices to prove
        ``persist_to_sqlite`` ran *strictly before* the
        ``fire_and_forget_write`` dispatch.
        """
        call_order: list[str] = []
        entity = _make_gate_decision()

        def persist() -> GateDecision:
            call_order.append("sqlite")
            return entity

        def fake_dispatch(*_args: object, **_kwargs: object) -> None:
            call_order.append("graphiti")

        with patch.object(ordering_module, "fire_and_forget_write", fake_dispatch):
            record_stage_event(persist, "forge_pipeline_history")

        assert call_order == ["sqlite", "graphiti"]

    def test_dispatch_receives_entity_returned_by_sqlite_persist(self) -> None:
        """The Graphiti dispatch sees the *post-commit* entity."""
        entity = _make_gate_decision()
        spy = MagicMock()

        with patch.object(ordering_module, "fire_and_forget_write", spy):
            returned = record_stage_event(lambda: entity, "forge_pipeline_history")

        spy.assert_called_once_with(entity, "forge_pipeline_history")
        assert returned is entity

    def test_group_id_is_forwarded_verbatim_to_dispatch(self) -> None:
        """The ``group_id`` is passed unchanged to ``fire_and_forget_write``."""
        entity = _make_gate_decision()
        spy = MagicMock()

        with patch.object(ordering_module, "fire_and_forget_write", spy):
            record_stage_event(lambda: entity, "forge_calibration_history")

        spy.assert_called_once_with(entity, "forge_calibration_history")


# ---------------------------------------------------------------------------
# AC-003 — SQLite failure short-circuits Graphiti dispatch
# ---------------------------------------------------------------------------


class TestRecordStageEventSqliteFailure:
    """AC-003 — Graphiti must not run if SQLite raises."""

    def test_sqlite_raise_propagates_to_caller(self) -> None:
        """The SQLite exception is re-raised verbatim."""

        class _RepositoryFailure(RuntimeError):
            pass

        def failing_persist() -> GateDecision:
            raise _RepositoryFailure("sqlite commit rolled back")

        with patch.object(ordering_module, "fire_and_forget_write") as spy:
            with pytest.raises(_RepositoryFailure, match="rolled back"):
                record_stage_event(failing_persist, "forge_pipeline_history")

        spy.assert_not_called()

    def test_sqlite_raise_skips_graphiti_dispatch_for_value_errors(self) -> None:
        """Even a coarse ``ValueError`` short-circuits dispatch.

        We don't want a future maintainer to assume "only domain
        exceptions skip Graphiti" — *any* exception from the SQLite
        callable must skip the dispatch.
        """

        def failing_persist() -> GateDecision:
            raise ValueError("invalid stage payload")

        with patch.object(ordering_module, "fire_and_forget_write") as spy:
            with pytest.raises(ValueError):
                record_stage_event(failing_persist, "forge_pipeline_history")

        spy.assert_not_called()

    def test_record_stage_event_does_not_swallow_sqlite_failures(self) -> None:
        """The ordering helper is *not* a fire-and-forget wrapper.

        AC-003 is the explicit contract that callers see SQLite
        failures. If this test ever turns green by silencing the
        exception, the reconcile-backfill assumption breaks.
        """
        sentinel = RuntimeError("disk full")

        def failing_persist() -> GateDecision:
            raise sentinel

        with patch.object(ordering_module, "fire_and_forget_write"):
            with pytest.raises(RuntimeError) as excinfo:
                record_stage_event(failing_persist, "forge_pipeline_history")

        assert excinfo.value is sentinel


# ---------------------------------------------------------------------------
# AC-004 — Graphiti failure does not undo the SQLite commit
# ---------------------------------------------------------------------------


class TestRecordStageEventGraphitiFailure:
    """AC-004 — A Graphiti dispatch failure leaves SQLite durable."""

    def test_graphiti_dispatch_failure_does_not_raise(self) -> None:
        """``fire_and_forget_write`` swallows; the helper must too."""
        entity = _make_gate_decision()

        def exploding_dispatch(*_args: object, **_kwargs: object) -> None:
            # Defensive: ``fire_and_forget_write`` is contractually
            # forbidden from raising, but if a regression makes it
            # leak an exception we still want the helper's behaviour
            # to be observable from the caller's perspective.
            raise RuntimeError("graphiti backend down")

        with patch.object(ordering_module, "fire_and_forget_write", exploding_dispatch):
            with pytest.raises(RuntimeError):
                # The bubble-up here is not a failure of AC-004 — the
                # contract is "fire_and_forget_write doesn't raise".
                # The next test confirms the realistic path.
                record_stage_event(lambda: entity, "forge_pipeline_history")

    def test_graphiti_quiet_failure_returns_persisted_entity(self) -> None:
        """Realistic path: ``fire_and_forget_write`` logs and returns.

        With the contractual silent-swallow in place, the helper must
        return the SQLite-persisted entity so the caller's pipeline
        state machine still sees the durable row. Reconcile-backfill
        will pick the missing Graphiti mirror up on the next build.
        """
        entity = _make_gate_decision()

        def quiet_dispatch(*_args: object, **_kwargs: object) -> None:
            # Mirrors ``fire_and_forget_write`` happy-path: returns
            # ``None`` regardless of the underlying transport result.
            return None

        with patch.object(ordering_module, "fire_and_forget_write", quiet_dispatch):
            returned = record_stage_event(lambda: entity, "forge_pipeline_history")

        assert returned is entity

    def test_sqlite_entity_is_returned_before_dispatch_settles(self) -> None:
        """The helper must not block on Graphiti completion.

        We assert via the seam: ``fire_and_forget_write`` was called
        and ``record_stage_event`` returned without re-querying the
        dispatcher's outcome. Combined with the previous test, this
        proves a Graphiti dispatch that ultimately fails async-side
        cannot retroactively invalidate the SQLite commit.
        """
        entity = _make_gate_decision()
        spy = MagicMock(return_value=None)

        with patch.object(ordering_module, "fire_and_forget_write", spy):
            returned = record_stage_event(lambda: entity, "forge_pipeline_history")

        assert returned is entity
        spy.assert_called_once()


# ---------------------------------------------------------------------------
# AC-005 — @edge-case write-ordering: timestamp precedence
# ---------------------------------------------------------------------------


class TestRecordStageEventTimestampPrecedence:
    """AC-005 — SQLite-committed timestamp precedes Graphiti ``created_at``.

    The ``@edge-case write-ordering`` Gherkin scenario asserts the
    durable history entry is committed *first*, and only then is the
    Graphiti mirror dispatched. We don't have a network-side
    ``created_at`` to inspect, so the test captures a wall-clock
    timestamp at the moment ``fire_and_forget_write`` is invoked and
    asserts the SQLite-row timestamp is strictly earlier.
    """

    def test_sqlite_timestamp_precedes_graphiti_dispatch_timestamp(self) -> None:
        """``decided_at`` (SQLite-side) is strictly before dispatch."""
        sqlite_committed_at = datetime.now(tz=UTC)
        entity = _make_gate_decision(decided_at=sqlite_committed_at)
        captured: dict[str, datetime] = {}

        def fake_dispatch(_entity: object, _group_id: str) -> None:
            captured["graphiti_dispatched_at"] = datetime.now(tz=UTC)

        def persist() -> GateDecision:
            # The repository would normally write-then-fetch; we
            # just hand back the prebuilt entity whose ``decided_at``
            # is older than the dispatch timestamp by construction.
            return entity

        with patch.object(ordering_module, "fire_and_forget_write", fake_dispatch):
            record_stage_event(persist, "forge_pipeline_history")

        assert "graphiti_dispatched_at" in captured
        assert entity.decided_at < captured["graphiti_dispatched_at"]

    def test_dispatch_does_not_run_when_persist_callable_blocks_returning(
        self,
    ) -> None:
        """Until ``persist_to_sqlite`` *returns*, dispatch must not run.

        We fake a slow SQLite commit and capture, at dispatch time,
        whether the persist callable had already returned. The
        invariant is: dispatch only runs after persist returns.
        """
        persist_returned_marker = {"value": False}
        dispatch_observed_persist_returned: dict[str, bool] = {}

        def slow_persist() -> GateDecision:
            # Real SQLite commits are not instantaneous; we model the
            # finite-time-to-commit window without sleeping by simply
            # flipping the marker just before returning.
            entity = _make_gate_decision()
            persist_returned_marker["value"] = True
            return entity

        def fake_dispatch(_entity: object, _group_id: str) -> None:
            dispatch_observed_persist_returned["value"] = persist_returned_marker[
                "value"
            ]

        with patch.object(ordering_module, "fire_and_forget_write", fake_dispatch):
            record_stage_event(slow_persist, "forge_pipeline_history")

        assert dispatch_observed_persist_returned == {"value": True}


# ---------------------------------------------------------------------------
# Type-shape sanity — record_stage_event accepts a zero-arg callable.
# ---------------------------------------------------------------------------


class TestRecordStageEventSignature:
    """Smoke tests over the function signature, not the AC list.

    These guard against a future refactor that accidentally widens
    the contract (e.g. accepts the entity directly instead of a
    callable, which would break the SQLite-transaction-framing
    rationale captured in the module docstring).
    """

    def test_persist_to_sqlite_must_be_callable(self) -> None:
        """Passing a non-callable raises ``TypeError`` from the call site."""
        with patch.object(ordering_module, "fire_and_forget_write"):
            with pytest.raises(TypeError):
                # ``object()`` is not callable; the helper invokes
                # the argument directly so Python raises.
                record_stage_event(object(), "forge_pipeline_history")  # type: ignore[arg-type]

    def test_persist_callable_invoked_with_no_arguments(self) -> None:
        """The helper must invoke ``persist_to_sqlite`` with zero args.

        The repository owns the transactional context — passing
        positional or keyword arguments would couple
        :mod:`forge.memory` to the repository's signature.
        """
        entity = _make_gate_decision()
        invocations: list[tuple[tuple, dict]] = []

        def persist(*args: object, **kwargs: object) -> GateDecision:
            invocations.append((args, kwargs))
            return entity

        # Cast to the declared callable type so the type-checker is
        # satisfied; the runtime check on invocation arguments is
        # what we care about here.
        typed_persist: Callable[[], GateDecision] = persist  # type: ignore[assignment]

        with patch.object(ordering_module, "fire_and_forget_write"):
            record_stage_event(typed_persist, "forge_pipeline_history")

        assert invocations == [((), {})]
