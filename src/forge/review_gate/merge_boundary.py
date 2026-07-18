"""The attended-v1 merge-boundary INVOKE point (H-A Stage 2).

The ``review_gate`` subsystem is fully built and green but, until this module,
had **zero production callers** at the merge boundary — the CLI
(``forge review-gate``) was the only surface, and the streak/auto-merge machinery
(H-A Stage 3, guardkit) needs a programmatic seam it can drive per merge.

:func:`dispatch_merge_review_gate` is that seam. Given a **human-collected**
review-input document (N dimension reviewers + ≥2 independent refuters per
critical/high finding — the ``forge review-gate`` docstring shape), it runs the
**CLI adjudication path** — :func:`assemble_review_findings` (refuted-by-default:
LPA-14/15 enforced structurally in the assembler) →
:func:`write_review_findings` → ``<record_dir>/review-<id>.yaml`` — and returns a
:class:`MergeBoundaryReviewResult` the merge boundary branches on.

It deliberately does **not** touch the reviewer seat: it never calls the
runner's ``_fan_out`` (which needs a live ``ReviewerInvoker`` — that is WS4). The
human already collected the fan-out; this seam only adjudicates + records it.

Flag posture (attended-v1): ``review_gate.enabled`` defaults False and stays the
default. With the flag **OFF this dispatch is a byte-for-byte no-op** — it
adjudicates nothing and writes nothing (``ran=False``, ``record_ref=None``), so
the merge proceeds exactly as it did before the gate existed. The new invoke
point activates only under an attended per-feature opt-in (``enabled=True``),
never a boot default. The ``DispositionEscalator`` stays absent: a BLOCKED result
is surfaced (``outcome="blocked"``) for the **attended operator** to disposition
the written record directly (wiring an approval publisher is WS4).

Outcome → exit-code mapping mirrors the CLI (``cli/review_gate.py``) exactly:
0 = clean (merge may proceed), 4 = blocked (confirmed critical/high — the human
checkpoint's disposition), and a disabled no-op maps to 0 (the gate is simply not
active, so the merge is not held). Malformed input / assembly violations raise
loudly (``ReviewInputError`` / ``ReviewAssemblyError``) — never a silent clean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from forge.config.models import ReviewGateConfig
from forge.review_gate.assembler import assemble_review_findings
from forge.review_gate.models import (
    Finding,
    ReviewFindingsRecord,
    ReviewInputError,
    ReviewSubject,
    raw_findings_from_input,
)
from forge.review_gate.record import write_review_findings

__all__ = [
    "MergeBoundaryReviewResult",
    "dispatch_merge_review_gate",
    "load_review_input",
]

#: Exit code for a BLOCKED review (a confirmed critical/high finding). Mirrors
#: the CLI's ``_EXIT_BLOCKED`` — a blocked merge is not an error, but it is a
#: distinct non-zero outcome a caller (Stage 3's ledger) can branch on.
_EXIT_BLOCKED = 4


@dataclass(frozen=True, slots=True)
class MergeBoundaryReviewResult:
    """The outcome of one merge-boundary review-gate dispatch.

    Attributes:
        outcome: ``"clean"`` (no confirmed critical/high — merge may proceed),
            ``"blocked"`` (≥1 confirmed critical/high — held for the attended
            disposition), or ``"disabled"`` (the gate flag is OFF — byte no-op).
        ran: True iff the gate actually adjudicated + wrote a record. False for
            the disabled no-op (nothing was written).
        review_id: The review id (None only for the disabled no-op).
        record_ref: Path of the written F14 record (None for the disabled
            no-op — nothing is written when the flag is OFF).
        record: The assembled F14 record (None for the disabled no-op).
        confirmed_serious: The confirmed critical/high findings — the
            disposition trigger (empty unless blocked).
    """

    outcome: str
    ran: bool
    review_id: str | None = None
    record_ref: str | None = None
    record: ReviewFindingsRecord | None = None
    confirmed_serious: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        """True iff the merge must be held for the attended disposition."""
        return self.outcome == "blocked"

    @property
    def exit_code(self) -> int:
        """CLI-aligned exit code: 4 = blocked, 0 = clean or disabled no-op."""
        return _EXIT_BLOCKED if self.blocked else 0


def load_review_input(path: str | Path) -> dict:
    """Load + JSON-parse a human-collected review-input document (loud on error).

    Raises:
        ReviewInputError: the file cannot be read or is not a JSON object.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewInputError(f"cannot read review-input {path}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"review-input {path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ReviewInputError("review-input must be a JSON object (mapping)")
    return doc


def dispatch_merge_review_gate(
    *,
    review_input: dict,
    feature: str,
    config: ReviewGateConfig,
    record_dir: str | None = None,
    subject_kind: str = "merge",
    subject_ref: str | None = None,
) -> MergeBoundaryReviewResult:
    """Run the attended-v1 review gate at the merge boundary.

    Given a **human-collected** ``review_input`` (the reviewer/refuter fan-out
    already collected — this seam does not dispatch a reviewer seat), adjudicate
    it via the assembler (refuted-by-default) and write the F14 record to
    ``<record_dir>/review-<review_id>.yaml``.

    Args:
        review_input: The parsed review-input document (see
            :func:`load_review_input` for the on-disk form). Its ``review_id`` /
            ``subject`` / ``findings`` follow the ``forge review-gate`` shape.
        feature: The agent-built feature under review (labels the review + seeds
            the review_id / subject ref when the input omits them).
        config: The ``review_gate`` config. When ``config.enabled`` is False the
            dispatch is a byte-for-byte no-op (nothing is adjudicated or written).
        record_dir: Override for where the F14 record is written (default
            ``config.record_dir``, repo-relative ``qa``).
        subject_kind: Fallback subject kind when the input omits ``subject``.
        subject_ref: Fallback subject ref when the input omits ``subject``
            (defaults to ``feature``).

    Returns:
        A :class:`MergeBoundaryReviewResult`. ``blocked`` / ``exit_code`` tell the
        caller whether to hold the merge (exit 4) or proceed (exit 0).

    Raises:
        ReviewInputError: the input document / subject is malformed.
        ReviewAssemblyError: the fan-out cannot form an honest F14 record (e.g. a
            critical/high finding without the ≥min_refuters adversarial quorum) —
            fail loud, never emit a dishonest record.
    """
    # Flag OFF ⇒ byte-for-byte no-op: adjudicate nothing, write nothing. The
    # merge proceeds exactly as it did before the gate existed (the attended
    # opt-in flips enabled=True per feature; it is never a boot default).
    if not config.enabled:
        return MergeBoundaryReviewResult(outcome="disabled", ran=False)

    review_id = review_input.get("review_id") or f"{feature}-merge-review"
    subj = review_input.get("subject")
    # ReviewSubject validates kind/ref at construction — a bad subject fails
    # loud here, never a silently schema-invalid record.
    if isinstance(subj, dict) and subj.get("kind") and subj.get("ref"):
        subject = ReviewSubject(kind=subj["kind"], ref=subj["ref"])
    else:
        subject = ReviewSubject(kind=subject_kind, ref=subject_ref or feature)

    raw_findings = raw_findings_from_input(review_input)
    dimensions = tuple(dict.fromkeys(f.dimension for f in raw_findings)) or tuple(
        config.dimensions
    )
    # The adjudication path (assembler → record) — NOT the runner's _fan_out.
    # LPA-14/15 are enforced structurally inside assemble_review_findings; this
    # seam never asserts a status or skips refuter adjudication.
    record = assemble_review_findings(
        review_id=review_id,
        subject=subject,
        dimensions=dimensions,
        raw_findings=raw_findings,
        min_refuters=config.min_refuters,
    )
    record_ref = write_review_findings(record, root=record_dir or config.record_dir)

    confirmed_serious = record.confirmed_serious
    outcome = "blocked" if confirmed_serious else "clean"
    return MergeBoundaryReviewResult(
        outcome=outcome,
        ran=True,
        review_id=review_id,
        record_ref=record_ref,
        record=record,
        confirmed_serious=confirmed_serious,
    )
