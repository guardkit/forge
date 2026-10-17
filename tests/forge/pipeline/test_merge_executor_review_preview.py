"""The merge press for an offer with a review preview (web-readiness W7).

An offer whose details carry ``review_evidence`` may be approved only while
its preview is usable. The press asks the review-preview service before the
decision row is written; a refusal is written onto the build's publication
record as a refusal — never as an approve or reject decision — so the card
can still be rejected. Once a decision has ended (rejected, or the approved
press finished) the preview is released.

The press also refuses when the tree it joins now is not the tree that was
previewed — main moved since the preview — and merges nothing (R3, asked for
by the owner of candidate identity).

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
    execute_merge_deploy,
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
    _git,
    _JoinsForReal,
    _legs,
    _receipts_env,
    _repair_rows,
    _stage_ids,
    _tree_of,
    _write_offer,
    pool,
    repo_root,
)
from tests.forge.pipeline.test_merge_executor import _envelope as _an_envelope
from tests.forge.recorded_fetch_address import origin_of

#: Who approves in this file (a neutral name; the shared fixtures' own
#: approver is left as it is).
APPROVER = "the-approver"


@pytest.fixture
def config(repo_root):
    from forge.config.models import ForgeConfig

    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {REPO: str(repo_root)},
                "fetch_addresses": {REPO: origin_of(repo_root)},
            },
            "approval": {"expected_approver": APPROVER},
            "merge_executor": {"enabled": True},
        }
    )


def _envelope(**kwargs: Any) -> Any:
    kwargs.setdefault("decided_by", APPROVER)
    return _an_envelope(**kwargs)

PRESSED_AT = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def _write_previewed_offer(
    pool, *, state: str = "ready", preview_tree: str = PREVIEW_TREE
) -> dict[str, Any]:
    """The durable offer a card with a preview leaves, as the offer writes it."""
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    evidence = an_evidence(
        state,
        candidate_sha="c" * 40,
        candidate_tree="d" * 40,
        # An offer that names no previewed tree still carries a valid record.
        preview_tree=preview_tree or PREVIEW_TREE,
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
                    "preview_tree": preview_tree,
                }
            },
        )
    )
    return evidence


def _consumer(config, pool, preview: StandInReviewPreview | None, **kwargs: Any):
    deps, publisher, gk, dp = _deps(config, pool, **kwargs)
    deps.review_preview = preview
    deps.clock = lambda: PRESSED_AT
    return MergeApprovalConsumer(deps), publisher, gk, dp


def _the_join_tree(repo_root) -> str:
    """The tree the merge word's join makes of main and the build's branch now.

    The same tree the review preview records as ``preview_tree``: the
    candidate joined onto the main the offer pinned.
    """
    return _git(
        repo_root, "merge-tree", "--write-tree", "main", f"autobuild/{FEATURE_ID}"
    )


def _main_moves_on_the_remote(repo_root) -> str:
    """Another feature lands on main after the preview was prepared."""
    _git(repo_root, "checkout", "-q", "main")
    (repo_root / "moved.txt").write_text("another feature landed\n", encoding="utf-8")
    _git(repo_root, "add", "moved.txt")
    _git(repo_root, "commit", "-q", "-m", "main moved after the preview")
    _git(repo_root, "push", "-q", "origin", "main")
    return _git(repo_root, "rev-parse", "main")


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
        assert record[0] == APPROVER
        assert record[1] == "publication pending"
        assert record[3] is None
        lines = json.loads(record[2])
        assert [(line["kind"], line["step"]) for line in lines] == [("done", "join")]
        assert lines[0]["detail"]["joined"] is False
        assert lines[0]["detail"]["refusal"] == f"the approval was refused: {why}"
        # The preview this offer names is released: it can never be approved
        # now. Only that one, by run id (16 October 2026), never every preview
        # of the build, so a refusal during a rebuild cannot tear it down.
        assert preview.released == []
        assert preview.released_runs == [
            {"run_id": evidence["run_id"], "reason": f"approval refused: {why}"}
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
        assert [r["reason"] for r in preview.released_runs] == [
            "approval refused: the preview was not prepared",
        ]
        # Reject still releases everything for the build.
        assert [r["reason"] for r in preview.released] == ["rejected"]

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
        # A check that could not be made is not a verdict on the preview: it
        # is left standing, so the same card can be approved later.
        assert preview.released == []

    @pytest.mark.asyncio
    async def test_after_a_check_that_could_not_be_made_the_card_can_still_be_approved(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool)
        failures = [OSError("ledger busy")]

        class _BusyOnce(StandInReviewPreview):
            def approval_refusal(self, review_evidence, *, now):
                if failures:
                    raise failures.pop()
                return super().approval_refusal(review_evidence, now=now)

        preview = _BusyOnce()
        consumer, publisher, gk, dp = _consumer(config, pool, preview)
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        assert gk.calls == [] and preview.released == []

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert len(gk.calls) == 1
        assert MERGE_DECISION_TARGET_IDENTIFIER in _stage_ids(pool)


# ---------------------------------------------------------------------------
# A usable preview is approved, and released once the press has finished
# ---------------------------------------------------------------------------


class TestAnApprovalThePreviewAllows:
    @pytest.mark.asyncio
    async def test_the_press_runs_and_the_preview_is_released_after(
        self, config, pool, repo_root
    ) -> None:
        _write_previewed_offer(pool, preview_tree=_the_join_tree(repo_root))
        preview = StandInReviewPreview()
        consumer, publisher, gk, dp = _consumer(
            config, pool, preview, guardkit=_JoinsForReal()
        )

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        decision = next(
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        )
        assert decision.status == "PASSED"
        assert _legs(dp) == ["candidate_check", "candidate_down"]
        assert [r.result for r in publisher.reports] == ["publication-pending"]
        assert preview.released == [
            {
                "feature_id": FEATURE_ID,
                "build_id": BUILD_ID,
                "reason": "approval ended: publication-pending",
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


# ---------------------------------------------------------------------------
# The tree that was previewed is the only tree the approval may merge (R3)
# ---------------------------------------------------------------------------


async def _press(deps, repo_root, **kwargs: Any) -> Any:
    _ensure_build(deps.pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=CORRELATION,
        decided_by=APPROVER,
        **kwargs,
    )


class TestTheTreeThatWasPreviewed:
    @pytest.mark.asyncio
    async def test_main_moved_since_the_preview_is_refused_and_nothing_is_merged(
        self, config, pool, repo_root, tmp_path
    ) -> None:
        """The R3 regression: a card approved after main moved merges nothing."""
        previewed = _the_join_tree(repo_root)
        moved = _main_moves_on_the_remote(repo_root)
        joined = _the_join_tree(repo_root)
        assert joined != previewed
        deps, publisher, gk, dp = _deps(config, pool, guardkit=_JoinsForReal())

        outcome = await _press(deps, repo_root, expected_preview_tree=previewed)

        assert outcome.result == "candidate-refused"
        assert outcome.detail == (
            f"{FEATURE_ID} could not be checked in the sandbox before merging: "
            f"main moved since the preview: previewed tree {previewed}, joined "
            f"tree {joined}; nothing was merged and the branch is kept."
        )
        # The joined commit is the join of the moved main, and its tree is the
        # one named.
        assert _tree_of(repo_root, outcome.gate_before_merge["candidate_sha"]) == joined
        # Nothing was checked, published or deployed; the remote's main is
        # exactly where it moved to; nothing is filed for repair.
        assert dp.calls == []
        assert _git(tmp_path / "origin.git", "rev-parse", "main") == moved
        assert [r.result for r in publisher.reports] == ["candidate-refused"]
        assert _repair_rows(pool) == []
        # The record says nothing was published (15 October 2026), and the
        # hold is put down: the join answered and no leg was sent, so nothing
        # this press started can still be running (FEAT-A9DA, 15 October
        # 2026: the hold was kept and the release idle check waited on it).
        record = _the_record(pool)
        assert record is not None and record[1] == "publication pending"
        assert (
            pool.connection.execute(
                "SELECT lease_holder FROM publication_records WHERE build_id = ?",
                (BUILD_ID,),
            ).fetchone()[0]
            is None
        )

    @pytest.mark.asyncio
    async def test_the_same_tree_is_checked_as_before(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool, guardkit=_JoinsForReal())

        outcome = await _press(
            deps, repo_root, expected_preview_tree=_the_join_tree(repo_root)
        )

        assert outcome.result == "publication-pending"
        assert _legs(dp) == ["candidate_check", "candidate_down"]

    @pytest.mark.asyncio
    async def test_without_a_previewed_tree_a_moved_main_is_joined_as_before(
        self, config, pool, repo_root
    ) -> None:
        _main_moves_on_the_remote(repo_root)
        deps, publisher, gk, dp = _deps(config, pool, guardkit=_JoinsForReal())

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert _legs(dp) == ["candidate_check", "candidate_down"]

    @pytest.mark.asyncio
    async def test_an_approved_card_after_main_moved_is_refused_through_the_press(
        self, config, pool, repo_root
    ) -> None:
        _write_previewed_offer(pool, preview_tree=_the_join_tree(repo_root))
        _main_moves_on_the_remote(repo_root)
        preview = StandInReviewPreview()
        consumer, publisher, gk, dp = _consumer(
            config, pool, preview, guardkit=_JoinsForReal()
        )

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert [r.result for r in publisher.reports] == ["candidate-refused"]
        assert "main moved since the preview" in publisher.reports[0].detail
        assert dp.calls == []
        assert preview.released[-1]["reason"] == "approval ended: candidate-refused"

    @pytest.mark.asyncio
    async def test_an_offer_that_names_no_previewed_tree_is_refused(
        self, config, pool
    ) -> None:
        _write_previewed_offer(pool, preview_tree="")
        preview = StandInReviewPreview()
        consumer, publisher, gk, dp = _consumer(config, pool, preview)

        await consumer.handle_envelope(_envelope())
        await _drain(consumer)

        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)
        lines = json.loads(_the_record(pool)[2])
        assert lines[0]["detail"]["refusal"] == (
            "the approval was refused: the offer does not say which tree was "
            "previewed"
        )


# ---------------------------------------------------------------------------
# The preview joins what the press joins: the remote, never a stale local main
# ---------------------------------------------------------------------------


class _JoinsLikeThePreview(StandInReviewPreview):
    """Prepares with the real preview join (``make_preview_tree``), in the repo."""

    def __init__(self, repo_root) -> None:
        super().__init__()
        self.repo_root = repo_root

    async def prepare(self, req: Any) -> Any:
        from forge.deploy.review_preview import make_preview_tree

        self.requests.append(req)
        laid = make_preview_tree(
            self.repo_root,
            lease_id="pv000000000001",
            onto=req.preview_base_sha,
            candidate=req.candidate_sha,
        )
        assert "refusal" not in laid, laid
        return an_evidence(
            "ready",
            candidate_sha=req.candidate_sha,
            candidate_tree=req.candidate_tree,
            preview_tree=laid["tree"],
        )


def _local_main_lags_the_remote(repo_root) -> tuple[str, str]:
    """Another feature was merged and published; this clone's main never moved."""
    local = _git(repo_root, "rev-parse", "main")
    _git(repo_root, "checkout", "-q", "-b", "elsewhere", "main")
    (repo_root / "other.txt").write_text("another feature\n", encoding="utf-8")
    _git(repo_root, "add", "other.txt")
    _git(repo_root, "commit", "-q", "-m", "another feature merged and published")
    _git(repo_root, "push", "-q", "origin", "elsewhere:main")
    _git(repo_root, "checkout", "-q", "main")
    return local, _git(repo_root, "rev-parse", "elsewhere")


def _the_remote_moves_again(repo_root) -> None:
    _git(repo_root, "checkout", "-q", "elsewhere")
    (repo_root / "third.txt").write_text("a third feature\n", encoding="utf-8")
    _git(repo_root, "add", "third.txt")
    _git(repo_root, "commit", "-q", "-m", "a third feature")
    _git(repo_root, "push", "-q", "origin", "elsewhere:main")
    _git(repo_root, "checkout", "-q", "main")


async def _offer_with_a_preview(config, pool, preview) -> dict[str, Any]:
    from types import SimpleNamespace

    from forge.pipeline.merge_offer import MergeOfferService

    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)

    async def _publish(*_a: Any) -> None:
        return None

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_publish),
        raw_publish=_publish,
        baseline_reader=lambda _b: None,
        finished_feature_reader=lambda *_a, **_k: (None, None, "nothing exported"),
        scope_pass=lambda **_k: None,
        after_deploy_check_reader=lambda _p, _r: None,
        review_preview=preview,
    )
    assert await service.offer(
        build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=lambda _b, _m: "words"
    )
    return next(
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ).details[MERGE_OFFER_DETAILS_KEY]


class TestThePreviewJoinsWhatThePressJoins:
    @pytest.mark.asyncio
    async def test_a_local_main_lagging_the_remote_is_not_refused(
        self, config, pool, repo_root
    ) -> None:
        local, fetched = _local_main_lags_the_remote(repo_root)
        preview = _JoinsLikeThePreview(repo_root)

        offer = await _offer_with_a_preview(config, pool, preview)

        # The preview was joined onto the remote's main — the commit the press
        # will join onto — and since 17 October 2026 the card's pin is that
        # same fetched commit, never the clone's lagging local main.
        assert offer["expect_main_sha"] == fetched != local
        assert offer["preview_base_sha"] == fetched
        assert preview.requests[0].preview_base_sha == fetched
        consumer, publisher, gk, dp = _consumer(
            config, pool, preview, guardkit=_JoinsForReal()
        )
        await consumer.handle_envelope(_envelope(correlation_id=f"corr-{BUILD_ID}"))
        await _drain(consumer)

        assert [r.result for r in publisher.reports] == ["publication-pending"]
        assert _legs(dp) == ["candidate_check", "candidate_down"]

    @pytest.mark.asyncio
    async def test_the_remote_moving_after_the_preview_is_refused(
        self, config, pool, repo_root
    ) -> None:
        _local_main_lags_the_remote(repo_root)
        preview = _JoinsLikeThePreview(repo_root)
        offer = await _offer_with_a_preview(config, pool, preview)
        _the_remote_moves_again(repo_root)
        consumer, publisher, gk, dp = _consumer(
            config, pool, preview, guardkit=_JoinsForReal()
        )

        await consumer.handle_envelope(_envelope(correlation_id=f"corr-{BUILD_ID}"))
        await _drain(consumer)

        assert [r.result for r in publisher.reports] == ["candidate-refused"]
        detail = publisher.reports[0].detail
        assert f"previewed tree {offer['preview_tree']}" in detail
        assert "main moved since the preview" in detail
        assert dp.calls == []


# ---------------------------------------------------------------------------
# Previews on, but the service could not start: every card fails closed (R2)
# ---------------------------------------------------------------------------


class TestThePreviewServiceCouldNotStart:
    @pytest.mark.asyncio
    async def test_the_card_carries_a_failed_preview_and_approval_is_refused(
        self, config, pool, repo_root
    ) -> None:
        from forge.deploy.review_preview import UnavailableReviewPreview

        stand_in = UnavailableReviewPreview("RuntimeError: no ledger")
        offer = await _offer_with_a_preview(config, pool, stand_in)

        reason = (
            "previews are switched on but the preview service could not start: "
            "RuntimeError: no ledger"
        )
        assert offer["review_evidence"]["state"] == "failed"
        assert offer["review_evidence"]["failure_reason"] == reason
        consumer, publisher, gk, dp = _consumer(
            config, pool, stand_in, guardkit=_JoinsForReal()
        )
        await consumer.handle_envelope(_envelope(correlation_id=f"corr-{BUILD_ID}"))
        await _drain(consumer)

        assert gk.calls == [] and dp.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)
        lines = json.loads(_the_record(pool)[2])
        assert lines[0]["detail"]["refusal"] == f"the approval was refused: {reason}"
        # Reject still ends it.
        await consumer.handle_envelope(
            _envelope(decision="reject", correlation_id=f"corr-{BUILD_ID}")
        )
        await _drain(consumer)
        assert [r.result for r in publisher.reports] == ["rejected"]
