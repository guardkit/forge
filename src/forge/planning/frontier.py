"""Frontier second opinion provider (TASK-MP-007).

Implements the config-gated escalation subcontractor for DF-006 frontier
second opinion. Fires ONLY on FLAG_FOR_REVIEW outcomes and sends compressed,
policy-filtered structured JSON (never raw conversation). Unreachable frontier
degrades to forced human review.

Key design decisions:
--------------------
* **FLAG-only predicate**: Client called only when gate mode is FLAG_FOR_REVIEW
* **Policy-filtered brief**: Only allowlisted keys passed to client (DF-009 §2.3)
* **Degrade-to-human**: Client errors/timeouts attach "unavailable" note
* **Never approve**: Provider returns data only, structurally cannot approve

References:
-----------
- TASK-MP-007 — this task brief
- TASK-MP-004B — SecondOpinionProvider protocol
- DF-006 — frontier second opinion design
- DF-009 §2.3 — field allowlist specification
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol

from forge.gating.models import GateMode

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

__all__ = ["FrontierClient", "FrontierSecondOpinion", "build_compressed_brief"]

# Field allowlist per DF-009 §2.3 — only these keys passed to frontier
_ALLOWLISTED_KEYS = frozenset(
    [
        "docs_summary",
        "assumptions",
        "coach_evidence",
        "structured_findings",
    ]
)


class FrontierClient(Protocol):
    """Protocol for injected frontier client.

    Implementations handle the HTTP/network layer for frontier model access.
    This protocol exists for dependency injection and testing.
    """

    async def get_opinion(self, brief: dict[str, Any]) -> dict[str, Any]:
        """Request a second opinion from frontier model.

        Args:
            brief: Compressed, policy-filtered PO output summary.

        Returns:
            Opinion data from frontier model (structure varies by implementation).

        Raises:
            Exception: On network errors, timeouts, or frontier unavailability.
        """
        ...


def build_compressed_brief(po_output: dict[str, Any]) -> dict[str, Any]:
    """Build compressed brief with field allowlist (DF-009 §2.3).

    Filters PO output to only allowlisted keys, removing raw conversation data.
    This enforces the policy-filter predicate (AC-003).

    Args:
        po_output: Full product owner output dictionary.

    Returns:
        Compressed brief containing only allowlisted keys.
    """
    return {key: po_output[key] for key in _ALLOWLISTED_KEYS if key in po_output}


class FrontierSecondOpinion:
    """Frontier second opinion provider implementing SecondOpinionProvider.

    Fires only on FLAG_FOR_REVIEW outcomes and sends compressed, policy-filtered
    briefs to the frontier client. Degrades gracefully to human review on errors.
    """

    def __init__(
        self,
        *,
        client: FrontierClient,
        frontier_enabled: bool,
        frontier_timeout_seconds: int,
        get_po_output: Callable[[str], dict[str, Any]],
        get_gate_decision: Callable[[str], GateMode],
    ) -> None:
        """Initialize frontier second opinion provider.

        Args:
            client: Injected frontier client for model access.
            frontier_enabled: Master toggle from PlanningConfig.
            frontier_timeout_seconds: Timeout for frontier requests.
            get_po_output: Callback to fetch PO output for a run.
            get_gate_decision: Callback to fetch gate decision for a run.
        """
        self._client = client
        self._frontier_enabled = frontier_enabled
        self._frontier_timeout_seconds = frontier_timeout_seconds
        self._get_po_output = get_po_output
        self._get_gate_decision = get_gate_decision

    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        """Return compressed PO output summary with optional frontier opinion.

        Implements SecondOpinionProvider protocol. Checks frontier_enabled and
        FLAG_FOR_REVIEW predicate before calling client. On errors, degrades to
        human review by attaching unavailable note.

        Args:
            plan_run_id: Planning run identifier (plan-{correlation_id}).
            stage_label: Stage label for this checkpoint.

        Returns:
            Dictionary with compressed brief and optional second opinion data.
            Never contains approval decision fields (DF-009 enforcement).
        """
        # Fetch PO output and build compressed brief
        po_output = self._get_po_output(plan_run_id)
        brief = build_compressed_brief(po_output)

        # AC-001: Check frontier_enabled toggle
        if not self._frontier_enabled:
            logger.info(
                "frontier second opinion: disabled for %s (frontier_enabled=False)",
                plan_run_id,
            )
            return brief

        # AC-002: Check FLAG_FOR_REVIEW predicate
        gate_decision = self._get_gate_decision(plan_run_id)
        if gate_decision != GateMode.FLAG_FOR_REVIEW:
            logger.info(
                "frontier second opinion: skipped for %s (gate_mode=%s, not FLAG_FOR_REVIEW)",
                plan_run_id,
                gate_decision,
            )
            return brief

        # Both predicates satisfied — call frontier client with timeout
        logger.info(
            "frontier second opinion: requesting for %s (timeout=%ds)",
            plan_run_id,
            self._frontier_timeout_seconds,
        )

        try:
            # AC-004: Enforce timeout around client call
            opinion = await asyncio.wait_for(
                self._client.get_opinion(brief),
                timeout=self._frontier_timeout_seconds,
            )

            # Success — attach opinion data (AC-005: data only, no approval)
            result = dict(brief)
            result["second_opinion"] = opinion
            logger.info("frontier second opinion: received for %s", plan_run_id)
            return result

        except asyncio.TimeoutError:
            # AC-004: Timeout — degrade to human with note
            logger.warning(
                "frontier second opinion: timeout for %s after %ds",
                plan_run_id,
                self._frontier_timeout_seconds,
            )
            result = dict(brief)
            result["second_opinion_unavailable"] = True
            result["second_opinion_error"] = (
                f"Frontier client timeout after {self._frontier_timeout_seconds}s"
            )
            return result

        except Exception as exc:
            # AC-004: Client error — degrade to human with note
            logger.exception(
                "frontier second opinion: error for %s: %s",
                plan_run_id,
                exc,
            )
            result = dict(brief)
            result["second_opinion_unavailable"] = True
            result["second_opinion_error"] = str(exc)
            return result
