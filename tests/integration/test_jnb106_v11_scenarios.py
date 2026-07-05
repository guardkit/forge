"""TASK-JNB-106 — the seven FEAT-UBS-003 v1.1 scenarios, production-wired.

Window/expiry enforcement is exclusively forge-side, so this suite is the
single authoritative validation of window semantics for the entire
notification bridge. Every scenario drives
:func:`forge.gating.wrappers.gate_check` through the TASK-JNB-101
production factory over the in-memory NATS double — the production-wired
chain, never re-mocked internals. The subscriber binds AGENTS-stream
subjects via core-NATS subscribe (no JetStream consumer is created or
faked — the err-10100 single-consumer rule concerns the PIPELINE stream
only and is untouched by this suite).

Scenario classes map 1:1 to the spec counterparts enumerated in the task
file (one named test per scenario; the collect-only guard at the bottom
pins all seven so a refactor cannot silently drop one):

1. ``TestWithinWindowApproveResumes``
2. ``TestAfterWindowReplyNotApplied``
3. ``TestUnrecognisedDecisionRefused``
4. ``TestWrongCorrelationIdRefused``
5. ``TestDuplicateResponseDeduped``
6. ``TestReplyAfterTerminalIgnored``
7. ``TestApproveVsExpiryRace``

Note on scenario 1's "(mark_resume_pending invoked)" AC wording: that
mechanism was replaced by the recorded TASK-JNB-101 AC-3 deviation (the
adapter path is dead in production and its guard is broken for the
restart case); the equivalent assertion here is the wire-level
``build-resumed`` emit — exactly once, with real decision/responder.

DDR-027: no test simulates dedup or pending state surviving a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from typing import Any

import pytest

from forge.gating.wrappers import REASON_MAX_WAIT, GateOutcome

from .conftest import (
    BUILD_ID,
    RICH,
    InMemoryNats,
    InMemoryRepository,
    InMemoryStateMachine,
)
from .test_jnb101_production_wiring import (
    CORRELATION_ID,
    MIRROR_SUBJECT,
    RESUMED_SUBJECT,
    _drive_response,
    _forge_config,
    _production_deps,
    _request_id,
    _start_gate,
    _wait_until,
)
from nats_core.envelope import EventType, MessageEnvelope

#: The seven scenario test ids the collect-only guard pins (task file
#: Test Requirements: "a refactor cannot silently drop a scenario while
#: the run stays green").
SCENARIO_TEST_NAMES = (
    "test_scenario_1_within_window_approve_resumes",
    "test_scenario_2_after_window_reply_not_applied",
    "test_scenario_3_unrecognised_decision_refused_and_logged",
    "test_scenario_4_wrong_correlation_id_refused_as_anomaly",
    "test_scenario_5_duplicate_response_request_id_deduped",
    "test_scenario_6_reply_after_terminal_state_ignored",
    "test_scenario_7_approve_vs_expiry_race_single_outcome",
)


def _resumed_payloads(nats: InMemoryNats) -> list[dict[str, Any]]:
    return [
        json.loads(body)["payload"] for body in nats.published.get(RESUMED_SUBJECT, [])
    ]


def _outcome_count(nats: InMemoryNats, state_machine: InMemoryStateMachine) -> int:
    """Count recorded terminal-ish outcomes for the single-locus checks."""
    return len(state_machine.running) + len(state_machine.cancelled)


class TestWithinWindowApproveResumes:
    """Scenario 1 — a valid approve inside the wait window resumes."""

    @pytest.mark.asyncio
    async def test_scenario_1_within_window_approve_resumes(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        # Exactly one approved outcome recorded, state + wire agreeing
        # (the deviation-recorded equivalent of "mark_resume_pending
        # invoked": one build-resumed envelope, real responder).
        assert state_machine.running == [BUILD_ID]
        assert state_machine.cancelled == []
        assert repo.resumed == [(BUILD_ID, "Implementation")]
        resumed = _resumed_payloads(nats)
        assert len(resumed) == 1
        assert resumed[0]["responder"] == RICH


class TestAfterWindowReplyNotApplied:
    """Scenario 2 — a reply arriving after expiry is not applied."""

    @pytest.mark.asyncio
    async def test_scenario_2_after_window_reply_not_applied(
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

        # The window has expired and the wait loop has torn down its
        # subscription. A perfectly valid approve arriving late must
        # not be applied — the expiry outcome stands.
        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(0), decision="approve"
        )

        assert state_machine.running == []
        assert repo.resumed == []
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        assert RESUMED_SUBJECT not in nats.published


class TestUnrecognisedDecisionRefused:
    """Scenario 3 — an unknown decision is refused and logged."""

    @pytest.mark.asyncio
    async def test_scenario_3_unrecognised_decision_refused_and_logged(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        with caplog.at_level(logging.WARNING):
            await _drive_response(nats, request_id=_request_id(0), decision="maybe")
        # Refused at step 1 (payload validation): no state transition,
        # a WARNING names the unrecognised decision, and the pause
        # survives — a correctly-formed response still resumes.
        assert state_machine.running == []
        assert state_machine.cancelled == []
        assert any(
            "invalid payload" in r.message and "maybe" in r.getMessage()
            for r in caplog.records
        )

        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(0), decision="approve"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)
        assert outcome is GateOutcome.RESUMED


class TestWrongCorrelationIdRefused:
    """Scenario 4 — a mismatched correlation_id is refused as an anomaly."""

    @pytest.mark.asyncio
    async def test_scenario_4_wrong_correlation_id_refused_as_anomaly(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _wait_until(
            lambda: nats.subscribers.get(MIRROR_SUBJECT),
            what=f"subscriber on {MIRROR_SUBJECT}",
        )
        spoof = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id="someone-elses-build-context",
            payload={
                "request_id": _request_id(0),
                "decision": "approve",
                "decided_by": RICH,
                "notes": None,
            },
        )
        with caplog.at_level(logging.WARNING):
            await nats.publish(MIRROR_SUBJECT, spoof.model_dump_json().encode())

        assert state_machine.running == []
        assert any(
            "correlation_id mismatch" in r.message and "anomaly" in r.message
            for r in caplog.records
        )

        # The anomaly did not consume the request_id (guard runs before
        # dedup) — the legitimate reply still resolves the pause.
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


class TestDuplicateResponseDeduped:
    """Scenario 5 — same request_id inside the dedup horizon is ignored."""

    @pytest.mark.asyncio
    async def test_scenario_5_duplicate_response_request_id_deduped(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        with caplog.at_level(logging.INFO):
            await nats.deliver_response(
                build_id=BUILD_ID,
                request_id=_request_id(0),
                decision="approve",
            )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        # Exactly one recorded outcome — the duplicate was observed and
        # discarded (DDR-027: this in-memory dedup is the authoritative
        # double-publish guard for the whole bridge).
        assert state_machine.running == [BUILD_ID]
        assert repo.resumed == [(BUILD_ID, "Implementation")]
        assert len(_resumed_payloads(nats)) == 1
        assert any("duplicate response" in r.message for r in caplog.records)


class TestReplyAfterTerminalIgnored:
    """Scenario 6 — a reply after the terminal state is ignored."""

    @pytest.mark.asyncio
    async def test_scenario_6_reply_after_terminal_state_ignored(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="stop"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)
        assert outcome is GateOutcome.CANCELLED
        assert state_machine.cancelled == [(BUILD_ID, "stop")]

        # Build is terminal; a fresh, otherwise-valid approve (new
        # request_id — NOT a dedup case) arrives on the mirror subject.
        # Ignored without error and without state change.
        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(1), decision="approve"
        )

        assert state_machine.running == []
        assert repo.resumed == []
        assert state_machine.cancelled == [(BUILD_ID, "stop")]
        assert RESUMED_SUBJECT not in nats.published


class TestApproveVsExpiryRace:
    """Scenario 7 — the race resolves in one place to exactly one outcome.

    Window enforcement is exclusively forge-side; this is the sole
    authoritative coverage of the reply-vs-expiry race anywhere in the
    bridge. Interleaving is controlled explicitly through event-loop
    ordering (never sleeps): under the synchronous in-memory transport,
    a response is either enqueued before the wait loop's timeout branch
    runs to completion — approve wins — or after the loop has torn down
    (the whole expiry path runs without yielding) — expiry wins. Both
    reachable interleavings are pinned; in each, EXACTLY ONE outcome is
    recorded, and state and wire agree.
    """

    @pytest.mark.asyncio
    async def test_scenario_7_approve_vs_expiry_race_single_outcome(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # Leg A — approve wins: the reply lands inside the (1s) window.
        deps = _production_deps(nats, repo, state_machine)
        deps.per_attempt_wait_seconds = 1
        gate_task = _start_gate(deps)
        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert _outcome_count(nats, state_machine) == 1
        assert state_machine.cancelled == []
        assert len(_resumed_payloads(nats)) == 1

    @pytest.mark.asyncio
    async def test_scenario_7_expiry_wins_leg_records_exactly_one_outcome(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # Leg B — expiry wins: nothing is delivered until the window
        # (zero seconds, no refresh publisher) has fully resolved; the
        # late approve must not create a second outcome.
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

        await nats.deliver_response(
            build_id=BUILD_ID, request_id=_request_id(0), decision="approve"
        )

        assert _outcome_count(nats, state_machine) == 1
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        assert state_machine.running == []
        assert RESUMED_SUBJECT not in nats.published


class TestScenarioCollectionGuard:
    """Collect-only count assertion (task file Test Requirements)."""

    def test_collect_only_yields_all_seven_scenario_tests(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", __file__, "--collect-only", "-q"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for name in SCENARIO_TEST_NAMES:
            assert name in result.stdout, (
                f"scenario test {name!r} missing from collection — a "
                "refactor silently dropped a required scenario"
            )
