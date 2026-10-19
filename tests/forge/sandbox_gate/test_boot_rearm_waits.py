"""A paused build's recovery waits, as one unit, for its sandbox (Codex S2
implementation review 2, R3).

The coordinator restarts with a build PAUSED at its card and a run recorded
for it. The build's sandbox does not come up at boot: its recorded run is not
interrupted and its card is not re-armed. Once the sandbox is ready — without
another restart — the interrupt and the re-arm happen, in that order. Built on
the real approval gate harness of ``tests/forge/cli/test_interrupt_recorded_run``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from forge.sandbox_gate import gate as gate_module
from forge.sandbox_gate.gate import SandboxGate

from tests.forge.cli.test_interrupt_recorded_run import (  # noqa: F401 — fixtures
    OTHER,
    _paused_after_a_first_restart,
    recovery,
    runners,
)
from tests.forge.sandbox_gate._fakes import HELPER, RUNNER, SANDBOX, Clock, FakeStarter, Services


def test_a_paused_builds_interrupt_and_rearm_wait_for_its_sandbox(
    runners, recovery, monkeypatch  # noqa: F811
) -> None:
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    _paused_after_a_first_restart(recovery)
    cards_before = recovery.cards()
    interrupted_before = list(runners["global"].cancelled)  # the first restart's
    seen: list[str] = []
    runners["global"].probe = lambda: seen.append(recovery.status())

    starter = FakeStarter(running=False)
    starter.reachable = False  # the starter does not answer at boot
    entry = SimpleNamespace(name=SANDBOX, sidecar_url=HELPER, runner_url=RUNNER)
    clock = Clock()

    async def _go() -> None:
        gate = SandboxGate(
            sandboxes={SANDBOX: (entry, (OTHER,))},
            starter=starter,  # type: ignore[arg-type]
            connection=lambda: recovery.pool.connection,
            http_get=Services(starter),
            clock=clock.now,
            sleep=clock.sleep,
            ready_timeout_seconds=60,
        )
        gate_module.install_gate(gate)
        try:
            assert await gate.boot() == [SANDBOX]
            assert await recovery.rearm() == []  # nothing re-armed now
            assert runners["global"].cancelled == interrupted_before  # not interrupted now
            assert recovery.cards() == cards_before
            assert recovery.status() == "PAUSED"

            # The starter answers again; the next look brings the sandbox up,
            # and the deferred recovery runs — no coordinator restart.
            starter.reachable = True
            await gate.poll_once()
            await asyncio.wait_for(asyncio.shield(gate._slots[SANDBOX].bring_up), 10)
            deadline = asyncio.get_running_loop().time() + 30
            while recovery.cards() == cards_before:
                assert asyncio.get_running_loop().time() < deadline, "no card re-armed"
                await asyncio.sleep(0.05)
            assert runners["global"].cancelled == interrupted_before + ["thread-original"]
            assert seen == ["PAUSED"]  # interrupted before the card, as always
            await recovery.answer("reject")
            while recovery.status() != "CANCELLED":
                assert asyncio.get_running_loop().time() < deadline, "not answered"
                await asyncio.sleep(0.05)
        finally:
            gate_module.install_gate(None)

    asyncio.run(_go())
    assert recovery.cards() == cards_before + 1
