"""TASK-FRR-PEB-001 — defer the inbound build-queued ack to terminal arrival.

Each ``Test*`` class maps to one acceptance criterion of the task brief
so the mapping between criterion and verifier stays explicit:

* :class:`TestAckHandleRegistration` — AC-1 + AC-2: dispatch path stores
  a :class:`BuildAckHandle` in the bridge registry keyed by
  ``(feature_id, correlation_id)``; ``msg.ack`` does NOT fire on
  ``dispatch_build`` return when the bridge is wired.
* :class:`TestBridgeAckAndNak` — AC-2: the registered handle exposes
  both ``ack()`` and ``nak()``; calling either drives the underlying
  ``msg.ack`` / ``msg.nak`` exactly once (idempotent).
* :class:`TestF010FFallback` — AC-3: with ``register_ack_handle=None``
  the consumer falls back to the existing F010F sync-raise behaviour.
* :class:`TestDuplicateDetection` — AC-4: duplicate ``build-queued``
  envelopes for the same identity are acked + skipped without a second
  registration.
* :class:`TestCorrelationIdAstGuardCompat` — AC-5: every emit site the
  consumer touches threads ``correlation_id=`` explicitly so the F010C
  AST guard remains green.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope

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
from forge.pipeline.build_ack_handle import (
    BuildAckHandle,
    MsgBuildAckHandle,
    make_msg_ack_handle,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


FEATURE_ID = "FEAT-PEB1"
CORRELATION_ID = "e9433033-ea80-449f-885d-b2d1bdfb839e"


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


def _valid_payload(yaml_path: Path) -> dict[str, Any]:
    """Minimum-viable :class:`BuildQueuedPayload` dict."""
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
        "correlation_id": CORRELATION_ID,
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }


def _envelope_bytes(payload: dict[str, Any]) -> bytes:
    envelope = MessageEnvelope(
        message_id="msg-test-001",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=CORRELATION_ID,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _make_msg(data: bytes) -> AsyncMock:
    """Mock NATS Msg exposing ``.data``, awaitable ``.ack()`` and ``.nak()``."""
    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


def _make_deps(
    forge_config: ForgeConfig,
    *,
    is_duplicate_terminal: bool = False,
    register_ack_handle: Any = None,
) -> tuple[PipelineConsumerDeps, dict[str, AsyncMock]]:
    is_dup = AsyncMock(return_value=is_duplicate_terminal)
    dispatch = AsyncMock()
    publish_failed = AsyncMock()
    deps = PipelineConsumerDeps(
        forge_config=forge_config,
        is_duplicate_terminal=is_dup,
        dispatch_build=dispatch,
        publish_build_failed=publish_failed,
        register_ack_handle=register_ack_handle,
    )
    return deps, {
        "is_duplicate_terminal": is_dup,
        "dispatch_build": dispatch,
        "publish_build_failed": publish_failed,
    }


# ---------------------------------------------------------------------------
# AC-1 + AC-2: dispatch path registers a BuildAckHandle in the bridge
# ---------------------------------------------------------------------------


class TestAckHandleRegistration:
    """AC-1 + AC-2: dispatch stores a :class:`BuildAckHandle` in the registry.

    The registry call is keyed by ``(feature_id, correlation_id)``;
    ``msg.ack`` is NOT invoked on ``dispatch_build`` return. The
    lifecycle bridge owns the ack via the registered handle.
    """

    @pytest.mark.asyncio
    async def test_register_ack_handle_called_with_identity_pair(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # TASK-GATE-D659 R1: registration is DEFERRED. The consumer no
        # longer registers pre-dispatch; it hands dispatch_build a
        # ``register_observer`` closure (3rd arg) that dispatch_build
        # invokes only on the approve → launch path. Invoking the closure
        # is what performs the registration with the identity pair.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        register = AsyncMock()
        deps, mocks = _make_deps(forge_config, register_ack_handle=register)

        await handle_message(msg, deps)

        register.assert_not_awaited()
        args = mocks["dispatch_build"].await_args.args
        assert len(args) == 3, "consumer must pass the register_observer closure"
        register_observer = args[2]

        await register_observer()

        register.assert_awaited_once()
        feature_id, correlation_id, handle = register.await_args.args
        assert feature_id == FEATURE_ID
        assert correlation_id == CORRELATION_ID
        assert isinstance(handle, BuildAckHandle)

    @pytest.mark.asyncio
    async def test_msg_ack_not_called_on_dispatch_return_with_bridge(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # AC-1: when the bridge is wired, the consumer must NOT ack on
        # dispatch_build return — the ack is the bridge's responsibility
        # at terminal SSE arrival.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        register = AsyncMock()
        deps, mocks = _make_deps(forge_config, register_ack_handle=register)

        await handle_message(msg, deps)

        # dispatch_build returned cleanly, registry was called, but ack
        # MUST be deferred to the bridge.
        mocks["dispatch_build"].assert_awaited_once()
        msg.ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_handle_acks_underlying_msg(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # The lifecycle bridge will eventually call handle.ack() — that
        # MUST drive the underlying msg.ack exactly once. R1: the handle is
        # obtained by invoking the deferred register_observer closure.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        register = AsyncMock()
        deps, mocks = _make_deps(forge_config, register_ack_handle=register)

        await handle_message(msg, deps)
        register_observer = mocks["dispatch_build"].await_args.args[2]
        await register_observer()
        _, _, handle = register.await_args.args

        msg.ack.assert_not_called()
        await handle.ack()
        msg.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-2: BuildAckHandle exposes both ack() and nak()
# ---------------------------------------------------------------------------


class TestBridgeAckAndNak:
    """AC-2: handle exposes both ``ack()`` and ``nak()``; both idempotent."""

    @pytest.mark.asyncio
    async def test_handle_ack_is_idempotent(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        handle = make_msg_ack_handle(msg)

        await handle.ack()
        await handle.ack()
        await handle.ack()

        assert msg.ack.await_count == 1
        msg.nak.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_nak_drives_msg_nak(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        handle = make_msg_ack_handle(msg)

        await handle.nak()

        msg.nak.assert_awaited_once()
        msg.ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_nak_is_idempotent(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        handle = make_msg_ack_handle(msg)

        await handle.nak()
        await handle.nak()

        assert msg.nak.await_count == 1

    @pytest.mark.asyncio
    async def test_ack_after_nak_is_ignored(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        handle = make_msg_ack_handle(msg)

        await handle.nak()
        await handle.ack()  # mixed-mode: contract bug upstream, ignored

        msg.nak.assert_awaited_once()
        msg.ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_nak_after_ack_is_ignored(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        handle = make_msg_ack_handle(msg)

        await handle.ack()
        await handle.nak()  # mixed-mode: contract bug upstream, ignored

        msg.ack.assert_awaited_once()
        msg.nak.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_is_msgbuildackhandle_concrete_type(self) -> None:
        msg = AsyncMock()
        msg.ack = AsyncMock()
        handle = make_msg_ack_handle(msg)

        # The concrete type satisfies the BuildAckHandle Protocol AND
        # exposes the underlying msg for tests that want to verify the
        # binding (handle.msg is msg).
        assert isinstance(handle, BuildAckHandle)
        assert isinstance(handle, MsgBuildAckHandle)
        assert handle.msg is msg


# ---------------------------------------------------------------------------
# AC-3: F010F sync-raise fallback when no bridge is wired
# ---------------------------------------------------------------------------


class TestF010FFallback:
    """AC-3: ``register_ack_handle=None`` preserves existing F010F semantics."""

    @pytest.mark.asyncio
    async def test_no_registration_when_bridge_is_none(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        deps, mocks = _make_deps(forge_config, register_ack_handle=None)

        await handle_message(msg, deps)

        # No bridge → no registration call. Dispatch still ran.
        mocks["dispatch_build"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_receives_ack_callback_in_fallback(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # F010F preserves the AC-009 contract: dispatch_build is handed
        # an idempotent ack_callback bound to msg.ack.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        deps, mocks = _make_deps(forge_config, register_ack_handle=None)

        await handle_message(msg, deps)

        _, ack_callback = mocks["dispatch_build"].await_args.args
        assert callable(ack_callback)
        msg.ack.assert_not_called()
        await ack_callback()
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_raise_acks_and_publishes_in_fallback(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # F010F sync-raise: when dispatch_build raises, the consumer
        # publishes build-failed AND acks (the existing contract from
        # TASK-FORGE-FRR-F010F is unchanged in the fallback path).
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        deps, mocks = _make_deps(forge_config, register_ack_handle=None)
        mocks["dispatch_build"].side_effect = RuntimeError("boom")

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        msg.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-4: duplicate-detection unchanged (acked + skipped, no registration)
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """AC-4: duplicate envelopes are acked-and-skipped without re-registering."""

    @pytest.mark.asyncio
    async def test_duplicate_terminal_acks_immediately_no_registration(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        register = AsyncMock()
        deps, mocks = _make_deps(
            forge_config,
            is_duplicate_terminal=True,
            register_ack_handle=register,
        )

        await handle_message(msg, deps)

        # Duplicate path: ack immediately, never call dispatch, never
        # register a handle (no in-flight build to track).
        msg.ack.assert_awaited_once()
        mocks["dispatch_build"].assert_not_called()
        register.assert_not_called()
        mocks["publish_build_failed"].assert_not_called()


# ---------------------------------------------------------------------------
# AC-5: F010C correlation-id AST guard — every consumer emit threads it
# ---------------------------------------------------------------------------


class TestCorrelationIdAstGuardCompat:
    """AC-5: every consumer emit site threads ``correlation_id=`` explicitly."""

    @pytest.mark.asyncio
    async def test_publish_build_failed_called_with_correlation_id_kw(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # On the fallback dispatch-raise path the consumer publishes
        # build-failed with correlation_id threaded as a keyword arg.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        deps, mocks = _make_deps(forge_config, register_ack_handle=None)
        mocks["dispatch_build"].side_effect = RuntimeError("boom")

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        kwargs = mocks["publish_build_failed"].await_args.kwargs
        assert "correlation_id" in kwargs
        assert kwargs["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_bridge_path_publishes_no_failure_envelope(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # Sanity: the happy bridge path publishes nothing — terminal
        # envelopes are the bridge's responsibility, not the consumer's.
        msg = _make_msg(_envelope_bytes(_valid_payload(feature_yaml)))
        register = AsyncMock()
        deps, mocks = _make_deps(forge_config, register_ack_handle=register)

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_not_called()
