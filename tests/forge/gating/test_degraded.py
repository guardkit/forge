"""The pause card's wording is an owner surface — pin it and fence it.

The degraded gate's rationale renders VERBATIM on the Slack approval card
(``GateDecision.rationale`` → approval details → ``BuildPausedPayload`` →
jarvis's "Rationale: ..." line), so its words are part of the surface the
owner reads, not an implementation detail. The owner's standing rule: plain
English there — no register codes, no internal shorthand. The old sentence
named an architecture-decision id and a ratchet code on the card; these
tests pin the agreed plain sentence and fence every payload-facing wording
constant against codenames so the jargon cannot creep back.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from forge.gating.degraded import (
    DEGRADED_RATIONALE,
    degraded_dispatch_gate_model,
    degraded_recovery_decision,
)
from forge.gating.models import GateMode
from forge.lifecycle_bridge.translation import (
    CANCELLED_REASON_FALLBACK,
    COMPLETED_SUMMARY_FALLBACK,
    FAILED_REASON_FALLBACK,
)
from forge.planning.checkpoint import PRODUCT_DOCS_PAUSE_RATIONALE

#: The exact sentence agreed for the build-gate pause card (2026-08-26).
PLAIN_PAUSE_SENTENCE = (
    "No automatic score exists at this stage, so the factory always asks "
    "you before starting a build."
)

# Internal vocabulary that must never reach a Slack-rendered payload string.
# Mirrors the jarvis-side plain-name fence (its template test); this is the
# forge-side twin for the strings forge authors and jarvis relays verbatim.
_FORBIDDEN_WORDS: tuple[str, ...] = (
    "coach",
    "autobuild",
    "guardkit",
    "forge",
    "degraded",
    "prior",
    "sse",
    "ratchet",
)
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"\bADR-[A-Z]+-\d+",  # architecture decision ids
    r"\bDDR-\d+",  # design decision ids
    r"\bDF-\d+",  # design fence ids
    r"\bMode [ABCP]\b",  # internal mode codenames
    r"\bv\d+\b",  # bare version-ratchet talk ("v1 never ...")
    r"§",  # section-clause references
)

#: Every forge-authored string that lands verbatim on a Slack surface.
_SLACK_FACING_STRINGS: dict[str, str] = {
    "pause card rationale": DEGRADED_RATIONALE,
    "product docs checkpoint rationale": PRODUCT_DOCS_PAUSE_RATIONALE,
    "completed summary fallback": COMPLETED_SUMMARY_FALLBACK,
    "failed reason fallback": FAILED_REASON_FALLBACK,
    "cancelled reason fallback": CANCELLED_REASON_FALLBACK,
}


def _offenders(text: str) -> list[str]:
    lowered = text.lower()
    found = [word for word in _FORBIDDEN_WORDS if word in lowered]
    for pattern in _FORBIDDEN_PATTERNS:
        found.extend(re.findall(pattern, text))
    return found


class TestPauseCardRationale:
    """The gate's pause sentence is exactly the agreed plain English."""

    def test_the_rationale_is_the_agreed_sentence(self) -> None:
        assert DEGRADED_RATIONALE == PLAIN_PAUSE_SENTENCE

    def test_the_static_response_machine_fields_are_untouched(self) -> None:
        """Only the human-read sentence changed; the parsed contract did not."""
        body = json.loads(degraded_dispatch_gate_model("ignored prompt"))
        assert body["mode"] == GateMode.MANDATORY_HUMAN_APPROVAL.value
        assert body["threshold_applied"] is None
        assert body["relevant_prior_ids"] == []
        assert body["rationale"] == PLAIN_PAUSE_SENTENCE

    def test_the_recovery_decision_carries_the_same_sentence(self) -> None:
        """The rearm fallback card reads the same as a fresh pause card."""
        decision = degraded_recovery_decision(
            build_id="build-wording-1",
            stage_label="autobuild",
            decided_at=datetime(2026, 8, 26, tzinfo=UTC),
        )
        assert decision.rationale == PLAIN_PAUSE_SENTENCE
        assert decision.mode is GateMode.MANDATORY_HUMAN_APPROVAL
        assert decision.degraded_mode is True
        assert decision.coach_score is None
        assert decision.threshold_applied is None


class TestSlackFacingStringsSpeakPlainEnglish:
    """No register codes or house shorthand in any Slack-bound string."""

    @pytest.mark.parametrize(
        ("surface", "text"),
        sorted(_SLACK_FACING_STRINGS.items()),
        ids=sorted(_SLACK_FACING_STRINGS),
    )
    def test_no_codenames_reach_the_card(self, surface: str, text: str) -> None:
        found = _offenders(text)
        assert not found, (
            f"{surface} carries internal vocabulary {found} — this string "
            f"renders verbatim on a Slack surface the owner reads, so it "
            f"must be plain English (owner's standing rule)."
        )

    def test_the_fence_bites(self) -> None:
        """The detector catches the exact old defect, so passing means something."""
        old = (
            "Degraded/training mode: no coach score is available at dispatch "
            "and v1 never auto-approves, so the gate mandates human approval "
            "before the build starts; any retrieved priors ride the decision "
            "as evidence (ADR-ARCH-019 honesty posture)."
        )
        found = _offenders(old)
        assert "coach" in found
        assert "ADR-ARCH-019" in found
