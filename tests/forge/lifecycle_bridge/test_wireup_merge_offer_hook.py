"""The wireup's merge-offer hook — the make-merge-work terminal seam.

Make-merge-work build spec (2026-08-24) piece 2, wireup half. Pins:

* the hook fires exactly once, with the typed terminal payload, AFTER the
  terminal publish succeeded (the BuildStateRecorder hop lives inside
  ``_publish_event``, so a successful publish implies the hop ran);
* it is fire-and-forget — a hook that raises never breaks the ack/detach
  terminal sequence;
* no hook (the default, every merge_executor.enabled=False deployment) is a
  strict no-op;
* a FAILED terminal publish never fires the hook (no offer for a terminal
  that is not yet on the wire).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import BuildCompletePayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
from forge.persistence.migrations import (
    lifecycle_bridge_registry as bridge_migration,
)
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle

FEATURE_ID = "FEAT-MOH1"
CORRELATION = "corr-moh1"
BUILD_ID = "build-FEAT-MOH1-20260824"


# ---------------------------------------------------------------------------
# Fixtures (the test_wireup.py harness, trimmed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def bridge(registry: BridgeRegistry) -> LifecycleBridge:
    return LifecycleBridge(registry=registry)


@pytest.fixture()
def translator() -> StreamEventTranslator:
    return StreamEventTranslator()


@pytest.fixture()
def fake_publisher() -> MagicMock:
    pub = MagicMock(name="PipelinePublisher")
    for name in (
        "publish_build_started",
        "publish_stage_complete",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_cancelled",
        "publish_build_progress",
    ):
        setattr(pub, name, AsyncMock(name=name))
    return pub


def _make_handle() -> BuildAckHandle:
    handle = AsyncMock(spec=BuildAckHandle)
    handle.ack = AsyncMock()
    handle.nak = AsyncMock()
    return handle


def _state_part(lifecycle: str, *, tasks_completed: int = 5) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                FEATURE_ID: {
                    "feature_id": FEATURE_ID,
                    "build_id": BUILD_ID,
                    "lifecycle": lifecycle,
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": 0,
                    "tasks_completed": tasks_completed,
                    "tasks_failed": 0,
                    "waiting_for": None,
                    "last_coach_score": None,
                }
            }
        },
        id=None,
    )


def _make_stream_source(parts: list[StreamPart]):
    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            for part in parts:
                yield part
                await asyncio.sleep(0)

        return gen()

    return factory


def _identity_provider():
    async def _provider(
        _feature_id: str, _correlation_id: str = ""
    ) -> tuple[str, str] | None:
        return ("thread-x", "run-x")

    return _provider


def _build_wireup(
    bridge: LifecycleBridge,
    translator: StreamEventTranslator,
    fake_publisher: MagicMock,
    *,
    parts: list[StreamPart],
    merge_offer_hook: Any = None,
) -> LifecycleBridgeWireup:
    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source(parts),
        identity_provider=_identity_provider(),
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
        merge_offer_hook=merge_offer_hook,
    )


async def _drive_to_terminal(
    wireup: LifecycleBridgeWireup, handle: BuildAckHandle
) -> None:
    await wireup.register_ack_handle(FEATURE_ID, CORRELATION, handle)
    task = wireup.get_observer_task(FEATURE_ID)
    if task is not None:
        await asyncio.wait_for(task, timeout=2.0)
    # Let the fire-and-forget offer task(s) run to completion.
    pending = list(wireup._merge_offer_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


_TERMINAL_PARTS = [_state_part("starting"), _state_part("completed")]


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


class TestMergeOfferHookSeam:
    @pytest.mark.asyncio
    async def test_hook_fires_once_with_the_terminal_payload(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        seen: list[Any] = []

        async def hook(event: Any) -> None:
            seen.append(event)

        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            parts=_TERMINAL_PARTS,
            merge_offer_hook=hook,
        )
        handle = _make_handle()
        await _drive_to_terminal(wireup, handle)

        assert len(seen) == 1
        payload = seen[0]
        assert isinstance(payload, BuildCompletePayload)
        assert payload.build_id == BUILD_ID
        assert payload.tasks_failed == 0
        # The terminal publish preceded the hook (published_ok gate).
        fake_publisher.publish_build_complete.assert_awaited_once()
        # And the terminal sequence still ran: ack + registry detach.
        handle.ack.assert_awaited_once()
        assert registry.list_active(correlation_id=CORRELATION) == []
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_a_raising_hook_never_breaks_ack_and_detach(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        async def hook(_event: Any) -> None:
            raise RuntimeError("offer exploded")

        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            parts=_TERMINAL_PARTS,
            merge_offer_hook=hook,
        )
        handle = _make_handle()
        await _drive_to_terminal(wireup, handle)

        handle.ack.assert_awaited_once()
        assert registry.list_active(correlation_id=CORRELATION) == []
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_no_hook_is_a_strict_no_op(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=_TERMINAL_PARTS
        )
        handle = _make_handle()
        await _drive_to_terminal(wireup, handle)

        handle.ack.assert_awaited_once()
        assert wireup._merge_offer_tasks == set()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_failed_terminal_publish_never_fires_the_hook(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        seen: list[Any] = []

        async def hook(event: Any) -> None:
            seen.append(event)

        fake_publisher.publish_build_complete.side_effect = RuntimeError("wire down")
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            parts=_TERMINAL_PARTS,
            merge_offer_hook=hook,
        )
        handle = _make_handle()
        await _drive_to_terminal(wireup, handle)

        assert seen == []
        # The existing publish-failure contract stands: no ack on a failed
        # terminal publish (JetStream redelivery retries it).
        handle.ack.assert_not_awaited()
        await wireup.shutdown()
