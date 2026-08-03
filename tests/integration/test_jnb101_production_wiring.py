"""TASK-JNB-101 — production approval-gate wiring, end to end.

Every scenario drives :func:`forge.gating.wrappers.gate_check` through
the REAL production factory output —
:func:`forge.cli._serve_deps_gating.build_approval_gate_parts` +
:func:`make_gate_check_deps` — over the in-memory NATS double, with a
REAL :class:`PipelineLifecycleEmitter` on the same transport (arch
review R5: the build-resumed assertion is against an actually-published
envelope, never a mock call). Only the transport and the SQLite-shaped
repository / state machine are doubled.

Scenario classes mirror the task's Test Requirements names:

* ``TestApproveResumesOnce``
* ``TestOverrideResumes``
* ``TestRejectCancels``
* ``TestDeferRepublishWithRefreshedRequestId``
* ``TestWindowExpiryCancels``
* ``TestCeilingBreachCancels``
* ``TestSpoofedReplyRefused``
* ``TestConfigAlignment``

The four-step validation chain (payload validation → ``decided_by``
allowlist → ``correlation_id`` match → ``request_id`` 300s dedup) is
exercised through the wiring — the correlation step is live here for
the first time because ``make_gate_check_deps`` binds the build's
``expected_correlation_id`` via ``_BoundContextSubscriber``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from forge.cli._serve_deps_gating import (
    build_approval_gate_parts,
    make_gate_check_deps,
)
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.config.models import ForgeConfig
from forge.gating.degraded import EmptyPriorsReader
from forge.gating.identity import derive_request_id
from forge.gating.models import GateMode
from forge.gating.wrappers import (
    REASON_MAX_WAIT,
    GateCheckDeps,
    GateOutcome,
    gate_check,
)
from forge.pipeline import BuildContext
from nats_core.envelope import EventType, MessageEnvelope

from .conftest import (
    BUILD_ID,
    FEATURE_ID,
    RICH,
    STAGE_LABEL,
    FakeAdjustmentsReader,
    FakePriorsReader,
    FakeRulesReader,
    FixedDateClock,
    InMemoryNats,
    InMemoryRepository,
    InMemoryStateMachine,
    model_returning,
)

CORRELATION_ID = "corr-jnb101-0001"
REQUEST_SUBJECT = f"agents.approval.forge.{BUILD_ID}"
MIRROR_SUBJECT = f"agents.approval.forge.{BUILD_ID}.response"
RESUMED_SUBJECT = f"pipeline.build-resumed.{FEATURE_ID}"


class _AdvancingClock:
    """Monotonic clock auto-stepping per read — drives the ceiling loop."""

    def __init__(self, step: float = 1.0) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        value = self._now
        self._now += self._step
        return value


def _forge_config(**approval_overrides: Any) -> ForgeConfig:
    doc: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
    }
    if approval_overrides:
        doc["approval"] = approval_overrides
    return ForgeConfig.model_validate(doc)


def _production_deps(
    nats: InMemoryNats,
    repo: InMemoryRepository,
    sm: InMemoryStateMachine,
    *,
    forge_config: ForgeConfig | None = None,
    refresh_repository: InMemoryRepository | None = None,
    subscriber_clock: Any = None,
) -> GateCheckDeps:
    """Assemble per-build GateCheckDeps exactly as production would.

    The emitter is a REAL :class:`PipelineLifecycleEmitter` over the
    same in-memory transport, so resume envelopes land on
    ``pipeline.build-resumed.<feature_id>`` for the wire assertions.
    """
    cfg = forge_config or _forge_config()
    _publisher, emitter = build_publisher_and_emitter(nats)
    parts = build_approval_gate_parts(
        nats,
        cfg,
        priors_reader=EmptyPriorsReader(),
        emitter=emitter,
        repository=refresh_repository,
        subscriber_clock=subscriber_clock,
    )
    ctx = BuildContext(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        correlation_id=CORRELATION_ID,
        wave_total=1,
    )
    return make_gate_check_deps(
        parts,
        ctx=ctx,
        priors_reader=FakePriorsReader(),
        adjustments_reader=FakeAdjustmentsReader(),
        rules_reader=FakeRulesReader(),
        repository=repo,
        state_machine=sm,
        reasoning_model_call=model_returning(GateMode.FLAG_FOR_REVIEW),
        clock=FixedDateClock(),
    )


def _start_gate(deps: GateCheckDeps) -> "asyncio.Task[Any]":
    return asyncio.create_task(
        gate_check(
            deps=deps,
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            stage_label=STAGE_LABEL,
            target_kind="local_tool",
            target_identifier="t",
            coach_score=0.7,
            criterion_breakdown={"c": 0.7},
            detection_findings=[],
        )
    )


async def _wait_until(cond: Any, *, timeout: float = 5.0, what: str = "") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition never held within {timeout}s: {what}")
        await asyncio.sleep(0)


async def _drive_response(
    nats: InMemoryNats,
    *,
    request_id: str,
    decision: str,
    decided_by: str = RICH,
    notes: str | None = None,
) -> None:
    """Wait for the live subscriber, then deliver a response envelope."""
    await _wait_until(
        lambda: nats.subscribers.get(MIRROR_SUBJECT),
        what=f"subscriber on {MIRROR_SUBJECT}",
    )
    await nats.deliver_response(
        build_id=BUILD_ID,
        request_id=request_id,
        decision=decision,
        decided_by=decided_by,
        notes=notes,
    )


def _resumed_payloads(nats: InMemoryNats) -> list[dict[str, Any]]:
    """Parse every build-resumed envelope published on the wire."""
    return [
        json.loads(body)["payload"] for body in nats.published.get(RESUMED_SUBJECT, [])
    ]


def _request_id(attempt: int) -> str:
    return derive_request_id(
        build_id=BUILD_ID, stage_label=STAGE_LABEL, attempt_count=attempt
    )


# ---------------------------------------------------------------------------
# Scenario: within-window approve resumes exactly once (dedup guarded)
# ---------------------------------------------------------------------------


class TestApproveResumesOnce:
    """Approve resumes the build exactly once; duplicates are deduped."""

    @pytest.mark.asyncio
    async def test_approve_resumes_once_and_emits_build_resumed_on_wire(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        # Duplicate reply with the SAME request_id while the wait is
        # still live — the 300s dedup buffer must drop it (DDR-027:
        # forge-side dedup is the authoritative double-publish guard).
        await nats.deliver_response(
            build_id=BUILD_ID,
            request_id=_request_id(0),
            decision="approve",
        )

        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert state_machine.running == [BUILD_ID]
        assert repo.resumed == [(BUILD_ID, STAGE_LABEL)]
        # The OUTBOUND approval request carries the build's
        # correlation_id (TASK-JNB-101 correlation threading) — this is
        # what jarvis echoes back, giving the correlation guard a real
        # value against live traffic.
        request_envelope = json.loads(nats.published[REQUEST_SUBJECT][0])
        assert request_envelope["correlation_id"] == CORRELATION_ID
        # AC-3 intent: exactly ONE build-resumed envelope on the wire,
        # carrying the real decision + responder (full fidelity — no
        # hardcoded responder, no adapter fabrication).
        resumed = _resumed_payloads(nats)
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "approve"
        assert resumed[0]["responder"] == RICH
        assert resumed[0]["build_id"] == BUILD_ID
        assert resumed[0]["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_resume_envelope_precedes_running_transition(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # FW10-010 ordering contract: observers see the resume on the
        # wire BEFORE the state machine runs PAUSED → RUNNING. The
        # emit is awaited inside the inbound callback; the RUNNING
        # transition can only happen after the wait loop returns.
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        # The envelope is already on the wire, strictly before the
        # gate task has been scheduled again to run the transition.
        assert len(_resumed_payloads(nats)) == 1
        assert state_machine.running == []

        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)
        assert outcome is GateOutcome.RESUMED
        assert state_machine.running == [BUILD_ID]


# ---------------------------------------------------------------------------
# Scenario: override resumes — the second quadrant of the decision gate
# ---------------------------------------------------------------------------


class TestOverrideResumes:
    """Override continues the build AND emits build-resumed (AC-3)."""

    @pytest.mark.asyncio
    async def test_override_emits_build_resumed_with_override_decision(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats,
            request_id=_request_id(0),
            decision="override",
            notes="ship it",
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.OVERRIDDEN
        assert repo.overridden == [(BUILD_ID, STAGE_LABEL, "ship it")]
        assert state_machine.running == [BUILD_ID]
        # The decision gate's approve/override quadrant: override MUST
        # emit build-resumed (pinned so a regression to approve-only
        # cannot pass silently).
        resumed = _resumed_payloads(nats)
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "override"
        assert resumed[0]["responder"] == RICH


# ---------------------------------------------------------------------------
# Scenario: reject cancels — and emits NO build-resumed
# ---------------------------------------------------------------------------


class TestRejectCancels:
    """Reject transitions the build to CANCELLED; zero resume emits."""

    @pytest.mark.asyncio
    async def test_reject_cancels_and_never_emits_build_resumed(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="not safe"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        assert state_machine.cancelled == [(BUILD_ID, "not safe")]
        assert repo.cancelled == [(BUILD_ID, "not safe")]
        # The decision gate on the FW10-010 emit step: a reject MUST NOT
        # publish build-resumed (the phone would render resumed-then-
        # cancelled). Its terminal signal is TASK-JNB-102's
        # build-cancelled — out of scope here.
        assert RESUMED_SUBJECT not in nats.published


# ---------------------------------------------------------------------------
# Scenario: defer republishes with attempt_count + 1 and a fresh request_id
# ---------------------------------------------------------------------------


class TestDeferRepublishWithRefreshedRequestId:
    """Defer re-publishes the request and the next approve resumes."""

    @pytest.mark.asyncio
    async def test_defer_republishes_then_approve_resumes(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="defer")
        # Wait for the republished request (attempt 1), then for the
        # fresh subscription of the recursive await_response.
        await _wait_until(
            lambda: len(nats.published.get(REQUEST_SUBJECT, [])) >= 2,
            what="republished approval request",
        )
        await _drive_response(nats, request_id=_request_id(1), decision="approve")

        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        # The republished envelope carries the REFRESHED request_id
        # (derive_request_id over attempt_count + 1).
        republished = json.loads(nats.published[REQUEST_SUBJECT][1])
        assert republished["payload"]["request_id"] == _request_id(1)
        # The refreshed paused row was recorded before the republish.
        assert [snap.attempt_count for snap in repo.paused] == [0, 1]
        assert repo.paused[1].request_id == _request_id(1)
        # No resume emit fired on the defer itself — exactly one, from
        # the final approve.
        resumed = _resumed_payloads(nats)
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "approve"


# ---------------------------------------------------------------------------
# Scenario: window expiry (no refresh publisher) cancels
# ---------------------------------------------------------------------------


class TestWindowExpiryCancels:
    """Response-window expiry with no refresh → CANCELLED(REASON_MAX_WAIT).

    Production today wires no GateRepository (none exists yet), so the
    subscriber runs single-window waits: the per-attempt window expiring
    without a response returns ``None`` and gate_check applies the
    CANCELLED transition. The window is compressed to zero seconds to
    keep the test wall-clock instant; the code path is identical at 300s.
    """

    @pytest.mark.asyncio
    async def test_window_expiry_produces_cancelled_transition(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(
            nats,
            repo,
            state_machine,
            forge_config=_forge_config(
                default_wait_seconds=0,
                max_wait_seconds=3600,
                expected_approver=RICH,
            ),
        )

        outcome, _ = await asyncio.wait_for(_start_gate(deps), timeout=5.0)

        assert outcome is GateOutcome.TIMED_OUT
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        assert repo.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        assert RESUMED_SUBJECT not in nats.published


# ---------------------------------------------------------------------------
# Scenario: max-wait ceiling breach (refresh wired) cancels
# ---------------------------------------------------------------------------


class TestCeilingBreachCancels:
    """With refresh wired, the total-wait ceiling still cancels the build."""

    @pytest.mark.asyncio
    async def test_ceiling_breach_refreshes_then_cancels(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # The SAME repository backs the pause rows and the refresh
        # lookup — exactly how production will wire it once the SQLite
        # GateRepository adapter exists.
        deps = _production_deps(
            nats,
            repo,
            state_machine,
            forge_config=_forge_config(
                default_wait_seconds=0,
                max_wait_seconds=3,
                expected_approver=RICH,
            ),
            refresh_repository=repo,
            subscriber_clock=_AdvancingClock(step=1.0),
        )

        outcome, _ = await asyncio.wait_for(_start_gate(deps), timeout=5.0)

        assert outcome is GateOutcome.TIMED_OUT
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        # The refresh path ran: republished requests with refreshed,
        # persisted request_ids (API §7) before the ceiling hit.
        assert len(nats.published[REQUEST_SUBJECT]) >= 2
        attempts = [snap.attempt_count for snap in repo.paused]
        assert attempts[0] == 0
        assert attempts[1:] == list(range(1, len(attempts)))
        republished = json.loads(nats.published[REQUEST_SUBJECT][1])
        assert republished["payload"]["request_id"] == _request_id(1)


# ---------------------------------------------------------------------------
# Scenario: spoofed / mismatched replies are refused without transitions
# ---------------------------------------------------------------------------


class TestSpoofedReplyRefused:
    """Wrong decided_by / correlation_id / stale request_id never transition."""

    @pytest.mark.asyncio
    async def test_wrong_decided_by_refused_then_valid_reply_resumes(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats,
            request_id=_request_id(0),
            decision="approve",
            decided_by="mallory",
        )
        # The spoof produced no transition and no emit; the build is
        # still waiting. The responder check runs BEFORE dedup, so the
        # legitimate request_id was not poisoned and still works.
        assert state_machine.running == []
        assert RESUMED_SUBJECT not in nats.published

        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(0), decision="approve"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert state_machine.running == [BUILD_ID]
        assert len(_resumed_payloads(nats)) == 1

    @pytest.mark.asyncio
    async def test_mismatched_correlation_id_refused_as_anomaly(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # Four-step-chain proof: the correlation guard (step 2b) is live
        # because make_gate_check_deps bound expected_correlation_id.
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _wait_until(
            lambda: nats.subscribers.get(MIRROR_SUBJECT),
            what=f"subscriber on {MIRROR_SUBJECT}",
        )
        spoof = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id="a-different-build-context",
            payload={
                "request_id": _request_id(0),
                "decision": "approve",
                "decided_by": RICH,
                "notes": None,
            },
        )
        await nats.publish(MIRROR_SUBJECT, spoof.model_dump_json().encode())

        assert state_machine.running == []
        assert RESUMED_SUBJECT not in nats.published

        # A reply carrying the CORRECT correlation_id is accepted — and
        # the refused one did not consume the request_id (the guard
        # runs before dedup).
        good = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id=CORRELATION_ID,
            payload={
                "request_id": _request_id(0),
                "decision": "approve",
                "decided_by": RICH,
                "notes": None,
            },
        )
        await nats.publish(MIRROR_SUBJECT, good.model_dump_json().encode())
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert len(_resumed_payloads(nats)) == 1

    @pytest.mark.asyncio
    async def test_stale_request_id_after_defer_is_refused(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="defer")
        await _wait_until(
            lambda: len(nats.published.get(REQUEST_SUBJECT, [])) >= 2,
            what="republished approval request",
        )
        await _wait_until(
            lambda: nats.subscribers.get(MIRROR_SUBJECT),
            what="fresh subscription after defer",
        )
        # Replay of the CONSUMED attempt-0 request_id inside the dedup
        # TTL — refused, no transition.
        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(0), decision="approve"
        )
        assert state_machine.running == []
        assert RESUMED_SUBJECT not in nats.published

        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(1), decision="approve"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert state_machine.running == [BUILD_ID]
        assert len(_resumed_payloads(nats)) == 1


# ---------------------------------------------------------------------------
# Scenario: config alignment — the APPROVER_IDENTITY contract, end to end
# ---------------------------------------------------------------------------


class TestConfigAlignment:
    """forge.yaml default → subscriber deps → verbatim decided_by gate."""

    @pytest.mark.asyncio
    async def test_default_config_pins_rich_and_enforces_verbatim_match(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # A ForgeConfig with NO approval block — the pinned default
        # must flow through the factory into the wired deps.
        cfg = _forge_config()
        assert cfg.approval.expected_approver == "rich"

        deps = _production_deps(nats, repo, state_machine, forge_config=cfg)
        gate_task = _start_gate(deps)

        # Case/whitespace variants are NOT equal — the comparison is
        # verbatim (jarvis publishes decided_by untouched; forge
        # compares with ``!=``). "Rich" must be refused.
        await _drive_response(
            nats,
            request_id=_request_id(0),
            decision="approve",
            decided_by="Rich",
        )
        assert state_machine.running == []

        await nats.deliver_response(
            build_id=BUILD_ID,
            request_id=_request_id(0),
            decision="approve",
            decided_by="rich",
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert _resumed_payloads(nats)[0]["responder"] == "rich"
