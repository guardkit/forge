"""Tests for ``forge.adapters.nats.pipeline_consumer``.

Each ``Test*`` class maps to one acceptance criterion of TASK-NFI-007 so the
mapping between criterion and verifier stays explicit. Production
collaborators (state machine entry, SQLite duplicate read, build-failed
publisher) are stubbed with ``unittest.mock.AsyncMock``; the only real
external library exercised here is ``nats-py``'s ``ConsumerConfig`` dataclass
(used for AC-001 shape verification).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildFailedPayload, BuildQueuedPayload

from nats_core.events import (
    ApprovalRequestPayload,
    BuildPausedPayload,
)

from forge.adapters.nats.pipeline_consumer import (
    ACK_WAIT_SECONDS,
    BUILD_QUEUE_SUBJECT,
    DURABLE_NAME,
    IN_FLIGHT_BUILD_STATES,
    PAUSED_BUILD_STATE,
    REASON_MALFORMED_PAYLOAD,
    REASON_ORIGINATOR_NOT_RECOGNISED,
    REASON_PATH_OUTSIDE_ALLOWLIST,
    RESTART_FROM_PREPARING_STATES,
    STREAM_NAME,
    TERMINAL_BUILD_STATES,
    PausedBuildSnapshot,
    PipelineConsumerDeps,
    ReconcileDeps,
    ReconcileReport,
    build_consumer_config,
    handle_message,
    reconcile_on_boot,
)
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PipelineConfig,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def allowlist_root(tmp_path: Path) -> Path:
    """A real, resolved directory used as the only allowlist entry."""

    root = tmp_path / "repos"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def forge_config(allowlist_root: Path) -> ForgeConfig:
    """``ForgeConfig`` with the default approved-originators list and a
    single allowlisted directory rooted in ``tmp_path``."""

    return ForgeConfig(
        pipeline=PipelineConfig(),  # default approved_originators
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[allowlist_root]),
        ),
    )


@pytest.fixture
def deps_factory(forge_config: ForgeConfig):
    """Build a :class:`PipelineConsumerDeps` whose collaborators are mocks.

    Returns a factory so individual tests can override the duplicate-check
    return value or capture dispatch arguments.
    """

    def _make(
        *,
        is_duplicate_terminal: bool = False,
    ) -> tuple[PipelineConsumerDeps, dict[str, AsyncMock]]:
        is_dup = AsyncMock(return_value=is_duplicate_terminal)
        dispatch = AsyncMock()
        publish_failed = AsyncMock()
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
    """Wrap ``payload`` in a valid ``MessageEnvelope`` and serialise to JSON
    bytes ready for ``msg.data``."""

    envelope = MessageEnvelope(
        message_id="msg-test-001",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id="corr-001",
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _valid_payload_dict(yaml_path: Path) -> dict[str, Any]:
    """A minimum-viable :class:`BuildQueuedPayload` dict with a path that
    resolves inside the allowlist root."""

    return {
        "feature_id": "FEAT-A1B2",
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
        "correlation_id": "corr-001",
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_msg(data: bytes) -> AsyncMock:
    """Mock :class:`nats.aio.msg.Msg` exposing the ``.data`` byte buffer
    and an awaitable ``.ack()``."""

    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# AC-001: pull consumer config matches API-nats-pipeline-events.md §2.2
# ---------------------------------------------------------------------------


class TestConsumerConfig:
    """AC-001: consumer config matches the API contract verbatim."""

    def test_returns_consumer_config_instance(self) -> None:
        cfg = build_consumer_config()
        assert isinstance(cfg, ConsumerConfig)

    def test_durable_name_matches_contract(self) -> None:
        assert build_consumer_config().durable_name == "forge-consumer"
        assert DURABLE_NAME == "forge-consumer"

    def test_max_ack_pending_is_one(self) -> None:
        # ADR-ARCH-014 — sequential builds enforced at the transport.
        assert build_consumer_config().max_ack_pending == 1

    def test_ack_wait_is_one_hour(self) -> None:
        assert ACK_WAIT_SECONDS == 3600.0
        assert build_consumer_config().ack_wait == 3600.0

    def test_deliver_policy_is_all(self) -> None:
        assert build_consumer_config().deliver_policy is DeliverPolicy.ALL

    def test_ack_policy_is_explicit(self) -> None:
        assert build_consumer_config().ack_policy is AckPolicy.EXPLICIT

    def test_max_deliver_is_infinite(self) -> None:
        assert build_consumer_config().max_deliver == -1

    def test_filter_subject_matches_subscription_subject(self) -> None:
        assert build_consumer_config().filter_subject == BUILD_QUEUE_SUBJECT

    def test_subject_and_stream_constants(self) -> None:
        assert BUILD_QUEUE_SUBJECT == "pipeline.build-queued.>"
        assert STREAM_NAME == "PIPELINE"


# ---------------------------------------------------------------------------
# AC-002 + AC-009: valid payload dispatched with ack deferred until terminal
# ---------------------------------------------------------------------------


class TestValidPayloadDispatch:
    """AC-002: valid payload reaches state machine with ``ack_callback``.

    AC-009: non-terminal transitions do NOT ack. The callback is the
    state machine's only path to ``msg.ack()``.
    """

    @pytest.mark.asyncio
    async def test_valid_payload_invokes_dispatch_build(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["dispatch_build"].assert_awaited_once()
        sent_payload, ack_callback = mocks["dispatch_build"].await_args.args
        assert isinstance(sent_payload, BuildQueuedPayload)
        assert sent_payload.feature_id == "FEAT-A1B2"
        assert callable(ack_callback)

    @pytest.mark.asyncio
    async def test_ack_is_deferred_until_callback_invoked(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        # AC-009: msg.ack() must NOT be called during handle_message itself
        # for a valid build — only when the state machine fires the callback.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_not_called()
        # Now simulate the state machine reaching a terminal state.
        _, ack_callback = mocks["dispatch_build"].await_args.args
        await ack_callback()
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_failure_event_on_valid_payload(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-008: ack is called exactly once
# ---------------------------------------------------------------------------


class TestAckCalledExactlyOnce:
    """AC-008: ack is called exactly once per message."""

    @pytest.mark.asyncio
    async def test_ack_callback_is_idempotent(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)
        _, ack_callback = mocks["dispatch_build"].await_args.args

        # Even if the state machine accidentally fires the callback twice,
        # the underlying msg.ack() must only run once.
        await ack_callback()
        await ack_callback()
        assert msg.ack.await_count == 1

    @pytest.mark.asyncio
    async def test_rejection_acks_once_no_dispatch(
        self, deps_factory, tmp_path: Path
    ) -> None:
        # A payload outside the allowlist should ack exactly once (here,
        # immediately) and never invoke the dispatcher.
        outside = tmp_path / "outside" / "feature.yaml"
        outside.parent.mkdir()
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        assert msg.ack.await_count == 1
        mocks["dispatch_build"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-003: malformed payload → ack + build-failed; never reaches state machine
# ---------------------------------------------------------------------------


class TestMalformedPayload:
    """AC-003: malformed payload → ack + ``build-failed`` published."""

    @pytest.mark.asyncio
    async def test_invalid_envelope_json_acks_and_publishes_failure(
        self, deps_factory
    ) -> None:
        msg = _make_msg(b"this is not even close to valid json {{{")
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, _feature_id = mocks["publish_build_failed"].await_args.args
        assert isinstance(sent, BuildFailedPayload)
        assert sent.failure_reason == REASON_MALFORMED_PAYLOAD
        assert sent.recoverable is False
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_buildqueued_payload_acks_and_publishes_failure(
        self, deps_factory
    ) -> None:
        # Envelope is well-formed, but the inner payload is missing required
        # fields. We expect a malformed-payload rejection, not a crash.
        broken_payload = {"feature_id": "FEAT-OOPS"}  # missing everything else
        msg = _make_msg(_envelope_bytes(broken_payload))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, feature_id_arg = mocks["publish_build_failed"].await_args.args
        assert sent.failure_reason == REASON_MALFORMED_PAYLOAD
        assert feature_id_arg == "FEAT-OOPS"
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_garbage_bytes_does_not_invoke_dispatcher(
        self, deps_factory
    ) -> None:
        msg = _make_msg(b"\x00\x01\x02not-utf8")
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["dispatch_build"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-004 + AC-010: path outside allowlist → ack + build-failed; ``..`` rejected
# ---------------------------------------------------------------------------


class TestPathAllowlist:
    """AC-004: path outside allowlist → ack + ``build-failed``.

    AC-010: ``Path.resolve() + is_relative_to`` rejects ``..`` traversal.
    """

    @pytest.mark.asyncio
    async def test_path_outside_allowlist_is_rejected(
        self, deps_factory, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere" / "feature.yaml"
        outside.parent.mkdir()
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, _feature_id = mocks["publish_build_failed"].await_args.args
        assert sent.failure_reason == REASON_PATH_OUTSIDE_ALLOWLIST
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_dotdot_traversal_escape_is_rejected(
        self, deps_factory, allowlist_root: Path, tmp_path: Path
    ) -> None:
        # Construct a path that *literally* sits inside the allowlist root
        # but uses ``..`` to escape — Path.resolve() must collapse the
        # traversal so is_relative_to returns False.
        traversal = (
            allowlist_root
            / ".."
            / ".."
            / tmp_path.name  # back into tmp_path itself, outside the root
            / "outside"
            / "feature.yaml"
        )
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(traversal)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, _feature_id = mocks["publish_build_failed"].await_args.args
        assert sent.failure_reason == REASON_PATH_OUTSIDE_ALLOWLIST
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_path_inside_allowlist_via_dotdot_is_accepted(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        # Lexical ``..`` that *resolves back into* the allowlist root must
        # be accepted — only escapes are rejected.
        nested = allowlist_root / "subdir" / ".." / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(nested)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["dispatch_build"].assert_awaited_once()
        mocks["publish_build_failed"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-005: unrecognised originating_adapter → ack + build-failed
# ---------------------------------------------------------------------------


class TestOriginatorAllowlist:
    """AC-005: ``originating_adapter`` not in approved list → rejected."""

    @pytest.mark.asyncio
    async def test_none_originator_is_rejected(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        payload = _valid_payload_dict(yaml_path)
        # Bypass the Pydantic Literal so the consumer is the layer that
        # rejects it — emulates a producer publishing without populating
        # the field.
        envelope = MessageEnvelope(
            message_id="m1",
            timestamp=datetime.now(timezone.utc),
            version="1.0",
            source_id="cli-wrapper",
            event_type=EventType.BUILD_QUEUED,
            payload={**payload, "originating_adapter": None},
        )
        msg = _make_msg(envelope.model_dump_json().encode("utf-8"))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, _feature_id = mocks["publish_build_failed"].await_args.args
        assert sent.failure_reason == REASON_ORIGINATOR_NOT_RECOGNISED
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_unapproved_originator_is_rejected(
        self,
        forge_config: ForgeConfig,
        allowlist_root: Path,
    ) -> None:
        # Tighten the approved list so ``cli-wrapper`` becomes invalid
        # without having to monkey-patch the Pydantic Literal.
        narrow_config = ForgeConfig(
            pipeline=PipelineConfig(approved_originators=["terminal"]),
            permissions=forge_config.permissions,
        )
        is_dup = AsyncMock(return_value=False)
        dispatch = AsyncMock()
        publish_failed = AsyncMock()
        deps = PipelineConsumerDeps(
            forge_config=narrow_config,
            is_duplicate_terminal=is_dup,
            dispatch_build=dispatch,
            publish_build_failed=publish_failed,
        )

        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        publish_failed.assert_awaited_once()
        sent, _feature_id = publish_failed.await_args.args
        assert sent.failure_reason == REASON_ORIGINATOR_NOT_RECOGNISED
        dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# AC-006 + AC-007: duplicate already-terminal build → ack + skip
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """AC-006/AC-007: duplicates are acked and skipped without restart."""

    @pytest.mark.asyncio
    async def test_already_complete_build_is_acked_and_skipped(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(is_duplicate_terminal=True)

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()
        mocks["publish_build_failed"].assert_not_called()

    @pytest.mark.asyncio
    async def test_already_failed_build_is_also_acked_and_skipped(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        # The duplicate-check helper returns True for ANY terminal status —
        # COMPLETE, FAILED, CANCELLED, SKIPPED. We exercise the same code
        # path for FAILED to satisfy AC-007 explicitly.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(is_duplicate_terminal=True)

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_check_called_with_feature_and_correlation(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = deps_factory(is_duplicate_terminal=False)

        await handle_message(msg, deps)

        mocks["is_duplicate_terminal"].assert_awaited_once_with(
            "FEAT-A1B2", "corr-001"
        )


# ---------------------------------------------------------------------------
# AC-011 sanity: handle_message never raises on adversarial input
# ---------------------------------------------------------------------------


class TestRobustness:
    """The consumer is the I/O boundary — it must absorb bad input rather
    than blowing up the fetch loop. This guards the lint/format-zero-errors
    spirit of AC-011 by making sure stylistic edits never weaken the
    error-handling envelope.
    """

    @pytest.mark.asyncio
    async def test_empty_data_is_treated_as_malformed(self, deps_factory) -> None:
        msg = _make_msg(b"")
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        sent, _feature_id = mocks["publish_build_failed"].await_args.args
        assert sent.failure_reason == REASON_MALFORMED_PAYLOAD

    @pytest.mark.asyncio
    async def test_envelope_with_non_dict_payload_is_treated_as_malformed(
        self, deps_factory
    ) -> None:
        # We can't construct this through MessageEnvelope (Pydantic enforces
        # dict[str, Any]) — but on the wire a producer might publish a
        # string payload. Hand-craft the JSON so the inner validation fails.
        wire = json.dumps(
            {
                "message_id": "m1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": "1.0",
                "source_id": "cli-wrapper",
                "event_type": "build_queued",
                "project": None,
                "correlation_id": "corr-001",
                "payload": "not-a-dict",
            }
        ).encode("utf-8")
        msg = _make_msg(wire)
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        msg.ack.assert_awaited_once()
        # Either the envelope itself rejects the non-dict payload, or the
        # inner validation does — both flow into "malformed". We just need
        # to confirm we never dispatched.
        mocks["dispatch_build"].assert_not_called()


# ===========================================================================
# TASK-NFI-009: reconcile_on_boot crash recovery
# ===========================================================================
#
# Each `TestReconcile*` class maps to one or more acceptance criteria of
# TASK-NFI-009. SQLite is mocked via two ``AsyncMock``s — ``read_build_state``
# (returns the persisted status) and ``mark_interrupted_and_reset`` (no-op
# writer). The redelivery queue is simulated with a list-backed
# ``fetch_redeliveries`` callable that yields one batch then drains.
# ---------------------------------------------------------------------------


def _make_paused_snapshot(
    *,
    feature_id: str = "FEAT-A1B2",
    correlation_id: str = "corr-001",
    approval_subject: str = "agents.approval.forge.task-001",
) -> PausedBuildSnapshot:
    """Construct a :class:`PausedBuildSnapshot` with realistic payloads.

    The two payloads carry the same ``correlation_id`` so AC-004 ("ORIGINAL
    correlation_id") can be asserted by reading back the re-emitted
    envelope's correlation_id.
    """

    paused = BuildPausedPayload(
        feature_id=feature_id,
        build_id=f"build-{feature_id}-20260424120000",
        stage_label="implementation",
        gate_mode="MANDATORY_HUMAN_APPROVAL",
        coach_score=0.55,
        rationale="quality below threshold",
        approval_subject=approval_subject,
        paused_at=datetime.now(timezone.utc).isoformat(),
        correlation_id=correlation_id,
    )
    approval = ApprovalRequestPayload(
        request_id=f"req-{correlation_id}",
        agent_id="forge",
        action_description=(
            f"Resume paused build for {feature_id} after gate review"
        ),
        risk_level="medium",
        details={"feature_id": feature_id, "correlation_id": correlation_id},
        timeout_seconds=3600,
    )
    return PausedBuildSnapshot(
        feature_id=feature_id,
        correlation_id=correlation_id,
        build_paused_payload=paused,
        approval_request_payload=approval,
        approval_subject=approval_subject,
    )


def _make_fetch_redeliveries(batches: list[list[Any]]):
    """Return an async callable that yields ``batches`` then ``[]`` forever.

    Mirrors how ``js.PullSubscription.fetch`` behaves once the inbox is
    empty: subsequent calls return ``[]`` rather than raising.
    """

    queue: list[list[Any]] = list(batches)

    async def _fetch() -> list[Any]:
        if queue:
            return queue.pop(0)
        return []

    return _fetch


@pytest.fixture
def reconcile_factory(deps_factory):
    """Build a :class:`ReconcileDeps` with mocks for every collaborator.

    Returns a factory so individual tests can override the persisted state
    map, the paused-build snapshot list, and the redelivery batches.
    """

    def _make(
        *,
        state_by_key: dict[tuple[str, str], str | None] | None = None,
        paused_snapshots: list[PausedBuildSnapshot] | None = None,
        redelivery_batches: list[list[Any]] | None = None,
    ) -> tuple[ReconcileDeps, dict[str, AsyncMock]]:
        consumer_deps, consumer_mocks = deps_factory()
        state_map: dict[tuple[str, str], str | None] = state_by_key or {}

        async def _read(feature_id: str, correlation_id: str) -> str | None:
            return state_map.get((feature_id, correlation_id))

        read_build_state = AsyncMock(side_effect=_read)
        mark_interrupted_and_reset = AsyncMock()
        iter_paused_builds = AsyncMock(return_value=paused_snapshots or [])
        publish_build_paused = AsyncMock()
        publish_approval_request = AsyncMock()
        fetch_redeliveries = AsyncMock(
            side_effect=_make_fetch_redeliveries(redelivery_batches or [])
        )

        deps = ReconcileDeps(
            consumer_deps=consumer_deps,
            fetch_redeliveries=fetch_redeliveries,
            read_build_state=read_build_state,
            mark_interrupted_and_reset=mark_interrupted_and_reset,
            iter_paused_builds=iter_paused_builds,
            publish_build_paused=publish_build_paused,
            publish_approval_request=publish_approval_request,
        )
        return deps, {
            **consumer_mocks,
            "read_build_state": read_build_state,
            "mark_interrupted_and_reset": mark_interrupted_and_reset,
            "iter_paused_builds": iter_paused_builds,
            "publish_build_paused": publish_build_paused,
            "publish_approval_request": publish_approval_request,
            "fetch_redeliveries": fetch_redeliveries,
        }

    return _make


# ---------------------------------------------------------------------------
# AC-001: reconcile runs to completion and returns a report
# ---------------------------------------------------------------------------


class TestReconcileLifecycle:
    """AC-001: reconcile_on_boot runs once and returns a typed report."""

    @pytest.mark.asyncio
    async def test_returns_report_with_no_redeliveries_and_no_paused(
        self, reconcile_factory
    ) -> None:
        deps, _mocks = reconcile_factory()
        report = await reconcile_on_boot(deps)
        assert isinstance(report, ReconcileReport)
        # Empty inbox + empty paused scan ⇒ all counters zero.
        assert report.acked_terminal == 0
        assert report.restarted_in_flight == 0
        assert report.re_emitted_paused == 0
        assert report.fresh_builds == 0
        assert report.malformed == 0
        assert report.paused_scan_re_emitted == 0

    @pytest.mark.asyncio
    async def test_drains_until_fetch_returns_empty(
        self, reconcile_factory, allowlist_root: Path
    ) -> None:
        # Two non-empty batches then drain — verify we fetch until empty.
        yaml_path = allowlist_root / "feature.yaml"
        msg_a = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        msg_b = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))

        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): "COMPLETE"},
            redelivery_batches=[[msg_a], [msg_b]],
        )
        report = await reconcile_on_boot(deps)

        # First two calls return batches, third returns [] terminating loop.
        assert mocks["fetch_redeliveries"].await_count == 3
        assert report.acked_terminal == 2


# ---------------------------------------------------------------------------
# AC-002 + AC-007: terminal-state redelivery → ack, no new build (idempotent)
# ---------------------------------------------------------------------------


class TestReconcileTerminalStates:
    """AC-002 + AC-007: redelivered terminal-state builds are acked + skipped."""

    @pytest.mark.parametrize(
        "terminal_state", sorted(TERMINAL_BUILD_STATES)
    )
    @pytest.mark.asyncio
    async def test_terminal_state_is_acked_and_no_build_dispatched(
        self,
        reconcile_factory,
        allowlist_root: Path,
        terminal_state: str,
    ) -> None:
        # AC-007 scenario: "A redelivered build-queued message for a
        # completed build is acknowledged idempotently". Run for every
        # terminal value in the contract — COMPLETE, FAILED, CANCELLED,
        # SKIPPED — to lock in the idempotency invariant.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): terminal_state},
            redelivery_batches=[[msg]],
        )

        report = await reconcile_on_boot(deps)

        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()
        mocks["mark_interrupted_and_reset"].assert_not_called()
        mocks["publish_build_paused"].assert_not_called()
        assert report.acked_terminal == 1
        assert report.fresh_builds == 0
        assert report.restarted_in_flight == 0


# ---------------------------------------------------------------------------
# AC-003: INTERRUPTED-marked rows transition to PREPARING (retry-from-scratch)
# ---------------------------------------------------------------------------


class TestReconcileInFlightStates:
    """AC-003: RUNNING/FINALISING are marked INTERRUPTED and restarted."""

    @pytest.mark.parametrize(
        "in_flight_state", sorted(IN_FLIGHT_BUILD_STATES)
    )
    @pytest.mark.asyncio
    async def test_in_flight_marks_interrupted_and_redispatches(
        self,
        reconcile_factory,
        allowlist_root: Path,
        in_flight_state: str,
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): in_flight_state},
            redelivery_batches=[[msg]],
        )

        report = await reconcile_on_boot(deps)

        # Row was marked INTERRUPTED + reset to PREPARING via the writer.
        mocks["mark_interrupted_and_reset"].assert_awaited_once_with(
            "FEAT-A1B2", "corr-001"
        )
        # Build was re-dispatched through the standard pipeline path.
        mocks["dispatch_build"].assert_awaited_once()
        sent_payload, ack_callback = mocks["dispatch_build"].await_args.args
        assert sent_payload.feature_id == "FEAT-A1B2"
        assert callable(ack_callback)
        # ack is deferred — only the state machine's terminal callback fires it.
        msg.ack.assert_not_called()
        assert report.restarted_in_flight == 1
        assert report.acked_terminal == 0

    @pytest.mark.asyncio
    async def test_preparing_state_is_treated_as_in_flight(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # PREPARING + crash also means INTERRUPTED + re-prepare. Lock this
        # in so a future migration that introduces a separate
        # PREPARING-only branch does not silently regress.
        assert "PREPARING" in RESTART_FROM_PREPARING_STATES
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): "PREPARING"},
            redelivery_batches=[[msg]],
        )

        await reconcile_on_boot(deps)

        mocks["mark_interrupted_and_reset"].assert_awaited_once()
        mocks["dispatch_build"].assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-004 + AC-005: paused builds re-emit BuildPaused + ApprovalRequest
# ---------------------------------------------------------------------------


class TestReconcilePausedRedelivery:
    """AC-004 + AC-005: PAUSED redelivery re-emits both lifecycle events."""

    @pytest.mark.asyncio
    async def test_paused_redelivery_reemits_build_paused_with_original_correlation_id(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # AC-004: re-emit MUST carry the ORIGINAL correlation_id so
        # subscribers thread the resumption onto the same conversation.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        snap = _make_paused_snapshot()
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): PAUSED_BUILD_STATE},
            paused_snapshots=[snap],
            redelivery_batches=[[msg]],
        )

        await reconcile_on_boot(deps)

        mocks["publish_build_paused"].assert_awaited_once()
        (sent_payload,) = mocks["publish_build_paused"].await_args.args
        assert isinstance(sent_payload, BuildPausedPayload)
        assert sent_payload.feature_id == "FEAT-A1B2"
        # The ORIGINAL correlation_id is preserved on the re-emit.
        assert sent_payload.correlation_id == "corr-001"
        # No INTERRUPTED transition; PAUSED is kept.
        mocks["mark_interrupted_and_reset"].assert_not_called()
        # Message is NOT acked — paused builds keep the queue position.
        msg.ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_redelivery_reemits_approval_request_on_original_subject(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # AC-005: ApprovalRequest re-emit honours ADR-ARCH-021's
        # first-response-wins. Same request_id ⇒ same logical request, so
        # a late approval responder cannot double-resume after restart.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        snap = _make_paused_snapshot(
            approval_subject="agents.approval.forge.gate-impl"
        )
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): PAUSED_BUILD_STATE},
            paused_snapshots=[snap],
            redelivery_batches=[[msg]],
        )

        await reconcile_on_boot(deps)

        mocks["publish_approval_request"].assert_awaited_once()
        sent_payload, sent_subject = (
            mocks["publish_approval_request"].await_args.args
        )
        assert isinstance(sent_payload, ApprovalRequestPayload)
        # Same request_id as the original (first-response-wins guard).
        assert sent_payload.request_id == "req-corr-001"
        assert sent_subject == "agents.approval.forge.gate-impl"


# ---------------------------------------------------------------------------
# AC-006: unknown (feature_id, correlation_id) → fresh build
# ---------------------------------------------------------------------------


class TestReconcileUnknownBuild:
    """AC-006: unknown identity → fresh dispatch via handle_message."""

    @pytest.mark.asyncio
    async def test_unknown_redelivery_is_dispatched_as_fresh(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # No SQLite row → state_by_key returns None → fresh build.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        deps, mocks = reconcile_factory(
            state_by_key={},  # empty → read_build_state returns None
            redelivery_batches=[[msg]],
        )

        report = await reconcile_on_boot(deps)

        # Fresh dispatch — handle_message validates allowlists, finds no
        # duplicate (is_duplicate_terminal is the default mock returning
        # False), and dispatches as if it were a brand-new build.
        mocks["dispatch_build"].assert_awaited_once()
        # Critical: the duplicate-terminal check did fire (handle_message
        # path), but the reconcile-side ack-and-skip branch did NOT.
        msg.ack.assert_not_called()
        mocks["mark_interrupted_and_reset"].assert_not_called()
        assert report.fresh_builds == 1
        assert report.acked_terminal == 0


# ---------------------------------------------------------------------------
# AC-008: SQLite belt-and-braces paused scan
# ---------------------------------------------------------------------------


class TestReconcilePausedScan:
    """AC-008 / Group D @edge-case: PAUSED rows re-emit even with no redelivery.

    The ``iter_paused_builds`` reader returns a paused snapshot whose
    redelivery did NOT arrive in the JetStream batch. ``reconcile_on_boot``
    must still re-emit the paused lifecycle so subscribers see the gate
    request after a Forge restart.
    """

    @pytest.mark.asyncio
    async def test_paused_scan_reemits_when_no_redelivery(
        self,
        reconcile_factory,
    ) -> None:
        snap = _make_paused_snapshot()
        deps, mocks = reconcile_factory(
            state_by_key={},  # nothing in the redelivery loop
            paused_snapshots=[snap],
            redelivery_batches=[],  # JetStream redelivered nothing
        )

        report = await reconcile_on_boot(deps)

        # Belt-and-braces fired both publishes from the SQLite scan.
        mocks["publish_build_paused"].assert_awaited_once()
        mocks["publish_approval_request"].assert_awaited_once()
        assert report.paused_scan_re_emitted == 1
        assert report.re_emitted_paused == 0  # nothing came via redelivery

    @pytest.mark.asyncio
    async def test_paused_redelivery_and_scan_emit_only_once_per_build(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # If JetStream redelivers AND SQLite has the row, we must not
        # re-emit twice. The redelivery branch wins; the scan skips.
        yaml_path = allowlist_root / "feature.yaml"
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(yaml_path)))
        snap = _make_paused_snapshot()
        deps, mocks = reconcile_factory(
            state_by_key={("FEAT-A1B2", "corr-001"): PAUSED_BUILD_STATE},
            paused_snapshots=[snap],
            redelivery_batches=[[msg]],
        )

        report = await reconcile_on_boot(deps)

        # Each publisher fires exactly once total — the redelivery branch.
        mocks["publish_build_paused"].assert_awaited_once()
        mocks["publish_approval_request"].assert_awaited_once()
        assert report.re_emitted_paused == 1
        assert report.paused_scan_re_emitted == 0


# ---------------------------------------------------------------------------
# AC-008: all four rule branches with mocked SQLite reader (one-shot test)
# ---------------------------------------------------------------------------


class TestReconcileAllBranches:
    """AC-008: a single reconcile call dispatches every rule branch correctly."""

    @pytest.mark.asyncio
    async def test_one_call_handles_terminal_running_paused_and_unknown(
        self,
        reconcile_factory,
        allowlist_root: Path,
    ) -> None:
        # Compose four redeliveries — one per rule branch — with distinct
        # (feature_id, correlation_id) tuples so the SQLite mock can
        # return a different state for each.
        yaml_path = allowlist_root / "feature.yaml"

        def _msg_for(feature_id: str, correlation_id: str):
            payload = _valid_payload_dict(yaml_path)
            payload["feature_id"] = feature_id
            payload["correlation_id"] = correlation_id
            return _make_msg(_envelope_bytes(payload))

        msg_terminal = _msg_for("FEAT-DONE", "corr-done")
        msg_running = _msg_for("FEAT-RUN", "corr-run")
        msg_paused = _msg_for("FEAT-PAUSE", "corr-pause")
        msg_unknown = _msg_for("FEAT-NEW", "corr-new")

        snap = _make_paused_snapshot(
            feature_id="FEAT-PAUSE", correlation_id="corr-pause"
        )

        deps, mocks = reconcile_factory(
            state_by_key={
                ("FEAT-DONE", "corr-done"): "COMPLETE",
                ("FEAT-RUN", "corr-run"): "RUNNING",
                ("FEAT-PAUSE", "corr-pause"): PAUSED_BUILD_STATE,
                # FEAT-NEW intentionally absent → None → fresh
            },
            paused_snapshots=[snap],
            redelivery_batches=[
                [msg_terminal, msg_running, msg_paused, msg_unknown]
            ],
        )

        report = await reconcile_on_boot(deps)

        # Branch 1: terminal acked.
        msg_terminal.ack.assert_awaited_once()
        # Branch 2: running marked INTERRUPTED and dispatched.
        mocks["mark_interrupted_and_reset"].assert_awaited_once_with(
            "FEAT-RUN", "corr-run"
        )
        # Branch 3: paused re-emitted.
        mocks["publish_build_paused"].assert_awaited_once()
        mocks["publish_approval_request"].assert_awaited_once()
        # Branch 4: unknown handed to handle_message → dispatch_build.
        # dispatch_build is called twice: once for RUNNING, once for unknown.
        assert mocks["dispatch_build"].await_count == 2
        # Confirm the report counters reflect each branch firing once.
        assert report.acked_terminal == 1
        assert report.restarted_in_flight == 1
        assert report.re_emitted_paused == 1
        assert report.fresh_builds == 1
        assert report.paused_scan_re_emitted == 0  # already covered via redelivery


# ---------------------------------------------------------------------------
# Robustness: malformed redelivery delegates to handle_message (ack + failed)
# ---------------------------------------------------------------------------


class TestReconcileMalformedRedelivery:
    """Malformed redelivery does not break the loop; handle_message owns it."""

    @pytest.mark.asyncio
    async def test_malformed_redelivery_is_acked_and_published_failed(
        self,
        reconcile_factory,
    ) -> None:
        msg = _make_msg(b"this is not even close to valid json {{{")
        deps, mocks = reconcile_factory(
            redelivery_batches=[[msg]],
        )

        report = await reconcile_on_boot(deps)

        # handle_message owns the ack + build-failed for malformed input.
        msg.ack.assert_awaited_once()
        mocks["publish_build_failed"].assert_awaited_once()
        assert report.malformed == 1
        # SQLite was never read for a malformed payload — we don't have
        # a parseable identity to look up.
        mocks["read_build_state"].assert_not_called()
