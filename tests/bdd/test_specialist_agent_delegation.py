"""Pytest-bdd wiring for FEAT-FORGE-003 Specialist Agent Delegation scenarios.

This module is the executable surface for TASK-SAD-011: it binds the 5
``@key-example`` Gherkin scenarios (2 of which are also tagged
``@smoke``) from
``features/specialist-agent-delegation/specialist-agent-delegation.feature``
to pytest-bdd step functions that exercise the real Forge dispatch
domain (``DispatchOrchestrator``, ``CorrelationRegistry``,
``RetryCoordinator``, ``parse_reply``, ``correlate_outcome``) through
the in-process recorder defined in ``conftest.py:FakeNatsClient``.

The remaining 28 scenarios across groups B/C/D/E are deferred to
follow-up testing tasks per the TASK-SAD-011 task description (one
per group). They are NOT collected here.

Step organisation
-----------------

The project's documentation level is ``minimal`` (max 2 created files),
so all step bindings are consolidated into this single module. Sections
are arranged in the same scenario order as the feature file — exact
tool dispatch → intent fallback → coach output parsing → retry → outcome
correlation — so a reader can navigate by Gherkin scenario at a glance.

Subscribe-before-publish invariant (LES1)
-----------------------------------------

Scenario A.exact-tool-dispatch asserts the canonical LES1 invariant:
``CorrelationRegistry.bind`` MUST establish the reply subscription
before ``DispatchCommandPublisher.publish_dispatch`` writes the command.
The assertion runs against the recording-order property of
``FakeNatsClient.published`` — both ``reply.subscribe`` and
``dispatch.publish`` are appended to that single list, so a simple
index comparison proves the ordering deterministically.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability
from pytest_bdd import given, scenario, then, when

from forge.discovery.cache import DiscoveryCache
from forge.discovery.resolve import resolve
from forge.dispatch.correlation import CorrelationRegistry
from forge.dispatch.models import (
    DispatchAttempt,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)
from forge.dispatch.orchestrator import DispatchOrchestrator
from forge.dispatch.outcome import correlate_outcome
from forge.dispatch.persistence import (
    DispatchParameter,
    SqliteHistoryWriter,
)
from forge.dispatch.reply_parser import parse_reply
from forge.dispatch.retry import RetryCoordinator
from forge.dispatch.timeout import TimeoutCoordinator

from tests.bdd.conftest import (
    FakeClock,
    FakeNatsClient,
    make_specialist_manifest,
)


FEATURE_FILE = "specialist-agent-delegation/specialist-agent-delegation.feature"


# ---------------------------------------------------------------------------
# Scenario registrations — each @scenario decorator materialises a pytest
# test whose body is the steps below. ``@pytest.mark.smoke`` /
# ``@pytest.mark.key_example`` decorators mirror the Gherkin tags so CI
# can filter via ``pytest -m smoke``.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge delegates a stage to a specialist advertising the exact tool",
)
def test_exact_tool_dispatch() -> None:
    """@smoke @key-example — TASK-SAD-006/SAD-003 happy-path round-trip."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge falls back to intent-pattern matching when no tool match exists",
)
def test_intent_pattern_fallback() -> None:
    """@key-example — TASK-SAD-008 intent-fallback resolution."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge reads Coach output preferring top-level fields over nested "
    "result fields",
)
def test_coach_output_top_vs_nested() -> None:
    """@smoke @key-example — TASK-SAD-005 reply-parser top-level preference."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge retries a failed dispatch with additional context on the "
    "second attempt",
)
def test_retry_with_additional_context() -> None:
    """@key-example — TASK-SAD-007 retry coordinator."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge links each capability resolution to its downstream outcome",
)
def test_outcome_correlation() -> None:
    """@key-example — TASK-SAD-009 outcome correlation."""


# ===========================================================================
# Test-only adapters
# ===========================================================================


class _FakeDispatchPublisher:
    """In-process :class:`DispatchCommandPublisher` recorder.

    Mirrors what the production NATS adapter (TASK-SAD-010) will record
    on the wire: each ``publish_dispatch`` call lands in
    ``recorder.published`` under the topic ``"dispatch.publish"`` so
    step assertions can compare the recording-order index against
    ``"reply.subscribe"`` for the LES1 subscribe-before-publish
    invariant.

    After recording, the publisher fires any ``scheduled_reply`` payload
    the test pre-set on the ``world`` dict via
    ``recorder.deliver_reply`` — this stands in for the specialist
    publishing its result on the correlation-keyed reply channel.
    Firing on publish (rather than via an arbitrary ``asyncio.sleep``)
    is the deterministic seam the TASK-SAD-011 AC asks for.
    """

    def __init__(self, recorder: FakeNatsClient, world: dict[str, Any]) -> None:
        self._recorder = recorder
        self._world = world

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
    ) -> None:
        # Record the publish event. We store a small dict rather than a
        # raw envelope because the suite asserts on the wire-equivalent
        # correlation_key and matched_agent_id — both already present on
        # the DispatchAttempt model.
        from tests.bdd.conftest import _RecordedPublish

        self._recorder.published.append(
            _RecordedPublish(
                "dispatch.publish",
                {
                    "attempt": attempt,
                    "parameters": list(parameters),
                },
            )
        )

        scheduled = self._world.get("scheduled_reply")
        if scheduled is None:
            return
        # Deliver synchronously via the recorder — CorrelationRegistry's
        # deliver_reply is itself sync, so this round-trip completes
        # inside the same event-loop tick the publish observed.
        self._recorder.deliver_reply(
            attempt.correlation_key,
            scheduled.get("source_agent_id", attempt.matched_agent_id),
            scheduled["payload"],
        )


class _RegistryWaitAdapter:
    """Adapter satisfying :class:`TimeoutCoordinator._RegistryLike`.

    The structural protocol declares ``wait_for_reply(binding)`` but
    :meth:`CorrelationRegistry.wait_for_reply` takes
    ``(binding, timeout_seconds)``. The :class:`TimeoutCoordinator` is
    the single source of truth for the hard cut-off (it wraps the call
    in :func:`asyncio.timeout`), so we hand the registry a very-large
    inner timeout — the coordinator's outer ``asyncio.timeout`` is
    what actually fires. This shim mirrors the one used in the
    orchestrator unit tests (TASK-SAD-006) so the BDD wiring composes
    the production classes the same way.
    """

    def __init__(self, registry: CorrelationRegistry) -> None:
        self._registry = registry
        # Sentinel large timeout — the coordinator's asyncio.timeout
        # owns the real cut-off, so this value should never fire.
        self._inner_timeout: float = 1e9

    async def wait_for_reply(self, binding: Any) -> Any:
        return await self._registry.wait_for_reply(
            binding, self._inner_timeout
        )

    def release(self, binding: Any) -> None:
        self._registry.release(binding)


def _build_orchestrator(
    *,
    discovery_cache: DiscoveryCache,
    nats_client_mock: FakeNatsClient,
    fake_clock: FakeClock,
    db_writer: SqliteHistoryWriter,
    world: dict[str, Any],
    timeout_seconds: float = 5.0,
) -> DispatchOrchestrator:
    """Compose a real :class:`DispatchOrchestrator` against the recorder.

    All collaborators are real production classes — only the transport
    publisher and the reply-channel surface are recorder-backed. This
    mirrors the AC requirement that step definitions exercise the
    ``DispatchOrchestrator`` end-to-end via the canonical
    ``FakeNatsClient`` rather than any parallel test transport.
    """
    registry = CorrelationRegistry(transport=nats_client_mock)
    timeout = TimeoutCoordinator(
        registry=_RegistryWaitAdapter(registry),
        clock=_DispatchClockAdapter(fake_clock),
        default_timeout_seconds=timeout_seconds,
    )
    publisher = _FakeDispatchPublisher(nats_client_mock, world)
    return DispatchOrchestrator(
        cache=discovery_cache,
        registry=registry,
        timeout=timeout,
        publisher=publisher,
        db_writer=db_writer,
    )


class _DispatchClockAdapter:
    """Adapt :class:`FakeClock` to :class:`forge.discovery.protocol.Clock`.

    The dispatch :class:`TimeoutCoordinator` only reads ``now()`` for the
    audit-log timestamp; it uses ``asyncio.timeout`` for the actual
    deadline. ``FakeClock.now()`` already returns a UTC ``datetime``,
    so this adapter is a thin pass-through kept here so the test file
    does not export a private clock symbol from conftest.
    """

    def __init__(self, fc: FakeClock) -> None:
        self._fc = fc

    def now(self):  # type: ignore[no-untyped-def]
        return self._fc.now()


# ===========================================================================
# Per-test fixtures specific to this module
# ===========================================================================


@pytest.fixture
def db_writer() -> SqliteHistoryWriter:
    """In-memory SQLite writer for resolution + parameter persistence."""
    writer = SqliteHistoryWriter.in_memory()
    try:
        yield writer
    finally:
        writer.close()


@pytest.fixture
def orchestrator(
    discovery_cache: DiscoveryCache,
    nats_client_mock: FakeNatsClient,
    fake_clock: FakeClock,
    db_writer: SqliteHistoryWriter,
    world: dict[str, Any],
) -> DispatchOrchestrator:
    """Wire a fresh :class:`DispatchOrchestrator` per scenario."""
    return _build_orchestrator(
        discovery_cache=discovery_cache,
        nats_client_mock=nats_client_mock,
        fake_clock=fake_clock,
        db_writer=db_writer,
        world=world,
    )


# ===========================================================================
# Background steps — shared by every priority scenario
# ===========================================================================


@given("Forge is registered with the fleet and is watching fleet lifecycle events")
def forge_registered_and_watching(
    nats_client_mock: FakeNatsClient,
    discovery_cache: DiscoveryCache,
    world: dict[str, Any],
) -> None:
    # Wire a watcher callback that mirrors what the production fleet
    # watcher does on AGENT_REGISTER: upsert the cache. Specialist seed
    # steps below invoke this callback rather than poking the cache
    # directly so the "watching lifecycle events" precondition is
    # exercised end-to-end.
    async def on_register(manifest: AgentManifest) -> None:
        await discovery_cache.upsert_agent(manifest)

    nats_client_mock.watcher_callback = on_register
    world["registered"] = True
    world["watcher_active"] = True


@given("the live capability cache is fresh")
def capability_cache_is_fresh(discovery_cache: DiscoveryCache) -> None:
    # The DiscoveryCache fixture is constructed per-scenario, so it is
    # by definition fresh. The assertion below documents the invariant
    # rather than performing a setup step — a stale cache would mean a
    # leak across the per-test fixture boundary.
    assert len(discovery_cache) == 0


# ===========================================================================
# Scenario A: exact-tool-dispatch (smoke + key-example)
# ===========================================================================


@given(
    "a specialist agent advertises a tool matching the stage's "
    "requested capability"
)
def specialist_advertises_exact_tool(
    nats_client_mock: FakeNatsClient,
    world: dict[str, Any],
) -> None:
    tool_name = "review-pr"
    manifest = make_specialist_manifest(
        agent_id="exact-match-specialist",
        tool_name=tool_name,
    )
    callback = nats_client_mock.watcher_callback
    assert callback is not None, "background must wire watcher callback"
    asyncio.run(callback(manifest))
    world["target_tool"] = tool_name
    world["expected_agent_id"] = manifest.agent_id


@when("Forge dispatches the stage to that capability")
def forge_dispatches_stage(
    orchestrator: DispatchOrchestrator,
    world: dict[str, Any],
) -> None:
    # Pre-arm the recorder so the specialist's reply is delivered the
    # moment the publisher records the publish event. This makes the
    # round-trip deterministic without any time.sleep / asyncio.sleep.
    world["scheduled_reply"] = {
        "source_agent_id": world["expected_agent_id"],
        "payload": {
            "agent_id": world["expected_agent_id"],
            "coach_score": 0.91,
            "criterion_breakdown": {"completeness": 0.95, "correctness": 0.88},
            "detection_findings": [
                {"detector": "lint", "severity": "info", "count": 0},
            ],
        },
    }
    outcome: DispatchOutcome = asyncio.run(
        orchestrator.dispatch(
            capability=world["target_tool"],
            parameters=[
                DispatchParameter(name="pr_url", value="https://example.test/1"),
            ],
            build_id="build-sad011",
            stage_label="review",
        )
    )
    world["outcome"] = outcome


@then(
    "Forge should subscribe to a correlation-keyed reply channel before "
    "publishing the command"
)
def assert_subscribe_before_publish(nats_client_mock: FakeNatsClient) -> None:
    topics = [event.topic for event in nats_client_mock.published]
    assert "reply.subscribe" in topics, "expected at least one reply.subscribe"
    assert "dispatch.publish" in topics, "expected at least one dispatch.publish"
    # Recording-order property — the canonical LES1 assertion.
    first_subscribe = topics.index("reply.subscribe")
    first_publish = topics.index("dispatch.publish")
    assert first_subscribe < first_publish, (
        "subscribe-before-publish invariant violated: "
        f"reply.subscribe at index {first_subscribe} but "
        f"dispatch.publish at index {first_publish}"
    )


@then("the specialist should publish its result on that correlation-keyed channel")
def assert_specialist_replied_on_correlation(
    nats_client_mock: FakeNatsClient,
    world: dict[str, Any],
) -> None:
    publishes = [
        event for event in nats_client_mock.published
        if event.topic == "dispatch.publish"
    ]
    assert publishes, "expected at least one dispatch.publish entry"
    attempt: DispatchAttempt = publishes[-1].payload["attempt"]
    # The reply was delivered to the same correlation_key the dispatch
    # carried — proven by the SyncResult outcome bound to that
    # correlation's resolution_id.
    outcome = world["outcome"]
    assert isinstance(outcome, SyncResult), f"expected SyncResult, got {type(outcome).__name__}"
    assert outcome.resolution_id == attempt.resolution_id


@then(
    "Forge should feed the coach score, criterion breakdown, and detection "
    "findings into the gating layer"
)
def assert_gating_inputs_present(world: dict[str, Any]) -> None:
    outcome = world["outcome"]
    assert isinstance(outcome, SyncResult)
    assert outcome.coach_score is not None
    assert 0.0 <= outcome.coach_score <= 1.0
    assert outcome.criterion_breakdown, "criterion_breakdown must be populated"
    assert outcome.detection_findings, "detection_findings must be populated"


# ===========================================================================
# Scenario B: intent-pattern-fallback (key-example)
# ===========================================================================


@given("no specialist advertises the requested tool by exact name")
def no_specialist_advertises_tool(world: dict[str, Any]) -> None:
    # Anchor the requested tool. The cache is empty until the intent-
    # advertising specialist is upserted in the next Given step.
    world["target_tool"] = "synthesize-report"


@given(
    "a specialist advertises an intent pattern that covers the request at "
    "sufficient confidence"
)
def specialist_advertises_intent(
    nats_client_mock: FakeNatsClient,
    world: dict[str, Any],
) -> None:
    intent_pattern = "synthesize.*"
    manifest = AgentManifest(
        agent_id="intent-fallback-specialist",
        name="Intent Fallback Specialist",
        version="0.1.0",
        template="bdd-test-specialist",
        trust_tier="specialist",
        status="ready",
        max_concurrent=1,
        # Tools advertised must NOT include ``synthesize-report`` — the
        # whole point of this scenario is to exercise the intent-fallback
        # branch when no exact tool match exists.
        tools=[
            ToolCapability(
                name="unrelated-tool",
                description="Unrelated tool to populate the manifest",
                parameters={"type": "object", "properties": {}},
                returns="dict",
                risk_level="read_only",
            )
        ],
        intents=[
            IntentCapability(
                pattern=intent_pattern,
                confidence=0.85,  # comfortably above the 0.7 floor
                description="Synthesises domain reports",
            )
        ],
        required_permissions=[],
    )
    callback = nats_client_mock.watcher_callback
    assert callback is not None
    asyncio.run(callback(manifest))
    world["intent_pattern"] = intent_pattern
    world["expected_agent_id"] = manifest.agent_id


@when("Forge resolves the capability")
def forge_resolves_capability(
    discovery_cache: DiscoveryCache,
    world: dict[str, Any],
) -> None:
    snapshot = asyncio.run(discovery_cache.snapshot())
    matched_agent_id, resolution = resolve(
        snapshot=snapshot,
        tool_name=world["target_tool"],
        intent_pattern=world.get("intent_pattern"),
        min_confidence=0.7,
        build_id="build-sad011",
        stage_label="resolve",
    )
    world["matched_agent_id"] = matched_agent_id
    world["resolution"] = resolution


@then("Forge should select the intent-matching specialist")
def assert_intent_specialist_selected(world: dict[str, Any]) -> None:
    assert world["matched_agent_id"] == world["expected_agent_id"]


@then(
    "the resulting resolution record should mark the match source as an "
    "intent-pattern match"
)
def assert_match_source_intent(world: dict[str, Any]) -> None:
    assert world["resolution"].match_source == "intent_pattern"


# ===========================================================================
# Scenario C: coach-output-parsing (smoke + key-example)
# ===========================================================================


@given(
    "a specialist returns a result carrying both top-level Coach fields and "
    "nested Coach fields"
)
def specialist_returns_dual_coach_fields(world: dict[str, Any]) -> None:
    # Distinct top-level vs nested values let assertions prove the
    # top-level branch was taken — same value either side would not
    # discriminate.
    world["reply_payload"] = {
        "agent_id": "dual-fields-specialist",
        "coach_score": 0.93,
        "criterion_breakdown": {"completeness": 0.95},
        "detection_findings": [
            {"detector": "lint", "severity": "info", "count": 0},
        ],
        "result": {
            # Nested values intentionally differ so a fallback would be
            # observable. The parser MUST prefer the top-level fields.
            "coach_score": 0.10,
            "criterion_breakdown": {"completeness": 0.10},
            "detection_findings": [
                {"detector": "stub", "severity": "error", "count": 99},
            ],
        },
    }


@when("Forge parses the reply")
def forge_parses_reply(world: dict[str, Any]) -> None:
    outcome = parse_reply(
        world["reply_payload"],
        resolution_id="res-coach-output",
        attempt_no=1,
    )
    world["outcome"] = outcome


@then(
    "Forge should use the top-level coach score, criterion breakdown, and "
    "detection findings"
)
def assert_top_level_fields_used(world: dict[str, Any]) -> None:
    outcome = world["outcome"]
    assert isinstance(outcome, SyncResult)
    payload = world["reply_payload"]
    assert outcome.coach_score == payload["coach_score"]
    assert outcome.criterion_breakdown == payload["criterion_breakdown"]
    assert outcome.detection_findings == payload["detection_findings"]


@then("the nested fields should be retained only as fallback evidence")
def assert_nested_fields_are_fallback(world: dict[str, Any]) -> None:
    outcome = world["outcome"]
    assert isinstance(outcome, SyncResult)
    nested = world["reply_payload"]["result"]
    # The parser must NOT surface nested values when top-level values
    # were present — that's the whole top-level-preference contract.
    assert outcome.coach_score != nested["coach_score"]
    assert outcome.criterion_breakdown != nested["criterion_breakdown"]
    assert outcome.detection_findings != nested["detection_findings"]


# ===========================================================================
# Scenario D: retry-with-additional-context (key-example)
# ===========================================================================


@given("a first dispatch to a specialist returns an error result")
def first_dispatch_returns_error(
    nats_client_mock: FakeNatsClient,
    orchestrator: DispatchOrchestrator,
    world: dict[str, Any],
) -> None:
    tool_name = "build-feature"
    manifest = make_specialist_manifest(
        agent_id="retry-specialist",
        tool_name=tool_name,
    )
    callback = nats_client_mock.watcher_callback
    assert callback is not None
    asyncio.run(callback(manifest))
    world["target_tool"] = tool_name
    world["expected_agent_id"] = manifest.agent_id

    # Pre-arm the publisher to deliver an error reply, so the first
    # dispatch terminates as DispatchError without any timer.
    world["scheduled_reply"] = {
        "source_agent_id": manifest.agent_id,
        "payload": {
            "agent_id": manifest.agent_id,
            "error": "missing-context: caller did not supply build manifest",
        },
    }
    original_parameters = [
        DispatchParameter(name="feature_id", value="FEAT-FORGE-003"),
    ]
    first_outcome = asyncio.run(
        orchestrator.dispatch(
            capability=tool_name,
            parameters=original_parameters,
            build_id="build-sad011",
            stage_label="implement",
        )
    )
    assert isinstance(first_outcome, DispatchError), (
        f"expected DispatchError, got {type(first_outcome).__name__}"
    )
    world["first_outcome"] = first_outcome
    world["original_parameters"] = original_parameters
    # Capture the first attempt's correlation key (recorded on the
    # publish event) so the Then-step can prove the retry used a
    # fresh one rather than reusing it.
    publishes = [
        event for event in nats_client_mock.published
        if event.topic == "dispatch.publish"
    ]
    world["first_correlation_key"] = (
        publishes[-1].payload["attempt"].correlation_key
    )


@when("Forge decides to retry the stage with additional context")
def forge_retries_with_context(
    orchestrator: DispatchOrchestrator,
    world: dict[str, Any],
) -> None:
    coordinator = RetryCoordinator(orchestrator)
    additional_context = [
        DispatchParameter(
            name="build_manifest",
            value="manifest-blob-attached-by-reasoning-loop",
        ),
    ]
    # On the retry, schedule a successful reply so the orchestrator
    # path completes (we still assert the retry was issued; success
    # vs error of the *retry* itself is not what this scenario tests).
    world["scheduled_reply"] = {
        "source_agent_id": world["expected_agent_id"],
        "payload": {
            "agent_id": world["expected_agent_id"],
            "coach_score": 0.7,
            "criterion_breakdown": {"completeness": 0.7},
            "detection_findings": [],
        },
    }
    retry_outcome = asyncio.run(
        coordinator.retry_with_context(
            previous_outcome=world["first_outcome"],
            capability=world["target_tool"],
            original_parameters=world["original_parameters"],
            additional_context=additional_context,
        )
    )
    world["retry_outcome"] = retry_outcome
    world["additional_context"] = additional_context


@then("Forge should issue a new dispatch with a fresh correlation")
def assert_fresh_correlation(
    nats_client_mock: FakeNatsClient,
    world: dict[str, Any],
) -> None:
    publishes = [
        event for event in nats_client_mock.published
        if event.topic == "dispatch.publish"
    ]
    # Two dispatch publish events — one per attempt.
    assert len(publishes) >= 2, (
        f"expected at least two dispatch.publish events, got {len(publishes)}"
    )
    second_correlation_key = publishes[-1].payload["attempt"].correlation_key
    assert second_correlation_key != world["first_correlation_key"], (
        "retry MUST use a fresh correlation key, not reuse the prior one"
    )


@then("the additional context should be carried in the retry command")
def assert_additional_context_carried(
    nats_client_mock: FakeNatsClient,
    world: dict[str, Any],
) -> None:
    publishes = [
        event for event in nats_client_mock.published
        if event.topic == "dispatch.publish"
    ]
    retry_parameters: list[DispatchParameter] = publishes[-1].payload["parameters"]
    extra_names = {p.name for p in world["additional_context"]}
    carried_names = {p.name for p in retry_parameters}
    assert extra_names.issubset(carried_names), (
        f"retry parameters {carried_names} missing extras {extra_names}"
    )


@then(
    "the retry attempt should be recorded alongside the original attempt"
)
def assert_retry_recorded_alongside(
    db_writer: SqliteHistoryWriter,
    world: dict[str, Any],
) -> None:
    rows = db_writer.read_resolutions()
    # Two resolution rows persisted — original + retry sibling.
    assert len(rows) == 2, f"expected 2 persisted resolutions, got {len(rows)}"
    retry_rows = [r for r in rows if r.retry_of is not None]
    assert len(retry_rows) == 1, (
        f"expected exactly one row with retry_of set, got {len(retry_rows)}"
    )
    assert retry_rows[0].retry_of == world["first_outcome"].resolution_id


# ===========================================================================
# Scenario E: outcome-correlation (key-example)
# ===========================================================================


@given("Forge has dispatched to a resolved specialist")
def forge_has_dispatched_resolved(
    nats_client_mock: FakeNatsClient,
    orchestrator: DispatchOrchestrator,
    world: dict[str, Any],
) -> None:
    tool_name = "evaluate-stage"
    manifest = make_specialist_manifest(
        agent_id="outcome-specialist",
        tool_name=tool_name,
    )
    callback = nats_client_mock.watcher_callback
    assert callback is not None
    asyncio.run(callback(manifest))

    world["scheduled_reply"] = {
        "source_agent_id": manifest.agent_id,
        "payload": {
            "agent_id": manifest.agent_id,
            "coach_score": 0.88,
            "criterion_breakdown": {"completeness": 0.9},
            "detection_findings": [],
        },
    }
    outcome = asyncio.run(
        orchestrator.dispatch(
            capability=tool_name,
            parameters=[
                DispatchParameter(name="stage_label", value="evaluate"),
            ],
            build_id="build-sad011",
            stage_label="evaluate",
        )
    )
    assert isinstance(outcome, SyncResult)
    world["outcome"] = outcome


@when("the specialist's reply is received and the gate decision is produced")
def gate_decision_produced(
    db_writer: SqliteHistoryWriter,
    world: dict[str, Any],
) -> None:
    gate_decision_id = "gate-sad011-001"
    correlated = correlate_outcome(
        world["outcome"].resolution_id,
        gate_decision_id,
        db_writer=db_writer,
    )
    world["gate_decision_id"] = gate_decision_id
    world["correlated_resolution"] = correlated


@then(
    "the resolution record should be linked to that gate decision as its "
    "outcome"
)
def assert_resolution_linked_to_gate(world: dict[str, Any]) -> None:
    correlated = world["correlated_resolution"]
    assert correlated.gate_decision_id == world["gate_decision_id"]


@then("the resolution record should be marked as having its outcome correlated")
def assert_outcome_correlated_flag(world: dict[str, Any]) -> None:
    assert world["correlated_resolution"].outcome_correlated is True


__all__: list[str] = []
