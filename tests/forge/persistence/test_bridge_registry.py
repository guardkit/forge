"""Tests for ``forge.persistence.repositories.bridge_registry`` (TASK-FRR-PEB-002).

Acceptance-criteria coverage map:

* AC-2: ``lifecycle_bridge_registry`` table creation via the migration —
  :class:`TestMigrationCreatesTable`.
* AC-3: ``BridgeRegistry`` repository exposes ``record``,
  ``update_lifecycle``, ``get``, ``list_active``, ``delete`` —
  :class:`TestBridgeRegistryOperations`.
* AC-4: ``record`` writes a row, ``delete`` removes it, ``list_active``
  returns rows safe for ``forge status --in-flight`` (no SSE
  metadata leaks) — :class:`TestBridgeRegistryListActive`.
* AC-5: every ``BridgeRegistry`` operation accepts ``correlation_id``
  explicitly — :class:`TestCorrelationIdContract`.
* Concurrency: two ``record`` calls for the same ``feature_id``
  serialise correctly — second overwrites first (UPSERT semantics) —
  :class:`TestRecordConcurrency`.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
    BridgeRegistryNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writer connection against a freshly-migrated db file.

    Applies the existing forge schema migrations and then the new
    ``lifecycle_bridge_registry`` migration so tests run against the
    full production substrate.
    """
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
    """Return a ``BridgeRegistry`` bound to the migrated writer connection."""
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def fixed_now() -> datetime:
    return datetime(2026, 5, 7, 9, 0, 0, tzinfo=UTC)


def _make_entry(
    *,
    feature_id: str = "FEAT-PEB-001",
    thread_id: str = "thread-001",
    run_id: str = "run-001",
    correlation_id: str = "corr-001",
    last_event_id: str | None = None,
    ack_handle_token: str = "ack-token-aaa",
    deadline_at: datetime | None = None,
    attached_at: datetime | None = None,
    current_lifecycle: str = "queued",
    updated_at: datetime | None = None,
) -> BridgeRegistryEntry:
    now = datetime(2026, 5, 7, 9, 0, 0, tzinfo=UTC)
    return BridgeRegistryEntry(
        feature_id=feature_id,
        thread_id=thread_id,
        run_id=run_id,
        correlation_id=correlation_id,
        last_event_id=last_event_id,
        ack_handle_token=ack_handle_token,
        deadline_at=deadline_at or (now + timedelta(seconds=300)),
        attached_at=attached_at or now,
        current_lifecycle=current_lifecycle,
        updated_at=updated_at or now,
    )


# ---------------------------------------------------------------------------
# AC-2: migration creates the table on a fresh database
# ---------------------------------------------------------------------------


class TestMigrationCreatesTable:
    """The migration script must materialise the lifecycle_bridge_registry table."""

    def test_apply_creates_lifecycle_bridge_registry_table(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "fresh.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(cx)
            # Sanity: table absent before the bridge migration runs.
            row = cx.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='lifecycle_bridge_registry'",
            ).fetchone()
            assert row is None

            bridge_migration.apply(cx)

            row = cx.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='lifecycle_bridge_registry'",
            ).fetchone()
            assert row is not None
        finally:
            cx.close()

    def test_apply_is_idempotent(self, writer_db: sqlite3.Connection) -> None:
        # Second invocation must not raise nor duplicate the schema row.
        bridge_migration.apply(writer_db)
        bridge_migration.apply(writer_db)
        row = writer_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='lifecycle_bridge_registry'",
        ).fetchone()
        assert row is not None

    def test_table_has_required_columns(
        self, writer_db: sqlite3.Connection
    ) -> None:
        rows = writer_db.execute(
            "PRAGMA table_info(lifecycle_bridge_registry)",
        ).fetchall()
        names = {row[1] for row in rows}
        # AC-2 column manifest.
        required = {
            "feature_id",
            "thread_id",
            "run_id",
            "correlation_id",
            "last_event_id",
            "ack_handle_token",
            "deadline_at",
            "attached_at",
            "current_lifecycle",
            "updated_at",
        }
        assert required.issubset(names)


# ---------------------------------------------------------------------------
# AC-3: BridgeRegistry repository operations
# ---------------------------------------------------------------------------


class TestBridgeRegistryOperations:
    """Each repository method round-trips against an in-memory sqlite db."""

    def test_record_inserts_entry_round_trip(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(feature_id="FEAT-RT-001")
        registry.record(entry, correlation_id="corr-001")

        loaded = registry.get("FEAT-RT-001", correlation_id="corr-001")
        assert loaded is not None
        assert loaded.feature_id == entry.feature_id
        assert loaded.thread_id == entry.thread_id
        assert loaded.run_id == entry.run_id
        assert loaded.correlation_id == entry.correlation_id
        assert loaded.ack_handle_token == entry.ack_handle_token
        assert loaded.current_lifecycle == entry.current_lifecycle
        assert loaded.last_event_id is None

    def test_get_returns_none_for_unknown_feature(
        self, registry: BridgeRegistry
    ) -> None:
        assert registry.get("FEAT-MISSING", correlation_id="corr-x") is None

    def test_update_lifecycle_changes_state_and_updated_at(
        self, registry: BridgeRegistry, fixed_now: datetime
    ) -> None:
        entry = _make_entry(
            feature_id="FEAT-UP-001",
            current_lifecycle="queued",
            updated_at=fixed_now,
        )
        registry.record(entry, correlation_id="corr-up")

        registry.update_lifecycle(
            "FEAT-UP-001",
            "running",
            correlation_id="corr-up",
        )
        loaded = registry.get("FEAT-UP-001", correlation_id="corr-up")
        assert loaded is not None
        assert loaded.current_lifecycle == "running"
        # ``updated_at`` is refreshed to a wall-clock value at the call
        # site, so only the inequality with the seeded value is asserted.
        assert loaded.updated_at >= fixed_now

    def test_update_lifecycle_writes_last_event_id_when_supplied(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(feature_id="FEAT-EV-001")
        registry.record(entry, correlation_id="corr-ev")

        registry.update_lifecycle(
            "FEAT-EV-001",
            "running",
            correlation_id="corr-ev",
            last_event_id="evt-42",
        )
        loaded = registry.get("FEAT-EV-001", correlation_id="corr-ev")
        assert loaded is not None
        assert loaded.last_event_id == "evt-42"

    def test_update_lifecycle_preserves_last_event_id_when_omitted(
        self, registry: BridgeRegistry
    ) -> None:
        entry = _make_entry(
            feature_id="FEAT-EV-002",
            last_event_id="evt-prior",
        )
        registry.record(entry, correlation_id="corr-evp")

        registry.update_lifecycle(
            "FEAT-EV-002",
            "running",
            correlation_id="corr-evp",
        )
        loaded = registry.get("FEAT-EV-002", correlation_id="corr-evp")
        assert loaded is not None
        assert loaded.last_event_id == "evt-prior"

    def test_update_lifecycle_raises_for_unknown_feature(
        self, registry: BridgeRegistry
    ) -> None:
        with pytest.raises(BridgeRegistryNotFoundError):
            registry.update_lifecycle(
                "FEAT-NOPE",
                "running",
                correlation_id="corr-nope",
            )

    def test_delete_removes_row(self, registry: BridgeRegistry) -> None:
        entry = _make_entry(feature_id="FEAT-DEL-001")
        registry.record(entry, correlation_id="corr-del")
        registry.delete("FEAT-DEL-001", correlation_id="corr-del")
        assert registry.get("FEAT-DEL-001", correlation_id="corr-del") is None

    def test_delete_missing_feature_is_idempotent(
        self, registry: BridgeRegistry
    ) -> None:
        # Deleting a row that never existed is a no-op — the bridge
        # `detach()` may be invoked on a feature that has already been
        # cleaned up by `recover_in_flight`, and it must not raise.
        registry.delete("FEAT-NEVER", correlation_id="corr-z")


# ---------------------------------------------------------------------------
# AC-4: list_active() supports forge status --in-flight without leaking
# SSE connection metadata.
# ---------------------------------------------------------------------------


class TestBridgeRegistryListActive:
    """``list_active`` returns the rows for ``forge status --in-flight``."""

    def test_list_active_returns_recorded_rows(
        self, registry: BridgeRegistry
    ) -> None:
        registry.record(
            _make_entry(feature_id="FEAT-A"),
            correlation_id="corr-a",
        )
        registry.record(
            _make_entry(feature_id="FEAT-B", current_lifecycle="running"),
            correlation_id="corr-b",
        )
        active = registry.list_active(correlation_id="corr-list")
        feature_ids = {entry.feature_id for entry in active}
        assert feature_ids == {"FEAT-A", "FEAT-B"}

    def test_list_active_returns_empty_when_no_rows(
        self, registry: BridgeRegistry
    ) -> None:
        assert registry.list_active(correlation_id="corr-empty") == []

    def test_list_active_entries_have_no_sse_metadata(
        self, registry: BridgeRegistry
    ) -> None:
        registry.record(
            _make_entry(feature_id="FEAT-NOSSE"),
            correlation_id="corr-nosse",
        )
        active = registry.list_active(correlation_id="corr-nosse")
        # AC-4 — the entry exposes only the canonical fields. SSE
        # bookkeeping (open connection handle, http session, retry
        # counters) must NOT leak through this read path.
        forbidden = {"connection", "session", "stream", "client", "_sse"}
        for entry in active:
            attrs = set(vars(entry).keys()) if hasattr(entry, "__dict__") else set()
            # ``BridgeRegistryEntry`` is a frozen dataclass — defensive
            # check against any future addition that smuggles SSE state
            # into the entry.
            assert attrs.isdisjoint(forbidden), (
                f"BridgeRegistryEntry leaks SSE metadata: {attrs & forbidden}"
            )


# ---------------------------------------------------------------------------
# AC-5: every operation accepts correlation_id as a keyword argument.
# ---------------------------------------------------------------------------


class TestCorrelationIdContract:
    """Every BridgeRegistry method must accept ``correlation_id`` explicitly."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "record",
            "update_lifecycle",
            "get",
            "list_active",
            "delete",
        ],
    )
    def test_method_accepts_correlation_id_kwarg(
        self, registry: BridgeRegistry, method_name: str
    ) -> None:
        import inspect

        method = getattr(registry, method_name)
        signature = inspect.signature(method)
        assert "correlation_id" in signature.parameters, (
            f"{method_name!r} must accept correlation_id explicitly "
            "(F010C correlation-id contract)"
        )
        param = signature.parameters["correlation_id"]
        assert param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )


# ---------------------------------------------------------------------------
# Concurrency: two attach() calls for the same feature_id serialise.
# ---------------------------------------------------------------------------


class TestRecordConcurrency:
    """Two ``record`` calls for the same ``feature_id`` must not corrupt."""

    def test_second_record_overwrites_first(
        self, registry: BridgeRegistry
    ) -> None:
        first = _make_entry(
            feature_id="FEAT-CONC",
            thread_id="thread-1",
            run_id="run-1",
            ack_handle_token="ack-1",
            current_lifecycle="queued",
        )
        second = _make_entry(
            feature_id="FEAT-CONC",
            thread_id="thread-2",
            run_id="run-2",
            ack_handle_token="ack-2",
            current_lifecycle="running",
        )
        registry.record(first, correlation_id="corr-c1")
        registry.record(second, correlation_id="corr-c2")

        loaded = registry.get("FEAT-CONC", correlation_id="corr-c1")
        assert loaded is not None
        # The second write wins — UPSERT semantics so re-attach after
        # crash recovery does not leave dangling rows.
        assert loaded.thread_id == "thread-2"
        assert loaded.run_id == "run-2"
        assert loaded.ack_handle_token == "ack-2"
        assert loaded.current_lifecycle == "running"

    def test_parallel_records_serialise_without_corruption(
        self, tmp_path: Path
    ) -> None:
        # Each thread opens its own writer connection so we exercise
        # SQLite's BEGIN IMMEDIATE serialisation. The final state must
        # reflect exactly one of the writers (UPSERT is atomic).
        db_path = tmp_path / "forge.db"
        bootstrap = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(bootstrap)
            bridge_migration.apply(bootstrap)
        finally:
            bootstrap.close()

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _worker(token: str, lifecycle: str) -> None:
            try:
                cx = sqlite_connect.connect_writer(db_path)
                try:
                    repo = BridgeRegistry(connection=cx)
                    barrier.wait(timeout=5)
                    repo.record(
                        _make_entry(
                            feature_id="FEAT-PARALLEL",
                            ack_handle_token=token,
                            current_lifecycle=lifecycle,
                        ),
                        correlation_id=f"corr-{token}",
                    )
                finally:
                    cx.close()
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        t1 = threading.Thread(target=_worker, args=("ack-T1", "queued"))
        t2 = threading.Thread(target=_worker, args=("ack-T2", "running"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"parallel record raised: {errors!r}"

        verifier = sqlite_connect.connect_writer(db_path)
        try:
            repo = BridgeRegistry(connection=verifier)
            loaded = repo.get("FEAT-PARALLEL", correlation_id="corr-verify")
            assert loaded is not None
            # The winner is whichever write committed second; both
            # outcomes are valid as long as the row reflects one writer.
            assert loaded.ack_handle_token in {"ack-T1", "ack-T2"}
            assert loaded.current_lifecycle in {"queued", "running"}
        finally:
            verifier.close()
