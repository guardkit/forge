"""ReviewSubject validates at construction — no silently schema-invalid records."""

from __future__ import annotations

import pytest

from forge.review_gate.assembler import assemble_review_findings
from forge.review_gate.models import (
    RawFinding,
    ReviewInputError,
    ReviewSubject,
)


class TestSubjectValidation:
    def test_valid_kinds(self):
        for kind in ("tree", "commit", "merge"):
            assert ReviewSubject(kind=kind, ref="x").kind == kind

    def test_bad_kind_raises(self):
        with pytest.raises(ReviewInputError, match="subject.kind"):
            ReviewSubject(kind="workingtree", ref="a..b")

    def test_empty_ref_raises(self):
        with pytest.raises(ReviewInputError, match="subject.ref"):
            ReviewSubject(kind="merge", ref="   ")

    def test_non_string_ref_raises(self):
        with pytest.raises(ReviewInputError, match="subject.ref"):
            ReviewSubject(kind="merge", ref=123)  # type: ignore[arg-type]

    def test_invalid_subject_blocks_assembly_on_every_path(self):
        # Even with validate off downstream, a bad subject cannot form a record.
        with pytest.raises(ReviewInputError):
            assemble_review_findings(
                review_id="t",
                subject=ReviewSubject(kind="bogus", ref="x"),
                dimensions=("correctness",),
                raw_findings=(
                    RawFinding(
                        id="F-01",
                        dimension="correctness",
                        severity="low",
                        summary="x",
                        executed_reproduction="ran",
                    ),
                ),
            )
