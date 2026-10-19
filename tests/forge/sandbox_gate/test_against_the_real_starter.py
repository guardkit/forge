"""The gate and the holds against the real starter, run in this process (Codex
S2 implementation review 1, R1).

The starter (``deploy/compose/sandbox-starter/starter.py``) keeps the
deployment it was told durably, so the coordinator always says it: "none"
while the ledger records none, then the recorded identity once one exists.
The scenario: an empty ledger and a start; then a first deployment; then an
outside command's wake, which must wait for the coordinator's redeploy and be
confirmed for that very deployment.

The starter is driven with its own test's stand-in ``docker`` and ``sbx`` and
stand-in helper and runner; no real sandbox, engine or daemon is touched. The
module is skipped where the starter is not in the tree.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
if not (ROOT / "deploy" / "compose" / "sandbox-starter" / "starter.py").exists():
    pytest.skip("the sandbox starter is not in this tree", allow_module_level=True)

from forge.config.models import ForgeConfig  # noqa: E402
from forge.sandbox_gate.gate import READY, RedeployOutcome, SandboxGate, managed_sandboxes  # noqa: E402
from forge.sandbox_gate.holds import hold_for_config  # noqa: E402
from forge.sandbox_gate.starter_client import StarterClient  # noqa: E402

from tests.forge.deploy.test_sandbox_starter import NAME, world  # noqa: E402,F401
from tests.forge.sandbox_gate._fakes import Clock, a_ledger  # noqa: E402

REPO = "example-org/private-notes"
PROJECT = "private-notes-live"
KEY = "org.example.identity"


def _config(services_url: str, starter_url: str) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {REPO: "/tmp/private-notes"},
                "sandbox_starter": {"url": starter_url},
                "sandboxes": {
                    REPO: {
                        "name": NAME,
                        "sidecar_url": services_url,
                        "runner_url": services_url,
                        "idle_stop": True,
                        "deployment_compose_project": PROJECT,
                        "deployment_identity_label": KEY,
                    }
                },
            },
        }
    )


def _status(url: str) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(f"{url}/status/{NAME}", timeout=10) as answer:
        return json.loads(answer.read())


@pytest.mark.asyncio
async def test_none_then_a_first_deployment_then_an_outside_wake(world: Any, tmp_path: Path) -> None:  # noqa: F811
    starter = world.make()
    url = world.http(starter)
    config = _config(world.services.url, url)
    ledger: sqlite3.Connection = a_ledger(tmp_path)
    clock = Clock()
    redeployed: list[str] = []

    async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
        redeployed.append(name)
        return RedeployOutcome(ok=True)

    gate = SandboxGate(
        sandboxes=managed_sandboxes(config),
        starter=StarterClient(url),
        connection=lambda: ledger,
        redeploy=redeploy,
        clock=clock.now,
        sleep=clock.sleep,
        poll_interval_seconds=0.05,
    )

    # 1. An empty ledger: the start says "none", and ready needs no redeploy.
    assert await gate.ensure_ready(NAME)
    said = _status(url)
    assert said["deployment"] is None and said["readiness"] == READY
    assert starter.sandboxes[NAME].deployment_told == {}  # told "none", and kept
    assert redeployed == []

    # 2. A first deployment is recorded; the idle stop names it.
    ledger.execute(
        "INSERT INTO deployment_targets (target, counter, running_commit, running_identity, "
        "running_build, running_at) VALUES (?, 1, 'abc1234', 'private-notes@abc1234', 'B-1', "
        "'2026-10-19T09:00:00+00:00')",
        (f"{REPO}::production",),
    )
    ledger.commit()
    # The next look sees the starter hold "none" while the ledger records a
    # deployment (Codex review 2, R1): it is told, and the deployment restored.
    await gate.poll_once()
    await asyncio.wait_for(asyncio.shield(gate._slots[NAME].bring_up), timeout=30)
    assert redeployed == [NAME]
    await gate.poll_once()  # idle from here
    clock.advance(31 * 60)
    await gate.poll_once()
    task = gate._slots[NAME].stop_task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    named = {"compose_project": PROJECT, "identity_label": f"{KEY}=private-notes@abc1234"}
    said = _status(url)
    assert said["deployment"] == named  # "none" did not outlive the first deployment
    assert said["container"]["state"] != "running"

    # 3. An outside command wakes it until deployed; the coordinator's poll
    #    redeploys and confirms ready for that very deployment.
    got: list[Any] = []

    def outside() -> None:
        # Another process in life: a connection of its own to the same ledger.
        from forge.adapters.sqlite import connect as sqlite_connect

        own = sqlite_connect.connect_writer(tmp_path / "forge.db")
        try:
            with hold_for_config(
                config, REPO, command="forge merge-deploy", until="deployed", connection=own
            ) as hold_id:
                got.append(hold_id)
        except Exception as exc:  # noqa: BLE001 — reported through ``got``
            got.append(exc)
        finally:
            own.close()

    holder = threading.Thread(target=outside)
    holder.start()
    for _ in range(200):
        await asyncio.sleep(0.05)
        await gate.poll_once()
        slot = gate._slots[NAME]
        if slot.bring_up is not None:
            await asyncio.wait_for(asyncio.shield(slot.bring_up), timeout=30)
        if got:
            break
    holder.join(30)
    assert got and isinstance(got[0], str) and got[0].startswith("hold-"), got
    assert redeployed == [NAME, NAME]  # the wake's redeploy, confirmed before the hold returned
    said = _status(url)
    assert said["readiness"] == READY and said["ready_confirmed_for"] == named
    assert gate.state_of(NAME) == READY



@pytest.mark.asyncio
async def test_a_first_deployment_then_a_services_only_hold_with_no_stop_between(
    world: Any, tmp_path: Path  # noqa: F811
) -> None:
    """Codex review 2, R1: the starter was last told "none" and is ready; a
    first deployment is recorded; an outside command holds it for services
    only, with no coordinator stop in between. The coordinator does not take
    the starter's "ready" as it stands: it tells it the recorded deployment,
    the starter reports "pending", and the redeploy is confirmed for it."""
    starter = world.make()
    url = world.http(starter)
    config = _config(world.services.url, url)
    ledger: sqlite3.Connection = a_ledger(tmp_path)
    clock = Clock()
    redeployed: list[str] = []

    async def redeploy(name: str, repos: tuple[str, ...]) -> RedeployOutcome:
        redeployed.append(name)
        return RedeployOutcome(ok=True)

    gate = SandboxGate(
        sandboxes=managed_sandboxes(config),
        starter=StarterClient(url),
        connection=lambda: ledger,
        redeploy=redeploy,
        clock=clock.now,
        sleep=clock.sleep,
        poll_interval_seconds=0.05,
    )
    assert await gate.ensure_ready(NAME)
    assert _status(url)["readiness"] == READY and redeployed == []
    ledger.execute(
        "INSERT INTO deployment_targets (target, counter, running_commit, running_identity, "
        "running_build, running_at) VALUES (?, 1, 'abc1234', 'private-notes@abc1234', 'B-1', "
        "'2026-10-19T09:00:00+00:00')",
        (f"{REPO}::production",),
    )
    ledger.commit()

    # register-repo's hold: services only, and it cannot read the ledger.
    hold = await asyncio.to_thread(
        StarterClient(url).hold_sync, NAME, owner="forge register-repo", until="services"
    )
    assert hold.ok, hold.sentence
    try:
        await gate.poll_once()
        slot = gate._slots[NAME]
        if slot.bring_up is not None:
            await asyncio.wait_for(asyncio.shield(slot.bring_up), timeout=30)
    finally:
        await asyncio.to_thread(StarterClient(url).release_sync, hold.hold_id or "")
    named = {"compose_project": PROJECT, "identity_label": f"{KEY}=private-notes@abc1234"}
    said = _status(url)
    assert redeployed == [NAME]
    assert said["deployment"] == named
    assert said["readiness"] == READY and said["ready_confirmed_for"] == named
    assert gate.state_of(NAME) == READY
