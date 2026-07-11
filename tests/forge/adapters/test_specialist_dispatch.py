"""Unit tests for :mod:`forge.adapters.nats.specialist_dispatch` (TASK-SAD-010).

Each ``Test*`` class maps to one acceptance criterion (AC) in
``tasks/design_approved/TASK-SAD-010-nats-adapter-specialist-dispatch.md``:

* AC-001 — module exposes :class:`NatsSpecialistDispatchAdapter` with
  ``subscribe_reply``, ``unsubscribe_reply``, ``publish_dispatch``.
* AC-002 — singular subject convention; subjects pass a regex.
* AC-003 — dispatch headers carry ``correlation_key``,
  ``requesting_agent_id="forge"``, ``dispatched_at`` (ISO 8601 UTC).
* AC-004 — ``subscribe_reply`` returns only after subscription is active;
  reply published immediately after subscribe-return is received.
* AC-005 — PubAck on the audit stream does NOT trigger
  ``registry.deliver_reply``.
* AC-006 — ``unsubscribe_reply`` is idempotent.
* AC-007 — ``_on_reply_received`` extracts ``source_agent_id`` from msg
  headers and forwards to ``registry.deliver_reply``; no auth here.
* AC-008 — compatibility seam: a fake NATS client mirroring
  ``tests/bdd/conftest.py:FakeNatsClient`` shape works as a drop-in.
* AC-009 — lint/format gate (CI-enforced; not asserted here).

We deliberately do **not** stand up a live NATS server; a hand-rolled
``FakeNATSClient`` mirrors the surface ``NatsSpecialistDispatchAdapter``
exercises (``subscribe`` / ``publish`` / ``flush``).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import pytest

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import CommandPayload

from forge.adapters.nats import specialist_dispatch as sd_module
from forge.adapters.nats.specialist_dispatch import (
    COMMAND_SUBJECT_TEMPLATE,
    CORRELATION_KEY_HEADER,
    DISPATCH_COMMAND_PLACEHOLDER,
    DISPATCHED_AT_HEADER,
    DispatchCommandPublisher,
    NatsSpecialistDispatchAdapter,
    REQUESTING_AGENT_HEADER,
    REQUESTING_AGENT_ID,
    RESULT_SUBJECT_TEMPLATE,
    ReplyChannel,
    SOURCE_AGENT_HEADER,
)
from forge.dispatch.correlation import CorrelationRegistry
from forge.dispatch.models import DispatchAttempt
from forge.dispatch.persistence import DispatchParameter


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    """Minimal stand-in for :class:`nats.aio.msg.Msg` used in tests."""

    subject: str
    data: bytes
    headers: dict[str, str] | None = None


@dataclass
class _FakeSubscription:
    """Stand-in for :class:`nats.aio.subscription.Subscription`.

    Records every ``unsubscribe()`` call so the adapter's idempotency
    is observable from the test side.
    """

    subject: str
    callback: Callable[[Any], Awaitable[None]]
    unsubscribe_calls: int = 0
    raise_on_unsubscribe: BaseException | None = None

    async def unsubscribe(self) -> None:
        self.unsubscribe_calls += 1
        if self.raise_on_unsubscribe is not None:
            raise self.raise_on_unsubscribe

    async def deliver(self, msg: _FakeMessage) -> None:
        """Test helper: invoke the registered callback with ``msg``."""
        await self.callback(msg)


@dataclass
class _RecordedPublish:
    subject: str
    body: bytes
    headers: dict[str, str] | None


class FakeNATSClient:
    """In-process fake mirroring the slice of :class:`nats.aio.Client` we use.

    The shape (``subscribe(subject, cb=...)`` / ``publish(subject, body,
    headers=...)`` / ``flush()``) intentionally mirrors the
    ``FakeNatsClient`` in ``tests/bdd/conftest.py`` so a future BDD-side
    extension (TASK-SAD-011) can drop this same surface in.

    Test hooks:

    * ``subscribe_gate`` — when set to an :class:`asyncio.Event`,
      ``subscribe()`` parks until the gate is opened. Used by AC-004 to
      assert that the adapter does not return from ``subscribe_reply``
      while the underlying SUB is in-flight.
    * ``publish_ack`` — value returned from ``publish()``; defaults to
      ``None`` (mirroring nats-py core publish, which returns ``None``).
      Tests that simulate JetStream PubAck set this to a sentinel.
    * ``flush_calls`` — count of ``flush()`` invocations so AC-004 can
      verify the belt-and-braces flush ran.
    """

    def __init__(self) -> None:
        self.subscriptions: list[_FakeSubscription] = []
        self.published: list[_RecordedPublish] = []
        self.subscribe_gate: asyncio.Event | None = None
        self.publish_ack: Any = None
        self.flush_calls: int = 0
        self.publish_raises: BaseException | None = None

    async def subscribe(
        self,
        subject: str,
        cb: Callable[[Any], Awaitable[None]] | None = None,
    ) -> _FakeSubscription:
        if self.subscribe_gate is not None:
            await self.subscribe_gate.wait()
        if cb is None:
            raise ValueError("FakeNATSClient.subscribe requires cb")
        sub = _FakeSubscription(subject=subject, callback=cb)
        self.subscriptions.append(sub)
        return sub

    async def publish(
        self,
        subject: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> Any:
        if self.publish_raises is not None:
            raise self.publish_raises
        self.published.append(
            _RecordedPublish(subject=subject, body=body, headers=headers)
        )
        return self.publish_ack

    async def flush(self) -> None:
        self.flush_calls += 1


class _RecordingRegistry:
    """In-memory stand-in for :class:`CorrelationRegistry.deliver_reply`.

    We only need to verify that the adapter's ``_on_reply_received``
    forwards the right tuple — the registry's own behaviour is exercised
    in ``tests/forge/dispatch/test_correlation.py``.
    """

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, dict[str, Any]]] = []

    def deliver_reply(
        self,
        correlation_key: str,
        source_agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.delivered.append((correlation_key, source_agent_id, payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nats_client() -> FakeNATSClient:
    return FakeNATSClient()


@pytest.fixture
def recording_registry() -> _RecordingRegistry:
    return _RecordingRegistry()


@pytest.fixture
def adapter(
    nats_client: FakeNATSClient,
    recording_registry: _RecordingRegistry,
) -> NatsSpecialistDispatchAdapter:
    return NatsSpecialistDispatchAdapter(
        nats_client=nats_client,
        registry=recording_registry,  # type: ignore[arg-type]
    )


@pytest.fixture
def real_registry_adapter(nats_client: FakeNATSClient) -> tuple[
    NatsSpecialistDispatchAdapter, CorrelationRegistry
]:
    """Adapter wired to a *real* :class:`CorrelationRegistry`.

    Used by AC-007 / AC-005 where we want the full end-to-end forwarding
    behaviour (including the registry's own drop logic) rather than only
    the adapter's part.
    """

    class _StubReplyChannel:
        async def subscribe(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover - unused
            return None

        async def unsubscribe(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover - unused
            return None

    registry = CorrelationRegistry(_StubReplyChannel())  # type: ignore[arg-type]
    adapter = NatsSpecialistDispatchAdapter(nats_client=nats_client, registry=registry)
    return adapter, registry


def _make_attempt(
    *,
    correlation_key: str = "0" * 32,
    matched_agent_id: str = "po-agent",
    resolution_id: str = "res-001",
    attempt_no: int = 1,
    retry_of: str | None = None,
) -> DispatchAttempt:
    return DispatchAttempt(
        resolution_id=resolution_id,
        correlation_key=correlation_key,
        matched_agent_id=matched_agent_id,
        attempt_no=attempt_no,
        retry_of=retry_of,
    )


# ---------------------------------------------------------------------------
# AC-001: public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """AC-001 — module exposes the documented adapter + protocols."""

    def test_adapter_exposes_three_lifecycle_methods(
        self, adapter: NatsSpecialistDispatchAdapter
    ) -> None:
        for name in ("subscribe_reply", "unsubscribe_reply", "publish_dispatch"):
            method = getattr(adapter, name, None)
            assert method is not None, f"missing method: {name}"
            assert asyncio.iscoroutinefunction(method), (
                f"{name!r} must be async"
            )

    def test_protocols_are_exported(self) -> None:
        # Both Protocols are exported so the wiring layer can declare
        # the dependency direction explicitly without importing the
        # concrete adapter.
        assert ReplyChannel is not None
        assert DispatchCommandPublisher is not None

    def test_subject_constants_are_exported(self) -> None:
        assert COMMAND_SUBJECT_TEMPLATE == "agents.command.{agent_id}"
        assert RESULT_SUBJECT_TEMPLATE == (
            "agents.result.{agent_id}.{correlation_key}"
        )

    def test_header_constants_are_exported(self) -> None:
        assert CORRELATION_KEY_HEADER == "correlation_key"
        assert REQUESTING_AGENT_HEADER == "requesting_agent_id"
        assert DISPATCHED_AT_HEADER == "dispatched_at"
        assert SOURCE_AGENT_HEADER == "source_agent_id"
        assert REQUESTING_AGENT_ID == "forge"


# ---------------------------------------------------------------------------
# AC-002: singular subject convention
# ---------------------------------------------------------------------------


class TestSubjectConvention:
    """AC-002 — singular ``agents.command`` / ``agents.result`` convention."""

    @pytest.mark.asyncio
    async def test_subscribe_reply_uses_singular_result_subject(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        assert len(nats_client.subscriptions) == 1
        last = nats_client.subscriptions[-1]
        # Verify against the regex from the seam-test stamp.
        assert re.fullmatch(
            r"agents\.result\.[a-z0-9-]+\.[0-9a-f]{32}",
            last.subject,
        ), last.subject

    @pytest.mark.asyncio
    async def test_publish_dispatch_uses_singular_command_subject(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        attempt = _make_attempt(matched_agent_id="po-agent")
        await adapter.publish_dispatch(attempt, parameters=[])
        assert len(nats_client.published) == 1
        recorded = nats_client.published[-1]
        assert re.fullmatch(
            r"agents\.command\.[a-z0-9-]+",
            recorded.subject,
        ), recorded.subject
        assert recorded.subject == "agents.command.po-agent"

    def test_subject_helpers_compose_canonical_format(self) -> None:
        assert NatsSpecialistDispatchAdapter.command_subject_for("po") == (
            "agents.command.po"
        )
        assert NatsSpecialistDispatchAdapter.result_subject_for(
            "po", "f" * 32
        ) == ("agents.result.po." + "f" * 32)


# ---------------------------------------------------------------------------
# AC-003: dispatch headers
# ---------------------------------------------------------------------------


class TestDispatchHeaders:
    """AC-003 — headers carry correlation_key, requesting_agent_id, dispatched_at."""

    @pytest.mark.asyncio
    async def test_required_headers_present(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        attempt = _make_attempt(correlation_key="ab" * 16)
        await adapter.publish_dispatch(attempt, parameters=[])
        recorded = nats_client.published[-1]
        assert recorded.headers is not None
        for key in (
            CORRELATION_KEY_HEADER,
            REQUESTING_AGENT_HEADER,
            DISPATCHED_AT_HEADER,
        ):
            assert key in recorded.headers, (
                f"missing required header: {key}; got {list(recorded.headers)}"
            )

    @pytest.mark.asyncio
    async def test_correlation_key_header_matches_attempt(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        key = "cd" * 16
        await adapter.publish_dispatch(_make_attempt(correlation_key=key), [])
        recorded = nats_client.published[-1]
        assert recorded.headers is not None
        assert recorded.headers[CORRELATION_KEY_HEADER] == key

    @pytest.mark.asyncio
    async def test_requesting_agent_id_is_forge(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.publish_dispatch(_make_attempt(), [])
        recorded = nats_client.published[-1]
        assert recorded.headers is not None
        assert recorded.headers[REQUESTING_AGENT_HEADER] == "forge"

    @pytest.mark.asyncio
    async def test_dispatched_at_is_iso8601_utc(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.publish_dispatch(_make_attempt(), [])
        recorded = nats_client.published[-1]
        assert recorded.headers is not None
        timestamp = recorded.headers[DISPATCHED_AT_HEADER]
        # Round-trip: parse and verify it is UTC.
        parsed = datetime.fromisoformat(timestamp)
        assert parsed.tzinfo is not None, (
            f"dispatched_at must carry tz info; got {timestamp}"
        )
        # ISO 8601 with explicit UTC offset (``+00:00``) — datetime
        # normalises ``Z`` to that form on parse.
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed), timestamp

    @pytest.mark.asyncio
    async def test_wire_args_are_command_args_and_never_the_parameters(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        # M2 + M3 (DISPATCHFMT+ S2, D3): the wire ``CommandPayload.args`` is
        # EXACTLY ``command_args`` — the deployed-handler argument dict. The
        # FEAT-FORGE-003 ``parameters`` list (correlation-id + forward-context
        # audit records) is persisted upstream and NEVER serialised onto the
        # wire, so no deployed handler is fed a ``parameters`` / ``context``
        # arg it does not read, and sensitive parameter values cannot leak.
        attempt = _make_attempt(
            correlation_key="ab" * 16,
            matched_agent_id="po-agent",
            resolution_id="res-42",
            attempt_no=2,
            retry_of="res-41",
        )
        params = [
            DispatchParameter(name="correlation_id", value="ab" * 16),
            DispatchParameter(name="context", value="--context=text=charter"),
            DispatchParameter(name="api_key", value="sk-SECRET", sensitive=True),
        ]
        await adapter.publish_dispatch(
            attempt,
            params,
            command="greenfield",
            command_args={"problem_statement": "a voice-first standup bot"},
        )
        recorded = nats_client.published[-1]
        envelope = MessageEnvelope.model_validate_json(recorded.body)
        command = CommandPayload.model_validate(envelope.payload)
        # args == command_args, verbatim; the parameters blob is gone.
        assert command.args == {"problem_statement": "a voice-first standup bot"}
        assert "parameters" not in command.args
        assert "context" not in command.args
        # No parameter name/value (sensitive or not) reaches the wire body.
        body_text = recorded.body.decode("utf-8")
        assert "sk-SECRET" not in body_text
        assert "api_key" not in body_text
        assert "charter" not in body_text
        # Forge-local dispatch bookkeeping is NOT put on the wire either.
        decoded = json.loads(body_text)
        assert "resolution_id" not in decoded
        assert "attempt_no" not in decoded
        assert "retry_of" not in decoded


# ---------------------------------------------------------------------------
# M1 + M5-command (DISPATCHFMT+ S1): the wire body is a parseable nats-core
# MessageEnvelope wrapping a CommandPayload — the deployed specialist's inbound
# parse (client.subscribe_with_reply -> MessageEnvelope.model_validate_json)
# must succeed, and the router demuxes on the BODY correlation value.
# ---------------------------------------------------------------------------


class TestDispatchEnvelopeWireFormat:
    """M1/M5-command — forge publishes a valid nats-core command envelope.

    Fixtures are the REAL nats-core contract types (``MessageEnvelope`` /
    ``CommandPayload``), which discovery proved byte-identical between the
    deployed image's nats-core 0.4.0 and repo tip for these surfaces — so
    a repo-tip parse here == the deployed parse-target.
    """

    @pytest.mark.asyncio
    async def test_body_is_a_valid_nats_core_message_envelope(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        # Pre-S1 this body was a bare dict → deployed parse raised
        # "3 validation errors: source_id / event_type / payload". It must
        # now parse cleanly as a MessageEnvelope.
        await adapter.publish_dispatch(
            _make_attempt(correlation_key="ab" * 16), []
        )
        recorded = nats_client.published[-1]
        envelope = MessageEnvelope.model_validate_json(recorded.body)
        assert envelope.source_id == REQUESTING_AGENT_ID == "forge"

    @pytest.mark.asyncio
    async def test_event_type_is_command(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.publish_dispatch(_make_attempt(), [])
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        # Member exists in the deployed nats-core 0.4.0; the router's
        # step-1 gate requires event_type == COMMAND to route at all.
        assert envelope.event_type is EventType.COMMAND

    @pytest.mark.asyncio
    async def test_envelope_correlation_id_is_the_attempt_key(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        key = "ab" * 16
        await adapter.publish_dispatch(_make_attempt(correlation_key=key), [])
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        # D2: the deployed router demuxes replies by the BODY correlation
        # value, so it MUST live on the envelope (not only in headers).
        assert envelope.correlation_id == key

    @pytest.mark.asyncio
    async def test_payload_is_a_valid_command_payload(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        key = "cd" * 16
        await adapter.publish_dispatch(_make_attempt(correlation_key=key), [])
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        command = CommandPayload.model_validate(envelope.payload)
        # This call resolves no verb, so the adapter falls back to the
        # non-routing placeholder default (production always passes the
        # stage-resolved ``greenfield`` verb — see TestDispatchDeployedVerb).
        assert command.command == DISPATCH_COMMAND_PLACEHOLDER
        assert len(command.command) >= 1  # CommandPayload min_length contract
        # Correlation is also threaded onto the CommandPayload (the deployed
        # router reads cmd_payload.correlation_id first, envelope second).
        assert command.correlation_id == key

    @pytest.mark.asyncio
    async def test_headers_are_retained_for_tracing_only(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        # D2: headers may remain for tracing, but nothing in the parse-target
        # depends on them — the envelope must be self-sufficient. We assert
        # both: headers still present AND the body alone carries correlation.
        key = "ef" * 16
        await adapter.publish_dispatch(_make_attempt(correlation_key=key), [])
        recorded = nats_client.published[-1]
        assert recorded.headers is not None
        assert recorded.headers[CORRELATION_KEY_HEADER] == key
        envelope = MessageEnvelope.model_validate_json(recorded.body)
        assert envelope.correlation_id == key


# ---------------------------------------------------------------------------
# M2 + M3 (DISPATCHFMT+ S2): the wire carries the stage-resolved DEPLOYED verb
# and a dict of the deployed handler's required inputs. Fixtures are the REAL
# nats-core CommandPayload + the deployed PO command map / required-args tables
# copied verbatim from the in-container command_router.py (13-day image).
# ---------------------------------------------------------------------------


#: Deployed product-owner command map (verbatim from the in-container
#: ``command_router.py`` ``_PO_COMMAND_MAP``) — the set of verbs the deployed
#: PO router will route rather than answering "Command not supported".
_DEPLOYED_PO_COMMAND_MAP: dict[str, str] = {
    "idea": "_handle_po_idea",
    "extract": "_handle_po_extract",
    "greenfield": "_handle_po_greenfield",
    "evolve": "_handle_po_evolve",
    "impact": "_handle_po_impact",
    "scope": "_handle_po_scope",
}

#: Deployed PO required-args table (verbatim ``_PO_REQUIRED_ARGS``).
_DEPLOYED_PO_REQUIRED_ARGS: dict[str, list[str]] = {
    "idea": ["idea"],
    "extract": ["docs_path"],
    "greenfield": ["problem_statement"],
    "evolve": ["docs_path", "build_plan_path"],
    "impact": ["docs_path", "build_plan_path", "new_info"],
    "scope": ["constraint"],
}


class TestDispatchDeployedVerb:
    """M2/M3 — the wire command names a deployed verb + dict args w/ inputs."""

    @pytest.mark.asyncio
    async def test_po_greenfield_command_routes_in_deployed_map(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.publish_dispatch(
            _make_attempt(matched_agent_id="product-owner-agent"),
            [],
            command="greenfield",
            command_args={"problem_statement": "a voice-first standup bot"},
        )
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        command = CommandPayload.model_validate(envelope.payload)
        # M2: the verb resolves to a real deployed handler (not "Command not
        # supported"). The deployed router does ``command_map.get(command)``.
        assert command.command == "greenfield"
        assert _DEPLOYED_PO_COMMAND_MAP.get(command.command) == "_handle_po_greenfield"

    @pytest.mark.asyncio
    async def test_command_args_satisfy_deployed_required_args_gate(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.publish_dispatch(
            _make_attempt(matched_agent_id="product-owner-agent"),
            [],
            command="greenfield",
            command_args={"problem_statement": "a voice-first standup bot"},
        )
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        command = CommandPayload.model_validate(envelope.payload)
        # M3: args is a dict, and every deployed-required key is present with a
        # non-empty value — the router's ``_check_required_args`` finds none
        # missing (``[a for a in required if a not in args]`` is empty).
        assert isinstance(command.args, dict)
        required = _DEPLOYED_PO_REQUIRED_ARGS[command.command]
        missing = [arg for arg in required if arg not in command.args]
        assert missing == []
        assert command.args["problem_statement"] == "a voice-first standup bot"

    @pytest.mark.asyncio
    async def test_correlation_id_stays_the_attempt_key(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        key = "ab" * 16
        await adapter.publish_dispatch(
            _make_attempt(correlation_key=key),
            [],
            command="greenfield",
            command_args={"problem_statement": "x"},
        )
        envelope = MessageEnvelope.model_validate_json(
            nats_client.published[-1].body
        )
        command = CommandPayload.model_validate(envelope.payload)
        # S2 preserves the S1 correlation contract: attempt.correlation_key on
        # BOTH the CommandPayload and the envelope (the router reads
        # cmd_payload.correlation_id first, envelope.correlation_id second).
        assert command.correlation_id == key
        assert envelope.correlation_id == key


# ---------------------------------------------------------------------------
# AC-004: subscribe-before-publish — subscribe_reply blocks until SUB active
# ---------------------------------------------------------------------------


class TestSubscribeBeforePublish:
    """AC-004 — subscribe_reply returns only after subscription is active."""

    @pytest.mark.asyncio
    async def test_subscribe_reply_blocks_until_underlying_subscribe_returns(
        self, recording_registry: _RecordingRegistry
    ) -> None:
        gate = asyncio.Event()
        client = FakeNATSClient()
        client.subscribe_gate = gate
        adapter = NatsSpecialistDispatchAdapter(
            nats_client=client, registry=recording_registry  # type: ignore[arg-type]
        )

        sub_task = asyncio.create_task(
            adapter.subscribe_reply("po-agent", "a" * 32)
        )
        # Yield once — subscribe should be parked on the gate.
        await asyncio.sleep(0)
        assert not sub_task.done(), (
            "subscribe_reply returned before underlying subscribe completed "
            "— publishing now would violate subscribe-before-publish."
        )
        assert client.subscriptions == []

        # Open the gate — subscribe_reply should now finish.
        gate.set()
        await sub_task
        assert len(client.subscriptions) == 1
        # And flush ran at least once after subscribe (belt-and-braces).
        assert client.flush_calls >= 1

    @pytest.mark.asyncio
    async def test_reply_published_after_subscribe_return_is_received(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Establish the subscription synchronously (no gate).
        await adapter.subscribe_reply("po-agent", "a" * 32)
        # Immediately deliver a reply on that subscription.
        sub = nats_client.subscriptions[-1]
        msg = _FakeMessage(
            subject=sub.subject,
            data=b'{"ok": true}',
            headers={
                CORRELATION_KEY_HEADER: "a" * 32,
                SOURCE_AGENT_HEADER: "po-agent",
            },
        )
        await sub.deliver(msg)
        # The registry observed the forwarded reply — proving the
        # subscription was active end-to-end before we delivered.
        assert recording_registry.delivered == [
            ("a" * 32, "po-agent", {"ok": True})
        ]

    @pytest.mark.asyncio
    async def test_flush_failure_does_not_raise(
        self,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # A flush that raises should be swallowed — the SUB itself has
        # already been written, so the subscribe-before-publish contract
        # is upheld even without the belt-and-braces flush.
        class _FlushRaises(FakeNATSClient):
            async def flush(self) -> None:  # type: ignore[override]
                raise RuntimeError("transient flush failure")

        client = _FlushRaises()
        adapter = NatsSpecialistDispatchAdapter(
            nats_client=client, registry=recording_registry  # type: ignore[arg-type]
        )
        # Must not raise.
        await adapter.subscribe_reply("po-agent", "a" * 32)
        assert len(client.subscriptions) == 1


# ---------------------------------------------------------------------------
# AC-005: PubAck does NOT trigger registry.deliver_reply
# ---------------------------------------------------------------------------


class TestPubAckNotSuccess:
    """AC-005 — PubAck on the audit stream is observation-only."""

    @pytest.mark.asyncio
    async def test_pubAck_is_not_routed_through_registry(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Simulate a JetStream-emitted PubAck — the publish call returns
        # a sentinel object that the adapter would log at DEBUG.
        nats_client.publish_ack = object()
        attempt = _make_attempt()
        await adapter.publish_dispatch(attempt, [])
        # Critical: deliver_reply was NOT invoked just because publish
        # returned an ack. Outcome lives on the reply subscription.
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_pubAck_is_not_routed_with_real_registry(
        self,
        real_registry_adapter: tuple[
            NatsSpecialistDispatchAdapter, CorrelationRegistry
        ],
        nats_client: FakeNATSClient,
    ) -> None:
        adapter, _registry = real_registry_adapter
        nats_client.publish_ack = {"ack_id": "abc"}
        await adapter.publish_dispatch(_make_attempt(), [])
        # Nothing the registry can observe — no reply was forwarded.
        # (We assert via published list and absence of any subscription
        # delivery, which is structurally guaranteed because we never
        # invoked any subscription callback.)
        assert len(nats_client.published) == 1


# ---------------------------------------------------------------------------
# AC-006: unsubscribe_reply is idempotent
# ---------------------------------------------------------------------------


class TestUnsubscribeIdempotency:
    """AC-006 — calling unsubscribe_reply twice is safe."""

    @pytest.mark.asyncio
    async def test_unsubscribe_invokes_underlying_unsubscribe_once(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        sub = nats_client.subscriptions[-1]
        await adapter.unsubscribe_reply("a" * 32)
        assert sub.unsubscribe_calls == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_twice_is_no_op(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        sub = nats_client.subscriptions[-1]
        await adapter.unsubscribe_reply("a" * 32)
        # Second call must not raise and must not call unsubscribe again.
        await adapter.unsubscribe_reply("a" * 32)
        await adapter.unsubscribe_reply("a" * 32)
        assert sub.unsubscribe_calls == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_key_is_no_op(
        self,
        adapter: NatsSpecialistDispatchAdapter,
    ) -> None:
        # No subscribe_reply call ever made — must not raise.
        await adapter.unsubscribe_reply("z" * 32)

    @pytest.mark.asyncio
    async def test_unsubscribe_swallows_transport_error(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        sub = nats_client.subscriptions[-1]
        sub.raise_on_unsubscribe = RuntimeError("nats unreachable")
        # Must not propagate — the registry's release path is sync and
        # cannot meaningfully act on a transport-level failure.
        await adapter.unsubscribe_reply("a" * 32)
        # And the slot is still cleared so a follow-up call is a no-op.
        await adapter.unsubscribe_reply("a" * 32)


# ---------------------------------------------------------------------------
# AC-007: _on_reply_received forwards to registry; no auth here
# ---------------------------------------------------------------------------


class TestOnReplyReceived:
    """AC-007 — adapter forwards inbound replies; auth lives in registry."""

    @pytest.mark.asyncio
    async def test_forwards_correlation_key_source_and_payload(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
        recording_registry: _RecordingRegistry,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        sub = nats_client.subscriptions[-1]
        msg = _FakeMessage(
            subject=sub.subject,
            data=b'{"value": 42}',
            headers={
                CORRELATION_KEY_HEADER: "a" * 32,
                SOURCE_AGENT_HEADER: "po-agent",
            },
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == [
            ("a" * 32, "po-agent", {"value": 42})
        ]

    @pytest.mark.asyncio
    async def test_does_not_authenticate_locally(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # The adapter MUST forward whatever source_agent_id arrived in
        # the headers — even an obviously-wrong value. Authentication
        # is the registry's job (TASK-SAD-003 E.reply-source-authenticity).
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b'{"value": 1}',
            headers={
                CORRELATION_KEY_HEADER: "a" * 32,
                SOURCE_AGENT_HEADER: "imposter-agent",
            },
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == [
            ("a" * 32, "imposter-agent", {"value": 1})
        ]

    @pytest.mark.asyncio
    async def test_drops_message_with_missing_correlation_key_header(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b'{"v": 1}',
            headers={SOURCE_AGENT_HEADER: "po-agent"},
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_message_with_missing_source_agent_header(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b'{"v": 1}',
            headers={CORRELATION_KEY_HEADER: "a" * 32},
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_message_with_no_headers(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b'{"v": 1}',
            headers=None,
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_malformed_json_body(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b"this is not json {",
            headers={
                CORRELATION_KEY_HEADER: "a" * 32,
                SOURCE_AGENT_HEADER: "po-agent",
            },
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_non_object_payload(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # JSON arrays / scalars are valid JSON but not a dict.
        msg = _FakeMessage(
            subject="agents.result.po-agent." + "a" * 32,
            data=b"[1, 2, 3]",
            headers={
                CORRELATION_KEY_HEADER: "a" * 32,
                SOURCE_AGENT_HEADER: "po-agent",
            },
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_callback_never_raises_on_unexpected_error(
        self,
        adapter: NatsSpecialistDispatchAdapter,
    ) -> None:
        # A message whose ``headers`` attribute access blows up should
        # not propagate out of the callback — that would tear down the
        # subscription's task in production.
        class _BrokenMsg:
            subject = "agents.result.po-agent." + "a" * 32
            data = b"{}"

            @property
            def headers(self) -> dict[str, str]:
                raise RuntimeError("transient failure reading headers")

        await adapter._on_reply_received(_BrokenMsg())  # must not raise


# ---------------------------------------------------------------------------
# AC-008: drop-in compatibility with FakeNatsClient shape
# ---------------------------------------------------------------------------


class TestFakeNatsClientCompatibility:
    """AC-008 — adapter works against the BDD fake's shape."""

    @pytest.mark.asyncio
    async def test_adapter_uses_only_subscribe_publish_flush(
        self,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Build a deliberately-tiny client exposing only the three methods
        # the adapter is allowed to call. If the adapter ever introduces
        # a new transport call, this test will fail loudly — that's the
        # whole point of pinning the surface here.

        @dataclass
        class _MinimalClient:
            subscriptions: list[_FakeSubscription] = field(default_factory=list)
            published: list[_RecordedPublish] = field(default_factory=list)
            flush_count: int = 0

            async def subscribe(
                self,
                subject: str,
                cb: Callable[[Any], Awaitable[None]] | None = None,
            ) -> _FakeSubscription:
                assert cb is not None
                sub = _FakeSubscription(subject=subject, callback=cb)
                self.subscriptions.append(sub)
                return sub

            async def publish(
                self,
                subject: str,
                body: bytes = b"",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.published.append(
                    _RecordedPublish(
                        subject=subject, body=body, headers=headers
                    )
                )

            async def flush(self) -> None:
                self.flush_count += 1

        client = _MinimalClient()
        adapter = NatsSpecialistDispatchAdapter(
            nats_client=client, registry=recording_registry  # type: ignore[arg-type]
        )
        await adapter.subscribe_reply("po-agent", "a" * 32)
        await adapter.publish_dispatch(_make_attempt(), [])
        await adapter.unsubscribe_reply("a" * 32)
        assert len(client.subscriptions) == 1
        assert len(client.published) == 1
        # Flush is called from subscribe_reply (belt-and-braces).
        assert client.flush_count >= 1


# ---------------------------------------------------------------------------
# Seam test — CorrelationKey contract on the wire (mirrors task spec)
# ---------------------------------------------------------------------------


class TestSeamCorrelationKeyOnTheWire:
    """Seam test from TASK-SAD-010 ``Seam Tests`` section.

    Verifies the ``CorrelationKey`` contract from TASK-SAD-003 (32 lowercase
    hex chars) is preserved end-to-end through the subject suffix.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration_contract("CorrelationKey")
    async def test_dispatch_subject_format_matches_correlation_key_re(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        # Real registry to fabricate a key in the canonical format.
        registry = CorrelationRegistry(transport=_FakeTransportThatNeverYields())  # type: ignore[arg-type]
        key = registry.fresh_correlation_key()
        await adapter.subscribe_reply("po-agent", key)

        last = nats_client.subscriptions[-1]
        assert re.fullmatch(
            r"agents\.result\.[a-z0-9-]+\.[0-9a-f]{32}",
            last.subject,
        ), last.subject


class _FakeTransportThatNeverYields:
    """Stand-in that satisfies CorrelationRegistry's __init__ Protocol."""

    async def subscribe(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover
        return None

    async def unsubscribe(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Module re-export hygiene
# ---------------------------------------------------------------------------


class TestModuleExports:
    def test_all_documented_symbols_in_dunder_all(self) -> None:
        for name in (
            "NatsSpecialistDispatchAdapter",
            "ReplyChannel",
            "DispatchCommandPublisher",
            "COMMAND_SUBJECT_TEMPLATE",
            "RESULT_SUBJECT_TEMPLATE",
            "CORRELATION_KEY_HEADER",
            "REQUESTING_AGENT_HEADER",
            "DISPATCHED_AT_HEADER",
            "SOURCE_AGENT_HEADER",
            "REQUESTING_AGENT_ID",
        ):
            assert name in sd_module.__all__, (
                f"{name!r} missing from __all__"
            )
