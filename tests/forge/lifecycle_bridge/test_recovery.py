"""Tests for ``forge.lifecycle_bridge.recovery`` (TASK-FRR-PEB-009).

Acceptance-criteria coverage map:

* AC-1: :meth:`RecoveryRunner.run` iterates active registry entries and
  schedules per-entry asyncio tasks — :class:`TestInBufferReplay`,
  :class:`TestMultiBuildRecovery`.
* AC-2: idempotency — :class:`TestPublishedLifecyclesIdempotency`,
  :class:`TestBuildStartedNotRePublished`.
* AC-3: out-of-buffer sweep falls back to ``runs.get`` —
  :class:`TestOutOfBufferSweep`.
* AC-4: 30s budget — :class:`TestRecoveryBudget`.
* AC-5: build-started not re-published —
  :class:`TestBuildStartedNotRePublished`.
* AC-6: multi-build recovery — :class:`TestMultiBuildRecovery`.

The tests use deterministic in-memory fakes for the SSE source,
``runs.get`` client, translator, and publisher so the recovery flow can
be exercised without a langgraph-runner sidecar.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import BuildContext, LifecycleBridge
from forge.lifecycle_bridge.recovery import (
    DEFAULT_RECOVERY_BUDGET_SECONDS,
    LastEventIdRejected,
    RecoveryRunner,
    subject_for_payload_type,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
)
from nats_core.events import (
    BuildCompletePayload,
    BuildFailedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedStream:
    """One scripted SSE replay session.

    ``parts`` are yielded in order; if ``raise_rejection`` is set the
    iterator raises :class:`LastEventIdRejected` on first iteration so
    the recovery flow switches to the sweep fallback.
    """

    parts: list[Any]
    raise_rejection: bool = False


class FakeRecoverySource:
    """Test :class:`RecoverySource` that records calls and yields fixtures.

    Each call captures the ``last_event_id`` so tests can assert the
    replay path used the persisted cursor and the sweep path used
    ``None`` (i.e. "from now").
    """

    def __init__(self, scripts: dict[str, list[_ScriptedStream]]) -> None:
        self._scripts = scripts
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        feature_id: str,
        thread_id: str,
        run_id: str,
        last_event_id: str | None,
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "feature_id": feature_id,
                "thread_id": thread_id,
                "run_id": run_id,
                "last_event_id": last_event_id,
            }
        )
        scripts = self._scripts.get(feature_id, [])
        if not scripts:
            return _empty_async_iterator()
        script = scripts.pop(0)
        if script.raise_rejection:
            raise LastEventIdRejected(feature_id, last_event_id)
        return _async_iterator(script.parts)


async def _empty_async_iterator() -> AsyncIterator[Any]:
    if False:  # pragma: no cover - generator coercion
        yield None


async def _async_iterator(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        # Yield control so concurrent tasks can interleave (AC-6 test).
        await asyncio.sleep(0)
        yield item


class FakeRunsClient:
    """Test :class:`RunsGetClient` that returns scripted ``runs.get`` responses."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.get = AsyncMock(side_effect=self._lookup)

    async def _lookup(self, thread_id: str, run_id: str) -> dict[str, Any]:
        # Look up by thread_id (tests key on it for clarity).
        return self._responses.get(thread_id, {"status": "running"})


class FakePublisher:
    """Test :class:`PipelinePublisher` that records every publish call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def publish_build_started(self, payload: Any) -> None:
        self.calls.append(("build-started", payload))

    async def publish_stage_complete(self, payload: Any) -> None:
        self.calls.append(("stage-complete", payload))

    async def publish_build_complete(self, payload: Any) -> None:
        self.calls.append(("build-complete", payload))

    async def publish_build_failed(self, payload: Any) -> None:
        self.calls.append(("build-failed", payload))

    async def publish_build_paused(self, payload: Any) -> None:
        self.calls.append(("build-paused", payload))

    async def publish_build_resumed(self, payload: Any) -> None:
        self.calls.append(("build-resumed", payload))

    async def publish_build_cancelled(self, payload: Any) -> None:
        self.calls.append(("build-cancelled", payload))


class FakeTranslator:
    """Translator stand-in that maps StreamPart sentinels to typed payloads.

    Each ``StreamPart``-shaped input is just a ``(payload, ...)`` tuple
    here; the translator returns the first element. Using a fake keeps
    these tests focused on the recovery orchestration rather than on
    transition diff semantics (which are covered by
    ``test_translation.py``).
    """

    def translate(self, stream_part: Any, context: BuildContext) -> Any:
        if isinstance(stream_part, _PayloadEnvelope):
            return stream_part.payload
        return None


@dataclass(frozen=True)
class _PayloadEnvelope:
    """Test sentinel that the FakeTranslator passes straight through."""

    payload: Any


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def bridge(registry: BridgeRegistry) -> LifecycleBridge:
    return LifecycleBridge(registry=registry)


def _make_entry(
    *,
    feature_id: str = "FEAT-REC-001",
    thread_id: str | None = None,
    run_id: str | None = None,
    correlation_id: str = "corr-rec-001",
    last_event_id: str | None = "evt-99",
    published_lifecycles: frozenset[str] = frozenset(),
    current_lifecycle: str = "running",
) -> BridgeRegistryEntry:
    now = datetime.now(UTC)
    return BridgeRegistryEntry(
        feature_id=feature_id,
        thread_id=thread_id or f"thread-{feature_id}",
        run_id=run_id or f"run-{feature_id}",
        correlation_id=correlation_id,
        ack_handle_token=f"ack-{feature_id}",
        deadline_at=now + timedelta(seconds=300),
        attached_at=now,
        current_lifecycle=current_lifecycle,
        updated_at=now,
        last_event_id=last_event_id,
        published_lifecycles=published_lifecycles,
    )


def _make_payload_started(feature_id: str = "FEAT-REC-001") -> BuildStartedPayload:
    payload = BuildStartedPayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}",
        wave_total=1,
    )
    object.__setattr__(payload, "correlation_id", "corr-rec-001")
    return payload


def _make_payload_stage(feature_id: str = "FEAT-REC-001") -> StageCompletePayload:
    payload = StageCompletePayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}",
        stage_label="recovery-stage",
        target_kind="local_tool",
        target_identifier="recovery-tool",
        status="PASSED",
        gate_mode="AUTO_APPROVE",
        coach_score=0.93,
        duration_secs=0.1,
        completed_at=datetime.now(UTC).isoformat(),
        correlation_id="corr-rec-001",
    )
    return payload


def _make_payload_complete(feature_id: str = "FEAT-REC-001") -> BuildCompletePayload:
    payload = BuildCompletePayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}",
        repo="example/repo",
        branch="main",
        tasks_completed=1,
        tasks_failed=0,
        tasks_total=1,
        pr_url=None,
        duration_seconds=1,
        summary="recovery completed",
    )
    object.__setattr__(payload, "correlation_id", "corr-rec-001")
    return payload


def _make_payload_failed(feature_id: str = "FEAT-REC-001") -> BuildFailedPayload:
    payload = BuildFailedPayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}",
        failure_reason="recovered failure",
        recoverable=False,
    )
    object.__setattr__(payload, "correlation_id", "corr-rec-001")
    return payload


def _seed_entry(
    registry: BridgeRegistry,
    entry: BridgeRegistryEntry,
) -> None:
    registry.record(entry, correlation_id=entry.correlation_id)


# ---------------------------------------------------------------------------
# AC-1 / AC-4 — In-buffer replay
# ---------------------------------------------------------------------------


class TestInBufferReplay:
    """ASSUM-001: the SSE buffer replays in-window envelopes."""

    @pytest.mark.asyncio
    async def test_replay_publishes_3_envelopes_and_deletes_row(
        self,
        registry: BridgeRegistry,
    ) -> None:
        # Seed registry with one in-flight build.
        entry = _make_entry(feature_id="FEAT-REPLAY-1", last_event_id="evt-3")
        _seed_entry(registry, entry)

        # Stub SSE source replays 3 in-window events including a terminal.
        scripts = {
            "FEAT-REPLAY-1": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_started("FEAT-REPLAY-1")),
                        _PayloadEnvelope(_make_payload_stage("FEAT-REPLAY-1")),
                        _PayloadEnvelope(_make_payload_complete("FEAT-REPLAY-1")),
                    ]
                ),
            ],
        }
        source = FakeRecoverySource(scripts)
        publisher = FakePublisher()

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=source,
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-recover-1")

        # Exactly 3 envelopes published.
        subjects_published = [subject for subject, _ in publisher.calls]
        assert subjects_published == [
            "build-started",
            "stage-complete",
            "build-complete",
        ]
        # Registry entry deleted.
        assert (
            registry.get("FEAT-REPLAY-1", correlation_id="corr-recover-1") is None
        )
        # SSE was attached with the persisted Last-Event-ID.
        assert source.calls[0]["last_event_id"] == "evt-3"
        # Recovery result reports replay mode + terminal published.
        assert len(results) == 1
        assert results[0].mode == "replay"
        assert results[0].terminal_published is True
        assert results[0].events_published == 3

    @pytest.mark.asyncio
    async def test_replay_with_no_active_entries_is_noop(
        self, registry: BridgeRegistry
    ) -> None:
        runner = RecoveryRunner(
            registry=registry,
            recovery_source=FakeRecoverySource({}),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=FakePublisher(),  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-empty")
        assert results == []


# ---------------------------------------------------------------------------
# AC-3 — Out-of-buffer sweep
# ---------------------------------------------------------------------------


class TestOutOfBufferSweep:
    """ASSUM-002: SSE rejection routes to ``runs.get`` fallback."""

    @pytest.mark.asyncio
    async def test_410_rejection_publishes_terminal_via_fresh_stream(
        self,
        registry: BridgeRegistry,
    ) -> None:
        entry = _make_entry(feature_id="FEAT-SWEEP-1", last_event_id="evt-stale")
        _seed_entry(registry, entry)

        # First call (replay) raises LastEventIdRejected; second call
        # (fresh-stream sweep) yields the terminal payload — single
        # emit-site invariant preserved.
        scripts = {
            "FEAT-SWEEP-1": [
                _ScriptedStream(parts=[], raise_rejection=True),
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_complete("FEAT-SWEEP-1"))
                    ]
                ),
            ],
        }
        source = FakeRecoverySource(scripts)
        publisher = FakePublisher()
        runs_client = FakeRunsClient(
            {entry.thread_id: {"status": "success"}}
        )

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=source,
            runs_client=runs_client,  # type: ignore[arg-type]
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-sweep")

        # runs.get was called exactly once (AC-3: "fall back ... once").
        runs_client.get.assert_awaited_once_with(entry.thread_id, entry.run_id)
        # Exactly one terminal envelope published.
        assert [s for s, _ in publisher.calls] == ["build-complete"]
        # Registry entry deleted.
        assert (
            registry.get("FEAT-SWEEP-1", correlation_id="corr-sweep") is None
        )
        # The fresh stream call used last_event_id=None (AC-3).
        fresh_call = source.calls[1]
        assert fresh_call["last_event_id"] is None
        # Mode reported as sweep-terminal because runs.get said success.
        assert results[0].mode == "sweep-terminal"
        assert results[0].terminal_published is True

    @pytest.mark.asyncio
    async def test_410_rejection_with_running_status_resumes_fresh_stream(
        self,
        registry: BridgeRegistry,
    ) -> None:
        entry = _make_entry(feature_id="FEAT-SWEEP-RUNNING", last_event_id="evt-99")
        _seed_entry(registry, entry)

        # Replay rejected; fresh stream emits a started + stage and
        # then closes without terminal (the run is still going).
        scripts = {
            "FEAT-SWEEP-RUNNING": [
                _ScriptedStream(parts=[], raise_rejection=True),
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(
                            _make_payload_stage("FEAT-SWEEP-RUNNING")
                        ),
                    ]
                ),
            ],
        }
        source = FakeRecoverySource(scripts)
        publisher = FakePublisher()
        runs_client = FakeRunsClient(
            {entry.thread_id: {"status": "running"}}
        )

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=source,
            runs_client=runs_client,  # type: ignore[arg-type]
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-sweep-run")

        # The terminal was not seen — registry row stays in place.
        assert (
            registry.get("FEAT-SWEEP-RUNNING", correlation_id="corr-sweep-run")
            is not None
        )
        assert results[0].mode == "sweep-running"
        assert results[0].terminal_published is False


# ---------------------------------------------------------------------------
# AC-2 / AC-5 — Idempotency
# ---------------------------------------------------------------------------


class TestPublishedLifecyclesIdempotency:
    """``published_lifecycles`` set guards against double-emit."""

    @pytest.mark.asyncio
    async def test_already_published_subject_is_skipped(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(
            feature_id="FEAT-IDEM-1",
            published_lifecycles=frozenset({"build-started"}),
        )
        _seed_entry(registry, entry)

        # SSE replays a build-started (already published pre-restart) +
        # a stage-complete (NEW).
        scripts = {
            "FEAT-IDEM-1": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_started("FEAT-IDEM-1")),
                        _PayloadEnvelope(_make_payload_stage("FEAT-IDEM-1")),
                    ]
                ),
            ],
        }
        publisher = FakePublisher()

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=FakeRecoverySource(scripts),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-idem")

        published = [subject for subject, _ in publisher.calls]
        # AC-2: build-started must NOT be re-published.
        assert "build-started" not in published
        # Subsequent NEW events still publish normally.
        assert "stage-complete" in published
        # Skip metadata reflected in the result.
        assert "build-started" in results[0].skipped_subjects


class TestBuildStartedNotRePublished:
    """AC-5 regression scenario: build-started is the canonical guard.

    "A daemon restart after build-started has been published does not
    re-publish build-started after recovery."
    """

    @pytest.mark.asyncio
    async def test_build_started_skipped_then_terminal_published(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(
            feature_id="FEAT-AC5",
            published_lifecycles=frozenset({"build-started"}),
        )
        _seed_entry(registry, entry)

        scripts = {
            "FEAT-AC5": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_started("FEAT-AC5")),
                        _PayloadEnvelope(_make_payload_complete("FEAT-AC5")),
                    ]
                ),
            ],
        }
        publisher = FakePublisher()

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=FakeRecoverySource(scripts),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        await runner.run(correlation_id="corr-ac5")

        published = [subject for subject, _ in publisher.calls]
        # build-started skipped (regression locked).
        assert "build-started" not in published
        # Terminal envelope still emitted.
        assert "build-complete" in published
        # Registry row deleted on terminal arrival.
        assert registry.get("FEAT-AC5", correlation_id="corr-ac5") is None


# ---------------------------------------------------------------------------
# AC-6 — Multi-build recovery
# ---------------------------------------------------------------------------


class TestMultiBuildRecovery:
    """3 concurrent recoveries run without interference."""

    @pytest.mark.asyncio
    async def test_three_concurrent_recoveries_all_complete(
        self, registry: BridgeRegistry
    ) -> None:
        feature_ids = ["FEAT-M1", "FEAT-M2", "FEAT-M3"]
        for feature_id in feature_ids:
            _seed_entry(registry, _make_entry(feature_id=feature_id))

        scripts: dict[str, list[_ScriptedStream]] = {
            feature_id: [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_started(feature_id)),
                        _PayloadEnvelope(_make_payload_complete(feature_id)),
                    ]
                ),
            ]
            for feature_id in feature_ids
        }
        publisher = FakePublisher()

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=FakeRecoverySource(scripts),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=publisher,  # type: ignore[arg-type]
        )

        results = await runner.run(correlation_id="corr-multi")

        assert len(results) == 3
        # Every entry deleted.
        for feature_id in feature_ids:
            assert (
                registry.get(feature_id, correlation_id="corr-multi") is None
            )
        # Each build saw its own pair of envelopes — 6 publishes total.
        published_subjects_per_feature: dict[str, list[str]] = {
            feature_id: [] for feature_id in feature_ids
        }
        for subject, payload in publisher.calls:
            published_subjects_per_feature[payload.feature_id].append(subject)
        for feature_id in feature_ids:
            assert published_subjects_per_feature[feature_id] == [
                "build-started",
                "build-complete",
            ]
        # All three results report terminal_published.
        assert all(r.terminal_published for r in results)


# ---------------------------------------------------------------------------
# AC-4 — Recovery budget
# ---------------------------------------------------------------------------


class TestRecoveryBudget:
    """``RecoveryRunner.run`` honours the 30s budget (default)."""

    def test_default_budget_is_30_seconds(self) -> None:
        assert DEFAULT_RECOVERY_BUDGET_SECONDS == 30.0

    @pytest.mark.asyncio
    async def test_budget_exceeded_cancels_pending_tasks(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(feature_id="FEAT-SLOW")
        _seed_entry(registry, entry)

        # SSE source that hangs forever — recovery will time out.
        async def _hanging() -> AsyncIterator[Any]:
            await asyncio.sleep(60)
            if False:  # pragma: no cover
                yield None

        class HangingSource:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def __call__(
                self, **kwargs: Any
            ) -> AsyncIterator[Any]:
                self.calls.append(kwargs)
                return _hanging()

        runner = RecoveryRunner(
            registry=registry,
            recovery_source=HangingSource(),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=FakePublisher(),  # type: ignore[arg-type]
            budget_seconds=0.05,
        )

        results = await runner.run(correlation_id="corr-slow")

        assert len(results) == 1
        assert results[0].mode == "failed"
        assert results[0].failure_reason == "budget-exceeded"


# ---------------------------------------------------------------------------
# Pre-startup ordering — recovery completes before consumer attaches
# ---------------------------------------------------------------------------


class TestPreStartupOrdering:
    """``run_recovery_at_boot`` is awaitable and returns deterministically.

    The consumer's first ``fetch`` call must be preceded by recovery
    completing. We assert that property by composing the recovery run
    inside an explicit ordering and checking the recorded call order.
    """

    @pytest.mark.asyncio
    async def test_recovery_completes_before_subsequent_step(
        self, registry: BridgeRegistry
    ) -> None:
        from forge.lifecycle_bridge.recovery import run_recovery_at_boot

        _seed_entry(registry, _make_entry(feature_id="FEAT-ORDER"))
        publisher = FakePublisher()
        order: list[str] = []

        scripts = {
            "FEAT-ORDER": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_make_payload_complete("FEAT-ORDER")),
                    ]
                ),
            ],
        }

        async def _recovery_step() -> None:
            order.append("recovery-start")
            runner = RecoveryRunner(
                registry=registry,
                recovery_source=FakeRecoverySource(scripts),
                runs_client=FakeRunsClient({}),
                translator=FakeTranslator(),  # type: ignore[arg-type]
                publisher=publisher,  # type: ignore[arg-type]
            )
            await run_recovery_at_boot(runner, correlation_id="corr-order")
            order.append("recovery-end")

        async def _consumer_step() -> None:
            order.append("consumer-fetch")

        # Sequential await: recovery MUST end before consumer fetch.
        await _recovery_step()
        await _consumer_step()

        assert order == ["recovery-start", "recovery-end", "consumer-fetch"]
        assert publisher.calls  # build-complete emitted


# ---------------------------------------------------------------------------
# Subject helpers and constructor validation
# ---------------------------------------------------------------------------


class TestSubjectHelpers:
    def test_subject_for_started_payload(self) -> None:
        assert (
            subject_for_payload_type(BuildStartedPayload) == "build-started"
        )

    def test_subject_for_complete_payload(self) -> None:
        assert (
            subject_for_payload_type(BuildCompletePayload) == "build-complete"
        )

    def test_subject_for_failed_payload(self) -> None:
        assert (
            subject_for_payload_type(BuildFailedPayload) == "build-failed"
        )

    def test_subject_for_unknown_type_returns_none(self) -> None:
        class _NotARealPayload:
            pass

        assert subject_for_payload_type(_NotARealPayload) is None


class TestConstructorValidation:
    def test_runner_rejects_non_bridge_registry(self) -> None:
        with pytest.raises(TypeError):
            RecoveryRunner(
                registry="not-a-registry",  # type: ignore[arg-type]
                recovery_source=FakeRecoverySource({}),
                runs_client=FakeRunsClient({}),
                translator=FakeTranslator(),  # type: ignore[arg-type]
                publisher=FakePublisher(),  # type: ignore[arg-type]
            )

    def test_runner_rejects_zero_budget(
        self, registry: BridgeRegistry
    ) -> None:
        with pytest.raises(ValueError):
            RecoveryRunner(
                registry=registry,
                recovery_source=FakeRecoverySource({}),
                runs_client=FakeRunsClient({}),
                translator=FakeTranslator(),  # type: ignore[arg-type]
                publisher=FakePublisher(),  # type: ignore[arg-type]
                budget_seconds=0,
            )

    def test_runner_rejects_missing_recovery_source(
        self, registry: BridgeRegistry
    ) -> None:
        with pytest.raises(ValueError):
            RecoveryRunner(
                registry=registry,
                recovery_source=None,  # type: ignore[arg-type]
                runs_client=FakeRunsClient({}),
                translator=FakeTranslator(),  # type: ignore[arg-type]
                publisher=FakePublisher(),  # type: ignore[arg-type]
            )

    def test_runner_rejects_missing_publisher(
        self, registry: BridgeRegistry
    ) -> None:
        with pytest.raises(ValueError):
            RecoveryRunner(
                registry=registry,
                recovery_source=FakeRecoverySource({}),
                runs_client=FakeRunsClient({}),
                translator=FakeTranslator(),  # type: ignore[arg-type]
                publisher=None,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_run_rejects_empty_correlation_id(
        self, registry: BridgeRegistry
    ) -> None:
        runner = RecoveryRunner(
            registry=registry,
            recovery_source=FakeRecoverySource({}),
            runs_client=FakeRunsClient({}),
            translator=FakeTranslator(),  # type: ignore[arg-type]
            publisher=FakePublisher(),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError):
            await runner.run(correlation_id="")


# ---------------------------------------------------------------------------
# Registry mark_published + migration smoke tests
# ---------------------------------------------------------------------------


class TestRegistryMarkPublished:
    """``BridgeRegistry.mark_published`` appends idempotently to the column."""

    def test_mark_published_appends_subject(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(feature_id="FEAT-MP-1")
        _seed_entry(registry, entry)

        new_set = registry.mark_published(
            "FEAT-MP-1",
            "build-started",
            correlation_id="corr-mp",
        )
        assert new_set == frozenset({"build-started"})

        new_set = registry.mark_published(
            "FEAT-MP-1",
            "stage-complete",
            correlation_id="corr-mp",
        )
        assert new_set == frozenset({"build-started", "stage-complete"})

        # Reading back via get() exposes the persisted set.
        loaded = registry.get("FEAT-MP-1", correlation_id="corr-mp")
        assert loaded is not None
        assert loaded.published_lifecycles == frozenset(
            {"build-started", "stage-complete"}
        )

    def test_mark_published_is_idempotent_for_repeated_subject(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(feature_id="FEAT-MP-2")
        _seed_entry(registry, entry)

        registry.mark_published(
            "FEAT-MP-2", "build-started", correlation_id="corr-mp2"
        )
        result = registry.mark_published(
            "FEAT-MP-2", "build-started", correlation_id="corr-mp2"
        )
        assert result == frozenset({"build-started"})


class TestMigrationIsIdempotent:
    """Re-applying the migration on an already-migrated DB is a no-op."""

    def test_apply_twice_is_safe(self, writer_db: sqlite3.Connection) -> None:
        # The fixture already applied once; apply again.
        bridge_migration.apply(writer_db)
        # Column exists.
        rows = writer_db.execute(
            f"PRAGMA table_info({bridge_migration.TABLE_NAME})"
        ).fetchall()
        names = {row[1] for row in rows}
        assert bridge_migration.PUBLISHED_LIFECYCLES_COLUMN in names
