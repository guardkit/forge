"""Wiring tests for the fleet-memory priors seam (ApprovalGateParts).

One ``Test*`` class per wiring guarantee:

* ``TestPartsRequirePriorsReader`` — the field is required with NO
  default: omitting it is a ``TypeError``, never a quiet empty read.
* ``TestSentinelThreading`` — the reader placed on the parts IS the
  object the gate awaits at all three activation paths
  (``maybe_gate_build``, the merge card's ``publish_card``,
  ``rearm_paused_gates``) — plus a source-level guard that no
  activation path constructs its own ``EmptyPriorsReader``.
* ``TestMergeInertGolden`` — env OFF composes ``EmptyPriorsReader`` and
  a ``gate_check`` run yields the pre-change decision shape
  (``evidence=[]``, ``MANDATORY_HUMAN_APPROVAL``).
* ``TestEvidenceNotMode`` — a reader returning priors widens the
  decision's evidence but NEVER its mode while the degraded reasoning
  model is composed.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.cli import _serve_deps_gating, _serve_gate_activation
from forge.cli._serve_deps_gating import (
    ApprovalGateParts,
    build_approval_gate_parts,
)
from forge.cli._serve_gate_activation import (
    make_merge_card_publisher,
    maybe_gate_build,
    rearm_paused_gates,
)
from forge.gating.degraded import (
    DEGRADED_RATIONALE,
    EmptyAdjustmentsReader,
    EmptyPriorsReader,
    EmptyRulesReader,
    degraded_dispatch_gate_model,
)
from forge.gating.models import GateDecision, GateMode, PriorReference
from forge.gating.wrappers import (
    GateCheckDeps,
    GateOutcome,
    PausedBuildSnapshot,
    gate_check,
)


def _forge_config() -> Any:
    from forge.config.models import ForgeConfig

    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 8, 3, tzinfo=UTC)


class _StubClient:
    """Raw-signature NATS stand-in (subscribe/publish only)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes) -> None:
        self.published.append((subject, body))

    async def subscribe(self, subject: str, cb: Any = None) -> Any:
        return SimpleNamespace(unsubscribe=_noop)


async def _noop() -> None:
    return None


class _SentinelReader:
    """Identity-checked stand-in satisfying the PriorsReader protocol."""

    async def read_priors(self, **_: Any) -> list[PriorReference]:
        return []


def _parts_with(
    reader: Any, *, emitter: Any = None
) -> ApprovalGateParts:
    return build_approval_gate_parts(
        _StubClient(),
        _forge_config(),
        priors_reader=reader,
        emitter=emitter,
    )


def _decision(mode: GateMode = GateMode.MANDATORY_HUMAN_APPROVAL) -> GateDecision:
    return GateDecision(
        build_id="build-w1",
        stage_label="autobuild",
        target_kind="subagent",
        target_identifier="autobuild_runner",
        mode=mode,
        rationale=DEGRADED_RATIONALE,
        coach_score=None,
        criterion_breakdown={},
        detection_findings=[],
        evidence=[],
        threshold_applied=None,
        degraded_mode=True,
        decided_at=_fixed_clock(),
    )


class _RunningRowPool:
    """SqliteLifecyclePersistence stand-in: builds row already RUNNING."""

    def __init__(self) -> None:
        self.connection = SimpleNamespace(execute=self._execute)

    def _execute(self, _sql: str, _params: Any) -> Any:
        return SimpleNamespace(
            fetchone=lambda: {
                "status": "RUNNING",
                "pending_approval_request_id": None,
            }
        )

    def apply_transition(self, _hop: Any) -> None:
        raise AssertionError("RUNNING row needs no synthetic hop")

    def get_build_row(self, _build_id: str) -> Any:
        return None


class TestPartsRequirePriorsReader:
    """The no-silent-fallback seam: required field, required kwarg."""

    def test_parts_without_priors_reader_raise_type_error(self) -> None:
        with pytest.raises(TypeError):
            ApprovalGateParts(  # type: ignore[call-arg]
                publisher=object(),
                subscriber=object(),
                injector=object(),
                approval_config=object(),
                expected_approver="rich",
                emitter=None,
            )

    def test_factory_without_priors_reader_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            build_approval_gate_parts(  # type: ignore[call-arg]
                _StubClient(), _forge_config()
            )


class TestSentinelThreading:
    """parts.priors_reader IS the object gate_check awaits, everywhere."""

    @pytest.mark.asyncio
    async def test_maybe_gate_build_threads_parts_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _SentinelReader()
        parts = _parts_with(sentinel)
        captured: list[GateCheckDeps] = []

        async def _fake_gate_check(*, deps: GateCheckDeps, **kwargs: Any) -> Any:
            captured.append(deps)
            return GateOutcome.AUTO_APPROVED, _decision()

        monkeypatch.setattr(_serve_gate_activation, "gate_check", _fake_gate_check)

        outcome = await maybe_gate_build(
            parts=parts,
            sqlite_pool=_RunningRowPool(),  # type: ignore[arg-type]
            gate_repository=object(),  # type: ignore[arg-type]
            gate_state_machine=object(),  # type: ignore[arg-type]
            build_id="build-w1",
            feature_id="FEAT-W1",
            correlation_id="corr-w1",
            clock=_fixed_clock,
        )

        assert outcome is GateOutcome.AUTO_APPROVED
        assert captured[0].priors_reader is sentinel

    @pytest.mark.asyncio
    async def test_merge_card_threads_parts_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _SentinelReader()
        parts = _parts_with(sentinel)
        captured: list[GateCheckDeps] = []

        async def _fake_gate_check(*, deps: GateCheckDeps, **kwargs: Any) -> Any:
            captured.append(deps)
            return GateOutcome.RESUMED, _decision()

        monkeypatch.setattr(_serve_gate_activation, "gate_check", _fake_gate_check)

        publish_card = make_merge_card_publisher(
            parts=parts,
            sqlite_pool=_RunningRowPool(),  # type: ignore[arg-type]
            gate_repository=object(),  # type: ignore[arg-type]
            gate_state_machine=object(),  # type: ignore[arg-type]
            clock=_fixed_clock,
        )
        outcome = await publish_card(
            build_id="build-w1", feature_id="FEAT-W1"
        )

        assert outcome is GateOutcome.RESUMED
        assert captured[0].priors_reader is sentinel

    @pytest.mark.asyncio
    async def test_rearm_paused_gates_threads_parts_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _SentinelReader()

        class _Emitter:
            async def emit_paused(self, *_a: Any, **_kw: Any) -> None:
                return None

        parts = _parts_with(sentinel, emitter=_Emitter())

        snap = PausedBuildSnapshot(
            build_id="build-w1",
            feature_id="FEAT-W1",
            stage_label="autobuild",
            request_id="req-w1",
            attempt_count=0,
            decision_snapshot=_decision(),
            correlation_id="corr-w1",
        )

        class _Repo:
            async def list_paused_builds(self) -> list[PausedBuildSnapshot]:
                return [snap]

        class _Pool(_RunningRowPool):
            def get_build_row(self, _build_id: str) -> Any:
                return SimpleNamespace(repo="acme/app")

        captured: list[Any] = []
        real_make = _serve_deps_gating.make_gate_check_deps

        def _recording_make(parts_arg: Any, **kwargs: Any) -> Any:
            captured.append(kwargs["priors_reader"])
            return real_make(parts_arg, **kwargs)

        monkeypatch.setattr(
            _serve_deps_gating, "make_gate_check_deps", _recording_make
        )

        from forge.adapters.nats import approval_publisher as ap_module

        recovery_envelope = MessageEnvelope(
            source_id="forge",
            event_type=EventType.APPROVAL_REQUEST,
            payload={
                "request_id": "req-w1",
                "details": {
                    "build_id": "build-w1",
                    "stage_label": "autobuild",
                    "gate_mode": "MANDATORY_HUMAN_APPROVAL",
                    "rationale": "",
                },
            },
        )
        monkeypatch.setattr(
            ap_module,
            "build_recovery_approval_envelope",
            lambda _row: recovery_envelope,
        )

        tasks = await rearm_paused_gates(
            parts=parts,
            sqlite_pool=_Pool(),  # type: ignore[arg-type]
            gate_repository=_Repo(),  # type: ignore[arg-type]
            gate_state_machine=object(),  # type: ignore[arg-type]
            resume_launcher=_noop_launcher,
            client=_StubClient(),
            clock=_fixed_clock,
        )
        try:
            assert len(tasks) == 1
            assert captured == [sentinel]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def test_no_activation_path_builds_its_own_empty_reader(self) -> None:
        # Structural guard for all three call sites at once: the
        # activation module reads parts.priors_reader and never
        # constructs an EmptyPriorsReader of its own (the docstring may
        # still NAME the class; constructing it is the regression).
        source = inspect.getsource(_serve_gate_activation)
        assert "EmptyPriorsReader()" not in source
        assert source.count("priors_reader=parts.priors_reader") == 3


async def _noop_launcher(**_: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# gate_check-level goldens over in-memory fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeRepository:
    decisions: list[GateDecision] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    resumed: list[tuple[str, str]] = field(default_factory=list)

    async def record_decision(self, decision: GateDecision) -> None:
        self.decisions.append(decision)

    async def write_to_graphiti(self, decision: GateDecision) -> None:
        return None

    async def record_paused_build(self, **kwargs: Any) -> None:
        self.paused.append(kwargs["request_id"])

    async def list_paused_builds(self) -> list[PausedBuildSnapshot]:
        return []

    async def mark_resumed(self, *, build_id: str, stage_label: str) -> None:
        self.resumed.append((build_id, stage_label))

    async def mark_overridden(self, **kwargs: Any) -> None:
        return None

    async def mark_cancelled(self, **kwargs: Any) -> None:
        return None


@dataclass
class _FakeStateMachine:
    paused: list[str] = field(default_factory=list)
    running: list[str] = field(default_factory=list)

    async def transition_to_paused(
        self, *, build_id: str, stage_label: str
    ) -> None:
        self.paused.append(build_id)

    async def transition_to_running(self, *, build_id: str) -> None:
        self.running.append(build_id)

    async def transition_to_failed(self, **kwargs: Any) -> None:
        return None

    async def transition_to_cancelled(self, **kwargs: Any) -> None:
        return None


@dataclass
class _FakePublisher:
    envelopes: list[MessageEnvelope] = field(default_factory=list)

    async def publish_request(self, envelope: MessageEnvelope) -> None:
        self.envelopes.append(envelope)


@dataclass
class _ApproveSubscriber:
    async def await_response(self, build_id: str, **_: Any) -> Any:
        return ApprovalResponsePayload(
            request_id="ignored", decision="approve", decided_by="rich"
        )


class _StubPriorsReader:
    def __init__(self, priors: list[PriorReference]) -> None:
        self._priors = priors

    async def read_priors(self, **_: Any) -> list[PriorReference]:
        return list(self._priors)


def _gate_deps(priors_reader: Any) -> GateCheckDeps:
    return GateCheckDeps(
        priors_reader=priors_reader,
        adjustments_reader=EmptyAdjustmentsReader(),
        rules_reader=EmptyRulesReader(),
        repository=_FakeRepository(),
        state_machine=_FakeStateMachine(),
        publisher=_FakePublisher(),
        subscriber=_ApproveSubscriber(),
        injector=object(),
        reasoning_model_call=degraded_dispatch_gate_model,
        clock=_fixed_clock,
    )


async def _run_degraded_gate(
    priors_reader: Any,
) -> tuple[GateOutcome, GateDecision]:
    return await gate_check(
        deps=_gate_deps(priors_reader),
        build_id="build-golden",
        feature_id="FEAT-GOLD",
        stage_label="autobuild",
        target_kind="subagent",
        target_identifier="autobuild_runner",
        coach_score=None,
        criterion_breakdown={},
        detection_findings=[],
    )


class TestMergeInertGolden:
    """Env OFF: the composed reader is Empty and the decision shape is
    byte-identical to the pre-change degraded posture."""

    @pytest.mark.asyncio
    async def test_env_off_gate_run_matches_pre_change_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("FLEET_MEMORY_ENABLED", "FLEET_MEMORY_PG_DSN"):
            monkeypatch.delenv(name, raising=False)
        from forge.adapters.fleet_memory import build_priors_reader_from_env

        reader = build_priors_reader_from_env()
        parts = _parts_with(reader)
        assert isinstance(parts.priors_reader, EmptyPriorsReader)

        outcome, decision = await _run_degraded_gate(parts.priors_reader)

        assert outcome is GateOutcome.RESUMED
        assert decision.mode is GateMode.MANDATORY_HUMAN_APPROVAL
        assert decision.evidence == []
        assert decision.rationale == DEGRADED_RATIONALE
        assert decision.coach_score is None
        assert decision.threshold_applied is None
        assert decision.degraded_mode is True


class TestEvidenceNotMode:
    """Priors widen evidence; the degraded model still mandates approval."""

    @pytest.mark.asyncio
    async def test_two_priors_ride_as_evidence_mode_unchanged(self) -> None:
        priors = [
            PriorReference(
                entity_id="build_outcome:guardkit:TASK_0001",
                group_id="forge_pipeline_history",
                summary="prior one",
                relevance_score=0.9,
            ),
            PriorReference(
                entity_id="warning:guardkit:WARN_0002",
                group_id="forge_pipeline_history",
                summary="prior two",
                relevance_score=0.4,
            ),
        ]

        outcome, decision = await _run_degraded_gate(_StubPriorsReader(priors))

        assert outcome is GateOutcome.RESUMED
        assert decision.mode is GateMode.MANDATORY_HUMAN_APPROVAL
        assert decision.evidence == priors
