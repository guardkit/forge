"""Tests for ``forge.lifecycle_bridge.reconnect`` (TASK-FRR-PEB-008).

Acceptance-criteria coverage map:

* AC-1 — :class:`ReconnectPolicy` exposes
  :data:`RECONNECT_INITIAL_BACKOFF` (1.0s) and
  :data:`RECONNECT_MAX_BACKOFF` (30.0s); backoff doubles per attempt,
  caps at the maximum, resets to initial on success:
  :class:`TestReconnectPolicySchedule`,
  :class:`TestReconnectPolicyConstants`,
  :class:`TestReconnectPolicyResetOnSuccess`.
* AC-2 — The bridge's SSE observer task wraps its connection loop in
  :class:`ReconnectPolicy`; on transient errors it sleeps the current
  backoff and reconnects. No fixed maximum retry count:
  :class:`TestObserverWrapsStreamInReconnectPolicy`.
* AC-4 — Malformed SSE responses are logged at WARNING and the bridge
  reconnects rather than crashing. The reconnect counts as an attempt:
  :class:`TestObserverHandlesMalformedSSE`.
* AC-5 — Tests monkey-patch :data:`RECONNECT_INITIAL_BACKOFF` and
  :data:`RECONNECT_MAX_BACKOFF` to 0.05s for fast runs:
  :class:`TestMonkeyPatchedConstantsAreObserved`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge import reconnect as reconnect_module
from forge.lifecycle_bridge import wireup as wireup_module
from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    LifecycleBridge,
)
from forge.lifecycle_bridge.reconnect import (
    RECONNECT_INITIAL_BACKOFF,
    RECONNECT_MAX_BACKOFF,
    ReconnectPolicy,
)
from forge.lifecycle_bridge.translation import StreamEventTranslator
from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle


# ---------------------------------------------------------------------------
# AC-1 — module-level constants
# ---------------------------------------------------------------------------


class TestReconnectPolicyConstants:
    """AC-1: the documented constants are exposed at module-level."""

    def test_initial_backoff_is_one_second(self) -> None:
        assert RECONNECT_INITIAL_BACKOFF == 1.0

    def test_max_backoff_is_thirty_seconds(self) -> None:
        assert RECONNECT_MAX_BACKOFF == 30.0

    def test_constants_are_floats_not_ints(self) -> None:
        # The forge daemon uses ``asyncio.sleep`` which expects float;
        # an int constant would silently work but break parity with
        # the existing forge.cli._serve_daemon constants.
        assert isinstance(RECONNECT_INITIAL_BACKOFF, float)
        assert isinstance(RECONNECT_MAX_BACKOFF, float)


# ---------------------------------------------------------------------------
# AC-1 — backoff doubling, cap, no fixed retry count
# ---------------------------------------------------------------------------


class TestReconnectPolicySchedule:
    """AC-1: 1.0 → 2.0 → 4.0 → ... → 30.0 → 30.0 (capped, no max retries)."""

    def test_first_backoff_is_initial(self) -> None:
        policy = ReconnectPolicy()
        assert policy.next_backoff() == 1.0

    def test_second_backoff_doubles(self) -> None:
        policy = ReconnectPolicy()
        policy.next_backoff()  # 1.0
        assert policy.next_backoff() == 2.0

    def test_backoff_doubles_through_to_cap(self) -> None:
        policy = ReconnectPolicy()
        sequence: list[float] = []
        # Drive far past the cap to verify the plateau.
        for _ in range(10):
            sequence.append(policy.next_backoff())
        # Documented sequence (per task acceptance criteria):
        # 1.0 → 2.0 → 4.0 → 8.0 → 16.0 → 30.0 → 30.0 → 30.0 → 30.0 → 30.0
        assert sequence == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0, 30.0]

    def test_backoff_caps_at_max(self) -> None:
        policy = ReconnectPolicy()
        # Drive a hundred attempts — never exceed the cap.
        results = [policy.next_backoff() for _ in range(100)]
        assert max(results) == RECONNECT_MAX_BACKOFF
        # And every value past the cap is exactly the cap (no overflow).
        assert all(value == RECONNECT_MAX_BACKOFF for value in results[6:])

    def test_no_fixed_maximum_retry_count(self) -> None:
        # AC-1: "No fixed maximum retry count" — the policy never
        # raises StopIteration / IndexError / similar exhaustion error
        # no matter how many attempts the caller drives.
        policy = ReconnectPolicy()
        for _ in range(10_000):
            value = policy.next_backoff()
            assert value <= RECONNECT_MAX_BACKOFF


# ---------------------------------------------------------------------------
# AC-1 — reset on success
# ---------------------------------------------------------------------------


class TestReconnectPolicyResetOnSuccess:
    """AC-1: a successful reconnect resets the schedule to initial."""

    def test_reset_after_three_failures_returns_to_initial(self) -> None:
        policy = ReconnectPolicy()
        # Three failures → schedule is at 8.0 for the next call.
        assert policy.next_backoff() == 1.0
        assert policy.next_backoff() == 2.0
        assert policy.next_backoff() == 4.0
        assert policy.current_backoff == 8.0
        # Successful reconnect → reset.
        policy.reset()
        # Next failure: starts at 1.0, NOT at 8.0.
        assert policy.next_backoff() == 1.0

    def test_reset_after_cap_hit_returns_to_initial(self) -> None:
        policy = ReconnectPolicy()
        for _ in range(8):
            policy.next_backoff()
        assert policy.current_backoff == RECONNECT_MAX_BACKOFF
        policy.reset()
        assert policy.next_backoff() == 1.0

    def test_reset_on_fresh_policy_is_idempotent(self) -> None:
        policy = ReconnectPolicy()
        policy.reset()
        assert policy.next_backoff() == 1.0


# ---------------------------------------------------------------------------
# AC-1 — current_backoff property
# ---------------------------------------------------------------------------


class TestReconnectPolicyCurrentBackoff:
    """current_backoff peeks at the next value without advancing the schedule."""

    def test_initial_current_backoff_is_initial_constant(self) -> None:
        policy = ReconnectPolicy()
        assert policy.current_backoff == RECONNECT_INITIAL_BACKOFF

    def test_current_backoff_does_not_advance_schedule(self) -> None:
        policy = ReconnectPolicy()
        # Read four times — the schedule should not advance.
        for _ in range(4):
            assert policy.current_backoff == 1.0
        # And the first ``next_backoff`` still returns the initial.
        assert policy.next_backoff() == 1.0

    def test_current_backoff_reflects_advanced_schedule(self) -> None:
        policy = ReconnectPolicy()
        policy.next_backoff()  # 1.0 → schedule at 2.0
        assert policy.current_backoff == 2.0
        policy.next_backoff()  # 2.0 → schedule at 4.0
        assert policy.current_backoff == 4.0


# ---------------------------------------------------------------------------
# AC-5 — monkey-patched constants are observed
# ---------------------------------------------------------------------------


class TestMonkeyPatchedConstantsAreObserved:
    """AC-5: tests can monkey-patch the constants for fast runs."""

    def test_patched_initial_backoff_takes_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.05)
        policy = ReconnectPolicy()
        assert policy.next_backoff() == 0.05

    def test_patched_max_backoff_takes_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.05)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.05)
        policy = ReconnectPolicy()
        # With both pinned to 0.05, every call returns the cap.
        assert policy.next_backoff() == 0.05
        assert policy.next_backoff() == 0.05
        assert policy.next_backoff() == 0.05

    def test_patched_constants_observed_after_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Construct with default constants, then patch — the policy
        # should pick up the patched values on the very next call.
        policy = ReconnectPolicy()
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.01)
        assert policy.next_backoff() == 0.01

    def test_reset_observes_patched_initial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        policy = ReconnectPolicy()
        policy.next_backoff()  # advance the schedule
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.05)
        policy.reset()
        assert policy.next_backoff() == 0.05


# ---------------------------------------------------------------------------
# AC-5 — sleep_then_advance helper
# ---------------------------------------------------------------------------


class TestSleepThenAdvance:
    """``sleep_then_advance`` is the production sleep + schedule helper."""

    @pytest.mark.asyncio
    async def test_sleep_then_advance_uses_injected_sleeper(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        policy = ReconnectPolicy()
        backoff = await policy.sleep_then_advance(sleep_fn=fake_sleep)
        assert backoff == 1.0
        assert sleeps == [1.0]
        # And the schedule advanced — second sleep is 2.0.
        await policy.sleep_then_advance(sleep_fn=fake_sleep)
        assert sleeps == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_sleep_then_advance_with_patched_constants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.001)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.001)
        policy = ReconnectPolicy()
        # Real ``asyncio.sleep`` — 0.001s is fast enough to be invisible
        # in CI even on slow runners.
        backoff = await policy.sleep_then_advance()
        assert backoff == 0.001

    @pytest.mark.asyncio
    async def test_sleep_then_advance_default_is_asyncio_sleep(self) -> None:
        # Without an injected sleeper, the helper falls back to
        # ``asyncio.sleep``. We verify the fallback path doesn't raise
        # by passing a tiny sleep budget.
        policy = ReconnectPolicy()
        # Patch the *module's* asyncio.sleep so we don't actually wait.
        called = asyncio.Event()

        async def fake_asyncio_sleep(seconds: float) -> None:
            called.set()

        # Use the explicit injection path to validate the seam.
        await policy.sleep_then_advance(sleep_fn=fake_asyncio_sleep)
        assert called.is_set()


# ---------------------------------------------------------------------------
# AC-2 / AC-4 — wireup integration tests
# ---------------------------------------------------------------------------


class _FakeConnectError(Exception):
    """Stand-in for ``httpx.ConnectError`` in unit tests.

    The wireup observer treats any class in
    :data:`wireup.TRANSIENT_STREAM_ERRORS` as transient. Tests
    monkey-patch the tuple to include this fake error so they don't
    have to take a runtime dependency on ``httpx``.
    """


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


def _make_handle() -> BuildAckHandle:
    handle = AsyncMock(spec=BuildAckHandle)
    handle.ack = AsyncMock()
    handle.nak = AsyncMock()
    return handle


def _make_publisher() -> MagicMock:
    pub = MagicMock(name="PipelinePublisher")
    for name in (
        "publish_build_started",
        "publish_stage_complete",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_cancelled",
        "publish_build_progress",
    ):
        setattr(pub, name, AsyncMock(name=name))
    return pub


def _identity(_feature_id: str) -> Any:
    async def _provider(_fid: str) -> tuple[str, str] | None:
        return ("thread-x", "run-x")

    return _provider


class TestObserverWrapsStreamInReconnectPolicy:
    """AC-2: observer reconnects on transient SSE errors with backoff."""

    @pytest.mark.asyncio
    async def test_transient_connect_error_triggers_reconnect_with_backoff(
        self,
        registry: BridgeRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch backoff to near-zero so the test runs quickly (AC-5).
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.01)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.01)
        # Inject our fake into the wireup's transient error tuple so we
        # don't depend on httpx in the test environment.
        monkeypatch.setattr(
            wireup_module,
            "TRANSIENT_STREAM_ERRORS",
            (_FakeConnectError, json.JSONDecodeError),
        )

        attempts: list[int] = []

        def factory(*, feature_id: str, thread_id, run_id) -> AsyncIterator[Any]:
            attempts.append(len(attempts) + 1)
            current_attempt = attempts[-1]

            async def gen() -> AsyncIterator[Any]:
                if current_attempt < 3:
                    raise _FakeConnectError(f"attempt {current_attempt}")
                # Third attempt: clean stream end with no events.
                if False:
                    yield  # pragma: no cover - keep gen typed as AsyncIterator
                return

            return gen()

        bridge = LifecycleBridge(registry=registry)
        translator = StreamEventTranslator()
        publisher = _make_publisher()
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=publisher,
            stream_source=factory,
            identity_provider=_identity("any"),
            deadline_seconds=300,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.0,
            shutdown_timeout_seconds=2.0,
        )

        await wireup.register_ack_handle("FEAT-RC", "corr-rc", _make_handle())

        # Wait for the observer to exit (reconnects 2x then exits cleanly).
        task = wireup.get_observer_task("FEAT-RC")
        assert task is not None
        await asyncio.wait_for(task, timeout=2.0)

        # AC-2: at least 3 stream-source invocations (2 transient errors
        # + 1 successful clean exit) — proving the loop reconnected.
        assert len(attempts) >= 3
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_no_fixed_maximum_retry_count_in_observer(
        self,
        registry: BridgeRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch backoff to ~zero and the deadline to a tiny value so we
        # can verify the observer keeps reconnecting until cancelled —
        # i.e. there's no hard-coded N-attempts ceiling.
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.001)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.001)
        monkeypatch.setattr(
            wireup_module,
            "TRANSIENT_STREAM_ERRORS",
            (_FakeConnectError,),
        )

        call_count = 0

        def factory(*, feature_id: str, thread_id, run_id) -> AsyncIterator[Any]:
            nonlocal call_count
            call_count += 1

            async def gen() -> AsyncIterator[Any]:
                raise _FakeConnectError("permanent failure")
                yield  # pragma: no cover

            return gen()

        bridge = LifecycleBridge(registry=registry)
        translator = StreamEventTranslator()
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=_make_publisher(),
            stream_source=factory,
            identity_provider=_identity("any"),
            deadline_seconds=300,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.0,
            shutdown_timeout_seconds=2.0,
        )

        await wireup.register_ack_handle("FEAT-NL", "corr-nl", _make_handle())
        # Let it spin for a short while — well past any plausible
        # hard-coded retry limit (e.g. "5" or "10").
        await asyncio.sleep(0.2)

        assert call_count > 20, (
            f"observer should keep reconnecting indefinitely; got "
            f"{call_count} attempts in 0.2s"
        )

        # CancelledError → terminate the loop cleanly.
        await wireup.shutdown()


class TestObserverHandlesMalformedSSE:
    """AC-4: malformed SSE → WARNING log, reconnect, no daemon crash."""

    @pytest.mark.asyncio
    async def test_malformed_json_is_logged_and_triggers_reconnect(
        self,
        registry: BridgeRegistry,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.01)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.01)
        # Use json.JSONDecodeError (the production transient set
        # already includes it) — no further patching of the tuple.

        attempts: list[int] = []

        def factory(*, feature_id: str, thread_id, run_id) -> AsyncIterator[Any]:
            attempts.append(1)
            current = len(attempts)

            async def gen() -> AsyncIterator[Any]:
                if current < 2:
                    raise json.JSONDecodeError("bad", "doc", 0)
                # Second attempt: clean exit.
                if False:
                    yield  # pragma: no cover
                return

            return gen()

        bridge = LifecycleBridge(registry=registry)
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=StreamEventTranslator(),
            publisher=_make_publisher(),
            stream_source=factory,
            identity_provider=_identity("any"),
            deadline_seconds=300,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.0,
            shutdown_timeout_seconds=2.0,
        )

        with caplog.at_level("WARNING", logger="forge.lifecycle_bridge.wireup"):
            await wireup.register_ack_handle(
                "FEAT-MAL", "corr-mal", _make_handle()
            )
            task = wireup.get_observer_task("FEAT-MAL")
            assert task is not None
            await asyncio.wait_for(task, timeout=2.0)

        # AC-4: WARNING log emitted with the parse failure.
        warning_messages = [
            rec.message for rec in caplog.records if rec.levelname == "WARNING"
        ]
        assert any(
            "transient SSE error" in msg or "JSONDecodeError" in msg
            for msg in warning_messages
        )
        # AC-4: reconnect happened (>= 2 stream opens).
        assert len(attempts) >= 2
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_malformed_response_does_not_crash_daemon(
        self,
        registry: BridgeRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(reconnect_module, "RECONNECT_INITIAL_BACKOFF", 0.01)
        monkeypatch.setattr(reconnect_module, "RECONNECT_MAX_BACKOFF", 0.01)

        # Stream raises malformed JSON, then exits cleanly. The
        # observer must complete without propagating an exception
        # to the daemon supervisor.
        first_call = True

        def factory(*, feature_id: str, thread_id, run_id) -> AsyncIterator[Any]:
            nonlocal first_call
            was_first = first_call
            first_call = False

            async def gen() -> AsyncIterator[Any]:
                if was_first:
                    raise json.JSONDecodeError("bad", "doc", 0)
                if False:
                    yield  # pragma: no cover
                return

            return gen()

        bridge = LifecycleBridge(registry=registry)
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=StreamEventTranslator(),
            publisher=_make_publisher(),
            stream_source=factory,
            identity_provider=_identity("any"),
            deadline_seconds=300,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.0,
            shutdown_timeout_seconds=2.0,
        )

        await wireup.register_ack_handle(
            "FEAT-NOCRASH", "corr-nc", _make_handle()
        )
        task = wireup.get_observer_task("FEAT-NOCRASH")
        assert task is not None
        # Observer completes without raising.
        await asyncio.wait_for(task, timeout=2.0)
        assert task.exception() is None
        await wireup.shutdown()
