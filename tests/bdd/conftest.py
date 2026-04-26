"""Pytest-bdd shared fixtures for the NATS Fleet Integration feature suite.

This conftest is the **scaffolding seam** described by TASK-NFI-011: every
Gherkin scenario in
``features/nats-fleet-integration/nats-fleet-integration.feature`` resolves
its step-injected dependencies through the fixtures defined here.

Design notes
------------

* The fixtures intentionally do **not** spin up a live NATS server. The
  R2 BDD oracle runs as part of ``/task-work`` Phase 4 in fast-feedback
  mode, so transport is replaced by an in-process recorder that captures
  the publish calls Forge would make on the wire. Behavioural contracts
  (manifest contents, heartbeat shape, lifecycle event payloads, registry
  visibility) are still verified against the real domain types
  (``AgentManifest``, ``StageCompletePayload``, ``DiscoveryCache``).
* ``fake_clock`` exposes both the :class:`Clock` protocol used by the
  fleet publisher (``monotonic`` / ``async sleep``) **and** a synchronous
  ``now()`` reader used by ``DiscoveryCache``. Tests advance time
  explicitly — wall-clock dependencies are forbidden (TASK-NFI-004 AC-002).
* ``world`` is a per-scenario mutable dict that threads cross-step state
  (``correlation_id``, ``build_id``, captured payloads). This is the
  pytest-bdd-recommended pattern for scenarios whose Then-steps must
  reference values produced by a When-step.

CI integration
--------------

The pyproject ``[tool.pytest.ini_options]`` block registers the
``smoke``/``key_example``/etc. markers that mirror the Gherkin tags.
GitHub Actions invokes the suite as:

* On every PR (gate)::

      pytest -m smoke tests/bdd/

* On merge to ``main`` (extended gate)::

      pytest -m "smoke or key_example" tests/bdd/

The 26 ``@skip``-tagged scenarios remain non-blocking until their
owning subtask wires up the missing step functions — the follow-up
ticket is recorded in the inline SKIP comments inside the feature file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability

from forge.adapters.nats.fleet_publisher import AGENT_ID, AgentStatus
from forge.discovery.cache import DiscoveryCache
from forge.discovery.protocol import Clock as DiscoveryClock


# ---------------------------------------------------------------------------
# Lightweight test doubles
# ---------------------------------------------------------------------------


@dataclass
class _RecordedPublish:
    """One captured publish call.

    Attributes:
        topic: Logical NATS subject (e.g. ``fleet.register``,
            ``fleet.heartbeat``, ``pipeline.stage_complete.<feature_id>``).
        payload: The model the production code passed to the publish
            method, kept in its original pydantic form for assertion
            convenience.
    """

    topic: str
    payload: Any


class FakeClock:
    """Deterministic clock used by both the heartbeat loop and DiscoveryCache.

    Implements the union of the two ``Clock`` protocols this codebase
    uses: ``monotonic()`` / ``async sleep()`` (fleet publisher) and
    ``now()`` returning a UTC ``datetime`` (discovery cache). Tests
    drive time forward via :meth:`advance` or :meth:`set_now`.
    """

    def __init__(self, *, start: datetime | None = None) -> None:
        self._instant: datetime = start if start is not None else datetime(
            2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc
        )
        self._monotonic_seconds: float = 0.0

    def now(self) -> datetime:
        return self._instant

    def set_now(self, instant: datetime) -> None:
        self._instant = instant

    def monotonic(self) -> float:
        return self._monotonic_seconds

    async def sleep(self, seconds: float) -> None:
        # Synchronous time advance — never yield to the real event loop's
        # wall-clock so heartbeat-loop tests stay deterministic.
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock.advance: seconds must be non-negative")
        self._monotonic_seconds += seconds
        # Mirror the same delta onto the wall-clock reading so cache
        # cached_at / heartbeat last_heartbeat_at stay coherent.
        self._instant = self._instant + _timedelta_seconds(seconds)


def _timedelta_seconds(seconds: float):
    """Tiny helper to avoid leaking ``timedelta`` import into step files."""
    from datetime import timedelta

    return timedelta(seconds=seconds)


class FakeNatsClient:
    """In-process recorder standing in for ``nats_core.client.NATSClient``.

    Records every publish-shaped call so step ``Then`` clauses can assert
    on the wire-equivalent traffic Forge would emit. Implements the subset
    of methods the fleet publisher and pipeline publisher invoke.

    The fleet *registry* is also represented here as a dict — that's the
    KV-bucket projection the registration / deregistration paths mutate.
    """

    def __init__(self) -> None:
        self.published: list[_RecordedPublish] = []
        self.registry: dict[str, AgentManifest] = {}
        # Watcher subscription callback registry. Step "Forge is watching
        # fleet lifecycle events" wires a callback here; the When-step
        # invokes it directly to simulate a registration arrival.
        self.watcher_callback: Any = None
        # When True, the next register_agent call raises RuntimeError to
        # simulate transport failure (used by integration scenarios).
        self.simulate_registry_unreachable: bool = False
        # Per-correlation deliver callbacks owned by the
        # CorrelationRegistry transport seam. Populated by ``subscribe``
        # and consulted by ``deliver_reply`` so step files can inject
        # specialist replies without importing transport types.
        # TASK-SAD-011 — extends the recorder for the dispatch suite.
        self._reply_callbacks: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Methods used by forge.adapters.nats.fleet_publisher
    # ------------------------------------------------------------------

    async def register_agent(self, manifest: AgentManifest) -> None:
        if self.simulate_registry_unreachable:
            raise RuntimeError("agent-registry KV bucket unreachable")
        self.registry[manifest.agent_id] = manifest
        self.published.append(_RecordedPublish("fleet.register", manifest))

    async def heartbeat(self, payload: AgentHeartbeatPayload) -> None:
        self.published.append(_RecordedPublish("fleet.heartbeat", payload))

    async def deregister_agent(self, agent_id: str, *, reason: str = "shutdown") -> None:
        self.registry.pop(agent_id, None)
        self.published.append(
            _RecordedPublish(
                "fleet.deregister", {"agent_id": agent_id, "reason": reason}
            )
        )

    # ------------------------------------------------------------------
    # Methods used by forge.adapters.nats.pipeline_publisher (bus.publish)
    # ------------------------------------------------------------------

    async def publish(self, subject: str, body: bytes) -> None:  # pragma: no cover - unused path
        # The real PipelinePublisher serialises envelopes via JSON. The
        # BDD suite asserts on payload models directly via the
        # ``record_pipeline_publish`` shim below, so this generic
        # publish() is only here to satisfy duck-typing.
        self.published.append(_RecordedPublish(subject, body))

    # ------------------------------------------------------------------
    # ReplyChannel surface used by forge.dispatch.correlation
    # ------------------------------------------------------------------
    # The CorrelationRegistry (TASK-SAD-003) treats its transport as a
    # ``ReplyChannel`` Protocol with ``async subscribe``/``async
    # unsubscribe``. The methods below let the BDD suite reuse this
    # recorder as the dispatch transport — TASK-SAD-011 AC requires
    # NOT introducing a parallel test transport. Subscribe/unsubscribe
    # events land in ``self.published`` so step assertions can verify
    # subscribe-before-publish ordering against a single recording list.

    async def subscribe(self, correlation_key: str, deliver: Any) -> str:
        """Register a deliver callback and record the subscribe event.

        Returns the correlation key as the opaque subscription handle —
        the registry passes it back to :meth:`unsubscribe`.
        """
        self._reply_callbacks[correlation_key] = deliver
        self.published.append(
            _RecordedPublish(
                "reply.subscribe", {"correlation_key": correlation_key}
            )
        )
        return correlation_key

    async def unsubscribe(self, subscription: Any) -> None:
        """Drop the deliver callback and record the unsubscribe event."""
        self._reply_callbacks.pop(subscription, None)
        self.published.append(
            _RecordedPublish(
                "reply.unsubscribe", {"correlation_key": subscription}
            )
        )

    def deliver_reply(
        self,
        correlation_key: str,
        source_agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Invoke the registered deliver callback for ``correlation_key``.

        Step ``Then`` clauses use this to simulate a specialist publishing
        its result on the correlation-keyed reply channel — the canonical
        round-trip half of the LES1 invariant test.
        """
        callback = self._reply_callbacks.get(correlation_key)
        if callback is not None:
            callback(correlation_key, source_agent_id, payload)


@dataclass
class StubStatusProvider:
    """Trivial ``StatusProvider`` returning whatever the test pre-set.

    Mirrors :class:`forge.adapters.nats.fleet_publisher.StatusProvider`
    without importing it (Protocol satisfied structurally).
    """

    status: AgentStatus = "ready"
    queue_depth: int = 0
    active_tasks: int = 0

    def get_current_status(self) -> AgentStatus:
        return self.status

    def get_active_tasks(self) -> int:
        return self.active_tasks

    def get_queue_depth(self) -> int:
        return self.queue_depth


class RecordingPipelinePublisher:
    """Captures pipeline lifecycle publishes by payload model.

    The production :class:`forge.adapters.nats.pipeline_publisher.PipelinePublisher`
    serialises the payload into a :class:`MessageEnvelope` before writing
    bytes to NATS. For BDD assertions we want the *payload model* itself
    (so we can inspect ``correlation_id``, ``coach_score``, etc.) — this
    shim mirrors the publisher's eight ``publish_*`` methods and stores
    the payload directly. It is functionally equivalent for the purpose
    of "did Forge emit a stage-complete event with the right shape?".
    """

    def __init__(self, recorder: FakeNatsClient) -> None:
        self._recorder = recorder

    async def publish_stage_complete(self, payload: Any) -> None:
        self._recorder.published.append(
            _RecordedPublish("pipeline.stage_complete", payload)
        )

    async def publish_build_complete(
        self, payload: Any, *, correlation_id: str | None = None
    ) -> None:
        # Production puts correlation_id on the envelope rather than the
        # payload (BuildCompletePayload v2.0 has no correlation_id field).
        # The recorder threads both so step assertions can verify the
        # envelope-level value via the .correlation_id attribute.
        self._recorder.published.append(
            _RecordedPublish("pipeline.build_complete", payload)
        )
        # Stash the correlation_id alongside the recorded entry so Then
        # steps can assert envelope-level values without parsing wire bytes.
        self._recorder.published[-1].correlation_id = correlation_id  # type: ignore[attr-defined]

    async def publish_build_started(self, payload: Any) -> None:  # pragma: no cover - covered indirectly
        self._recorder.published.append(
            _RecordedPublish("pipeline.build_started", payload)
        )


@dataclass
class StubPipelineConsumer:
    """Inert build-queue consumer used as a placeholder injection target.

    The 10 priority scenarios do not exercise the consumer path directly
    (those scenarios live under @negative / @security / @integration
    which are currently @skip-tagged). The fixture is exported so step
    files added by the follow-up ticket have a stable injection name.
    """

    acknowledged_subjects: list[str] = field(default_factory=list)

    async def acknowledge(self, subject: str) -> None:  # pragma: no cover - placeholder
        self.acknowledged_subjects.append(subject)


# ---------------------------------------------------------------------------
# Fixtures — names match the step files exactly
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    """Deterministic clock used by every time-sensitive step."""
    return FakeClock()


@pytest.fixture
def nats_client_mock() -> FakeNatsClient:
    """In-process recorder for register / heartbeat / deregister calls."""
    return FakeNatsClient()


@pytest.fixture
def discovery_cache(fake_clock: FakeClock) -> DiscoveryCache:
    """Real :class:`DiscoveryCache` instance backed by ``fake_clock``.

    Using the real cache keeps the BDD suite honest: when a scenario
    asserts "the new agent should be present in Forge's fleet view",
    the assertion is checked against production code, not a mirror.
    """

    # DiscoveryCache.Clock requires .now() returning UTC datetime — our
    # FakeClock implements that. The protocol is structurally typed.
    return DiscoveryCache(clock=_DiscoveryClockAdapter(fake_clock))


class _DiscoveryClockAdapter:
    """Adapt :class:`FakeClock` to :class:`forge.discovery.protocol.Clock`."""

    def __init__(self, fc: FakeClock) -> None:
        self._fc = fc

    def now(self) -> datetime:
        return self._fc.now()


# Make ``_DiscoveryClockAdapter`` satisfy the runtime-checkable Clock
# protocol the cache imports — silence unused-import warnings via __all__.
assert isinstance(_DiscoveryClockAdapter(FakeClock()), DiscoveryClock)


@pytest.fixture
def status_provider() -> StubStatusProvider:
    """Pre-set status provider for heartbeat assertions."""
    return StubStatusProvider()


@pytest.fixture
def pipeline_publisher(nats_client_mock: FakeNatsClient) -> RecordingPipelinePublisher:
    """Pipeline event recorder bound to the same NATS recorder."""
    return RecordingPipelinePublisher(nats_client_mock)


@pytest.fixture
def pipeline_consumer() -> StubPipelineConsumer:
    """Placeholder consumer for follow-up @negative / @integration scenarios."""
    return StubPipelineConsumer()


@pytest.fixture
def world() -> dict[str, Any]:
    """Per-scenario mutable scratch dict for cross-step state.

    Step functions read and write keys such as:

    * ``correlation_id`` / ``build_id`` — set by Given/When, asserted in Then
    * ``shutdown_event`` — :class:`asyncio.Event` raised by the When step
    * ``last_heartbeat`` — payload published by ``heartbeat_loop``
    * ``resolution`` — :class:`CapabilityResolution` produced by resolve()
    """
    return {}


# ---------------------------------------------------------------------------
# Helpers exported for step files (kept here to honour the 2-file
# constraint — step bindings consolidate into a single test module).
# ---------------------------------------------------------------------------


def make_specialist_manifest(
    *,
    agent_id: str,
    tool_name: str,
    trust_tier: str = "specialist",
    intents: Iterable[IntentCapability] = (),
) -> AgentManifest:
    """Build a minimal :class:`AgentManifest` for a fake specialist.

    Used by Given-steps that need to seed the registry with peer agents.
    Returns a manifest schema-equivalent to what a peer would publish on
    ``fleet.register`` — the cache and resolver path treat it identically.
    """

    return AgentManifest(
        agent_id=agent_id,
        name=agent_id.replace("-", " ").title(),
        version="0.1.0",
        template="bdd-test-specialist",
        trust_tier=trust_tier,  # type: ignore[arg-type]
        status="ready",
        max_concurrent=1,
        tools=[
            ToolCapability(
                name=tool_name,
                description=f"Test tool {tool_name} advertised by {agent_id}",
                parameters={"type": "object", "properties": {}},
                returns="dict",
                risk_level="read_only",
            )
        ],
        intents=list(intents),
        required_permissions=[],
    )


def run_async(coro):
    """Drive a coroutine to completion from a synchronous step function.

    pytest-bdd 8 step bodies are synchronous; we wrap async fixtures /
    domain methods with this helper rather than scattering ``asyncio.run``
    calls across step files.
    """

    return asyncio.get_event_loop().run_until_complete(coro) if (
        _has_running_loop() is False
    ) else asyncio.ensure_future(coro)


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Confidence-Gated Checkpoint Protocol fixtures (TASK-CGCP-012)
# ---------------------------------------------------------------------------
#
# These fixtures activate the R2 BDD oracle for FEAT-FORGE-004 scenarios:
#
# * ``deterministic_reasoning_model`` — a callable double for
#   :class:`forge.gating.reasoning.ReasoningModelCall` that returns
#   pre-canned JSON. Tests configure its scripted responses via
#   ``world['reasoning_script']``; the call counter on the double feeds
#   the assertion that ``evaluate_gate`` invoked the model exactly once.
# * ``temp_sqlite_path`` — pytest ``tmp_path`` mapped to a ``.sqlite``
#   file path so persistence-layer scenarios can exercise the durable
#   record path without leaking state across tests.
# * ``approval_config`` — :class:`ApprovalConfig` with the §10 defaults
#   so publisher/wait-time scenarios assert against documented values
#   rather than ad-hoc numbers.
#
# All three fixtures are scenario-scoped; nothing crosses scenario
# boundaries except the per-scenario ``world`` dict.


class DeterministicReasoningModel:
    """Scripted ``ReasoningModelCall`` double for gating-protocol BDD steps.

    Implements ``__call__(prompt: str) -> str`` returning the JSON
    fixture currently at the head of ``self.script``. Tests push
    response bodies onto the script with :meth:`queue_response`. The
    instance counts invocations so step assertions can verify
    ``evaluate_gate`` invoked the model exactly once per scenario.

    The double does **not** parse the prompt — assertions about prompt
    contents belong in TASK-CGCP-005 unit tests, not the R2 oracle.
    """

    def __init__(self) -> None:
        self.script: list[str] = []
        self.calls: list[str] = []

    def queue_response(self, parsed_decision_json: str) -> None:
        """Push a ``ParsedDecision``-shaped JSON body onto the FIFO."""
        self.script.append(parsed_decision_json)

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.script:
            raise AssertionError(
                "DeterministicReasoningModel: no scripted response queued; "
                "call queue_response() in the Given step."
            )
        return self.script.pop(0)


@pytest.fixture
def deterministic_reasoning_model() -> DeterministicReasoningModel:
    """Per-scenario reasoning-model double for FEAT-FORGE-004 scenarios."""
    return DeterministicReasoningModel()


@pytest.fixture
def temp_sqlite_path(tmp_path):
    """Per-scenario SQLite file path; cleaned up by ``tmp_path`` teardown."""
    return tmp_path / "forge-cgcp.sqlite"


@pytest.fixture
def approval_config():
    """:class:`ApprovalConfig` populated with §10 / ASSUM defaults.

    Lazy import keeps the fixture available even when the config module
    is mid-refactor (the failure surfaces as a clear ImportError at
    fixture-resolution time rather than at module load).
    """
    from forge.config.models import ApprovalConfig

    return ApprovalConfig()


# Re-export the constant so step files can assert on AGENT_ID without
# re-importing from the production module's deeper path.
# ---------------------------------------------------------------------------
# Infrastructure Coordination fixtures (TASK-IC-011)
# ---------------------------------------------------------------------------
#
# These fixtures activate the R2 BDD oracle for FEAT-FORGE-006 (the
# 43 scenarios in
# ``features/infrastructure-coordination/infrastructure-coordination.feature``).
# Cardinal rule: scenarios bind to the **real** production modules
# (``forge.memory.*``, ``forge.build.*``); the fixtures below provide
# the seams the steps need (Graphiti recorder, tmp worktree, tmp SQLite,
# env-cleared subprocess) without ever touching the network or the
# filesystem outside the per-scenario ``tmp_path``.


class FakeGraphitiClient:
    """In-process recorder standing in for the Graphiti write/read path.

    The IC scenarios assert against three observable Graphiti effects:

    1. ``writes`` — every entity passed to ``write_entity``-like seams.
       Each entry is a ``(group_id, entity_type, entity_dict)`` tuple so
       step functions can grep by group / type / id.
    2. ``query_results`` — pre-canned dict-shaped rows the priors-retrieval
       seam returns when a scenario primes the query side.
    3. ``unreachable`` — when ``True``, every write seam raises
       :class:`forge.memory.writer.GraphitiUnavailableError` so the
       ``@negative memory-write-failure-tolerated`` scenario can prove
       the build proceeds despite the failure.

    The recorder also tracks ``write_order`` — a list of timestamps
    captured as each write enters the seam — so the
    ``@edge-case write-ordering`` scenario can prove the SQLite commit
    happened *before* the Graphiti dispatch.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.query_results: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.existing_session_outcomes: set[str] = set()
        self.unreachable: bool = False
        self.cli_unreachable: bool = False
        self.write_order: list[float] = []
        # Per-entity_id index so split-brain dedupe / session-outcome
        # idempotency scenarios can assert "exactly one stored entity".
        self.entities_by_id: dict[str, dict[str, Any]] = {}

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        group_id: str,
        source: str = "json",
    ) -> None:
        """``graphiti_core.Graphiti().add_episode`` shape.

        Patched into :func:`forge.memory.writer._dispatch_write` via
        ``monkeypatch`` in step setup so production code reaches this
        recorder rather than a real backend.
        """
        if self.unreachable:
            raise RuntimeError("graphiti unreachable (test-controlled)")
        import json as _json
        import time as _time

        payload = _json.loads(episode_body)
        entity_id = payload.get("entity_id", "<unknown>")
        entity_type = name.split(":", 1)[0]
        # Dedupe on entity_id so split-brain / session-outcome idempotency
        # scenarios see exactly one record per id.
        if entity_id not in self.entities_by_id:
            self.writes.append((group_id, entity_type, payload))
            self.entities_by_id[entity_id] = payload
            self.write_order.append(_time.monotonic())

    def queue_query(
        self, group_id: str, entity_type: str, rows: list[dict[str, Any]]
    ) -> None:
        """Pre-canned rows for a priors-retrieval query category."""
        self.query_results[(group_id, entity_type)] = list(rows)

    async def query(
        self, *, group_id: str, entity_type: str, **_: Any
    ) -> list[dict[str, Any]]:
        return list(self.query_results.get((group_id, entity_type), []))


@pytest.fixture
def graphiti_client_mock() -> FakeGraphitiClient:
    """In-process recorder for the Graphiti add-context / query path."""
    return FakeGraphitiClient()


@pytest.fixture
def patched_graphiti_writer(monkeypatch, graphiti_client_mock):
    """Patch :mod:`forge.memory.writer` to route writes through the recorder.

    Returns the recorder so step functions have a single named handle
    for both pre-flight setup (``queue_query`` / ``unreachable=True``)
    and post-flight assertion (``writes``).
    """
    from forge.memory import writer as _writer_mod

    async def _fake_dispatch(payload, group_id, episode_name):
        if graphiti_client_mock.unreachable:
            raise _writer_mod.GraphitiUnavailableError(
                "test-controlled unreachable"
            )
        await graphiti_client_mock.add_episode(
            name=episode_name,
            episode_body=__import__("json").dumps(payload),
            group_id=group_id,
            source="json",
        )

    monkeypatch.setattr(_writer_mod, "_dispatch_write", _fake_dispatch)
    return graphiti_client_mock


@pytest.fixture
def tmp_worktree(tmp_path):
    """Per-scenario absolute worktree path under ``tmp_path``.

    Mirrors the per-build ephemeral worktree contract from TASK-IC-010 —
    the directory exists, is writable, and is absolute (so the
    ``security-working-directory-allowlist`` invariant has something
    concrete to validate against).
    """
    worktree = tmp_path / "build-worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


@pytest.fixture
def tmp_sqlite_db(tmp_path):
    """Per-scenario SQLite ledger path used by reconcile / session-outcome.

    Returned as a :class:`Path` rather than a connection — the IC code
    paths use a Protocol-typed repository, so individual steps build a
    minimal in-memory repository implementation rather than coupling to
    a SQLite schema. The path is provided so scenarios that need a
    backing file (e.g. snapshot store, reconcile) have a stable location.
    """
    return tmp_path / "forge-history.sqlite"


@pytest.fixture
def env_cleared_subprocess(monkeypatch):
    """Strip credential env vars so ``create_pull_request`` returns ``None``.

    Used by the ``@negative cred-missing-pr-graceful`` scenario. The
    pop is reversed automatically by ``monkeypatch`` teardown.
    """
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def execute_seam_recorder(monkeypatch):
    """Stub the DeepAgents ``execute`` seam in build modules.

    Records every command the test code dispatches and returns scripted
    ``(stdout, stderr, exit_code, duration, timed_out)`` tuples. The
    git/gh seam in :mod:`forge.build.git_operations` and the pytest
    seam in :mod:`forge.build.test_verification` are patched together
    so a single scenario can drive both.
    """

    from forge.build import git_operations as _git
    from forge.build import test_verification as _tv

    @dataclass
    class _Recorder:
        commands: list[tuple[list[str], str]] = field(default_factory=list)
        next_results: list[tuple[str, str, int, float, bool]] = field(
            default_factory=list
        )
        # When True, raise DisallowedBinaryError-equivalent semantics
        # are tested via the production module's own validation, so the
        # seam itself just records and returns success.
        default_result: tuple[str, str, int, float, bool] = (
            "",
            "",
            0,
            0.01,
            False,
        )

        def queue(self, result: tuple[str, str, int, float, bool]) -> None:
            self.next_results.append(result)

        async def __call__(
            self, *, command: list[str], cwd: str, timeout: int
        ) -> tuple[str, str, int, float, bool]:
            self.commands.append((list(command), cwd))
            if self.next_results:
                return self.next_results.pop(0)
            return self.default_result

    recorder = _Recorder()
    monkeypatch.setattr(_git, "_execute_via_deepagents", recorder)
    monkeypatch.setattr(_tv, "_execute_via_deepagents", recorder)
    return recorder


__all__ = [
    "AGENT_ID",
    "DeterministicReasoningModel",
    "FakeClock",
    "FakeGraphitiClient",
    "FakeNatsClient",
    "RecordingPipelinePublisher",
    "StubPipelineConsumer",
    "StubStatusProvider",
    "make_specialist_manifest",
    "run_async",
]
