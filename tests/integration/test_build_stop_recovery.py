"""A coordinator-only restart while a build's original run is still alive.

Through the PRODUCTION path: ``handle_message`` → ``dispatch_build`` → the real
approval gate → the real async-task starter (deepagents middleware, as
``forge serve`` builds it) launching onto a real ``langgraph dev`` runner; the
real identity provider (``async_tasks`` + ``runs.list``), the real stream
source and translator in the lifecycle bridge. GuardKit is the stand-in
script; the approval card's transport is the in-memory NATS double the gate
tests use.

Process 1 dispatches the build, it is approved and launched; a labelled
fixture container is started for it and its removal is refused for a while.
The coordinator restarts (its watcher dies, boot recovery marks the row
INTERRUPTED) while the runner — and the original child and fixture — carry
on. The redelivered message reaches process 2, which clears the original's
identity and relaunches on approval. The runner stops the original first and
the relaunch does not spawn until the original's processes and fixture are
gone; the replacement is observed on its OWN thread and run, completes, and
the message is acknowledged once; the original's cancelled terminal is never
published and never acknowledges.

And when the original's identity cannot be cleared, nothing is launched and
the message is held for its redelivery.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildQueuedPayload

from forge.cli import _serve_deps_gating
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.config.models import ForgeConfig
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.lifecycle.identifiers import derive_build_id
from forge.persistence.migrations import (
    lifecycle_bridge_registry as bridge_migration,
)
from tests.forge.build_stop_support import (
    container_running,
    docker_available,
    kill_marked,
    kill_recorded,
    make_estate,
    marked_alive,
    proc_alive,
    real_runner,
    refusing_engine,
    remove_test_containers,
    start_fixture_container,
)

from .test_gate_activation_production_wiring import (  # noqa: F401 — fixtures
    FixedClock,
    OrderRecordingNats,
    _build_parts,
    _drive_response,
    _row,
    _wait_until,
    nats,
    pool,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc"),
    pytest.mark.skipif(not docker_available(), reason="needs docker and busybox:1.36"),
]

FEATURE = "FEAT-RS7A"
QUEUED_AT = datetime(2026, 10, 4, 9, 0, 0, tzinfo=UTC)


class _Msg:
    """The build-queued message, counting acknowledgements."""

    def __init__(self, data: bytes, on_ack: Any = None) -> None:
        self.data = data
        self.subject = f"pipeline.build-queued.{FEATURE}"
        self.acks = 0
        self._on_ack = on_ack

    async def ack(self) -> None:
        if self._on_ack is not None:
            self._on_ack()
        self.acks += 1

    async def nak(self) -> None:  # pragma: no cover
        raise AssertionError("never negatively acknowledged")


class _Publisher:
    """Records every lifecycle envelope the bridge publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("publish_"):
            raise AttributeError(name)

        async def _record(*args: Any, **kwargs: Any) -> None:
            self.published.append((name, kwargs.get("payload", args[0] if args else None)))

        return _record


def _envelope(correlation_id: str) -> bytes:
    payload = BuildQueuedPayload(
        feature_id=FEATURE,
        repo="example/example",
        branch="main",
        feature_yaml_path=f"/srv/forge/features/{FEATURE}/{FEATURE}.yaml",
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=correlation_id,
        requested_at=QUEUED_AT,
        queued_at=QUEUED_AT,
    )
    return (
        MessageEnvelope(
            source_id="forge-cli",
            event_type=EventType.BUILD_QUEUED,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode("utf-8")
    )


def _open_config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
    )


class _Coordinator:
    """One coordinator process: gate, consumer deps, lifecycle bridge."""

    def __init__(self, nats: Any, pool: Any, url: str, cfg: ForgeConfig) -> None:
        from forge.cli._serve_production import (
            _build_async_tasks_identity_provider,
            _resolve_async_task_starter,
        )
        from forge.cli.serve import _build_async_subagent_middleware
        from forge.lifecycle_bridge.bridge import LifecycleBridge
        from forge.lifecycle_bridge.run_state_source import (
            langgraph_run_state_fetcher,
        )
        from forge.lifecycle_bridge.stream_source import langgraph_stream_source
        from forge.lifecycle_bridge.translation import StreamEventTranslator
        from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
        from forge.persistence.repositories.bridge_registry import BridgeRegistry

        _serve_deps_gating._reset_for_tests()
        _serve_deps_gating.bind_gate_parts(_build_parts(nats, forge_config=cfg))
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        self.publisher = _Publisher()
        identity = _build_async_tasks_identity_provider(
            sqlite_pool=pool, autobuild_runner_url=url
        )
        self.resolved: list[tuple[str, str]] = []

        async def _identity(feature_id: str, correlation_id: str) -> Any:
            found = await identity(feature_id, correlation_id)
            if found is not None:
                self.resolved.append(found)
            return found

        self.wireup = LifecycleBridgeWireup(
            bridge=LifecycleBridge(registry=BridgeRegistry(connection=pool.connection)),
            translator=StreamEventTranslator(),
            publisher=self.publisher,
            stream_source=langgraph_stream_source(runner_url=url),
            identity_provider=_identity,
            run_state_fetcher=langgraph_run_state_fetcher(runner_url=url),
        )
        starter = _resolve_async_task_starter(
            _build_async_subagent_middleware(autobuild_runner_url=url)
        )
        self.deps = build_pipeline_consumer_deps(
            nats,
            cfg,
            pool,
            async_task_starter=starter,
            register_ack_handle=self.wireup.register_ack_handle,
            gate_repository=repo,
            gate_state_machine=sm,
            gate_clock=FixedClock(),
        )

    async def restart(self) -> None:
        """This coordinator dies: its watchers and checkers go with it."""
        await self.wireup.shutdown()


def _card(pool: Any, build_id: str) -> str | None:
    row = pool.connection.execute(
        "SELECT pending_approval_request_id FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return None if row is None else row[0]


async def _approve_or_reject(nats: Any, pool: Any, build_id: str, decision: str) -> None:
    deadline = asyncio.get_running_loop().time() + 60
    while not _card(pool, build_id):
        assert asyncio.get_running_loop().time() < deadline, "no card"
        await asyncio.sleep(0.05)
    await _drive_response(
        nats, build_id=build_id, request_id=_card(pool, build_id), decision=decision
    )


async def _boot_recovery(pool: Any) -> None:
    from unittest.mock import AsyncMock

    from forge.lifecycle.recovery import reconcile_on_boot

    await reconcile_on_boot(pool, AsyncMock(), AsyncMock())


def test_a_restart_relaunch_is_observed_on_its_own_run(
    nats, pool, tmp_path  # noqa: F811 — imported fixtures
) -> None:
    from forge.adapters.nats.pipeline_consumer import handle_message

    bridge_migration.apply(pool.connection)
    estate = make_estate(tmp_path / "estate")
    correlation = f"corr-restart-{uuid.uuid4().hex[:8]}"
    build_id = derive_build_id(FEATURE, QUEUED_AT)
    estate.add_build(FEATURE, build_id, branch="main", relaunch_run_seconds=1)
    wrapper, refuse = refusing_engine(tmp_path)
    fixture: dict[str, str] = {}

    async def _go(url: str) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        # --- coordinator process 1: dispatch, approve, launch -------------
        one = _Coordinator(nats, pool, url, _open_config())
        first = _Msg(_envelope(correlation))
        # The gate waits for the card's answer inside the dispatch, so each
        # delivery runs as its own task, as the daemon runs it.
        tasks = [asyncio.ensure_future(handle_message(first, one.deps))]
        await _approve_or_reject(nats, pool, build_id, "approve")
        original = await asyncio.to_thread(estate.pids, FEATURE)
        fixture["id"] = await asyncio.to_thread(start_fixture_container, build_id)
        await _wait_until(lambda: one.resolved, timeout=30, what="process 1 observes")
        original_identity = one.resolved[0]

        # --- the coordinator restarts; the runner does not ---------------
        await one.restart()
        await _boot_recovery(pool)
        assert _row(pool, build_id)[0] == "INTERRUPTED"

        def _at_ack() -> None:
            seen["at_ack_marked"] = marked_alive(build_id)
            seen["at_ack_fixture"] = container_running(fixture["id"])

        second = _Msg(_envelope(correlation), on_ack=_at_ack)
        two = _Coordinator(nats, pool, url, _open_config())
        tasks.append(asyncio.ensure_future(handle_message(second, two.deps)))
        await _approve_or_reject(nats, pool, build_id, "approve")

        # The runner stops the original first; its fixture cannot be removed
        # yet, so the relaunch does not spawn.
        await asyncio.sleep(4.0)
        seen["original_gone"] = [p for p, s in original if proc_alive(p, s)] == []
        seen["held_fixture"] = container_running(fixture["id"])
        seen["relaunched_while_held"] = (
            estate.records / f"{FEATURE}.relaunch.started"
        ).exists()

        refuse.unlink()  # the fixture can go now
        await _wait_until(lambda: second.acks >= 1, timeout=90, what="the ack")
        await asyncio.sleep(2.0)  # a second acknowledgement would land here
        seen["acks"] = second.acks
        seen["first_acks"] = first.acks
        seen["original_identity"] = original_identity
        seen["observed"] = list(two.resolved)
        seen["published"] = [name for name, _ in two.publisher.published]
        await two.wireup.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return seen

    try:
        with real_runner(
            estate,
            "restart",
            jobs=2,
            extra_env={"FORGE_FIXTURE_ENGINE": str(wrapper)},
        ) as runner:
            seen = asyncio.run(asyncio.wait_for(_go(runner.url), timeout=240))
    finally:
        kill_recorded(estate)
        kill_marked([build_id])
        remove_test_containers([build_id])
        _serve_deps_gating._reset_for_tests()

    # The runner stopped the original and held the relaunch while the
    # original's fixture was up.
    assert seen["original_gone"]
    assert seen["held_fixture"], "the fixture should still have been up"
    assert not seen["relaunched_while_held"]
    # The replacement ran — on its own thread — after every process of the
    # original was gone, completed, and was acknowledged once.
    relaunch = json.loads((estate.records / f"{FEATURE}.relaunch.started").read_text())
    assert relaunch["others_alive"] == {FEATURE: []}
    assert seen["observed"] and all(
        ident[0] != seen["original_identity"][0] for ident in seen["observed"]
    ), seen
    assert "publish_build_complete" in seen["published"], seen["published"]
    assert "publish_build_cancelled" not in seen["published"]
    assert seen["acks"] == 1 and seen["first_acks"] == 0
    assert seen["at_ack_marked"] == [] and seen["at_ack_fixture"] is False


def test_a_relaunch_whose_earlier_identity_cannot_be_cleared_is_held(
    nats, pool, monkeypatch  # noqa: F811 — imported fixtures
) -> None:
    """The card is approved but the earlier run's identity cannot be cleared:
    nothing is launched and the message is held for its redelivery."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli import _serve_gate_activation
    from forge.cli._serve_deps_state_channel import (
        build_autobuild_state_initialiser,
    )

    _serve_deps_gating._reset_for_tests()
    cfg = _open_config()
    _serve_deps_gating.bind_gate_parts(_build_parts(nats, forge_config=cfg))
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())

    class _NoLaunch:
        async def astart_async_task(self, **_: Any) -> str:
            raise AssertionError("nothing may launch")

    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=_NoLaunch(),
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
    )
    data = _envelope(f"corr-clear-{uuid.uuid4().hex[:8]}")
    payload = BuildQueuedPayload.model_validate(json.loads(data)["payload"])
    build_id = pool.record_pending_build(payload)
    build_autobuild_state_initialiser(pool).initialise_autobuild_state(
        build_id=build_id,
        feature_id=FEATURE,
        task_id="thread-earlier",
        correlation_id=payload.correlation_id,
        lifecycle="starting",
        wave_index=0,
        task_index=0,
    )
    # Boot recovery's verdict on a build whose run it could not see.
    pool.connection.execute(
        "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?", (build_id,)
    )
    pool.connection.execute(
        "CREATE TRIGGER refuse_delete BEFORE DELETE ON async_tasks "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    async def _approved(**_: Any) -> Any:
        return _serve_gate_activation.GateOutcome.RESUMED

    monkeypatch.setattr(_serve_gate_activation, "maybe_gate_build", _approved)
    msg = _Msg(data)
    try:
        asyncio.run(handle_message(msg, deps))
    finally:
        _serve_deps_gating._reset_for_tests()
    assert msg.acks == 0
    assert nats.published.get(f"pipeline.build-failed.{FEATURE}", []) == []


def _envelope_at(correlation_id: str, queued_at: datetime) -> bytes:
    payload = BuildQueuedPayload(
        feature_id=FEATURE,
        repo="example/example",
        branch="main",
        feature_yaml_path=f"/srv/forge/features/{FEATURE}/{FEATURE}.yaml",
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=correlation_id,
        requested_at=queued_at,
        queued_at=queued_at,
    )
    return (
        MessageEnvelope(
            source_id="forge-cli",
            event_type=EventType.BUILD_QUEUED,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode("utf-8")
    )


def test_an_interrupted_build_still_goes_first_after_a_restart(
    nats, pool, monkeypatch  # noqa: F811 — imported fixtures
) -> None:
    """Coach finding 1: after a factory-only restart build c1 is INTERRUPTED
    (its run may still be going). A second build c2 of the feature is refused;
    c1's redelivery is not — it is relaunched through the gate."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli import _serve_gate_activation

    _serve_deps_gating._reset_for_tests()
    cfg = _open_config()
    _serve_deps_gating.bind_gate_parts(_build_parts(nats, forge_config=cfg))
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=object(),
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
    )
    c1 = _envelope_at("corr-first", QUEUED_AT)
    c2 = _envelope_at("corr-second", datetime(2026, 10, 4, 9, 5, 0, tzinfo=UTC))
    c1_build = pool.record_pending_build(
        BuildQueuedPayload.model_validate(json.loads(c1)["payload"])
    )
    pool.connection.execute(
        "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?", (c1_build,)
    )
    gated: list[str] = []

    async def _gate(**kwargs: Any) -> Any:
        gated.append(kwargs["build_id"])
        return _serve_gate_activation.HOLD_SLOT

    monkeypatch.setattr(_serve_gate_activation, "maybe_gate_build", _gate)
    second, first = _Msg(c2), _Msg(c1)
    try:
        asyncio.run(handle_message(second, deps))
        asyncio.run(handle_message(first, deps))
    finally:
        _serve_deps_gating._reset_for_tests()
    assert second.acks == 1, "c2 should have been refused while c1 is recovering"
    assert gated == [c1_build], gated
    assert first.acks == 0
    failed = [
        json.loads(b)["payload"]
        for b in nats.published.get(f"pipeline.build-failed.{FEATURE}", [])
    ]
    assert len(failed) == 1 and "already in progress" in str(failed[0])


def test_a_declined_relaunch_interrupts_the_original(
    nats, pool, tmp_path, monkeypatch  # noqa: F811 — imported fixtures
) -> None:
    """Coach finding 2: after a factory-only restart the relaunch's card is
    declined. The original run, still going, is interrupted (the runner's
    fence then stops everything it owns) and the message is acknowledged."""
    from forge.adapters.nats.pipeline_consumer import handle_message

    bridge_migration.apply(pool.connection)
    estate = make_estate(tmp_path / "estate")
    correlation = f"corr-decline-{uuid.uuid4().hex[:8]}"
    build_id = derive_build_id(FEATURE, QUEUED_AT)
    estate.add_build(FEATURE, build_id, branch="main")

    async def _go(url: str) -> dict[str, Any]:
        # The factory's runner address, as forge serve has it.
        monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", url)
        one = _Coordinator(nats, pool, url, _open_config())
        first = _Msg(_envelope(correlation))
        tasks = [asyncio.ensure_future(handle_message(first, one.deps))]
        await _approve_or_reject(nats, pool, build_id, "approve")
        original = await asyncio.to_thread(estate.pids, FEATURE)
        await _wait_until(lambda: one.resolved, timeout=30, what="process 1 observes")
        await one.restart()
        await _boot_recovery(pool)

        second = _Msg(_envelope(correlation))
        two = _Coordinator(nats, pool, url, _open_config())
        tasks.append(asyncio.ensure_future(handle_message(second, two.deps)))
        await _approve_or_reject(nats, pool, build_id, "reject")
        await _wait_until(lambda: second.acks >= 1, timeout=60, what="the ack")
        deadline = asyncio.get_running_loop().time() + 30
        while any(proc_alive(p, s) for p, s in original):
            assert asyncio.get_running_loop().time() < deadline, "original still runs"
            await asyncio.sleep(0.2)
        seen = {"acks": second.acks}
        await two.wireup.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return seen

    try:
        with real_runner(estate, "decline", jobs=2) as runner:
            seen = asyncio.run(asyncio.wait_for(_go(runner.url), timeout=180))
    finally:
        kill_recorded(estate)
        kill_marked([build_id])
        _serve_deps_gating._reset_for_tests()
    assert seen["acks"] == 1
    assert not (estate.records / f"{FEATURE}.relaunch.started").exists()


@pytest.mark.parametrize("ending", ["reject", "approve-launch-fails"])
def test_a_rearmed_card_after_a_second_restart_interrupts_the_original(
    nats, pool, tmp_path, monkeypatch, ending  # noqa: F811 — imported fixtures
) -> None:
    """Review R10: restart (the recovered build's card is shown and left
    PAUSED), restart again, and the re-armed card is rejected — or approved
    and the relaunch fails: either way the original run, still going, is
    interrupted and the runner's fence stops it."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli._serve_gate_activation import rearm_paused_gates

    bridge_migration.apply(pool.connection)
    estate = make_estate(tmp_path / "estate")
    correlation = f"corr-rearm-{uuid.uuid4().hex[:8]}"
    build_id = derive_build_id(FEATURE, QUEUED_AT)
    estate.add_build(FEATURE, build_id, branch="main")

    async def _go(url: str) -> bool:
        monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", url)
        one = _Coordinator(nats, pool, url, _open_config())
        tasks = [asyncio.ensure_future(handle_message(_Msg(_envelope(correlation)), one.deps))]
        await _approve_or_reject(nats, pool, build_id, "approve")
        original = await asyncio.to_thread(estate.pids, FEATURE)
        await _wait_until(lambda: one.resolved, timeout=30, what="process 1 observes")
        await one.restart()
        await _boot_recovery(pool)

        # Process 2: the recovered build's card is shown, then process 2 dies.
        two = _Coordinator(nats, pool, url, _open_config())
        tasks.append(
            asyncio.ensure_future(handle_message(_Msg(_envelope(correlation)), two.deps))
        )
        deadline = asyncio.get_running_loop().time() + 30
        while _row(pool, build_id)[0] != "PAUSED":
            assert asyncio.get_running_loop().time() < deadline, "no PAUSED card"
            await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await two.restart()
        assert any(proc_alive(p, s) for p, s in original), "original should still run"

        # Process 3: re-arms the card; it is rejected.
        _serve_deps_gating._reset_for_tests()
        cfg = _open_config()
        parts = _build_parts(nats, forge_config=cfg)
        _serve_deps_gating.bind_gate_parts(parts)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())

        launches: list[str] = []

        async def _no_launch(**kwargs: Any) -> None:
            launches.append(kwargs["build_id"])
            raise RuntimeError("the relaunch could not be submitted")

        rearmed = await rearm_paused_gates(
            parts=parts,
            sqlite_pool=pool,
            gate_repository=repo,
            gate_state_machine=sm,
            resume_launcher=_no_launch,
            client=nats,
            clock=FixedClock(),
            forge_config=cfg,
        )
        await _approve_or_reject(
            nats, pool, build_id, "reject" if ending == "reject" else "approve"
        )
        await asyncio.wait_for(
            asyncio.gather(*rearmed, return_exceptions=True), timeout=30
        )
        assert launches == ([] if ending == "reject" else [build_id])
        deadline = asyncio.get_running_loop().time() + 30
        while any(proc_alive(p, s) for p, s in original):
            assert asyncio.get_running_loop().time() < deadline, "original still runs"
            await asyncio.sleep(0.2)
        return True

    try:
        with real_runner(estate, "rearm", jobs=2) as runner:
            assert asyncio.run(asyncio.wait_for(_go(runner.url), timeout=180))
    finally:
        kill_recorded(estate)
        kill_marked([build_id])
        _serve_deps_gating._reset_for_tests()
