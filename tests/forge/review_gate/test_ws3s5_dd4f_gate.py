"""WS3-S5 GATE — the stage runs end-to-end on the DD4F exemplar feature.

This is the acceptance test for the S5 row: the practiced adversarial merge
workflow, replayed on ONE real agent-built feature (the FEAT-SPL-002 / DD4F
post-merge review — 16/16 confirmed, 0/32 refutations), produces a valid F14
record and enforces refuted-by-default. It exercises the whole machinery:
packet → reviewer fan-out → refuted-by-default assembly → F14 emission →
guardkit validation (when the CLI is present) → disposition.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from forge.config.models import ReviewGateConfig
from forge.review_gate.models import (
    RawFinding,
    RefuterVote,
    ReviewSubject,
    raw_findings_from_input,
)
from forge.review_gate.record import validate_review_findings
from forge.review_gate.reviewer import ReviewerInvoker
from forge.review_gate.stage import MergeReviewGateRunner, build_review_packet

FIXTURE = Path(__file__).parent / "fixtures" / "dd4f_review_input.json"
GUARDKIT_ABSENT = (
    shutil.which("guardkit") is None and shutil.which("guardkit-py") is None
)


class DD4FReplayInvoker:
    """A reviewer seat that replays the DD4F fan-out from the fixture.

    Reviewers propose the fixture findings for their dimension (refuter votes
    stripped — the runner re-dispatches them); refuters return not_refuted,
    replaying the DD4F record's 0/32 refutations (every finding survived).
    """

    def __init__(self, doc: dict) -> None:
        raws = raw_findings_from_input(doc)
        self._by_dim: dict[str, list[RawFinding]] = {}
        for r in raws:
            # Strip refuter votes — the gate owns refuter dispatch (LPA-14).
            self._by_dim.setdefault(r.dimension, []).append(
                RawFinding(
                    id=r.id,
                    dimension=r.dimension,
                    severity=r.severity,
                    summary=r.summary,
                    executed_reproduction=r.executed_reproduction,
                )
            )

    def review_dimension(self, packet, dimension):
        return tuple(self._by_dim.get(dimension, ()))

    def refute_finding(self, packet, finding, refuter_id):
        # DD4F: every refutation attempt failed (0/32 succeeded).
        return RefuterVote(who=refuter_id, verdict="not_refuted")


@pytest.fixture
def dd4f_doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def dd4f_result(tmp_path, dd4f_doc):
    config = ReviewGateConfig(enabled=True)
    dims = tuple(dict.fromkeys(f["dimension"] for f in dd4f_doc["findings"]))
    packet = build_review_packet(
        review_id=dd4f_doc["review_id"],
        subject=ReviewSubject(**dd4f_doc["subject"]),
        feature="FEAT-SPL-002",
        config=config.model_copy(update={"dimensions": list(dims)}),
    )
    runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
    result = runner.run_gate(packet, DD4FReplayInvoker(dd4f_doc))
    return result


class TestGateRunsEndToEnd:
    def test_isinstance_reviewer_protocol(self, dd4f_doc):
        assert isinstance(DD4FReplayInvoker(dd4f_doc), ReviewerInvoker)

    def test_all_findings_with_repro_confirmed(self, dd4f_result):
        # Every DD4F finding carries an executed reproduction ⇒ all confirmed.
        rec = dd4f_result.record
        assert rec.stats.findings_total == 7
        assert rec.stats.confirmed == 7
        assert rec.stats.refuted == 0

    def test_refutations_attempted_matches_serious_count(self, dd4f_result):
        # 5 serious (1 critical + 3 high + ... ) findings × 2 refuters each.
        rec = dd4f_result.record
        serious = [f for f in rec.findings if f.severity in ("critical", "high")]
        assert rec.stats.refutations_attempted == len(serious) * 2

    def test_blocked_on_confirmed_serious(self, dd4f_result):
        assert dd4f_result.outcome == "blocked"
        assert dd4f_result.disposition_required is True
        assert dd4f_result.confirmed_serious >= 1

    def test_record_written(self, dd4f_result):
        assert Path(dd4f_result.record_ref).exists()


class TestRefutedByDefaultOnDD4F:
    """The gate-critical property, demonstrated on the real fixture."""

    def test_repro_less_finding_cannot_reach_confirmed(self, tmp_path, dd4f_doc):
        # Poison the fixture: strip the CRITICAL finding's executed reproduction.
        # The whole review still runs, but that finding is STRUCTURALLY unable to
        # reach confirmed — reading is not a verdict (LPA-15).
        doc = json.loads(json.dumps(dd4f_doc))
        for f in doc["findings"]:
            if f["id"] == "F-01":
                f.pop("executed_reproduction", None)
        config = ReviewGateConfig(enabled=True)
        dims = tuple(dict.fromkeys(f["dimension"] for f in doc["findings"]))
        packet = build_review_packet(
            review_id="dd4f-poisoned",
            subject=ReviewSubject(**doc["subject"]),
            feature="FEAT-SPL-002",
            config=config.model_copy(update={"dimensions": list(dims)}),
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        result = runner.run_gate(packet, DD4FReplayInvoker(doc))
        f01 = next(f for f in result.record.findings if f.id == "F-01")
        assert f01.status == "refuted"  # no repro ⇒ cannot be confirmed
        assert result.record.stats.confirmed == 6  # one fewer than the clean 7


class TestGuardkitValidation:
    @pytest.mark.skipif(GUARDKIT_ABSENT, reason="guardkit CLI not on PATH")
    def test_emitted_f14_validates_via_guardkit(self, dd4f_result):
        result = validate_review_findings(dd4f_result.record_ref)
        assert result.ok, (
            f"guardkit rejected the DD4F F14 record "
            f"(rc={result.returncode}): {result.stderr or result.stdout}"
        )

    def test_emitted_yaml_is_f14_shaped(self, dd4f_result):
        doc = yaml.safe_load(Path(dd4f_result.record_ref).read_text())
        assert doc["format_version"] == "1.0"
        assert doc["review_id"] == "FEAT-SPL-002-DD4F-postmerge"
        assert doc["subject"] == {"kind": "merge", "ref": "d13d88f..1fcb72c"}
        # Every confirmed finding carries an executed reproduction (LPA-15).
        for f in doc["findings"]:
            if f["status"] == "confirmed":
                assert f.get("executed_reproduction", "").strip()
            if f["severity"] in ("critical", "high"):
                assert len(f.get("refuters", [])) >= 2
