"""TASK-FORGE-FRR-F010C — outbound ``pipeline.*`` envelopes thread inbound ``correlation_id``.

DDR-029's notification-thread contract requires every outbound lifecycle
envelope (``build-started``, ``stage-complete``, ``build-complete``,
``build-failed``) to carry the **same** ``correlation_id`` as the inbound
``build-queued`` envelope that triggered it. Without this, jarvis's
``forge_subscriber`` cannot route the notification back to the originating
chat session — the envelope is visible-on-the-wire-but-unrouteable.

This module pins the threading invariant for the consumer-side rejection
paths (where the bug lived prior to this task) and the production wrapper
that adapts the consumer's ``PublishBuildFailed`` Protocol onto the
shared :class:`PipelinePublisher`.

Test classes map 1:1 to the task's acceptance criteria:

* ``TestPathRejectionThreadsCorrelationId`` — AC-2: the empirical case
  reproduced as ``correlation_id 21df1258-…`` on 2026-05-04.
* ``TestProductionWrapperThreadsCorrelationId`` — AC-3: the production
  wrapper roundtrip in :func:`forge.cli._serve_deps._build_publish_build_failed`.
  The wrapper is what every consumer-side rejection path actually calls
  in production; covering it here closes the gap between the consumer's
  ``PublishBuildFailed`` Protocol and the publisher's wire emission.
* ``TestAllRejectionPathsThreadCorrelationId`` — AC-4: parametrised
  cross-cut over every rejection path in
  :func:`forge.adapters.nats.pipeline_consumer.handle_message`, plus a
  lint-style guard that grep-detects future ``_safe_publish_failure``
  call sites missing the ``correlation_id=`` kwarg.
"""

from __future__ import annotations

import ast
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
# Fixtures
# ---------------------------------------------------------------------------


INBOUND_CORRELATION_ID = "21df1258-63cb-4e8a-9bef-89234833b68e"
"""The empirical inbound correlation_id reproduced on 2026-05-04 (run 1)."""


@pytest.fixture
def allowlist_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def forge_config(allowlist_root: Path) -> ForgeConfig:
    return ForgeConfig(
        pipeline=PipelineConfig(),
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[allowlist_root]),
        ),
    )


@pytest.fixture
def deps_factory(forge_config: ForgeConfig):
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


def _envelope_bytes(
    payload: dict[str, Any],
    *,
    correlation_id: str = INBOUND_CORRELATION_ID,
) -> bytes:
    envelope = MessageEnvelope(
        message_id="msg-correlation-test",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=correlation_id,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _valid_payload_dict(yaml_path: Path) -> dict[str, Any]:
    return {
        "feature_id": "FEAT-43DE",
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


# ---------------------------------------------------------------------------
# AC-2: path-allowlist rejection threads inbound correlation_id
# ---------------------------------------------------------------------------


class TestPathRejectionThreadsCorrelationId:
    """AC-2 — the empirical case from 2026-05-04 (correlation_id 21df1258-…).

    The inbound envelope carries ``correlation_id`` 21df1258-…; the path
    is outside the allowlist so the consumer rejects on the path-validation
    path. The outbound ``build-failed`` MUST carry the inbound
    ``correlation_id`` (DDR-029); prior to this task it carried ``None``.
    """

    @pytest.mark.asyncio
    async def test_path_outside_allowlist_threads_inbound_correlation_id(
        self, deps_factory, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside" / "feature.yaml"
        outside.parent.mkdir()
        msg = _make_msg(_envelope_bytes(_valid_payload_dict(outside)))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        # ``correlation_id`` is passed as a keyword argument so existing
        # positional unpacking ``(payload, feature_id) = await_args.args``
        # in legacy tests stays valid.
        kwargs = mocks["publish_build_failed"].await_args.kwargs
        assert "correlation_id" in kwargs, (
            "_safe_publish_failure must pass correlation_id as a kwarg "
            "(DDR-029); positional fallback is a regression"
        )
        assert kwargs["correlation_id"] == INBOUND_CORRELATION_ID


# ---------------------------------------------------------------------------
# AC-3: production wrapper roundtrip — every consumer-side rejection runs
# through this code path in production, so it MUST attach correlation_id.
# ---------------------------------------------------------------------------


class TestProductionWrapperThreadsCorrelationId:
    """AC-3 — the production wrapper in ``_serve_deps`` attaches
    ``correlation_id`` to the v1 ``BuildFailedPayload`` so the publisher's
    central envelope-construction path threads it onto the outbound
    envelope.

    The consumer's ``dispatch_build`` path itself does NOT publish
    ``build-failed`` per ADR-ARCH-008 — the running state machine owns
    that publish via the lifecycle emitter, which already threads
    ``correlation_id`` correctly. The boundary that bug F010C exposed
    is the consumer→wrapper→publisher chain on rejection; covering the
    wrapper here is the testable equivalent of AC-3's "any future
    inner-exception-during-dispatch path will hit" — any future publish
    site routed through this wrapper is forced to thread the kwarg.
    """

    @pytest.mark.asyncio
    async def test_wrapper_attaches_correlation_id_to_payload(self) -> None:
        from forge.cli._serve_deps import _build_publish_build_failed

        publisher = AsyncMock()
        publisher.publish_build_failed = AsyncMock()

        wrapper = _build_publish_build_failed(publisher)
        failure = BuildFailedPayload(
            feature_id="FEAT-43DE",
            build_id="FEAT-43DE",
            failure_reason="path outside allowlist",
            recoverable=False,
            failed_task_id=None,
        )

        await wrapper(failure, "FEAT-43DE", correlation_id=INBOUND_CORRELATION_ID)

        publisher.publish_build_failed.assert_awaited_once()
        sent_payload = publisher.publish_build_failed.await_args.args[0]
        # The publisher's central ``_publish_envelope`` reads correlation_id
        # off the payload via ``getattr(payload, "correlation_id", None)``;
        # the wrapper attaches it via ``attach_correlation_id`` so the
        # envelope carries the inbound value (DDR-029).
        assert getattr(sent_payload, "correlation_id", None) == (
            INBOUND_CORRELATION_ID
        )

    @pytest.mark.asyncio
    async def test_wrapper_correlation_id_none_does_not_attach(self) -> None:
        """``correlation_id=None`` on the malformed-envelope path is a
        no-op — the v1 payload remains correlation_id-less and the
        publisher's ``getattr`` returns ``None``, matching the v1 contract.
        """
        from forge.cli._serve_deps import _build_publish_build_failed

        publisher = AsyncMock()
        publisher.publish_build_failed = AsyncMock()

        wrapper = _build_publish_build_failed(publisher)
        failure = BuildFailedPayload(
            feature_id="unknown",
            build_id="unknown",
            failure_reason="malformed BuildQueuedPayload",
            recoverable=False,
            failed_task_id=None,
        )

        await wrapper(failure, "unknown", correlation_id=None)

        publisher.publish_build_failed.assert_awaited_once()
        sent_payload = publisher.publish_build_failed.await_args.args[0]
        assert getattr(sent_payload, "correlation_id", None) is None


# ---------------------------------------------------------------------------
# AC-4: cross-cut — every rejection path threads correlation_id, AND a
# lint-style guard so a future publish site cannot regress the invariant.
# ---------------------------------------------------------------------------


class TestAllRejectionPathsThreadCorrelationId:
    """AC-4 — the contract MUST NOT be lost again.

    The parametrised test exercises every rejection path in
    :func:`handle_message` (originator-not-recognised and path-outside-
    allowlist — both have a parsed envelope, so both threading paths are
    proven by one assertion shape). The malformed-envelope path is
    covered by the dedicated ``test_malformed_envelope_passes_none`` case
    because it is the only path where ``correlation_id=None`` is the
    correct value.

    The lint-style guard greps the consumer module and asserts every
    ``_safe_publish_failure(`` call site passes the ``correlation_id=``
    kwarg. A future PR that adds a new failure path without threading
    the field will fail this test.
    """

    @pytest.mark.asyncio
    async def test_originator_rejection_threads_correlation_id(
        self, deps_factory, allowlist_root: Path
    ) -> None:
        yaml_path = allowlist_root / "feature.yaml"
        bad = _valid_payload_dict(yaml_path)
        bad["originating_adapter"] = "not-on-the-allowlist"
        msg = _make_msg(_envelope_bytes(bad))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        kwargs = mocks["publish_build_failed"].await_args.kwargs
        assert kwargs.get("correlation_id") == INBOUND_CORRELATION_ID

    @pytest.mark.asyncio
    async def test_malformed_payload_with_parseable_envelope_threads_correlation_id(
        self, deps_factory
    ) -> None:
        # Envelope parses (so envelope.correlation_id is available) but
        # the inner payload is missing required fields.
        msg = _make_msg(_envelope_bytes({"feature_id": "FEAT-BROKEN"}))
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        kwargs = mocks["publish_build_failed"].await_args.kwargs
        assert kwargs.get("correlation_id") == INBOUND_CORRELATION_ID

    @pytest.mark.asyncio
    async def test_malformed_envelope_passes_none(self, deps_factory) -> None:
        # The envelope itself is unparseable — there is no source value
        # for correlation_id. This is the ONLY rejection path where
        # correlation_id=None on the outbound is acceptable.
        msg = _make_msg(b"this-is-not-json")
        deps, mocks = deps_factory()

        await handle_message(msg, deps)

        mocks["publish_build_failed"].assert_awaited_once()
        kwargs = mocks["publish_build_failed"].await_args.kwargs
        assert "correlation_id" in kwargs
        assert kwargs["correlation_id"] is None

    def test_every_safe_publish_failure_call_passes_correlation_id_kwarg(
        self,
    ) -> None:
        """Lint-style guard: every ``_safe_publish_failure(`` call in
        ``pipeline_consumer.py`` must pass ``correlation_id=`` explicitly.

        AST-based so nested ``_failure_payload(...)`` calls don't confuse
        the parser. This is the AC-4 cross-cut: a future PR that adds a
        new rejection path without threading the field MUST fail this
        test, even before any new behaviour test is written.
        """
        source_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "forge"
            / "adapters"
            / "nats"
            / "pipeline_consumer.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        offenders: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_safe_publish_failure"):
                continue
            keyword_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "correlation_id" not in keyword_names:
                offenders.append((node.lineno, ast.unparse(node)))

        assert not offenders, (
            "_safe_publish_failure call site(s) missing correlation_id= "
            "kwarg (DDR-029 — every outbound lifecycle envelope MUST "
            "carry the inbound correlation_id). If this is a new "
            "rejection path, thread envelope.correlation_id (or None for "
            "the malformed-envelope path).\n\nOffending call sites:\n"
            + "\n".join(f"  line {lineno}: {snippet}" for lineno, snippet in offenders)
        )

        # Sanity check: confirm the AST walk found the existing call
        # sites (otherwise the test is silently a no-op).
        all_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_safe_publish_failure"
        ]
        assert len(all_calls) >= 4, (
            f"expected ≥4 _safe_publish_failure call sites in "
            f"pipeline_consumer.py, found {len(all_calls)} — module "
            "restructured? Update this guard if the rejection-path count "
            "legitimately changed."
        )
