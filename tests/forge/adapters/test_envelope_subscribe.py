"""Tests for the envelope-aware subscribe adapter (TASK-JNB-109).

THE test-shape rule this file exists to enforce: the fake client mimics the
PRODUCTION ``nats.aio.client.Client`` signature —
``subscribe(subject, queue="", cb=None)`` delivering raw ``Msg`` objects —
NOT the consumer's wished-for envelope surface. Consumer-shaped fakes are
exactly how the raw-client defect stayed green through TASK-JNB-101 (build
gate reply path) and the TASK-MP-012 fleet-watcher finding.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.nats.envelope_subscribe import EnvelopeSubscribeClient


@dataclass
class _RawMsg:
    """Shape of nats.aio.msg.Msg as consumers see it."""

    data: bytes


class _RawSubscription:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class RawNatsClientFake:
    """Signature-faithful fake of nats.aio.client.Client.subscribe.

    A positional second argument binds to ``queue`` (a str) exactly like
    nats-py — passing a callable there raises TypeError, which is the
    production failure mode this adapter exists to prevent.
    """

    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[Any], Awaitable[None]]] = {}
        self.subscriptions: list[str] = []

    async def subscribe(
        self,
        subject: str,
        queue: str = "",
        cb: Callable[[Any], Awaitable[None]] | None = None,
    ) -> _RawSubscription:
        if not isinstance(queue, str):
            # nats-py does string ops on queue; a callable lands here when
            # a consumer calls subscribe(subject, callback) positionally.
            raise TypeError("queue must be a str (callback bound to queue?)")
        if cb is None:
            raise TypeError("cb is required for async subscriptions")
        self.subscriptions.append(subject)
        self.callbacks[subject] = cb
        return _RawSubscription()

    async def deliver(self, subject: str, body: bytes) -> None:
        await self.callbacks[subject](_RawMsg(data=body))


def _response_envelope() -> bytes:
    payload = ApprovalResponsePayload(
        request_id="plan-x:product_docs:0", decision="approve", decided_by="rich"
    )
    return (
        MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id="x",
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode("utf-8")
    )


class TestEnvelopeSubscribeClient:
    @pytest.mark.asyncio
    async def test_raw_positional_subscribe_would_fail_without_adapter(self) -> None:
        """Pin the defect: consumer-style subscribe on the raw client raises."""
        raw = RawNatsClientFake()

        async def consumer_callback(envelope: Any) -> None:  # pragma: no cover
            pass

        with pytest.raises(TypeError):
            await raw.subscribe("agents.approval.forge.b1.response", consumer_callback)

    @pytest.mark.asyncio
    async def test_adapter_delivers_parsed_envelopes_from_raw_client(self) -> None:
        raw = RawNatsClientFake()
        adapter = EnvelopeSubscribeClient(raw)
        received: list[MessageEnvelope] = []

        async def consumer_callback(envelope: MessageEnvelope) -> None:
            received.append(envelope)

        sub = await adapter.subscribe(
            "agents.approval.forge.b1.response", consumer_callback
        )
        await raw.deliver("agents.approval.forge.b1.response", _response_envelope())

        assert len(received) == 1
        assert isinstance(received[0], MessageEnvelope)
        assert received[0].payload["decision"] == "approve"
        assert hasattr(sub, "unsubscribe")

    @pytest.mark.asyncio
    async def test_malformed_payload_is_dropped_not_raised(self) -> None:
        raw = RawNatsClientFake()
        adapter = EnvelopeSubscribeClient(raw)
        received: list[Any] = []

        async def consumer_callback(envelope: Any) -> None:  # pragma: no cover
            received.append(envelope)

        await adapter.subscribe("t.1", consumer_callback)
        await raw.deliver("t.1", b"not json at all")

        assert received == []

    @pytest.mark.asyncio
    async def test_armed_event_fires_on_active_subscription(self) -> None:
        raw = RawNatsClientFake()
        armed = asyncio.Event()
        adapter = EnvelopeSubscribeClient(raw, armed)

        async def consumer_callback(envelope: Any) -> None:  # pragma: no cover
            pass

        assert not armed.is_set()
        await adapter.subscribe("t.2", consumer_callback)
        assert armed.is_set()


class TestBuildGateReplyPathOverRawClient:
    """End-to-end JNB-109 pin: the production-composed ApprovalSubscriber
    resolves a jarvis-shaped response arriving on a RAW-signature client."""

    @pytest.mark.asyncio
    async def test_await_response_resolves_through_gate_parts(self) -> None:
        from forge.cli._serve_deps_gating import build_approval_gate_parts
        from forge.config.models import ForgeConfig

        raw = RawNatsClientFake()
        raw.published: list[Any] = []  # type: ignore[attr-defined]

        async def publish(subject: str, body: bytes) -> None:
            raw.published.append((subject, body))

        raw.publish = publish  # type: ignore[attr-defined]

        config = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        parts = build_approval_gate_parts(raw, config)

        wait = asyncio.create_task(
            parts.subscriber.await_response(
                "b1", stage_label="s", attempt_count=0, timeout_seconds=5
            )
        )
        # Let the subscription arm, then deliver the response on the wire.
        for _ in range(20):
            await asyncio.sleep(0)
            if raw.subscriptions:
                break
        assert raw.subscriptions == ["agents.approval.forge.b1.response"]

        await raw.deliver("agents.approval.forge.b1.response", _response_envelope())
        response = await wait

        assert response is not None
        assert response.decision == "approve"
        assert response.decided_by == "rich"
