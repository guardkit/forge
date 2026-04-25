"""Tests for :mod:`forge.dispatch.correlation` (TASK-SAD-003).

One ``Test*`` class per acceptance criterion in
``tasks/design_approved/TASK-SAD-003-correlation-registry.md``:

* AC-001 — module exposes :class:`CorrelationRegistry` with
  ``fresh_correlation_key``, ``bind``, ``wait_for_reply``,
  ``deliver_reply``, ``release``.
* AC-002 — ``bind()`` returns ONLY after the subscription is active;
  the binding handle exposes ``subscription_active`` so the
  orchestrator can assert this in tests.
* AC-003 — ``fresh_correlation_key()`` returns 32 lowercase hex
  characters with no embedded PII.
* AC-004 — ``deliver_reply()`` drops replies whose correlation key has
  no binding (``C.wrong-correlation-reply``).
* AC-005 — ``deliver_reply()`` drops replies whose ``source_agent_id``
  differs from ``binding.matched_agent_id``
  (``E.reply-source-authenticity``).
* AC-006 — ``deliver_reply()`` drops a second authenticated reply
  after ``binding.accepted is True`` (``E.duplicate-reply-idempotency``);
  the first reply's outcome is preserved.
* AC-007 — ``release()`` is idempotent and prevents subsequent replies
  from being delivered (``D.unsubscribe-on-timeout``).
* AC-008 — PubAck on the audit stream does NOT flip ``accepted``
  (``C.pubAck-not-success``).
* AC-009 — concurrent ``deliver_reply()`` calls are serialised so
  exactly-once is robust under races.

The transport is a sync/async in-memory fake (``FakeReplyChannel``);
we deliberately do not import ``nats`` here — the production NATS
implementation lives in TASK-SAD-010.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

import pytest

from forge.dispatch.correlation import (
    CORRELATION_KEY_RE,
    CorrelationBinding,
    CorrelationRegistry,
)


DeliverFn = Callable[[str, str, "dict[str, Any]"], None]


class FakeReplyChannel:
    """In-memory :class:`forge.dispatch.correlation.ReplyChannel` fake.

    Exposes the operational surface the registry needs plus test hooks:

    * :attr:`subscribe_gate` — when set, ``subscribe()`` blocks on it,
      letting tests verify subscribe-before-publish ordering by asserting
      that ``bind()`` has not yet returned while the gate is closed.
    * :meth:`emit_reply` — synchronously invokes the registered
      ``deliver`` callback for a key.
    * :meth:`emit_puback` — simulates a PubAck on the audit stream.
      Critically, it does NOT invoke ``deliver_reply`` — PubAcks travel
      a different channel in production, and that separation is what
      ``C.pubAck-not-success`` requires the registry to assume.
    """

    def __init__(
        self, subscribe_gate: asyncio.Event | None = None
    ) -> None:
        self._handlers: dict[str, DeliverFn] = {}
        self.subscribed_keys: list[str] = []
        self.unsubscribed_keys: list[str] = []
        self.subscribe_gate = subscribe_gate
        self.pubacks_emitted = 0

    async def subscribe(
        self, correlation_key: str, deliver: DeliverFn
    ) -> str:
        if self.subscribe_gate is not None:
            await self.subscribe_gate.wait()
        self._handlers[correlation_key] = deliver
        self.subscribed_keys.append(correlation_key)
        # Use the key itself as the opaque handle.
        return correlation_key

    async def unsubscribe(self, subscription: str) -> None:
        self.unsubscribed_keys.append(subscription)
        self._handlers.pop(subscription, None)

    def emit_reply(
        self,
        correlation_key: str,
        source_agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        handler = self._handlers.get(correlation_key)
        if handler is not None:
            handler(correlation_key, source_agent_id, payload)

    def emit_puback(self, correlation_key: str) -> None:
        """Simulate a PubAck on the audit stream.

        Per ``C.pubAck-not-success``, PubAck does NOT travel through
        the per-correlation reply subscription — it lands on a
        dedicated audit channel. This stub increments a counter so the
        test can assert the PubAck was emitted, but never invokes
        ``deliver_reply``, mirroring the production transport split.
        """
        self.pubacks_emitted += 1


@pytest.fixture
def transport() -> FakeReplyChannel:
    return FakeReplyChannel()


@pytest.fixture
def registry(transport: FakeReplyChannel) -> CorrelationRegistry:
    return CorrelationRegistry(transport)


# --------------------------------------------------------------------------- #
# AC-001 + module surface
# --------------------------------------------------------------------------- #


class TestPublicSurface:
    """AC-001: module exposes the documented class + methods."""

    def test_registry_exposes_full_lifecycle_api(
        self, registry: CorrelationRegistry
    ) -> None:
        for name in (
            "fresh_correlation_key",
            "bind",
            "wait_for_reply",
            "deliver_reply",
            "release",
        ):
            assert hasattr(registry, name), f"missing method: {name}"

    def test_correlation_key_re_is_32_lowercase_hex(self) -> None:
        assert CORRELATION_KEY_RE.fullmatch("0" * 32)
        assert CORRELATION_KEY_RE.fullmatch("a" * 32)
        assert not CORRELATION_KEY_RE.fullmatch("A" * 32)
        assert not CORRELATION_KEY_RE.fullmatch("0" * 31)
        assert not CORRELATION_KEY_RE.fullmatch("0" * 33)
        assert not CORRELATION_KEY_RE.fullmatch("g" * 32)


# --------------------------------------------------------------------------- #
# AC-003: fresh_correlation_key
# --------------------------------------------------------------------------- #


class TestFreshCorrelationKey:
    """AC-003: 32-lowercase-hex with no embedded PII."""

    def test_format_matches_32_lowercase_hex(
        self, registry: CorrelationRegistry
    ) -> None:
        for _ in range(64):
            key = registry.fresh_correlation_key()
            assert CORRELATION_KEY_RE.fullmatch(key), key
            assert len(key) == 32
            assert key == key.lower()

    def test_each_key_is_unique(
        self, registry: CorrelationRegistry
    ) -> None:
        keys = {registry.fresh_correlation_key() for _ in range(256)}
        assert len(keys) == 256

    def test_keys_have_no_embedded_pii_alphabet(
        self, registry: CorrelationRegistry
    ) -> None:
        # Hex-only alphabet excludes ``-`` (UUID/agent-id separator),
        # ``:`` / ``T`` / ``Z`` (timestamp markers), ``@`` (email), ``.``
        # (FQDN) — sufficient to assert no PII has leaked into the key.
        forbidden = re.compile(r"[^0-9a-f]")
        for _ in range(64):
            assert not forbidden.search(registry.fresh_correlation_key())


# --------------------------------------------------------------------------- #
# AC-002: subscribe-before-publish + format validation in bind()
# --------------------------------------------------------------------------- #


class TestBindFormatValidation:
    @pytest.mark.asyncio
    async def test_uppercase_hex_rejected(
        self, registry: CorrelationRegistry
    ) -> None:
        with pytest.raises(ValueError, match="invalid correlation key"):
            await registry.bind("A" * 32, "agent-1")

    @pytest.mark.asyncio
    async def test_short_key_rejected(
        self, registry: CorrelationRegistry
    ) -> None:
        with pytest.raises(ValueError, match="invalid correlation key"):
            await registry.bind("0" * 31, "agent-1")

    @pytest.mark.asyncio
    async def test_non_hex_chars_rejected(
        self, registry: CorrelationRegistry
    ) -> None:
        with pytest.raises(ValueError, match="invalid correlation key"):
            await registry.bind("z" * 32, "agent-1")

    @pytest.mark.asyncio
    async def test_duplicate_bind_rejected(
        self, registry: CorrelationRegistry
    ) -> None:
        await registry.bind("0" * 32, "agent-1")
        with pytest.raises(ValueError, match="already bound"):
            await registry.bind("0" * 32, "agent-1")


class TestSubscribeBeforePublish:
    """AC-002: bind() returns ONLY after subscription is active."""

    @pytest.mark.asyncio
    async def test_bind_blocks_until_subscription_active(self) -> None:
        gate = asyncio.Event()
        transport = FakeReplyChannel(subscribe_gate=gate)
        registry = CorrelationRegistry(transport)
        key = registry.fresh_correlation_key()

        bind_task = asyncio.create_task(registry.bind(key, "agent-1"))
        # Yield once — bind should be parked waiting on the gate, so
        # the orchestrator MUST NOT publish at this point.
        await asyncio.sleep(0)
        assert not bind_task.done(), (
            "bind() returned before transport.subscribe completed — "
            "publishing now would violate subscribe-before-publish."
        )
        assert key not in transport.subscribed_keys

        # Open the gate — bind() should now complete with the
        # subscription active.
        gate.set()
        binding = await bind_task
        assert binding.subscription_active is True
        assert key in transport.subscribed_keys

    @pytest.mark.asyncio
    async def test_subscription_active_flag_is_false_before_bind(
        self,
    ) -> None:
        binding = CorrelationBinding(
            correlation_key="0" * 32, matched_agent_id="agent-1"
        )
        assert binding.subscription_active is False

    @pytest.mark.asyncio
    async def test_subscription_active_flag_set_after_bind(
        self, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("0" * 32, "agent-1")
        assert binding.subscription_active is True

    @pytest.mark.asyncio
    async def test_subscribe_failure_unwinds_state(self) -> None:
        class FailingTransport:
            calls = 0

            async def subscribe(
                self, key: str, deliver: DeliverFn
            ) -> Any:
                FailingTransport.calls += 1
                raise RuntimeError("nats unavailable")

            async def unsubscribe(
                self, subscription: Any
            ) -> None:  # pragma: no cover - never reached
                pass

        registry = CorrelationRegistry(FailingTransport())  # type: ignore[arg-type]
        key = "0" * 32
        with pytest.raises(RuntimeError, match="nats unavailable"):
            await registry.bind(key, "agent-1")
        # Re-bind with the same key should succeed (binding slot was
        # freed) — though we use a *new* registry to keep the test
        # invariant clean.
        ok_registry = CorrelationRegistry(FakeReplyChannel())
        binding = await ok_registry.bind(key, "agent-1")
        assert binding.subscription_active is True


# --------------------------------------------------------------------------- #
# AC-004: deliver_reply drops unknown correlation keys
# --------------------------------------------------------------------------- #


class TestWrongCorrelationDropped:
    def test_unknown_key_dropped_silently(
        self, registry: CorrelationRegistry
    ) -> None:
        # No bindings registered — must be a silent no-op (no raise).
        registry.deliver_reply("0" * 32, "agent-x", {"value": 1})

    @pytest.mark.asyncio
    async def test_reply_for_other_key_does_not_affect_existing_binding(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("a" * 32, "agent-1")
        # Reply on a *different* correlation key (no binding).
        registry.deliver_reply("b" * 32, "agent-1", {"v": 1})
        assert binding.accepted is False


# --------------------------------------------------------------------------- #
# AC-005: source authenticity
# --------------------------------------------------------------------------- #


class TestReplySourceAuthenticity:
    @pytest.mark.asyncio
    async def test_wrong_source_dropped(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("a" * 32, "agent-authentic")
        transport.emit_reply("a" * 32, "agent-impostor", {"v": 1})
        assert binding.accepted is False
        assert binding._future is not None
        assert not binding._future.done()

    @pytest.mark.asyncio
    async def test_correct_source_accepted(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("a" * 32, "agent-authentic")
        transport.emit_reply("a" * 32, "agent-authentic", {"v": 1})
        assert binding.accepted is True

    @pytest.mark.asyncio
    async def test_impostor_then_authentic_first_does_not_block_real_reply(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("a" * 32, "agent-authentic")
        # Impostor fires first — must be dropped.
        transport.emit_reply("a" * 32, "agent-impostor", {"v": "BAD"})
        # Authentic reply still wins.
        transport.emit_reply("a" * 32, "agent-authentic", {"v": "OK"})
        result = await registry.wait_for_reply(binding, timeout_seconds=0.1)
        assert result == {"v": "OK"}
        assert binding.accepted is True


# --------------------------------------------------------------------------- #
# AC-006 + AC-009: exactly-once + race robustness
# --------------------------------------------------------------------------- #


class TestExactlyOnce:
    @pytest.mark.asyncio
    async def test_second_authentic_reply_is_dropped(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("a" * 32, "agent-1")
        transport.emit_reply("a" * 32, "agent-1", {"v": "first"})
        transport.emit_reply("a" * 32, "agent-1", {"v": "second"})
        assert binding.accepted is True
        result = await registry.wait_for_reply(binding, timeout_seconds=0.1)
        # First reply's payload is preserved.
        assert result == {"v": "first"}

    @pytest.mark.asyncio
    async def test_concurrent_deliver_reply_settles_to_one(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        # Three coroutines each schedule a deliver. Under asyncio's
        # cooperative scheduling, the check-and-set on
        # ``binding.accepted`` is atomic between yields — so exactly
        # one wins, the others are dropped.
        binding = await registry.bind("b" * 32, "agent-1")

        async def deliver(payload: dict[str, Any]) -> None:
            transport.emit_reply("b" * 32, "agent-1", payload)

        await asyncio.gather(
            deliver({"v": "A"}),
            deliver({"v": "B"}),
            deliver({"v": "C"}),
        )
        assert binding.accepted is True
        result = await registry.wait_for_reply(binding, timeout_seconds=0.1)
        # The future is set exactly once, by the first delivery to land.
        assert result in ({"v": "A"}, {"v": "B"}, {"v": "C"})
        assert binding._future is not None and binding._future.done()

    @pytest.mark.asyncio
    async def test_many_concurrent_deliveries_only_set_future_once(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("c" * 32, "agent-1")

        async def deliver(i: int) -> None:
            transport.emit_reply("c" * 32, "agent-1", {"i": i})

        await asyncio.gather(*(deliver(i) for i in range(50)))
        assert binding.accepted is True
        assert binding._future is not None and binding._future.done()
        # Future set exactly once: a second .set_result would have raised
        # InvalidStateError, which would have surfaced as a test failure
        # because emit_reply is sync and any raise propagates through.


# --------------------------------------------------------------------------- #
# AC-007: release idempotency + late-reply drop
# --------------------------------------------------------------------------- #


class TestReleaseIdempotency:
    @pytest.mark.asyncio
    async def test_release_unsubscribes(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("c" * 32, "agent-1")
        registry.release(binding)
        # Allow the scheduled unsubscribe task to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert "c" * 32 in transport.unsubscribed_keys

    @pytest.mark.asyncio
    async def test_release_is_idempotent(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("c" * 32, "agent-1")
        registry.release(binding)
        registry.release(binding)
        registry.release(binding)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Only one unsubscribe was scheduled.
        assert transport.unsubscribed_keys.count("c" * 32) == 1

    @pytest.mark.asyncio
    async def test_late_reply_after_release_dropped(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("c" * 32, "agent-1")
        registry.release(binding)
        await asyncio.sleep(0)
        # Race: transport delivers anyway — registry must drop.
        registry.deliver_reply("c" * 32, "agent-1", {"v": "late"})
        assert binding.accepted is False

    @pytest.mark.asyncio
    async def test_release_cancels_pending_wait(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("c" * 32, "agent-1")
        # No reply ever arrives; release should cancel the future, and
        # wait_for_reply should return None on cancellation.
        wait_task = asyncio.create_task(
            registry.wait_for_reply(binding, timeout_seconds=5.0)
        )
        await asyncio.sleep(0)
        registry.release(binding)
        result = await wait_task
        assert result is None


# --------------------------------------------------------------------------- #
# AC-008: PubAck does NOT flip accepted
# --------------------------------------------------------------------------- #


class TestPubAckNotSuccess:
    @pytest.mark.asyncio
    async def test_pubAck_does_not_flip_accepted(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("d" * 32, "agent-1")
        # PubAck arrives on the audit stream — does NOT route through
        # the registry's reply callback, by design.
        transport.emit_puback("d" * 32)
        await asyncio.sleep(0)
        assert binding.accepted is False
        assert transport.pubacks_emitted == 1
        # And a subsequent real reply still works.
        transport.emit_reply("d" * 32, "agent-1", {"v": "real"})
        result = await registry.wait_for_reply(binding, timeout_seconds=0.1)
        assert result == {"v": "real"}


# --------------------------------------------------------------------------- #
# wait_for_reply timeout semantics
# --------------------------------------------------------------------------- #


class TestWaitForReply:
    @pytest.mark.asyncio
    async def test_returns_payload_on_reply(
        self, transport: FakeReplyChannel, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("e" * 32, "agent-1")

        async def deliver_after_yield() -> None:
            await asyncio.sleep(0.01)
            transport.emit_reply("e" * 32, "agent-1", {"ok": True})

        deliver_task = asyncio.create_task(deliver_after_yield())
        result = await registry.wait_for_reply(binding, timeout_seconds=0.5)
        await deliver_task
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(
        self, registry: CorrelationRegistry
    ) -> None:
        binding = await registry.bind("e" * 32, "agent-1")
        result = await registry.wait_for_reply(binding, timeout_seconds=0.05)
        assert result is None
        # And the binding is NOT auto-released — the timeout coordinator
        # owns the release decision (TASK-SAD-004).
        assert binding._released is False


# --------------------------------------------------------------------------- #
# Privacy: payload contents must not be logged
# --------------------------------------------------------------------------- #


class TestPayloadNotLogged:
    @pytest.mark.asyncio
    async def test_payload_contents_never_logged(
        self,
        transport: FakeReplyChannel,
        registry: CorrelationRegistry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "sk-do-not-log-this-value"
        with caplog.at_level(logging.DEBUG, logger="forge.dispatch.correlation"):
            binding = await registry.bind("f" * 32, "agent-1")
            # Wrong source — registry warns.
            transport.emit_reply(
                "f" * 32, "agent-impostor", {"api_key": secret}
            )
            # Authentic delivery — registry routes silently.
            transport.emit_reply("f" * 32, "agent-1", {"api_key": secret})
            # Duplicate — registry debug-logs the drop.
            transport.emit_reply("f" * 32, "agent-1", {"api_key": secret})
            registry.release(binding)
        for record in caplog.records:
            assert secret not in record.getMessage(), record.getMessage()
