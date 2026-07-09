"""The adversarial merge-gate stage runner (WS3-S5).

:class:`MergeReviewGateRunner` ties the machinery together and drives the
practiced LPA-14/15 workflow for one agent-built feature, end to end:

    build packet
      → reviewer fan-out: one reviewer per dimension
      → for each critical/high proposed finding, dispatch ≥min_refuters
        independent refuters (refuted-by-default)
      → assemble the F14 record (refuted-by-default enforced structurally —
        a finding without an executed reproduction cannot reach confirmed)
      → write the F14 review-findings record
      → [optional] validate it via `guardkit qa validate` (frozen seam)
      → disposition: CLEAN (no confirmed critical/high) or BLOCKED
        (confirmed critical/high ⇒ PAUSE for the human checkpoint)

Attended-v1 (Q2, Rich 2026-07-09) — the disposition pause: the runner exposes an
``escalate`` seam so a BLOCKED review can be routed to the EXISTING approval-gate
machinery (Gate G1-proven) WITHOUT re-implementing it. **In v1 that seam is
present but UNWIRED in production** — no boot code injects the approval publisher
yet (the live wire lands when the gate is promoted off default-OFF, with WS4's
local reviewer seats; same posture as the deploy stage's ``awaiting_approval``
routing). When an ``escalate`` callback IS injected (tests today; the daemon
publisher later) the runner invokes it; otherwise it surfaces
``disposition_required=True`` and the attended operator dispositions the written
record directly. Either way the gate never auto-approves a merge over a
confirmed critical/high finding (DF-009 posture).

The runner is only *constructed and driven* when ``review_gate.enabled`` is true
(default False — inert in production until reviewer-seat SLMs land in WS4). Flag
OFF is a byte-for-byte no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from forge.config.models import ReviewGateConfig
from forge.review_gate.assembler import assemble_review_findings
from forge.review_gate.models import (
    SERIOUS,
    RawFinding,
    ReviewFindingsRecord,
    ReviewPacket,
    ReviewSubject,
)
from forge.review_gate.record import (
    ValidationResult,
    validate_review_findings,
    write_review_findings,
)
from forge.review_gate.reviewer import ReviewerInvoker

logger = logging.getLogger(__name__)

__all__ = ["MergeReviewGateRunner", "MergeReviewResult", "build_review_packet"]

#: Callback that routes a BLOCKED review to the existing approval-gate machinery
#: (Gate G1-proven) for the checkpoint's disposition — the runner does not
#: re-implement the approval loop. Injected, not wired in v1 (see module
#: docstring). Signature: (record, record_ref) -> None.
DispositionEscalator = Callable[[ReviewFindingsRecord, str], None]


def build_review_packet(
    *,
    review_id: str,
    subject: ReviewSubject,
    feature: str,
    config: ReviewGateConfig,
    diff_ref: str | None = None,
    context: tuple[str, ...] = (),
) -> ReviewPacket:
    """Assemble the review packet from config + subject (the dimensions come
    from ``review_gate.dimensions``)."""
    return ReviewPacket(
        review_id=review_id,
        subject=subject,
        feature=feature,
        dimensions=tuple(config.dimensions),
        diff_ref=diff_ref,
        context=tuple(context),
    )


@dataclass(frozen=True, slots=True)
class MergeReviewResult:
    """The outcome of one merge-gate run.

    Attributes:
        outcome: "clean" (no confirmed critical/high — merge may proceed) or
            "blocked" (confirmed critical/high — paused for disposition).
        review_id: The review id.
        record_ref: Path of the written F14 record.
        record: The assembled F14 record.
        disposition_required: True iff there is ≥1 confirmed critical/high
            finding (the pause trigger). Equivalent to outcome == "blocked".
        escalated: True iff the disposition-escalation callback was invoked.
        validation: The `guardkit qa validate` result (None if not validated).
        confirmed_serious: Count of confirmed critical/high findings.
    """

    outcome: str
    review_id: str
    record_ref: str
    record: ReviewFindingsRecord
    disposition_required: bool
    escalated: bool = False
    validation: ValidationResult | None = None
    confirmed_serious: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


class MergeReviewGateRunner:
    """Drives the adversarial merge gate for one agent-built feature.

    Args:
        config: The ``review_gate`` config (dimensions, min_refuters, record_dir).
        record_root: Absolute/relative root dir for the F14 record
            (``<record_root>`` is joined with the repo; the CLI passes
            ``config.record_dir``).
        escalate: Optional disposition-escalation callback routing a BLOCKED
            review to the existing approval-gate publisher. Unwired in v1
            (defaults None) ⇒ the runner surfaces the pause and the attended
            operator dispositions the record.
        validate: When True, validate the emitted record via `guardkit qa
            validate` (the frozen seam). Off by default so the hot path has no
            hard CLI dependency; the gate turns it on for the acceptance run.
    """

    def __init__(
        self,
        *,
        config: ReviewGateConfig,
        record_root: str,
        escalate: DispositionEscalator | None = None,
        validate: bool = False,
    ) -> None:
        self._config = config
        self._record_root = record_root
        self._escalate = escalate
        self._validate = validate

    def _fan_out(
        self, packet: ReviewPacket, invoker: ReviewerInvoker
    ) -> tuple[RawFinding, ...]:
        """Dispatch dimension reviewers, then refuters for each crit/high finding."""
        proposed: list[RawFinding] = []
        for dimension in packet.dimensions:
            proposed.extend(invoker.review_dimension(packet, dimension))

        adjudicable: list[RawFinding] = []
        for pf in proposed:
            if pf.severity in SERIOUS:
                # The gate OWNS refuter dispatch for serious findings — exactly
                # min_refuters independent refuters, refuted-by-default (LPA-14).
                votes = tuple(
                    invoker.refute_finding(packet, pf, f"{pf.id}-refuter-{i}")
                    for i in range(1, self._config.min_refuters + 1)
                )
                adjudicable.append(replace(pf, refuter_votes=votes))
            else:
                # Medium/low: no adversarial quorum required; honour whatever
                # the reviewer attached (usually none).
                adjudicable.append(pf)
        return tuple(adjudicable)

    def run_gate(
        self, packet: ReviewPacket, invoker: ReviewerInvoker
    ) -> MergeReviewResult:
        """Run the gate for ``packet`` against the reviewer ``invoker``.

        Returns a :class:`MergeReviewResult`. Raises only on a structural fan-out
        violation (via the assembler) or a loud reviewer-seat unavailability —
        both are honest failures, never a silent clean verdict.
        """
        raw_findings = self._fan_out(packet, invoker)
        record = assemble_review_findings(
            review_id=packet.review_id,
            subject=packet.subject,
            dimensions=packet.dimensions,
            raw_findings=raw_findings,
            min_refuters=self._config.min_refuters,
        )
        record_ref = write_review_findings(record, root=self._record_root)

        validation: ValidationResult | None = None
        if self._validate:
            validation = validate_review_findings(record_ref)
            if not validation.ok:
                logger.warning(
                    "F14 record %s failed guardkit validation (rc=%s): %s",
                    record_ref,
                    validation.returncode,
                    validation.stderr.strip() or validation.stdout.strip(),
                )

        confirmed_serious = record.confirmed_serious
        disposition_required = bool(confirmed_serious)
        escalated = False
        if disposition_required and self._escalate is not None:
            # Route to the injected approval-gate seam (if one is wired) — the
            # runner never re-implements the approval loop. Unwired in v1.
            self._escalate(record, record_ref)
            escalated = True

        outcome = "blocked" if disposition_required else "clean"
        logger.info(
            "merge gate %s: %s (%d confirmed / %d total; %d confirmed crit-high)",
            packet.review_id,
            outcome,
            record.stats.confirmed,
            record.stats.findings_total,
            len(confirmed_serious),
        )
        return MergeReviewResult(
            outcome=outcome,
            review_id=packet.review_id,
            record_ref=record_ref,
            record=record,
            disposition_required=disposition_required,
            escalated=escalated,
            validation=validation,
            confirmed_serious=len(confirmed_serious),
        )
