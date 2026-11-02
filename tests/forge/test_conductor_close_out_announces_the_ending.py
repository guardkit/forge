"""A fix journey's ending is announced before its queued message is released.

1 November 2026, sixth measurement run: repair build
``fix-build-FEAT-689D-20261101202307`` stopped at its 7,200-second cap. No
budget-pause collaborator is wired in production, so the driver closed it
out FAILED and released the queued message — and nothing published
``pipeline.build-failed``. The lifecycle bridge stands down for a fix
journey, so the close-out is the only place that can announce it; the
measurement harness waited seven more hours for its own limit.

These run the driver over the PRODUCTION close-out with a real
:class:`PipelineLifecycleEmitter` on a recording publisher, so the payloads
are the real ones and nothing touches the network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import build_conductor_driver_deps_factory
from forge.config.models import ForgeConfig, PipelineConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import PipelineLifecycleEmitter
from forge.pipeline.conductor_driver import ConductorRunOutcome, drive_fix_journey
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "fix-build-FEAT-689D-20261101202307"
FEATURE_ID = "FEAT-689D"
CORRELATION_ID = "fix-build-FEAT-689D-20261101100000"


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, ?, 'r', 'fix/x', "
        "'f.yaml', 'RUNNING', 'cli', ?, '2026-11-01T20:23:07Z', "
        "'2026-11-01T20:23:07Z', '/work/b', 'mode-c', 'TASK-FIX-689D')",
        (BUILD_ID, FEATURE_ID, CORRELATION_ID),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


class _RecordingPublisher:
    """Records each terminal publish, and where it fell against the release."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.failed: list[Any] = []
        self.complete: list[Any] = []

    async def publish_build_failed(self, payload: Any) -> None:
        self.failed.append(payload)
        self.order.append("build-failed")

    async def publish_build_complete(self, payload: Any) -> None:
        self.complete.append(payload)
        self.order.append("build-complete")


@dataclasses.dataclass
class _Supervisor:
    script: list[Any]
    budget_pause: Any = None

    async def next_turn(self, build_id: str) -> Any:
        return self.script.pop(0)


class _Decision:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _Card:
    card_published = True
    card_result = "RESUMED"


async def _no_sleep(_seconds: float) -> None:
    return None


def _drive(
    pool: SqliteLifecyclePersistence, tmp_path: Path, report: Any
) -> tuple[Any, _RecordingPublisher, list[str]]:
    order: list[str] = []
    publisher = _RecordingPublisher(order)
    emitter = PipelineLifecycleEmitter(publisher, PipelineConfig())  # type: ignore[arg-type]
    supervisor = _Supervisor(script=[report])
    deps = build_conductor_driver_deps_factory(
        pool=pool,
        config=ForgeConfig.model_validate(
            {
                "pipeline": {
                    "build_queue_subject": "pipeline.build-queued.team-a",
                    "approved_originators": ["terminal"],
                },
                "permissions": {"filesystem": {"allowlist": ["/work"]}},
                "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
            }
        ),
        receipts_root=tmp_path / "receipts",
        lifecycle_emitter=emitter,
    )(BUILD_ID, supervisor)

    async def release(build_id: str) -> None:
        order.append("release")

    deps = dataclasses.replace(deps, release_queue_message=release, sleep=_no_sleep)
    result = asyncio.run(drive_fix_journey(BUILD_ID, deps))
    return result, publisher, order


def test_the_cap_with_nobody_to_ask_announces_build_failed_before_the_release(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    result, publisher, order = _drive(
        pool,
        tmp_path,
        TurnReport(
            outcome=TurnOutcome.PAUSED_BUDGET,
            build_id=BUILD_ID,
            rationale="wallclock 7201s exceeded cap 7200s",
        ),
    )

    assert result.outcome is ConductorRunOutcome.PAUSED_BUDGET
    assert pool.get_build_row(BUILD_ID).status.value == "FAILED"
    assert order == ["build-failed", "release"]
    assert publisher.complete == []
    [payload] = publisher.failed
    assert payload.feature_id == FEATURE_ID
    assert payload.build_id == BUILD_ID
    assert payload.correlation_id == CORRELATION_ID
    assert payload.recoverable is False
    assert payload.failed_task_id == "TASK-FIX-689D"
    assert "stopped at the cap (wallclock 7201s exceeded cap 7200s)" in (
        payload.failure_reason
    )
    assert payload.failure_reason == pool.get_build_row(BUILD_ID).error


def test_a_clean_review_ending_announces_build_complete_before_the_release(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    result, publisher, order = _drive(
        pool,
        tmp_path,
        TurnReport(
            outcome=TurnOutcome.TERMINAL,
            build_id=BUILD_ID,
            rationale="clean-review-no-fixes: mode-c-task-review-empty",
            dispatch_result=_Decision("clean-review-no-fixes"),
        ),
    )

    assert result.outcome is ConductorRunOutcome.COMPLETED
    assert pool.get_build_row(BUILD_ID).status.value == "COMPLETE"
    assert order == ["build-complete", "release"]
    assert publisher.failed == []
    [payload] = publisher.complete
    assert payload.feature_id == FEATURE_ID
    assert payload.build_id == BUILD_ID
    assert payload.correlation_id == CORRELATION_ID
    assert payload.pr_url is None
    assert payload.summary == "clean-review-no-fixes: mode-c-task-review-empty"


def test_the_merge_card_path_announces_nothing_from_the_close_out(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    _, publisher, order = _drive(
        pool,
        tmp_path,
        TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            rationale="the merge card was published",
            dispatch_result=_Card(),
        ),
    )

    assert pool.get_build_row(BUILD_ID).status.value == "RUNNING"
    assert publisher.failed == [] and publisher.complete == []
    assert order == ["release"]


def test_a_row_already_ended_elsewhere_is_not_announced_again(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """``forge cancel`` publishes its own ``build-cancelled``; a second
    terminal for the same build would contradict it."""
    pool.connection.execute(
        "UPDATE builds SET status = 'CANCELLED' WHERE build_id = ?", (BUILD_ID,)
    )
    pool.connection.commit()

    _, publisher, order = _drive(
        pool,
        tmp_path,
        TurnReport(
            outcome=TurnOutcome.TERMINAL,
            build_id=BUILD_ID,
            rationale="failed: the build was cancelled",
            dispatch_result=_Decision("failed"),
        ),
    )

    assert pool.get_build_row(BUILD_ID).status.value == "CANCELLED"
    assert publisher.failed == [] and publisher.complete == []
    assert order == ["release"]
