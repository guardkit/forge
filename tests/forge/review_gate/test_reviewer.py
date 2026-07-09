"""The reviewer seam — the production default raises loudly (WS3-S5)."""

from __future__ import annotations

import pytest

from forge.review_gate.models import RawFinding, ReviewPacket, ReviewSubject
from forge.review_gate.reviewer import (
    ReviewerInvoker,
    ReviewerUnavailable,
    UnconfiguredReviewerInvoker,
)

PACKET = ReviewPacket(
    review_id="t",
    subject=ReviewSubject(kind="merge", ref="a..b"),
    feature="F",
    dimensions=("correctness",),
)


def test_unconfigured_review_dimension_raises():
    with pytest.raises(ReviewerUnavailable, match="reviewer seat"):
        UnconfiguredReviewerInvoker().review_dimension(PACKET, "correctness")


def test_unconfigured_refute_raises():
    finding = RawFinding(
        id="F-01", dimension="correctness", severity="high", summary="x"
    )
    with pytest.raises(ReviewerUnavailable, match="WS4"):
        UnconfiguredReviewerInvoker().refute_finding(PACKET, finding, "r1")


def test_unconfigured_satisfies_the_protocol():
    assert isinstance(UnconfiguredReviewerInvoker(), ReviewerInvoker)
