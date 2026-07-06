"""Tests for frontier second opinion provider (TASK-MP-007)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from forge.gating.models import GateMode
from forge.planning.frontier import (
    FrontierSecondOpinion,
    build_compressed_brief,
)


class RecordingFrontierClient:
    """Recording fake FrontierClient for tests."""

    def __init__(self, *, should_fail: bool = False, timeout: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.should_fail = should_fail
        self.timeout = timeout

    async def get_opinion(self, brief: dict[str, Any]) -> dict[str, Any]:
        """Record the call and optionally fail."""
        self.calls.append(brief)

        if self.timeout:
            await asyncio.sleep(100)  # Will be cancelled by timeout

        if self.should_fail:
            raise RuntimeError("Frontier service unavailable")

        return {
            "model_used": "frontier-test-model",
            "opinion_text": "Test opinion",
            "confidence": 0.85,
        }


@pytest.mark.asyncio
class TestFrontierSecondOpinion:
    """Tests for FrontierSecondOpinion provider (TASK-MP-007)."""

    async def test_frontier_disabled_zero_calls(self) -> None:
        """AC-001: frontier_enabled=False -> zero calls to client."""
        client = RecordingFrontierClient()
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=False,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-001",
            stage_label="product_docs",
        )

        # Should return compressed brief but NOT call frontier
        assert len(client.calls) == 0
        assert "docs_summary" in result
        assert "second_opinion" not in result

    async def test_non_flagged_outcome_zero_calls(self) -> None:
        """AC-002: Enabled + non-flagged outcome -> zero calls."""
        client = RecordingFrontierClient()
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.AUTO_APPROVE,  # Not FLAG
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-002",
            stage_label="product_docs",
        )

        # Should return compressed brief but NOT call frontier (not flagged)
        assert len(client.calls) == 0
        assert "docs_summary" in result
        assert "second_opinion" not in result

    async def test_brief_contains_only_allowlisted_keys(self) -> None:
        """AC-003: Brief contains only allowlisted keys, no raw conversation."""
        client = RecordingFrontierClient()
        po_output = {
            "docs_summary": "Feature X summary",
            "assumptions": ["ASSUM-001", "ASSUM-002"],
            "coach_evidence": {"coach_score": 0.9},
            "structured_findings": {"criteria": ["AC-001"]},
            # Forbidden keys that should be filtered out
            "transcript": "raw conversation data",
            "messages": ["user: hello", "assistant: hi"],
            "request_text": "original request",
            "raw_conversation": "full chat log",
        }

        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: po_output,
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        await provider.get_summary_for_approval(
            plan_run_id="plan-test-003",
            stage_label="product_docs",
        )

        # Check that the brief passed to client has no forbidden keys
        assert len(client.calls) == 1
        brief = client.calls[0]

        # Allowed keys should be present
        assert "docs_summary" in brief
        assert "assumptions" in brief
        assert "coach_evidence" in brief
        assert "structured_findings" in brief

        # Forbidden keys should NOT be present
        assert "transcript" not in brief
        assert "messages" not in brief
        assert "request_text" not in brief
        assert "raw_conversation" not in brief

    async def test_client_error_degrades_to_human(self) -> None:
        """AC-004: Client raising -> degrade to human review with note."""
        client = RecordingFrontierClient(should_fail=True)
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-004",
            stage_label="product_docs",
        )

        # Should still return valid result with unavailable note
        assert "docs_summary" in result
        assert "second_opinion_unavailable" in result
        assert result["second_opinion_unavailable"] is True
        assert "second_opinion_error" in result
        assert "Frontier service unavailable" in result["second_opinion_error"]

    async def test_client_timeout_degrades_to_human(self) -> None:
        """AC-004: Client timeout -> degrade to human review with note."""
        client = RecordingFrontierClient(timeout=True)
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=1,  # Very short timeout
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-005",
            stage_label="product_docs",
        )

        # Should still return valid result with timeout note
        assert "docs_summary" in result
        assert "second_opinion_unavailable" in result
        assert result["second_opinion_unavailable"] is True
        assert "second_opinion_error" in result
        assert "timeout" in result["second_opinion_error"].lower()

    async def test_provider_returns_opinion_data_only(self) -> None:
        """AC-005: Return type carries opinion data only, no approve/decision field."""
        client = RecordingFrontierClient()
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-006",
            stage_label="product_docs",
        )

        # Should return data only, never an approval decision
        assert "docs_summary" in result
        assert "second_opinion" in result

        # These fields MUST NOT exist (type-level never-approve)
        assert "approved" not in result
        assert "decision" not in result
        assert "auto_approve" not in result

    async def test_successful_opinion_attached(self) -> None:
        """Test successful frontier opinion is attached to summary."""
        client = RecordingFrontierClient()
        provider = FrontierSecondOpinion(
            client=client,
            frontier_enabled=True,
            frontier_timeout_seconds=30,
            get_po_output=lambda run_id: {"docs_summary": "test"},
            get_gate_decision=lambda run_id: GateMode.FLAG_FOR_REVIEW,
        )

        result = await provider.get_summary_for_approval(
            plan_run_id="plan-test-007",
            stage_label="product_docs",
        )

        # Should include the opinion data
        assert "second_opinion" in result
        opinion = result["second_opinion"]
        assert opinion["model_used"] == "frontier-test-model"
        assert opinion["opinion_text"] == "Test opinion"
        assert opinion["confidence"] == 0.85


class TestBuildCompressedBrief:
    """Tests for build_compressed_brief field allowlist (TASK-MP-007)."""

    def test_allowlisted_keys_preserved(self) -> None:
        """Test that allowlisted keys are preserved in the brief."""
        po_output = {
            "docs_summary": "Summary text",
            "assumptions": ["ASSUM-001"],
            "coach_evidence": {"score": 0.8},
            "structured_findings": {"criteria": []},
        }

        brief = build_compressed_brief(po_output)

        assert brief["docs_summary"] == "Summary text"
        assert brief["assumptions"] == ["ASSUM-001"]
        assert brief["coach_evidence"] == {"score": 0.8}
        assert brief["structured_findings"] == {"criteria": []}

    def test_forbidden_keys_filtered(self) -> None:
        """Test that forbidden keys are filtered from the brief."""
        po_output = {
            "docs_summary": "Summary",
            "transcript": "forbidden",
            "messages": ["forbidden"],
            "request_text": "forbidden",
            "raw_conversation": "forbidden",
            "extra_field": "should be ignored",
        }

        brief = build_compressed_brief(po_output)

        # Only allowlisted key should be present
        assert "docs_summary" in brief
        assert "transcript" not in brief
        assert "messages" not in brief
        assert "request_text" not in brief
        assert "raw_conversation" not in brief
        assert "extra_field" not in brief

    def test_missing_keys_handled_gracefully(self) -> None:
        """Test that missing allowlisted keys don't cause errors."""
        po_output = {
            "docs_summary": "Summary only",
        }

        brief = build_compressed_brief(po_output)

        # Should only have the key that was present
        assert brief["docs_summary"] == "Summary only"
        assert len(brief) == 1

    def test_empty_po_output(self) -> None:
        """Test that empty PO output returns empty brief."""
        po_output: dict[str, Any] = {}

        brief = build_compressed_brief(po_output)

        assert brief == {}
