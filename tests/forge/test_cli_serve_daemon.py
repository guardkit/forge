"""Tests for the ``forge serve`` daemon body (TASK-F009-003 + TASK-FW10-001).

Each ``Test*`` class maps to one acceptance criterion of either
TASK-F009-003 (original daemon body) or TASK-FW10-001 (seam refactor +
``max_ack_pending=1`` + paired reconcile_on_boot wiring) so the
criterion → verifier mapping stays explicit (per the project's testing
rules — AAA pattern, descriptive names, AC traceability).

Production collaborators (NATS connect, JetStream subscribe, dispatch)
are stubbed with ``unittest.mock.AsyncMock`` and small in-process fakes.
The only real external library exercised here is ``nats-py``'s
``ConsumerConfig`` / ``AckPolicy`` enums for shape verification.

TASK-FW10-001 seam contract:

* ``DispatchFn`` is ``Callable[[_MsgLike], Awaitable[None]]``. All
  monkey-patched ``dispatch_payload`` fakes accept the whole message,
  not just its bytes payload.
* ``_process_message`` does NOT ack on the success path — the
  dispatcher (or the state machine) owns terminal-only ack.
* ``_process_message`` DOES ack on the ``except Exception`` failure
  path — releasing the durable's single ack slot keeps the daemon
  available (E3.1).
* The pull-subscribe ``ConsumerConfig`` sets ``max_ack_pending=1`` by
  default (the configured build limit; see
  ``tests/forge/test_concurrent_build_consumer.py`` for other values).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from pydantic import ValidationError

from forge.cli import _serve_daemon
from forge.cli._serve_config import (
    DEFAULT_DURABLE_NAME,
    DEFAULT_NATS_URL,
    ServeConfig,
)
from forge.cli._serve_daemon import (
    BUILD_QUEUED_SUBJECT_FILTER,
    PIPELINE_STREAM_NAME,
    SHUTDOWN_TIMEOUT_SECONDS,
    run_daemon,
)
from forge.cli._serve_state import SubscriptionState


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Minimal stand-in for ``nats.aio.msg.Msg``.

    Carries a payload as bytes and an ``ack`` async method that records
    whether ack was called — enough to verify the at-most-once / E2.1
    crash-redelivery semantics.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSubscription:
    """Pull subscription whose ``fetch()`` returns batches from a queue.

    Returning an empty list (or raising ``asyncio.TimeoutError``) lets
    the daemon's main loop poll without messages so we can assert state
    transitions independently of message delivery.
    """

    def __init__(self) -> None:
        self.batches: list[list[_FakeMsg]] = []
        self.fetch_calls = 0
        self.unsubscribed = False
        # When ``raise_on_fetch`` is set the next ``fetch`` raises it once
        # — used to model broker-loss → reconnect (D3 scenario).
        self.raise_on_fetch: BaseException | None = None

    async def fetch(self, batch: int = 1, timeout: float = 1.0) -> list[_FakeMsg]:
        self.fetch_calls += 1
        if self.raise_on_fetch is not None:
            err = self.raise_on_fetch
            self.raise_on_fetch = None
            raise err
        if self.batches:
            return self.batches.pop(0)
        # Mimic nats-py's "no messages" behaviour: TimeoutError.
        raise asyncio.TimeoutError()

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJetStream:
    """Captures ``pull_subscribe`` arguments and yields a ``_FakeSubscription``."""

    def __init__(self, sub: _FakeSubscription) -> None:
        self._sub = sub
        self.pull_subscribe_kwargs: dict[str, Any] | None = None

    async def consumer_info(self, stream: str, durable: str) -> Any:
        # The daemon reads the live durable before binding so it can apply
        # the configured build limit. "Not found" means pull_subscribe
        # creates it, which is what these tests model.
        from nats.js.errors import NotFoundError

        raise NotFoundError()

    async def pull_subscribe(self, **kwargs: Any) -> _FakeSubscription:
        self.pull_subscribe_kwargs = kwargs
        return self._sub


class _FakeNatsClient:
    """Fake ``nats`` client returning a fake JetStream context."""

    def __init__(self, sub: _FakeSubscription | None = None) -> None:
        self._js = _FakeJetStream(sub or _FakeSubscription())
        self.closed = False

    def jetstream(self) -> _FakeJetStream:
        return self._js

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sub() -> _FakeSubscription:
    return _FakeSubscription()


@pytest.fixture
def fake_client(fake_sub: _FakeSubscription) -> _FakeNatsClient:
    return _FakeNatsClient(fake_sub)


@pytest.fixture
def patched_seams(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: _FakeNatsClient,
) -> _FakeNatsClient:
    """Bind the module-level NATS connect seam to ``fake_client``."""

    async def _connect(url: str) -> Any:
        return fake_client

    monkeypatch.setattr(_serve_daemon, "nats_connect", _connect)
    return fake_client


async def _drive_daemon(
    config: ServeConfig,
    state: SubscriptionState,
    *,
    until: callable,
    timeout: float = 2.0,
) -> None:
    """Run the daemon until ``until()`` returns truthy then signal stop.

    Spawns ``run_daemon`` as a task, polls ``until`` between asyncio
    ticks, sets the daemon's stop event, and awaits clean exit. Returns
    on success; raises ``asyncio.TimeoutError`` on stuck daemons.
    """
    task = asyncio.create_task(run_daemon(config, state))

    async def _waiter() -> None:
        while not until():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(_waiter(), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseException)):
            await task
        raise

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Seam test (TASK-F009-003 §Seam Tests) — durable name contract with T1
# ---------------------------------------------------------------------------


class TestDurableNameContract:
    """Seam: ``DEFAULT_DURABLE_NAME == 'forge-serve'`` exactly (case-sensitive)."""

    @pytest.mark.seam
    @pytest.mark.integration_contract("JETSTREAM_DURABLE_NAME")
    def test_jetstream_durable_name_format(self) -> None:
        from forge.cli.serve import DEFAULT_DURABLE_NAME as exported

        assert exported, "DEFAULT_DURABLE_NAME must not be empty"
        assert exported == "forge-serve", (
            f"Expected exact 'forge-serve' (case-sensitive), got: {exported!r}"
        )


# ---------------------------------------------------------------------------
# AC-001: forge serve starts and stays running until SIGTERM
# ---------------------------------------------------------------------------


class TestDaemonStaysRunningUntilStop:
    """AC-001: Daemon runs indefinitely until cancelled / signalled."""

    @pytest.mark.asyncio
    async def test_daemon_runs_until_cancelled(
        self,
        patched_seams: _FakeNatsClient,
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config,
            state,
            until=lambda: state.live,
            timeout=2.0,
        )
        # Reaching this point means the daemon was alive until we cancelled.
        # ``state.live`` was True at least once (driver waited for it).
        assert patched_seams.closed is True


# ---------------------------------------------------------------------------
# AC-002: Daemon attaches the durable consumer named ``forge-serve``
# on ``pipeline.build-queued.*`` (Contract C; ASSUM-006).
# ---------------------------------------------------------------------------


class TestDurableConsumerAttach:
    """AC-002: Pull-subscribe arguments match Contract C exactly."""

    @pytest.mark.asyncio
    async def test_pull_subscribe_uses_forge_serve_durable(
        self,
        patched_seams: _FakeNatsClient,
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config, state, until=lambda: state.live, timeout=2.0,
        )

        kwargs = patched_seams.jetstream().pull_subscribe_kwargs
        assert kwargs is not None
        # Durable name MUST be exactly 'forge-serve' (case-sensitive).
        assert kwargs["durable"] == "forge-serve"
        assert kwargs["durable"] == DEFAULT_DURABLE_NAME
        # Subject filter is the build-queued wildcard.
        assert kwargs["subject"] == "pipeline.build-queued.*"
        assert kwargs["subject"] == BUILD_QUEUED_SUBJECT_FILTER
        # Stream is shared with the existing pipeline consumer.
        assert kwargs["stream"] == "PIPELINE"
        assert kwargs["stream"] == PIPELINE_STREAM_NAME

    @pytest.mark.asyncio
    async def test_consumer_config_uses_explicit_ack_and_filter(
        self,
        patched_seams: _FakeNatsClient,
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config, state, until=lambda: state.live, timeout=2.0,
        )

        kwargs = patched_seams.jetstream().pull_subscribe_kwargs
        assert kwargs is not None
        cfg = kwargs.get("config")
        assert isinstance(cfg, ConsumerConfig)
        # Explicit ack — required for at-least-once + work-queue semantics.
        assert cfg.ack_policy == AckPolicy.EXPLICIT
        assert cfg.deliver_policy == DeliverPolicy.ALL
        assert cfg.durable_name == "forge-serve"
        assert cfg.filter_subject == "pipeline.build-queued.*"


# ---------------------------------------------------------------------------
# AC-003 (D2): Two replicas share the durable; semantics are work-queue.
# Contract: each replica calls pull_subscribe with the SAME durable name.
# ---------------------------------------------------------------------------


class TestSharedDurableForMultiReplica:
    """AC-003 (D2): each replica binds the same durable, not a per-replica name."""

    @pytest.mark.asyncio
    async def test_two_replicas_share_durable_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Each "replica" gets its own client + recorder.
        clients: list[_FakeNatsClient] = []

        async def _connect(url: str) -> Any:
            client = _FakeNatsClient()
            clients.append(client)
            return client

        monkeypatch.setattr(_serve_daemon, "nats_connect", _connect)

        async def _run_one() -> None:
            config = ServeConfig()
            state = SubscriptionState()
            await _drive_daemon(
                config, state, until=lambda: state.live, timeout=2.0,
            )

        # Run sequentially (same effect — we assert against the recorded
        # subscribe kwargs, not message-fanout behaviour, which is the
        # broker's responsibility).
        await _run_one()
        await _run_one()

        assert len(clients) == 2
        durables = {
            c.jetstream().pull_subscribe_kwargs["durable"]
            for c in clients
            if c.jetstream().pull_subscribe_kwargs
        }
        # Both replicas share *one* durable name — the heart of D2.
        assert durables == {"forge-serve"}


# ---------------------------------------------------------------------------
# AC-004 (D3): Broker-outage recovery — state.live flips False then True
# ---------------------------------------------------------------------------


class TestBrokerOutageRecovery:
    """AC-004: state.live flips False on broker loss, True on reconnect."""

    @pytest.mark.asyncio
    async def test_state_clears_on_fetch_error_then_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First call: a sub that raises on fetch (broker loss).
        # Second call: a healthy sub that yields nothing (broker back).
        sub_bad = _FakeSubscription()
        sub_bad.raise_on_fetch = ConnectionError("broker gone")
        client_bad = _FakeNatsClient(sub_bad)

        sub_good = _FakeSubscription()
        client_good = _FakeNatsClient(sub_good)

        seq = iter([client_bad, client_good])

        async def _connect(url: str) -> Any:
            return next(seq)

        monkeypatch.setattr(_serve_daemon, "nats_connect", _connect)
        monkeypatch.setattr(
            _serve_daemon, "RECONNECT_INITIAL_BACKOFF", 0.05
        )
        monkeypatch.setattr(_serve_daemon, "RECONNECT_MAX_BACKOFF", 0.05)

        config = ServeConfig()
        state = SubscriptionState()

        # Wait for second attach (state goes False then True again).
        await _drive_daemon(
            config,
            state,
            until=lambda: client_good.jetstream().pull_subscribe_kwargs is not None,
            timeout=2.0,
        )
        assert client_bad.closed is True


# ---------------------------------------------------------------------------
# AC-005 (E2.1): On crash before ack, message stays pending.
# We cannot directly test "process crash"; instead we assert that the
# daemon does NOT ack a message whose dispatch is interrupted by stop.
# ---------------------------------------------------------------------------


class TestUnackedOnCrash:
    """AC-005 (E2.1): unacked messages survive abrupt stop for redelivery."""

    @pytest.mark.asyncio
    async def test_message_not_acked_when_dispatch_blocks_through_cancel(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Subscription yields one message immediately.
        msg = _FakeMsg(b"{}")
        sub = _FakeSubscription()
        sub.batches.append([msg])
        client = _FakeNatsClient(sub)

        async def _connect(url: str) -> Any:
            return client

        # Dispatch hangs forever until cancelled — simulates a long-running
        # build. When SIGTERM fires before dispatch returns, ack must NOT
        # run, so JetStream redelivers on restart.
        dispatch_started = asyncio.Event()

        async def _hang(msg: Any) -> None:
            # TASK-FW10-001 seam: receives the whole message, not bytes.
            dispatch_started.set()
            await asyncio.Event().wait()  # never returns

        monkeypatch.setattr(_serve_daemon, "nats_connect", _connect)
        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _hang)

        config = ServeConfig()
        state = SubscriptionState()
        task = asyncio.create_task(run_daemon(config, state))

        try:
            await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        finally:
            if not task.done():
                task.cancel()

        # Crash-safety property: ack did not run.
        assert msg.acked is False


# ---------------------------------------------------------------------------
# AC-006 (E3.1): Provider unavailable — only that build fails.
# Daemon stays available for subsequent messages.
# ---------------------------------------------------------------------------


class TestDispatchFailureIsolated:
    """AC-006 (E3.1): a failed dispatch does NOT take the daemon down."""

    @pytest.mark.asyncio
    async def test_dispatch_exception_acks_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        msg_bad = _FakeMsg(b"{}")
        msg_good = _FakeMsg(b"{}")
        sub = _FakeSubscription()
        sub.batches.append([msg_bad])
        sub.batches.append([msg_good])
        client = _FakeNatsClient(sub)

        async def _connect(url: str) -> Any:
            return client

        seen: list[bytes] = []

        async def _dispatch(msg: Any) -> None:
            # TASK-FW10-001 seam: receives the whole message; the
            # success path must NOT ack from inside _process_message,
            # so for the "good" branch we ack from the dispatcher to
            # mirror the production state-machine ack_callback.
            seen.append(msg.data)
            if len(seen) == 1:
                raise RuntimeError("provider unavailable")
            await msg.ack()

        monkeypatch.setattr(_serve_daemon, "nats_connect", _connect)
        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch)

        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config,
            state,
            until=lambda: msg_good.acked,
            timeout=2.0,
        )

        # The failed dispatch was still acked — releases the queue slot
        # so the daemon stays available (E3.1).
        assert msg_bad.acked is True
        assert msg_good.acked is True
        # Both payloads observed; the daemon did not crash on the first.
        assert len(seen) == 2


# ---------------------------------------------------------------------------
# AC-007: SubscriptionState.live mirrors broker connectivity
# ---------------------------------------------------------------------------


class TestSubscriptionStateLive:
    """AC-007: ``state.live`` is True after attach, False on broker loss."""

    @pytest.mark.asyncio
    async def test_state_starts_false(self) -> None:
        state = SubscriptionState()
        assert state.live is False

    @pytest.mark.asyncio
    async def test_state_flips_true_after_subscribe(
        self, patched_seams: _FakeNatsClient
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config, state, until=lambda: state.live, timeout=2.0,
        )
        # After daemon exits, live is False again — clean tear-down.
        assert state.live is False


# ---------------------------------------------------------------------------
# AC-008: SIGTERM completes within the documented 10 s budget
# ---------------------------------------------------------------------------


class TestShutdownGrace:
    """AC-008: SIGTERM grace constant is bounded at 10 s."""

    def test_shutdown_timeout_is_at_most_ten_seconds(self) -> None:
        # The constant is declarative — the daemon uses it to bound the
        # final ``client.close()`` so a hung broker cannot delay exit
        # past the AC's 10 s budget.
        assert SHUTDOWN_TIMEOUT_SECONDS <= 10.0
        assert SHUTDOWN_TIMEOUT_SECONDS > 0

    @pytest.mark.asyncio
    async def test_daemon_exits_quickly_after_cancel(
        self, patched_seams: _FakeNatsClient
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()
        task = asyncio.create_task(run_daemon(config, state))

        # Wait for live, then cancel.
        for _ in range(200):
            if state.live:
                break
            await asyncio.sleep(0.01)
        task.cancel()

        loop = asyncio.get_event_loop()
        start = loop.time()
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except asyncio.CancelledError:
            pass
        elapsed = loop.time() - start
        # The smoke bound is well under the AC's 10 s ceiling.
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# AC-009 (C5): ServeConfig refuses to construct with invalid fields.
# ---------------------------------------------------------------------------


class TestServeConfigValidationRefusal:
    """AC-009: ServeConfig validation names the offending field."""

    def test_invalid_healthz_port_raises_with_field_name(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ServeConfig(healthz_port=0)
        # Pydantic v2 includes the field name in the error message.
        assert "healthz_port" in str(exc_info.value)

    def test_empty_durable_name_raises_with_field_name(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ServeConfig(durable_name="")
        assert "durable_name" in str(exc_info.value)

    def test_extra_field_refused_by_pydantic(self) -> None:
        # ``ConfigDict(extra="forbid")`` makes typos visible at boot
        # rather than silently shadowing a default.
        with pytest.raises(ValidationError) as exc_info:
            ServeConfig(unknown_field="oops")  # type: ignore[call-arg]
        assert "unknown_field" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC-010 (lint-clean): the module imports cleanly, exports the public API.
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """AC-010: ``run_daemon`` and module constants are public-API."""

    def test_run_daemon_is_callable(self) -> None:
        assert callable(run_daemon)

    def test_module_exports_seams(self) -> None:
        # The seams are module-level so tests (and operators) can rebind
        # them — same pattern as ``forge.cli.queue.publish``.
        assert hasattr(_serve_daemon, "nats_connect")
        assert hasattr(_serve_daemon, "dispatch_payload")
        assert callable(_serve_daemon.nats_connect)
        assert callable(_serve_daemon.dispatch_payload)

    def test_constants_match_contracts(self) -> None:
        assert _serve_daemon.BUILD_QUEUED_SUBJECT_FILTER == "pipeline.build-queued.*"
        assert _serve_daemon.PIPELINE_STREAM_NAME == "PIPELINE"
        # Default constants match the T1 producers.
        assert DEFAULT_DURABLE_NAME == "forge-serve"
        assert DEFAULT_NATS_URL == "nats://127.0.0.1:4222"


# ---------------------------------------------------------------------------
# Helper-level test: dispatch helper does not raise on malformed envelope
# (buffer for E3.1 isolation property — even garbage input cannot kill
# the daemon's main loop)
# ---------------------------------------------------------------------------


class TestDefaultDispatchHandlesMalformed:
    """Defensive: the default dispatch helper never raises (TASK-FW10-001)."""

    @pytest.mark.asyncio
    async def test_default_dispatch_swallows_garbage_bytes(self) -> None:
        # ``_default_dispatch`` is the production seam — it logs warnings
        # but must NEVER propagate an exception, otherwise the daemon's
        # main loop catches it and the bad message blocks the queue.
        # TASK-FW10-001 seam: now takes a _MsgLike, not bytes; acks the
        # message itself before returning.
        bad = _FakeMsg(b"not-json")
        empty = _FakeMsg(b"")
        await _serve_daemon._default_dispatch(bad)
        await _serve_daemon._default_dispatch(empty)
        # Reaching here is the assertion: no exception escaped.
        # TASK-FW10-001: malformed envelopes are still acked so they do
        # not jam the durable's single ack slot (max_ack_pending=1).
        assert bad.acked is True
        assert empty.acked is True


# ---------------------------------------------------------------------------
# TASK-FW10-001 AC-001: DispatchFn is Callable[[_MsgLike], Awaitable[None]]
# ---------------------------------------------------------------------------


class TestDispatchFnSignature:
    """TASK-FW10-001 AC-001: ``DispatchFn`` accepts a message, not bytes."""

    def test_dispatch_fn_alias_uses_msglike(self) -> None:
        # The alias is the producer-side seam contract for TASK-FW10-007.
        # ``__args__`` exposes ``(arg_type, return_type)`` for a
        # ``Callable[[arg], ret]`` alias.
        assert hasattr(_serve_daemon, "DispatchFn")
        dispatch_fn = _serve_daemon.DispatchFn
        # Either the alias resolves to typing.Callable with __args__, or
        # it stringifies to mention ``_MsgLike``. Both are acceptable
        # contracts — typing internals vary across 3.10/3.11/3.12.
        rendered = repr(dispatch_fn)
        assert "_MsgLike" in rendered or "MsgLike" in rendered, (
            f"DispatchFn must reference _MsgLike; got {rendered!r}"
        )

    def test_default_dispatch_signature_takes_one_param_named_msg(self) -> None:
        import inspect

        sig = inspect.signature(_serve_daemon._default_dispatch)
        params = list(sig.parameters.values())
        assert len(params) == 1
        assert params[0].name == "msg"


# ---------------------------------------------------------------------------
# TASK-FW10-001 AC-002 / AC-003: ack semantics on success vs failure
# ---------------------------------------------------------------------------


class TestProcessMessageAckSemantics:
    """TASK-FW10-001 AC-002 / AC-003: success no-ack; failure acks."""

    @pytest.mark.asyncio
    async def test_success_path_does_not_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC-002: the dispatcher (or the state machine via
        # handle_message's ack_callback) owns terminal-only ack.
        # _process_message must not ack on the success path.
        msg = _FakeMsg(b"{}")

        async def _dispatch_no_ack(received: Any) -> None:
            # Mirror the production contract: dispatcher returns
            # successfully without acking; state machine would ack on
            # terminal completion.
            return None

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch_no_ack)

        await _serve_daemon._process_message(msg)

        assert msg.acked is False

    @pytest.mark.asyncio
    async def test_failure_path_acks_before_logging(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # AC-003: ``_process_message``'s ``except Exception`` path acks
        # the message before logging. The ack release the durable's
        # single ack slot so the daemon stays available.
        msg = _FakeMsg(b"{}")

        async def _dispatch_raises(received: Any) -> None:
            raise RuntimeError("dispatcher exploded")

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch_raises)

        with caplog.at_level("WARNING", logger="forge.cli._serve_daemon"):
            await _serve_daemon._process_message(msg)

        assert msg.acked is True, (
            "AC-003 violated: failure path must ack to release the "
            "max_ack_pending=1 queue slot"
        )
        # And the warning was emitted (observability is preserved).
        assert any(
            "dispatch failed" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_cancelled_error_does_not_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CancelledError mid-dispatch propagates without acking; the
        # message stays pending for redelivery (E2.1).
        msg = _FakeMsg(b"{}")

        async def _dispatch_cancels(received: Any) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch_cancels)

        with pytest.raises(asyncio.CancelledError):
            await _serve_daemon._process_message(msg)

        assert msg.acked is False


# ---------------------------------------------------------------------------
# TASK-FW10-001 AC-004: ConsumerConfig sets max_ack_pending=1
# ---------------------------------------------------------------------------


class TestMaxAckPendingIsOne:
    """TASK-FW10-001 AC-004: durable's ConsumerConfig has ``max_ack_pending=1``."""

    @pytest.mark.asyncio
    async def test_consumer_config_sets_max_ack_pending_one(
        self, patched_seams: _FakeNatsClient
    ) -> None:
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config, state, until=lambda: state.live, timeout=2.0,
        )

        kwargs = patched_seams.jetstream().pull_subscribe_kwargs
        assert kwargs is not None
        cfg = kwargs.get("config")
        assert isinstance(cfg, ConsumerConfig)
        # ADR-ARCH-014 / TASK-FW10-001 §2: strict serial processing.
        assert cfg.max_ack_pending == 1
        assert cfg.max_ack_pending == _serve_daemon.MAX_ACK_PENDING

    def test_module_constant_is_one(self) -> None:
        # The constant is the DEFAULT build limit; the live value comes from
        # pipeline.max_concurrent_builds, and _attach_consumer updates an
        # existing durable in place when it differs. The default of 1
        # keeps one build at a time.
        assert _serve_daemon.MAX_ACK_PENDING == 1


# ---------------------------------------------------------------------------
# TASK-FW10-001 AC-006: run_daemon accepts an injected client
# ---------------------------------------------------------------------------


class TestRunDaemonAcceptsInjectedClient:
    """TASK-FW10-001 AC-006: ``run_daemon(client=...)`` reuses one connection."""

    @pytest.mark.asyncio
    async def test_first_attach_uses_injected_client_no_seam_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        injected = _FakeNatsClient()
        connect_calls: list[str] = []

        async def _should_not_be_called(url: str) -> Any:
            connect_calls.append(url)
            # Return a fresh client so the test can fail clearly if it
            # is reached (instead of NoneType errors masking the bug).
            return _FakeNatsClient()

        monkeypatch.setattr(_serve_daemon, "nats_connect", _should_not_be_called)

        config = ServeConfig()
        state = SubscriptionState()

        task = asyncio.create_task(
            run_daemon(config, state, client=injected)
        )

        # Wait until the durable is bound; once `state.live` is True
        # the first attach has used the injected client.
        for _ in range(200):
            if state.live:
                break
            await asyncio.sleep(0.01)
        assert state.live is True, "daemon did not attach"
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        # The seam must NOT have been invoked — the injected client
        # was used for the first (and only) attach.
        assert connect_calls == [], (
            f"AC-006 violated: nats_connect was invoked despite an "
            f"injected client; calls={connect_calls!r}"
        )
        # The injected client was used for the JetStream subscribe.
        kwargs = injected.jetstream().pull_subscribe_kwargs
        assert kwargs is not None
        assert kwargs["durable"] == "forge-serve"

    @pytest.mark.asyncio
    async def test_no_client_falls_back_to_seam(
        self, patched_seams: _FakeNatsClient
    ) -> None:
        # Backwards compat: existing callers and tests pass no client;
        # the daemon then opens its own via the seam (legacy behaviour).
        config = ServeConfig()
        state = SubscriptionState()

        await _drive_daemon(
            config, state, until=lambda: state.live, timeout=2.0,
        )
        assert patched_seams.jetstream().pull_subscribe_kwargs is not None
