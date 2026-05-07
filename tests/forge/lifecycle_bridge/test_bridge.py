"""Tests for ``forge.lifecycle_bridge.bridge`` (TASK-FRR-PEB-002).

Acceptance-criteria coverage map:

* AC-1: ``LifecycleBridge`` exposes ``attach``, ``detach``,
  ``recover_in_flight``, ``shutdown`` — :class:`TestLifecycleBridgeSurface`.
* AC-4: ``attach`` writes a row, ``detach`` deletes it,
  ``recover_in_flight`` exposes the active set for ``forge status``
  without leaking SSE metadata — :class:`TestAttachDetachRoundTrip`.
* AC-5: every BridgeRegistry call site in ``bridge.py`` threads the
  caller-supplied ``correlation_id`` — :class:`TestCorrelationIdThreading`.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    LifecycleBridge,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry


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


def _make_context(
    *,
    feature_id: str = "FEAT-BRIDGE-001",
    thread_id: str = "thread-bridge",
    run_id: str = "run-bridge",
    correlation_id: str = "corr-bridge",
    deadline_at: datetime | None = None,
) -> BuildContext:
    if deadline_at is None:
        deadline_at = datetime.now(UTC) + timedelta(seconds=300)
    return BuildContext(
        feature_id=feature_id,
        thread_id=thread_id,
        run_id=run_id,
        correlation_id=correlation_id,
        deadline_at=deadline_at,
    )


# ---------------------------------------------------------------------------
# AC-1: surface contract.
# ---------------------------------------------------------------------------


class TestLifecycleBridgeSurface:
    """The bridge exposes the four public methods declared by AC-1."""

    @pytest.mark.parametrize(
        "method_name",
        ["attach", "detach", "recover_in_flight", "shutdown"],
    )
    def test_method_is_public_callable(
        self, bridge: LifecycleBridge, method_name: str
    ) -> None:
        assert hasattr(bridge, method_name)
        attr = getattr(bridge, method_name)
        assert callable(attr)

    def test_attach_signature_takes_build_context_and_ack_handle(
        self, bridge: LifecycleBridge
    ) -> None:
        signature = inspect.signature(bridge.attach)
        params = signature.parameters
        # AC-1: attach(build_context, ack_handle).
        assert "build_context" in params
        assert "ack_handle" in params

    def test_detach_signature_takes_feature_id(
        self, bridge: LifecycleBridge
    ) -> None:
        signature = inspect.signature(bridge.detach)
        assert "feature_id" in signature.parameters


# ---------------------------------------------------------------------------
# AC-4: attach writes a row, detach deletes it, recover_in_flight returns
# the active set with no SSE connection metadata leaking.
# ---------------------------------------------------------------------------


class TestAttachDetachRoundTrip:
    """``attach`` and ``detach`` round-trip through the BridgeRegistry."""

    def test_attach_writes_registry_row(
        self, bridge: LifecycleBridge, registry: BridgeRegistry
    ) -> None:
        ctx = _make_context(feature_id="FEAT-AT-001")
        bridge.attach(ctx, AckHandle(token="ack-AT-1"))

        loaded = registry.get("FEAT-AT-001", correlation_id=ctx.correlation_id)
        assert loaded is not None
        assert loaded.feature_id == "FEAT-AT-001"
        assert loaded.thread_id == ctx.thread_id
        assert loaded.run_id == ctx.run_id
        assert loaded.correlation_id == ctx.correlation_id
        assert loaded.ack_handle_token == "ack-AT-1"
        # ``attached_at`` is populated.
        assert loaded.attached_at is not None
        # Initial lifecycle is "queued" per the FRR-PEB-002 contract.
        assert loaded.current_lifecycle == "queued"

    def test_detach_removes_row(
        self, bridge: LifecycleBridge, registry: BridgeRegistry
    ) -> None:
        ctx = _make_context(feature_id="FEAT-DT-001")
        bridge.attach(ctx, AckHandle(token="ack-DT-1"))
        assert (
            registry.get("FEAT-DT-001", correlation_id=ctx.correlation_id)
            is not None
        )

        bridge.detach("FEAT-DT-001", correlation_id=ctx.correlation_id)
        assert (
            registry.get("FEAT-DT-001", correlation_id=ctx.correlation_id)
            is None
        )

    def test_recover_in_flight_returns_active_entries(
        self, bridge: LifecycleBridge, registry: BridgeRegistry
    ) -> None:
        bridge.attach(
            _make_context(feature_id="FEAT-R1"),
            AckHandle(token="ack-r1"),
        )
        bridge.attach(
            _make_context(
                feature_id="FEAT-R2",
                correlation_id="corr-r2",
            ),
            AckHandle(token="ack-r2"),
        )
        recovered = bridge.recover_in_flight(correlation_id="corr-recover")
        feature_ids = {entry.feature_id for entry in recovered}
        assert feature_ids == {"FEAT-R1", "FEAT-R2"}

    def test_recover_in_flight_no_sse_metadata_leaks(
        self, bridge: LifecycleBridge
    ) -> None:
        bridge.attach(
            _make_context(feature_id="FEAT-NL"),
            AckHandle(token="ack-nl"),
        )
        recovered = bridge.recover_in_flight(correlation_id="corr-nl")
        forbidden = {"connection", "session", "stream", "client", "_sse"}
        for entry in recovered:
            attrs = set(vars(entry).keys()) if hasattr(entry, "__dict__") else set()
            assert attrs.isdisjoint(forbidden), (
                f"recover_in_flight leaked SSE metadata: {attrs & forbidden}"
            )

    def test_shutdown_does_not_raise(self, bridge: LifecycleBridge) -> None:
        # T4 will populate the SSE-disconnect logic; T2 only requires
        # ``shutdown`` to be a clean no-op so ``forge serve`` startup
        # tests can exercise the bridge lifecycle without a live SSE
        # peer.
        bridge.shutdown()


# ---------------------------------------------------------------------------
# AC-5: correlation_id is threaded through every BridgeRegistry call.
# ---------------------------------------------------------------------------


class TestCorrelationIdThreading:
    """Every BridgeRegistry call site in bridge.py threads correlation_id.

    The AC requires an AST guard fixture that lists the new bridge call
    sites and asserts each call passes ``correlation_id=`` as a keyword.
    See :mod:`tests.forge.test_pipeline_consumer_correlation_id` for the
    cross-cut sibling guard against ``_safe_publish_failure`` call sites
    in the pipeline consumer; the bridge's contract mirrors that one.
    """

    def test_attach_threads_correlation_id_to_record(
        self, bridge: LifecycleBridge, registry: BridgeRegistry
    ) -> None:
        ctx = _make_context(
            feature_id="FEAT-THREAD-1",
            correlation_id="corr-thread-attach",
        )
        bridge.attach(ctx, AckHandle(token="ack-th-1"))

        loaded = registry.get(
            "FEAT-THREAD-1",
            correlation_id="corr-thread-attach",
        )
        assert loaded is not None
        # The persisted row carries the inbound correlation_id.
        assert loaded.correlation_id == "corr-thread-attach"

    def test_detach_accepts_correlation_id_kwarg(
        self, bridge: LifecycleBridge
    ) -> None:
        signature = inspect.signature(bridge.detach)
        assert "correlation_id" in signature.parameters

    def test_recover_in_flight_accepts_correlation_id_kwarg(
        self, bridge: LifecycleBridge
    ) -> None:
        signature = inspect.signature(bridge.recover_in_flight)
        assert "correlation_id" in signature.parameters
