"""Refuted-by-default adjudication — the structural heart of WS3-S5.

:func:`assemble_review_findings` takes the raw fan-out (dimension reviewers'
proposed findings + their dispatched refuter votes) and produces the adjudicated
F14 :class:`~forge.review_gate.models.ReviewFindingsRecord`. Two rules are
enforced *structurally* here — a caller cannot route around them, which is the
whole point of the gate (the DD4F recursion happened because a permissive fake
let a wrong contract stay green):

1. **Reading is not a verdict (LPA-15).** A finding reaches ``confirmed`` only
   if it carries a non-empty ``executed_reproduction``. A repro-less finding is
   *structurally unable* to reach confirmed — it is recorded ``refuted``,
   regardless of how many refuters failed to refute it. This is the property
   the WS3-S5 gate tests.

2. **Adversarial by default (LPA-14).** A finding starts *refuted*. A
   critical/high finding is promoted to ``confirmed`` only if it carries ≥
   ``min_refuters`` (default 2) independent refuters AND a majority of them
   returned ``not_refuted`` (they tried to refute and could not). If a majority
   refute, the finding is killed. A crit/high finding presented with fewer than
   ``min_refuters`` refuters is a fan-out contract violation and raises loudly —
   the gate never emits a record that would not survive its own honesty rule.

Medium/low findings need no refuters; they reach confirmed on an executed
reproduction alone (the F14 schema only mandates refuters for critical/high),
but any refuters they *do* carry are honoured by the majority rule.
"""

from __future__ import annotations

from forge.review_gate.models import (
    SERIOUS,
    Finding,
    FindingStatus,
    RawFinding,
    Refuter,
    ReviewFindingsRecord,
    ReviewStats,
    ReviewSubject,
)

__all__ = ["assemble_review_findings", "resolve_status", "ReviewAssemblyError"]


class ReviewAssemblyError(Exception):
    """The raw fan-out cannot form an honest F14 record (fail loud, never emit)."""


def _has_executed_reproduction(raw: RawFinding) -> bool:
    return bool(raw.executed_reproduction and raw.executed_reproduction.strip())


def resolve_status(raw: RawFinding, *, min_refuters: int) -> FindingStatus:
    """Adjudicate one proposed finding to ``confirmed`` or ``refuted``.

    Refuted by default. See the module docstring for the two rules. Never
    raises for an *adjudication* outcome — a finding that cannot be confirmed is
    simply ``refuted``. (Structural fan-out violations are caught by
    :func:`assemble_review_findings`, not here.)
    """
    # Rule 1 (LPA-15): no executed reproduction ⇒ refuted. Unconditional across
    # every severity — reading is not a verdict.
    if not _has_executed_reproduction(raw):
        return "refuted"

    votes = raw.refuter_votes
    # Rule 2 (LPA-14): critical/high without the adversarial quorum cannot be
    # confirmed. (assemble_* raises before we get here; belt-and-braces.)
    if raw.severity in SERIOUS and len(votes) < min_refuters:
        return "refuted"

    # Rule 3: survives only by surviving refutation. Killed if a majority of the
    # dispatched refuters succeeded in refuting it. With no refuters (medium/low
    # allowed), there is nothing to survive ⇒ the executed repro carries it.
    if votes:
        refuted_votes = sum(1 for v in votes if v.verdict == "refuted")
        if refuted_votes * 2 >= len(votes):  # ≥ majority refuted ⇒ killed
            return "refuted"

    return "confirmed"


def _to_record_refuters(raw: RawFinding) -> tuple[Refuter, ...]:
    return tuple(
        Refuter(who=v.who, verdict=v.verdict, note=v.note) for v in raw.refuter_votes
    )


def assemble_review_findings(
    *,
    review_id: str,
    subject: ReviewSubject,
    dimensions: tuple[str, ...],
    raw_findings: tuple[RawFinding, ...],
    min_refuters: int = 2,
) -> ReviewFindingsRecord:
    """Adjudicate the raw fan-out into an F14 record.

    Args:
        review_id: The review's id (non-empty).
        subject: What was reviewed.
        dimensions: The dispatched dimensions (≥1; every finding's dimension
            must be one of these).
        raw_findings: The reviewers' proposed findings with their refuter votes.
        min_refuters: Minimum refuters per critical/high finding (≥2, LPA-14).

    Returns:
        A :class:`ReviewFindingsRecord` whose per-finding ``status`` was derived
        by :func:`resolve_status` — never trusted from the input.

    Raises:
        ReviewAssemblyError: on a structural violation that would make the
            emitted record dishonest or invalid — empty review_id/dimensions, a
            finding on an undispatched dimension, a duplicate finding id, or a
            critical/high finding with fewer than ``min_refuters`` refuters.
    """
    if not review_id.strip():
        raise ReviewAssemblyError("review_id must be non-empty")
    if min_refuters < 2:
        raise ReviewAssemblyError(
            f"min_refuters must be ≥2 (LPA-14), got {min_refuters}"
        )
    if not dimensions:
        raise ReviewAssemblyError("at least one review dimension is required")
    dim_set = set(dimensions)

    seen_ids: set[str] = set()
    findings: list[Finding] = []
    for raw in raw_findings:
        if not raw.id.strip():
            raise ReviewAssemblyError("every finding needs a non-empty id")
        if raw.id in seen_ids:
            raise ReviewAssemblyError(f"duplicate finding id {raw.id!r}")
        seen_ids.add(raw.id)
        if raw.dimension not in dim_set:
            raise ReviewAssemblyError(
                f"finding {raw.id!r} is on dimension {raw.dimension!r} which was "
                f"not dispatched (dispatched: {sorted(dim_set)})"
            )
        # Independence (LPA-14): refuters must be DISTINCT — one seat voting
        # twice is not two independent challenges. Reject duplicate ``who``.
        whos = [v.who for v in raw.refuter_votes]
        if len(set(whos)) != len(whos):
            dupes = sorted({w for w in whos if whos.count(w) > 1})
            raise ReviewAssemblyError(
                f"finding {raw.id!r} has non-independent refuters (duplicate "
                f"who: {dupes}) — every refuter must be a distinct seat (LPA-14)."
            )
        # Fan-out contract: crit/high MUST carry the adversarial quorum of
        # DISTINCT refuters, or the F14 record would be invalid AND the review
        # dishonest (a serious finding with no independent challenge). Fail loud.
        if raw.severity in SERIOUS and len(set(whos)) < min_refuters:
            raise ReviewAssemblyError(
                f"finding {raw.id!r} is {raw.severity!r} but carries "
                f"{len(set(whos))} independent refuter(s) — every critical/high "
                f"finding needs ≥{min_refuters} independent refuters (LPA-14). "
                f"Dispatch the refuter fan-out before assembling."
            )
        status = resolve_status(raw, min_refuters=min_refuters)
        findings.append(
            Finding(
                id=raw.id,
                dimension=raw.dimension,
                severity=raw.severity,
                status=status,
                summary=raw.summary,
                # Only a confirmed finding keeps its executed_reproduction in the
                # record's confirmed sense; a refuted finding may still carry it
                # for the audit trail, so we preserve whatever was executed.
                executed_reproduction=(
                    raw.executed_reproduction
                    if _has_executed_reproduction(raw)
                    else None
                ),
                refuters=_to_record_refuters(raw),
            )
        )

    confirmed = sum(1 for f in findings if f.status == "confirmed")
    refuted = sum(1 for f in findings if f.status == "refuted")
    refutations_attempted = sum(len(f.refuters) for f in findings)
    stats = ReviewStats(
        findings_total=len(findings),
        confirmed=confirmed,
        refuted=refuted,
        refutations_attempted=refutations_attempted,
    )
    return ReviewFindingsRecord(
        review_id=review_id,
        subject=subject,
        dimensions=tuple(dimensions),
        findings=tuple(findings),
        stats=stats,
    )
