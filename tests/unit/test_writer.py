"""Unit tests for ``forge.memory.writer`` (TASK-IC-002).

Each test class maps to one or more acceptance criteria from
``tasks/design_approved/TASK-IC-002-fire-and-forget-graphiti-write.md``:

* :class:`TestWriteEntityRedaction`           — AC-001 (every text field is
                                                 redacted before write).
* :class:`TestWriteEntityBackendSelection`    — AC-002 (MCP vs CLI selection
                                                 mirrors the
                                                 ``graphiti-preamble.md``
                                                 3-tier pattern).
* :class:`TestFireAndForgetReturnsSync`       — AC-003 (fire-and-forget
                                                 returns synchronously).
* :class:`TestFireAndForgetSwallowsErrors`    — AC-004 / AC-006 (failures
                                                 are caught, logged, and
                                                 never raised — covers the
                                                 ``@negative
                                                 memory-write-failure-tolerated``
                                                 scenario).
* :class:`TestStructuredLogShape`             — AC-005 (log line carries
                                                 every required field).
* :class:`TestWriteEntityRaisesOnFailure`     — boundary contract for the
                                                 reconcile-backfill caller.
* :class:`TestSeamPipelineHistoryEntityIdContract` — seam test from the
                                                 task spec (verifies the
                                                 ``pipeline_history_entity_id_contract``
                                                 with TASK-IC-001).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

import forge.memory.writer as writer_module
from forge.memory.models import (
    CalibrationAdjustment,
    CalibrationEvent,
    CapabilityResolution,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
)
from forge.memory.writer import (
    GraphitiCLIError,
    GraphitiUnavailableError,
    fire_and_forget_write,
    write_entity,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

#: A canary credential whose redaction is easy to assert against — the
#: 36-char alphanumeric suffix matches the GitHub classic-PAT pattern in
#: ``forge.memory.redaction``.
_CREDENTIAL = "ghp_" + "A" * 36
_REDACTION_MARKER = "***REDACTED-GITHUB-TOKEN***"


def _ts(hour: int = 12) -> datetime:
    """Return a deterministic timezone-aware timestamp."""
    return datetime(2026, 4, 26, hour, 0, 0, tzinfo=UTC)


def _make_gate_decision(
    *, rationale: str = "All criteria met", entity_id: UUID | None = None
) -> GateDecision:
    """Build a valid :class:`GateDecision` for tests."""
    return GateDecision(
        entity_id=entity_id or uuid4(),
        stage_name="planning",
        decided_at=_ts(),
        score=0.92,
        criterion_breakdown={"completeness": 1.0, "correctness": 0.85},
        rationale=rationale,
    )


def _make_override_event(*, rationale: str = "operator override") -> OverrideEvent:
    return OverrideEvent(
        entity_id=uuid4(),
        gate_decision_id=uuid4(),
        original_recommendation="proceed",
        operator_decision="halt",
        operator_rationale=rationale,
        decided_at=_ts(),
    )


class _RecordingDispatcher:
    """Drop-in replacement for ``writer._dispatch_write`` that records calls.

    Used by tests that need to assert on the *payload* the writer would
    have sent, without going near a real Graphiti backend.
    """

    def __init__(self, *, raise_with: BaseException | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], str, str]] = []
        self._raise_with = raise_with

    async def __call__(
        self, payload: dict[str, Any], group_id: str, episode_name: str
    ) -> None:
        self.calls.append((payload, group_id, episode_name))
        if self._raise_with is not None:
            raise self._raise_with


# ---------------------------------------------------------------------------
# AC-001 — redaction is applied to every text field before write
# ---------------------------------------------------------------------------


class TestWriteEntityRedaction:
    """``write_entity`` redacts every text field before dispatch (AC-001)."""

    def test_credential_in_rationale_is_redacted_before_dispatch(self) -> None:
        """A GateDecision rationale containing a PAT is scrubbed."""
        recorder = _RecordingDispatcher()
        gate = _make_gate_decision(
            rationale=f"Approved with token {_CREDENTIAL} — see thread."
        )

        with patch.object(writer_module, "_dispatch_write", recorder):
            asyncio.run(write_entity(gate, "forge_pipeline_history"))

        assert len(recorder.calls) == 1
        payload, group_id, _ = recorder.calls[0]
        assert group_id == "forge_pipeline_history"
        assert _CREDENTIAL not in payload["rationale"]
        assert _REDACTION_MARKER in payload["rationale"]

    def test_credential_in_override_operator_rationale_is_redacted(self) -> None:
        """OverrideEvent.operator_rationale is also scrubbed."""
        recorder = _RecordingDispatcher()
        override = _make_override_event(
            rationale=f"Override: {_CREDENTIAL} not authorised"
        )

        with patch.object(writer_module, "_dispatch_write", recorder):
            asyncio.run(write_entity(override, "forge_pipeline_history"))

        payload = recorder.calls[0][0]
        assert _CREDENTIAL not in payload["operator_rationale"]
        assert _REDACTION_MARKER in payload["operator_rationale"]

    def test_calibration_event_question_and_answer_are_redacted(self) -> None:
        """Every str field on CalibrationEvent — including nested ones — is scrubbed."""
        recorder = _RecordingDispatcher()
        cal = CalibrationEvent(
            entity_id="sha256:abc123",
            source_file="docs/calibration.md",
            question=f"What about {_CREDENTIAL}?",
            answer=f"Use Bearer {'X' * 30}",
            captured_at=_ts(),
            partial=False,
        )

        with patch.object(writer_module, "_dispatch_write", recorder):
            asyncio.run(write_entity(cal, "forge_calibration_history"))

        payload = recorder.calls[0][0]
        assert _CREDENTIAL not in payload["question"]
        # Bearer token is replaced by the bearer redaction marker.
        assert payload["answer"].startswith("Use Bearer ***REDACTED***")

    def test_redaction_is_applied_recursively_to_nested_dicts(self) -> None:
        """Nested str values (criterion_breakdown is dict[str,float]) are visited.

        ``criterion_breakdown`` keys are strings — although they are
        unlikely to contain credentials, the recursion guarantees that
        any future free-text field nested in a ``dict`` or ``list``
        would still be scrubbed without changes here.
        """
        from forge.memory.writer import _redact_payload

        nested = {
            "outer_text": f"contains {_CREDENTIAL}",
            "inner": {"deep_text": f"also contains {_CREDENTIAL}"},
            "items": [f"list item with {_CREDENTIAL}", "clean"],
            "non_string": 42,
        }
        scrubbed = _redact_payload(nested)
        assert _CREDENTIAL not in scrubbed["outer_text"]
        assert _CREDENTIAL not in scrubbed["inner"]["deep_text"]
        assert _CREDENTIAL not in scrubbed["items"][0]
        assert scrubbed["items"][1] == "clean"
        assert scrubbed["non_string"] == 42


# ---------------------------------------------------------------------------
# AC-002 — MCP vs CLI selection mirrors the 3-tier preamble
# ---------------------------------------------------------------------------


class TestWriteEntityBackendSelection:
    """``_dispatch_write`` selects MCP first, then CLI, then errors (AC-002)."""

    def test_mcp_backend_chosen_when_available(self) -> None:
        """If graphiti_core is importable, the MCP path is taken."""
        called: dict[str, Any] = {}

        async def fake_mcp(
            payload: dict[str, Any], group_id: str, episode_name: str
        ) -> None:
            called["backend"] = "mcp"
            called["payload"] = payload
            called["group_id"] = group_id

        async def fake_cli(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            called["backend"] = "cli"

        with (
            patch.object(writer_module, "_mcp_backend_available", return_value=True),
            patch.object(writer_module, "_cli_backend_available", return_value=True),
            patch.object(writer_module, "_write_via_mcp", fake_mcp),
            patch.object(writer_module, "_write_via_cli", fake_cli),
        ):
            asyncio.run(
                write_entity(_make_gate_decision(), "forge_pipeline_history")
            )

        assert called["backend"] == "mcp"
        assert called["group_id"] == "forge_pipeline_history"

    def test_cli_backend_chosen_when_mcp_unavailable(self) -> None:
        """If graphiti_core is missing but guardkit is on PATH, CLI fires."""
        called: dict[str, Any] = {}

        async def fake_cli(
            payload: dict[str, Any], group_id: str, episode_name: str
        ) -> None:
            called["backend"] = "cli"
            called["episode_name"] = episode_name

        with (
            patch.object(writer_module, "_mcp_backend_available", return_value=False),
            patch.object(writer_module, "_cli_backend_available", return_value=True),
            patch.object(writer_module, "_write_via_cli", fake_cli),
        ):
            asyncio.run(
                write_entity(_make_gate_decision(), "forge_pipeline_history")
            )

        assert called["backend"] == "cli"
        # Episode name follows the documented "<EntityType>:<entity_id>" shape.
        assert called["episode_name"].startswith("GateDecision:")

    def test_unavailable_raises_when_neither_backend_present(self) -> None:
        """Both tiers absent → GraphitiUnavailableError surfaces."""
        with (
            patch.object(writer_module, "_mcp_backend_available", return_value=False),
            patch.object(writer_module, "_cli_backend_available", return_value=False),
        ):
            with pytest.raises(GraphitiUnavailableError):
                asyncio.run(
                    write_entity(
                        _make_gate_decision(), "forge_pipeline_history"
                    )
                )

    def test_cli_non_zero_exit_raises_graphiti_cli_error(self) -> None:
        """The CLI tier wraps non-zero exit codes in GraphitiCLIError."""

        class _FakeProc:
            returncode = 2

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"server unreachable"

        async def fake_create(*_a: Any, **_kw: Any) -> _FakeProc:  # noqa: ANN401
            return _FakeProc()

        with (
            patch.object(writer_module, "_mcp_backend_available", return_value=False),
            patch.object(writer_module, "_cli_backend_available", return_value=True),
            patch(
                "asyncio.create_subprocess_exec", side_effect=fake_create
            ),
        ):
            with pytest.raises(GraphitiCLIError) as ei:
                asyncio.run(
                    write_entity(
                        _make_gate_decision(), "forge_pipeline_history"
                    )
                )
        assert "server unreachable" in str(ei.value)


# ---------------------------------------------------------------------------
# AC-003 — fire-and-forget returns synchronously
# ---------------------------------------------------------------------------


class TestFireAndForgetReturnsSync:
    """``fire_and_forget_write`` returns immediately without awaiting (AC-003)."""

    def test_returns_immediately_when_loop_running(self) -> None:
        """Inside a loop, the write is scheduled and the call returns ASAP."""
        scheduled: list[float] = []

        async def slow_write(payload: dict[str, Any], group_id: str, name: str) -> None:
            scheduled.append(time.monotonic())
            await asyncio.sleep(0.05)

        async def driver() -> float:
            with patch.object(writer_module, "_dispatch_write", slow_write):
                start = time.monotonic()
                fire_and_forget_write(
                    _make_gate_decision(), "forge_pipeline_history"
                )
                elapsed = time.monotonic() - start
                # Yield control so the scheduled task actually runs.
                await asyncio.sleep(0.1)
            return elapsed

        elapsed = asyncio.run(driver())
        # Returning synchronously means we should be well under the
        # 50ms slow_write — the overhead of scheduling alone is sub-ms.
        assert elapsed < 0.02, (
            f"fire_and_forget_write blocked for {elapsed:.4f}s — "
            "expected non-blocking dispatch."
        )
        assert len(scheduled) == 1, "scheduled coroutine never ran"

    def test_returns_immediately_in_sync_context_via_thread(self) -> None:
        """Outside a loop, the write runs in a background daemon thread."""
        completed = threading.Event()

        async def fake_dispatch(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            completed.set()

        # No running loop in the current thread — exercises the
        # thread-pool branch.
        with patch.object(writer_module, "_dispatch_write", fake_dispatch):
            start = time.monotonic()
            fire_and_forget_write(
                _make_gate_decision(), "forge_pipeline_history"
            )
            elapsed = time.monotonic() - start

            # The call must return synchronously, well before the
            # background thread has had a chance to import asyncio
            # and run the coroutine. A 200ms wait then confirms the
            # background thread *did* run.
            assert elapsed < 0.05
            assert completed.wait(timeout=2.0), (
                "background dispatch never completed"
            )


# ---------------------------------------------------------------------------
# AC-004 / AC-006 — failures are caught and never raised
# ---------------------------------------------------------------------------


class TestFireAndForgetSwallowsErrors:
    """Underlying-write failures never propagate (AC-004, AC-006)."""

    def test_dispatch_failure_in_loop_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """RuntimeError from dispatch is swallowed and logged."""

        async def failing(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            raise RuntimeError("simulated graphiti outage")

        async def driver() -> None:
            with patch.object(writer_module, "_dispatch_write", failing):
                # The call itself must not raise.
                fire_and_forget_write(
                    _make_gate_decision(), "forge_pipeline_history"
                )
                # Yield control so the done-callback runs.
                await asyncio.sleep(0.05)

        caplog.set_level(logging.ERROR, logger="forge.memory.writer")
        # No exception should escape ``asyncio.run``.
        asyncio.run(driver())

        assert any(
            "graphiti_fire_and_forget_write_failed" in rec.message
            for rec in caplog.records
        ), "expected structured failure log line"

    def test_dispatch_failure_in_sync_thread_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same guarantee applies to the background-thread path."""
        done = threading.Event()

        async def failing(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            try:
                raise RuntimeError("simulated outage in sync path")
            finally:
                done.set()

        caplog.set_level(logging.ERROR, logger="forge.memory.writer")
        with patch.object(writer_module, "_dispatch_write", failing):
            fire_and_forget_write(
                _make_gate_decision(), "forge_pipeline_history"
            )
            assert done.wait(2.0)
            # Give the logger a moment to flush from the daemon thread.
            time.sleep(0.05)

        assert any(
            "graphiti_fire_and_forget_write_failed" in rec.message
            for rec in caplog.records
        )

    def test_negative_memory_write_failure_tolerated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``@negative memory-write-failure-tolerated``: outage does not abort.

        Simulates a complete Graphiti outage by patching both backend
        availability checks to return False. The fire-and-forget call
        must still return without raising, and the caller-side code
        path (``post_write_step``) must execute.
        """
        post_write_executed = False

        with (
            patch.object(writer_module, "_mcp_backend_available", return_value=False),
            patch.object(writer_module, "_cli_backend_available", return_value=False),
        ):
            caplog.set_level(logging.ERROR, logger="forge.memory.writer")

            async def driver() -> bool:
                fire_and_forget_write(
                    _make_gate_decision(), "forge_pipeline_history"
                )
                # Allow the scheduled future to settle.
                await asyncio.sleep(0.05)
                return True  # simulates "the pipeline kept going"

            post_write_executed = asyncio.run(driver())

        assert post_write_executed, "pipeline aborted on memory write failure"
        assert any(
            "GraphitiUnavailableError" in (rec.message + str(rec.__dict__))
            or rec.exc_info is not None
            for rec in caplog.records
        ), "expected the outage to be logged"


# ---------------------------------------------------------------------------
# AC-005 — structured log line carries every required field
# ---------------------------------------------------------------------------


class TestStructuredLogShape:
    """The failure log line carries every field downstream alerting needs (AC-005)."""

    def test_log_record_extra_contains_all_required_keys(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``entity_id``, ``group_id``, ``entity_type``, ``error_class``, ``error_message`` present."""
        gate = _make_gate_decision()

        async def failing(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            raise ValueError("synthetic")

        async def driver() -> None:
            with patch.object(writer_module, "_dispatch_write", failing):
                fire_and_forget_write(gate, "forge_pipeline_history")
                await asyncio.sleep(0.05)

        caplog.set_level(logging.ERROR, logger="forge.memory.writer")
        asyncio.run(driver())

        failure_records = [
            rec
            for rec in caplog.records
            if rec.message == "graphiti_fire_and_forget_write_failed"
        ]
        assert failure_records, "no failure record emitted"
        rec = failure_records[0]

        # ``logger.error(..., extra={...})`` flattens extras onto the
        # LogRecord itself.
        assert getattr(rec, "entity_id", None) == str(gate.entity_id)
        assert getattr(rec, "group_id", None) == "forge_pipeline_history"
        assert getattr(rec, "entity_type", None) == "GateDecision"
        assert getattr(rec, "error_class", None) == "ValueError"
        assert getattr(rec, "error_message", None) == "synthetic"
        # Traceback must be attached so log aggregators with stack
        # rendering can surface it.
        assert rec.exc_info is not None


# ---------------------------------------------------------------------------
# write_entity raises on failure (boundary contract for reconcile-backfill)
# ---------------------------------------------------------------------------


class TestWriteEntityRaisesOnFailure:
    """``write_entity`` propagates errors so the reconcile caller sees them."""

    def test_dispatch_error_propagates_to_caller(self) -> None:
        """Errors from the backend are not swallowed at this layer."""
        boom = RuntimeError("boom")

        async def failing(*_a: Any, **_kw: Any) -> None:  # noqa: ANN401
            raise boom

        with patch.object(writer_module, "_dispatch_write", failing):
            with pytest.raises(RuntimeError) as ei:
                asyncio.run(
                    write_entity(
                        _make_gate_decision(), "forge_pipeline_history"
                    )
                )
        assert ei.value is boom

    def test_rejects_non_basemodel_entity(self) -> None:
        """Type-guarding the boundary input — TypeError on bad entity."""
        with pytest.raises(TypeError):
            asyncio.run(write_entity({"not": "an entity"}, "g"))  # type: ignore[arg-type]

    def test_rejects_empty_group_id(self) -> None:
        """Empty / non-string group_id is rejected."""
        with pytest.raises(ValueError):
            asyncio.run(write_entity(_make_gate_decision(), ""))


# ---------------------------------------------------------------------------
# Seam test — integration contract with TASK-IC-001 producers
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("pipeline_history_entity_id_contract")
class TestSeamPipelineHistoryEntityIdContract:
    """Verify the entity_id contract this consumer composes against.

    Producer: TASK-IC-001 / ``forge.memory.models``.

    Contract: ``entity_id`` MUST equal the SQLite-row UUID for the five
    typed pipeline-history entities; ``CalibrationEvent`` uses a
    deterministic hash str.
    """

    def test_pipeline_history_entity_id_format(self) -> None:
        """Pipeline-history entity_ids are typed UUIDs sourced from SQLite."""
        sqlite_uuid = uuid4()
        gate = GateDecision(
            entity_id=sqlite_uuid,
            stage_name="planning",
            decided_at=_ts(),
            score=0.9,
            criterion_breakdown={},
            rationale="seam",
        )
        assert gate.entity_id == sqlite_uuid, (
            "GateDecision.entity_id must equal the SQLite row UUID, "
            "not be regenerated"
        )
        assert isinstance(gate.entity_id, UUID), (
            "Pipeline-history entity_ids are typed UUID, not str"
        )

    def test_calibration_event_entity_id_is_deterministic_str(self) -> None:
        """CalibrationEvent uses a deterministic str hash, NOT a UUID."""
        cal = CalibrationEvent(
            entity_id="sha256:abc123",
            source_file="docs/calibration.md",
            question="q",
            answer="a",
            captured_at=_ts(),
            partial=False,
        )
        assert isinstance(cal.entity_id, str), (
            "CalibrationEvent.entity_id is a deterministic hash str, "
            "not a UUID"
        )

    def test_writer_accepts_every_pipeline_history_entity_type(self) -> None:
        """Every TASK-IC-001 entity must round-trip through ``write_entity``.

        The seam this test pins down is the *type* surface:
        :data:`forge.memory.writer.PipelineHistoryEntity` MUST cover
        every entity TASK-IC-001 declares. A future producer-side
        addition without a matching consumer-side update would fail
        here at construction time.
        """
        recorder = _RecordingDispatcher()
        sqlite_uuid = uuid4()
        gate = _make_gate_decision(entity_id=sqlite_uuid)
        cap_res = CapabilityResolution(
            entity_id=uuid4(),
            agent_id="agent-1",
            capability="codegen",
            selected_at=_ts(),
            discovery_cache_version="v1",
        )
        override = _make_override_event()
        cal_adj = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="threshold",
            old_value="0.5",
            new_value="0.6",
            approved=True,
            proposed_at=_ts(),
            expires_at=_ts(hour=23),
        )
        session = SessionOutcome(
            entity_id=uuid4(),
            build_id="build-1",
            outcome="success",
            gate_decision_ids=[sqlite_uuid],
            closed_at=_ts(),
        )
        cal_event = CalibrationEvent(
            entity_id="sha256:xyz",
            source_file="docs/x.md",
            question="q",
            answer="a",
            captured_at=_ts(),
            partial=False,
        )

        with patch.object(writer_module, "_dispatch_write", recorder):
            for entity in (gate, cap_res, override, cal_adj, session, cal_event):
                asyncio.run(write_entity(entity, "forge_pipeline_history"))

        assert len(recorder.calls) == 6
        # Each recorded payload must be a dict — the redaction
        # invariant. The producer surface is therefore intact.
        for payload, _, _ in recorder.calls:
            assert isinstance(payload, dict)
            assert "entity_id" in payload


# ---------------------------------------------------------------------------
# Misc invariants — entity_id stable across redaction (defence-in-depth)
# ---------------------------------------------------------------------------


class TestRedactionDoesNotMangleStructuralFields:
    """The redaction pass MUST NOT alter UUIDs, datetimes, or numeric fields."""

    def test_entity_id_uuid_survives_redaction(self) -> None:
        """A UUID's hex form is 36 chars — safely below the 40-char hex floor."""
        recorder = _RecordingDispatcher()
        gate = _make_gate_decision()

        with patch.object(writer_module, "_dispatch_write", recorder):
            asyncio.run(write_entity(gate, "forge_pipeline_history"))

        payload = recorder.calls[0][0]
        assert payload["entity_id"] == str(gate.entity_id)
        assert UUID(payload["entity_id"]) == gate.entity_id

    def test_score_and_breakdown_values_preserved(self) -> None:
        """Numeric fields untouched by redaction."""
        recorder = _RecordingDispatcher()
        gate = _make_gate_decision()

        with patch.object(writer_module, "_dispatch_write", recorder):
            asyncio.run(write_entity(gate, "forge_pipeline_history"))

        payload = recorder.calls[0][0]
        assert payload["score"] == pytest.approx(0.92)
        assert payload["criterion_breakdown"]["completeness"] == pytest.approx(1.0)
