"""Review-input parsing — loud on malformed input, verdict never asserted."""

from __future__ import annotations

import pytest

from forge.review_gate.models import (
    ReviewInputError,
    raw_finding_from_dict,
    raw_findings_from_input,
)


def _ok_finding():
    return {
        "id": "F-01",
        "dimension": "correctness",
        "severity": "high",
        "summary": "x",
        "executed_reproduction": "ran X",
        "refuter_votes": [
            {"who": "r1", "verdict": "not_refuted"},
            {"who": "r2", "verdict": "not_refuted", "note": "n"},
        ],
    }


class TestParseFinding:
    def test_valid(self):
        f = raw_finding_from_dict(_ok_finding())
        assert f.id == "F-01"
        assert len(f.refuter_votes) == 2
        assert f.refuter_votes[1].note == "n"

    def test_status_in_input_is_rejected(self):
        # A reviewer must not assert the verdict — the gate derives it.
        d = _ok_finding() | {"status": "confirmed"}
        with pytest.raises(ReviewInputError, match="must not carry 'status'"):
            raw_finding_from_dict(d)

    def test_unknown_key_rejected(self):
        d = _ok_finding() | {"bogus": 1}
        with pytest.raises(ReviewInputError, match="unknown finding key"):
            raw_finding_from_dict(d)

    def test_bad_severity_rejected(self):
        d = _ok_finding() | {"severity": "blocker"}
        with pytest.raises(ReviewInputError, match="severity"):
            raw_finding_from_dict(d)

    def test_bad_verdict_rejected(self):
        d = _ok_finding()
        d["refuter_votes"] = [{"who": "r1", "verdict": "maybe"}]
        with pytest.raises(ReviewInputError, match="verdict"):
            raw_finding_from_dict(d)

    def test_missing_id_rejected(self):
        d = _ok_finding()
        del d["id"]
        with pytest.raises(ReviewInputError, match="id"):
            raw_finding_from_dict(d)

    def test_default_verdict_is_refuted(self):
        d = _ok_finding()
        d["refuter_votes"] = [{"who": "r1"}]
        f = raw_finding_from_dict(d)
        assert f.refuter_votes[0].verdict == "refuted"


class TestParseDocument:
    def test_findings_must_be_list(self):
        with pytest.raises(ReviewInputError, match="findings must be a list"):
            raw_findings_from_input({"findings": {}})

    def test_parses_all(self):
        doc = {"findings": [_ok_finding(), _ok_finding() | {"id": "F-02"}]}
        assert len(raw_findings_from_input(doc)) == 2
