"""TASK-FRR-PEB-005 — F010F coexistence boundary tests.

The lifecycle bridge's async-terminal publish path and F010F's sync-raise
safety-net publish path must coexist without ever putting two terminal
``build-failed`` envelopes on the wire for the same
``(feature_id, correlation_id)`` pair.

Test classes map 1:1 to the task's acceptance criteria:

* :class:`TestSyncRaiseUsesSafetyNetNotBridge` — AC-1: when
  ``dispatch_build`` raises synchronously, the bridge's ``attach()`` is
  never called (the registry stays empty) and F010F's safety-net publish
  fires exactly one ``build-failed`` envelope. No ``build-started`` is
  published.
* :class:`TestBridgeFirstThenSyncRaiseSkipsSafetyNet` — AC-2: when the
  bridge marks "terminal-published" before a delayed sync-raise fires,
  the safety-net path observes the claim and skips its emit; exactly one
  envelope reaches the wire.
* :class:`TestFirstWinsInvariant` — AC-3: regardless of ordering
  (bridge-first / F010F-first / concurrent ``asyncio.gather``), the
  ledger's atomic claim guarantees exactly one envelope per build.
* :class:`TestF010fRegressionStillPasses` — AC-4: when no ledger is
  wired (the legacy F010F-only configuration), the safety-net path
  publishes unchanged. Pins the no-regression contract on the bridge-less
  unit-test path.
* :class:`TestApplyMigrationIdempotent` — supports AC-2 / AC-3: the
  migration is applied at boot and re-running it is a no-op.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildFailedPayload

from forge.adapters.nats.pipeline_consumer import (
    PipelineConsumerDeps,
    handle_message,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps import _build_publish_build_failed
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PipelineConfig,
)
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import (
    BuildContext,
    LifecycleBridge,
)
from forge.lifecycle_bridge.coexistence import (
    CLAIMER_BRIDGE_TERMINAL,
    CLAIMER_F010F_SAFETY_NET,
    TABLE_NAME,
    TerminalPublishLedger,
    apply_migration,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry


# ---------------------------------------------------------------------------
# Test identities
# ---------------------------------------------------------------------------

FEATURE_ID = "FEAT-C0EX01"
CORRELATION_ID = "corr-coex-7c2f9a55"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Boot the lifecycle + bridge + coexistence schema on a temp DB."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    apply_migration(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def ledger(writer_db: sqlite3.Connection) -> TerminalPublishLedger:
    return TerminalPublishLedger(connection=writer_db)


@pytest.fixture()
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def bridge(registry: BridgeRegistry) -> LifecycleBridge:
    return LifecycleBridge(registry=registry)


@pytest.fixture()
def allowlist_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    return root.resolve()


@pytest.fixture()
def feature_yaml(allowlist_root: Path) -> Path:
    yaml_path = allowlist_root / "feature.yaml"
    yaml_path.write_text("# placeholder", encoding="utf-8")
    return yaml_path


@pytest.fixture()
def forge_config(allowlist_root: Path) -> ForgeConfig:
    return ForgeConfig(
        pipeline=PipelineConfig(),
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[allowlist_root]),
        ),
    )


# ---------------------------------------------------------------------------
# Helpers — envelope construction mirrors test_pipeline_consumer_dispatch_failure_publish.py
# ---------------------------------------------------------------------------


def _envelope_bytes(payload: dict[str, Any]) -> bytes:
    envelope = MessageEnvelope(
        message_id="msg-coex-test",
        timestamp=datetime.now(UTC),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=CORRELATION_ID,
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
        "correlation_id": CORRELATION_ID,
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(UTC).isoformat(),
        "queued_at": datetime.now(UTC).isoformat(),
    }


def _make_msg(data: bytes) -> AsyncMock:
    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    return msg


def _make_publisher(side_effect: Exception | None = None) -> MagicMock:
    """Return a publisher mock whose ``publish_build_failed`` is async."""
    publisher = MagicMock()
    if side_effect is not None:
        publisher.publish_build_failed = AsyncMock(side_effect=side_effect)
    else:
        publisher.publish_build_failed = AsyncMock(return_value=None)
    return publisher


def _make_failure_payload() -> BuildFailedPayload:
    return BuildFailedPayload(
        feature_id=FEATURE_ID,
        build_id=FEATURE_ID,
        failure_reason="AttributeError: synthetic dispatch failure",
        recoverable=False,
    )


def _make_build_context() -> BuildContext:
    return BuildContext(
        feature_id=FEATURE_ID,
        thread_id="thread-coex",
        run_id="run-coex",
        correlation_id=CORRELATION_ID,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


# ---------------------------------------------------------------------------
# AC-1: sync-raise still uses F010F safety-net, not the bridge.
# ---------------------------------------------------------------------------


class TestSyncRaiseUsesSafetyNetNotBridge:
    """AC-1 — synchronous dispatch raise → safety-net publish, no bridge attach.

    The legacy F010F shape is preserved: the consumer's outer
    try/except (``handle_message`` in ``pipeline_consumer.py``) catches
    the synchronous exception, calls the wrapped
    ``publish_build_failed`` (which is the F010F safety-net), and acks.
    The bridge's ``attach()`` must NOT be invoked because the dispatch
    never reached the running state machine.
    """

    @pytest.mark.asyncio
    async def test_sync_raise_publishes_build_failed_and_does_not_attach_bridge(
        self,
        forge_config: ForgeConfig,
        feature_yaml: Path,
        ledger: TerminalPublishLedger,
        bridge: LifecycleBridge,
        registry: BridgeRegistry,
    ) -> None:
        # Spy on bridge.attach so we can assert "never called".
        attach_spy = MagicMock(wraps=bridge.attach)
        bridge.attach = attach_spy  # type: ignore[method-assign]

        # Wire the publisher and the ledger-aware wrapper exactly as
        # production does (TASK-FRR-PEB-005 hooks into _build_publish_build_failed).
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )

        # Arrange: dispatch_build raises synchronously (the empirical
        # F010.E shape — AttributeError before any state machine
        # transition).
        dispatch_build = AsyncMock(
            side_effect=AttributeError(
                "'StructuredTool' object has no attribute 'start_async_task'"
            )
        )
        deps = PipelineConsumerDeps(
            forge_config=forge_config,
            is_duplicate_terminal=AsyncMock(return_value=False),
            dispatch_build=dispatch_build,
            publish_build_failed=wrapped_publish,
        )

        msg = _make_msg(_envelope_bytes(_valid_payload_dict(feature_yaml)))

        # Act
        await handle_message(msg, deps)

        # Assert — F010F safety-net publish fired exactly once
        publisher.publish_build_failed.assert_awaited_once()
        published_payload: BuildFailedPayload = (
            publisher.publish_build_failed.await_args.args[0]
        )
        assert published_payload.feature_id == FEATURE_ID
        assert "AttributeError" in published_payload.failure_reason

        # AC-1 — bridge.attach() must NOT be invoked. The bridge owns
        # async-terminal only; sync-raise stays on the F010F path.
        attach_spy.assert_not_called()

        # AC-1 — registry is empty; no bridge-side row was written.
        assert registry.list_active(correlation_id=CORRELATION_ID) == []

        # AC-1 — the inbound message was acked so JetStream releases the
        # max_ack_pending=1 slot for the next build.
        msg.ack.assert_awaited_once()

        # The ledger now reflects the F010F path winning the claim. The
        # bridge would observe ``is_claimed == True`` if it tried to
        # publish later for the same identity — first-wins.
        assert (
            ledger.is_claimed(
                feature_id=FEATURE_ID,
                correlation_id=CORRELATION_ID,
            )
            is True
        )
        claim = ledger.get(
            feature_id=FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )
        assert claim is not None
        assert claim.claimed_by == CLAIMER_F010F_SAFETY_NET


# ---------------------------------------------------------------------------
# AC-2: bridge claims first → safety-net skips its emit.
# ---------------------------------------------------------------------------


class TestBridgeFirstThenSyncRaiseSkipsSafetyNet:
    """AC-2 — bridge marks "terminal-published"; delayed sync-raise skips.

    Models the live race we want to make impossible: the bridge sees a
    terminal SSE event, claims the slot, invokes ack — and then a delayed
    synchronous exception fires for the same ``(feature_id,
    correlation_id)``. The safety-net wrapper consults the ledger,
    observes the claim, and stops without putting a second envelope on
    the wire.
    """

    @pytest.mark.asyncio
    async def test_bridge_claim_then_safety_net_skip(
        self,
        ledger: TerminalPublishLedger,
    ) -> None:
        # 1. Bridge wins the race and marks terminal-published.
        bridge_won = ledger.claim(
            feature_id=FEATURE_ID,
            correlation_id=CORRELATION_ID,
            claimed_by=CLAIMER_BRIDGE_TERMINAL,
        )
        assert bridge_won is True

        # 2. F010F safety-net fires later (delayed sync-raise).
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )
        await wrapped_publish(
            _make_failure_payload(),
            FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )

        # AC-2 — exactly one envelope on the wire (the bridge's
        # imagined publish, NOT this one). The safety-net wrapper
        # observed the claim and short-circuited.
        publisher.publish_build_failed.assert_not_awaited()

        # The ledger row is still owned by the bridge — second writer
        # cannot overwrite ``claimed_by``.
        claim = ledger.get(
            feature_id=FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )
        assert claim is not None
        assert claim.claimed_by == CLAIMER_BRIDGE_TERMINAL


# ---------------------------------------------------------------------------
# AC-3: first-wins invariant across orderings (bridge-first / F010F-first / concurrent).
# ---------------------------------------------------------------------------


class TestFirstWinsInvariant:
    """AC-3 — exactly one envelope regardless of ordering."""

    @pytest.mark.asyncio
    async def test_bridge_first_then_safety_net_publishes_zero(
        self, ledger: TerminalPublishLedger
    ) -> None:
        # Bridge wins.
        assert (
            ledger.claim(
                feature_id=FEATURE_ID,
                correlation_id=CORRELATION_ID,
                claimed_by=CLAIMER_BRIDGE_TERMINAL,
            )
            is True
        )

        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )
        await wrapped_publish(
            _make_failure_payload(),
            FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )
        publisher.publish_build_failed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safety_net_first_then_bridge_claim_loses(
        self, ledger: TerminalPublishLedger
    ) -> None:
        # F010F wins via the wrapper.
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )
        await wrapped_publish(
            _make_failure_payload(),
            FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )
        publisher.publish_build_failed.assert_awaited_once()

        # Bridge's imagined later claim loses — it MUST observe ``False``
        # so the bridge knows to skip its own publish.
        bridge_won = ledger.claim(
            feature_id=FEATURE_ID,
            correlation_id=CORRELATION_ID,
            claimed_by=CLAIMER_BRIDGE_TERMINAL,
        )
        assert bridge_won is False

    @pytest.mark.asyncio
    async def test_concurrent_gather_produces_exactly_one_envelope(
        self, ledger: TerminalPublishLedger
    ) -> None:
        """Concurrent fire-and-forget — the ledger's atomic claim wins exactly once.

        Models the worst-case race: the bridge's terminal-observation
        coroutine and a delayed sync-raise's safety-net coroutine both
        scheduled on the same event loop, awaited together via
        ``asyncio.gather``. The ledger's ``BEGIN IMMEDIATE`` +
        ``INSERT OR IGNORE`` serialises them — exactly one wins.
        """
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )

        async def bridge_publish() -> bool:
            # The bridge claims the slot and then "publishes" via its
            # own publisher. We model the publish as a no-op on a
            # second mock to keep the count comparable.
            won = ledger.claim(
                feature_id=FEATURE_ID,
                correlation_id=CORRELATION_ID,
                claimed_by=CLAIMER_BRIDGE_TERMINAL,
            )
            if won:
                # Stand-in for the bridge's own publisher emit. We
                # account for it in ``total_envelopes`` below.
                return True
            return False

        async def f010f_publish() -> int:
            await wrapped_publish(
                _make_failure_payload(),
                FEATURE_ID,
                correlation_id=CORRELATION_ID,
            )
            return publisher.publish_build_failed.await_count

        bridge_won, f010f_publish_count = await asyncio.gather(
            bridge_publish(),
            f010f_publish(),
        )

        # Convert "bridge won" to its envelope count.
        bridge_envelope_count = 1 if bridge_won else 0

        total_envelopes = bridge_envelope_count + f010f_publish_count
        assert total_envelopes == 1, (
            f"first-wins violated: bridge_envelopes={bridge_envelope_count} "
            f"f010f_envelopes={f010f_publish_count} (total must be 1)"
        )

        # Exactly one ledger row, regardless of who won.
        cursor = ledger._cx.execute(  # type: ignore[attr-defined]
            f"SELECT COUNT(*) FROM {TABLE_NAME} "
            f"WHERE feature_id = ? AND correlation_id = ?",
            (FEATURE_ID, CORRELATION_ID),
        )
        (row_count,) = cursor.fetchone()
        assert row_count == 1


# ---------------------------------------------------------------------------
# AC-4: F010F regression suite — the bridge-less wrapper still publishes.
# ---------------------------------------------------------------------------


class TestF010fRegressionStillPasses:
    """AC-4 — without a wired ledger, the wrapper publishes unchanged.

    F010F's existing tests (e.g.
    ``tests/forge/test_pipeline_consumer_dispatch_failure_publish.py``)
    construct ``_build_publish_build_failed(publisher)`` without a
    ledger. The new ledger plumbing is **opt-in**: when the parameter
    is omitted, the wrapper's behaviour is byte-for-byte unchanged.
    """

    @pytest.mark.asyncio
    async def test_no_ledger_publishes_unconditionally(self) -> None:
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(publisher)

        await wrapped_publish(
            _make_failure_payload(),
            FEATURE_ID,
            correlation_id=CORRELATION_ID,
        )

        # The wrapper publishes the F010F safety-net envelope. This is
        # the byte-for-byte legacy behaviour the F010F suite relies on.
        publisher.publish_build_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_envelope_correlation_id_none_publishes(
        self, ledger: TerminalPublishLedger
    ) -> None:
        """``correlation_id=None`` (malformed-envelope path) bypasses the claim.

        Without a real correlation_id there is no
        ``(feature_id, correlation_id)`` pair the bridge could have
        observed. The wrapper publishes unconditionally on this path so
        the malformed-envelope rejection still surfaces as a
        ``build-failed`` envelope.
        """
        publisher = _make_publisher()
        wrapped_publish = _build_publish_build_failed(
            publisher,
            terminal_publish_ledger=ledger,
        )

        await wrapped_publish(
            _make_failure_payload(),
            FEATURE_ID,
            correlation_id=None,
        )

        publisher.publish_build_failed.assert_awaited_once()
        # The ledger is untouched on the malformed path.
        assert (
            ledger.is_claimed(
                feature_id=FEATURE_ID,
                correlation_id=CORRELATION_ID,
            )
            is False
        )


# ---------------------------------------------------------------------------
# Migration idempotency — supports AC-2 / AC-3 (boot path).
# ---------------------------------------------------------------------------


class TestApplyMigrationIdempotent:
    """``apply_migration`` is safe to invoke on every ``forge serve`` boot."""

    def test_apply_migration_creates_table_when_absent(
        self, tmp_path: Path
    ) -> None:
        cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
        try:
            apply_migration(cx)
            row = cx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE_NAME,),
            ).fetchone()
            assert row is not None and row[0] == TABLE_NAME
        finally:
            cx.close()

    def test_apply_migration_is_idempotent(self, tmp_path: Path) -> None:
        cx = sqlite_connect.connect_writer(tmp_path / "rerun.db")
        try:
            apply_migration(cx)
            apply_migration(cx)  # second invocation must not raise.
            # And the schema is still queryable.
            ledger = TerminalPublishLedger(connection=cx)
            assert (
                ledger.is_claimed(
                    feature_id=FEATURE_ID,
                    correlation_id=CORRELATION_ID,
                )
                is False
            )
        finally:
            cx.close()

    def test_apply_migration_rejects_non_connection(self) -> None:
        with pytest.raises(TypeError, match="sqlite3.Connection"):
            apply_migration("not-a-connection")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ledger argument validation — guard the public surface.
# ---------------------------------------------------------------------------


class TestLedgerArgumentValidation:
    """Empty arguments must surface as ``ValueError`` at the boundary."""

    def test_claim_rejects_empty_feature_id(
        self, ledger: TerminalPublishLedger
    ) -> None:
        with pytest.raises(ValueError, match="feature_id"):
            ledger.claim(
                feature_id="",
                correlation_id=CORRELATION_ID,
                claimed_by=CLAIMER_BRIDGE_TERMINAL,
            )

    def test_claim_rejects_empty_correlation_id(
        self, ledger: TerminalPublishLedger
    ) -> None:
        with pytest.raises(ValueError, match="correlation_id"):
            ledger.claim(
                feature_id=FEATURE_ID,
                correlation_id="",
                claimed_by=CLAIMER_BRIDGE_TERMINAL,
            )

    def test_claim_rejects_empty_claimed_by(
        self, ledger: TerminalPublishLedger
    ) -> None:
        with pytest.raises(ValueError, match="claimed_by"):
            ledger.claim(
                feature_id=FEATURE_ID,
                correlation_id=CORRELATION_ID,
                claimed_by="",
            )

    def test_constructor_rejects_non_connection(self) -> None:
        with pytest.raises(TypeError, match="sqlite3.Connection"):
            TerminalPublishLedger(connection="not-a-connection")  # type: ignore[arg-type]
