"""Mode C wire-layer smoke test against a local NATS broker.

TASK-F8-002 / F008-VAL-002 AC-10 — verifies that a Mode C
``BuildQueuedPayload`` carrying both ``feature_id`` (parent FEAT-) and
``task_id`` (per-fix-task TASK-) round-trips through a real NATS
broker, lands on ``pipeline.build-queued.<feature_id>``, and is read
back by a consumer with both identifier fields intact.

Distinct from :mod:`tests.integration.test_mode_c_smoke_e2e` (which is
the in-memory pipeline-level smoke test) — this file exercises the
wire layer end-to-end against a live broker and is **skipped by
default**. It activates only when ``FORGE_NATS_URL`` is set, e.g.::

    FORGE_NATS_URL=nats://127.0.0.1:4222 pytest \\
        tests/integration/test_mode_c_wire_smoke_e2e.py

This matches the discipline of every other broker-backed test in the
forge suite: opt in at the env-var level, never require a live broker
for a default ``pytest`` invocation.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime

import pytest

# Module-level skip — this test publishes a REAL build_queued envelope, so
# it requires the EXPLICIT live-broker opt-in (register 2a2, 2026-07-28):
# env-PRESENCE arming let an operator's convenience FORGE_NATS_URL export
# publish junk builds to the production broker three times (FEAT-D70CDF /
# FEAT-3E094C / FEAT-089E6F+812C4B), two of which reached the production
# gate and Slack. The root tests/conftest.py additionally overrides broker
# env vars to an unroutable address unless the opt-in is set.
_NATS_URL = os.environ.get("FORGE_NATS_URL")
if os.environ.get("FORGE_ALLOW_LIVE_BROKER") != "1" or not _NATS_URL:
    pytest.skip(
        "Mode C wire smoke E2E publishes a REAL build-queued envelope; it "
        "requires the EXPLICIT opt-in FORGE_ALLOW_LIVE_BROKER=1 plus "
        "FORGE_NATS_URL pointing at a broker you own (TASK-F8-002 AC-10; "
        "register 2a2 broker-isolation fence).",
        allow_module_level=True,
    )

# Lazy-import nats-py only after the skip — keeps test collection green
# on environments without the optional ``nats-py`` runtime dependency.
import nats  # noqa: E402

from nats_core.envelope import EventType, MessageEnvelope  # noqa: E402
from nats_core.events import BuildQueuedPayload  # noqa: E402

pytestmark = pytest.mark.integration


def _unique_feature_id() -> str:
    # The wire schema requires ``^FEAT-[A-Z0-9]{3,12}$`` so the suffix
    # must be A-Z0-9. Hex from uuid4 is lowercase, so upper() it and
    # take the first 6 chars to stay inside the 3..12 range.
    suffix = uuid.uuid4().hex[:6].upper()
    return f"FEAT-{suffix}"


def _unique_task_id() -> str:
    suffix = uuid.uuid4().hex[:6].upper()
    return f"TASK-{suffix}"


@pytest.mark.asyncio
async def test_mode_c_payload_round_trips_through_local_nats() -> None:
    """Publish + receive a Mode C ``BuildQueuedPayload`` against a live broker.

    End-to-end shape:

    * Build a Mode C payload with a parent ``feature_id`` and a
      per-fix-task ``task_id``.
    * Wrap it in a ``MessageEnvelope`` and publish to
      ``pipeline.build-queued.<feature_id>``.
    * Subscribe to the same subject; receive the envelope.
    * Deserialise via :meth:`BuildQueuedPayload.model_validate` and
      assert both identifier fields and ``mode == "mode-c"``.
    """
    feature_id = _unique_feature_id()
    task_id = _unique_task_id()
    correlation_id = f"smoke-{uuid.uuid4()}"
    subject = f"pipeline.build-queued.{feature_id}"

    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path=f"features/{feature_id}/fix-task.yaml",
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=correlation_id,
        requested_at=now,
        queued_at=now,
        # TASK-F8-002 fields under test:
        mode="mode-c",
        task_id=task_id,
    )
    envelope = MessageEnvelope(
        source_id="forge-cli",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    body = envelope.model_dump_json().encode("utf-8")

    received: list[bytes] = []
    received_event = asyncio.Event()

    async def _on_message(msg: "nats.aio.msg.Msg") -> None:
        received.append(msg.data)
        received_event.set()

    client = await nats.connect(servers=_NATS_URL)
    try:
        sub = await client.subscribe(subject, cb=_on_message)
        try:
            await client.publish(subject, body)
            await client.flush()
            try:
                await asyncio.wait_for(received_event.wait(), timeout=5.0)
            except asyncio.TimeoutError as exc:  # pragma: no cover - broker degraded
                raise AssertionError(
                    f"Did not receive Mode C envelope on {subject!r} within 5s"
                ) from exc
        finally:
            await sub.unsubscribe()
    finally:
        await client.close()

    assert len(received) == 1
    raw = received[0].decode("utf-8")
    parsed_envelope = MessageEnvelope.model_validate(json.loads(raw))
    assert parsed_envelope.event_type is EventType.BUILD_QUEUED
    assert parsed_envelope.correlation_id == correlation_id

    parsed_payload = BuildQueuedPayload.model_validate(parsed_envelope.payload)
    assert parsed_payload.feature_id == feature_id
    assert parsed_payload.task_id == task_id
    assert parsed_payload.mode == "mode-c"
