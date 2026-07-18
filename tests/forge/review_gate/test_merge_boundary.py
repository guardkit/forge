"""H-A Stage 2 — the attended-v1 merge-boundary INVOKE point.

Covers :func:`dispatch_merge_review_gate` — the production seam that runs the
review gate at the merge boundary from a human-collected review-input:

- flag OFF ⇒ byte-for-byte no-op (nothing adjudicated, nothing written);
- flag ON + a confirmed critical/high ⇒ a valid F14 MG-3 record at
  ``qa/review-<id>.yaml`` and BLOCKED (exit 4);
- flag ON + no confirmed critical/high ⇒ CLEAN (exit 0), record still written;
- review_id / subject derivation and the on-disk loader.

The reviewer seat is never touched (no ``_fan_out``) — the human collected the
fan-out; this seam only adjudicates + records it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from forge.config.models import ReviewGateConfig
from forge.review_gate.merge_boundary import (
    MergeBoundaryReviewResult,
    dispatch_merge_review_gate,
    load_review_input,
)
from forge.review_gate.models import ReviewInputError

FIXTURE = Path(__file__).parent / "fixtures" / "dd4f_review_input.json"


@pytest.fixture
def dd4f_doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _clean_doc() -> dict:
    """A review-input with no confirmed critical/high ⇒ a CLEAN disposition.

    One medium finding with an executed reproduction (confirmed, but medium is
    not "serious"), and one critical finding whose refuters killed it (a
    majority refuted ⇒ refuted, so it never becomes a confirmed crit/high).
    """
    return {
        "review_id": "CLEAN-001",
        "subject": {"kind": "merge", "ref": "aaa..bbb"},
        "findings": [
            {
                "id": "F-01",
                "dimension": "correctness",
                "severity": "medium",
                "summary": "a confirmed but non-serious nit",
                "executed_reproduction": "ran X, observed Y",
            },
            {
                "id": "F-02",
                "dimension": "correctness",
                "severity": "critical",
                "summary": "a critical claim the refuters killed",
                "executed_reproduction": "ran Z, observed W",
                "refuter_votes": [
                    {"who": "refuter-A", "verdict": "refuted"},
                    {"who": "refuter-B", "verdict": "refuted"},
                ],
            },
        ],
    }


class TestFlagOffByteNoOp:
    def test_disabled_default_writes_nothing(self, tmp_path, dd4f_doc):
        # enabled defaults False — the merge-boundary dispatch is a byte no-op.
        result = dispatch_merge_review_gate(
            review_input=dd4f_doc,
            feature="FEAT-SPL-002",
            config=ReviewGateConfig(),
            record_dir=str(tmp_path),
        )
        assert result.outcome == "disabled"
        assert result.ran is False
        assert result.record_ref is None
        assert result.record is None
        assert result.blocked is False
        assert result.exit_code == 0
        # Byte no-op: nothing written under the record dir.
        assert list(tmp_path.iterdir()) == []


class TestBlockedOnConfirmedSerious:
    @pytest.fixture
    def result(self, tmp_path, dd4f_doc) -> MergeBoundaryReviewResult:
        return dispatch_merge_review_gate(
            review_input=dd4f_doc,
            feature="FEAT-SPL-002",
            config=ReviewGateConfig(enabled=True),
            record_dir=str(tmp_path),
        )

    def test_outcome_blocked_exit_4(self, result):
        assert result.ran is True
        assert result.outcome == "blocked"
        assert result.blocked is True
        assert result.exit_code == 4
        assert len(result.confirmed_serious) >= 1

    def test_record_written_to_review_id_path(self, result, tmp_path):
        assert result.record_ref is not None
        expected = tmp_path / "review-FEAT-SPL-002-DD4F-postmerge.yaml"
        assert Path(result.record_ref) == expected
        assert expected.exists()

    def test_emitted_record_is_f14_mg3_shaped(self, result):
        doc = yaml.safe_load(Path(result.record_ref).read_text())
        assert doc["format_version"] == "1.0"
        assert doc["review_id"] == "FEAT-SPL-002-DD4F-postmerge"
        assert doc["subject"] == {"kind": "merge", "ref": "d13d88f..1fcb72c"}
        assert doc["stats"]["findings_total"] == len(doc["findings"])
        for f in doc["findings"]:
            # LPA-15: a confirmed finding carries an executed reproduction.
            if f["status"] == "confirmed":
                assert f.get("executed_reproduction", "").strip()
            # LPA-14: every critical/high finding carries ≥2 refuters.
            if f["severity"] in ("critical", "high"):
                assert len(f.get("refuters", [])) >= 2


class TestCleanDisposition:
    def test_no_confirmed_serious_is_clean_exit_0(self, tmp_path):
        result = dispatch_merge_review_gate(
            review_input=_clean_doc(),
            feature="FEAT-CLEAN",
            config=ReviewGateConfig(enabled=True),
            record_dir=str(tmp_path),
        )
        assert result.outcome == "clean"
        assert result.blocked is False
        assert result.exit_code == 0
        assert result.confirmed_serious == ()
        # The record is still written on a clean pass (durable audit trail).
        assert Path(result.record_ref).exists()
        doc = yaml.safe_load(Path(result.record_ref).read_text())
        # The refuted critical is recorded but not a confirmed serious finding.
        f02 = next(f for f in doc["findings"] if f["id"] == "F-02")
        assert f02["status"] == "refuted"


class TestReviewIdAndSubjectDerivation:
    def test_review_id_and_subject_default_from_feature(self, tmp_path):
        doc = {
            "findings": [
                {
                    "id": "F-01",
                    "dimension": "correctness",
                    "severity": "low",
                    "summary": "trivia",
                    "executed_reproduction": "ran it",
                }
            ]
        }
        result = dispatch_merge_review_gate(
            review_input=doc,
            feature="FEAT-XYZ",
            config=ReviewGateConfig(enabled=True),
            record_dir=str(tmp_path),
        )
        assert result.review_id == "FEAT-XYZ-merge-review"
        rec = yaml.safe_load(Path(result.record_ref).read_text())
        assert rec["subject"] == {"kind": "merge", "ref": "FEAT-XYZ"}


class TestLoadReviewInput:
    def test_round_trip_from_path(self, tmp_path, dd4f_doc):
        p = tmp_path / "in.json"
        p.write_text(json.dumps(dd4f_doc), encoding="utf-8")
        assert load_review_input(p) == dd4f_doc

    def test_missing_file_raises_loud(self, tmp_path):
        with pytest.raises(ReviewInputError):
            load_review_input(tmp_path / "nope.json")

    def test_malformed_json_raises_loud(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ReviewInputError):
            load_review_input(p)

    def test_non_object_raises_loud(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ReviewInputError):
            load_review_input(p)
