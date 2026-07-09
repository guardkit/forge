"""Refuted-by-default enforcement — the gate-critical structural property (WS3-S5).

These tests pin the one rule the WS3-S5 gate exists to enforce: a finding
without an executed reproduction is *structurally unable* to reach ``confirmed``,
and a serious finding cannot be confirmed without surviving the ≥2-refuter
adversarial pass. The verdict is derived by the assembler, never asserted by the
input.
"""

from __future__ import annotations

import pytest

from forge.review_gate.assembler import (
    ReviewAssemblyError,
    assemble_review_findings,
    resolve_status,
)
from forge.review_gate.models import RawFinding, RefuterVote, ReviewSubject

SUBJ = ReviewSubject(kind="merge", ref="abc..def")
DIMS = ("correctness", "spec-fidelity", "wire-topology")


def _raw(
    fid="F-01",
    dimension="correctness",
    severity="high",
    summary="x",
    executed_reproduction=None,
    votes=(),
):
    return RawFinding(
        id=fid,
        dimension=dimension,
        severity=severity,
        summary=summary,
        executed_reproduction=executed_reproduction,
        refuter_votes=tuple(votes),
    )


def _two_not_refuted():
    return (RefuterVote("r1", "not_refuted"), RefuterVote("r2", "not_refuted"))


class TestReadingIsNotAVerdict:
    """LPA-15: a finding without an executed reproduction cannot be confirmed."""

    @pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
    def test_no_reproduction_cannot_reach_confirmed(self, severity):
        # Even with two refuters who could NOT refute it, a repro-less finding
        # stays refuted — reading is not a verdict.
        raw = _raw(
            severity=severity, executed_reproduction=None, votes=_two_not_refuted()
        )
        assert resolve_status(raw, min_refuters=2) == "refuted"

    def test_blank_reproduction_is_not_a_reproduction(self):
        raw = _raw(executed_reproduction="   \n\t ", votes=_two_not_refuted())
        assert resolve_status(raw, min_refuters=2) == "refuted"

    def test_reproduction_present_and_survived_is_confirmed(self):
        raw = _raw(
            executed_reproduction="ran X, got TypeError", votes=_two_not_refuted()
        )
        assert resolve_status(raw, min_refuters=2) == "confirmed"

    def test_structural_via_full_assembly(self):
        # The property holds through the whole record, not just resolve_status.
        rec = assemble_review_findings(
            review_id="t",
            subject=SUBJ,
            dimensions=DIMS,
            raw_findings=(
                _raw(fid="F-01", executed_reproduction=None, votes=_two_not_refuted()),
                _raw(
                    fid="F-02",
                    executed_reproduction="ran, observed crash",
                    votes=_two_not_refuted(),
                ),
            ),
        )
        by_id = {f.id: f for f in rec.findings}
        assert by_id["F-01"].status == "refuted"
        assert by_id["F-02"].status == "confirmed"
        assert rec.stats.confirmed == 1
        assert rec.stats.refuted == 1


class TestAdversarialByDefault:
    """LPA-14: serious findings need ≥2 refuters and must survive them."""

    def test_majority_refuted_kills_the_finding(self):
        raw = _raw(
            executed_reproduction="ran X",
            votes=(RefuterVote("r1", "refuted"), RefuterVote("r2", "not_refuted")),
        )
        # 1 of 2 refuted ⇒ ≥ majority ⇒ killed.
        assert resolve_status(raw, min_refuters=2) == "refuted"

    def test_all_refuted_kills_the_finding(self):
        raw = _raw(
            executed_reproduction="ran X",
            votes=(RefuterVote("r1", "refuted"), RefuterVote("r2", "refuted")),
        )
        assert resolve_status(raw, min_refuters=2) == "refuted"

    def test_minority_refuted_survives(self):
        raw = _raw(
            severity="high",
            executed_reproduction="ran X",
            votes=(
                RefuterVote("r1", "not_refuted"),
                RefuterVote("r2", "not_refuted"),
                RefuterVote("r3", "refuted"),
            ),
        )
        # 1 of 3 refuted ⇒ minority ⇒ survives.
        assert resolve_status(raw, min_refuters=2) == "confirmed"

    def test_serious_finding_without_quorum_raises_at_assembly(self):
        # A crit/high finding with fewer than min_refuters is a fan-out contract
        # violation — the gate refuses to emit rather than record it unchallenged.
        with pytest.raises(ReviewAssemblyError, match="≥2 independent refuters"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(
                    _raw(
                        severity="critical",
                        executed_reproduction="ran X",
                        votes=(RefuterVote("r1", "not_refuted"),),
                    ),
                ),
            )

    def test_duplicate_who_is_not_independent(self):
        # One seat voting twice is not two independent challenges (LPA-14).
        with pytest.raises(ReviewAssemblyError, match="non-independent"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(
                    _raw(
                        severity="critical",
                        executed_reproduction="ran X",
                        votes=(
                            RefuterVote("r1", "not_refuted"),
                            RefuterVote("r1", "not_refuted"),
                        ),
                    ),
                ),
            )

    def test_serious_needs_distinct_refuters_for_quorum(self):
        # Two votes from the same seat do not meet the ≥2 independent quorum.
        with pytest.raises(ReviewAssemblyError, match="non-independent"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(
                    _raw(
                        fid="F-01",
                        severity="high",
                        executed_reproduction="ran X",
                        votes=(
                            RefuterVote("solo", "not_refuted"),
                            RefuterVote("solo", "refuted"),
                        ),
                    ),
                ),
            )

    def test_medium_low_need_no_refuters(self):
        rec = assemble_review_findings(
            review_id="t",
            subject=SUBJ,
            dimensions=DIMS,
            raw_findings=(
                _raw(fid="F-01", severity="medium", executed_reproduction="ran X"),
                _raw(fid="F-02", severity="low", executed_reproduction="grep X"),
            ),
        )
        assert all(f.status == "confirmed" for f in rec.findings)


class TestStructuralGuards:
    """The assembler refuses to emit a dishonest or invalid record."""

    def test_empty_review_id_raises(self):
        with pytest.raises(ReviewAssemblyError, match="review_id"):
            assemble_review_findings(
                review_id="  ", subject=SUBJ, dimensions=DIMS, raw_findings=()
            )

    def test_empty_dimensions_raises(self):
        with pytest.raises(ReviewAssemblyError, match="dimension"):
            assemble_review_findings(
                review_id="t", subject=SUBJ, dimensions=(), raw_findings=()
            )

    def test_min_refuters_below_two_raises(self):
        with pytest.raises(ReviewAssemblyError, match="min_refuters"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(),
                min_refuters=1,
            )

    def test_finding_on_undispatched_dimension_raises(self):
        with pytest.raises(ReviewAssemblyError, match="not dispatched"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=("correctness",),
                raw_findings=(_raw(dimension="security", executed_reproduction="x"),),
            )

    def test_duplicate_finding_id_raises(self):
        with pytest.raises(ReviewAssemblyError, match="duplicate"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(
                    _raw(fid="F-01", severity="low", executed_reproduction="x"),
                    _raw(fid="F-01", severity="low", executed_reproduction="y"),
                ),
            )

    def test_empty_finding_id_raises(self):
        with pytest.raises(ReviewAssemblyError, match="non-empty id"):
            assemble_review_findings(
                review_id="t",
                subject=SUBJ,
                dimensions=DIMS,
                raw_findings=(
                    _raw(fid="  ", severity="low", executed_reproduction="x"),
                ),
            )


class TestStats:
    def test_stats_tally(self):
        rec = assemble_review_findings(
            review_id="t",
            subject=SUBJ,
            dimensions=DIMS,
            raw_findings=(
                _raw(
                    fid="F-01",
                    severity="high",
                    executed_reproduction="x",
                    votes=_two_not_refuted(),
                ),
                _raw(
                    fid="F-02",
                    severity="high",
                    executed_reproduction=None,
                    votes=_two_not_refuted(),
                ),
                _raw(fid="F-03", severity="low", executed_reproduction="x"),
            ),
        )
        assert rec.stats.findings_total == 3
        assert rec.stats.confirmed == 2  # F-01 (high survived), F-03 (low w/ repro)
        assert rec.stats.refuted == 1  # F-02 (no repro)
        assert rec.stats.refutations_attempted == 4  # 2 + 2 + 0
