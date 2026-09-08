"""A finished fix journey gives the queue its message back (seam seven).

Observed on 2026-09-08, twice within an hour. The conductor closed two fix
journeys — one FAILED as a tooling fault, one after a cancel — and the bus
showed the pipeline consumer still holding that build's ``build-queued``
message, with the next build's message waiting behind it. The consumer
takes one message at a time and waits an hour before redelivery, so each
closed journey cost the queue an hour. forge-prod's health line said the
slot was held; restarting did not cure it, because the boot check read the
held message as a live build and was right to. The cure, both times, was
pulling the message off the stream by hand.

Why nothing released it: for a routine build the lifecycle bridge watches
the run and acknowledges the message when the run ends. For a fix journey
the bridge deliberately stands down — no identity ever resolves, so it has
nothing to watch and the conductor owns the terminal — and the handle went
out of scope with the bridge's observer. Nobody on the conductor's side
could reach it.

What this file pins:

* every terminal close-out releases the message — the plain FAILED
  terminal, the merge card's publication, a cancelled build, and a budget
  breach nobody could be asked about;
* it is released exactly once, however many times a build is closed out;
* a close-out with nothing to release says so in one line and does
  nothing else — no error, no crash, no second acknowledgement;
* a release that raises never takes the terminal down with it;
* a build with no release seam wired behaves exactly as before.

The suite drives the coroutines through ``asyncio.run`` — this package's
conductor tests do not declare ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    build_conductor_driver_deps_factory,
    make_conductor_queue_release,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.conductor_driver import (
    ConductorDriverDeps,
    ConductorRunOutcome,
    drive_fix_journey,
)
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "build-FEAT-39F6-20260908053507"
FEATURE_ID = "FEAT-39F6"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeAckHandle:
    """Stands in for the queued message's ack handle the bridge registers.

    The real one is idempotent; this one is not, deliberately, so a
    double acknowledgement shows up as two entries rather than being
    hidden by the handle's own belt.
    """

    acks: list[str] = field(default_factory=list)

    async def ack(self) -> None:
        self.acks.append("ack")

    async def nak(self) -> None:  # pragma: no cover - never used here
        raise AssertionError("a finished journey acknowledges; it never naks")


@dataclass
class FakeBridge:
    """The half of the lifecycle bridge the conductor is given.

    ``take_ack_handle`` hands the handle over and forgets it, exactly as
    the real wireup does, so a second close-out finds nothing.
    """

    handles: dict[str, Any] = field(default_factory=dict)
    asked_for: list[str] = field(default_factory=list)

    def take_ack_handle(self, feature_id: str) -> Any:
        self.asked_for.append(feature_id)
        return self.handles.pop(feature_id, None)


@dataclass
class FakeSupervisor:
    """Returns a scripted sequence of turn reports."""

    script: list[Any] = field(default_factory=list)
    #: Read by the loop to decide whether a budget breach can be escalated.
    budget_pause: Any | None = None

    async def next_turn(self, build_id: str) -> Any:
        if not self.script:
            return _report(TurnOutcome.TERMINAL, rationale="script exhausted")
        return self.script.pop(0)


class _Decision:
    """Duck-typed stand-in for the journey's terminal decision."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _Card:
    """Duck-typed stand-in for a published merge card."""

    card_published = True
    card_result = "RESUMED"


def _report(
    outcome: TurnOutcome, *, rationale: str = "", dispatch_result: Any = None
) -> TurnReport:
    return TurnReport(
        outcome=outcome,
        build_id=BUILD_ID,
        rationale=rationale,
        dispatch_result=dispatch_result,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def _deps(supervisor: FakeSupervisor, **overrides: Any) -> ConductorDriverDeps:
    base: dict[str, Any] = {"supervisor": supervisor, "sleep": _no_sleep}
    base.update(overrides)
    return ConductorDriverDeps(**base)


# ---------------------------------------------------------------------------
# A real ledger in a temp directory, with one fix-journey row
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, ?, "
        "'api_test', 'repair/TASK-FEAT39F6FIX1', 'f.yaml', 'RUNNING', 'cli', "
        "'fix-build-FEAT-39F6-20260907', '2026-09-08T05:35:07Z', "
        "'2026-09-08T05:35:30Z', '/work/journey', 'mode-c', "
        "'TASK-FEAT39F6FIX1')",
        (BUILD_ID, FEATURE_ID),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/work"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        }
    )


def _release(pool: SqliteLifecyclePersistence, bridge: FakeBridge | None):
    return make_conductor_queue_release(
        pool=pool,
        take_ack_handle=None if bridge is None else bridge.take_ack_handle,
    )


# ---------------------------------------------------------------------------
# Every terminal close-out releases the message
# ---------------------------------------------------------------------------


class TestEveryTerminalCloseOutReleasesTheMessage:
    def test_a_failed_journey_releases_it_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Attempt four, 05:41Z: FAILED as a tooling fault, slot held."""
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.TERMINAL,
                    rationale="failed: the work leg could not provision a "
                    "Python interpreter",
                    dispatch_result=_Decision("failed"),
                )
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, release_queue_message=_release(pool, bridge)),
            )
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert bridge.asked_for == [FEATURE_ID]
        assert handle.acks == ["ack"]

    def test_a_delivered_journey_releases_it_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The merge card is out; the journey is over on the conductor's side."""
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.DISPATCHED,
                    rationale="the merge card was published",
                    dispatch_result=_Card(),
                )
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, release_queue_message=_release(pool, bridge)),
            )
        )

        assert report.outcome is ConductorRunOutcome.DELIVERED
        assert handle.acks == ["ack"]

    def test_a_cancelled_journey_releases_it_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """``forge cancel`` on a fix journey, 2026-09-08.

        The cancel command runs in its own short-lived process and cannot
        reach the daemon's handle: all it does is mark the row terminal.
        The daemon's turn loop then reads that row on its next turn, gets
        TERMINAL back, and closes the journey out — and it is that
        close-out, here, that has to give the message back. Before the
        cure it did not, and the cancelled build held the slot exactly as
        the failed one did.
        """
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.TERMINAL,
                    rationale="cancelled: cli cancel",
                    dispatch_result=_Decision("cancelled"),
                )
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, release_queue_message=_release(pool, bridge)),
            )
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert handle.acks == ["ack"]

    def test_a_breach_nobody_could_be_asked_about_releases_it_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Seam nine's close-out is a terminal close-out too."""
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.PAUSED_BUDGET,
                    rationale="the review-cycle cap was reached",
                )
            ],
            budget_pause=None,
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, release_queue_message=_release(pool, bridge)),
            )
        )

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET
        assert handle.acks == ["ack"]

    def test_a_journey_that_is_still_running_keeps_its_message(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """A stop that is not a terminal close-out holds the slot on purpose.

        The escalation resolves and re-queues the build; releasing the
        message here would let a later build overtake a journey that is
        still someone's to answer.
        """
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.PAUSED_BUDGET, rationale="the wall clock ran out"
                )
            ],
            budget_pause=object(),
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, release_queue_message=_release(pool, bridge)),
            )
        )

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET
        assert handle.acks == []
        assert bridge.asked_for == []


# ---------------------------------------------------------------------------
# Exactly once, and never a crash
# ---------------------------------------------------------------------------


class TestReleasedExactlyOnceAndNeverACrash:
    def test_closing_the_same_build_out_twice_releases_it_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        release = _release(pool, bridge)

        asyncio.run(release(BUILD_ID))
        asyncio.run(release(BUILD_ID))

        assert handle.acks == ["ack"]
        assert bridge.asked_for == [FEATURE_ID, FEATURE_ID]

    def test_a_close_out_with_no_message_to_release_says_so_and_stops(
        self,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A journey started before the bridge attached, or any test."""
        bridge = FakeBridge(handles={})

        with caplog.at_level(logging.INFO, logger="forge.cli._serve_conductor"):
            asyncio.run(_release(pool, bridge)(BUILD_ID))

        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "nothing to release" in lines[0]

    def test_no_bridge_this_boot_says_so_and_stops(
        self,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="forge.cli._serve_conductor"):
            asyncio.run(_release(pool, None)(BUILD_ID))

        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "no lifecycle bridge is wired this boot" in lines[0]

    def test_an_unknown_build_says_so_and_stops(
        self,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bridge = FakeBridge(handles={FEATURE_ID: FakeAckHandle()})

        with caplog.at_level(logging.INFO, logger="forge.cli._serve_conductor"):
            asyncio.run(_release(pool, bridge)("build-nobody-queued"))

        assert bridge.asked_for == []
        assert "no feature id on its row" in caplog.records[-1].getMessage()

    def test_a_release_that_raises_never_loses_the_terminal(self) -> None:
        async def boom(_build_id: str) -> None:
            raise RuntimeError("the broker went away mid-acknowledgement")

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, release_queue_message=boom)
            )
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED

    def test_the_message_is_released_after_the_journey_is_written_down(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Order matters: the row is durable before the next build starts."""
        order: list[str] = []
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        underlying = _release(pool, bridge)

        def close_out(*, build_id: str, report: Any) -> None:
            order.append("wrote the journey down")

        async def release(build_id: str) -> None:
            order.append("released the message")
            await underlying(build_id)

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    close_out=close_out,
                    release_queue_message=release,
                ),
            )
        )

        assert order == ["wrote the journey down", "released the message"]

    def test_a_close_out_that_raises_still_releases_the_message(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The queue must never be held hostage by an unrelated fault."""
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})

        def boom(*, build_id: str, report: Any) -> None:
            raise RuntimeError("the ledger was locked")

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    close_out=boom,
                    release_queue_message=_release(pool, bridge),
                ),
            )
        )

        assert handle.acks == ["ack"]


# ---------------------------------------------------------------------------
# The composition, and the unwired case
# ---------------------------------------------------------------------------


class TestTheSeamIsWiredWhereTheDaemonComposesIt:
    def test_the_deps_factory_wires_the_release(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        handle = FakeAckHandle()
        bridge = FakeBridge(handles={FEATURE_ID: handle})
        deps = build_conductor_driver_deps_factory(
            pool=pool,
            config=_config(),
            take_ack_handle=bridge.take_ack_handle,
        )(BUILD_ID, supervisor=object())

        assert deps.release_queue_message is not None
        asyncio.run(deps.release_queue_message(BUILD_ID))

        assert handle.acks == ["ack"]

    def test_with_no_bridge_the_factory_still_wires_a_seam_that_does_nothing(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        deps = build_conductor_driver_deps_factory(
            pool=pool, config=_config()
        )(BUILD_ID, supervisor=object())

        assert deps.release_queue_message is not None
        asyncio.run(deps.release_queue_message(BUILD_ID))

    def test_a_loop_with_no_release_seam_behaves_exactly_as_before(self) -> None:
        """Every existing test, and every unit tier, takes this path."""
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.COMPLETED
