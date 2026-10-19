"""Every entry point that reaches a managed sandbox takes its gate (design 2.3a).

Queue admission and the mission step hold work while the sandbox starts; the
mission branch start, the mission finisher, the merge press, the merge offer
and preview preparation wait for start-up; the coordinator's boot brings
sandboxes up before recovery; with no gate installed each is today's.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.sandbox_gate import gate as gate_module
from forge.sandbox_gate.gate import SandboxGate
from forge.planning.work_queue_store import WorkQueueStore

from tests.forge.planning.test_work_queue_loop import (
    FakeClock,
    Notifier,
    build_loop,
    file_row,
)
from tests.forge.sandbox_gate._fakes import (
    OTHER_REPO,
    REPO,
    SANDBOX,
    Clock,
    FakeStarter,
    Services,
    a_gate,
    a_ledger,
    settle,
)


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = a_ledger(tmp_path)
    yield cx
    cx.close()


@pytest.fixture
def installed() -> Any:
    """Install a gate for one test, and always take it out again."""
    made: list[SandboxGate] = []

    def install(gate: SandboxGate) -> SandboxGate:
        gate_module.install_gate(gate)
        made.append(gate)
        return gate

    yield install
    gate_module.install_gate(None)


# ---------------------------------------------------------------------------
# Queue admission
# ---------------------------------------------------------------------------


class TestQueueAdmission:
    @pytest.mark.asyncio
    async def test_with_no_gate_a_row_is_admitted_as_before(
        self, ledger: sqlite3.Connection
    ) -> None:
        gate_module.install_gate(None)
        clock = FakeClock()
        store = WorkQueueStore(ledger, clock=clock.now)
        loop, maker = build_loop(store, clock=clock, notifier=Notifier(), runs={})
        queue_id = file_row(store, "c-1", repo=REPO)
        assert await loop.take_next() == queue_id
        assert [a.correlation_id for a in maker.admissions] == ["c-1"]

    @pytest.mark.asyncio
    async def test_a_row_for_a_stopped_sandbox_is_held_then_admitted(
        self, ledger: sqlite3.Connection, installed: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        starter = FakeStarter(running=False)
        gate = installed(a_gate(ledger, starter, clock=Clock()))
        clock = FakeClock()
        store = WorkQueueStore(ledger, clock=clock.now)
        loop, maker = build_loop(store, clock=clock, notifier=Notifier(), runs={})
        queue_id = file_row(store, "c-1", repo=REPO)
        with caplog.at_level(logging.INFO):
            assert await loop.take_next() is None
        assert store.get(queue_id)["status"] == "QUEUED"
        assert (
            f"holding #{queue_id} — waiting for the sandbox {SANDBOX} to start; it stays "
            "queued and is tried again"
        ) in caplog.text
        assert maker.admissions == []
        await settle(gate)
        assert "start notes-app-sbx" in starter.calls
        assert await loop.take_next() == queue_id
        assert [a.correlation_id for a in maker.admissions] == ["c-1"]

    @pytest.mark.asyncio
    async def test_a_held_row_does_not_hold_another_projects_row(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        installed(a_gate(ledger, FakeStarter(running=False), clock=Clock()))
        clock = FakeClock()
        store = WorkQueueStore(ledger, clock=clock.now)
        loop, _maker = build_loop(store, clock=clock, notifier=Notifier(), runs={}, max_in_flight=2)
        file_row(store, "c-1", repo=REPO)
        other = file_row(store, "c-2", repo=OTHER_REPO)
        assert await loop.take_next() == other

    @pytest.mark.asyncio
    async def test_a_sandbox_that_will_not_start_keeps_the_row_queued_with_the_reason(
        self, ledger: sqlite3.Connection, installed: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        starter = FakeStarter(running=False)
        services = Services(starter)
        services.helper_down = True
        gate = installed(
            a_gate(ledger, starter, clock=Clock(), services=services, ready_timeout_seconds=60)
        )
        clock = FakeClock()
        store = WorkQueueStore(ledger, clock=clock.now)
        loop, maker = build_loop(store, clock=clock, notifier=Notifier(), runs={})
        queue_id = file_row(store, "c-1", repo=REPO)
        assert await loop.take_next() is None
        await settle(gate)
        with caplog.at_level(logging.INFO):
            assert await loop.take_next() is None
        assert store.get(queue_id)["status"] == "QUEUED"  # never closed for it (R3)
        assert "the deploy helper did not answer /healthz" in caplog.text
        assert maker.admissions == []
        services.helper_down = False  # a later pass, once it answers
        assert await loop.take_next() is None
        await settle(gate)
        assert await loop.take_next() == queue_id


# ---------------------------------------------------------------------------
# The mission step's admission
# ---------------------------------------------------------------------------


class _Missions:
    """Just enough of the mission store for one WAITING attempt."""

    def __init__(self) -> None:
        self.admitted: list[str] = []
        self.attempt = SimpleNamespace(
            status="WAITING", key="A", correlation_id="m-1-A", attempt=1
        )

    def attempts(self, mission_id: str) -> list[Any]:
        return [self.attempt]

    def needs_resolution(self, mission_id: str, key: str) -> Any:
        return SimpleNamespace(ready=True)

    def admit_attempt(self, correlation_id: str, **_kw: Any) -> bool:
        self.admitted.append(correlation_id)
        return True

    def features(self, mission_id: str) -> list[Any]:
        return [SimpleNamespace(key="A", sentence="add tags")]


class TestMissionAdmission:
    @pytest.mark.asyncio
    async def test_a_mission_waits_for_its_sandbox_without_stopping_the_others(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        from forge.planning.mission_step import MissionStep

        gate = installed(a_gate(ledger, FakeStarter(running=False), clock=Clock()))
        missions = _Missions()
        started: list[Any] = []

        async def start_run(admission: Any) -> None:
            started.append(admission)

        step = MissionStep(
            missions,  # type: ignore[arg-type]
            planning_run=lambda cid: None,
            count_in_flight=lambda: 0,
            max_in_flight=4,
            start_run=start_run,
            failure_reason=lambda run, state, cid: "",
            clock=FakeClock().now,
        )
        mission = SimpleNamespace(
            id="M-1",
            target_repo=REPO,
            originating_user="U",
            parent_request_id=None,
            originating_adapter="slack",
            triggered_by="jarvis",
        )
        assert await step.admit(mission) is True  # not "the factory is full"
        assert missions.admitted == [] and started == []
        await settle(gate)
        assert await step.admit(mission) is True
        assert missions.admitted == ["m-1-A"]
        assert [a.target_repo for a in started] == [REPO]


# ---------------------------------------------------------------------------
# Steps that wait for start-up
# ---------------------------------------------------------------------------


class TestStepsWaitForTheGate:
    @pytest.mark.asyncio
    async def test_a_mission_approved_while_stopped_wakes_it_before_its_branch_start(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        from forge.planning.missions_flow import MissionFlow

        starter = FakeStarter(running=False)
        installed(a_gate(ledger, starter, clock=Clock()))
        flow = MissionFlow(
            object(),  # type: ignore[arg-type]
            publish_card=None,  # type: ignore[arg-type]
            subscriber_factory=None,  # type: ignore[arg-type]
            notify=None,  # type: ignore[arg-type]
            read_remote_tip=None,  # type: ignore[arg-type]
            create_branch=None,
            approval_wait_seconds=60,
        )
        seen: list[bool] = []

        async def branch_start(mission: Any) -> None:
            seen.append(starter.running[SANDBOX])

        flow._start_branch = branch_start  # type: ignore[method-assign]
        await flow.start_branch(SimpleNamespace(id="M-1", target_repo=REPO))  # type: ignore[arg-type]
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_the_mission_finisher_wakes_it_first(
        self, ledger: sqlite3.Connection, installed: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.pipeline import mission_finish

        starter = FakeStarter(running=False)
        gate = installed(a_gate(ledger, starter, clock=Clock()))
        seen: list[tuple[bool, int]] = []

        async def finish(deps: Any, mission_id: str) -> str:
            seen.append((starter.running[SANDBOX], gate.users_of(SANDBOX)))
            return "offered"

        monkeypatch.setattr(mission_finish, "finish_ready_mission", finish)
        await mission_finish._finish_guarded(SimpleNamespace(), "M-1", REPO)  # type: ignore[arg-type]
        assert seen == [(True, 1)]
        assert gate.users_of(SANDBOX) == 0

    @pytest.mark.asyncio
    async def test_the_merge_press_takes_the_gate_then_the_lock(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        from forge.pipeline.merge_executor import MergeApprovalConsumer

        starter = FakeStarter(running=False)
        gate = installed(a_gate(ledger, starter, clock=Clock()))
        consumer = MergeApprovalConsumer(SimpleNamespace())  # type: ignore[arg-type]
        async with consumer._pressing(REPO, "the merge press for B-1"):
            assert starter.running[SANDBOX] is True
            assert consumer._lock_for(REPO).locked()
            assert gate.users_of(SANDBOX) == 1
        assert not consumer._lock_for(REPO).locked()

    @pytest.mark.asyncio
    async def test_with_no_gate_the_press_is_exactly_the_lock(self) -> None:
        from forge.pipeline.merge_executor import MergeApprovalConsumer

        gate_module.install_gate(None)
        consumer = MergeApprovalConsumer(SimpleNamespace())  # type: ignore[arg-type]
        async with consumer._pressing(REPO, "the merge press for B-1"):
            assert consumer._lock_for(REPO).locked()

    @pytest.mark.asyncio
    async def test_preview_preparation_wakes_it_first(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        from forge.deploy.review_preview import ReviewPreviewService

        starter = FakeStarter(running=False)
        installed(a_gate(ledger, starter, clock=Clock()))
        service = ReviewPreviewService.__new__(ReviewPreviewService)
        service._clock = FakeClock().now  # type: ignore[attr-defined]
        service._run_id_factory = lambda: "run-1"  # type: ignore[attr-defined]
        seen: list[bool] = []

        async def prepare(req: Any, *, now: Any, run_id: str) -> str:
            seen.append(starter.running[SANDBOX])
            return "evidence"

        service._prepare = prepare  # type: ignore[method-assign]
        answer = await service.prepare(SimpleNamespace(project=REPO, build_id="B-1"))  # type: ignore[arg-type]
        assert answer == "evidence" and seen == [True]

    @pytest.mark.asyncio
    async def test_the_merge_offer_holds_the_sandbox_up_while_it_runs(
        self, ledger: sqlite3.Connection, installed: Any
    ) -> None:
        from forge.pipeline.merge_offer import MergeOfferService

        starter = FakeStarter(running=True)
        gate = installed(a_gate(ledger, starter, clock=Clock()))
        service = MergeOfferService.__new__(MergeOfferService)
        service._pool = SimpleNamespace(  # type: ignore[attr-defined]
            get_build_row=lambda build_id: SimpleNamespace(repo=REPO)
        )
        seen: list[int] = []

        async def offer(**kwargs: Any) -> bool:
            seen.append(gate.users_of(SANDBOX))
            return True

        service._offer = offer  # type: ignore[method-assign]
        assert await service.offer(build_id="B-1", feature_id="FEAT-1", card_words=lambda b, m: "")
        assert seen == [1]


# ---------------------------------------------------------------------------
# The coordinator's boot
# ---------------------------------------------------------------------------


class _RecordingGate:
    poll_seconds = 60.0

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def set_redeploy(self, route: Any) -> None:
        self.events.append(f"set_redeploy {route}")

    async def boot(self) -> list[str]:
        self.events.append("sandbox_gate_boot")
        return [SANDBOX]

    async def run_forever(self, interval: float) -> None:
        self.events.append("sandbox_gate_poll")


def test_boot_takes_the_gate_before_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge.cli import _serve_daemon
    from forge.cli import serve as serve_module
    from forge.cli._serve_config import ServeConfig
    from forge.cli._serve_state import SubscriptionState

    from tests.forge.test_cli_serve_skeleton import _StubNatsClient

    stub = _StubNatsClient()
    events: list[str] = []

    async def connect(servers: str) -> object:
        return stub

    async def recovery(client: object) -> None:
        events.append("recovery_reconcile")

    async def consumer(client: object) -> None:
        events.append("consumer_reconcile")

    async def compose(client: object) -> None:
        events.append("compose_dispatch_chain")

    async def daemon(config: object, state: object, *, client: object = None) -> None:
        events.append("daemon")
        await asyncio.sleep(0.05)

    async def healthz(config: object, state: object) -> None:
        await asyncio.sleep(0.05)

    async def open_fleet(url: str) -> object:
        return stub

    monkeypatch.setattr(_serve_daemon, "nats_connect", connect)
    monkeypatch.setattr(serve_module, "open_fleet_client", open_fleet)
    monkeypatch.setattr(serve_module, "recovery_reconcile_on_boot", recovery)
    monkeypatch.setattr(serve_module, "consumer_reconcile_on_boot", consumer)
    monkeypatch.setattr(serve_module, "compose_dispatch_chain", compose)
    monkeypatch.setattr(serve_module, "run_daemon", daemon)
    monkeypatch.setattr(serve_module, "run_healthz_server", healthz)
    monkeypatch.setattr(serve_module, "sandbox_gate", _RecordingGate(events))
    monkeypatch.setattr(
        serve_module, "sandbox_redeploy_factory", lambda client: f"route-on-{type(client).__name__}"
    )
    asyncio.run(serve_module._run_serve(ServeConfig(), SubscriptionState()))
    assert events.index("set_redeploy route-on-_StubNatsClient") < events.index("sandbox_gate_boot")
    assert events.index("sandbox_gate_boot") < events.index("recovery_reconcile")
    assert events.index("sandbox_gate_boot") < events.index("compose_dispatch_chain")
    assert "sandbox_gate_poll" in events


def test_without_a_gate_boot_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge.cli import serve as serve_module

    assert serve_module.sandbox_gate is None


def test_the_production_composition_installs_no_gate_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off by default: the gate the composition would build from plain
    settings is none, so nothing is installed."""
    from forge.sandbox_gate.gate import build_sandbox_gate

    from tests.forge.sandbox_gate._fakes import a_config

    assert build_sandbox_gate(a_config(idle_stop=False, starter_url=None), connection=lambda: None) is None
    assert gate_module.current_gate() is None
