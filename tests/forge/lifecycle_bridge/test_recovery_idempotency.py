"""Recovery idempotency tests (TASK-FRR-PEB-009 AC-2 / AC-5).

This file is the dedicated harness for the *idempotency* properties of
the lifecycle-bridge restart-recovery flow. The broader recovery
contract is exercised in
:mod:`tests.forge.lifecycle_bridge.test_recovery`; this module focuses
specifically on:

* AC-2: ``published_lifecycles`` set guards re-publication of any
  subject already on the wire pre-restart.
* AC-5: the regression scenario "build-started is not re-published"
  after a daemon restart that crashed *after* ``build-started`` was
  emitted but *before* a terminal envelope arrived.
* The migration column (``published_lifecycles TEXT NOT NULL DEFAULT
  '[]'``) — fresh installs and legacy installs converge on the same
  schema after :func:`forge.persistence.migrations.lifecycle_bridge_published_lifecycles.apply`.
* The :meth:`forge.persistence.repositories.bridge_registry.BridgeRegistry.mark_published`
  write path appends idempotently and atomically.

The tests use deterministic in-memory fakes for the SSE source,
``runs.get`` client, and publisher so the recovery flow can be
exercised without a langgraph-runner sidecar. Payloads are built by
the same factory helpers as ``test_recovery.py``; the import is
intentional duplication so this file remains independently runnable
(``pytest tests/forge/lifecycle_bridge/test_recovery_idempotency.py``)
without depending on test-internal helpers from the sibling module.
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
from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge.recovery import (
    LastEventIdRejected,
    RecoveryRunner,
    subject_for_payload_type,
)
from forge.persistence.migrations import (
    lifecycle_bridge_published_lifecycles as published_lifecycles_migration,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
    BridgeRegistryNotFoundError,
)
from nats_core.events import (
    BuildCompletePayload,
    BuildStartedPayload,
    StageCompletePayload,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedStream:
    parts: list[Any]
    raise_rejection: bool = False


class FakeRecoverySource:
    """Test :class:`RecoverySource` that records calls and yields fixtures."""

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
        await asyncio.sleep(0)
        yield item


class FakeRunsClient:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.get = AsyncMock(side_effect=self._lookup)

    async def _lookup(self, thread_id: str, run_id: str) -> dict[str, Any]:
        return self._responses.get(thread_id, {"status": "running"})


class FakePublisher:
    """Recording publisher whose calls list mirrors the wire emission order."""

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
    """Identity translator: passes the payload sentinel straight through."""

    def translate(self, stream_part: Any, context: BuildContext) -> Any:
        if isinstance(stream_part, _PayloadEnvelope):
            return stream_part.payload
        return None


@dataclass(frozen=True)
class _PayloadEnvelope:
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


def _make_entry(
    *,
    feature_id: str,
    correlation_id: str = "corr-idem",
    last_event_id: str | None = "evt-99",
    published_lifecycles: frozenset[str] = frozenset(),
    current_lifecycle: str = "running",
) -> BridgeRegistryEntry:
    now = datetime.now(UTC)
    return BridgeRegistryEntry(
        feature_id=feature_id,
        thread_id=f"thread-{feature_id}",
        run_id=f"run-{feature_id}",
        correlation_id=correlation_id,
        ack_handle_token=f"ack-{feature_id}",
        deadline_at=now + timedelta(seconds=300),
        attached_at=now,
        current_lifecycle=current_lifecycle,
        updated_at=now,
        last_event_id=last_event_id,
        published_lifecycles=published_lifecycles,
    )


def _seed(registry: BridgeRegistry, entry: BridgeRegistryEntry) -> None:
    registry.record(entry, correlation_id=entry.correlation_id)


def _payload_started(feature_id: str) -> BuildStartedPayload:
    payload = BuildStartedPayload(
        feature_id=feature_id, build_id=f"build-{feature_id}", wave_total=1
    )
    object.__setattr__(payload, "correlation_id", "corr-idem")
    return payload


def _payload_stage(feature_id: str) -> StageCompletePayload:
    return StageCompletePayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}",
        stage_label="idempotency-stage",
        target_kind="local_tool",
        target_identifier="idem-tool",
        status="PASSED",
        gate_mode="AUTO_APPROVE",
        coach_score=0.95,
        duration_secs=0.05,
        completed_at=datetime.now(UTC).isoformat(),
        correlation_id="corr-idem",
    )


def _payload_complete(feature_id: str) -> BuildCompletePayload:
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
        summary="idempotency test complete",
    )
    object.__setattr__(payload, "correlation_id", "corr-idem")
    return payload


def _build_runner(
    registry: BridgeRegistry,
    *,
    scripts: dict[str, list[_ScriptedStream]] | None = None,
    publisher: FakePublisher | None = None,
    runs_responses: dict[str, dict[str, Any]] | None = None,
) -> tuple[RecoveryRunner, FakePublisher, FakeRecoverySource, FakeRunsClient]:
    publisher = publisher or FakePublisher()
    source = FakeRecoverySource(scripts or {})
    runs_client = FakeRunsClient(runs_responses or {})
    runner = RecoveryRunner(
        registry=registry,
        recovery_source=source,
        runs_client=runs_client,  # type: ignore[arg-type]
        translator=FakeTranslator(),  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
    )
    return runner, publisher, source, runs_client


# ---------------------------------------------------------------------------
# AC-2: published_lifecycles set guards republication during replay
# ---------------------------------------------------------------------------


class TestPublishedLifecyclesGuardsReplay:
    """``published_lifecycles`` skips already-emitted subjects on replay."""

    @pytest.mark.asyncio
    async def test_replayed_build_started_is_skipped_when_in_set(
        self, registry: BridgeRegistry
    ) -> None:
        # Pre-restart state: build-started was published, terminal not.
        entry = _make_entry(
            feature_id="FEAT-IDEM-REPLAY",
            published_lifecycles=frozenset({"build-started"}),
        )
        _seed(registry, entry)

        scripts = {
            "FEAT-IDEM-REPLAY": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_payload_started("FEAT-IDEM-REPLAY")),
                        _PayloadEnvelope(_payload_stage("FEAT-IDEM-REPLAY")),
                    ]
                ),
            ],
        }
        runner, publisher, _src, _runs = _build_runner(
            registry, scripts=scripts
        )

        results = await runner.run(correlation_id="corr-idem-replay")

        published = [subject for subject, _ in publisher.calls]
        # AC-2: build-started skipped (already on the wire pre-restart).
        assert "build-started" not in published
        # NEW subjects publish normally.
        assert "stage-complete" in published
        # The skip is reflected in the result metadata.
        assert results[0].skipped_subjects == ("build-started",)

    @pytest.mark.asyncio
    async def test_subsequent_events_still_publish_after_skip(
        self, registry: BridgeRegistry
    ) -> None:
        # Build started AND stage-complete already published; only
        # the terminal is new.
        entry = _make_entry(
            feature_id="FEAT-IDEM-CONT",
            published_lifecycles=frozenset(
                {"build-started", "stage-complete"}
            ),
        )
        _seed(registry, entry)

        scripts = {
            "FEAT-IDEM-CONT": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_payload_started("FEAT-IDEM-CONT")),
                        _PayloadEnvelope(_payload_stage("FEAT-IDEM-CONT")),
                        _PayloadEnvelope(_payload_complete("FEAT-IDEM-CONT")),
                    ]
                ),
            ],
        }
        runner, publisher, _src, _runs = _build_runner(
            registry, scripts=scripts
        )

        await runner.run(correlation_id="corr-idem-cont")

        published = [subject for subject, _ in publisher.calls]
        assert "build-started" not in published
        assert "stage-complete" not in published
        # Only the genuinely-new terminal envelope was published.
        assert published == ["build-complete"]
        # Registry row deleted because terminal was emitted.
        assert (
            registry.get("FEAT-IDEM-CONT", correlation_id="corr-idem-cont")
            is None
        )

    @pytest.mark.asyncio
    async def test_no_publish_when_full_set_already_emitted(
        self, registry: BridgeRegistry
    ) -> None:
        # All envelopes already published pre-restart — replay produces
        # zero outbound emissions.
        entry = _make_entry(
            feature_id="FEAT-IDEM-FULL",
            published_lifecycles=frozenset(
                {"build-started", "stage-complete", "build-complete"}
            ),
        )
        _seed(registry, entry)

        scripts = {
            "FEAT-IDEM-FULL": [
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_payload_started("FEAT-IDEM-FULL")),
                        _PayloadEnvelope(_payload_stage("FEAT-IDEM-FULL")),
                        _PayloadEnvelope(_payload_complete("FEAT-IDEM-FULL")),
                    ]
                ),
            ],
        }
        runner, publisher, _src, _runs = _build_runner(
            registry, scripts=scripts
        )

        await runner.run(correlation_id="corr-idem-full")

        assert publisher.calls == []
        # Terminal already published pre-restart — registry row should
        # be cleaned up by the recovery flow (defensive cleanup).
        assert (
            registry.get("FEAT-IDEM-FULL", correlation_id="corr-idem-full")
            is None
        )


# ---------------------------------------------------------------------------
# AC-5: build-started regression scenario
# ---------------------------------------------------------------------------


class TestBuildStartedRegressionScenario:
    """The named regression: "build-started is not re-published"."""

    @pytest.mark.asyncio
    async def test_replay_after_build_started_only_emits_remaining_events(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(
            feature_id="FEAT-AC5-REGRESSION",
            published_lifecycles=frozenset({"build-started"}),
        )
        _seed(registry, entry)

        scripts = {
            "FEAT-AC5-REGRESSION": [
                _ScriptedStream(
                    parts=[
                        # SSE buffer replays build-started …
                        _PayloadEnvelope(_payload_started("FEAT-AC5-REGRESSION")),
                        # … then a stage-complete that's NEW.
                        _PayloadEnvelope(_payload_stage("FEAT-AC5-REGRESSION")),
                        # … then the terminal.
                        _PayloadEnvelope(_payload_complete("FEAT-AC5-REGRESSION")),
                    ]
                ),
            ],
        }
        runner, publisher, source, _runs = _build_runner(
            registry, scripts=scripts
        )

        await runner.run(correlation_id="corr-ac5-regression")

        published = [subject for subject, _ in publisher.calls]
        # The regression: build-started must NOT be re-published.
        assert "build-started" not in published, (
            "AC-5 regression: build-started was re-published after a "
            "daemon restart that already emitted it pre-restart"
        )
        # The build's NEW envelopes are still emitted.
        assert published == ["stage-complete", "build-complete"]
        # The replay used the persisted Last-Event-ID (AC-1).
        assert source.calls[0]["last_event_id"] == "evt-99"
        # Terminal arrival deleted the registry row.
        assert (
            registry.get(
                "FEAT-AC5-REGRESSION",
                correlation_id="corr-ac5-regression",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_sweep_path_also_skips_already_published_terminal(
        self, registry: BridgeRegistry
    ) -> None:
        # Edge case: the terminal was published pre-restart, but
        # detach() raced and the registry row stayed in place. The
        # buffer expired, so the sweep path runs. The fresh stream
        # replays the same terminal — which must be skipped.
        entry = _make_entry(
            feature_id="FEAT-IDEM-SWEEP",
            published_lifecycles=frozenset(
                {"build-started", "stage-complete", "build-complete"}
            ),
        )
        _seed(registry, entry)

        scripts = {
            "FEAT-IDEM-SWEEP": [
                # First call: replay rejected (buffer expired).
                _ScriptedStream(parts=[], raise_rejection=True),
                # Second call: fresh-stream replays the terminal.
                _ScriptedStream(
                    parts=[
                        _PayloadEnvelope(_payload_complete("FEAT-IDEM-SWEEP")),
                    ]
                ),
            ],
        }
        runner, publisher, _src, runs_client = _build_runner(
            registry,
            scripts=scripts,
            runs_responses={
                "thread-FEAT-IDEM-SWEEP": {"status": "success"}
            },
        )

        results = await runner.run(correlation_id="corr-idem-sweep")

        # Terminal was NOT re-published.
        assert publisher.calls == []
        # Registry row was cleaned up defensively.
        assert (
            registry.get(
                "FEAT-IDEM-SWEEP", correlation_id="corr-idem-sweep"
            )
            is None
        )
        # runs.get was still invoked exactly once.
        runs_client.get.assert_awaited_once()
        # The skip is reflected in the result.
        assert "build-complete" in results[0].skipped_subjects


# ---------------------------------------------------------------------------
# Migration column tests (TASK-FRR-PEB-009 AC-2 storage backbone)
# ---------------------------------------------------------------------------


class TestPublishedLifecyclesMigration:
    """The dedicated migration module is idempotent and detects column state."""

    def test_apply_on_legacy_table_adds_column(
        self, tmp_path: Path
    ) -> None:
        # Create a *legacy* registry table without the column so we can
        # exercise the ALTER TABLE path explicitly.
        db_path = tmp_path / "legacy.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(cx)
            cx.execute(
                f"DROP TABLE IF EXISTS {bridge_migration.TABLE_NAME}"
            )
            cx.execute(
                f"""
                CREATE TABLE {bridge_migration.TABLE_NAME} (
                    feature_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    last_event_id TEXT,
                    ack_handle_token TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    attached_at TEXT NOT NULL,
                    current_lifecycle TEXT NOT NULL CHECK (
                        current_lifecycle IN ('queued', 'running', 'paused')
                    ),
                    updated_at TEXT NOT NULL
                ) STRICT
                """
            )
            cx.commit()

            assert published_lifecycles_migration.column_exists(cx) is False
            applied = published_lifecycles_migration.apply(cx)
            assert applied is True
            assert published_lifecycles_migration.column_exists(cx) is True
        finally:
            cx.close()

    def test_apply_is_noop_when_column_exists(
        self, writer_db: sqlite3.Connection
    ) -> None:
        # The fixture's apply_at_boot + bridge_migration.apply() already
        # ensured the column exists. A second invocation is a no-op.
        assert published_lifecycles_migration.column_exists(writer_db) is True
        applied = published_lifecycles_migration.apply(writer_db)
        assert applied is False

    def test_column_name_constant_matches_canonical(self) -> None:
        # Cross-module sanity: the dedicated migration's ``COLUMN_NAME``
        # exposes the same canonical constant as the registry migration.
        assert (
            published_lifecycles_migration.COLUMN_NAME
            == bridge_migration.PUBLISHED_LIFECYCLES_COLUMN
        )

    def test_apply_rejects_non_connection(self) -> None:
        with pytest.raises(TypeError):
            published_lifecycles_migration.apply("not-a-connection")  # type: ignore[arg-type]

    def test_column_exists_rejects_non_connection(self) -> None:
        with pytest.raises(TypeError):
            published_lifecycles_migration.column_exists(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BridgeRegistry.mark_published — write-path idempotency
# ---------------------------------------------------------------------------


class TestMarkPublishedAtomicAppend:
    """``mark_published`` is the single canonical write site for the column."""

    def test_appends_subject_idempotently(
        self, registry: BridgeRegistry
    ) -> None:
        _seed(registry, _make_entry(feature_id="FEAT-MP-IDEM-1"))

        first = registry.mark_published(
            "FEAT-MP-IDEM-1",
            "build-started",
            correlation_id="corr-mp",
        )
        assert first == frozenset({"build-started"})

        # Re-mark the SAME subject — set is idempotent.
        second = registry.mark_published(
            "FEAT-MP-IDEM-1",
            "build-started",
            correlation_id="corr-mp",
        )
        assert second == frozenset({"build-started"})

        # Add a different subject.
        third = registry.mark_published(
            "FEAT-MP-IDEM-1",
            "stage-complete",
            correlation_id="corr-mp",
        )
        assert third == frozenset({"build-started", "stage-complete"})

        # Persisted value matches the in-memory return.
        loaded = registry.get(
            "FEAT-MP-IDEM-1", correlation_id="corr-mp"
        )
        assert loaded is not None
        assert loaded.published_lifecycles == third

    def test_persists_last_event_id_when_provided(
        self, registry: BridgeRegistry
    ) -> None:
        _seed(registry, _make_entry(feature_id="FEAT-MP-LEI"))

        registry.mark_published(
            "FEAT-MP-LEI",
            "build-started",
            correlation_id="corr-mp-lei",
            last_event_id="evt-100",
        )
        loaded = registry.get(
            "FEAT-MP-LEI", correlation_id="corr-mp-lei"
        )
        assert loaded is not None
        assert loaded.last_event_id == "evt-100"

    def test_preserves_last_event_id_when_omitted(
        self, registry: BridgeRegistry
    ) -> None:
        _seed(
            registry,
            _make_entry(feature_id="FEAT-MP-KEEP", last_event_id="evt-prev"),
        )

        registry.mark_published(
            "FEAT-MP-KEEP",
            "build-started",
            correlation_id="corr-mp-keep",
        )
        loaded = registry.get(
            "FEAT-MP-KEEP", correlation_id="corr-mp-keep"
        )
        assert loaded is not None
        # Existing cursor preserved (COALESCE semantics).
        assert loaded.last_event_id == "evt-prev"

    def test_raises_for_missing_row(
        self, registry: BridgeRegistry
    ) -> None:
        with pytest.raises(BridgeRegistryNotFoundError):
            registry.mark_published(
                "FEAT-DOES-NOT-EXIST",
                "build-started",
                correlation_id="corr-missing",
            )

    def test_rejects_empty_arguments(
        self, registry: BridgeRegistry
    ) -> None:
        _seed(registry, _make_entry(feature_id="FEAT-MP-EMPTY"))

        with pytest.raises(ValueError):
            registry.mark_published("", "build-started", correlation_id="c")
        with pytest.raises(ValueError):
            registry.mark_published(
                "FEAT-MP-EMPTY", "", correlation_id="c"
            )
        with pytest.raises(ValueError):
            registry.mark_published(
                "FEAT-MP-EMPTY", "build-started", correlation_id=""
            )


# ---------------------------------------------------------------------------
# Sanity: subject helper used by recovery is the canonical wire-format
# ---------------------------------------------------------------------------


class TestSubjectHelperContract:
    def test_started_subject_matches_publisher_segment(self) -> None:
        # Cross-check that the subject helper returns the same string
        # the publisher writes to JetStream — the AC-2 idempotency
        # check would silently fail if the two diverged.
        assert (
            subject_for_payload_type(BuildStartedPayload) == "build-started"
        )

    def test_complete_subject_matches_publisher_segment(self) -> None:
        assert (
            subject_for_payload_type(BuildCompletePayload) == "build-complete"
        )
