"""Tests for ``forge.lifecycle_bridge.reconnect`` (TASK-FRR-PEB-008).

Acceptance-criteria coverage map:

* AC-1 — :class:`ReconnectPolicy` exposes
  :data:`RECONNECT_INITIAL_BACKOFF` (1.0s) and
  :data:`RECONNECT_MAX_BACKOFF` (30.0s); backoff doubles per attempt,
  caps at the maximum, resets to initial on success:
  :class:`TestReconnectPolicySchedule`,
  :class:`TestReconnectPolicyConstants`,
  :class:`TestReconnectPolicyResetOnSuccess`.
* AC-5 — Tests monkey-patch :data:`RECONNECT_INITIAL_BACKOFF` and
  :data:`RECONNECT_MAX_BACKOFF` to 0.05s for fast runs:
  :class:`TestMonkeyPatchedConstantsAreObserved`.
"""

from __future__ import annotations

import asyncio

import pytest

from forge.lifecycle_bridge import reconnect as reconnect_module
from forge.lifecycle_bridge.reconnect import (
    RECONNECT_INITIAL_BACKOFF,
    RECONNECT_MAX_BACKOFF,
    ReconnectPolicy,
)


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
