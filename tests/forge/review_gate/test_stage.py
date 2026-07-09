"""The merge-gate stage runner — fan-out orchestration + disposition (WS3-S5)."""

from __future__ import annotations

import pytest

from forge.config.models import ReviewGateConfig
from forge.review_gate.models import (
    RawFinding,
    RefuterVote,
    ReviewPacket,
    ReviewSubject,
)
from forge.review_gate.reviewer import ReviewerUnavailable, UnconfiguredReviewerInvoker
from forge.review_gate.stage import (
    MergeReviewGateRunner,
    build_review_packet,
)

SUBJ = ReviewSubject(kind="merge", ref="abc..def")


class FixtureInvoker:
    """A reviewer seat that replays scripted proposals + refuter verdicts."""

    def __init__(self, proposals_by_dim, refute_verdict="not_refuted"):
        self._by_dim = proposals_by_dim
        self._refute_verdict = refute_verdict
        self.refuter_calls: list[str] = []

    def review_dimension(self, packet, dimension):
        return tuple(self._by_dim.get(dimension, ()))

    def refute_finding(self, packet, finding, refuter_id):
        self.refuter_calls.append(refuter_id)
        return RefuterVote(who=refuter_id, verdict=self._refute_verdict)


def _packet(config, dims=("correctness", "spec-fidelity")):
    cfg = config
    return build_review_packet(
        review_id="stage-test",
        subject=SUBJ,
        feature="FEAT-X",
        config=cfg.model_copy(update={"dimensions": list(dims)}),
    )


def _prop(fid, dimension, severity, repro="ran X"):
    return RawFinding(
        id=fid,
        dimension=dimension,
        severity=severity,
        summary=f"{fid} summary",
        executed_reproduction=repro,
    )


class TestFanOut:
    def test_runner_dispatches_two_refuters_per_serious_finding(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        invoker = FixtureInvoker(
            {
                "correctness": [_prop("F-01", "correctness", "critical")],
                "spec-fidelity": [_prop("F-02", "spec-fidelity", "high")],
            }
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        result = runner.run_gate(_packet(config), invoker)
        # Two serious findings × 2 refuters each = 4 refuter dispatches.
        assert len(invoker.refuter_calls) == 4
        assert result.record.stats.refutations_attempted == 4

    def test_low_severity_gets_no_refuters(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        invoker = FixtureInvoker({"correctness": [_prop("F-01", "correctness", "low")]})
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        runner.run_gate(_packet(config), invoker)
        assert invoker.refuter_calls == []

    def test_min_refuters_config_honoured(self, tmp_path):
        config = ReviewGateConfig(enabled=True, min_refuters=3)
        invoker = FixtureInvoker(
            {"correctness": [_prop("F-01", "correctness", "high")]}
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        runner.run_gate(_packet(config), invoker)
        assert len(invoker.refuter_calls) == 3


class TestDisposition:
    def test_confirmed_serious_blocks(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        invoker = FixtureInvoker(
            {"correctness": [_prop("F-01", "correctness", "critical")]},
            refute_verdict="not_refuted",  # survives ⇒ confirmed
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        result = runner.run_gate(_packet(config), invoker)
        assert result.outcome == "blocked"
        assert result.disposition_required is True
        assert result.confirmed_serious == 1

    def test_refuted_serious_is_clean(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        invoker = FixtureInvoker(
            {"correctness": [_prop("F-01", "correctness", "critical")]},
            refute_verdict="refuted",  # killed ⇒ not confirmed
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        result = runner.run_gate(_packet(config), invoker)
        assert result.outcome == "clean"
        assert result.disposition_required is False

    def test_repro_less_serious_cannot_block(self, tmp_path):
        # A reviewer that proposes a serious finding WITHOUT an executed repro:
        # it can never be confirmed, so it never blocks (refuted-by-default).
        config = ReviewGateConfig(enabled=True)
        invoker = FixtureInvoker(
            {"correctness": [_prop("F-01", "correctness", "critical", repro=None)]},
            refute_verdict="not_refuted",
        )
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        result = runner.run_gate(_packet(config), invoker)
        assert result.outcome == "clean"
        assert result.record.findings[0].status == "refuted"


class TestEscalation:
    def test_escalate_invoked_on_block(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        captured = []
        runner = MergeReviewGateRunner(
            config=config,
            record_root=str(tmp_path),
            escalate=lambda rec, ref: captured.append((rec.review_id, ref)),
        )
        invoker = FixtureInvoker(
            {"correctness": [_prop("F-01", "correctness", "critical")]}
        )
        result = runner.run_gate(_packet(config), invoker)
        assert result.escalated is True
        assert captured and captured[0][0] == "stage-test"

    def test_escalate_not_invoked_on_clean(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        captured = []
        runner = MergeReviewGateRunner(
            config=config,
            record_root=str(tmp_path),
            escalate=lambda rec, ref: captured.append(ref),
        )
        invoker = FixtureInvoker({"correctness": [_prop("F-01", "correctness", "low")]})
        result = runner.run_gate(_packet(config), invoker)
        assert result.escalated is False
        assert captured == []


class TestReviewerUnavailable:
    def test_unconfigured_invoker_raises_loudly(self, tmp_path):
        config = ReviewGateConfig(enabled=True)
        runner = MergeReviewGateRunner(config=config, record_root=str(tmp_path))
        packet = ReviewPacket(
            review_id="t",
            subject=SUBJ,
            feature="F",
            dimensions=("correctness",),
        )
        with pytest.raises(ReviewerUnavailable):
            runner.run_gate(packet, UnconfiguredReviewerInvoker())
