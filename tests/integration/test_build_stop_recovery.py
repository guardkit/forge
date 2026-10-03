"""A coordinator-only restart while a build's original run is still alive.

Review round 2 (4 October 2026), findings R2 and R3, through the PRODUCTION
path: ``handle_message`` → ``dispatch_build`` → the real approval gate → the
real async-task starter (deepagents middleware, as ``forge serve`` builds it)
launching onto a real ``langgraph dev`` runner; the real identity provider
(``async_tasks`` + ``runs.list``), the real stream source and translator in
the lifecycle bridge, and the real :class:`AckAfterStop` asking the runner's
real stop route. GuardKit is the stand-in script; the approval card's
transport is the in-memory NATS double the gate tests use.

Process 1 dispatches the build, it is approved and launched; a labelled
fixture container is started for it and its removal is refused for a while.
Then the coordinator restarts (its watcher dies, boot recovery marks the row
INTERRUPTED) while the runner — and the original child and fixture — carry
on. The redelivered message reaches process 2:

* approve: nothing happens (no card, no ack) until the original's processes
  AND fixture are gone; then the card; on approval the replacement launches,
  is observed on its OWN thread and run, completes, and the message is
  acknowledged once; the original's cancelled terminal is never published,
  never acknowledges and never stops the replacement.
* reject: the same hold; the rejection's acknowledgement comes only once the
  original's processes and fixture are gone.
* policy refusal (boot replay under a policy that refuses the repository):
  the same hold before the refusal's acknowledgement.
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
    _paused_subject,
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


def _strict_config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "publication": {"builds_may_run_inside_the_coordinator": False},
        }
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
            build_runner_stop_check,
        )
        from forge.cli.serve import _build_async_subagent_middleware
        from forge.lifecycle_bridge.bridge import LifecycleBridge
        from forge.lifecycle_bridge.build_stop import AckAfterStop
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
        self.guard = AckAfterStop(
            build_runner_stop_check(sqlite_pool=pool, default_url=url),
            recheck_seconds=0.5,
        )
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
            ack_guard=self.guard,
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
            ack_guard=self.guard,
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


@pytest.mark.parametrize("ending", ["approve", "reject", "policy"])
def test_a_restart_relaunch_stops_the_original_first(
    nats, pool, tmp_path, ending  # noqa: F811 — imported fixtures
) -> None:
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
        from forge.adapters.nats.pipeline_consumer import handle_message

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
        cards_before = len(nats.published.get(_paused_subject(FEATURE), []))

        def _at_ack() -> None:
            seen["at_ack_marked"] = marked_alive(build_id)
            seen["at_ack_fixture"] = container_running(fixture["id"])

        second = _Msg(_envelope(correlation), on_ack=_at_ack)
        two = _Coordinator(
            nats, pool, url, _strict_config() if ending == "policy" else _open_config()
        )
        if ending == "policy":
            # Boot replay of the recovered row under a policy that refuses it.
            tasks.append(
                asyncio.ensure_future(
                    two.deps.dispatch_build(
                        BuildQueuedPayload.model_validate(
                            json.loads(second.data)["payload"]
                        ),
                        second.ack,
                        runless_replay=True,
                    )
                )
            )
        else:
            tasks.append(asyncio.ensure_future(handle_message(second, two.deps)))

        # Held: the original's processes are stopped, its fixture cannot be
        # removed yet, so nothing else happens — no card, no acknowledgement.
        await asyncio.sleep(4.0)
        seen["held_acks"] = second.acks
        seen["held_cards"] = len(nats.published.get(_paused_subject(FEATURE), [])) - cards_before
        seen["held_fixture"] = container_running(fixture["id"])
        seen["original_gone"] = [p for p, s in original if proc_alive(p, s)] == []

        refuse.unlink()  # the fixture can go now
        if ending in ("approve", "reject"):
            await _approve_or_reject(
                nats, pool, build_id, "approve" if ending == "approve" else "reject"
            )
        await _wait_until(lambda: second.acks >= 1, timeout=90, what="the ack")
        await asyncio.sleep(2.0)  # a second acknowledgement would land here
        seen["acks"] = second.acks
        seen["first_acks"] = first.acks
        seen["original_identity"] = original_identity
        seen["observed"] = list(two.resolved)
        seen["published"] = [name for name, _ in two.publisher.published]
        await two.guard.shutdown()
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

    # Held while anything of the original was alive.
    assert seen["original_gone"]
    assert seen["held_fixture"], "the fixture should still have been up"
    assert seen["held_acks"] == 0 and seen["held_cards"] == 0
    # Acknowledged once, and only when nothing of the original was left.
    assert seen["acks"] == 1 and seen["first_acks"] == 0
    assert seen["at_ack_marked"] == [] and seen["at_ack_fixture"] is False
    if ending == "approve":
        # The replacement ran — on its own thread — and completed.
        relaunch = json.loads((estate.records / f"{FEATURE}.relaunch.started").read_text())
        assert relaunch["others_alive"] == {FEATURE: []}
        assert seen["observed"] and all(
            ident[0] != seen["original_identity"][0] for ident in seen["observed"]
        ), seen
        assert "publish_build_complete" in seen["published"], seen["published"]
        assert "publish_build_cancelled" not in seen["published"]
    else:
        assert not (estate.records / f"{FEATURE}.relaunch.started").exists()
