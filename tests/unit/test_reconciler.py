"""Unit tests for ``forge.memory.reconciler`` (TASK-IC-004).

Documentation level for this task is *minimal* (2 files total — one
source, one test), so every acceptance criterion is consolidated into
this single module. Each AC from
``tasks/design_approved/TASK-IC-004-reconcile-backfill.md`` maps to a
test class below.

Acceptance-criterion → test-class map
-------------------------------------

* AC: reads SQLite via the FEAT-FORGE-001 ``PipelineHistoryRepository``
  interface, NEVER via direct SQLite SQL or schema knowledge.
  → :class:`TestRepositoryProtocolBoundary`

* AC: queries Graphiti for entity_ids in ``forge_pipeline_history``
  within the horizon window.
  → :class:`TestGraphitiHorizonQuery`

* AC: computes set difference (SQLite UUIDs ∉ Graphiti entity_ids).
  → :class:`TestSetDifferenceDiff`

* AC: replays each missing entity via :func:`write_entity` (NOT
  fire-and-forget); collects per-entity outcomes.
  → :class:`TestBackfillReplay`

* AC: returns :class:`ReconcileReport` with ``total_sqlite``,
  ``total_graphiti``, ``backfilled_count``, ``failed_count``,
  ``failed_entities``.
  → :class:`TestReconcileReportShape`

* AC: backfill failures DO NOT raise — recorded in report and surfaced
  to structured log.
  → :class:`TestBackfillFailureTolerance`

* AC: default horizon = 30 days (configurable via ``forge.yaml``).
  → :class:`TestHorizonConfigurable`
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

import forge.memory.reconciler as reconciler_module
from forge.memory.models import (
    CalibrationAdjustment,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
)
from forge.memory.reconciler import (
    DEFAULT_RECONCILE_HORIZON_DAYS,
    PIPELINE_HISTORY_GROUP,
    FailedEntity,
    PipelineHistoryRepository,
    ReconcileReport,
    reconcile_pipeline_history,
)
from forge.memory.writer import PipelineHistoryEntity


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _ts(hour: int = 12, day: int = 26) -> datetime:
    """Deterministic timezone-aware timestamp generator."""
    return datetime(2026, 4, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_gate(entity_id: UUID | None = None) -> GateDecision:
    return GateDecision(
        entity_id=entity_id or uuid4(),
        stage_name="planning",
        decided_at=_ts(),
        score=0.9,
        criterion_breakdown={"completeness": 0.95},
        rationale="all criteria met",
    )


def _make_override(entity_id: UUID | None = None) -> OverrideEvent:
    return OverrideEvent(
        entity_id=entity_id or uuid4(),
        gate_decision_id=uuid4(),
        original_recommendation="block",
        operator_decision="proceed",
        operator_rationale="manual override",
        decided_at=_ts(),
    )


def _make_session(entity_id: UUID | None = None) -> SessionOutcome:
    return SessionOutcome(
        entity_id=entity_id or uuid4(),
        build_id="build-FEAT-FORGE-006-20260426120000",
        outcome="success",
        gate_decision_ids=[],
        closed_at=_ts(),
    )


def _make_adjustment(entity_id: UUID | None = None) -> CalibrationAdjustment:
    return CalibrationAdjustment(
        entity_id=entity_id or uuid4(),
        parameter="confidence_threshold",
        old_value="0.7",
        new_value="0.75",
        approved=True,
        proposed_at=_ts(day=24),
        expires_at=_ts(day=28),
    )


@dataclass
class _RecordingRepo:
    """In-memory stand-in for the FEAT-FORGE-001 SQLite repository.

    Records every ``since`` it is asked about so tests can assert on the
    horizon contract without touching real SQLite.
    """

    entities: list[PipelineHistoryEntity]
    calls: list[datetime] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def list_entities_since(
        self, since: datetime
    ) -> Iterable[PipelineHistoryEntity]:
        self.calls.append(since)
        return list(self.entities)


class _RaisingRepo:
    """Repository whose read raises — used to verify graceful degradation."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def list_entities_since(
        self, since: datetime
    ) -> Iterable[PipelineHistoryEntity]:
        self.calls += 1
        raise self._exc


class _RecordingWriter:
    """Recorder for the per-entity write call.

    Exposes the same coroutine signature as
    :func:`forge.memory.writer.write_entity` so it drops into
    ``write_fn=`` without adapter glue.
    """

    def __init__(
        self,
        *,
        fail_for: set[str] | None = None,
        fail_with: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[PipelineHistoryEntity, str]] = []
        self._fail_for = fail_for or set()
        self._fail_with = fail_with or RuntimeError("simulated backfill failure")

    async def __call__(
        self, entity: PipelineHistoryEntity, group_id: str
    ) -> None:
        self.calls.append((entity, group_id))
        ent_id = str(getattr(entity, "entity_id", ""))
        if ent_id in self._fail_for:
            raise self._fail_with


def _ids_query(returns: set[str]) -> Any:
    """Build a Graphiti id query that returns the given set."""

    async def _fn(*, group_id: str, since: datetime) -> set[str]:
        return set(returns)

    return _fn


# ---------------------------------------------------------------------------
# AC: reads SQLite via the FEAT-FORGE-001 PipelineHistoryRepository interface
# ---------------------------------------------------------------------------


class TestRepositoryProtocolBoundary:
    """``reconcile_pipeline_history`` must speak only to the repository Protocol.

    Risk-3 mitigation from review TASK-REV-IC8B: this module must
    have zero SQLite schema knowledge. The structural-typing test
    confirms a duck-typed object satisfying the Protocol is accepted,
    and reading the source confirms no ``sqlite3``/SQL strings appear.
    """

    @pytest.mark.asyncio
    async def test_accepts_object_satisfying_protocol(self) -> None:
        repo = _RecordingRepo(entities=[_make_gate()])
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=_RecordingWriter(),
            query_fn=_ids_query(set()),
        )
        assert isinstance(report, ReconcileReport)

    @pytest.mark.asyncio
    async def test_protocol_isinstance_check_passes_for_recording_repo(
        self,
    ) -> None:
        """The ``@runtime_checkable`` Protocol accepts our duck-typed repo."""
        repo = _RecordingRepo(entities=[])
        assert isinstance(repo, PipelineHistoryRepository)

    def test_module_does_not_reference_sqlite_internals(self) -> None:
        """Source-level guard — Risk 3: no schema or SQL coupling allowed."""
        import inspect

        source = inspect.getsource(reconciler_module)
        forbidden = ("sqlite3", "CREATE TABLE", "SELECT ", "stage_log")
        for needle in forbidden:
            assert needle not in source, (
                f"reconciler.py must not reference {needle!r} — "
                "Risk 3 mitigation requires going through the repository Protocol."
            )

    @pytest.mark.asyncio
    async def test_none_repository_raises_value_error(self) -> None:
        """A ``None`` repository is a programmer error, not a transient one."""
        with pytest.raises(ValueError):
            await reconcile_pipeline_history(
                None,  # type: ignore[arg-type]
                now=_ts(),
            )

    @pytest.mark.asyncio
    async def test_repository_read_failure_is_logged_and_returns_empty_report(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A repo read failure degrades gracefully — log + empty report."""
        repo = _RaisingRepo(IOError("disk read failed"))
        with caplog.at_level(logging.ERROR, logger="forge.memory.reconciler"):
            report = await reconcile_pipeline_history(
                repo,
                now=_ts(),
                write_fn=_RecordingWriter(),
                query_fn=_ids_query(set()),
            )
        assert report == ReconcileReport()
        assert any(
            "reconcile_sqlite_read_failed" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# AC: queries Graphiti for entity_ids within the horizon
# ---------------------------------------------------------------------------


class TestGraphitiHorizonQuery:
    """The Graphiti query must use ``forge_pipeline_history`` and the horizon."""

    @pytest.mark.asyncio
    async def test_query_targets_pipeline_history_group(self) -> None:
        recorded: list[dict[str, Any]] = []

        async def query(*, group_id: str, since: datetime) -> set[str]:
            recorded.append({"group_id": group_id, "since": since})
            return set()

        await reconcile_pipeline_history(
            _RecordingRepo(entities=[]),
            now=_ts(),
            write_fn=_RecordingWriter(),
            query_fn=query,
        )
        assert len(recorded) == 1
        assert recorded[0]["group_id"] == PIPELINE_HISTORY_GROUP

    @pytest.mark.asyncio
    async def test_query_passes_horizon_lower_bound(self) -> None:
        recorded: list[datetime] = []

        async def query(*, group_id: str, since: datetime) -> set[str]:
            recorded.append(since)
            return set()

        now = _ts()
        await reconcile_pipeline_history(
            _RecordingRepo(entities=[]),
            now=now,
            horizon_days=14,
            write_fn=_RecordingWriter(),
            query_fn=query,
        )
        assert recorded == [now - timedelta(days=14)]

    @pytest.mark.asyncio
    async def test_query_failure_degrades_to_empty_set_and_writes_everything(
        self,
    ) -> None:
        """A backend outage must NOT abort reconcile — re-write everything."""
        entities = [_make_gate(), _make_override()]
        repo = _RecordingRepo(entities=entities)
        writer = _RecordingWriter()

        async def failing_query(
            *, group_id: str, since: datetime
        ) -> set[str]:
            raise RuntimeError("graphiti backend exploded")

        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=failing_query,
        )
        # Every SQLite row treated as missing — writer is idempotent
        # on entity_id per ASSUM-007 so the re-write is safe.
        assert len(writer.calls) == len(entities)
        assert report.backfilled_count == len(entities)

    @pytest.mark.asyncio
    async def test_query_can_return_iterable_other_than_set(self) -> None:
        """A list/tuple from ``query_fn`` is coerced to a set internally."""
        gate = _make_gate()
        existing_id = str(gate.entity_id)

        async def list_query(*, group_id: str, since: datetime) -> Any:
            return [existing_id]  # type: ignore[return-value]

        repo = _RecordingRepo(entities=[gate])
        writer = _RecordingWriter()
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=list_query,
        )
        assert writer.calls == []
        assert report.backfilled_count == 0
        assert report.total_graphiti == 1


# ---------------------------------------------------------------------------
# AC: set difference (SQLite UUIDs ∉ Graphiti entity_ids)
# ---------------------------------------------------------------------------


class TestSetDifferenceDiff:
    """Only entities missing from Graphiti are sent to the writer."""

    @pytest.mark.asyncio
    async def test_entities_present_in_graphiti_are_skipped(self) -> None:
        kept_in_sqlite = _make_gate()
        already_in_graphiti = _make_override()
        repo = _RecordingRepo(
            entities=[kept_in_sqlite, already_in_graphiti]
        )
        writer = _RecordingWriter()

        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query({str(already_in_graphiti.entity_id)}),
        )
        # Only the missing one was written.
        assert len(writer.calls) == 1
        written_entity, _ = writer.calls[0]
        assert written_entity.entity_id == kept_in_sqlite.entity_id
        assert report.total_sqlite == 2
        assert report.total_graphiti == 1
        assert report.backfilled_count == 1

    @pytest.mark.asyncio
    async def test_full_overlap_writes_nothing(self) -> None:
        a, b = _make_gate(), _make_session()
        repo = _RecordingRepo(entities=[a, b])
        writer = _RecordingWriter()
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query({str(a.entity_id), str(b.entity_id)}),
        )
        assert writer.calls == []
        assert report.backfilled_count == 0
        assert report.failed_count == 0

    @pytest.mark.asyncio
    async def test_disjoint_sqlite_writes_every_entity(self) -> None:
        entities = [_make_gate(), _make_override(), _make_session()]
        repo = _RecordingRepo(entities=entities)
        writer = _RecordingWriter()
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query({"some-other-uuid", "another-one"}),
        )
        assert len(writer.calls) == 3
        assert report.backfilled_count == 3


# ---------------------------------------------------------------------------
# AC: replays missing entities via write_entity (synchronous variant)
# ---------------------------------------------------------------------------


class TestBackfillReplay:
    """Backfill must use the synchronous-failure ``write_entity`` path."""

    @pytest.mark.asyncio
    async def test_each_missing_entity_is_passed_to_writer_with_pipeline_group(
        self,
    ) -> None:
        entities = [_make_gate(), _make_adjustment()]
        repo = _RecordingRepo(entities=entities)
        writer = _RecordingWriter()
        await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query(set()),
        )
        assert len(writer.calls) == 2
        for _, group_id in writer.calls:
            assert group_id == PIPELINE_HISTORY_GROUP

    @pytest.mark.asyncio
    async def test_default_write_fn_is_writer_dot_write_entity(self) -> None:
        """Production callers omit ``write_fn=`` — must fall through to the writer."""
        entity = _make_gate()
        repo = _RecordingRepo(entities=[entity])
        captured: list[tuple[Any, str]] = []

        async def fake_write_entity(ent: Any, group_id: str) -> None:
            captured.append((ent, group_id))

        with patch.object(reconciler_module, "write_entity", fake_write_entity):
            report = await reconcile_pipeline_history(
                repo,
                now=_ts(),
                query_fn=_ids_query(set()),
            )
        assert len(captured) == 1
        assert captured[0][0] is entity
        assert captured[0][1] == PIPELINE_HISTORY_GROUP
        assert report.backfilled_count == 1

    @pytest.mark.asyncio
    async def test_replay_is_synchronous_not_fire_and_forget(self) -> None:
        """A failure from the writer must surface in the report — not be lost.

        If reconcile used ``fire_and_forget_write`` we would see
        ``backfilled_count == N`` even when every write raised, because
        fire-and-forget swallows. Instead we expect failures to be
        attributed.
        """
        entity = _make_gate()
        repo = _RecordingRepo(entities=[entity])
        writer = _RecordingWriter(
            fail_for={str(entity.entity_id)},
            fail_with=RuntimeError("graphiti write failed"),
        )
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query(set()),
        )
        assert report.backfilled_count == 0
        assert report.failed_count == 1
        assert report.failed_entities[0].error_class == "RuntimeError"


# ---------------------------------------------------------------------------
# AC: ReconcileReport shape
# ---------------------------------------------------------------------------


class TestReconcileReportShape:
    """Report carries the five required fields."""

    @pytest.mark.asyncio
    async def test_empty_inputs_produce_zeroed_report(self) -> None:
        report = await reconcile_pipeline_history(
            _RecordingRepo(entities=[]),
            now=_ts(),
            write_fn=_RecordingWriter(),
            query_fn=_ids_query(set()),
        )
        assert report.total_sqlite == 0
        assert report.total_graphiti == 0
        assert report.backfilled_count == 0
        assert report.failed_count == 0
        assert report.failed_entities == []

    @pytest.mark.asyncio
    async def test_report_counts_match_sqlite_and_graphiti_sizes(
        self,
    ) -> None:
        in_both = _make_gate()
        only_sqlite_a = _make_override()
        only_sqlite_b = _make_session()
        repo = _RecordingRepo(
            entities=[in_both, only_sqlite_a, only_sqlite_b]
        )
        graphiti_ids = {str(in_both.entity_id), "ghost-uuid-not-in-sqlite"}
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=_RecordingWriter(),
            query_fn=_ids_query(graphiti_ids),
        )
        assert report.total_sqlite == 3
        assert report.total_graphiti == 2
        assert report.backfilled_count == 2
        assert report.failed_count == 0

    def test_failed_entity_dataclass_fields(self) -> None:
        fe = FailedEntity(
            entity_id="abc-123",
            entity_type="GateDecision",
            error_class="RuntimeError",
            error_message="boom",
        )
        assert fe.entity_id == "abc-123"
        assert fe.entity_type == "GateDecision"
        assert fe.error_class == "RuntimeError"
        assert fe.error_message == "boom"


# ---------------------------------------------------------------------------
# AC: backfill failures DO NOT raise; they're recorded + logged
# ---------------------------------------------------------------------------


class TestBackfillFailureTolerance:
    """Per-entity failures stay inside the report — never propagate."""

    @pytest.mark.asyncio
    async def test_single_failure_is_recorded_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        good = _make_gate()
        bad = _make_override()
        repo = _RecordingRepo(entities=[good, bad])
        writer = _RecordingWriter(
            fail_for={str(bad.entity_id)},
            fail_with=ConnectionError("graphiti unreachable"),
        )

        with caplog.at_level(logging.ERROR, logger="forge.memory.reconciler"):
            report = await reconcile_pipeline_history(
                repo,
                now=_ts(),
                write_fn=writer,
                query_fn=_ids_query(set()),
            )

        assert report.backfilled_count == 1
        assert report.failed_count == 1
        assert len(report.failed_entities) == 1
        failed = report.failed_entities[0]
        assert failed.entity_id == str(bad.entity_id)
        assert failed.entity_type == "OverrideEvent"
        assert failed.error_class == "ConnectionError"
        assert "graphiti unreachable" in failed.error_message

    @pytest.mark.asyncio
    async def test_failure_emits_structured_log_with_required_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = _make_session()
        repo = _RecordingRepo(entities=[bad])
        writer = _RecordingWriter(
            fail_for={str(bad.entity_id)},
            fail_with=RuntimeError("kaboom"),
        )
        with caplog.at_level(logging.ERROR, logger="forge.memory.reconciler"):
            await reconcile_pipeline_history(
                repo,
                now=_ts(),
                write_fn=writer,
                query_fn=_ids_query(set()),
            )

        matching = [
            r for r in caplog.records
            if r.message == "reconcile_backfill_failed"
        ]
        assert len(matching) == 1
        record = matching[0]
        # The structured fields are attached via ``extra=`` so they
        # become attributes on the LogRecord.
        assert getattr(record, "entity_id") == str(bad.entity_id)
        assert getattr(record, "entity_type") == "SessionOutcome"
        assert getattr(record, "group_id") == PIPELINE_HISTORY_GROUP
        assert getattr(record, "error_class") == "RuntimeError"
        assert "kaboom" in getattr(record, "error_message")

    @pytest.mark.asyncio
    async def test_multiple_failures_do_not_short_circuit(self) -> None:
        e1, e2, e3 = _make_gate(), _make_override(), _make_session()
        repo = _RecordingRepo(entities=[e1, e2, e3])
        writer = _RecordingWriter(
            fail_for={str(e1.entity_id), str(e3.entity_id)},
            fail_with=RuntimeError("transient"),
        )
        report = await reconcile_pipeline_history(
            repo,
            now=_ts(),
            write_fn=writer,
            query_fn=_ids_query(set()),
        )
        # All three were attempted; 2 failed, 1 succeeded.
        assert len(writer.calls) == 3
        assert report.failed_count == 2
        assert report.backfilled_count == 1
        failed_ids = {fe.entity_id for fe in report.failed_entities}
        assert failed_ids == {str(e1.entity_id), str(e3.entity_id)}


# ---------------------------------------------------------------------------
# AC: default horizon = 30 days, configurable
# ---------------------------------------------------------------------------


class TestHorizonConfigurable:
    """Default horizon = 30 days; explicit override is respected."""

    def test_default_constant_is_thirty_days(self) -> None:
        assert DEFAULT_RECONCILE_HORIZON_DAYS == 30

    @pytest.mark.asyncio
    async def test_default_horizon_passed_to_repo_and_query(self) -> None:
        repo = _RecordingRepo(entities=[])
        recorded: list[datetime] = []

        async def query(*, group_id: str, since: datetime) -> set[str]:
            recorded.append(since)
            return set()

        now = _ts()
        await reconcile_pipeline_history(
            repo,
            now=now,
            write_fn=_RecordingWriter(),
            query_fn=query,
        )
        expected = now - timedelta(days=DEFAULT_RECONCILE_HORIZON_DAYS)
        assert repo.calls == [expected]
        assert recorded == [expected]

    @pytest.mark.asyncio
    async def test_explicit_horizon_overrides_default(self) -> None:
        repo = _RecordingRepo(entities=[])
        recorded: list[datetime] = []

        async def query(*, group_id: str, since: datetime) -> set[str]:
            recorded.append(since)
            return set()

        now = _ts()
        await reconcile_pipeline_history(
            repo,
            now=now,
            horizon_days=7,
            write_fn=_RecordingWriter(),
            query_fn=query,
        )
        expected = now - timedelta(days=7)
        assert repo.calls == [expected]
        assert recorded == [expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", [0, -1, -30])
    async def test_non_positive_horizon_raises_value_error(
        self, bad_value: int
    ) -> None:
        with pytest.raises(ValueError):
            await reconcile_pipeline_history(
                _RecordingRepo(entities=[]),
                horizon_days=bad_value,
                now=_ts(),
            )

    @pytest.mark.asyncio
    async def test_non_int_horizon_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            await reconcile_pipeline_history(
                _RecordingRepo(entities=[]),
                horizon_days="30",  # type: ignore[arg-type]
                now=_ts(),
            )


# ---------------------------------------------------------------------------
# Production-default integration: when callers omit query_fn the module
# falls through to the 3-tier dispatcher — verify the no-backend path
# returns an empty set without raising.
# ---------------------------------------------------------------------------


class TestDefaultDispatcherFallthrough:
    """Production callers omit ``query_fn=`` — the 3-tier dispatcher kicks in."""

    @pytest.mark.asyncio
    async def test_no_backend_available_returns_empty_set(self) -> None:
        with patch.object(
            reconciler_module, "_mcp_backend_available", return_value=False
        ), patch.object(
            reconciler_module, "_cli_backend_available", return_value=False
        ):
            ids = await reconciler_module._dispatch_id_query(
                group_id=PIPELINE_HISTORY_GROUP, since=_ts()
            )
        assert ids == set()

    @pytest.mark.asyncio
    async def test_dispatcher_invoked_when_query_fn_omitted(self) -> None:
        """Pruning the override means we hit the production code path."""
        entity = _make_gate()
        repo = _RecordingRepo(entities=[entity])
        writer = _RecordingWriter()
        recorded: list[dict[str, Any]] = []

        async def fake_dispatch(*, group_id: str, since: datetime) -> set[str]:
            recorded.append({"group_id": group_id, "since": since})
            return set()

        with patch.object(
            reconciler_module, "_dispatch_id_query", fake_dispatch
        ):
            report = await reconcile_pipeline_history(
                repo,
                now=_ts(),
                write_fn=writer,
            )
        assert len(recorded) == 1
        assert recorded[0]["group_id"] == PIPELINE_HISTORY_GROUP
        assert report.backfilled_count == 1


# ---------------------------------------------------------------------------
# Helper coverage: _extract_entity_ids handles the three Graphiti shapes.
# ---------------------------------------------------------------------------


class TestExtractEntityIds:
    """The id-extraction helper must be shape-tolerant.

    Graphiti backends return any of: a flat list of dicts, a list of
    envelopes wrapping ``episode_body`` (str or dict), or a top-level
    dict with ``"results"``/``"nodes"``. We assert all three.
    """

    def test_flat_list_of_dicts(self) -> None:
        raw = [{"entity_id": "a"}, {"entity_id": "b"}]
        assert reconciler_module._extract_entity_ids(raw) == {"a", "b"}

    def test_envelopes_with_string_episode_body(self) -> None:
        raw = [
            {"episode_body": '{"entity_id": "a"}'},
            {"episode_body": '{"entity_id": "b"}'},
        ]
        assert reconciler_module._extract_entity_ids(raw) == {"a", "b"}

    def test_envelopes_with_dict_episode_body(self) -> None:
        raw = [
            {"episode_body": {"entity_id": "a"}},
            {"episode_body": {"entity_id": "b"}},
        ]
        assert reconciler_module._extract_entity_ids(raw) == {"a", "b"}

    def test_results_wrapper(self) -> None:
        raw = {"results": [{"entity_id": "a"}, {"entity_id": "b"}]}
        assert reconciler_module._extract_entity_ids(raw) == {"a", "b"}

    def test_nodes_wrapper(self) -> None:
        raw = {"nodes": [{"entity_id": "x"}]}
        assert reconciler_module._extract_entity_ids(raw) == {"x"}

    def test_unparseable_episode_body_is_skipped(self) -> None:
        raw = [
            {"episode_body": "not json{"},
            {"entity_id": "ok"},
        ]
        assert reconciler_module._extract_entity_ids(raw) == {"ok"}

    def test_missing_entity_id_is_skipped(self) -> None:
        raw = [{"some_other_field": 1}, {"entity_id": "kept"}]
        assert reconciler_module._extract_entity_ids(raw) == {"kept"}

    def test_non_list_returns_empty_set(self) -> None:
        assert reconciler_module._extract_entity_ids(None) == set()
        assert reconciler_module._extract_entity_ids("not a list") == set()
