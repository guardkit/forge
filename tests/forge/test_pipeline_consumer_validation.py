"""Tests for the validation surface of ``pipeline_consumer.handle_message``.

This module pins TASK-FW10-009's six acceptance criteria — the Group C
negative-path scenarios from ``API-nats-pipeline-events.md §2.3``:

* AC-001: malformed payload → ``build-failed`` published, message acked,
  no orchestrator dispatch.
* AC-002: duplicate ``(feature_id, correlation_id)`` → ack + skip, no
  ``build-started`` (i.e. no orchestrator dispatch), no ``build-failed``.
* AC-003: ``feature_yaml_path`` outside the worktree allowlist →
  ``build-failed`` published BEFORE any ``dispatch_build`` invocation.
* AC-004: ``publish_build_failed`` raising on any rejection path →
  logged at WARNING, no exception propagates, no SQLite-recorded
  transition is regressed (ADR-ARCH-008).
* AC-005: ``dispatch_build`` raising during a build → exception
  contained, the inbound message is acked so the next build can be
  processed, daemon stays running.
* AC-006 (lint) — covered by project ruff/format gate; not asserted in
  Python here.

The collaborators of :class:`PipelineConsumerDeps` are stubbed with
``unittest.mock.AsyncMock`` so the tests exercise only ``handle_message``
itself — the real state-machine entry, SQLite duplicate read, and
NATS publisher are out of scope. Each ``Test*`` class maps to one
acceptance criterion to keep the criterion → verifier mapping explicit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildFailedPayload

from forge.adapters.nats.pipeline_consumer import (
    REASON_MALFORMED_PAYLOAD,
    REASON_PATH_OUTSIDE_ALLOWLIST,
    UNKNOWN_FEATURE_ID,
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
# Fixtures — kept narrow on purpose so each test reads top-down without
# having to spelunk through a shared factory.
# ---------------------------------------------------------------------------


@pytest.fixture
def allowlist_root(tmp_path: Path) -> Path:
    """Resolved directory that is the only entry on the filesystem allowlist."""

    root = tmp_path / "repos"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def forge_config(allowlist_root: Path) -> ForgeConfig:
    """``ForgeConfig`` with default approved-originators and one allowlist entry."""

    return ForgeConfig(
        pipeline=PipelineConfig(),  # default approved_originators
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[allowlist_root]),
        ),
    )


@pytest.fixture
def deps_factory(forge_config: ForgeConfig):
    """Build :class:`PipelineConsumerDeps` whose collaborators are mocks.

    Returns a factory so each test can override the duplicate-check return
    value or substitute a publisher / dispatcher that raises.
    """

    def _make(
        *,
        is_duplicate_terminal: bool = False,
        publish_build_failed: AsyncMock | None = None,
        dispatch_build: AsyncMock | None = None,
    ) -> tuple[PipelineConsumerDeps, dict[str, AsyncMock]]:
        is_dup = AsyncMock(return_value=is_duplicate_terminal)
        dispatch = dispatch_build if dispatch_build is not None else AsyncMock()
        publish_failed = (
            publish_build_failed if publish_build_failed is not None else AsyncMock()
        )
        deps = PipelineConsumerDeps(
            forge_config=forge_config,
            is_duplicate_terminal=is_dup,
            dispatch_build=dispatch,
            publish_build_failed=publish_failed,
        )
        return deps, {
            "is_duplicate_terminal": is_dup,
            "dispatch_build": dispatch,
            "publish_build_failed": publish_failed,
        }

    return _make


def _envelope_bytes(payload: dict[str, Any]) -> bytes:
    """Wrap ``payload`` in a valid envelope and serialise to JSON bytes."""

    envelope = MessageEnvelope(
        message_id="msg-validation-test",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id="corr-validation-001",
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _valid_payload_dict(yaml_path: Path) -> dict[str, Any]:
    """Minimum-viable :class:`BuildQueuedPayload` dict for an allowlisted path."""

    return {
        "feature_id": "FEAT-V001",
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
        "correlation_id": "corr-validation-001",
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_msg(data: bytes) -> AsyncMock:
    """Mock ``nats.aio.msg.Msg`` with ``.data`` and an awaitable ``.ack()``."""

    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# AC-001: malformed payload publishes ``build-failed`` and acks
# ---------------------------------------------------------------------------


class TestMalformedPayloadEmitsBuildFailedAndAcks:
    """AC-001 — Group C scenario "malformed payload"."""

    @pytest.mark.asyncio
    async def test_invalid_envelope_publishes_build_failed_and_acks(
        self, deps_factory
    ) -> None:
        # Garbage bytes → envelope parse fails → REASON_MALFORMED_PAYLOAD.
        msg = _make_msg(b"this-is-not-json")
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        # The message was acked (so JetStream releases the slot).
        msg.ack.assert_awaited_once()
        # ``build-failed`` was published with the contract-pinned reason.
        mocks["publish_build_failed"].assert_awaited_once()
        sent_payload, feature_id_arg = mocks["publish_build_failed"].await_args.args
        assert isinstance(sent_payload, BuildFailedPayload)
        assert sent_payload.failure_reason == REASON_MALFORMED_PAYLOAD
        assert sent_payload.recoverable is False
        # Unknown feature_id when the envelope itself can't be parsed.
        assert feature_id_arg == UNKNOWN_FEATURE_ID
        # Critical: orchestrator dispatch was NEVER invoked.
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_inner_payload_publishes_build_failed_and_acks(
        self, deps_factory
    ) -> None:
        # Envelope is valid; inner ``BuildQueuedPayload`` is missing fields.
        broken = {"feature_id": "FEAT-BROKEN"}
        msg = _make_msg(_envelope_bytes(broken))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent_payload, feature_id_arg = mocks["publish_build_failed"].await_args.args
        assert sent_payload.failure_reason == REASON_MALFORMED_PAYLOAD
        # When the inner payload fails but the envelope parsed, we keep
        # the producer-supplied feature_id for traceability.
        assert feature_id_arg == "FEAT-BROKEN"
        mocks["dispatch_build"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-002: duplicate is acked + skipped (no ``build-started``, no dispatch)
# ---------------------------------------------------------------------------


class TestDuplicateEnvelopeAcksAndSkipsWithoutBuildStarted:
    """AC-002 — Group C scenario "duplicate"."""

    @pytest.mark.asyncio
    async def test_duplicate_acks_and_does_not_dispatch_or_publish_failure(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(is_duplicate_terminal=True)

        await handle_message(msg, deps)

        # Duplicate path: ack the inbound + skip — no second ``build-started``
        # is published (the consumer never publishes that event itself; the
        # state machine does, and we never reach the state machine).
        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()
        # Idempotent skip: NO ``build-failed`` either — the prior terminal
        # build's outcome is the source of truth (ADR-ARCH-008).
        mocks["publish_build_failed"].assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_check_uses_feature_id_and_correlation_id(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        # Ensure the duplicate lookup is keyed by the
        # ``(feature_id, correlation_id)`` pair from the payload — this is
        # what TASK-FW10-007's deps factory wires into the SQLite reader
        # (per ASSUM-014).
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(is_duplicate_terminal=True)

        await handle_message(msg, deps)

        mocks["is_duplicate_terminal"].assert_awaited_once_with(
            "FEAT-V001", "corr-validation-001"
        )


# ---------------------------------------------------------------------------
# AC-003: worktree-allowlist failure publishes ``build-failed`` BEFORE
# any orchestrator dispatch
# ---------------------------------------------------------------------------


class TestAllowlistFailurePublishesBuildFailedBeforeDispatch:
    """AC-003 — Group C scenario "allowlist"."""

    @pytest.mark.asyncio
    async def test_path_outside_allowlist_publishes_build_failed_and_skips_dispatch(
        self, deps_factory, tmp_path: Path
    ) -> None:
        # Path that resolves outside the allowlist root.
        outside = tmp_path / "outside" / "feature.yaml"
        outside.parent.mkdir()
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        # The publish happened.
        mocks["publish_build_failed"].assert_awaited_once()
        sent_payload, _feature_id = mocks["publish_build_failed"].await_args.args
        assert sent_payload.failure_reason == REASON_PATH_OUTSIDE_ALLOWLIST
        # Critically: orchestrator dispatch was NEVER reached.
        mocks["dispatch_build"].assert_not_called()
        # And the inbound was acked — it must not be redelivered.
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publish_build_failed_is_invoked_before_dispatch_build(
        self, deps_factory, tmp_path: Path
    ) -> None:
        # AC wording: "before any orchestrator dispatch". We verify
        # ordering by giving both mocks a shared call-recorder — the
        # publish must appear in the recorder while dispatch never does.
        outside = tmp_path / "elsewhere" / "feature.yaml"
        outside.parent.mkdir()
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))

        call_log: list[str] = []

        async def _record_publish(*_args: Any, **_kwargs: Any) -> None:
            call_log.append("publish_build_failed")

        async def _record_dispatch(*_args: Any, **_kwargs: Any) -> None:
            call_log.append("dispatch_build")

        deps, _mocks = deps_factory(
            publish_build_failed=AsyncMock(side_effect=_record_publish),
            dispatch_build=AsyncMock(side_effect=_record_dispatch),
        )

        await handle_message(msg, deps)

        # Publish was invoked exactly once and dispatch was never invoked,
        # which trivially makes publish "before any dispatch".
        assert call_log == ["publish_build_failed"]


# ---------------------------------------------------------------------------
# AC-004: publish failure on any rejection path is contained — logs at
# WARNING, does not regress recorded transition, daemon stays running
# ---------------------------------------------------------------------------


class TestPublishFailureDoesNotRegressTransition:
    """AC-004 — Group C scenario "publish failure does not regress" (ADR-ARCH-008).

    We exercise each of the three rejection paths (malformed, allowlist,
    duplicate-not-applicable-here-because-it-doesn't-publish) and verify
    that when ``publish_build_failed`` raises, ``handle_message`` returns
    cleanly: no exception escapes, the message is still acked, and a
    WARNING is logged.

    The "does not regress" half of the AC is about SQLite state — but
    none of these rejection paths touch SQLite themselves (they reject
    *before* any state-machine entry), so the only state-side
    invariant we can assert here is the *absence* of any further
    collaborator interaction. The production wiring (TASK-FW10-007's
    deps factory) is responsible for ensuring no SQLite write fires
    from inside the publisher itself.
    """

    @pytest.mark.asyncio
    async def test_malformed_path_swallows_publish_failure(
        self, deps_factory, caplog: pytest.LogCaptureFixture
    ) -> None:
        publish = AsyncMock(side_effect=RuntimeError("nats publish failed"))
        msg = _make_msg(b"not-json")
        deps, mocks = deps_factory(publish_build_failed=publish)

        with caplog.at_level(
            logging.WARNING, logger="forge.adapters.nats.pipeline_consumer"
        ):
            # Must NOT raise — the daemon stays running.
            await handle_message(msg, deps)

        # Publish was attempted exactly once; it raised; we logged.
        publish.assert_awaited_once()
        assert any(
            "publish_build_failed raised" in rec.message
            and rec.levelno == logging.WARNING
            for rec in caplog.records
        )
        # Ack still happened — JetStream releases the slot.
        msg.ack.assert_awaited_once()
        # Dispatch was never invoked (rejection path).
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_allowlist_path_swallows_publish_failure(
        self,
        deps_factory,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        outside = tmp_path / "wrong-root" / "feature.yaml"
        outside.parent.mkdir()
        publish = AsyncMock(side_effect=ConnectionError("no responders"))
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))
        deps, mocks = deps_factory(publish_build_failed=publish)

        with caplog.at_level(
            logging.WARNING, logger="forge.adapters.nats.pipeline_consumer"
        ):
            await handle_message(msg, deps)

        publish.assert_awaited_once()
        assert any(
            "publish_build_failed raised" in rec.message for rec in caplog.records
        )
        # Ack happened (the rejection contract is "ack + (try to) publish").
        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_path_does_not_publish_so_no_failure_to_swallow(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        # Duplicate detection ack-and-skips with NO publish. We assert the
        # contract by giving the publisher a side effect that would crash
        # and verifying it never fires — proving the duplicate path is
        # immune to publisher faults.
        publish = AsyncMock(side_effect=RuntimeError("would crash if called"))
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(
            is_duplicate_terminal=True,
            publish_build_failed=publish,
        )

        await handle_message(msg, deps)

        publish.assert_not_called()
        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-005: dispatch error during a build is contained
# ---------------------------------------------------------------------------


class TestDispatchErrorIsContained:
    """AC-005 — Group C scenario "dispatch error contained".

    When ``dispatch_build`` raises out of the state-machine entry point,
    ``handle_message`` MUST:

    * NOT re-raise (so the fetch loop keeps running);
    * Publish a terminal ``build-failed`` envelope (TASK-FORGE-FRR-F010F)
      threading the inbound ``correlation_id`` so the operator's chat
      session sees a terminal envelope instead of a silent drop;
    * Best-effort ack the inbound message (so ``max_ack_pending=1`` does
      not block the next delivery);
    * Log at WARNING for operator visibility.

    F010.F's contract narrowed ADR-ARCH-008's "no duplicate publish"
    protection: the state machine only owns the publish *after* it has
    started transitioning. When ``dispatch_build`` raises out to this
    outer try/except, the state machine never started, so a consumer-
    side terminal envelope cannot duplicate one — and is the only way
    the operator's chat session learns the build dropped (DDR-030).
    """

    @pytest.mark.asyncio
    async def test_dispatch_error_publishes_build_failed_and_acks(
        self,
        deps_factory,
        allowlist_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dispatch = AsyncMock(side_effect=RuntimeError("state machine crashed"))
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(dispatch_build=dispatch)

        with caplog.at_level(
            logging.WARNING, logger="forge.adapters.nats.pipeline_consumer"
        ):
            # No exception escapes the boundary.
            await handle_message(msg, deps)

        dispatch.assert_awaited_once()
        # WARNING was logged with enough identity for triage.
        warning_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "dispatch_build raised" in rec.message
        ]
        assert warning_records, "expected a WARNING log on dispatch error"
        # The inbound was acked — the next delivered build can be processed.
        msg.ack.assert_awaited_once()
        # F010.F: a terminal ``build-failed`` envelope is published
        # before the ack so the operator's chat session learns the
        # build dropped. The reason carries the exception class for
        # triage; per-class assertions and correlation_id threading
        # are exercised in detail by
        # ``tests/forge/test_pipeline_consumer_dispatch_failure_publish.py``.
        mocks["publish_build_failed"].assert_awaited_once()
        publish_args = mocks["publish_build_failed"].await_args
        sent_payload = publish_args.args[0]
        assert "RuntimeError" in sent_payload.failure_reason
        assert sent_payload.recoverable is False

    @pytest.mark.asyncio
    async def test_dispatch_error_does_not_block_next_message(
        self,
        deps_factory,
        allowlist_root: Path,
    ) -> None:
        # Simulate the Group C "next delivered build is processed"
        # scenario by feeding two messages back-to-back: the first
        # dispatch raises; the second must still flow through to
        # ``dispatch_build`` cleanly.
        first_call = {"count": 0}

        async def _dispatch(*_args: Any, **_kwargs: Any) -> None:
            first_call["count"] += 1
            if first_call["count"] == 1:
                raise RuntimeError("first build crashed")
            # Second build succeeds — dispatch returns without ack.

        dispatch = AsyncMock(side_effect=_dispatch)
        yaml_path = allowlist_root / "feature.yaml"
        msg_1 = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        msg_2 = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, _mocks = deps_factory(dispatch_build=dispatch)

        # Sequential fetch-loop simulation.
        await handle_message(msg_1, deps)
        await handle_message(msg_2, deps)

        # Both messages reached dispatch — the daemon did not stall.
        assert dispatch.await_count == 2
        # First message was acked from the containment branch.
        msg_1.ack.assert_awaited_once()
        # Second message is unacked — the (mocked) state machine returned
        # successfully but never invoked its ack callback. That mirrors
        # production: ack is deferred until terminal transition.
        msg_2.ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_error_and_ack_failure_does_not_propagate(
        self,
        deps_factory,
        allowlist_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # If both dispatch AND the post-dispatch ack raise, ``handle_message``
        # still completes cleanly. JetStream will redeliver after ack_wait;
        # the daemon must not crash here.
        dispatch = AsyncMock(side_effect=RuntimeError("dispatch boom"))
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        msg.ack = AsyncMock(side_effect=RuntimeError("ack boom"))
        deps, _mocks = deps_factory(dispatch_build=dispatch)

        with caplog.at_level(
            logging.WARNING, logger="forge.adapters.nats.pipeline_consumer"
        ):
            await handle_message(msg, deps)

        # Both warnings were logged.
        messages = [
            rec.message for rec in caplog.records if rec.levelno == logging.WARNING
        ]
        assert any("dispatch_build raised" in m for m in messages)
        assert any("ack_callback raised" in m for m in messages)
