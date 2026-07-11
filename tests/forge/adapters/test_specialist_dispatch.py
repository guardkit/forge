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
* AC-007 — ``_on_reply_received`` parses the reply BODY as a nats-core
  ``MessageEnvelope`` (the DEPLOYED specialist's shape: envelope-wrapped
  ``ResultPayload``, NO headers), reads source from ``envelope.source_id``,
  demuxes by the body correlation, and forwards to
  ``registry.deliver_reply``; no auth here (M4 + M5-reply, DISPATCHFMT+ S3).
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
from nats_core.events import CommandPayload, ResultPayload

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


# The exact success ``result`` dict the DEPLOYED specialist emits — copied
# verbatim from ``wrap_role_output`` in
# ``specialist-agent/src/specialist_agent/adapters/result_wrapper.py``
# (read in-container; the ground truth for the reply-body shape).
def _wrap_role_output_result(role_id: str = "product-owner") -> dict[str, Any]:
    return {
        "role_id": role_id,
        "coach_score": 0.82,
        "criterion_breakdown": [
            {
                "criterion": "problem-clarity",
                "score": 0.9,
                "weight": 0.5,
                "rationale": "clear problem statement",
            },
            {
                "criterion": "scope-fit",
                "score": 0.74,
                "weight": 0.5,
                "rationale": "scope is bounded",
            },
        ],
        "detection_findings": [
            {
                "pattern": "vagueness",
                "severity": "low",
                "description": "one under-specified acceptance criterion",
                "location": "AC-3",
            }
        ],
        "role_output": {"document": "the real role document body"},
    }


def _deployed_reply_bytes(
    *,
    correlation_id: str | None,
    source_id: str = "po-agent",
    command: str = "greenfield",
    success: bool = True,
    result: dict[str, Any] | None = None,
    envelope_correlation_id: str | None = "__use_body__",
) -> bytes:
    """Build the EXACT bytes the deployed specialist publishes on reply.

    Fire-and-forget branch (forge publishes without ``reply_to``): the
    router envelope-wraps a ``ResultPayload`` and publishes to
    ``agents.result.{agent_id}`` with NO headers — correlation lives in the
    body (verified against the in-container ``command_router._publish_result``
    + ``nats_core`` ``client.publish``).

    ``envelope_correlation_id`` defaults to mirroring the body value (as the
    deployed ``client.publish(correlation_id=...)`` does); pass an explicit
    value to exercise the envelope fallback independently of the payload.
    """
    payload = ResultPayload(
        command=command,
        result=result if result is not None else _wrap_role_output_result(),
        correlation_id=correlation_id,
        success=success,
    )
    env_corr = (
        correlation_id
        if envelope_correlation_id == "__use_body__"
        else envelope_correlation_id
    )
    envelope = MessageEnvelope(
        source_id=source_id,
        event_type=EventType.RESULT,
        correlation_id=env_corr,
        payload=payload.model_dump(),
    )
    return envelope.model_dump_json().encode("utf-8")


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
        # M4 (DISPATCHFMT+ S3): reply subject is the plain 3-token
        # ``agents.result.{agent_id}`` — correlation is in the body, not
        # the subject. Aligned to nats-core ``Topics.Agents.RESULT``.
        assert RESULT_SUBJECT_TEMPLATE == "agents.result.{agent_id}"

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
        # M4: 3-token reply subject, no correlation suffix.
        assert re.fullmatch(
            r"agents\.result\.[a-z0-9-]+",
            last.subject,
        ), last.subject
        assert last.subject == "agents.result.po-agent"

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
            "po"
        ) == "agents.result.po"


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
        # Immediately deliver a deployed-shape reply on that subscription
        # (envelope-wrapped ResultPayload, NO headers, body correlation).
        sub = nats_client.subscriptions[-1]
        result = _wrap_role_output_result()
        msg = _FakeMessage(
            subject=sub.subject,
            data=_deployed_reply_bytes(
                correlation_id="a" * 32, source_id="po-agent", result=result
            ),
            headers=None,
        )
        await sub.deliver(msg)
        # The registry observed the forwarded reply — proving the
        # subscription was active end-to-end before we delivered. The
        # forwarded payload is the inner ResultPayload dict.
        assert recording_registry.delivered == [
            (
                "a" * 32,
                "po-agent",
                {
                    "command": "greenfield",
                    "result": result,
                    "correlation_id": "a" * 32,
                    "success": True,
                },
            )
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
    """AC-007 — adapter parses the reply BODY and forwards; auth in registry."""

    @pytest.mark.asyncio
    async def test_forwards_deployed_shape_reply_by_body_correlation(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # The DEPLOYED specialist shape: envelope-wrapped ResultPayload,
        # NO headers, correlation in the body.
        await adapter.subscribe_reply("po-agent", "a" * 32)
        sub = nats_client.subscriptions[-1]
        result = _wrap_role_output_result()
        msg = _FakeMessage(
            subject=sub.subject,
            data=_deployed_reply_bytes(
                correlation_id="a" * 32, source_id="po-agent", result=result
            ),
            headers=None,
        )
        await adapter._on_reply_received(msg)
        # Forwarded tuple: (body correlation, envelope source, inner
        # ResultPayload dict).
        assert recording_registry.delivered == [
            (
                "a" * 32,
                "po-agent",
                {
                    "command": "greenfield",
                    "result": result,
                    "correlation_id": "a" * 32,
                    "success": True,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_source_identity_comes_from_envelope_not_headers(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Source is read from ``envelope.source_id`` (D2) — NOT from any
        # header. The adapter forwards whatever source it observed, even an
        # obviously-wrong one; authentication is the registry's job
        # (TASK-SAD-003 E.reply-source-authenticity). Bogus tracing headers
        # are present to prove they are ignored.
        msg = _FakeMessage(
            subject="agents.result.po-agent",
            data=_deployed_reply_bytes(
                correlation_id="a" * 32, source_id="imposter-agent"
            ),
            headers={SOURCE_AGENT_HEADER: "po-agent"},
        )
        await adapter._on_reply_received(msg)
        assert len(recording_registry.delivered) == 1
        corr, source, _payload = recording_registry.delivered[0]
        assert corr == "a" * 32
        assert source == "imposter-agent"

    @pytest.mark.asyncio
    async def test_demux_prefers_body_result_payload_correlation(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # ResultPayload.correlation_id wins over envelope.correlation_id.
        msg = _FakeMessage(
            subject="agents.result.po-agent",
            data=_deployed_reply_bytes(
                correlation_id="a" * 32,
                envelope_correlation_id="b" * 32,
            ),
        )
        await adapter._on_reply_received(msg)
        assert [d[0] for d in recording_registry.delivered] == ["a" * 32]

    @pytest.mark.asyncio
    async def test_falls_back_to_envelope_correlation(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # ResultPayload carries no correlation_id → demux by envelope.
        msg = _FakeMessage(
            subject="agents.result.po-agent",
            data=_deployed_reply_bytes(
                correlation_id=None,
                envelope_correlation_id="c" * 32,
            ),
        )
        await adapter._on_reply_received(msg)
        assert [d[0] for d in recording_registry.delivered] == ["c" * 32]

    @pytest.mark.asyncio
    async def test_drops_reply_with_no_correlation_anywhere(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Neither the body ResultPayload nor the envelope carries a
        # correlation — the reply cannot be demuxed and is dropped.
        msg = _FakeMessage(
            subject="agents.result.po-agent",
            data=_deployed_reply_bytes(
                correlation_id=None, envelope_correlation_id=None
            ),
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_empty_body(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        msg = _FakeMessage(
            subject="agents.result.po-agent", data=b"", headers=None
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
            subject="agents.result.po-agent",
            data=b"this is not json {",
            headers=None,
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_drops_body_that_is_not_a_message_envelope(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Valid JSON, but missing the required MessageEnvelope fields
        # (source_id / event_type / payload) — schema validation drops it.
        msg = _FakeMessage(
            subject="agents.result.po-agent",
            data=b'{"foo": "bar"}',
            headers=None,
        )
        await adapter._on_reply_received(msg)
        assert recording_registry.delivered == []

    @pytest.mark.asyncio
    async def test_callback_never_raises_on_unexpected_error(
        self,
        adapter: NatsSpecialistDispatchAdapter,
    ) -> None:
        # A message whose ``data`` attribute access blows up should not
        # propagate out of the callback — that would tear down the SHARED
        # subscription's task in production.
        class _BrokenMsg:
            subject = "agents.result.po-agent"

            @property
            def data(self) -> bytes:
                raise RuntimeError("transient failure reading data")

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
    hex chars) is preserved end-to-end. Post DISPATCHFMT+ S3 (M4) the key is
    NOT on the subject — the subject is the 3-token
    ``agents.result.{agent_id}`` and the key travels in the reply BODY, where
    the adapter demuxes it back to the awaiting correlation.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration_contract("CorrelationKey")
    async def test_reply_subject_is_3_token_and_key_demuxes_from_body(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
        recording_registry: _RecordingRegistry,
    ) -> None:
        # Real registry to fabricate a key in the canonical format.
        registry = CorrelationRegistry(transport=_FakeTransportThatNeverYields())  # type: ignore[arg-type]
        key = registry.fresh_correlation_key()
        await adapter.subscribe_reply("po-agent", key)

        # The subscribed subject is the 3-token form — no key suffix.
        last = nats_client.subscriptions[-1]
        assert re.fullmatch(r"agents\.result\.[a-z0-9-]+", last.subject), (
            last.subject
        )
        assert last.subject == "agents.result.po-agent"

        # The 32-hex key is preserved through the reply body: a deployed-
        # shape reply carrying it demuxes back to that correlation.
        await adapter._on_reply_received(
            _FakeMessage(
                subject=last.subject,
                data=_deployed_reply_bytes(
                    correlation_id=key, source_id="po-agent"
                ),
                headers=None,
            )
        )
        assert [d[0] for d in recording_registry.delivered] == [key]


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


# ---------------------------------------------------------------------------
# M4 + M5-reply (DISPATCHFMT+ S3): shared per-agent subscription + body demux
# ---------------------------------------------------------------------------


class TestSharedSubscriptionLifecycle:
    """One 3-token subscription per agent serves all concurrent dispatches."""

    @pytest.mark.asyncio
    async def test_concurrent_correlations_share_one_subscription(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        await adapter.subscribe_reply("po-agent", "b" * 32)
        # Two in-flight correlations, ONE underlying NATS subscription.
        assert len(nats_client.subscriptions) == 1
        assert nats_client.subscriptions[0].subject == "agents.result.po-agent"

    @pytest.mark.asyncio
    async def test_shared_subscription_torn_down_only_on_last_release(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        await adapter.subscribe_reply("po-agent", "b" * 32)
        sub = nats_client.subscriptions[0]

        # Releasing the first correlation must NOT tear the shared sub down —
        # the second is still in flight.
        await adapter.unsubscribe_reply("a" * 32)
        assert sub.unsubscribe_calls == 0

        # Releasing the last correlation tears it down exactly once.
        await adapter.unsubscribe_reply("b" * 32)
        assert sub.unsubscribe_calls == 1

    @pytest.mark.asyncio
    async def test_distinct_agents_get_distinct_subscriptions(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        await adapter.subscribe_reply("architect-agent", "b" * 32)
        subjects = sorted(s.subject for s in nats_client.subscriptions)
        assert subjects == [
            "agents.result.architect-agent",
            "agents.result.po-agent",
        ]

    @pytest.mark.asyncio
    async def test_duplicate_subscribe_of_same_key_is_a_no_op(
        self,
        adapter: NatsSpecialistDispatchAdapter,
        nats_client: FakeNATSClient,
    ) -> None:
        await adapter.subscribe_reply("po-agent", "a" * 32)
        await adapter.subscribe_reply("po-agent", "a" * 32)
        # Same key twice: never a second NATS subscription, and a single
        # release still tears the (single) subscription down.
        assert len(nats_client.subscriptions) == 1
        await adapter.unsubscribe_reply("a" * 32)
        assert nats_client.subscriptions[0].unsubscribe_calls == 1


def _wire_real_registry(
    nats_client: FakeNATSClient,
) -> tuple[NatsSpecialistDispatchAdapter, CorrelationRegistry]:
    """Wire a real :class:`CorrelationRegistry` to a real adapter.

    The registry is the adapter's reply sink AND the adapter is the
    registry's transport, so we forward-declare the registry, build the
    adapter against it, then point the registry's transport at the adapter.
    ``bind`` then drives ``adapter.subscribe`` → ``subscribe_reply`` on the
    real code path.
    """

    class _Deferred:
        async def subscribe(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover
            raise AssertionError("transport not wired yet")

        async def unsubscribe(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
            raise AssertionError("transport not wired yet")

    registry = CorrelationRegistry(transport=_Deferred())  # type: ignore[arg-type]
    adapter = NatsSpecialistDispatchAdapter(
        nats_client=nats_client, registry=registry
    )
    registry._transport = adapter  # type: ignore[attr-defined]
    return adapter, registry


class TestConcurrentReplyDemux:
    """End-to-end through a real registry: headerless deployed-shape replies."""

    @pytest.mark.asyncio
    async def test_headerless_reply_resolves_the_matching_future(
        self,
        nats_client: FakeNATSClient,
    ) -> None:
        adapter, registry = _wire_real_registry(nats_client)
        key = registry.fresh_correlation_key()
        binding = await registry.bind(key, "po-agent")

        result = _wrap_role_output_result()
        await nats_client.subscriptions[0].deliver(
            _FakeMessage(
                subject="agents.result.po-agent",
                data=_deployed_reply_bytes(
                    correlation_id=key, source_id="po-agent", result=result
                ),
                headers=None,
            )
        )
        payload = await registry.wait_for_reply(binding, timeout_seconds=1.0)
        # The awaiting future resolves with the inner ResultPayload dict —
        # exact deployed shape, from a reply that carried NO headers.
        assert payload == {
            "command": "greenfield",
            "result": result,
            "correlation_id": key,
            "success": True,
        }

    @pytest.mark.asyncio
    async def test_mismatched_correlation_is_dropped(
        self,
        nats_client: FakeNATSClient,
    ) -> None:
        adapter, registry = _wire_real_registry(nats_client)
        key = registry.fresh_correlation_key()
        binding = await registry.bind(key, "po-agent")

        # A reply whose body correlation matches NO in-flight binding is
        # dropped by the registry — never resolves the waiter, never crashes.
        other_key = registry.fresh_correlation_key()
        await nats_client.subscriptions[0].deliver(
            _FakeMessage(
                subject="agents.result.po-agent",
                data=_deployed_reply_bytes(
                    correlation_id=other_key, source_id="po-agent"
                ),
                headers=None,
            )
        )
        payload = await registry.wait_for_reply(binding, timeout_seconds=0.05)
        assert payload is None

    @pytest.mark.asyncio
    async def test_two_concurrent_dispatches_each_get_their_own_reply(
        self,
        nats_client: FakeNATSClient,
    ) -> None:
        adapter, registry = _wire_real_registry(nats_client)
        key1 = registry.fresh_correlation_key()
        key2 = registry.fresh_correlation_key()
        binding1 = await registry.bind(key1, "po-agent")
        binding2 = await registry.bind(key2, "po-agent")

        # Both dispatches to the same agent share ONE subscription.
        assert len(nats_client.subscriptions) == 1
        sub = nats_client.subscriptions[0]

        result1 = _wrap_role_output_result(role_id="product-owner")
        result2 = _wrap_role_output_result(role_id="product-owner")
        result2["coach_score"] = 0.5  # make the two replies distinguishable

        # Deliver out of order — key2's reply first, then key1's.
        await sub.deliver(
            _FakeMessage(
                subject="agents.result.po-agent",
                data=_deployed_reply_bytes(
                    correlation_id=key2, source_id="po-agent", result=result2
                ),
                headers=None,
            )
        )
        await sub.deliver(
            _FakeMessage(
                subject="agents.result.po-agent",
                data=_deployed_reply_bytes(
                    correlation_id=key1, source_id="po-agent", result=result1
                ),
                headers=None,
            )
        )

        payload1 = await registry.wait_for_reply(binding1, timeout_seconds=1.0)
        payload2 = await registry.wait_for_reply(binding2, timeout_seconds=1.0)
        assert payload1 is not None and payload1["correlation_id"] == key1
        assert payload1["result"] == result1
        assert payload2 is not None and payload2["correlation_id"] == key2
        assert payload2["result"] == result2
