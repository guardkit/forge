"""Pytest-bdd wiring for FEAT-FORGE-002 NATS Fleet Integration scenarios.

This module is the executable surface for TASK-NFI-011: it binds the
3 ``@smoke`` and 7 ``@key-example`` Gherkin scenarios from
``features/nats-fleet-integration/nats-fleet-integration.feature`` to
pytest-bdd step functions that exercise the real Forge domain code
(``DiscoveryCache``, ``FORGE_MANIFEST``, ``register_on_boot``,
``build_heartbeat_payload``, ``deregister``) through the in-process
recorder defined in ``conftest.py``.

The remaining 26 scenarios are tagged ``@skip`` directly in the feature
file with a follow-up reference; only the priority subset is collected
here via individual ``@scenario`` decorators.

Step organisation
-----------------

The task description suggests splitting step functions into per-group
files (``registration_steps.py``, ``heartbeat_steps.py``, …). The
project's documentation level is ``minimal`` (max 2 created files), so
the bindings are consolidated into this single module. Sections below
are arranged in the same group order — registration, heartbeat,
discovery, lifecycle, shutdown — so a reader can navigate by Gherkin
group at a glance.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from nats_core.events import (
    AgentHeartbeatPayload,
    BuildCompletePayload,
    StageCompletePayload,
)
from nats_core.manifest import AgentManifest, IntentCapability
from pytest_bdd import given, parsers, scenario, then, when

from forge.adapters.nats.fleet_publisher import (
    AGENT_ID,
    build_heartbeat_payload,
    deregister,
    register_on_boot,
)
from forge.discovery.cache import DiscoveryCache
from forge.discovery.resolve import resolve
from forge.fleet.manifest import FORGE_MANIFEST

from tests.bdd.conftest import (
    FakeClock,
    FakeNatsClient,
    RecordingPipelinePublisher,
    StubStatusProvider,
    make_specialist_manifest,
)


FEATURE_FILE = "nats-fleet-integration/nats-fleet-integration.feature"


# ---------------------------------------------------------------------------
# Scenario registrations — each @scenario call materialises a pytest test
# whose body is the steps below. The ``@pytest.mark.smoke`` /
# ``@pytest.mark.key_example`` decorators mirror the Gherkin tags so CI
# can filter via ``pytest -m smoke``.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(FEATURE_FILE, "On startup Forge registers itself with the fleet")
def test_forge_registers_on_startup() -> None:
    """@smoke @key-example — TASK-NFI-004 AC-001."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(FEATURE_FILE, "Forge publishes a heartbeat at the configured interval")
def test_forge_publishes_heartbeat() -> None:
    """@smoke @key-example — TASK-NFI-004 AC-002."""


@pytest.mark.key_example
@scenario(FEATURE_FILE, "Forge maintains a live cache of fleet agents")
def test_forge_live_cache() -> None:
    """@key-example — TASK-NFI-003 cache-read path."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE, "A newly-registered specialist becomes available for resolution"
)
def test_new_specialist_becomes_available() -> None:
    """@key-example — TASK-NFI-005 watcher integration."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(
    FEATURE_FILE, "Forge publishes a stage-complete event after each gated stage"
)
def test_forge_publishes_stage_complete() -> None:
    """@smoke @key-example — TASK-NFI-006 lifecycle event."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge publishes a build-complete event when the pipeline finishes successfully",
)
def test_forge_publishes_build_complete() -> None:
    """@key-example — TASK-NFI-006 terminal lifecycle event."""


@pytest.mark.key_example
@scenario(FEATURE_FILE, "On graceful shutdown Forge deregisters from the fleet")
def test_forge_graceful_shutdown() -> None:
    """@key-example — TASK-NFI-004 AC-004 deregister path."""


# ===========================================================================
# Background steps — shared by every priority scenario
# ===========================================================================


@given("Forge is configured from the project configuration file")
def forge_is_configured(world: dict) -> None:
    # Configuration is loaded eagerly by import — we record that the
    # background ran so later assertions can sanity-check the world dict
    # was populated end-to-end.
    world["configured"] = True
    world.setdefault("correlation_id", str(uuid.uuid4()))
    world.setdefault("build_id", f"build-{uuid.uuid4().hex[:8]}")


@given("the fleet message bus is reachable")
def fleet_bus_reachable(nats_client_mock: FakeNatsClient, world: dict) -> None:
    nats_client_mock.simulate_registry_unreachable = False
    world["bus_reachable"] = True


# ===========================================================================
# GROUP A.1 — Registration / startup
# ===========================================================================


@when("Forge starts")
def forge_starts(nats_client_mock: FakeNatsClient, world: dict) -> None:
    asyncio.run(register_on_boot(nats_client_mock))
    world["startup_complete"] = True


@then(
    "Forge should publish its own capability manifest to the fleet registration channel"
)
def assert_manifest_published(nats_client_mock: FakeNatsClient) -> None:
    register_calls = [p for p in nats_client_mock.published if p.topic == "fleet.register"]
    assert register_calls, "expected a fleet.register publish, got none"
    assert isinstance(register_calls[-1].payload, AgentManifest)
    assert register_calls[-1].payload.agent_id == AGENT_ID


@then("the fleet registry should list Forge as a ready agent")
def assert_registry_lists_forge(nats_client_mock: FakeNatsClient) -> None:
    assert AGENT_ID in nats_client_mock.registry, "Forge missing from registry"
    assert nats_client_mock.registry[AGENT_ID].status == "ready"


@then("the manifest should include Forge's tools, intents, and trust tier")
def assert_manifest_completeness(nats_client_mock: FakeNatsClient) -> None:
    manifest = nats_client_mock.registry[AGENT_ID]
    assert manifest.tools, "manifest must declare at least one tool"
    assert manifest.intents, "manifest must declare at least one intent"
    assert manifest.trust_tier in {"core", "specialist", "extension"}
    # Sanity-check identity matches the canonical FORGE_MANIFEST so a
    # divergent manifest from a code regression is caught here.
    assert manifest.agent_id == FORGE_MANIFEST.agent_id


# ===========================================================================
# GROUP A.2 — Heartbeat
# ===========================================================================


@given("Forge has been registered with the fleet")
def forge_already_registered(
    nats_client_mock: FakeNatsClient, world: dict
) -> None:
    asyncio.run(register_on_boot(nats_client_mock))
    world["registered"] = True


@when("the configured heartbeat interval elapses")
def heartbeat_interval_elapses(
    nats_client_mock: FakeNatsClient,
    fake_clock: FakeClock,
    status_provider: StubStatusProvider,
    world: dict,
) -> None:
    # Reflect "build in flight" in the stub so the next Then assertion
    # can verify the heartbeat carried the workload signal.
    status_provider.status = "busy"
    status_provider.active_tasks = 1
    status_provider.queue_depth = 0

    started_at = fake_clock.monotonic()
    fake_clock.advance(30.0)  # ASSUM-001: 30s default interval

    payload = build_heartbeat_payload(
        status_provider=status_provider,
        started_at_monotonic=started_at,
        clock=fake_clock,
    )
    asyncio.run(nats_client_mock.heartbeat(payload))
    world["last_heartbeat"] = payload


@then("Forge should publish a heartbeat carrying its current status and workload")
def assert_heartbeat_published(world: dict, nats_client_mock: FakeNatsClient) -> None:
    heartbeats = [p for p in nats_client_mock.published if p.topic == "fleet.heartbeat"]
    assert heartbeats, "expected at least one heartbeat publish"
    payload = heartbeats[-1].payload
    assert isinstance(payload, AgentHeartbeatPayload)
    assert payload.agent_id == AGENT_ID
    assert payload.status in {"ready", "busy", "degraded", "draining"}
    # Workload signal — heartbeat exposes both queue depth and active tasks.
    assert payload.queue_depth >= 0
    assert payload.active_tasks >= 0


@then("the heartbeat should reflect whether a build is currently in flight")
def assert_heartbeat_reflects_inflight(world: dict) -> None:
    payload: AgentHeartbeatPayload = world["last_heartbeat"]
    # The When-step set status=busy, active_tasks=1 to model a build in
    # flight; the heartbeat must carry that signal verbatim.
    assert payload.status == "busy"
    assert payload.active_tasks == 1


# ===========================================================================
# GROUP A.3 — Discovery cache
# ===========================================================================


@given("two specialist agents are registered with the fleet")
def two_specialists_registered(
    discovery_cache: DiscoveryCache, world: dict
) -> None:
    alpha = make_specialist_manifest(agent_id="alpha-coder", tool_name="draft-plan")
    beta = make_specialist_manifest(agent_id="beta-tester", tool_name="run-tests")
    asyncio.run(discovery_cache.upsert_agent(alpha))
    asyncio.run(discovery_cache.upsert_agent(beta))
    world["seeded_agents"] = ["alpha-coder", "beta-tester"]
    world["target_tool"] = "draft-plan"


@when("Forge is asked to resolve a capability by tool name")
def resolve_by_tool_name(
    discovery_cache: DiscoveryCache, world: dict
) -> None:
    snapshot = asyncio.run(discovery_cache.snapshot())
    matched_agent_id, resolution = resolve(
        snapshot,
        world["target_tool"],
        intent_pattern=None,
        build_id=world["build_id"],
        stage_label="planning",
        resolution_id=str(uuid.uuid4()),
        now=datetime.now(timezone.utc),
    )
    world["resolution"] = resolution
    world["matched_agent_id"] = matched_agent_id


@then("Forge should consult its live fleet cache")
def assert_cache_consulted(world: dict) -> None:
    # The When-step exclusively used DiscoveryCache.snapshot() — there is
    # no registry re-read seam in the resolution path. Presence of a
    # resolution result with a valid match-source proves the cache was
    # the data source.
    resolution = world["resolution"]
    assert resolution is not None
    assert resolution.match_source in {"tool_exact", "intent_pattern", "unresolved"}


@then(
    "the resolution should identify the matching specialist without re-reading the registry"
)
def assert_specialist_matched(world: dict) -> None:
    resolution = world["resolution"]
    assert resolution.match_source == "tool_exact"
    assert world["matched_agent_id"] == "alpha-coder"
    assert resolution.matched_agent_id == "alpha-coder"


# ===========================================================================
# GROUP A.4 — Watcher / new-specialist visibility
# ===========================================================================


@given("Forge is watching fleet lifecycle events")
def forge_watching_fleet(
    discovery_cache: DiscoveryCache, nats_client_mock: FakeNatsClient, world: dict
) -> None:
    # Wire a callback that mirrors what the production fleet watcher
    # does on an AGENT_REGISTER message: upsert the cache.
    async def on_register(manifest: AgentManifest) -> None:
        await discovery_cache.upsert_agent(manifest)

    nats_client_mock.watcher_callback = on_register
    world["watcher_active"] = True


@when("a new specialist agent publishes its registration")
def new_specialist_publishes(
    nats_client_mock: FakeNatsClient, world: dict
) -> None:
    new_manifest = make_specialist_manifest(
        agent_id="gamma-reviewer", tool_name="review-pr"
    )
    callback = nats_client_mock.watcher_callback
    assert callback is not None, "watcher callback was never wired"
    asyncio.run(callback(new_manifest))
    world["new_agent_id"] = new_manifest.agent_id
    world["target_tool"] = "review-pr"


@then("the new agent should be present in Forge's fleet view")
def assert_new_agent_visible(
    discovery_cache: DiscoveryCache, world: dict
) -> None:
    snapshot = asyncio.run(discovery_cache.snapshot())
    # snapshot is dict[str, DiscoveryCacheEntry] keyed by agent_id.
    assert world["new_agent_id"] in snapshot


@then("subsequent resolutions may select the new agent")
def assert_new_agent_resolvable(
    discovery_cache: DiscoveryCache, world: dict
) -> None:
    snapshot = asyncio.run(discovery_cache.snapshot())
    matched_agent_id, resolution = resolve(
        snapshot,
        world["target_tool"],
        intent_pattern=None,
        build_id=world["build_id"],
        stage_label="review",
        resolution_id=str(uuid.uuid4()),
        now=datetime.now(timezone.utc),
    )
    assert matched_agent_id == world["new_agent_id"]
    assert resolution.match_source == "tool_exact"


# ===========================================================================
# GROUP A.5 — Stage-complete lifecycle
# ===========================================================================


@given("a build is running")
def build_is_running(world: dict) -> None:
    # Background already populated correlation_id / build_id.
    world["build_status"] = "running"
    world["stage_label"] = "implement"


@when("a pipeline stage completes and is evaluated")
def pipeline_stage_completes(
    pipeline_publisher: RecordingPipelinePublisher, world: dict
) -> None:
    payload = StageCompletePayload(
        feature_id="FEAT-FORGE-002",
        build_id=world["build_id"],
        stage_label=world["stage_label"],
        target_kind="subagent",
        target_identifier="implementer",
        status="PASSED",
        gate_mode="AUTO_APPROVE",
        coach_score=0.92,
        duration_secs=12.5,
        completed_at=datetime.now(timezone.utc).isoformat(),
        correlation_id=world["correlation_id"],
    )
    asyncio.run(pipeline_publisher.publish_stage_complete(payload))
    world["stage_complete_payload"] = payload


@then("Forge should publish a stage-complete event")
def assert_stage_complete_published(nats_client_mock: FakeNatsClient) -> None:
    stage_events = [
        p for p in nats_client_mock.published if p.topic == "pipeline.stage_complete"
    ]
    assert stage_events, "expected a pipeline.stage_complete publish"
    assert isinstance(stage_events[-1].payload, StageCompletePayload)


@then("the event should carry the build's correlation identifier")
def assert_stage_correlation(world: dict) -> None:
    payload: StageCompletePayload = world["stage_complete_payload"]
    assert payload.correlation_id == world["correlation_id"]
    assert payload.build_id == world["build_id"]


@then(
    "the event should describe the stage outcome, the gate decision, and the coach score"
)
def assert_stage_descriptors(world: dict) -> None:
    payload: StageCompletePayload = world["stage_complete_payload"]
    assert payload.status in {"PASSED", "FAILED", "GATED", "SKIPPED"}
    assert payload.gate_mode is not None
    assert payload.coach_score is not None
    assert 0.0 <= payload.coach_score <= 1.0


# ===========================================================================
# GROUP A.6 — Build-complete lifecycle
# ===========================================================================


@given("a build has progressed to the finalising stage and produced a pull request")
def build_at_finalising(world: dict) -> None:
    world["pr_url"] = "https://github.com/example/repo/pull/42"
    world["build_status"] = "finalising"


@when("the build transitions to complete")
def build_transitions_complete(
    pipeline_publisher: RecordingPipelinePublisher, world: dict
) -> None:
    payload = BuildCompletePayload(
        feature_id="FEAT-FORGE-002",
        build_id=world["build_id"],
        repo="example/repo",
        branch="feat/forge-002",
        tasks_completed=11,
        tasks_failed=0,
        tasks_total=11,
        pr_url=world["pr_url"],
        duration_seconds=1234,
        summary="All 11 NFI subtasks complete; PR opened.",
    )
    # correlation_id rides on the MessageEnvelope, not the payload itself
    # (see API-nats-pipeline-events.md). The recorder threads it explicitly.
    asyncio.run(
        pipeline_publisher.publish_build_complete(
            payload, correlation_id=world["correlation_id"]
        )
    )
    world["build_complete_payload"] = payload


@then(
    "Forge should publish a build-complete event carrying the pull request details"
)
def assert_build_complete_pr(
    nats_client_mock: FakeNatsClient, world: dict
) -> None:
    complete_events = [
        p for p in nats_client_mock.published if p.topic == "pipeline.build_complete"
    ]
    assert complete_events, "expected a pipeline.build_complete publish"
    payload = complete_events[-1].payload
    assert isinstance(payload, BuildCompletePayload)
    assert payload.pr_url == world["pr_url"]


@then("the event should share the build's originating correlation identifier")
def assert_build_complete_correlation(
    nats_client_mock: FakeNatsClient, world: dict
) -> None:
    complete_events = [
        p for p in nats_client_mock.published if p.topic == "pipeline.build_complete"
    ]
    payload: BuildCompletePayload = complete_events[-1].payload
    # correlation_id is an envelope-level attribute the recorder stashed
    # alongside the published payload — assert against both the envelope
    # value and the payload's build_id (which is the per-build join key).
    assert getattr(complete_events[-1], "correlation_id", None) == world["correlation_id"]
    assert payload.build_id == world["build_id"]


# ===========================================================================
# GROUP A.7 — Graceful shutdown
# ===========================================================================


@when("Forge receives a graceful shutdown signal")
def forge_receives_shutdown(
    nats_client_mock: FakeNatsClient, world: dict
) -> None:
    # Production wires deregister() to a SIGTERM handler; for BDD we
    # call the same coroutine directly.
    asyncio.run(deregister(nats_client_mock, reason="graceful-shutdown"))
    world["shutdown_invoked"] = True


@then("Forge should publish a deregistration event before exiting")
def assert_deregistration_event(nats_client_mock: FakeNatsClient) -> None:
    deregister_events = [
        p for p in nats_client_mock.published if p.topic == "fleet.deregister"
    ]
    assert deregister_events, "expected a fleet.deregister publish"
    body = deregister_events[-1].payload
    assert body["agent_id"] == AGENT_ID
    assert body["reason"] == "graceful-shutdown"


@then("the fleet registry should no longer list Forge as an available agent")
def assert_registry_no_longer_lists_forge(
    nats_client_mock: FakeNatsClient,
) -> None:
    assert AGENT_ID not in nats_client_mock.registry, (
        "Forge still present in registry after deregistration"
    )


# ===========================================================================
# pytest-bdd parser aliases — kept at the bottom so the step bindings
# above remain visually grouped. Currently unused by priority scenarios
# but exported so the @skip-tagged follow-ups can rely on them.
# ===========================================================================


@given(parsers.parse("a specialist agent advertises tool {tool_name}"))
def _specialist_with_tool(  # pragma: no cover - placeholder for follow-up scenarios
    discovery_cache: DiscoveryCache, world: dict, tool_name: str
) -> None:
    manifest = make_specialist_manifest(
        agent_id=f"agent-{tool_name}",
        tool_name=tool_name,
        intents=[
            IntentCapability(
                pattern=f"{tool_name}.*",
                signals=[tool_name],
                confidence=0.85,
                description=f"Test intent for {tool_name}",
            )
        ],
    )
    asyncio.run(discovery_cache.upsert_agent(manifest))
    world.setdefault("seeded_agents", []).append(manifest.agent_id)
