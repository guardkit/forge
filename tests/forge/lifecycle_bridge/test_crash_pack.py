"""A run that ends with no terminal still leaves a readable failure pack.

19 October 2026, build FEAT-B874: the runner's ``running_wave`` node raised,
the run ended in an error, the coordinator published a synthetic
``build-failed`` (``stream-ended-without-terminal``), and the repair was
refused because "the failure pack has no readable manifest".

These tests drive the coordinator's real no-terminal path, with the
production pack writer bound to a real SQLite build row, and then the repair
admission's real base resolution:

* the pack is readable by ``read_failure_pack``, says the runner crashed and
  why (credentials replaced), records the row's branch and the tasks the last
  state reported, and says nothing verified a candidate;
* the repair is refused for that stated reason, never "no readable manifest";
* a pack this run already has is never replaced, and an error text is cut.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import BuildQueuedPayload

from forge import receipts as forge_receipts
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_production import _build_crash_pack_writer
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.lifecycle_bridge.crash_pack import (
    CANDIDATE_NOT_VERIFIED,
    TASKS_SOURCE_HEARTBEAT,
    write_crash_pack,
)
from forge.lifecycle_bridge.run_state_source import RunStateSnapshot, _task_error
from forge.lifecycle_bridge.translation import StreamEventTranslator
from forge.lifecycle_bridge.wireup import (
    STREAM_NO_TERMINAL_FAILURE_REASON,
    LifecycleBridgeWireup,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle
from forge.pipeline.fix_admission import FixAdmissionRefused, resolve_repair_base
from forge.pipeline.fix_journey_receipts import read_failure_pack
from forge.pipeline.fix_row_producer import SOURCE_BUILD_FAILED
from forge.subagents import autobuild_runner as ar

_FEATURE_ID = "FEAT-B874"
_CORRELATION_ID = "corr-b874"
_REPO = "appmilla/ts-api-test"
_RUNNER_ERROR = (
    "running_wave: ValueError('Separator is not found, and chunk exceed the "
    "limit') password=hunter2"
)


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx: sqlite3.Connection = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


@pytest.fixture()
def receipts(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "project-receipts"
    forge_receipts.use_project_receipts({_REPO: root})
    try:
        yield root
    finally:
        forge_receipts.use_project_receipts({})


def _queued_build(persistence: SqliteLifecyclePersistence) -> str:
    now = datetime.now(UTC)
    return persistence.record_pending_build(
        BuildQueuedPayload(
            feature_id=_FEATURE_ID,
            repo=_REPO,
            feature_yaml_path=f".guardkit/features/{_FEATURE_ID}.yaml",
            triggered_by="cli",
            correlation_id=_CORRELATION_ID,
            requested_at=now,
            queued_at=now,
        )
    )


def _publisher() -> MagicMock:
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


def _empty_stream(*, feature_id: str, thread_id: str, run_id: str) -> Any:
    async def gen() -> AsyncIterator[StreamPart]:
        return
        yield  # pragma: no cover

    return gen()


async def _identity(_feature_id: str, _correlation_id: str = "") -> tuple[str, str]:
    return ("thread-x", "run-x")


def _errored_run_fetcher(build_id: str) -> Any:
    """The run ended in an error while its last state was still running."""

    async def _fetch(**_kwargs: Any) -> RunStateSnapshot:
        return RunStateSnapshot(
            status="error",
            values={
                "async_tasks": {
                    _FEATURE_ID: {
                        "feature_id": _FEATURE_ID,
                        "build_id": build_id,
                        "lifecycle": "running_wave",
                        "wave_index": 1,
                        "task_index": 0,
                        "tasks_completed": 2,
                        "tasks_failed": 0,
                    }
                }
            },
            error=_RUNNER_ERROR,
        )

    return _fetch


async def _run_no_terminal_path(
    persistence: SqliteLifecyclePersistence, build_id: str
) -> MagicMock:
    async def _resolver(_feature_id: str, _correlation_id: str) -> str:
        return build_id

    publisher = _publisher()
    wireup = LifecycleBridgeWireup(
        bridge=LifecycleBridge(registry=BridgeRegistry(connection=persistence.connection)),
        translator=StreamEventTranslator(),
        publisher=publisher,
        stream_source=_empty_stream,
        identity_provider=_identity,
        run_state_fetcher=_errored_run_fetcher(build_id),
        build_state_recorder=build_build_state_recorder(persistence),
        build_id_resolver=_resolver,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
        crash_pack_writer=_build_crash_pack_writer(sqlite_pool=persistence),
    )
    handle = AsyncMock(spec=BuildAckHandle)
    await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
    task = wireup.get_observer_task(_FEATURE_ID)
    if task is not None:
        await asyncio.wait_for(task, timeout=5.0)
    await wireup.shutdown()
    return publisher


class TestTheNoTerminalPathLeavesAPack:
    @pytest.mark.asyncio
    async def test_the_pack_is_readable_and_the_repair_names_the_crash(
        self,
        persistence: SqliteLifecyclePersistence,
        receipts: Path,
        tmp_path: Path,
    ) -> None:
        build_id = _queued_build(persistence)
        # What the runner had already saved before it died.
        (receipts / build_id).mkdir(parents=True)
        (receipts / build_id / ar.STDOUT_LOG_NAME).write_text(
            "Carried the builder's already-satisfied claim through for "
            "TASK-DEP-003 turn 1\n",
            encoding="utf-8",
        )

        publisher = await _run_no_terminal_path(persistence, build_id)

        # The wire is unchanged: the same synthetic build-failed.
        sent = publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON

        pack = read_failure_pack(build_id, receipts_root=receipts)
        assert pack is not None and pack.has_manifest
        assert pack.terminal == ar.TERMINAL_RUNNER_CRASHED
        assert pack.raw["build_id"] == build_id
        assert pack.feature_id == _FEATURE_ID
        assert pack.correlation_id == _CORRELATION_ID
        row = persistence.get_build_row(build_id)
        assert row is not None and pack.branch == row.branch
        assert "stopped without reporting an end (run status: error)" in pack.reason
        assert "Separator is not found" in pack.reason
        assert "hunter2" not in json.dumps(pack.raw)
        assert pack.raw["runner_error"].startswith("running_wave: ValueError(")
        assert pack.raw["tasks_completed"] == 2
        assert pack.raw["tasks_completed_source"] == "last-reported-state"
        assert pack.evidence is not None
        assert pack.evidence["subprocess_ran"] is None
        assert pack.evidence["worktree_kept"] is False
        assert pack.stdout_log is not None
        assert pack.raw["candidate"] == {
            "verified": False,
            "branch": None,
            "why": CANDIDATE_NOT_VERIFIED,
        }

        with pytest.raises(FixAdmissionRefused) as caught:
            await resolve_repair_base(
                minted={"source": SOURCE_BUILD_FAILED},
                branch="main",
                parent_feature=_FEATURE_ID,
                source_build_id=build_id,
                source_build=row,
                repo_path=tmp_path,
                receipts_root=receipts,
                sidecar=None,
            )
        message = caught.value.message
        assert "no readable manifest" not in message
        assert "ended without a terminal" in message
        assert "no verified candidate to repair" in message
        assert "Separator is not found" in message
        assert caught.value.reason == "repair-base"
        assert caught.value.permanent is True

    @pytest.mark.asyncio
    async def test_without_a_pack_the_old_refusal_still_stands(
        self,
        persistence: SqliteLifecyclePersistence,
        receipts: Path,
        tmp_path: Path,
    ) -> None:
        # The control: the refusal this lane replaces, when nothing is written.
        build_id = _queued_build(persistence)
        with pytest.raises(FixAdmissionRefused) as caught:
            await resolve_repair_base(
                minted={"source": SOURCE_BUILD_FAILED},
                branch="main",
                parent_feature=_FEATURE_ID,
                source_build_id=build_id,
                source_build=persistence.get_build_row(build_id),
                repo_path=tmp_path,
                receipts_root=receipts,
                sidecar=None,
            )
        assert "no readable manifest" in caught.value.message


class TestTheWriter:
    def test_task_counts_come_from_the_last_heartbeat_first(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "build-1"
        pack_dir.mkdir()
        (pack_dir / ar.IN_FLIGHT_STATE_NAME).write_text(
            json.dumps(
                {
                    "build_id": "build-1",
                    "last_task_id": "TASK-DEP-003",
                    "tasks_completed": 3,
                    "tasks_failed": 1,
                    "current_wave": 2,
                }
            ),
            encoding="utf-8",
        )
        path = write_crash_pack(
            build_id="build-1",
            feature_id="FEAT-X",
            correlation_id="corr-1",
            receipts_root=tmp_path,
            branch="main",
        )
        assert path is not None
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["tasks_completed"] == 3
        assert manifest["tasks_failed"] == 1
        assert manifest["tasks_completed_source"] == TASKS_SOURCE_HEARTBEAT
        assert manifest["last_heartbeat"]["last_task_id"] == "TASK-DEP-003"
        assert "could not be read" in manifest["reason"]
        assert manifest["runner_error"] is None

    def test_this_runs_own_pack_is_never_replaced(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "build-1"
        pack_dir.mkdir()
        own = {"build_id": "build-1", "correlation_id": "corr-1", "reason": "real"}
        (pack_dir / ar.FAILURE_MANIFEST_NAME).write_text(json.dumps(own))
        assert (
            write_crash_pack(
                build_id="build-1",
                feature_id="FEAT-X",
                correlation_id="corr-1",
                receipts_root=tmp_path,
            )
            is None
        )
        assert json.loads((pack_dir / ar.FAILURE_MANIFEST_NAME).read_text()) == own

    def test_an_earlier_runs_pack_is_archived_not_trusted(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "build-1"
        pack_dir.mkdir()
        (pack_dir / ar.FAILURE_MANIFEST_NAME).write_text(
            json.dumps({"build_id": "build-1", "correlation_id": "corr-OLD"})
        )
        path = write_crash_pack(
            build_id="build-1",
            feature_id="FEAT-X",
            correlation_id="corr-NEW",
            receipts_root=tmp_path,
        )
        assert path is not None
        assert json.loads(path.read_text())["correlation_id"] == "corr-NEW"
        assert len(list(pack_dir.glob("failure-manifest.*.json"))) == 1

    def test_a_long_error_is_cut(self, tmp_path: Path) -> None:
        path = write_crash_pack(
            build_id="build-1",
            feature_id="FEAT-X",
            correlation_id="corr-1",
            receipts_root=tmp_path,
            runner_error="boom " * 10_000,
        )
        assert path is not None
        manifest = json.loads(path.read_text())
        assert len(manifest["runner_error"]) < ar.ERROR_TEXT_LIMIT + 100
        assert "[cut:" in manifest["runner_error"]


class TestTheRunErrorIsRead:
    def test_the_first_task_error_is_named(self) -> None:
        state = {
            "values": {},
            "tasks": [
                {"id": "1", "name": "planning_waves", "error": None},
                {"id": "2", "name": "running_wave", "error": "ValueError('x')"},
            ],
        }
        assert _task_error(state) == "running_wave: ValueError('x')"

    def test_no_tasks_or_an_odd_shape_gives_none(self) -> None:
        assert _task_error({"values": {}}) is None
        assert _task_error({"tasks": "nonsense"}) is None
        assert _task_error(object()) is None
