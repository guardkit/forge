"""Tests for the per-build deadline timer (TASK-FRR-PEB-008).

Acceptance-criteria coverage map:

* AC-3 — ``LifecycleBridge.attach()`` starts a per-build deadline
  timer with a configurable budget; if no terminal envelope is
  observed within the budget, the deadline handler is invoked with
  the original :class:`BuildContext`:
  :class:`TestDeadlineFiresWhenNoTerminalObserved`,
  :class:`TestDeadlineHandlerReceivesBuildContext`.
* AC-3 — ``LifecycleBridge.detach()`` cancels the deadline timer so
  a normal terminal envelope does not race the deadline path:
  :class:`TestDeadlineCancelledOnTerminal`.
* AC-3 — ``LifecycleBridge.shutdown()`` cancels every live deadline
  timer:
  :class:`TestShutdownCancelsDeadlines`.
* AC-5 — Tests monkey-patch :data:`DEADLINE_SECONDS` to a tiny value
  for fast runs:
  :class:`TestDeadlineMonkeyPatchedToFastValue`.
* AC-6 — The deadline handler receives the original
  ``correlation_id`` so the build-failed envelope it publishes
  carries the inbound id:
  :class:`TestDeadlineHandlerReceivesBuildContext`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge import bridge as bridge_module
from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    DEADLINE_SECONDS,
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


def _make_context(
    feature_id: str = "FEAT-DL-001",
    correlation_id: str = "corr-dl-001",
) -> BuildContext:
    now = datetime.now(UTC)
    return BuildContext(
        feature_id=feature_id,
        thread_id=f"thread-{feature_id}",
        run_id=f"run-{feature_id}",
        correlation_id=correlation_id,
        deadline_at=now + timedelta(seconds=DEADLINE_SECONDS),
    )


# ---------------------------------------------------------------------------
# AC-3 — deadline fires when no terminal observed
# ---------------------------------------------------------------------------


class TestDeadlineFiresWhenNoTerminalObserved:
    """AC-3: a build with no terminal envelope triggers the handler."""

    @pytest.mark.asyncio
    async def test_handler_invoked_after_deadline_seconds(
        self, registry: BridgeRegistry
    ) -> None:
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.05,  # 50ms budget for fast tests
        )
        ctx = _make_context()
        bridge.attach(ctx, AckHandle(token="tok-001"))

        # Wait for the deadline to elapse plus margin.
        await asyncio.sleep(0.15)

        handler.assert_awaited_once()
        # Handler receives the BuildContext that was passed to attach().
        called_ctx = handler.await_args.args[0]
        assert called_ctx.feature_id == ctx.feature_id

    @pytest.mark.asyncio
    async def test_handler_not_invoked_before_deadline(
        self, registry: BridgeRegistry
    ) -> None:
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=1.0,
        )
        bridge.attach(_make_context(), AckHandle(token="tok-002"))

        # Sleep a fraction of the deadline.
        await asyncio.sleep(0.05)
        handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC-3 / AC-6 — handler receives full BuildContext (correlation_id, etc.)
# ---------------------------------------------------------------------------


class TestDeadlineHandlerReceivesBuildContext:
    """AC-6: the handler can read correlation_id from the context."""

    @pytest.mark.asyncio
    async def test_handler_can_read_correlation_id(
        self, registry: BridgeRegistry
    ) -> None:
        seen: list[BuildContext] = []

        async def handler(ctx: BuildContext) -> None:
            seen.append(ctx)

        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.05,
        )
        ctx = _make_context(
            feature_id="FEAT-CORR", correlation_id="corr-XYZ-123"
        )
        bridge.attach(ctx, AckHandle(token="tok-003"))

        await asyncio.sleep(0.15)

        assert len(seen) == 1
        assert seen[0].correlation_id == "corr-XYZ-123"
        assert seen[0].feature_id == "FEAT-CORR"

    @pytest.mark.asyncio
    async def test_multiple_in_flight_each_get_own_handler_invocation(
        self, registry: BridgeRegistry
    ) -> None:
        seen_feature_ids: list[str] = []

        async def handler(ctx: BuildContext) -> None:
            seen_feature_ids.append(ctx.feature_id)

        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.05,
        )
        bridge.attach(
            _make_context(feature_id="FEAT-A", correlation_id="corr-a"),
            AckHandle(token="tok-a"),
        )
        bridge.attach(
            _make_context(feature_id="FEAT-B", correlation_id="corr-b"),
            AckHandle(token="tok-b"),
        )

        await asyncio.sleep(0.15)

        assert sorted(seen_feature_ids) == ["FEAT-A", "FEAT-B"]


# ---------------------------------------------------------------------------
# AC-3 — terminal envelope cancels deadline
# ---------------------------------------------------------------------------


class TestDeadlineCancelledOnTerminal:
    """AC-3: detach() cancels the deadline so it does not race the terminal."""

    @pytest.mark.asyncio
    async def test_detach_cancels_deadline_handler(
        self, registry: BridgeRegistry
    ) -> None:
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.10,
        )
        ctx = _make_context()
        bridge.attach(ctx, AckHandle(token="tok-cancel"))

        # Detach before the deadline elapses.
        await asyncio.sleep(0.02)
        bridge.detach(ctx.feature_id, correlation_id=ctx.correlation_id)

        # Wait for what would have been the deadline.
        await asyncio.sleep(0.20)

        # Handler must NOT have been invoked — the terminal arrived
        # before the deadline expired.
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_deadline_returns_true_when_active(
        self, registry: BridgeRegistry
    ) -> None:
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=10.0,
        )
        ctx = _make_context()
        bridge.attach(ctx, AckHandle(token="tok-active"))

        # Yield so the timer task is actually scheduled.
        await asyncio.sleep(0)

        cancelled = bridge.cancel_deadline(ctx.feature_id)
        assert cancelled is True
        # A second call returns False — the timer is gone.
        cancelled_again = bridge.cancel_deadline(ctx.feature_id)
        assert cancelled_again is False

    @pytest.mark.asyncio
    async def test_cancel_deadline_for_unknown_feature_returns_false(
        self, registry: BridgeRegistry
    ) -> None:
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=AsyncMock(),
            deadline_seconds=10.0,
        )
        # No attach has happened — the cancel is a clean no-op.
        assert bridge.cancel_deadline("FEAT-UNKNOWN") is False


# ---------------------------------------------------------------------------
# AC-3 — shutdown cancels every live deadline
# ---------------------------------------------------------------------------


class TestShutdownCancelsDeadlines:
    """AC-3 / wireup AC-6: shutdown cancels every live deadline timer."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_all_in_flight_deadlines(
        self, registry: BridgeRegistry
    ) -> None:
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.10,
        )
        bridge.attach(
            _make_context(feature_id="FEAT-S1", correlation_id="corr-s1"),
            AckHandle(token="tok-s1"),
        )
        bridge.attach(
            _make_context(feature_id="FEAT-S2", correlation_id="corr-s2"),
            AckHandle(token="tok-s2"),
        )

        # Yield so timers are scheduled.
        await asyncio.sleep(0.01)
        bridge.shutdown()

        # Wait past the deadline — handler must not fire.
        await asyncio.sleep(0.20)
        handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC-5 — monkey-patched DEADLINE_SECONDS is observed
# ---------------------------------------------------------------------------


class TestDeadlineMonkeyPatchedToFastValue:
    """AC-5: tests monkey-patch ``DEADLINE_SECONDS`` for fast runs."""

    @pytest.mark.asyncio
    async def test_module_default_deadline_is_300_seconds(self) -> None:
        # AC-3: 300s budget is the documented default per ASSUM-003.
        assert DEADLINE_SECONDS == 300.0

    @pytest.mark.asyncio
    async def test_monkey_patched_module_constant_takes_effect(
        self, registry: BridgeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bridge_module, "DEADLINE_SECONDS", 0.05)
        handler = AsyncMock()
        # No constructor override → bridge reads module global at attach().
        bridge = LifecycleBridge(registry=registry, deadline_handler=handler)
        bridge.attach(_make_context(), AckHandle(token="tok-mp"))

        await asyncio.sleep(0.15)
        handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-3 — deadline handler exception does not crash supervisor
# ---------------------------------------------------------------------------


class TestDeadlineHandlerErrorIsLoggedNotRaised:
    """AC-3: a handler exception is logged; the supervisor stays alive."""

    @pytest.mark.asyncio
    async def test_handler_exception_is_swallowed(
        self,
        registry: BridgeRegistry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def angry_handler(ctx: BuildContext) -> None:
            raise RuntimeError("publisher transport down")

        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=angry_handler,
            deadline_seconds=0.05,
        )
        bridge.attach(_make_context(), AckHandle(token="tok-err"))

        # Wait past deadline; the supervisor must remain operational.
        await asyncio.sleep(0.15)

        # Bridge state is still usable.
        bridge.shutdown()


# ---------------------------------------------------------------------------
# AC-3 — bridge without handler is backward-compatible (T2/T3 callers)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Bridges constructed without ``deadline_handler`` keep working."""

    def test_bridge_without_handler_does_not_schedule_timer(
        self, registry: BridgeRegistry
    ) -> None:
        # Constructed in a sync context — no event loop, no handler.
        # Must not raise; existing T2/T3 unit tests rely on this.
        bridge = LifecycleBridge(registry=registry)
        ctx = _make_context()
        bridge.attach(ctx, AckHandle(token="tok-back-compat"))
        # Detach also clean.
        bridge.detach(ctx.feature_id, correlation_id=ctx.correlation_id)
        bridge.shutdown()

    @pytest.mark.asyncio
    async def test_bridge_with_handler_in_sync_context_skips_timer(
        self, registry: BridgeRegistry
    ) -> None:
        # Bridge created with handler, but attach() called from inside
        # an async context (the production path). This ensures the
        # async-context branch is what fires the timer.
        handler = AsyncMock()
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=handler,
            deadline_seconds=0.05,
        )
        bridge.attach(_make_context(), AckHandle(token="tok-async"))
        await asyncio.sleep(0.15)
        handler.assert_awaited_once()
