"""WS3-S5 adversarial merge-review gate for agent-built features.

Formalizes the practiced N-reviewers / ≥2-refuters / refuted-by-default /
executed-reproduction workflow (LPA-14/15; DD4F post-merge review as exemplar)
as a forge merge-gate STAGE — v1 = an **attended checkpoint** (Q2, Rich
2026-07-09). The stage assembles the review packet, dispatches the reviewer
fan-out, enforces refuted-by-default (a finding without an executed reproduction
is structurally unable to reach ``confirmed``), emits the guardkit **F14**
review-findings record, validates it across the frozen guardkit CLI seam, and
pauses for the human checkpoint's disposition. The pause is designed to route
through the existing approval-gate machinery (Gate G1-proven; DF-001/DF-003/
DF-009 posture) via an injected ``escalate`` seam — present but UNWIRED in v1
(the attended operator dispositions the emitted record directly; the live wire
lands with WS4's local reviewer seats).

Inert in production until reviewer-seat SLMs land in WS4: the gate is only
driven when ``review_gate.enabled`` is true (default False). Flag OFF is a
byte-for-byte no-op.
"""

from forge.review_gate.assembler import (
    ReviewAssemblyError,
    assemble_review_findings,
    resolve_status,
)
from forge.review_gate.models import (
    Finding,
    RawFinding,
    Refuter,
    RefuterVote,
    ReviewFindingsRecord,
    ReviewInputError,
    ReviewPacket,
    ReviewStats,
    ReviewSubject,
    raw_finding_from_dict,
    raw_findings_from_input,
)
from forge.review_gate.record import (
    GuardkitValidatorUnavailable,
    ValidationResult,
    render_review_findings,
    validate_review_findings,
    write_review_findings,
)
from forge.review_gate.reviewer import (
    ReviewerInvoker,
    ReviewerUnavailable,
    UnconfiguredReviewerInvoker,
)
from forge.review_gate.stage import (
    MergeReviewGateRunner,
    MergeReviewResult,
    build_review_packet,
)

__all__ = [
    # models
    "Finding",
    "RawFinding",
    "Refuter",
    "RefuterVote",
    "ReviewFindingsRecord",
    "ReviewInputError",
    "ReviewPacket",
    "ReviewStats",
    "ReviewSubject",
    "raw_finding_from_dict",
    "raw_findings_from_input",
    # assembler
    "assemble_review_findings",
    "resolve_status",
    "ReviewAssemblyError",
    # record
    "render_review_findings",
    "write_review_findings",
    "validate_review_findings",
    "ValidationResult",
    "GuardkitValidatorUnavailable",
    # reviewer
    "ReviewerInvoker",
    "UnconfiguredReviewerInvoker",
    "ReviewerUnavailable",
    # stage
    "MergeReviewGateRunner",
    "MergeReviewResult",
    "build_review_packet",
]
