"""Tests for ``forge.pipeline.dispatchers.specialist`` (TASK-MAG7-007).

Acceptance-criteria coverage map:

- AC-001: ``dispatch_specialist_stage`` exists at
  ``forge.pipeline.dispatchers.specialist`` and returns a
  :class:`StageDispatchResult` — :class:`TestSurface`.
- AC-002: Refuses any stage outside ``{PRODUCT_OWNER, ARCHITECT}`` —
  :class:`TestStageRefusal`.
- AC-003: Calls :meth:`ForwardContextBuilder.build_for` to assemble the
  payload context — :class:`TestForwardContextWiring`.
- AC-004: Delegates to the FEAT-FORGE-003 surface with the matching
  capability — :class:`TestCapabilityRouting`.
- AC-005 (Group I @data-integrity): Threads ``correlation_id`` through
  the dispatch envelope — :class:`TestCorrelationIdThreading`.
- AC-006: Records a ``stage_log`` entry on submit and updates on reply
  — :class:`TestStageLogLifecycle`.
- AC-007 (Group C @negative): Degraded specialist → ``DEGRADED``
  outcome — :class:`TestDegradedPath`.
- AC-008: Unit tests cover success, degraded, soft-timeout —
  :class:`TestSuccessPath`, :class:`TestDegradedPath`,
  :class:`TestSoftTimeoutPath`.

The tests use in-memory fakes for the three injected collaborators
(:class:`ForwardContextBuilder`, :class:`SpecialistDispatchSurface`,
:class:`StageLogWriter`) so the suite runs without SQLite, NATS, or
the FEAT-FORGE-003 transport layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability

from forge.discovery import DiscoveryCacheEntry, resolve
from forge.dispatch.models import (
    AsyncPending,
    Degraded,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)
from forge.dispatch.persistence import DispatchParameter
from forge.pipeline.dispatchers.specialist import (
    SPECIALIST_CAPABILITY_BY_STAGE,
    SPECIALIST_COMMAND_BY_STAGE,
    SPECIALIST_INTENT_BY_STAGE,
    SPECIALIST_REQUIRED_ARGS_BY_STAGE,
    SPECIALIST_STAGES,
    SpecialistDispatchSurface,
    StageDispatchOutcome,
    StageDispatchResult,
    StageLogWriter,
    build_specialist_command,
    dispatch_specialist_stage,
)
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ContextEntry,
    ForwardContextBuilder,
)
from forge.pipeline.stage_taxonomy import StageClass

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStageLogReader:
    """In-memory ``StageLogReader`` for the forward-context builder."""

    entries: dict[tuple[str, StageClass, str | None], ApprovedStageEntry] = field(
        default_factory=dict
    )

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:
        return self.entries.get((build_id, stage, feature_id))


@dataclass
class FakeWorktreeAllowlist:
    """Always-allow worktree allowlist — text-only specialist payloads
    do not exercise the path filter."""

    def is_allowed(self, build_id: str, path: str) -> bool:  # pragma: no cover
        return True


@dataclass
class RecordingDispatchSurface:
    """In-memory :class:`SpecialistDispatchSurface` recording one call.

    Stores the kwargs of the most recent ``dispatch`` call and returns
    the pre-seeded :data:`outcome`. Recording the kwargs (rather than
    asserting inline) keeps each test class focused on its own
    assertion — the same surface instance is reused across tests via
    a fixture.
    """

    outcome: DispatchOutcome | None = None
    last_call: dict[str, Any] = field(default_factory=dict)
    call_count: int = 0

    async def dispatch(
        self,
        *,
        capability: str,
        parameters: list[DispatchParameter],
        attempt_no: int = 1,
        retry_of: str | None = None,
        intent_pattern: str | None = None,
        build_id: str = "unknown",
        stage_label: str = "unknown",
        command: str = "dispatch",
        command_args: dict[str, Any] | None = None,
    ) -> DispatchOutcome:
        self.call_count += 1
        self.last_call = {
            "capability": capability,
            "parameters": list(parameters),
            "attempt_no": attempt_no,
            "retry_of": retry_of,
            "intent_pattern": intent_pattern,
            "build_id": build_id,
            "stage_label": stage_label,
            "command": command,
            "command_args": dict(command_args) if command_args else {},
        }
        if self.outcome is None:
            raise AssertionError("Test forgot to seed RecordingDispatchSurface.outcome")
        return self.outcome


@dataclass
class RecordingStageLogWriter:
    """In-memory :class:`StageLogWriter` recording the lifecycle pair."""

    submits: list[dict[str, Any]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    next_entry_id: int = 0

    def record_dispatch_submit(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        correlation_id: str,
        capability: str,
    ) -> str:
        self.next_entry_id += 1
        entry_id = f"stage-log-{self.next_entry_id}"
        self.submits.append(
            {
                "entry_id": entry_id,
                "build_id": build_id,
                "stage": stage,
                "feature_id": feature_id,
                "correlation_id": correlation_id,
                "capability": capability,
            }
        )
        return entry_id

    def record_dispatch_reply(
        self,
        *,
        entry_id: str,
        outcome: StageDispatchOutcome,
        coach_score: float | None,
        criterion_breakdown: Mapping[str, Any],
        detection_findings: Sequence[Any],
        reason: str | None,
    ) -> None:
        self.replies.append(
            {
                "entry_id": entry_id,
                "outcome": outcome,
                "coach_score": coach_score,
                "criterion_breakdown": dict(criterion_breakdown),
                "detection_findings": list(detection_findings),
                "reason": reason,
            }
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BUILD_ID = "build-FEAT-A-20260426"
_CORRELATION_ID = "corr-deadbeef0001"


@pytest.fixture
def reader() -> FakeStageLogReader:
    # Pre-seed the product-owner approved row so the architect stage's
    # forward-context lookup returns a non-empty list. This is the
    # Group A "product-owner output supplied to architect" scenario.
    return FakeStageLogReader(
        entries={
            (_BUILD_ID, StageClass.PRODUCT_OWNER, None): ApprovedStageEntry(
                gate_decision="approved",
                artefact_text="approved product-owner charter",
            ),
        }
    )


@pytest.fixture
def builder(reader: FakeStageLogReader) -> ForwardContextBuilder:
    return ForwardContextBuilder(
        stage_log_reader=reader,
        worktree_allowlist=FakeWorktreeAllowlist(),
    )


@pytest.fixture
def surface() -> RecordingDispatchSurface:
    return RecordingDispatchSurface()


@pytest.fixture
def writer() -> RecordingStageLogWriter:
    return RecordingStageLogWriter()


def _sync_result(score: float = 0.85) -> SyncResult:
    return SyncResult(
        resolution_id="res-001",
        attempt_no=1,
        coach_score=score,
        criterion_breakdown={"clarity": score, "completeness": score},
        detection_findings=[
            {"pattern": "PHANTOM", "severity": "low", "evidence": "n/a"}
        ],
    )


# ---------------------------------------------------------------------------
# AC-001 — surface
# ---------------------------------------------------------------------------


class TestSurface:
    """AC-001: ``dispatch_specialist_stage`` returns a StageDispatchResult."""

    @pytest.mark.asyncio
    async def test_returns_stage_dispatch_result(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()

        result = await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert isinstance(result, StageDispatchResult)
        assert result.stage is StageClass.PRODUCT_OWNER
        assert result.build_id == _BUILD_ID
        assert result.correlation_id == _CORRELATION_ID


# ---------------------------------------------------------------------------
# AC-002 — refuses non-specialist stages
# ---------------------------------------------------------------------------


class TestStageRefusal:
    """AC-002: refuses any stage outside the specialist set.

    Lane B / Phase E1 (B2) added FEATURE_SPEC / FEATURE_PLAN as target-terminal
    specialist stages (the 007/008 legs), so they are NO LONGER refused here —
    ``SYSTEM_ARCH`` / ``SYSTEM_DESIGN`` / ``AUTOBUILD`` / ``PULL_REQUEST_REVIEW``
    remain non-specialist (subprocess / async-autobuild) and still raise.
    """

    @pytest.mark.parametrize(
        "stage",
        [
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
            StageClass.AUTOBUILD,
            StageClass.PULL_REQUEST_REVIEW,
        ],
    )
    @pytest.mark.asyncio
    async def test_non_specialist_stage_raises_value_error(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
        stage: StageClass,
    ) -> None:
        with pytest.raises(ValueError) as excinfo:
            await dispatch_specialist_stage(
                stage=stage,
                build_id=_BUILD_ID,
                correlation_id=_CORRELATION_ID,
                forward_context_builder=builder,
                dispatch_surface=surface,
                stage_log_writer=writer,
            )
        # Programming error — message should name the offending stage so
        # the calling code can be located in the audit log.
        assert stage.value in str(excinfo.value) or repr(stage) in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_refusal_short_circuits_before_dispatch(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        with pytest.raises(ValueError):
            await dispatch_specialist_stage(
                stage=StageClass.AUTOBUILD,
                build_id=_BUILD_ID,
                correlation_id=_CORRELATION_ID,
                forward_context_builder=builder,
                dispatch_surface=surface,
                stage_log_writer=writer,
            )
        # Nothing must be persisted, dispatched, or logged for a
        # programming-error refusal.
        assert surface.call_count == 0
        assert writer.submits == []
        assert writer.replies == []


# ---------------------------------------------------------------------------
# AC-003 — forward-context wiring
# ---------------------------------------------------------------------------


class TestForwardContextWiring:
    """AC-003: calls ``ForwardContextBuilder.build_for``."""

    @pytest.mark.asyncio
    async def test_architect_stage_threads_product_owner_charter_into_context(
        self,
        reader: FakeStageLogReader,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=StageClass.ARCHITECT,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        # The charter text must appear inside one of the dispatch
        # parameters — the dispatcher does not silently drop the
        # forward-propagated context.
        param_values = [p.value for p in surface.last_call["parameters"]]
        assert any("approved product-owner charter" in v for v in param_values)

    @pytest.mark.asyncio
    async def test_product_owner_dispatch_has_no_forward_context(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        # PRODUCT_OWNER is the entry stage — no producer, so the
        # builder returns []. The only parameter must be the
        # correlation_id (no context entries flattened in).
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        names = [p.name for p in surface.last_call["parameters"]]
        assert names == ["correlation_id"]


# ---------------------------------------------------------------------------
# AC-004 — capability routing
# ---------------------------------------------------------------------------


class TestCapabilityRouting:
    """AC-004: dispatches to capability matching the stage."""

    @pytest.mark.parametrize(
        ("stage", "expected_capability"),
        [
            (StageClass.PRODUCT_OWNER, "product_owner_specialist"),
            (StageClass.ARCHITECT, "architect_specialist"),
        ],
    )
    @pytest.mark.asyncio
    async def test_capability_per_stage(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
        stage: StageClass,
        expected_capability: str,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=stage,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert surface.last_call["capability"] == expected_capability
        # The capability map should align with the public constant so
        # downstream auditors trust a single source of truth.
        assert SPECIALIST_CAPABILITY_BY_STAGE[stage] == expected_capability

    @pytest.mark.asyncio
    async def test_stage_label_threaded_through(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert surface.last_call["stage_label"] == StageClass.PRODUCT_OWNER.value
        assert surface.last_call["build_id"] == _BUILD_ID


# ---------------------------------------------------------------------------
# AC-005 — correlation_id threading (Group I @data-integrity)
# ---------------------------------------------------------------------------


class TestCorrelationIdThreading:
    """AC-005 / Group I: correlation_id threaded onto the envelope."""

    @pytest.mark.asyncio
    async def test_correlation_id_appears_as_dispatch_parameter(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        params = surface.last_call["parameters"]
        correlation_params = [p for p in params if p.name == "correlation_id"]
        assert len(correlation_params) == 1
        # Must thread the value unchanged — Group I @data-integrity
        # explicitly forbids re-derivation.
        assert correlation_params[0].value == _CORRELATION_ID
        # The correlation id is a public routing key — refusing to
        # mark it sensitive=False would make persistence redact it,
        # which would corrupt the audit trail.
        assert correlation_params[0].sensitive is False


# ---------------------------------------------------------------------------
# AC-006 — stage_log lifecycle
# ---------------------------------------------------------------------------


class TestStageLogLifecycle:
    """AC-006: records dispatch on submit and updates on reply."""

    @pytest.mark.asyncio
    async def test_submit_recorded_with_capability_and_correlation(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert len(writer.submits) == 1
        submit = writer.submits[0]
        assert submit["build_id"] == _BUILD_ID
        assert submit["stage"] is StageClass.PRODUCT_OWNER
        assert submit["correlation_id"] == _CORRELATION_ID
        assert submit["capability"] == "product_owner_specialist"

    @pytest.mark.asyncio
    async def test_reply_recorded_with_coach_score_and_findings(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result(score=0.91)

        result = await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert len(writer.replies) == 1
        reply = writer.replies[0]
        assert reply["entry_id"] == writer.submits[0]["entry_id"]
        assert reply["outcome"] is StageDispatchOutcome.COMPLETED
        assert reply["coach_score"] == pytest.approx(0.91)
        assert reply["criterion_breakdown"] == {
            "clarity": pytest.approx(0.91),
            "completeness": pytest.approx(0.91),
        }
        assert len(reply["detection_findings"]) == 1
        # The same data is mirrored on the returned result so the
        # gating layer can cross-reference the writer entry without a
        # second query.
        assert result.stage_log_entry_id == reply["entry_id"]

    @pytest.mark.asyncio
    async def test_submit_recorded_before_dispatch_call(
        self,
        builder: ForwardContextBuilder,
        writer: RecordingStageLogWriter,
    ) -> None:
        """Write-before-send: a dispatch surface that observes the
        writer state must see a submit row already present when its
        ``dispatch`` is invoked."""
        observed_submits_at_dispatch_time: list[int] = []

        @dataclass
        class ObservingSurface:
            outcome: DispatchOutcome | None = None

            async def dispatch(
                self,
                *,
                capability: str,
                parameters: list[DispatchParameter],
                attempt_no: int = 1,
                retry_of: str | None = None,
                intent_pattern: str | None = None,
                build_id: str = "unknown",
                stage_label: str = "unknown",
                command: str = "dispatch",
                command_args: dict[str, Any] | None = None,
            ) -> DispatchOutcome:
                observed_submits_at_dispatch_time.append(len(writer.submits))
                assert self.outcome is not None
                return self.outcome

        observer = ObservingSurface(outcome=_sync_result())

        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=observer,
            stage_log_writer=writer,
        )

        assert observed_submits_at_dispatch_time == [1]


# ---------------------------------------------------------------------------
# AC-008 — success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    """AC-008: success-path dispatch returns COMPLETED outcome."""

    @pytest.mark.asyncio
    async def test_completed_outcome_carries_coach_score(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result(score=0.78)

        result = await dispatch_specialist_stage(
            stage=StageClass.ARCHITECT,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert result.outcome is StageDispatchOutcome.COMPLETED
        assert result.coach_score == pytest.approx(0.78)
        assert result.reason is None
        assert len(result.detection_findings) == 1


# ---------------------------------------------------------------------------
# AC-007 — degraded path (Group C @negative)
# ---------------------------------------------------------------------------


class TestDegradedPath:
    """AC-007: ``Degraded`` reply translates to ``StageDispatchResult.DEGRADED``."""

    @pytest.mark.asyncio
    async def test_degraded_outcome_for_no_specialist_resolvable(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = Degraded(
            resolution_id="res-degraded-001",
            attempt_no=1,
            reason="no_specialist_resolvable",
        )

        result = await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        # AC-007 wording: "returns a StageDispatchResult.DEGRADED
        # outcome". Verify both the enum-style access pattern and the
        # value comparison so a future refactor of the access pattern
        # still trips this test.
        assert result.outcome is StageDispatchResult.DEGRADED
        assert result.outcome is StageDispatchOutcome.DEGRADED
        assert result.coach_score is None
        assert result.reason == "no_specialist_resolvable"
        # The reply still goes through the writer so the audit trail
        # records *why* the stage degraded.
        assert writer.replies[-1]["outcome"] is StageDispatchOutcome.DEGRADED
        assert writer.replies[-1]["reason"] == "no_specialist_resolvable"


# ---------------------------------------------------------------------------
# AC-008 — soft-timeout path
# ---------------------------------------------------------------------------


class TestSoftTimeoutPath:
    """AC-008: ``local_timeout`` reply translates to ``SOFT_TIMEOUT``.

    The dispatcher itself does NOT retry — retry-with-context lives at
    the supervisor (FEAT-FORGE-003 ASSUM-005). The test verifies the
    structured outcome is surfaced; the supervisor's retry logic is
    covered by its own task brief.
    """

    @pytest.mark.asyncio
    async def test_local_timeout_translates_to_soft_timeout(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = DispatchError(
            resolution_id="res-timeout-001",
            attempt_no=1,
            error_explanation="local_timeout",
        )

        result = await dispatch_specialist_stage(
            stage=StageClass.ARCHITECT,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert result.outcome is StageDispatchOutcome.SOFT_TIMEOUT
        assert result.coach_score is None
        assert result.reason == "local_timeout"
        # The dispatcher must not retry — exactly one dispatch call was
        # issued; the supervisor decides whether to call again.
        assert surface.call_count == 1

    @pytest.mark.asyncio
    async def test_other_error_translates_to_error_outcome(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        # Non-timeout DispatchError surfaces as ERROR, not SOFT_TIMEOUT,
        # so the supervisor's retry logic does not pick it up by
        # mistake.
        surface.outcome = DispatchError(
            resolution_id="res-err-001",
            attempt_no=1,
            error_explanation="malformed_reply",
        )

        result = await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert result.outcome is StageDispatchOutcome.ERROR
        assert result.reason == "malformed_reply"


# ---------------------------------------------------------------------------
# Defensive — async_pending must not silently succeed
# ---------------------------------------------------------------------------


class TestAsyncPendingRejection:
    """Specialist dispatch is synchronous in Mode A (FEAT-FORGE-007
    ASSUM-002) — an async_pending reply is treated as a protocol
    violation surfaced as ERROR rather than being silently held
    open."""

    @pytest.mark.asyncio
    async def test_async_pending_reply_yields_error_outcome(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = AsyncPending(
            resolution_id="res-async-001",
            attempt_no=1,
            run_identifier="run-99",
        )

        result = await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        assert result.outcome is StageDispatchOutcome.ERROR
        assert "async_pending" in (result.reason or "")


# ---------------------------------------------------------------------------
# Protocol structural conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """The injected fakes must structurally satisfy the public
    Protocols so production wiring (which substitutes real
    implementations) cannot drift from the test surface."""

    def test_recording_surface_satisfies_specialist_dispatch_surface(
        self, surface: RecordingDispatchSurface
    ) -> None:
        assert isinstance(surface, SpecialistDispatchSurface)

    def test_recording_writer_satisfies_stage_log_writer(
        self, writer: RecordingStageLogWriter
    ) -> None:
        assert isinstance(writer, StageLogWriter)


# ---------------------------------------------------------------------------
# TASK-FWD-PLAN-PODISCO — intent-pattern fallback resolution
# ---------------------------------------------------------------------------


def _resolve_entry(manifest: AgentManifest) -> DiscoveryCacheEntry:
    """A ready, zero-queue cache entry wrapping ``manifest``.

    Hand-built so the resolver runs against an in-memory snapshot with no
    live NATS broker (the whole point of ``resolve`` being a pure function
    of the snapshot).
    """
    ts = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
    return DiscoveryCacheEntry(
        manifest=manifest,
        last_heartbeat_at=ts,
        last_heartbeat_status="ready",
        last_queue_depth=0,
        last_active_tasks=0,
        cached_at=ts,
    )


def _tool(name: str) -> ToolCapability:
    return ToolCapability(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
        returns="dict",
        risk_level="read_only",
    )


def _intent(pattern: str, confidence: float = 0.95) -> IntentCapability:
    # Mirrors the live agents: signals present but NOT consulted by the
    # resolver's matcher — matching is on ``pattern`` + ``confidence`` only.
    return IntentCapability(
        pattern=pattern,
        signals=[pattern.split(".")[0], "specialist"],
        confidence=confidence,
        description=f"intent {pattern}",
    )


def _agent(
    agent_id: str,
    *,
    tools: list[ToolCapability] | None = None,
    intents: list[IntentCapability] | None = None,
) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name=agent_id.title(),
        version="0.1.0",
        template="specialist-agent",
        trust_tier="specialist",
        status="ready",
        max_concurrent=1,
        tools=tools or [],
        intents=intents or [],
        required_permissions=[],
    )


class TestSpecialistIntentFallbackResolution:
    """TASK-FWD-PLAN-PODISCO: specialist stages resolve via the intent
    fallback when no agent advertises the exact tool the dispatcher asks
    for — using forge's own exact-tool → intent-fallback algorithm.

    These tests feed :func:`resolve` the *exact same* ``tool_name`` and
    ``intent_pattern`` pair that :func:`dispatch_specialist_stage` threads
    through the dispatch surface, so they assert the wired-up behaviour end
    to end without touching a broker.
    """

    def test_intent_map_covers_every_specialist_stage(self) -> None:
        # Guardrail: the intent map must not drift from the capability map.
        assert set(SPECIALIST_INTENT_BY_STAGE) == set(SPECIALIST_STAGES)

    def test_product_owner_resolves_via_intent_when_only_product_intent(
        self,
    ) -> None:
        # Live reality: the product-owner-agent advertises ``po_*`` tools
        # plus a ``product.*`` intent — and NO ``product_owner_specialist``
        # tool. Model exactly that: the intent is the only handle.
        po_agent = _agent(
            "product-owner-agent",
            tools=[_tool("po_idea"), _tool("po_greenfield"), _tool("po_scope")],
            intents=[_intent("product.*", confidence=0.95)],
        )
        snapshot = {po_agent.agent_id: _resolve_entry(po_agent)}

        agent_id, resolution = resolve(
            snapshot,
            tool_name=SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER],
            intent_pattern=SPECIALIST_INTENT_BY_STAGE[StageClass.PRODUCT_OWNER],
        )

        assert agent_id == "product-owner-agent"
        assert resolution.match_source == "intent_pattern"
        assert resolution.matched_agent_id == "product-owner-agent"
        assert resolution.chosen_confidence == pytest.approx(0.95)

    def test_architect_resolves_via_intent_when_only_architecture_intent(
        self,
    ) -> None:
        arch_agent = _agent(
            "architect-agent",
            tools=[_tool("architect_design"), _tool("architect_review")],
            intents=[_intent("architecture.*", confidence=0.95)],
        )
        snapshot = {arch_agent.agent_id: _resolve_entry(arch_agent)}

        agent_id, resolution = resolve(
            snapshot,
            tool_name=SPECIALIST_CAPABILITY_BY_STAGE[StageClass.ARCHITECT],
            intent_pattern=SPECIALIST_INTENT_BY_STAGE[StageClass.ARCHITECT],
        )

        assert agent_id == "architect-agent"
        assert resolution.match_source == "intent_pattern"

    def test_product_intent_below_floor_stays_unresolved(self) -> None:
        # Confidence floor is real: a ``product.*`` intent under 0.70 must
        # NOT resolve — proves the match is gated on confidence, not just
        # the pattern.
        weak = _agent(
            "weak-po",
            intents=[_intent("product.*", confidence=0.5)],
        )
        snapshot = {weak.agent_id: _resolve_entry(weak)}

        agent_id, resolution = resolve(
            snapshot,
            tool_name=SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER],
            intent_pattern=SPECIALIST_INTENT_BY_STAGE[StageClass.PRODUCT_OWNER],
        )

        assert agent_id is None
        assert resolution.match_source == "unresolved"

    def test_tool_exact_still_wins_over_intent(self) -> None:
        # If an agent ever DOES advertise the exact capability tool, the
        # exact-tool step must win — the intent fallback is only reached
        # when the tool step misses.
        exact = _agent(
            "future-po",
            tools=[_tool(SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER])],
            intents=[_intent("product.*", confidence=0.95)],
        )
        snapshot = {exact.agent_id: _resolve_entry(exact)}

        agent_id, resolution = resolve(
            snapshot,
            tool_name=SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER],
            intent_pattern=SPECIALIST_INTENT_BY_STAGE[StageClass.PRODUCT_OWNER],
        )

        assert agent_id == "future-po"
        assert resolution.match_source == "tool_exact"
        assert resolution.chosen_confidence == 1.0


class TestSpecialistIntentThreading:
    """TASK-FWD-PLAN-PODISCO: ``dispatch_specialist_stage`` threads the
    stage's mapped intent pattern onto the dispatch surface call (which is
    the ``DispatchOrchestrator`` in production, forwarding it into
    :func:`resolve`)."""

    @pytest.mark.parametrize(
        ("stage", "expected_intent"),
        [
            (StageClass.PRODUCT_OWNER, "product.*"),
            (StageClass.ARCHITECT, "architecture.*"),
        ],
    )
    @pytest.mark.asyncio
    async def test_intent_pattern_threaded_to_surface(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
        stage: StageClass,
        expected_intent: str,
    ) -> None:
        surface.outcome = _sync_result()

        await dispatch_specialist_stage(
            stage=stage,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
        )

        # The exact tool name stays the first-choice capability; the intent
        # is the fallback threaded alongside it.
        assert surface.last_call["capability"] == SPECIALIST_CAPABILITY_BY_STAGE[stage]
        assert surface.last_call["intent_pattern"] == expected_intent
        assert surface.last_call["intent_pattern"] == SPECIALIST_INTENT_BY_STAGE[stage]


# ---------------------------------------------------------------------------
# M2 + M3 (DISPATCHFMT+ S2) — the data-driven dispatch build: a deployed verb
# plus a dict of that verb's required inputs, per contract decisions D1 + D3.
# ---------------------------------------------------------------------------


_REALISTIC_REQUEST = (
    "Build a voice-first standup bot that summarises yesterday's git activity "
    "and reads it back over the office speaker each morning."
)


class TestBuildSpecialistCommand:
    """`build_specialist_command` — verb + required-input args per stage."""

    def test_product_owner_uses_deployed_greenfield_verb(self) -> None:
        command, _ = build_specialist_command(
            StageClass.PRODUCT_OWNER,
            request_text=_REALISTIC_REQUEST,
            context_entries=[],
        )
        # M2: a real deployed verb (the PO command_map routes "greenfield").
        assert command == "greenfield"
        assert command == SPECIALIST_COMMAND_BY_STAGE[StageClass.PRODUCT_OWNER]

    def test_product_owner_threads_request_text_as_problem_statement(self) -> None:
        command, args = build_specialist_command(
            StageClass.PRODUCT_OWNER,
            request_text=_REALISTIC_REQUEST,
            context_entries=[],
        )
        # M3: args is a dict carrying every deployed-required key, non-empty.
        assert isinstance(args, dict)
        required = SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.PRODUCT_OWNER]
        assert all(args.get(name) for name in required)
        # D1: the greenfield problem_statement IS the raw planning request text.
        assert args["problem_statement"] == _REALISTIC_REQUEST

    def test_product_owner_strips_and_drops_blank_request_text(self) -> None:
        # A blank request cannot source problem_statement — the key is absent
        # (the caller logs it; the deployed router rejects the command).
        _, args = build_specialist_command(
            StageClass.PRODUCT_OWNER,
            request_text="   ",
            context_entries=[],
        )
        assert "problem_statement" not in args
        # And leading/trailing whitespace on a real request is trimmed.
        _, args2 = build_specialist_command(
            StageClass.PRODUCT_OWNER,
            request_text=f"  {_REALISTIC_REQUEST}  ",
            context_entries=[],
        )
        assert args2["problem_statement"] == _REALISTIC_REQUEST

    def test_product_owner_drops_forward_context_charter(self) -> None:
        # D3: no deployed greenfield handler reads a generic context arg, so the
        # "--context" charter entry is dropped — never leaks into the wire args.
        _, args = build_specialist_command(
            StageClass.PRODUCT_OWNER,
            request_text=_REALISTIC_REQUEST,
            context_entries=[
                ContextEntry(
                    flag="--context", value="approved charter blob", kind="text"
                )
            ],
        )
        assert args == {"problem_statement": _REALISTIC_REQUEST}
        assert "context" not in args

    def test_architect_uses_deployed_greenfield_verb(self) -> None:
        command, _ = build_specialist_command(
            StageClass.ARCHITECT,
            request_text=_REALISTIC_REQUEST,
            context_entries=[
                ContextEntry(flag="--docs_path", value="docs/product", kind="path"),
                ContextEntry(flag="--scope", value="whole-system", kind="text"),
            ],
        )
        # M2: the architect command_map also routes "greenfield" (overloaded
        # verb; the target agent disambiguates the handler).
        assert command == "greenfield"
        assert command == SPECIALIST_COMMAND_BY_STAGE[StageClass.ARCHITECT]

    def test_architect_sources_required_args_from_forward_context(self) -> None:
        _, args = build_specialist_command(
            StageClass.ARCHITECT,
            request_text=_REALISTIC_REQUEST,
            context_entries=[
                ContextEntry(flag="--docs_path", value="docs/product", kind="path"),
                ContextEntry(flag="--scope", value="whole-system", kind="text"),
            ],
        )
        # M3: every deployed-required architect key present with real values,
        # sourced by flag-name from the forward-context entries (D3).
        required = SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.ARCHITECT]
        assert all(args.get(name) for name in required)
        assert args["docs_path"] == "docs/product"
        assert args["scope"] == "whole-system"

    def test_refuses_non_specialist_stage(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            build_specialist_command(
                StageClass.AUTOBUILD,
                request_text=_REALISTIC_REQUEST,
                context_entries=[],
            )
        assert "AUTOBUILD".lower() in str(excinfo.value).lower() or (
            StageClass.AUTOBUILD.value in str(excinfo.value)
        )


class TestTargetTerminalLegs:
    """Lane B / Phase E1 (B2): the 007/008 target-terminal dispatch legs."""

    def test_feature_spec_threads_supplied_from_input(self) -> None:
        # Contract of record: specialist-agent roles/product_owner/modes/
        # feature_spec.py required_args=("from_input",).
        command, args = build_specialist_command(
            StageClass.FEATURE_SPEC,
            request_text=None,
            context_entries=[],
            extra_command_args={"from_input": "the committed handoff markdown"},
        )
        assert command == SPECIALIST_COMMAND_BY_STAGE[StageClass.FEATURE_SPEC]
        # The forge-supplied required arg reaches the wire (007 reads it).
        required = SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.FEATURE_SPEC]
        assert required == ("from_input",)
        assert all(args.get(name) for name in required)
        assert args["from_input"] == "the committed handoff markdown"

    def test_feature_plan_threads_spec_triple_descriptor_and_minted_id(self) -> None:
        # Contract of record: specialist-agent roles/architect/modes/
        # feature_plan.py required_args=("feature_id","spec_feature",
        # "spec_summary","target_repo_descriptor"). RV-1: forge mints + threads
        # the SUPPLIED feature_id. The descriptor is a structured object.
        descriptor = {"repo": "guardkit/api_test", "test_roots": ["tests"]}
        command, args = build_specialist_command(
            StageClass.FEATURE_PLAN,
            request_text=None,
            context_entries=[],
            extra_command_args={
                "feature_id": "FEAT-BEEF",
                "spec_feature": "Feature: stats\n  Scenario: ok\n    Given a\n",
                "spec_summary": "# summary\n",
                "target_repo_descriptor": descriptor,
            },
        )
        assert command == SPECIALIST_COMMAND_BY_STAGE[StageClass.FEATURE_PLAN]
        required = SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.FEATURE_PLAN]
        assert required == (
            "feature_id",
            "spec_feature",
            "spec_summary",
            "target_repo_descriptor",
        )
        assert all(args.get(name) for name in required)
        assert args["feature_id"] == "FEAT-BEEF"
        # The structured descriptor object passes through unchanged (not str()d).
        assert args["target_repo_descriptor"] == descriptor

    @pytest.mark.asyncio
    async def test_feature_spec_dispatch_resolves_via_product_intent(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        # 007 rides the product-owner agent's advertised ``product.*`` intent —
        # the exact tool name (po_feature_spec) never matches, so the resolver
        # falls back to intent (the M-08 pattern extended to the spec leg).
        surface.outcome = _sync_result()
        await dispatch_specialist_stage(
            stage=StageClass.FEATURE_SPEC,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
            extra_command_args={"from_input": "handoff md"},
        )
        assert surface.last_call["capability"] == "po_feature_spec"
        assert surface.last_call["intent_pattern"] == "product.*"
        assert surface.last_call["command_args"]["from_input"] == "handoff md"

    @pytest.mark.asyncio
    async def test_feature_plan_dispatch_resolves_via_architecture_intent(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()
        await dispatch_specialist_stage(
            stage=StageClass.FEATURE_PLAN,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
            extra_command_args={
                "feature_id": "FEAT-BEEF",
                "spec_feature": "Feature: stats\n",
                "spec_summary": "# summary\n",
                "target_repo_descriptor": {
                    "repo": "guardkit/api_test",
                    "test_roots": ["tests"],
                },
            },
        )
        assert surface.last_call["capability"] == "architect_feature_plan"
        assert surface.last_call["intent_pattern"] == "architecture.*"
        assert surface.last_call["command_args"]["feature_id"] == "FEAT-BEEF"
        assert surface.last_call["command_args"]["target_repo_descriptor"] == {
            "repo": "guardkit/api_test",
            "test_roots": ["tests"],
        }


class TestDispatchThreadsCommand:
    """`dispatch_specialist_stage` threads the resolved verb + args on."""

    @pytest.mark.asyncio
    async def test_product_owner_dispatch_carries_command_and_args(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
    ) -> None:
        surface.outcome = _sync_result()
        await dispatch_specialist_stage(
            stage=StageClass.PRODUCT_OWNER,
            build_id=_BUILD_ID,
            correlation_id=_CORRELATION_ID,
            forward_context_builder=builder,
            dispatch_surface=surface,
            stage_log_writer=writer,
            request_text=_REALISTIC_REQUEST,
        )
        # The resolved deployed verb + dict args reach the dispatch surface,
        # which stamps them onto the wire CommandPayload (M2 + M3).
        assert surface.last_call["command"] == "greenfield"
        assert surface.last_call["command_args"] == {
            "problem_statement": _REALISTIC_REQUEST
        }

    @pytest.mark.asyncio
    async def test_missing_request_text_still_dispatches_but_warns(
        self,
        builder: ForwardContextBuilder,
        surface: RecordingDispatchSurface,
        writer: RecordingStageLogWriter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        surface.outcome = _sync_result()
        with caplog.at_level("WARNING"):
            await dispatch_specialist_stage(
                stage=StageClass.PRODUCT_OWNER,
                build_id=_BUILD_ID,
                correlation_id=_CORRELATION_ID,
                forward_context_builder=builder,
                dispatch_surface=surface,
                stage_log_writer=writer,
                request_text=None,
            )
        # Best-effort: the command is still sent (verb resolves) but with no
        # problem_statement, and a loud warning names the missing arg.
        assert surface.last_call["command"] == "greenfield"
        assert "problem_statement" not in surface.last_call["command_args"]
        assert any(
            "missing required deployed arg" in rec.message
            and "problem_statement" in str(rec.args)
            for rec in caplog.records
        )
