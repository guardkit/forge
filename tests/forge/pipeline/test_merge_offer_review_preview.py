"""The merge card with a review preview (web-readiness W7) — offline.

A project whose deploy profile switches a preview on has its candidate joined
onto main and stood up before the card is offered, and the card carries the
evidence: on the durable offer row, on the approval request's details and on
the ``build-paused`` envelope Jarvis renders. A preview that could not be
prepared still reaches the card, with the reason and without Approve.

Every other offer is byte for byte what it was: the routine card and the fix
journey's checkpoint card are compared with a golden copy taken from Forge
``cbd1fc48`` before any of this existed, plus the details step 1 added to
every card on 24 October 2026 (the tasks not included and the change).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import MessageEnvelope

from forge.cli._serve_gate_activation import merge_card_words
from forge.deploy.review_preview import PreviewRequest
from forge.pipeline.merge_offer import MERGE_OFFER_DETAILS_KEY, MergeOfferService
from tests.forge._a_stand_in_review_preview import (
    PREVIEW_TREE,
    StandInReviewPreview,
)
from tests.forge.pipeline.test_merge_offer import (  # noqa: F401 - fixtures
    BUILD_ID,
    CANDIDATE_SHA,
    CANDIDATE_TREE,
    CORRELATION,
    FEATURE_ID,
    REPO,
    _CandidatePins,
    _event,
    _insert_build,
    _offer_rows,
    _Recorder,
    config,
    pool,
    repo_root,
)

#: Where the golden copy of the two cards without a preview is kept.
GOLDEN = Path(__file__).parents[1] / "fixtures" / "merge_offer_without_a_preview.json"

#: The fix journey's build, carded through the same offer.
CHECKPOINT_BUILD_ID = "build-FEAT-MO1-20260825"
CHECKPOINT_CORRELATION = "corr-mo-2"

FIXED_NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


def _service(config, pool, recorder: _Recorder, **kwargs: Any) -> MergeOfferService:
    """The offer with every reader pinned, so only the preview can differ."""

    async def _git_head(_repo_root: Path) -> str | None:
        return "mainsha1234"

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        git_surface=lambda _repo, _root: _CandidatePins(),
        baseline_reader=lambda _build_id: None,
        finished_feature_reader=lambda *_a, **_k: (
            None,
            None,
            "nothing was exported for this build",
        ),
        scope_pass=lambda **_k: None,
        after_deploy_check_reader=lambda _pool, _row: None,
        clock=lambda: FIXED_NOW,
        **kwargs,
    )


def _what_went_out(recorder: _Recorder, pool, build_id: str) -> dict[str, Any]:
    """Everything one card put on the wire and on its durable row."""
    assert [kind for kind, _ in recorder.events] == ["approval", "paused"]
    subject, body = recorder.events[0][1]
    envelope = MessageEnvelope.model_validate_json(body)
    paused = recorder.events[1][1].model_dump(mode="json")
    # The optional field a newer nats-core adds is left out of the comparison:
    # it is the library's default, not anything this offer chose, and the
    # tests below say separately that it stays empty without a preview.
    paused.pop("review_evidence", None)
    # Likewise the merge card block (16 October 2026), which only a previewed
    # card carries.
    assert paused.pop("merge_card", None) is None
    return {
        "subject": subject,
        "correlation_id": envelope.correlation_id,
        "approval": envelope.payload,
        "paused": paused,
        "latch": _offer_rows(pool, build_id)[0].details,
    }


async def what_the_two_cards_put_out(config, pool, **kwargs: Any) -> dict[str, Any]:
    """The routine card and the fix journey's checkpoint card, as sent."""
    _insert_build(pool)
    _insert_build(
        pool,
        build_id=CHECKPOINT_BUILD_ID,
        correlation_id=CHECKPOINT_CORRELATION,
    )
    routine = _Recorder()
    await _service(config, pool, routine, **kwargs).maybe_offer(_event())
    checkpoint = _Recorder()
    await _service(config, pool, checkpoint, **kwargs).offer(
        build_id=CHECKPOINT_BUILD_ID,
        feature_id=FEATURE_ID,
        card_words=lambda branch, _merge_branch: merge_card_words(
            feature_id=FEATURE_ID, branch=branch
        ),
    )
    return {
        "routine": _what_went_out(routine, pool, BUILD_ID),
        "checkpoint": _what_went_out(checkpoint, pool, CHECKPOINT_BUILD_ID),
        "paused_review_evidence": [
            getattr(rec.events[1][1], "review_evidence", None)
            for rec in (routine, checkpoint)
        ],
    }


# ---------------------------------------------------------------------------
# Without a preview: byte for byte what it was
# ---------------------------------------------------------------------------


class TestAnOfferWithoutAPreviewIsUnchanged:
    @pytest.mark.asyncio
    async def test_no_preview_service_matches_the_golden_copy(self, config, pool) -> None:
        sent = await what_the_two_cards_put_out(config, pool)
        assert sent.pop("paused_review_evidence") == [None, None]
        # Compared as text, key order included.
        assert json.dumps(sent, indent=2) + "\n" == GOLDEN.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_project_without_a_preview_matches_the_golden_copy(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview(enabled=False)
        sent = await what_the_two_cards_put_out(config, pool, review_preview=preview)
        assert sent.pop("paused_review_evidence") == [None, None]
        assert json.dumps(sent, indent=2) + "\n" == GOLDEN.read_text(encoding="utf-8")
        # It was asked about the project, and nothing was prepared.
        assert preview.asked_about == [REPO, REPO]
        assert preview.requests == []


# ---------------------------------------------------------------------------
# With a preview
# ---------------------------------------------------------------------------


async def _one_card(config, pool, preview: StandInReviewPreview) -> _Recorder:
    _insert_build(pool)
    recorder = _Recorder()
    await _service(config, pool, recorder, review_preview=preview).maybe_offer(_event())
    return recorder


class TestAnOfferWithAPreview:
    @pytest.mark.asyncio
    async def test_the_preview_is_prepared_for_the_pinned_candidate(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview()
        await _one_card(config, pool, preview)
        assert len(preview.requests) == 1
        asked = preview.requests[0]
        assert isinstance(asked, PreviewRequest)
        assert asked.project == REPO
        assert asked.feature_id == FEATURE_ID
        assert asked.build_id == BUILD_ID
        assert asked.branch == f"autobuild/{FEATURE_ID}"
        assert asked.candidate_sha == CANDIDATE_SHA
        assert asked.candidate_tree == CANDIDATE_TREE
        assert asked.expect_main_sha == "mainsha1234"
        # Nothing was recorded for this build, so main is the target.
        assert asked.target_branch == "main"

    @pytest.mark.asyncio
    async def test_a_ready_preview_rides_the_details_the_row_and_the_paused_card(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview()
        recorder = await _one_card(config, pool, preview)

        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["review_evidence"]["state"] == "ready"
        assert details["review_evidence"]["candidate_sha"] == CANDIDATE_SHA
        assert details["preview_tree"] == PREVIEW_TREE
        latch = _offer_rows(pool)[0].details[MERGE_OFFER_DETAILS_KEY]
        assert latch["review_evidence"] == details["review_evidence"]
        assert latch["preview_tree"] == PREVIEW_TREE

        paused = recorder.events[1][1]
        assert paused.review_evidence is not None
        assert paused.review_evidence.model_dump(mode="json") == details["review_evidence"]
        assert paused.rationale.endswith("\nPreview ready — see the buttons below.")
        # Everything the card said before is still there, in the same order.
        assert "Approve = merge into main" in paused.rationale

    @pytest.mark.asyncio
    async def test_a_failed_preview_still_offers_the_card_without_approve(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview(state="failed")
        recorder = await _one_card(config, pool, preview)

        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["review_evidence"]["state"] == "failed"
        assert details["review_evidence"]["failure_reason"] == (
            "the journey did not finish"
        )
        assert details["preview_tree"] == PREVIEW_TREE
        paused = recorder.events[1][1]
        assert paused.review_evidence.state == "failed"
        assert paused.rationale.endswith(
            "\nPreview could not be prepared: the journey did not finish; "
            "Approve is not available."
        )
        # The card is latched like any other, so it is never offered twice.
        assert len(_offer_rows(pool)) == 1

    @pytest.mark.asyncio
    async def test_the_checkpoint_card_says_the_same_line(self, config, pool) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        await _service(
            config, pool, recorder, review_preview=StandInReviewPreview()
        ).offer(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            card_words=lambda branch, _merge_branch: merge_card_words(
                feature_id=FEATURE_ID, branch=branch
            ),
        )
        paused = recorder.events[1][1]
        assert paused.rationale == (
            merge_card_words(feature_id=FEATURE_ID, branch=f"autobuild/{FEATURE_ID}")
            + "\nPreview ready — see the buttons below."
        )

    @pytest.mark.asyncio
    async def test_a_card_already_on_record_prepares_no_second_preview(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview()
        await _one_card(config, pool, preview)
        again = _Recorder()
        await _service(config, pool, again, review_preview=preview).maybe_offer(
            _event()
        )
        assert len(preview.requests) == 1
        assert again.events == []

    @pytest.mark.asyncio
    async def test_a_preparation_that_raises_offers_nothing_and_releases(
        self, config, pool
    ) -> None:
        preview = StandInReviewPreview(prepare_raises=RuntimeError("boom"))
        recorder = await _one_card(config, pool, preview)
        assert recorder.events == []
        assert _offer_rows(pool) == []
        assert preview.released == [
            {
                "feature_id": FEATURE_ID,
                "build_id": BUILD_ID,
                "reason": "the preview could not be prepared (RuntimeError)",
            }
        ]

    @pytest.mark.asyncio
    async def test_whether_a_project_has_a_preview_is_asked_off_the_event_loop(
        self, config, pool
    ) -> None:
        """The real ``enabled_for`` reads the deploy profile, which can block."""
        import threading

        asked_on: list[threading.Thread] = []

        class _Recording(StandInReviewPreview):
            def enabled_for(self, project: str) -> bool:
                asked_on.append(threading.current_thread())
                return super().enabled_for(project)

        await _one_card(config, pool, _Recording())
        assert len(asked_on) == 1
        assert asked_on[0] is not threading.main_thread()


# ---------------------------------------------------------------------------
# Preparation runs before the durable row (coach item 6)
# ---------------------------------------------------------------------------


class TestPreparationBeforeTheRow:
    @pytest.mark.asyncio
    async def test_a_second_offer_while_preparing_prepares_nothing(
        self, config, pool
    ) -> None:
        import asyncio

        started = asyncio.Event()
        go_on = asyncio.Event()

        class _Slow(StandInReviewPreview):
            async def prepare(self, req: Any) -> Any:
                started.set()
                await go_on.wait()
                return await super().prepare(req)

        _insert_build(pool)
        preview = _Slow()
        first = _Recorder()
        service = _service(config, pool, first, review_preview=preview)

        offering = asyncio.create_task(service.maybe_offer(_event()))
        await started.wait()
        again = await service.offer(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            card_words=lambda _b, _m: "words",
        )
        go_on.set()
        await offering

        assert again is False
        assert len(preview.requests) == 1
        assert len(_offer_rows(pool)) == 1
        assert [kind for kind, _ in first.events] == ["approval", "paused"]

    @pytest.mark.asyncio
    async def test_a_card_that_could_not_be_published_releases_its_preview(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        preview = StandInReviewPreview()
        recorder = _Recorder(approval_raises=True)
        await _service(config, pool, recorder, review_preview=preview).maybe_offer(
            _event()
        )

        assert len(_offer_rows(pool)) == 1  # latched, never retried
        assert preview.released == [
            {
                "feature_id": FEATURE_ID,
                "build_id": BUILD_ID,
                "reason": "the merge card could not be published",
            }
        ]


@pytest.mark.asyncio
async def test_a_service_that_could_not_start_puts_its_reason_on_the_card(
    config, pool
) -> None:
    from forge.deploy.review_preview import UnavailableReviewPreview

    _insert_build(pool)
    recorder = _Recorder()
    await _service(
        config, pool, recorder, review_preview=UnavailableReviewPreview("OSError: x")
    ).maybe_offer(_event())

    paused = recorder.events[1][1]
    assert paused.review_evidence.state == "failed"
    assert paused.rationale.endswith(
        "\nPreview could not be prepared: previews are switched on but the preview "
        "service could not start: OSError: x; Approve is not available."
    )
