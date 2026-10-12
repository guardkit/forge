"""The merge press for an offer with a review preview (web-readiness W7).

An offer whose details carry ``review_evidence`` may be approved only while
its preview is usable. The press asks the review-preview service before the
decision row is written; a refusal is written onto the build's publication
record as a refusal — never as an approve or reject decision — so the card
can still be rejected. Once a decision has ended (rejected, or the approved
press finished) the preview is released.

Every offer without ``review_evidence`` is pressed exactly as before, and the
service is never asked about it.

The service is the stand-in in ``tests/forge/_a_stand_in_review_preview.py``;
the repository, the build system and the deploy stage are the same stand-ins
the merge executor's own tests use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
)
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
)
from tests.forge._a_stand_in_review_preview import (
    PREVIEW_TREE,
    StandInReviewPreview,
    an_evidence,
)
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    BUILD_ID,
    CORRELATION,
    FEATURE_ID,
    MAIN_SHA,
    REPO,
    _deps,
    _drain,
    _ensure_build,
    _envelope,
    _legs,
    _receipts_env,
    _stage_ids,
    _write_offer,
    config,
    pool,
    repo_root,
)

PRESSED_AT = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def _write_previewed_offer(pool, *, state: str = "ready") -> dict[str, Any]:
    """The durable offer a card with a preview leaves, as the offer writes it."""
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    evidence = an_evidence(
        state, candidate_sha="c" * 40, candidate_tree="d" * 40
    ).model_dump(mode="json")
    now = datetime.now(timezone.utc)
    pool.record_stage(
        StageLogEntry(
            build_id=BUILD_ID,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
            status="GATED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={
                MERGE_OFFER_DETAILS_KEY: {
                    "request_id": f"merge-{BUILD_ID}",
                    "correlation_id": CORRELATION,
                    "feature_id": FEATURE_ID,
                    "repo": REPO,
                    "expect_main_sha": MAIN_SHA,
                    "baseline_failing": None,
                    "review_evidence": evidence,
                    "preview_tree": PREVIEW_TREE,
                }
            },
        )
    )
    return evidence


def _consumer(config, pool, preview: StandInReviewPreview | None):
    deps, publisher, gk, dp = _deps(config, pool)
    deps.review_preview = preview
    deps.clock = lambda: PRESSED_AT
    return MergeApprovalConsumer(deps), publisher, gk, dp


def _the_record(pool) -> tuple[Any, ...] | None:
    return pool.connection.execute(
        "SELECT decided_by, result, lines_json, j_commit "
        "FROM publication_records WHERE build_id = ?",
        (BUILD_ID,),
    ).fetchone()


# ---------------------------------------------------------------------------
# A refused approval is a refusal, and Reject still works afterwards
# ---------------------------------------------------------------------------


class TestAnApprovalThePreviewRefuses:
    @pytest.mark.parametrize(
        "why",
        [
            "the preview was not prepared (state failed)",
            "the preview and its Approve window ended at 2026-10-13 12:00 UTC",
            "the preview's evidence folder is missing",
        ],
    )
    @pytest.mark.asyncio
    async def test_it_is_written_down_as_a_refusal_and_nothing_runs(
        self, config, pool, why: str
    ) -> None:
        evidence = _write_previewed_offer(pool)
        preview = StandInReviewPreview(refusal=why)
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        # The service was asked about this offer's own evidence, now.
        assert preview.refusal_questions == [(evidence, PRESSED_AT)]
        # Nothing ran and no decision was recorded — neither approve nor reject.
        assert gk.calls == []
        assert dp.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)
        assert publisher.reports == []
        # The refusal is on the build's publication record, with who and why.
        record = _the_record(pool)
        assert record is not None
        assert record[0] == "rich"
        assert record[1] == "publication pending"
        assert record[3] is None
        lines = json.loads(record[2])
        assert [(line["kind"], line["step"]) for line in lines] == [("done", "join")]
        assert lines[0]["detail"]["joined"] is False
        assert lines[0]["detail"]["refusal"] == f"the approval was refused: {why}"
        # The preview is released: it can never be approved now.
        assert preview.released == [
            {
                "feature_id": FEATURE_ID,
                "build_id": BUILD_ID,
                "reason": f"approval refused: {why}",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_later_reject_still_works(self, config, pool) -> None:
        _write_previewed_offer(pool, state="failed")
        preview = StandInReviewPreview(refusal="the preview was not prepared")
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        await consumer.handle_envelope(_envelope(decision="reject"))
        await _drain(consumer)

        decisions = [
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ]
        assert len(decisions) == 1
        assert decisions[0].status == "SKIPPED"
        assert decisions[0].details["merge_decision"]["decision"] == "reject"
        assert [r.result for r in publisher.reports] == ["rejected"]
        assert gk.calls == []
        assert [r["reason"] for r in preview.released] == [
            "approval refused: the preview was not prepared",
            "rejected",
        ]

    @pytest.mark.asyncio
    async def test_no_service_here_refuses_rather_than_passes(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool)
        consumer, publisher, gk, dp = _consumer(config, pool, None)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)
        lines = json.loads(_the_record(pool)[2])
        assert "the preview service is not running here" in (
            lines[0]["detail"]["refusal"]
        )

    @pytest.mark.asyncio
    async def test_a_check_that_raises_refuses_rather_than_passes(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool)

        class _Raises(StandInReviewPreview):
            def approval_refusal(self, review_evidence, *, now):
                raise OSError("disk gone")

        preview = _Raises()
        consumer, publisher, gk, dp = _consumer(config, pool, preview)
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)
        lines = json.loads(_the_record(pool)[2])
        assert lines[0]["detail"]["refusal"] == (
            "the approval was refused: the preview could not be checked (OSError)"
        )


# ---------------------------------------------------------------------------
# A usable preview is approved, and released once the press has finished
# ---------------------------------------------------------------------------


class TestAnApprovalThePreviewAllows:
    @pytest.mark.asyncio
    async def test_the_press_runs_and_the_preview_is_released_after(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool)
        preview = StandInReviewPreview(preview_tree=PREVIEW_TREE)
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        decision = next(
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        )
        assert decision.status == "PASSED"
        assert len(publisher.reports) == 1
        result = publisher.reports[0].result
        assert preview.released == [
            {
                "feature_id": FEATURE_ID,
                "build_id": BUILD_ID,
                "reason": f"approval ended: {result}",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_release_that_fails_never_costs_the_decision(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool)
        preview = StandInReviewPreview(release_raises=RuntimeError("slot gone"))
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope(decision="reject"))
        await _drain(consumer)

        assert [r.result for r in publisher.reports] == ["rejected"]
        assert len(preview.released) == 1


# ---------------------------------------------------------------------------
# An offer without a preview: the service is never asked
# ---------------------------------------------------------------------------


class TestAnOfferWithoutAPreviewIsPressedAsBefore:
    @pytest.mark.asyncio
    async def test_approve_never_asks_the_service(self, config, pool) -> None:
        _write_offer(pool)
        preview = StandInReviewPreview(refusal="would refuse if it were asked")
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert preview.refusal_questions == []
        assert preview.released == []
        assert len(gk.calls) == 1
        assert _legs(dp) == ["candidate_check", "candidate_down"]
        assert MERGE_DECISION_TARGET_IDENTIFIER in _stage_ids(pool)
        assert [r.result for r in publisher.reports] == ["publication-pending"]

    @pytest.mark.asyncio
    async def test_reject_never_asks_the_service(self, config, pool) -> None:
        _write_offer(pool)
        preview = StandInReviewPreview()
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope(decision="reject"))
        await _drain(consumer)

        assert preview.refusal_questions == []
        assert preview.released == []
        assert [r.result for r in publisher.reports] == ["rejected"]
