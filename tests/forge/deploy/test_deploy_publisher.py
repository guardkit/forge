"""Signature-binding contract tests for DeployPublisher (WS2-B8).

Mirrors ``tests/forge/test_runbook_publisher.py``: asserts the six method names
exist and are coroutines, and that each publishes to the correct
``Topics.Deploy`` subject with a well-formed ``source_id="forge"`` envelope
carrying the payload's ``correlation_id``. These are the merge-review
signature-binding checks for the deploy-domain wire (the FEAT-DD4F lesson: pin
the real contract, not a shape a fake could satisfy).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nats_core.envelope import EventType
from nats_core.events import (
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployRevertedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)
from nats_core.topics import Topics

from forge.adapters.nats.deploy_publisher import SOURCE_ID, DeployPublisher

CID = "corr-123"
NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def nats_client() -> AsyncMock:
    client = AsyncMock()
    client.publish = AsyncMock(return_value=None)
    return client


@pytest.fixture
def publisher(nats_client: AsyncMock) -> DeployPublisher:
    return DeployPublisher(nats_client=nats_client)


def _decode(call: Any) -> tuple[str, dict[str, Any]]:
    args, kwargs = call.args, call.kwargs
    subject = args[0] if args else kwargs["subject"]
    body = args[1] if len(args) > 1 else kwargs["payload"]
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return subject, json.loads(body)


class TestPublisherSurface:
    @pytest.mark.parametrize(
        "method_name",
        [
            "publish_deploy_queued",
            "publish_deploy_started",
            "publish_deploy_complete",
            "publish_deploy_failed",
            "publish_deploy_reverted",
            "publish_qa_verdict",
            "publish_live_gate_result",
        ],
    )
    def test_method_exists_and_is_coroutine(self, method_name: str) -> None:
        method = getattr(DeployPublisher, method_name, None)
        assert method is not None, f"{method_name!r} not defined"
        assert asyncio.iscoroutinefunction(method)


class TestPublishContract:
    def test_deploy_queued_subject_and_envelope(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = DeployQueuedPayload(
            correlation_id=CID,
            env_id="fleet-memory-nas",
            deploy_run_id="run-1",
            queued_at=NOW,
        )
        asyncio.run(publisher.publish_deploy_queued(payload))
        subject, envelope = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.queued.{CID}"
        assert subject == Topics.Deploy.DEPLOY_QUEUED.format(correlation_id=CID)
        assert envelope["source_id"] == SOURCE_ID == "forge"
        assert envelope["event_type"] == EventType.DEPLOY_QUEUED.value
        assert envelope["correlation_id"] == CID
        assert envelope["payload"]["env_id"] == "fleet-memory-nas"

    def test_deploy_complete_carries_required_record_ref(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = DeployCompletePayload(
            correlation_id=CID,
            env_id="e",
            deploy_run_id="run-1",
            deploy_record_ref="docs/state/WS2-B8/deploy-record-2026-07-09.md",
            completed_at=NOW,
        )
        asyncio.run(publisher.publish_deploy_complete(payload))
        subject, envelope = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.complete.{CID}"
        assert envelope["payload"]["deploy_record_ref"].endswith(
            "deploy-record-2026-07-09.md"
        )
        assert envelope["payload"]["status"] == "complete"

    def test_deploy_failed_carries_failed_step(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = DeployFailedPayload(
            correlation_id=CID,
            env_id="e",
            deploy_run_id="run-1",
            failed_step="health_check",
            failure_reason="smoke.sh exit 1",
            failed_at=NOW,
        )
        asyncio.run(publisher.publish_deploy_failed(payload))
        subject, envelope = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.failed.{CID}"
        assert envelope["payload"]["failed_step"] == "health_check"
        assert envelope["payload"]["status"] == "failed"

    def test_deploy_reverted_subject_and_event_type(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = DeployRevertedPayload(
            correlation_id=CID,
            env_id="e",
            deploy_run_id="run-1",
            reverted_to_image_ref="app:rollback-20260713",
            failing_verdict="fail",
            reverted_at=NOW,
        )
        asyncio.run(publisher.publish_deploy_reverted(payload))
        subject, envelope = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.reverted.{CID}"
        assert envelope["event_type"] == EventType.DEPLOY_REVERTED.value
        assert envelope["payload"]["reverted_to_image_ref"] == "app:rollback-20260713"
        assert envelope["payload"]["status"] == "reverted"

    def test_qa_verdict_subject(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = QAVerdictPayload(
            correlation_id=CID,
            run_id="r1",
            env_id="e",
            verdict="pass",
            gate_ids=["g1"],
            evidence_index_ref="ev",
            attempt=1,
            decided_at=NOW,
        )
        asyncio.run(publisher.publish_qa_verdict(payload))
        subject, envelope = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.qa-verdict.{CID}"
        assert envelope["event_type"] == EventType.QA_VERDICT.value
        assert envelope["payload"]["verdict"] == "pass"

    def test_live_gate_result_subject(
        self, publisher: DeployPublisher, nats_client: AsyncMock
    ) -> None:
        payload = LiveGateResultPayload(
            correlation_id=CID,
            run_id="r1",
            env_id="e",
            verdict="pass",
            gate_ids=["g1"],
            evidence_index_ref="ev",
            attempt=1,
            finished_at=NOW,
        )
        asyncio.run(publisher.publish_live_gate_result(payload))
        subject, _ = _decode(nats_client.publish.call_args)
        assert subject == f"deploy.live-gate-result.{CID}"

    def test_missing_correlation_id_raises(self, publisher: DeployPublisher) -> None:
        # A payload without correlation_id cannot build a subject — fail loud.
        class _Bare:
            correlation_id = ""

            def model_dump(self, **_: Any) -> dict[str, Any]:
                return {}

        with pytest.raises(ValueError, match="correlation_id"):
            asyncio.run(publisher.publish_deploy_queued(_Bare()))  # type: ignore[arg-type]
