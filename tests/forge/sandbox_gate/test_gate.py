"""The coordinator's sandbox gate (one sandbox per project, stage S2, design
sections 2.3 and 2.3a).

What a person notices:

- with nothing configured, nothing changes: no gate is built, and every entry
  point passes straight through;
- work for a stopped sandbox is held, the sandbox is started, and the work goes
  once both services answer;
- a step that takes the gate waits for a stop in flight to finish, then for the
  start, and never meets a stopping sandbox;
- after a start the recorded deployment is brought back before "ready", and a
  failed redeploy suspends management (the sandbox stays up);
- the once-a-minute look stops a sandbox only after the idle time with nothing
  open, and anything open, a step in progress or a hold keeps it up;
- an unclear stop leaves the sandbox "unknown" until the starter says nothing
  is pending;
- at start-up, a managed sandbox with unfinished work is brought up first.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.config.models import ForgeConfig
from forge.sandbox_gate import gate as gate_module
from forge.sandbox_gate.gate import (
    READY,
    STOPPED,
    STOPPING,
    SUSPENDED,
    UNKNOWN,
    RedeployOutcome,
    SandboxNotReady,
    build_sandbox_gate,
)
from forge.sandbox_gate.starter_client import read_status

from tests.forge.sandbox_gate._fakes import (
    OTHER_REPO,
    REPO,
    SANDBOX,
    Clock,
    FakeStarter,
    Services,
    a_config,
    a_build,
    a_gate,
    a_ledger,
    a_preview_lease,
    settle,
    stopped,
    LABEL_KEY,
    COMPOSE_PROJECT,
)

NAMED = {"compose_project": COMPOSE_PROJECT, "identity_label": f"{LABEL_KEY}=notes-app@abc1234"}


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = a_ledger(tmp_path)
    yield cx
    cx.close()


def _record_deployment(cx: sqlite3.Connection, target: str = REPO) -> None:
    cx.execute(
        "INSERT INTO deployment_targets (target, counter, running_commit, running_identity) "
        "VALUES (?, 3, 'abc1234', 'notes-app@abc1234')",
        (target,),
    )
    cx.commit()


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


class TestOffByDefault:
    def test_the_settings_default_to_off(self) -> None:
        config = a_config(idle_stop=False, starter_url=None)
        assert config.planning.sandbox_starter.url is None
        assert config.planning.sandbox_starter.idle_minutes == 30
        assert config.planning.sandboxes[REPO].idle_stop is False

    def test_no_gate_without_a_starter_address(self, ledger: sqlite3.Connection) -> None:
        assert build_sandbox_gate(a_config(starter_url=None), connection=lambda: ledger) is None

    def test_no_gate_when_no_sandbox_says_idle_stop(self, ledger: sqlite3.Connection) -> None:
        assert build_sandbox_gate(a_config(idle_stop=False), connection=lambda: ledger) is None

    def test_a_gate_when_both_are_set(self, ledger: sqlite3.Connection) -> None:
        gate = build_sandbox_gate(a_config(), connection=lambda: ledger)
        assert gate is not None
        assert gate.names == (SANDBOX,)
        assert gate.sandbox_for(OTHER_REPO) is None

    @pytest.mark.asyncio
    async def test_with_no_gate_installed_every_entry_point_passes(self) -> None:
        gate_module.install_gate(None)
        assert gate_module.admits(REPO) is True
        async with gate_module.using(REPO):
            pass

    def test_the_shared_sandbox_is_never_managed(self) -> None:
        with pytest.raises(ValidationError, match="never stopped"):
            ForgeConfig.model_validate(
                {
                    "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                    "planning": {
                        "target_repo_paths": {REPO: "/tmp/x"},
                        "sandboxes": {
                            REPO: {
                                "name": "api-test-deploy",
                                "sidecar_url": "http://127.0.0.1:8925",
                                "runner_url": "http://127.0.0.1:8924",
                                "idle_stop": True,
                            }
                        },
                    },
                }
            )

    def test_one_sandbox_gives_one_answer(self) -> None:
        with pytest.raises(ValidationError, match="different idle_stop"):
            ForgeConfig.model_validate(
                {
                    "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                    "planning": {
                        "target_repo_paths": {REPO: "/tmp/x", OTHER_REPO: "/tmp/y"},
                        "sandboxes": {
                            REPO: {
                                "name": SANDBOX,
                                "sidecar_url": "http://127.0.0.1:18935",
                                "runner_url": "http://127.0.0.1:18934",
                                "idle_stop": True,
                            },
                            OTHER_REPO: {
                                "name": SANDBOX,
                                "sidecar_url": "http://127.0.0.1:18935",
                                "runner_url": "http://127.0.0.1:18934",
                            },
                        },
                    },
                }
            )


# ---------------------------------------------------------------------------
# Reading the starter
# ---------------------------------------------------------------------------


class TestReadingTheStarter:
    def test_redeploy_pending_is_read(self) -> None:
        status = read_status(SANDBOX, 200, FakeStarter(running=True).body(SANDBOX) | {
            "readiness": "services ready, redeploy pending"
        })
        assert status.redeploy_pending and status.services_up and status.settled

    def test_an_operation_running_is_not_settled(self) -> None:
        starter = FakeStarter(running=True)
        starter.pending_operation = "stop"
        status = read_status(SANDBOX, 200, starter.body(SANDBOX))
        assert status.busy and not status.settled

    def test_a_name_not_listed(self) -> None:
        status = read_status(SANDBOX, 404, {"ok": False, "message": "not in this starter's list"})
        assert status.reached and not status.listed

    def test_suspension_and_holds_are_read(self) -> None:
        starter = FakeStarter(running=True)
        starter.suspended[SANDBOX] = "the proxy did not answer"
        starter.holds["h1"] = (SANDBOX, "register-repo", "services")
        status = read_status(SANDBOX, 200, starter.body(SANDBOX))
        assert status.suspended == "the proxy did not answer"
        assert status.holds and status.holds[0]["owner"] == "register-repo"


# ---------------------------------------------------------------------------
# Admission holds and wakes
# ---------------------------------------------------------------------------


class TestAdmission:
    @pytest.mark.asyncio
    async def test_a_stopped_sandbox_holds_the_work_and_is_started(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=clock)
        assert gate.admits(REPO) is False
        await settle(gate)
        assert "start notes-app-sbx" in starter.calls
        assert starter.told == [("start", {})]  # no recorded deployment: says none
        assert gate.state_of(SANDBOX) == READY
        assert gate.admits(REPO) is True

    @pytest.mark.asyncio
    async def test_an_unmanaged_repository_is_never_held(
        self, ledger: sqlite3.Connection
    ) -> None:
        gate = a_gate(ledger, FakeStarter(), clock=Clock())
        assert gate.admits(OTHER_REPO) is True
        assert gate.admits(None) is True

    @pytest.mark.asyncio
    async def test_a_partial_start_keeps_the_work_held_and_says_which_service(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=False)
        services = Services(starter)
        services.runner_down = True
        gate = a_gate(ledger, starter, clock=clock, services=services, ready_timeout_seconds=60)
        assert gate.admits(REPO) is False
        await settle(gate)
        assert gate.state_of(SANDBOX) == UNKNOWN
        failure = gate.take_failure(REPO)
        assert failure is not None and "the build runner did not answer /ok" in failure
        assert gate.take_failure(REPO) is None  # said once

    @pytest.mark.asyncio
    async def test_a_name_the_starter_does_not_list_passes_through(
        self, ledger: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        starter = FakeStarter(running=True, listed=False)
        gate = a_gate(ledger, starter, clock=Clock())
        assert gate.admits(REPO) is False
        await settle(gate)
        assert gate.admits(REPO) is True
        assert gate.sandbox_for(REPO) is None


# ---------------------------------------------------------------------------
# Using a sandbox, and the stop/start race
# ---------------------------------------------------------------------------


class TestUse:
    @pytest.mark.asyncio
    async def test_use_starts_a_stopped_sandbox_first(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=clock)
        async with gate.use(REPO, purpose="the merge press"):
            assert starter.running[SANDBOX] is True
            assert gate.users_of(SANDBOX) == 1
        assert gate.users_of(SANDBOX) == 0

    @pytest.mark.asyncio
    async def test_a_step_waits_for_a_stop_in_flight_then_starts_it_again(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = a_gate(ledger, starter, clock=clock)
        await gate.ensure_ready(SANDBOX)
        clock.advance(31 * 60)
        await gate.poll_once()  # first idle sighting
        clock.advance(31 * 60)
        starter.stop_gate = asyncio.Event()
        poll = asyncio.ensure_future(gate.poll_once())
        for _ in range(50):
            await asyncio.sleep(0)
            if gate.state_of(SANDBOX) == STOPPING:
                break
        assert gate.state_of(SANDBOX) == STOPPING
        entered: list[str] = []

        async def a_step() -> None:
            async with gate.use(REPO, purpose="a preview"):
                entered.append(gate.state_of(SANDBOX) or "")

        step = asyncio.ensure_future(a_step())
        for _ in range(20):
            await asyncio.sleep(0)
        assert entered == []  # it waits; it never meets a stopping sandbox
        starter.stop_gate.set()
        await asyncio.wait_for(poll, 5)
        await asyncio.wait_for(step, 5)
        assert entered == [READY]
        assert starter.calls.index("stop notes-app-sbx") < starter.calls.index(
            "start notes-app-sbx"
        )

    @pytest.mark.asyncio
    async def test_a_step_whose_sandbox_never_comes_never_runs_and_says_why(
        self, ledger: sqlite3.Connection
    ) -> None:
        starter = FakeStarter(running=False)
        starter.start_refusal = "the sandbox client is not signed in"
        gate = a_gate(ledger, starter, clock=Clock(), ready_timeout_seconds=60)
        ran: list[str] = []
        with pytest.raises(SandboxNotReady, match="not signed in") as refused:
            async with gate.use(REPO, purpose="the merge press"):
                ran.append("body")
        assert ran == [] and gate.users_of(SANDBOX) == 0
        assert str(refused.value).startswith("the merge press did not run:")

    @pytest.mark.asyncio
    async def test_a_stop_still_pending_times_out_without_running_the_step(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = a_gate(ledger, starter, clock=clock, ready_timeout_seconds=60)
        assert await gate.ensure_ready(SANDBOX)
        await gate.poll_once()
        clock.advance(31 * 60)
        starter.stop_gate = asyncio.Event()  # the stop never answers in time
        await gate.poll_once()
        assert gate.state_of(SANDBOX) == STOPPING
        ran: list[str] = []
        with pytest.raises(SandboxNotReady, match="a stop of notes-app-sbx was still being answered"):
            async with gate.use(REPO, purpose="a preview"):
                ran.append("body")
        assert ran == []
        starter.stop_gate.set()
        await stopped(gate)

    @pytest.mark.asyncio
    async def test_a_redeploy_still_pending_times_out_without_running_the_step(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger)
        starter = FakeStarter(running=False)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            return RedeployOutcome(ok=False, retry=True, sentence="another build holds the lock")

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy, ready_timeout_seconds=60)
        ran: list[str] = []
        with pytest.raises(SandboxNotReady, match="its redeploy is still pending: another build holds"):
            async with gate.use(REPO, purpose="the merge press", needs_deployment=True):
                ran.append("body")
        assert ran == [] and not starter.suspended


# ---------------------------------------------------------------------------
# The redeploy after a start
# ---------------------------------------------------------------------------


class TestRedeploy:
    @pytest.mark.asyncio
    async def test_a_recorded_deployment_is_brought_back_before_ready(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger, f"{REPO}::production")
        asked: list[tuple[str, tuple[str, ...]]] = []
        order: list[str] = []
        starter = FakeStarter(running=False)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            asked.append((name, repos))
            order.append("redeploy")
            return RedeployOutcome(ok=True)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert asked == [(SANDBOX, (REPO,))]
        assert starter.calls.index("ready notes-app-sbx") > starter.calls.index("start notes-app-sbx")
        # Always said: the recorded identity under the entry's names (R1).
        assert starter.told[0] == ("start", NAMED)
        assert ("ready", NAMED) in starter.told

    @pytest.mark.asyncio
    async def test_a_failed_redeploy_suspends_and_the_sandbox_stays_up(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger)
        starter = FakeStarter(running=False)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            return RedeployOutcome(ok=False, sentence="the proxy did not answer its health check")

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert any(c.startswith("suspend notes-app-sbx: the proxy") for c in starter.calls)
        assert starter.running[SANDBOX] is True
        assert "ready notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_no_redeploy_route_with_a_recorded_deployment_suspends(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger)
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=Clock(), redeploy=None)
        assert await gate.ensure_ready(SANDBOX)
        assert any("no route to bring the recorded deployment back" in c for c in starter.calls)

    @pytest.mark.asyncio
    async def test_a_redeploy_pending_after_a_hold_is_run_by_the_poll(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger)
        starter = FakeStarter(running=False)
        ran: list[str] = []

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(name)
            return RedeployOutcome(ok=True)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        gate._slots[SANDBOX].state = STOPPED
        # An outside command's hold wakes it: services ready, redeploy pending.
        assert starter.hold_sync(
            SANDBOX, owner="merge-deploy", until="deployed", deployment=NAMED
        ).ok
        await gate.poll_once()
        await settle(gate)
        assert ran == [SANDBOX]
        assert "ready notes-app-sbx" in starter.calls
        assert gate.state_of(SANDBOX) == READY


# ---------------------------------------------------------------------------
# The idle stop
# ---------------------------------------------------------------------------


async def _ready_gate(ledger: sqlite3.Connection, starter: FakeStarter, clock: Clock, **kw):
    gate = a_gate(ledger, starter, clock=clock, **kw)
    assert await gate.ensure_ready(SANDBOX)
    return gate


class TestIdleStop:
    @pytest.mark.asyncio
    async def test_a_sandbox_with_nothing_open_stops_after_the_idle_time(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        await gate.poll_once()  # idle from now
        clock.advance(29 * 60)
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls
        clock.advance(2 * 60)
        await gate.poll_once()
        await stopped(gate)
        assert "stop notes-app-sbx" in starter.calls
        assert starter.told[-1] == ("stop", {})  # no recorded deployment: says none
        assert gate.state_of(SANDBOX) == STOPPED

    @pytest.mark.asyncio
    async def test_a_step_in_progress_keeps_it_up(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        await gate.poll_once()
        async with gate.use(REPO):
            clock.advance(60 * 60)
            await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_a_use_restarts_the_idle_clock(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        await gate.poll_once()
        clock.advance(20 * 60)
        async with gate.use(REPO):
            pass
        clock.advance(20 * 60)
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_unfinished_ledger_work_keeps_it_up(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        a_preview_lease(ledger, "ready")
        await gate.poll_once()
        clock.advance(2 * 60 * 60)
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_a_hold_at_the_starter_means_no_stop_is_asked_for(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        starter.holds["h9"] = (SANDBOX, "estate-check", "services")
        await gate.poll_once()
        clock.advance(2 * 60 * 60)
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_a_refused_stop_leaves_it_unknown_then_ready_again(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        starter.stop_refusal = "the deploy helper is still executing 1 request(s)"
        await gate.poll_once()
        clock.advance(31 * 60)
        await gate.poll_once()
        await stopped(gate)
        assert gate.state_of(SANDBOX) == UNKNOWN
        assert starter.running[SANDBOX] is True
        assert await gate.ensure_ready(SANDBOX)
        assert gate.state_of(SANDBOX) == READY

    @pytest.mark.asyncio
    async def test_a_lost_stop_answer_is_unknown_until_the_starter_is_settled(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        starter.lose_stop_answer = True
        await gate.poll_once()
        clock.advance(31 * 60)
        await gate.poll_once()
        await stopped(gate)
        assert gate.state_of(SANDBOX) == UNKNOWN
        # The stop had in fact finished: a step now waits for a fresh start
        # rather than going ahead on a sandbox that is not there.
        starter.pending_operation = "stop"  # the starter still finishing it
        step_started = asyncio.Event()

        async def a_step() -> None:
            async with gate.use(REPO):
                step_started.set()

        task = asyncio.ensure_future(a_step())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not step_started.is_set()
        assert "start notes-app-sbx" not in starter.calls  # nothing while pending
        starter.pending_operation = None
        await asyncio.wait_for(task, 5)
        assert "start notes-app-sbx" in starter.calls

    @pytest.mark.asyncio
    async def test_stopped_behind_its_back_is_noticed(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = await _ready_gate(ledger, starter, clock)
        starter.running[SANDBOX] = False
        await gate.poll_once()
        assert gate.state_of(SANDBOX) == STOPPED
        assert gate.admits(REPO) is False

    @pytest.mark.asyncio
    async def test_woken_by_a_hold_it_is_brought_to_ready_so_it_can_stop_again(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=clock)
        gate._slots[SANDBOX].state = STOPPED
        hold = starter.hold_sync(SANDBOX, owner="register-repo", until="services", deployment={})
        await gate.poll_once()
        await settle(gate)
        assert gate.state_of(SANDBOX) == READY
        starter.release_sync(hold.hold_id or "")
        await gate.poll_once()
        clock.advance(31 * 60)
        await gate.poll_once()
        await stopped(gate)
        assert "stop notes-app-sbx" in starter.calls


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------


class TestBoot:
    @pytest.mark.asyncio
    async def test_boot_brings_up_a_stopped_sandbox_with_an_interrupted_build(
        self, ledger: sqlite3.Connection
    ) -> None:
        a_build(ledger, "INTERRUPTED")
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=Clock())
        assert await gate.boot() == []  # nothing left not ready
        assert starter.running[SANDBOX] is True
        assert gate.state_of(SANDBOX) == READY

    @pytest.mark.asyncio
    async def test_boot_leaves_an_idle_sandbox_alone(self, ledger: sqlite3.Connection) -> None:
        starter = FakeStarter(running=False)
        gate = a_gate(ledger, starter, clock=Clock())
        assert await gate.boot() == []
        assert "start notes-app-sbx" not in starter.calls


# ---------------------------------------------------------------------------
# Suspended is its own state (Codex S2 implementation review 1, R2)
# ---------------------------------------------------------------------------


class TestSuspended:
    async def _suspended(self, ledger: sqlite3.Connection, starter: FakeStarter, ran: list[str], clock: Clock):
        _record_deployment(ledger)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(name)
            return RedeployOutcome(ok=False, sentence="the proxy did not answer its health check")

        gate = a_gate(ledger, starter, clock=clock, redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert gate.state_of(SANDBOX) == SUSPENDED
        return gate

    @pytest.mark.asyncio
    async def test_a_suspension_is_not_readiness_and_no_poll_redeploys_again(
        self, ledger: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock, ran = Clock(), []
        starter = FakeStarter(running=False)
        gate = await self._suspended(ledger, starter, ran, clock)
        assert starter.redeploy_pending[SANDBOX] is True  # the real starter keeps it
        for _ in range(5):
            clock.advance(31 * 60)
            await gate.poll_once()
            await settle(gate)
        assert ran == [SANDBOX]  # once, never again until a clear
        assert "stop notes-app-sbx" not in starter.calls
        assert "ready notes-app-sbx" not in starter.calls
        assert gate.state_of(SANDBOX) == SUSPENDED
        # Work that needs the deployment is held, with the reason; other work goes.
        assert gate.admits(REPO, needs_deployment=True) is False
        assert "is suspended (the proxy did not answer" in (gate.why_held(REPO) or "")
        with pytest.raises(SandboxNotReady, match="suspended"):
            async with gate.use(REPO, purpose="the merge press", needs_deployment=True):
                raise AssertionError("the body must not run")
        assert gate.admits(REPO) is True
        async with gate.use(REPO, purpose="a planning step"):
            pass

    @pytest.mark.asyncio
    async def test_a_restarted_coordinator_finds_it_suspended_and_does_not_redeploy(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock, ran = Clock(), []
        starter = FakeStarter(running=False)
        await self._suspended(ledger, starter, ran, clock)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(f"after restart {name}")
            return RedeployOutcome(ok=True)

        again = a_gate(ledger, starter, clock=clock, redeploy=redeploy)
        assert again.state_of(SANDBOX) == UNKNOWN
        await again.poll_once()
        await settle(again)
        assert again.state_of(SANDBOX) == SUSPENDED
        assert await again.ensure_ready(SANDBOX)  # services answer: not ready for a deploy
        assert again.admits(REPO, needs_deployment=True) is False
        assert ran == [SANDBOX]

    @pytest.mark.asyncio
    async def test_after_a_clear_the_next_look_redeploys(self, ledger: sqlite3.Connection) -> None:
        clock, ran = Clock(), []
        starter = FakeStarter(running=False)
        gate = await self._suspended(ledger, starter, ran, clock)

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append("after clear")
            return RedeployOutcome(ok=True)

        gate.set_redeploy(redeploy)
        starter.clear_sync(SANDBOX)
        await gate.poll_once()
        await settle(gate)
        assert ran == [SANDBOX, "after clear"]
        assert gate.state_of(SANDBOX) == READY


# ---------------------------------------------------------------------------
# A stop never holds up the poll (R6)
# ---------------------------------------------------------------------------


class TestStopsRunOnTheirOwn:
    @pytest.mark.asyncio
    async def test_a_pending_redeploy_is_serviced_while_another_sandboxs_stop_is_outstanding(
        self, ledger: sqlite3.Connection
    ) -> None:
        from tests.forge.sandbox_gate._fakes import SECOND_REPO, SECOND_SANDBOX

        _record_deployment(ledger)
        clock = Clock()
        starter = FakeStarter(running=True)
        starter.running[SECOND_SANDBOX] = True
        ran: list[str] = []

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(name)
            return RedeployOutcome(ok=True)

        gate = a_gate(ledger, starter, clock=clock, redeploy=redeploy, config=a_config(two_managed=True))
        assert await gate.ensure_ready(SECOND_SANDBOX)
        await gate.poll_once()  # the second is idle from now
        clock.advance(31 * 60)
        starter.stop_gate = asyncio.Event()  # its stop will hang
        # Meanwhile the first sandbox is waiting for its redeploy.
        starter.redeploy_pending[SANDBOX] = True
        await asyncio.wait_for(gate.poll_once(), timeout=5)  # never waits for the stop
        assert gate.state_of(SECOND_SANDBOX) == STOPPING
        await settle(gate, REPO)
        assert ran == [SANDBOX] and gate.state_of(SANDBOX) == READY
        starter.stop_gate.set()
        await stopped(gate, SECOND_SANDBOX)
        assert gate.state_of(SECOND_SANDBOX) == STOPPED


# ---------------------------------------------------------------------------
# Ready is accepted only for the recorded deployment (Codex review 2, R1)
# ---------------------------------------------------------------------------


class TestReadyOnlyForTheRecordedDeployment:
    @pytest.mark.asyncio
    async def test_a_first_deployment_then_a_services_only_hold_is_restored(
        self, ledger: sqlite3.Connection
    ) -> None:
        """No coordinator stop in between: the starter was last told "none" and
        says ready. An outside services-only hold leaves it so. The next poll
        sees the starter disagree with the ledger, tells it the recorded
        deployment, and redeploys before taking "ready" again."""
        clock = Clock()
        starter = FakeStarter(running=False)
        ran: list[str] = []

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(name)
            return RedeployOutcome(ok=True)

        gate = a_gate(ledger, starter, clock=clock, redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert starter.current(SANDBOX) is None and ran == []
        _record_deployment(ledger)  # the first deployment
        hold = starter.hold_sync(SANDBOX, owner="register-repo", until="services")
        assert hold.ok and starter.redeploy_pending.get(SANDBOX) is not True  # still "ready"
        await gate.poll_once()
        await settle(gate)
        assert ran == [SANDBOX]
        assert starter.current(SANDBOX) == NAMED
        assert ("ready", NAMED) in starter.told
        assert gate.state_of(SANDBOX) == READY

    @pytest.mark.asyncio
    async def test_boot_does_not_take_ready_for_another_deployment(
        self, ledger: sqlite3.Connection
    ) -> None:
        _record_deployment(ledger)
        a_build(ledger, "RUNNING")  # unfinished work: boot brings it up
        starter = FakeStarter(running=True)  # up, and "ready" for none
        ran: list[str] = []

        async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
            ran.append(name)
            return RedeployOutcome(ok=True)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.boot() == []
        assert ran == [SANDBOX] and starter.current(SANDBOX) == NAMED
