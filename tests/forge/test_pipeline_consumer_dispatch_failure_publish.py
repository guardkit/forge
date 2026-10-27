"""TASK-FORGE-FRR-F010F — terminal ``build-failed`` envelope on dispatch raise.

When :meth:`PipelineConsumerDeps.dispatch_build` raises an unhandled
exception before the running state machine takes ownership of the
publish, the consumer publishes a ``pipeline.build-failed.{feature_id}``
envelope (threading the inbound ``correlation_id`` per DDR-029) **before**
acking the inbound message. The change closes the silent-drop hole
observed live on 2026-05-04 as Gap F010.B (`'SqliteLifecyclePersistence'
object has no attribute 'get_approved_stage_entry'`) and Gap F010.E
(`'StructuredTool' object has no attribute 'start_async_task'`) — both
produced zero outbound envelopes despite the daemon having a known
``correlation_id`` in hand.

Test classes map 1:1 to the task's acceptance criteria:

* ``TestDispatchRaisePublishesBuildFailed`` — AC-2 + AC-3: parametrised
  over ``AttributeError`` (the empirical F010.E shape) and
  ``RuntimeError`` (cross-cut for unrelated exception classes). The
  outbound envelope MUST carry the inbound ``correlation_id`` and a
  ``failure_reason`` containing the exception class name.
* ``TestPublishFailureStillAcks`` — AC-4: even when publishing the
  ``build-failed`` envelope itself fails (transport error), the inbound
  message is still acked. The daemon must never wedge the queue on an
  outbound publish failure.
* ``TestHappyPathDoesNotPublish`` — AC-5 protective: the new publish
  fires only on the exception path, never on the dispatch-success path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildFailedPayload

from forge.adapters.nats.pipeline_consumer import (
    PipelineConsumerDeps,
    handle_message,
)
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PipelineConfig,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/forge/test_pipeline_consumer_correlation_id.py)
# ---------------------------------------------------------------------------


INBOUND_CORRELATION_ID = "dfad8e7f-92af-4b5f-896f-ca75ad8343bf"
"""The empirical inbound correlation_id reproduced on 2026-05-04 (late
afternoon rerun, run 1) — the F010.E co-symptom that surfaced the need
for this safety-net publish site."""

FEATURE_ID = "FEAT-43DE"


@pytest.fixture
def allowlist_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def feature_yaml(allowlist_root: Path) -> Path:
    yaml_path = allowlist_root / "feature.yaml"
    yaml_path.write_text("# placeholder", encoding="utf-8")
    return yaml_path


@pytest.fixture
def forge_config(allowlist_root: Path) -> ForgeConfig:
    return ForgeConfig(
        pipeline=PipelineConfig(),
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[allowlist_root]),
        ),
    )


def _envelope_bytes(payload: dict[str, Any]) -> bytes:
    envelope = MessageEnvelope(
        message_id="msg-dispatch-failure-test",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=INBOUND_CORRELATION_ID,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _valid_payload_dict(yaml_path: Path) -> dict[str, Any]:
    return {
        "feature_id": FEATURE_ID,
        "repo": "appmilla/example",
        "branch": "main",
        "feature_yaml_path": str(yaml_path),
        "max_turns": 5,
        "sdk_timeout_seconds": 1800,
        "wave_gating": True,
        "config_overrides": None,
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "originating_user": "rich",
        "correlation_id": INBOUND_CORRELATION_ID,
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_msg(data: bytes) -> AsyncMock:
    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    return msg


def _make_deps(
    forge_config: ForgeConfig,
    *,
    dispatch_build: AsyncMock,
    publish_build_failed: AsyncMock | None = None,
) -> tuple[PipelineConsumerDeps, dict[str, AsyncMock]]:
    is_dup = AsyncMock(return_value=False)
    publish_failed = (
        publish_build_failed if publish_build_failed is not None else AsyncMock()
    )
    deps = PipelineConsumerDeps(
        forge_config=forge_config,
        is_duplicate_terminal=is_dup,
        dispatch_build=dispatch_build,
        publish_build_failed=publish_failed,
    )
    return deps, {
        "is_duplicate_terminal": is_dup,
        "dispatch_build": dispatch_build,
        "publish_build_failed": publish_failed,
    }


# ---------------------------------------------------------------------------
# AC-2 + AC-3: dispatch raise publishes build-failed (parametrised over
# exception classes — empirical AttributeError plus an unrelated RuntimeError).
# ---------------------------------------------------------------------------


class TestDispatchRaisePublishesBuildFailed:
    """AC-2 + AC-3 — the safety-net publish fires for every exception class.

    Both ``AttributeError`` (the empirical F010.E shape) and
    ``RuntimeError`` (cross-cut for unrelated exception classes) must
    produce envelope-correct ``build-failed`` output. Pegging the
    contract to a single exception type would let a future failure
    mode regress the safety net.
    """

    @pytest.mark.parametrize(
        "exc, exc_class_name",
        [
            (
                AttributeError(
                    "'StructuredTool' object has no attribute "
                    "'start_async_task'"
                ),
                "AttributeError",
            ),
            (RuntimeError("disk full"), "RuntimeError"),
        ],
        ids=["attribute_error_f010e_shape", "runtime_error_cross_cut"],
    )
    @pytest.mark.asyncio
    async def test_dispatch_raise_publishes_build_failed_with_correlation_id(
        self,
        forge_config: ForgeConfig,
        feature_yaml: Path,
        exc: Exception,
        exc_class_name: str,
    ) -> None:
        # Arrange: a dispatch_build that raises before any state machine
        # transition is recorded — mirrors the empirical F010.B / F010.E
        # failure mode on 2026-05-04.
        dispatch_build = AsyncMock(side_effect=exc)
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(feature_yaml)))
        deps, mocks = _make_deps(forge_config, dispatch_build=dispatch_build)

        # Act
        await handle_message(msg, deps)

        # Assert — outbound build-failed envelope is published exactly once
        mocks["publish_build_failed"].assert_awaited_once()
        await_args = mocks["publish_build_failed"].await_args

        # Subject feature_id is the second positional arg per
        # ``PublishBuildFailed`` Protocol contract.
        assert await_args.args[1] == FEATURE_ID

        # AC-1 — DDR-029 correlation_id threading: the inbound
        # ``correlation_id`` MUST appear on the outbound publish call
        # so jarvis's ``forge_subscriber`` can route the terminal
        # envelope back to the originating chat session.
        assert (
            await_args.kwargs.get("correlation_id") == INBOUND_CORRELATION_ID
        ), (
            "dispatch-failure publish must thread the inbound "
            "correlation_id (DDR-029); F010.C's contract extends to "
            "this site automatically via ``_safe_publish_failure``."
        )

        # AC-1 — failure_reason includes exception class name + message
        failure: BuildFailedPayload = await_args.args[0]
        assert exc_class_name in failure.failure_reason, (
            f"failure_reason {failure.failure_reason!r} must mention "
            f"the exception class {exc_class_name!r} so triage of the "
            f"chat REPL terminal-card rendering is fast"
        )
        assert str(exc) in failure.failure_reason

        # AC-1 — recoverable=False matches the existing rejection-publish
        # convention: dispatch failures are not retried by operator
        # workflow; they're surfaced as terminal so the operator can
        # decide whether to re-issue.
        assert failure.recoverable is False
        assert failure.feature_id == FEATURE_ID

        # The ack_callback must still fire so JetStream releases the
        # ``max_ack_pending=1`` slot for the next build (ADR-ARCH-014).
        msg.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-4: publish failure does not block ack — the daemon never wedges the queue.
# ---------------------------------------------------------------------------


class TestPublishFailureStillAcks:
    """AC-4 — even when publishing ``build-failed`` itself fails, ack still fires.

    Mirrors the existing ``_safe_publish_failure`` swallow-and-log
    pattern at ``pipeline_consumer.py:308-322``. A NATS connection
    refused / encoding error during the safety-net publish must be
    logged, not raised; the inbound message is still acked so
    JetStream releases the ``max_ack_pending=1`` slot and the next
    build is processed.
    """

    @pytest.mark.asyncio
    async def test_publish_build_failed_raises_does_not_block_ack(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # Arrange: dispatch raises (triggering the safety net) AND the
        # safety-net publish itself raises (transport blip).
        dispatch_build = AsyncMock(side_effect=AttributeError("test failure"))
        publish_build_failed = AsyncMock(
            side_effect=ConnectionError("nats connection refused")
        )
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(feature_yaml)))
        deps, mocks = _make_deps(
            forge_config,
            dispatch_build=dispatch_build,
            publish_build_failed=publish_build_failed,
        )

        # Act — handle_message must not raise; the daemon keeps running.
        await handle_message(msg, deps)

        # Assert — the publish was attempted...
        mocks["publish_build_failed"].assert_awaited_once()

        # ...and the inbound ack still fired despite the publish raising.
        # Without this, a transport blip during the safety-net publish
        # would wedge the consumer (max_ack_pending=1) until ack_wait
        # expired and the message was redelivered.
        msg.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-5 protective: happy-path dispatch never publishes build-failed.
# ---------------------------------------------------------------------------


class TestHappyPathDoesNotPublish:
    """AC-5 protective — the safety-net publish fires only on dispatch raise.

    On the success path, ``dispatch_build`` returns normally and the
    state machine owns the publish lifecycle (per ADR-ARCH-008). The
    consumer must not emit a competing ``build-failed`` envelope when
    dispatch succeeds — that would violate the single-source-of-truth
    contract on the path the contract still applies to.
    """

    @pytest.mark.asyncio
    async def test_dispatch_success_does_not_publish_build_failed(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        dispatch_build = AsyncMock(return_value=None)  # happy path
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(feature_yaml)))
        deps, mocks = _make_deps(forge_config, dispatch_build=dispatch_build)

        await handle_message(msg, deps)

        mocks["dispatch_build"].assert_awaited_once()
        # The state machine owns the publish on the success path; the
        # consumer must stay silent.
        mocks["publish_build_failed"].assert_not_awaited()
