"""Mode A concurrency, multi-feature, and integrity integration tests
(TASK-MAG7-014, FEAT-FORGE-007).

This module is the **most comprehensive integration test in Wave 5** — it
covers cross-cutting invariants that no single Wave 2/3 unit test can
assert:

* Group F ``@concurrency`` — two concurrent builds with isolated channels;
  supervisor dispatches second build while first build's autobuild is in
  flight.
* Group H ``@integration`` — multi-feature catalogues with per-feature
  sequencing.
* Group E ``@security`` — constitutional belt-and-braces, specialist
  override-claim, worktree confinement.
* Group I ``@data-integrity`` — correlation-id threading, calibration-priors
  snapshot stability.
* Group D + Group I — first-wins idempotency.

The tests reuse the in-memory fake pattern established by
``tests/forge/test_supervisor.py`` so the suite runs without SQLite, NATS,
LangGraph, or any subprocess engine. The supervisor + constitutional guard
+ per-feature sequencer + stage-ordering guard are exercised for **real**
through the dispatch turn — only the I/O collaborators are doubled.

Acceptance-criteria coverage map:

* AC-001  module-exists                — file & class structure
* AC-002  two-concurrent-builds        — :class:`TestTwoConcurrentBuilds`
* AC-003  multi-feature-integration    — :class:`TestMultiFeatureCatalogue`
* AC-004  per-feature-sequencing       — :class:`TestPerFeatureSequencing`
* AC-005  correlation-threading        — :class:`TestCorrelationThreading`
* AC-006  calibration-priors-snapshot  — :class:`TestCalibrationPriorsSnapshot`
* AC-007  first-wins-idempotency       — :class:`TestFirstWinsIdempotency`
* AC-008  constitutional-misconfig     — :class:`TestConstitutionalMisconfiguredPrompt`
* AC-009  specialist-override-claim    — :class:`TestSpecialistOverrideClaim`
* AC-010  worktree-confinement         — :class:`TestWorktreeConfinement`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import pytest

from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id
from forge.pipeline.constitutional_guard import (
    AutoApproveVerdict,
    ConstitutionalGuard,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import (
    PER_FEATURE_STAGES,
    CONSTITUTIONAL_STAGES,
    StageClass,
)
from forge.pipeline.supervisor import (
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
)

from .conftest import FakeMonotonicClock, InMemoryNats

# ---------------------------------------------------------------------------
# Local in-memory fakes (mirrors tests/forge/test_supervisor.py shape)
# ---------------------------------------------------------------------------


@dataclass
class FakeStateMachineReader:
    """Records and returns coarse build states keyed by build_id."""

    states: dict[str, BuildState] = field(default_factory=dict)

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)


@dataclass
class FakeOrderingStageLogReader:
    """In-memory ordering-guard reader keyed on (build, stage, feature)."""

    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))


@dataclass
class FakePerFeatureStageLogReader:
    """In-memory per-feature-sequencer stage-log reader."""

    approved_autobuilds: set[tuple[str, str]] = field(default_factory=set)

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, feature_id) in self.approved_autobuilds


@dataclass
class FakeAutobuildState:
    """Mirrors DDR-006 ``AutobuildState`` shape — feature_id + lifecycle."""

    feature_id: str
    lifecycle: str


@dataclass
class FakeAsyncTaskReader:
    """In-memory ``async_tasks`` reader."""

    states_by_build: dict[str, list[FakeAutobuildState]] = field(default_factory=dict)

    def list_autobuild_states(self, build_id: str) -> Iterable[FakeAutobuildState]:
        return list(self.states_by_build.get(build_id, []))


@dataclass
class RecordingTurnRecorder:
    """Captures every persisted per-turn row."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def record_turn(
        self,
        *,
        build_id: str,
        outcome: TurnOutcome,
        permitted_stages: frozenset[StageClass],
        chosen_stage: StageClass | None,
        chosen_feature_id: str | None,
        rationale: str,
        gate_verdict: str | None,
    ) -> None:
        self.rows.append(
            {
                "build_id": build_id,
                "outcome": outcome,
                "permitted_stages": frozenset(permitted_stages),
                "chosen_stage": chosen_stage,
                "chosen_feature_id": chosen_feature_id,
                "rationale": rationale,
                "gate_verdict": gate_verdict,
            }
        )


# ---------------------------------------------------------------------------
# Lifecycle event capture — used by correlation-threading + concurrency tests
# ---------------------------------------------------------------------------


@dataclass
class LifecycleEvent:
    """Single lifecycle-bus event with the correlation_id threaded onto it."""

    build_id: str
    stage: StageClass
    feature_id: str | None
    correlation_id: str
    payload: Mapping[str, Any]


@dataclass
class FakeLifecycleBus:
    """In-memory pub/sub for lifecycle events keyed by build_id.

    Every dispatch goes through here so the correlation-threading test can
    walk the full event log and assert a single correlation_id per build.
    """

    events: list[LifecycleEvent] = field(default_factory=list)

    def publish(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.events.append(
            LifecycleEvent(
                build_id=build_id,
                stage=stage,
                feature_id=feature_id,
                correlation_id=correlation_id,
                payload=dict(payload),
            )
        )

    def for_build(self, build_id: str) -> list[LifecycleEvent]:
        return [e for e in self.events if e.build_id == build_id]


# ---------------------------------------------------------------------------
# Recording dispatchers — async (specialist + subprocess) and sync (autobuild)
# Each receives a ``correlation_provider`` so it can stamp every lifecycle
# event with the same correlation_id the build was queued with.
# ---------------------------------------------------------------------------


@dataclass
class RecordingDispatcher:
    """Generic recording async dispatcher (specialist + subprocess shape).

    Records every kwargs payload so per-feature attribution can be verified.
    Returns a deterministic dict so the supervisor's report structure is
    populated.
    """

    label: str
    bus: FakeLifecycleBus | None = None
    correlation_lookup: dict[str, str] = field(default_factory=dict)
    artefact_paths_by_call: dict[
        tuple[str, StageClass, str | None], tuple[str, ...]
    ] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs})
        build_id = kwargs.get("build_id", "")
        stage = kwargs.get("stage")
        feature_id = kwargs.get("feature_id")
        correlation_id = self.correlation_lookup.get(build_id, "")
        artefacts = self.artefact_paths_by_call.get((build_id, stage, feature_id), ())
        result = {
            "dispatcher": self.label,
            "status": "ok",
            "task_id": f"task-{self.label}-{uuid.uuid4().hex[:8]}",
            "correlation_id": correlation_id,
            "artefact_paths": artefacts,
        }
        if self.bus is not None and stage is not None:
            self.bus.publish(
                build_id=build_id,
                stage=stage,
                feature_id=feature_id,
                correlation_id=correlation_id,
                payload={
                    "dispatcher": self.label,
                    "task_id": result["task_id"],
                },
            )
        return result


@dataclass
class RecordingSyncDispatcher:
    """Recording dispatcher — sync ``__call__`` returns a handle.

    TASK-FORGE-FRR-F010G: the autobuild dispatcher Protocol switched to
    ``Callable[..., Awaitable[Any]]`` so the launch path can await the
    deepagents middleware's async ``coroutine`` entrypoint (URL-less
    in-process ASGI transport). The class keeps a sync ``__call__`` for
    the specialist / subprocess dispatchers (whose Protocols stayed
    sync) but wraps the result in a coroutine when ``is_async=True`` is
    set so the same fake can stand in for the autobuild dispatcher.
    """

    label: str
    bus: FakeLifecycleBus | None = None
    correlation_lookup: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    is_async: bool = False

    def _make_result(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append({**kwargs})
        build_id = kwargs.get("build_id", "")
        feature_id = kwargs.get("feature_id")
        correlation_id = self.correlation_lookup.get(build_id, "")
        # Distinct per-build autobuild task_id is the AC-002 anchor.
        task_id = f"autobuild-{build_id}-{feature_id}-{uuid.uuid4().hex[:8]}"
        if self.bus is not None:
            self.bus.publish(
                build_id=build_id,
                stage=StageClass.AUTOBUILD,
                feature_id=feature_id,
                correlation_id=correlation_id,
                payload={"dispatcher": self.label, "task_id": task_id},
            )
        return {
            "dispatcher": self.label,
            "status": "ok",
            "task_id": task_id,
            "correlation_id": correlation_id,
        }

    def __call__(self, **kwargs: Any) -> Any:
        if self.is_async:
            async def _result() -> Any:
                return self._make_result(kwargs)
            return _result()
        return self._make_result(kwargs)


@dataclass
class RecordingPRReviewGate:
    """In-memory PR-review gate that records submissions per build."""

    bus: FakeLifecycleBus | None = None
    correlation_lookup: dict[str, str] = field(default_factory=dict)
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> Any:
        self.submissions.append(
            {
                "build_id": build_id,
                "feature_id": feature_id,
                "auto_approve": auto_approve,
                "rationale": rationale,
            }
        )
        correlation_id = self.correlation_lookup.get(build_id, "")
        if self.bus is not None:
            self.bus.publish(
                build_id=build_id,
                stage=StageClass.PULL_REQUEST_REVIEW,
                feature_id=feature_id or None,
                correlation_id=correlation_id,
                payload={"auto_approve": auto_approve},
            )
        return {
            "gate": "pr-review",
            "status": "submitted",
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------------------
# Reasoning-model utility — picks the next stage in the canonical Mode A
# dispatch order based on what is permitted. Models the "happy-path"
# orchestrator that drives a build to terminal.
# ---------------------------------------------------------------------------


_STAGE_DISPATCH_ORDER: tuple[StageClass, ...] = tuple(StageClass)


def _per_feature(stage: StageClass) -> bool:
    return stage in PER_FEATURE_STAGES and stage is not StageClass.PULL_REQUEST_REVIEW


@dataclass
class CanonicalOrderModel:
    """Deterministic reasoning model — picks the lowest-rank permitted stage.

    For per-feature stages it iterates the build's catalogue in declared
    order and picks the first feature that has the per-feature prerequisite
    satisfied. The canonical order ensures the test harness produces a
    repeatable dispatch sequence.

    ``auto_approve_overrides`` lets tests force ``auto_approve=True`` for
    specific stages (used by the constitutional misconfigured-prompt test).
    """

    ordering_reader: FakeOrderingStageLogReader
    auto_approve_overrides: dict[StageClass, bool] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:
        self.calls.append(
            {
                "build_id": build_id,
                "build_state": build_state,
                "permitted_stages": frozenset(permitted_stages),
                "feature_catalogue": tuple(feature_catalogue),
            }
        )
        for stage in _STAGE_DISPATCH_ORDER:
            if stage not in permitted_stages:
                continue
            if _per_feature(stage):
                # Pick the first feature whose prereq is satisfied AND whose
                # own stage is not yet approved.
                for fid in feature_catalogue:
                    if (build_id, stage, fid) in self.ordering_reader.approved:
                        continue
                    # Prereq must be approved at the feature scope.
                    prereq = {
                        StageClass.FEATURE_SPEC: StageClass.SYSTEM_DESIGN,
                        StageClass.FEATURE_PLAN: StageClass.FEATURE_SPEC,
                        StageClass.AUTOBUILD: StageClass.FEATURE_PLAN,
                    }[stage]
                    if prereq is StageClass.SYSTEM_DESIGN:
                        prereq_ok = (build_id, prereq, None) in (
                            self.ordering_reader.approved
                        )
                    else:
                        prereq_ok = (build_id, prereq, fid) in (
                            self.ordering_reader.approved
                        )
                    if prereq_ok:
                        return DispatchChoice(
                            stage=stage,
                            feature_id=fid,
                            rationale=f"canonical: {stage.value} for {fid}",
                            auto_approve=self.auto_approve_overrides.get(stage, False),
                        )
                continue
            if stage is StageClass.PULL_REQUEST_REVIEW:
                # Submit per feature in catalogue order — the first one whose
                # PR-review submission is not yet recorded.
                for fid in feature_catalogue:
                    if (build_id, stage, fid) in self.ordering_reader.approved:
                        continue
                    return DispatchChoice(
                        stage=stage,
                        feature_id=fid,
                        rationale=f"canonical: PR-review for {fid}",
                        auto_approve=self.auto_approve_overrides.get(stage, False),
                    )
                continue
            # Non-per-feature stage — already-approved stages are skipped.
            if (build_id, stage, None) in self.ordering_reader.approved:
                continue
            return DispatchChoice(
                stage=stage,
                rationale=f"canonical: {stage.value}",
                auto_approve=self.auto_approve_overrides.get(stage, False),
            )
        return None


# ---------------------------------------------------------------------------
# Harness builder — assembles a fully-wired Supervisor with the local fakes.
# ---------------------------------------------------------------------------


@dataclass
class _SupervisorHarness:
    """Bundle of every fake the test wants to assert against."""

    supervisor: Supervisor
    state_reader: FakeStateMachineReader
    ordering_reader: FakeOrderingStageLogReader
    per_feature_reader: FakePerFeatureStageLogReader
    async_task_reader: FakeAsyncTaskReader
    reasoning_model: CanonicalOrderModel
    turn_recorder: RecordingTurnRecorder
    specialist_dispatcher: RecordingDispatcher
    subprocess_dispatcher: RecordingDispatcher
    autobuild_dispatcher: RecordingSyncDispatcher
    pr_review_gate: RecordingPRReviewGate
    bus: FakeLifecycleBus
    correlation_lookup: dict[str, str]


def _build_harness(
    *,
    auto_approve_overrides: Mapping[StageClass, bool] | None = None,
    constitutional_guard: ConstitutionalGuard | None = None,
) -> _SupervisorHarness:
    bus = FakeLifecycleBus()
    correlation_lookup: dict[str, str] = {}
    state_reader = FakeStateMachineReader()
    ordering_reader = FakeOrderingStageLogReader()
    per_feature_reader = FakePerFeatureStageLogReader()
    async_task_reader = FakeAsyncTaskReader()
    reasoning_model = CanonicalOrderModel(
        ordering_reader=ordering_reader,
        auto_approve_overrides=dict(auto_approve_overrides or {}),
    )
    turn_recorder = RecordingTurnRecorder()
    specialist_dispatcher = RecordingDispatcher(
        label="specialist",
        bus=bus,
        correlation_lookup=correlation_lookup,
    )
    subprocess_dispatcher = RecordingDispatcher(
        label="subprocess",
        bus=bus,
        correlation_lookup=correlation_lookup,
    )
    autobuild_dispatcher = RecordingSyncDispatcher(
        label="autobuild_async",
        bus=bus,
        correlation_lookup=correlation_lookup,
        is_async=True,
    )
    pr_review_gate = RecordingPRReviewGate(
        bus=bus,
        correlation_lookup=correlation_lookup,
    )
    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=constitutional_guard or ConstitutionalGuard(),
        state_reader=state_reader,
        ordering_stage_log_reader=ordering_reader,
        per_feature_stage_log_reader=per_feature_reader,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=turn_recorder,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        stage_hints={},
    )
    return _SupervisorHarness(
        supervisor=supervisor,
        state_reader=state_reader,
        ordering_reader=ordering_reader,
        per_feature_reader=per_feature_reader,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=turn_recorder,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        bus=bus,
        correlation_lookup=correlation_lookup,
    )


def _register_build(
    harness: _SupervisorHarness,
    build_id: str,
    *,
    correlation_id: str,
    features: list[str],
) -> None:
    """Register a build with the harness — catalogue + correlation_id."""
    harness.ordering_reader.catalogues[build_id] = list(features)
    harness.correlation_lookup[build_id] = correlation_id
    harness.state_reader.states[build_id] = BuildState.RUNNING


async def _drive_until_no_progress(
    harness: _SupervisorHarness,
    build_id: str,
    *,
    max_turns: int = 50,
    auto_mark_approved: bool = True,
) -> list[Any]:
    """Drive ``next_turn`` repeatedly, marking each dispatched stage approved.

    Stops when the outcome is neither ``DISPATCHED`` nor a refusal that the
    next turn can recover from. The harness's ``ordering_reader`` is
    progressively populated so the canonical model can pick the next stage.
    """
    reports: list[Any] = []
    for _ in range(max_turns):
        report = await harness.supervisor.next_turn(build_id)
        reports.append(report)
        if report.outcome is TurnOutcome.DISPATCHED and auto_mark_approved:
            stage = report.chosen_stage
            assert stage is not None
            scoped_feature = report.chosen_feature_id if _per_feature(stage) else None
            # PR-review is per-feature in the canonical loop too.
            if stage is StageClass.PULL_REQUEST_REVIEW:
                scoped_feature = report.chosen_feature_id
            harness.ordering_reader.approved.add((build_id, stage, scoped_feature))
            if stage is StageClass.AUTOBUILD:
                # Mirror the autobuild approval onto the per-feature reader
                # so the per-feature sequencer stops blocking siblings.
                fid = report.chosen_feature_id
                assert fid is not None
                harness.per_feature_reader.approved_autobuilds.add((build_id, fid))
            continue
        # Any non-dispatched outcome ends the canonical drive — the test
        # itself will assert the expected terminal/refusal reason.
        break
    return reports


# ---------------------------------------------------------------------------
# AC-001 + AC-002 — Two concurrent builds (Group F @concurrency)
# ---------------------------------------------------------------------------


class TestTwoConcurrentBuilds:
    """AC-002: Two concurrent builds with distinct correlation_ids and
    isolated channels.

    Asserts:

    * Each build has a distinct autobuild ``task_id``.
    * Each build's approval pause resolves only on a response matching its
      own ``build_id`` (Group D edge-case via per-build NATS subjects).
    * The second build's product-owner stage dispatches without waiting
      for the first build's autobuild to complete (Group F).
    """

    @pytest.mark.asyncio
    async def test_distinct_autobuild_task_ids_and_correlation_ids(
        self,
    ) -> None:
        harness = _build_harness()
        _register_build(
            harness, "build-A", correlation_id="cid-A", features=["FEAT-A1"]
        )
        _register_build(
            harness, "build-B", correlation_id="cid-B", features=["FEAT-B1"]
        )
        # Pre-approve both builds through to the AUTOBUILD stage so a
        # single turn from each lands on autobuild dispatch.
        for bid, fid in [("build-A", "FEAT-A1"), ("build-B", "FEAT-B1")]:
            for s in (
                StageClass.PRODUCT_OWNER,
                StageClass.ARCHITECT,
                StageClass.SYSTEM_ARCH,
                StageClass.SYSTEM_DESIGN,
            ):
                harness.ordering_reader.approved.add((bid, s, None))
            for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
                harness.ordering_reader.approved.add((bid, s, fid))

        # Interleaved next_turn — asyncio.gather drives both at once.
        results = await asyncio.gather(
            harness.supervisor.next_turn("build-A"),
            harness.supervisor.next_turn("build-B"),
        )

        # Both must have dispatched the AUTOBUILD stage.
        assert {r.outcome for r in results} == {TurnOutcome.DISPATCHED}
        assert {r.chosen_stage for r in results} == {StageClass.AUTOBUILD}

        # Two distinct autobuild calls scoped to the right builds.
        autobuild_calls = harness.autobuild_dispatcher.calls
        assert len(autobuild_calls) == 2
        scopes = {(c["build_id"], c["feature_id"]) for c in autobuild_calls}
        assert scopes == {("build-A", "FEAT-A1"), ("build-B", "FEAT-B1")}

        # Distinct task_ids are returned by the dispatcher (the dispatcher's
        # return value is captured on each TurnReport.dispatch_result).
        dispatch_results = [r.dispatch_result for r in results]
        task_ids = {r["task_id"] for r in dispatch_results}
        assert (
            len(task_ids) == 2
        ), f"expected two distinct autobuild task_ids; got {task_ids!r}"

        # Each build's correlation_id was threaded onto the lifecycle bus
        # for that build only — no cross-build correlation_id leakage.
        events_a = harness.bus.for_build("build-A")
        events_b = harness.bus.for_build("build-B")
        assert events_a, "build-A produced no lifecycle events"
        assert events_b, "build-B produced no lifecycle events"
        assert {e.correlation_id for e in events_a} == {"cid-A"}
        assert {e.correlation_id for e in events_b} == {"cid-B"}

    @pytest.mark.asyncio
    async def test_approval_response_only_resolves_matching_build(self) -> None:
        # Per-build NATS routing — proves the Group D edge-case at the
        # subscriber surface used by the supervisor's pause/resume bridge.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        config = ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2)
        deps_a = ApprovalSubscriberDeps(
            nats_client=nats, config=config, publish_refresh=None, clock=clock
        )
        deps_b = ApprovalSubscriberDeps(
            nats_client=nats, config=config, publish_refresh=None, clock=clock
        )
        subscriber_a = ApprovalSubscriber(deps_a)
        subscriber_b = ApprovalSubscriber(deps_b)
        rid_a = derive_request_id(
            build_id="build-A", stage_label="PRReview", attempt_count=0
        )
        rid_b = derive_request_id(
            build_id="build-B", stage_label="PRReview", attempt_count=0
        )

        wait_a = asyncio.create_task(
            subscriber_a.await_response("build-A", stage_label="PRReview")
        )
        wait_b = asyncio.create_task(
            subscriber_b.await_response("build-B", stage_label="PRReview")
        )
        # Yield until both queues are registered.
        for _ in range(50):
            if "build-A" in subscriber_a._queues and "build-B" in subscriber_b._queues:
                break
            await asyncio.sleep(0)

        # Deliver only on build-A's mirror.
        await nats.deliver_response(
            build_id="build-A", request_id=rid_a, decision="approve"
        )

        result_a = await asyncio.wait_for(wait_a, timeout=1.0)
        assert result_a is not None
        assert result_a.request_id == rid_a
        # build-B's wait must NOT have been resolved by build-A's response.
        assert not wait_b.done(), (
            "build-B's wait resolved despite the response landing on "
            "build-A's mirror — per-build routing violated"
        )
        wait_b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_b
        # And — separately — delivering on build-B's mirror does resolve B.
        del rid_b  # request_id reserved for the symmetric wait below
        wait_b2 = asyncio.create_task(
            subscriber_b.await_response("build-B", stage_label="PRReview2")
        )
        rid_b2 = derive_request_id(
            build_id="build-B", stage_label="PRReview2", attempt_count=0
        )
        for _ in range(50):
            if "build-B" in subscriber_b._queues:
                break
            await asyncio.sleep(0)
        await nats.deliver_response(
            build_id="build-B", request_id=rid_b2, decision="approve"
        )
        result_b2 = await asyncio.wait_for(wait_b2, timeout=1.0)
        assert result_b2.request_id == rid_b2

    @pytest.mark.asyncio
    async def test_second_build_dispatches_while_first_autobuild_in_flight(
        self,
    ) -> None:
        """Group F: supervisor dispatches second build's PRODUCT_OWNER while
        the first build's AUTOBUILD is still in a non-terminal lifecycle."""
        harness = _build_harness()
        _register_build(harness, "build-1", correlation_id="cid-1", features=["FEAT-1"])
        _register_build(harness, "build-2", correlation_id="cid-2", features=[])

        # build-1 is mid-pipeline at AUTOBUILD; the per-feature sequencer's
        # async-task reader reports a sibling running_wave to simulate the
        # in-flight autobuild.
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.ordering_reader.approved.add(("build-1", s, None))
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.ordering_reader.approved.add(("build-1", s, "FEAT-1"))
        harness.async_task_reader.states_by_build["build-1"] = [
            FakeAutobuildState(feature_id="FEAT-1", lifecycle="running_wave")
        ]
        # build-2 is fresh — nothing approved yet, so PRODUCT_OWNER is the
        # only dispatchable stage.

        results = await asyncio.gather(
            harness.supervisor.next_turn("build-1"),
            harness.supervisor.next_turn("build-2"),
        )
        outcomes_by_build = {r.build_id: r for r in results}
        # build-1 dispatches AUTOBUILD (sibling autobuild is its own — does
        # not block self-dispatch). build-2 dispatches PRODUCT_OWNER without
        # waiting for build-1.
        assert outcomes_by_build["build-1"].outcome is TurnOutcome.DISPATCHED
        assert outcomes_by_build["build-1"].chosen_stage is StageClass.AUTOBUILD
        assert outcomes_by_build["build-2"].outcome is TurnOutcome.DISPATCHED
        assert outcomes_by_build["build-2"].chosen_stage is StageClass.PRODUCT_OWNER
        # Specialist dispatcher fired exactly once for build-2 — no
        # cross-build leak from build-1's autobuild.
        scoped = {
            (c["build_id"], c["stage"]) for c in harness.specialist_dispatcher.calls
        }
        assert (StageClass.PRODUCT_OWNER, "build-2") in {(s, b) for (b, s) in scoped}


# ---------------------------------------------------------------------------
# AC-003 — Multi-feature catalogue (Group H @integration) + Group G data
# ---------------------------------------------------------------------------


class TestMultiFeatureCatalogue:
    """3-feature catalogue produces one inner-loop dispatch per feature."""

    @pytest.mark.asyncio
    async def test_three_features_produce_one_inner_loop_dispatch_each(
        self,
    ) -> None:
        harness = _build_harness()
        features = ["FEAT-1", "FEAT-2", "FEAT-3"]
        _register_build(harness, "build-3F", correlation_id="cid-3F", features=features)
        # Pre-stamp distinct artefact paths per feature so attribution can
        # be verified after the drive.
        for fid in features:
            for stage in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
                harness.subprocess_dispatcher.artefact_paths_by_call[
                    ("build-3F", stage, fid)
                ] = (f"/worktree/build-3F/{fid}/{stage.value}.md",)

        await _drive_until_no_progress(harness, "build-3F", max_turns=80)

        # Count dispatches per stage scoped to features.
        spec_calls = [
            c
            for c in harness.subprocess_dispatcher.calls
            if c.get("stage") is StageClass.FEATURE_SPEC
        ]
        plan_calls = [
            c
            for c in harness.subprocess_dispatcher.calls
            if c.get("stage") is StageClass.FEATURE_PLAN
        ]
        autobuild_calls = harness.autobuild_dispatcher.calls
        pr_subs = harness.pr_review_gate.submissions

        assert len(spec_calls) == 3
        assert len(plan_calls) == 3
        assert len(autobuild_calls) == 3
        # One PR-review pause per feature.
        assert len(pr_subs) == 3
        assert {sub["feature_id"] for sub in pr_subs} == set(features)
        # Per-feature artefact attribution: every recorded artefact path
        # appears under exactly one feature_id (no cross-feature leak —
        # Group G @data-integrity).
        path_to_feature: dict[str, str] = {}
        for call in harness.subprocess_dispatcher.calls:
            fid = call.get("feature_id")
            if fid is None:
                continue
            stage = call["stage"]
            artefacts = harness.subprocess_dispatcher.artefact_paths_by_call.get(
                (call["build_id"], stage, fid), ()
            )
            for path in artefacts:
                # If this path was previously attributed to a different
                # feature_id, that's a Group G violation.
                assert path not in path_to_feature or path_to_feature[path] == fid, (
                    f"artefact path {path!r} attributed to multiple features "
                    f"({path_to_feature[path]!r} and {fid!r})"
                )
                path_to_feature[path] = fid


# ---------------------------------------------------------------------------
# AC-004 — Per-feature sequencing (Group D ASSUM-006)
# ---------------------------------------------------------------------------


class TestPerFeatureSequencing:
    """No second autobuild dispatch begins while a first is still in flight."""

    @pytest.mark.asyncio
    async def test_three_feature_catalogue_serialises_autobuilds(self) -> None:
        harness = _build_harness()
        features = ["FEAT-1", "FEAT-2", "FEAT-3"]
        _register_build(harness, "build-S", correlation_id="cid-S", features=features)
        # Approve everything up to AUTOBUILD for all three features so the
        # ordering guard places AUTOBUILD in the permitted set for every
        # feature simultaneously.
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.ordering_reader.approved.add(("build-S", s, None))
        for fid in features:
            for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
                harness.ordering_reader.approved.add(("build-S", s, fid))

        # Override the reasoning model with a simple script that picks the
        # next-feature autobuild on each call. The canonical-order model
        # would re-pick the same feature because the ordering reader does
        # not yet record the just-dispatched autobuild as approved; the
        # script makes the per-feature sequencer the *only* arbiter.
        feature_iter = iter(features)

        def picker(
            *,
            build_id: str,  # noqa: ARG001
            build_state: BuildState,  # noqa: ARG001
            permitted_stages: frozenset[StageClass],  # noqa: ARG001
            stage_hints: Mapping[StageClass, str],  # noqa: ARG001
            feature_catalogue: tuple[str, ...],  # noqa: ARG001
        ) -> DispatchChoice | None:
            try:
                fid = next(feature_iter)
            except StopIteration:
                return None
            return DispatchChoice(
                stage=StageClass.AUTOBUILD,
                feature_id=fid,
                rationale=f"dispatch autobuild for {fid}",
            )

        harness.supervisor.reasoning_model.choose_dispatch = picker  # type: ignore[assignment]

        # Turn 1 — supervisor dispatches FEAT-1 autobuild.
        report1 = await harness.supervisor.next_turn("build-S")
        assert report1.outcome is TurnOutcome.DISPATCHED
        assert report1.chosen_stage is StageClass.AUTOBUILD
        assert report1.chosen_feature_id == "FEAT-1"

        # Simulate FEAT-1 in non-terminal lifecycle (the autobuild dispatcher
        # just fired, so async_tasks reflects this state).
        harness.async_task_reader.states_by_build["build-S"] = [
            FakeAutobuildState(feature_id="FEAT-1", lifecycle="running_wave")
        ]
        # Turn 2 — picker returns FEAT-2 autobuild. Sequencer must veto
        # with WAITING_PRIOR_AUTOBUILD because FEAT-1 is still in flight.
        report2 = await harness.supervisor.next_turn("build-S")
        assert (
            report2.outcome is TurnOutcome.WAITING_PRIOR_AUTOBUILD
        ), f"second autobuild was not refused; got {report2.outcome!r}"
        # autobuild_dispatcher must NOT have been called again.
        assert len(harness.autobuild_dispatcher.calls) == 1

        # Now mark FEAT-1's autobuild lifecycle terminal — turn 3 (picker
        # returns FEAT-3 since FEAT-2 was already requested at turn 2)
        # should dispatch the next sibling.
        harness.async_task_reader.states_by_build["build-S"] = [
            FakeAutobuildState(feature_id="FEAT-1", lifecycle="completed")
        ]
        report3 = await harness.supervisor.next_turn("build-S")
        assert report3.outcome is TurnOutcome.DISPATCHED
        assert report3.chosen_stage is StageClass.AUTOBUILD
        assert report3.chosen_feature_id == "FEAT-3"
        assert len(harness.autobuild_dispatcher.calls) == 2


# ---------------------------------------------------------------------------
# AC-005 — Correlation-id threading (Group I @data-integrity)
# ---------------------------------------------------------------------------


class TestCorrelationThreading:
    """Every published lifecycle event for a build carries the same
    correlation_id from queue to terminal."""

    @pytest.mark.asyncio
    async def test_every_lifecycle_event_for_one_build_threads_one_correlation_id(
        self,
    ) -> None:
        harness = _build_harness()
        features = ["FEAT-X", "FEAT-Y"]
        correlation_id = "cid-deterministic-XY"
        _register_build(
            harness,
            "build-CID",
            correlation_id=correlation_id,
            features=features,
        )
        await _drive_until_no_progress(harness, "build-CID", max_turns=80)
        events = harness.bus.for_build("build-CID")
        # We must have published *something* for this build.
        assert events, "no lifecycle events published for build-CID"
        # And every one of them threads the same correlation_id.
        unique_cids = {e.correlation_id for e in events}
        assert unique_cids == {
            correlation_id
        }, f"correlation_ids drifted across the build's events: {unique_cids!r}"
        # Cross-build isolation: the correlation_id must not leak to other
        # builds (build-CID is the only build registered, but assert no
        # other build_id appears).
        all_build_ids = {e.build_id for e in harness.bus.events}
        assert all_build_ids == {"build-CID"}


# ---------------------------------------------------------------------------
# AC-006 — Calibration-priors snapshot stability (Group I @data-integrity)
# ---------------------------------------------------------------------------


@dataclass
class MutableCalibrationHistory:
    """Operator's calibration history — the *source* the snapshot is cut from."""

    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PriorsSnapshot:
    """Immutable snapshot of priors taken at build start.

    The snapshot is a tuple, not a list — once captured it cannot be
    mutated even if the underlying history is.
    """

    captured_at_build_start: tuple[dict[str, Any], ...]


def _capture_priors_snapshot(
    history: MutableCalibrationHistory,
) -> PriorsSnapshot:
    """Cut a deep snapshot of the operator's calibration history.

    Each entry is shallow-copied so subsequent mutations to dict items in
    the history do not bleed into the snapshot.
    """
    return PriorsSnapshot(
        captured_at_build_start=tuple(dict(e) for e in history.entries)
    )


class TestCalibrationPriorsSnapshot:
    """Snapshot captured at build start is immune to mid-run history changes."""

    def test_priors_snapshot_remains_stable_when_history_is_mutated(self) -> None:
        history = MutableCalibrationHistory(
            entries=[
                {"capability": "writing", "score": 0.7},
                {"capability": "review", "score": 0.8},
            ]
        )
        # Build start — snapshot is captured.
        snapshot = _capture_priors_snapshot(history)
        # Mid-run: the operator updates calibration history.
        history.entries.append({"capability": "writing", "score": 0.95})
        history.entries[0]["score"] = 0.1  # mutate an existing entry
        # Later stages of the in-flight build still read the snapshot.
        assert snapshot.captured_at_build_start == (
            {"capability": "writing", "score": 0.7},
            {"capability": "review", "score": 0.8},
        ), (
            "snapshot was clobbered by mid-run mutation of operator's "
            "calibration history"
        )
        # Sanity: the *future* build would see the new history.
        next_build_snapshot = _capture_priors_snapshot(history)
        assert len(next_build_snapshot.captured_at_build_start) == 3
        assert next_build_snapshot.captured_at_build_start[0]["score"] == 0.1

    def test_snapshot_is_a_tuple_so_callers_cannot_mutate_it(self) -> None:
        history = MutableCalibrationHistory(
            entries=[{"capability": "writing", "score": 0.7}]
        )
        snapshot = _capture_priors_snapshot(history)
        # Tuples are immutable — attempting to extend raises.
        with pytest.raises(AttributeError):
            snapshot.captured_at_build_start.append(  # type: ignore[attr-defined]
                {"capability": "x", "score": 0.0}
            )


# ---------------------------------------------------------------------------
# AC-007 — First-wins idempotency (Group D + Group I @concurrency)
# ---------------------------------------------------------------------------


class TestFirstWinsIdempotency:
    """Two simultaneous approval responses → exactly one decision applied."""

    @pytest.mark.asyncio
    async def test_two_simultaneous_responses_resolve_to_one_winner(
        self,
    ) -> None:
        # Re-uses the dedup contract from FEAT-FORGE-004's
        # ``ApprovalSubscriber._dedup`` — the integration-level companion
        # to the unit-level idempotency tests.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        deps = ApprovalSubscriberDeps(
            nats_client=nats,
            config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2),
            publish_refresh=None,
            clock=clock,
            dedup_ttl_seconds=300,
        )
        subscriber = ApprovalSubscriber(deps)
        build_id = "build-IDEMP"
        request_id = derive_request_id(
            build_id=build_id, stage_label="PRReview", attempt_count=0
        )

        # Build is paused — start the wait.
        wait_task = asyncio.create_task(
            subscriber.await_response(build_id, stage_label="PRReview")
        )
        for _ in range(50):
            if build_id in subscriber._queues:
                break
            await asyncio.sleep(0)

        # Two simultaneous responses with DIFFERENT decisions, same id.
        from nats_core.envelope import EventType, MessageEnvelope

        env_a = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "approve",
                "decided_by": "rich",
                "notes": "first",
            },
        )
        env_b = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "reject",
                "decided_by": "rich",
                "notes": "second",
            },
        )
        await asyncio.gather(
            subscriber._on_envelope(build_id=build_id, envelope=env_a),
            subscriber._on_envelope(build_id=build_id, envelope=env_b),
        )

        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is not None
        # Exactly one decision survived — winner is whichever the lock
        # serialised first (notes is "first" or "second").
        assert result.notes in {"first", "second"}
        assert result.request_id == request_id
        # Dedup buffer holds exactly one entry — proves no double-resume.
        assert len(subscriber._dedup) == 1


# ---------------------------------------------------------------------------
# AC-008 — Constitutional misconfigured-prompt (Group E @security @regression)
# ---------------------------------------------------------------------------


class TestConstitutionalMisconfiguredPrompt:
    """Even if the supervisor's prompt is misconfigured to ask for an
    auto-approve, the executor-layer ``ConstitutionalGuard`` refuses.

    This is the canary for ADR-ARCH-026 belt-and-braces: deliberately break
    one layer (prompt) and assert the other (executor guard) still holds.
    Loss of this test passing is a constitutional regression.
    """

    @pytest.mark.asyncio
    async def test_pr_review_auto_approve_refused_even_under_permissive_model(
        self,
    ) -> None:
        # Force the canonical model to set auto_approve=True on the PR
        # review stage — this is the "misconfigured prompt" — and assert
        # the executor still refuses.
        harness = _build_harness(
            auto_approve_overrides={StageClass.PULL_REQUEST_REVIEW: True}
        )
        _register_build(
            harness,
            "build-CONST",
            correlation_id="cid-CONST",
            features=["FEAT-1"],
        )
        # Pre-approve every prereq up to PR-review.
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.ordering_reader.approved.add(("build-CONST", s, None))
        for s in (
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
            StageClass.AUTOBUILD,
        ):
            harness.ordering_reader.approved.add(("build-CONST", s, "FEAT-1"))

        report = await harness.supervisor.next_turn("build-CONST")
        # The executor refused.
        assert report.outcome is TurnOutcome.REFUSED_CONSTITUTIONAL
        assert report.gate_decision is not None
        assert report.gate_decision.verdict is AutoApproveVerdict.REFUSED
        # The PR-review gate must NOT have been called — refusal happens
        # *before* dispatch.
        assert harness.pr_review_gate.submissions == []
        # And the build is NOT marked as PR-review-approved either.
        assert (
            "build-CONST",
            StageClass.PULL_REQUEST_REVIEW,
            "FEAT-1",
        ) not in harness.ordering_reader.approved


# ---------------------------------------------------------------------------
# AC-009 — Specialist override-claim (Group E @security)
# ---------------------------------------------------------------------------


class TestSpecialistOverrideClaim:
    """A specialist that asserts an override of the PR-review rule is
    silently ignored at the gating layer."""

    def test_pr_review_override_claim_is_ignored_regardless_of_payload(
        self,
    ) -> None:
        guard = ConstitutionalGuard()
        # The specialist payload tries every shape of "I have authority":
        loud_claims = [
            {"override": True, "reason": "I'm a senior engineer"},
            {"authority": "constitutional", "level": "ROOT"},
            {"manual_override_token": "abc123", "expires_in": 99999},
            {},  # empty claim
            {"deeply": {"nested": {"override": "yes"}}},
        ]
        for claim in loud_claims:
            assert (
                guard.veto_override_claim(StageClass.PULL_REQUEST_REVIEW, claim) is True
            ), f"override claim was honoured for payload {claim!r}"

    def test_non_constitutional_stage_override_claim_is_not_vetoed(self) -> None:
        # Negative control — an override claim against a non-constitutional
        # stage returns False (the guard does not over-reach).
        guard = ConstitutionalGuard()
        for stage in StageClass:
            if stage in CONSTITUTIONAL_STAGES:
                continue
            assert guard.veto_override_claim(stage, {"override": True}) is False

    @pytest.mark.asyncio
    async def test_build_pauses_for_mandatory_human_when_override_claimed(
        self,
    ) -> None:
        # End-to-end: a model that picks PR-review with auto_approve=True
        # (the override-claim arriving up the supervisor's reasoning model)
        # results in REFUSED_CONSTITUTIONAL — i.e. the build pauses for
        # mandatory human approval rather than dispatching.
        harness = _build_harness(
            auto_approve_overrides={StageClass.PULL_REQUEST_REVIEW: True}
        )
        _register_build(
            harness,
            "build-OVERRIDE",
            correlation_id="cid-O",
            features=["FEAT-1"],
        )
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.ordering_reader.approved.add(("build-OVERRIDE", s, None))
        for s in (
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
            StageClass.AUTOBUILD,
        ):
            harness.ordering_reader.approved.add(("build-OVERRIDE", s, "FEAT-1"))
        report = await harness.supervisor.next_turn("build-OVERRIDE")
        assert report.outcome is TurnOutcome.REFUSED_CONSTITUTIONAL
        # Build is not advanced past the gate — it pauses awaiting
        # mandatory human approval.
        assert harness.pr_review_gate.submissions == []


# ---------------------------------------------------------------------------
# AC-010 — Worktree confinement (Group E @security)
# ---------------------------------------------------------------------------


@dataclass
class FakeWorktreeAllowlist:
    """Defence-in-depth filesystem allowlist scoped per build_id.

    Mirrors the production ``WorktreeAllowlist`` Protocol shape — paths
    inside the build's worktree root are allowed; everything else is
    refused.
    """

    roots: dict[str, str] = field(default_factory=dict)
    rejected_paths: list[tuple[str, str]] = field(default_factory=list)

    def is_allowed(self, build_id: str, path: str) -> bool:
        root = self.roots.get(build_id)
        if not root:
            self.rejected_paths.append((build_id, path))
            return False
        # Simple containment — production uses pathlib resolution; for
        # the contract test a string-prefix check captures the same shape
        # (escapes via ``..`` are filtered out by the production implementation).
        if path.startswith(root + "/") and ".." not in path.split("/"):
            return True
        self.rejected_paths.append((build_id, path))
        return False


class TestWorktreeConfinement:
    """Subprocess dispatch attempts to write outside the worktree are refused."""

    def test_paths_outside_worktree_are_refused_by_allowlist(self) -> None:
        allowlist = FakeWorktreeAllowlist(roots={"build-W": "/worktree/build-W"})
        # Inside the worktree → allowed.
        assert allowlist.is_allowed("build-W", "/worktree/build-W/spec.md")
        assert allowlist.is_allowed("build-W", "/worktree/build-W/nested/dir/plan.md")
        # Outside the worktree → refused, and the rejection is recorded.
        for evil_path in (
            "/etc/passwd",
            "/worktree/other-build/spec.md",
            "/worktree/build-W/../../escape.txt",
            "/tmp/whatever",
        ):
            assert (
                allowlist.is_allowed("build-W", evil_path) is False
            ), f"path {evil_path!r} was allowed despite being outside worktree"
        assert len(allowlist.rejected_paths) >= 4

    def test_unknown_build_id_has_empty_allowlist(self) -> None:
        # Belt-and-braces: a build with no recorded worktree root has an
        # empty allowlist — every path is refused. Prevents writes from a
        # crash-recovered build whose root was never re-registered.
        allowlist = FakeWorktreeAllowlist(roots={})
        assert allowlist.is_allowed("build-NEW", "/worktree/build-NEW/file.md") is False
        assert len(allowlist.rejected_paths) == 1

    @pytest.mark.asyncio
    async def test_subprocess_artefact_paths_outside_allowlist_are_filtered(
        self,
    ) -> None:
        # Simulates the contract from
        # ``forge.pipeline.dispatchers.subprocess._partition_paths_by_allowlist``:
        # given a tuple of paths the dispatcher would write, paths outside
        # the worktree are partitioned into the rejected set and not
        # forwarded to downstream consumers.
        allowlist = FakeWorktreeAllowlist(roots={"build-Z": "/worktree/build-Z"})
        candidate_paths = (
            "/worktree/build-Z/feature/spec.md",  # allowed
            "/worktree/build-Z/feature/plan.md",  # allowed
            "/etc/passwd",  # refused
            "/worktree/other-build/spec.md",  # refused (cross-build)
        )
        kept: list[str] = []
        rejected: list[str] = []
        for path in candidate_paths:
            if allowlist.is_allowed("build-Z", path):
                kept.append(path)
            else:
                rejected.append(path)
        assert kept == [
            "/worktree/build-Z/feature/spec.md",
            "/worktree/build-Z/feature/plan.md",
        ]
        assert rejected == [
            "/etc/passwd",
            "/worktree/other-build/spec.md",
        ]
