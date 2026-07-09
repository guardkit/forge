"""The reviewer fan-out seam (WS3-S5).

The gate *orchestrates* the fan-out — one reviewer per dimension, then
≥``min_refuters`` independent refuters per critical/high proposed finding — but
the reviewer *seat* itself is an injected backend. Per DF-001, on the unattended
critical path the reviewer seats must be local; those local SLM seats are WS4's
scope. So v1 (attended checkpoint) ships:

- :class:`ReviewerInvoker` — the seat protocol the runner drives.
- :class:`UnconfiguredReviewerInvoker` — the PRODUCTION DEFAULT: every method
  raises loudly. An unconfigured reviewer seat is honest ("no local reviewer
  seats yet — WS4"), never a silent empty review that would read as "clean".

This mirrors the deploy stage's loud-raising ``LiveGateInvoker`` /
``BrokerInspector`` stubs: the machinery is real and wired; the model-bearing
backend raises until it is configured. The WS3-S5 gate is proven end-to-end by
injecting a fixture invoker that replays the DD4F review's findings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.review_gate.models import RawFinding, RefuterVote, ReviewPacket

__all__ = ["ReviewerInvoker", "UnconfiguredReviewerInvoker", "ReviewerUnavailable"]


class ReviewerUnavailable(Exception):
    """No reviewer seat is configured — loud, never a silent empty review."""


@runtime_checkable
class ReviewerInvoker(Protocol):
    """A reviewer/refuter seat backend the gate fans out to.

    Two responsibilities, both driven by :class:`~forge.review_gate.stage`:

    - ``review_dimension`` — one dimension reviewer reads the packet and
      proposes findings for its dimension. It returns
      :class:`~forge.review_gate.models.RawFinding` objects with their
      ``refuter_votes`` left EMPTY (proposals; the runner adds refuters).
      A reviewer attaches ``executed_reproduction`` iff it actually ran one —
      reading is not a verdict (LPA-15).

    - ``refute_finding`` — one independent refuter, defaulting to *refuted*,
      attempts to refute a proposed finding and returns its vote (LPA-14).
    """

    def review_dimension(
        self, packet: ReviewPacket, dimension: str
    ) -> tuple[RawFinding, ...]:
        """Propose findings for ``dimension`` (refuter_votes left empty)."""
        ...

    def refute_finding(
        self, packet: ReviewPacket, finding: RawFinding, refuter_id: str
    ) -> RefuterVote:
        """Independently attempt to refute ``finding`` (default: refuted)."""
        ...


class UnconfiguredReviewerInvoker:
    """Production default — every seat method raises loudly (WS4 owns the SLMs)."""

    _MESSAGE = (
        "no reviewer seat is configured — the WS3-S5 merge gate has no local "
        "reviewer SLM to dispatch (reviewer-seat serving is WS4's scope, "
        "DF-001). Inject a configured ReviewerInvoker before running the gate; "
        "an unconfigured seat is never a silent empty review."
    )

    def review_dimension(
        self, packet: ReviewPacket, dimension: str
    ) -> tuple[RawFinding, ...]:
        raise ReviewerUnavailable(self._MESSAGE)

    def refute_finding(
        self, packet: ReviewPacket, finding: RawFinding, refuter_id: str
    ) -> RefuterVote:
        raise ReviewerUnavailable(self._MESSAGE)
