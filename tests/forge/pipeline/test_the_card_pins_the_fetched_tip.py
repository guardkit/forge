"""The merge card pins the target branch as fetched now, not the clone's local one.

Found in the missions trial, 17 October 2026: the card's ``expect_main_sha``
was read off the clone's LOCAL ``main``, which nothing fetches, so in a
sandbox clone every card pinned the clone's first commit. The pin now comes
from the same fresh read of the recorded target branch the preview's base and
the merge check use (``merge_join.target_branch_now``).

Real git with a bare "remote" on disk; nothing real is contacted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
    TargetRead,
)
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    BUILD_ID,
    FEATURE_ID,
    REPO,
    _ensure_build,
    _git,
    _receipts_env,
    config,
    pool,
    repo_root,
)


def _somebody_lands_on_main(repo_root: Path) -> str:  # noqa: F811
    """Another clone pushes to the remote's main; this clone's main lags."""
    bare = _git(repo_root, "remote", "get-url", "origin")
    other = repo_root.parent / "other"
    subprocess.run(["git", "clone", "-q", bare, str(other)], check=True, capture_output=True)
    (other / "later.txt").write_text("later\n", encoding="utf-8")
    _git(other, "add", "later.txt")
    _git(other, "commit", "-q", "-m", "later work on main")
    _git(other, "push", "-q", "origin", "main")
    return _git(other, "rev-parse", "main")


def _service(config: ForgeConfig, pool: SqliteLifecyclePersistence) -> MergeOfferService:  # noqa: F811
    async def _nothing(*_a: Any) -> None:
        return None

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_nothing),
        raw_publish=_nothing,
    )


def _words(_branch: str, _merge_branch: str | None) -> str:
    return "the card"


@pytest.mark.asyncio
async def test_the_pin_is_the_fetched_tip_when_local_main_lags(
    config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path  # noqa: F811
) -> None:
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    local_main = _git(repo_root, "rev-parse", "main")
    fetched = _somebody_lands_on_main(repo_root)
    assert fetched != local_main

    assert await _service(config, pool).offer(
        build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=_words
    )

    offer = [
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ][0].details[MERGE_OFFER_DETAILS_KEY]
    assert offer["expect_main_sha"] == fetched
    # The clone's own main was not moved by the read.
    assert _git(repo_root, "rev-parse", "main") == local_main


@pytest.mark.asyncio
async def test_an_ordinary_build_whose_fetch_fails_still_gets_its_card(
    config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """A routine build is offered once and never again, so a fetch that fails
    must not cost it its card: it is pinned the way it always was."""
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    local_main = _git(repo_root, "rev-parse", "main")
    away = config.model_copy(deep=True)
    away.planning.fetch_addresses[REPO] = str(tmp_path / "nowhere.git")

    assert await _service(away, pool).offer(
        build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=_words
    )

    offer = [
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ][0].details[MERGE_OFFER_DETAILS_KEY]
    assert offer["expect_main_sha"] == local_main


@pytest.mark.asyncio
async def test_a_retrying_caller_whose_fetch_fails_is_refused_and_may_pass(
    config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    away = config.model_copy(deep=True)
    away.planning.fetch_addresses[REPO] = str(tmp_path / "nowhere.git")
    refusals: list[tuple[str, bool]] = []

    offered = await _service(away, pool).offer(
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        card_words=_words,
        on_refusal=lambda sentence, may_pass: refusals.append((sentence, may_pass)),
    )

    assert offered is False
    assert pool.read_stages(BUILD_ID) == []
    assert len(refusals) == 1 and refusals[0][1] is True
    assert refusals[0][0].startswith("the target branch could not be fetched")


@pytest.mark.asyncio
async def test_a_renamed_default_branch_will_not_pass_for_a_retrying_caller(
    config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path  # noqa: F811
) -> None:
    # The work was recorded against "trunk"; the remote's default is "main".
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID, target_branch="trunk")
    refusals: list[tuple[str, bool]] = []

    offered = await _service(config, pool).offer(
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        card_words=_words,
        on_refusal=lambda sentence, may_pass: refusals.append((sentence, may_pass)),
    )

    assert offered is False
    assert len(refusals) == 1 and refusals[0][1] is False
    assert "now says its default branch is" in refusals[0][0]


class _OnePreview:
    """A preview service that records what it was asked to join onto."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def enabled_for(self, _repo: str) -> bool:
        return True

    async def prepare(self, request: Any) -> Any:
        self.requests.append(request)
        return None


@pytest.mark.asyncio
async def test_the_pin_and_the_preview_base_are_one_read(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge.pipeline import merge_join

    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    fetched = _somebody_lands_on_main(repo_root)
    reads: list[Any] = []
    real = merge_join.target_branch_now

    async def counted(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        return await real(*args, **kwargs)

    preview = _OnePreview()
    service = _service(config, pool)
    service._review_preview = preview
    monkeypatch.setattr(merge_join, "target_branch_now", counted)
    await service._prepare_the_preview(
        row=pool.get_build_row(BUILD_ID),
        git=service._git_for(pool.get_build_row(BUILD_ID), repo_root),
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        branch=f"autobuild/{FEATURE_ID}",
        candidate_sha="c" * 40,
        candidate_tree="d" * 40,
        expect_main_sha=fetched,
        target_branch="main",
        target_read=TargetRead(commit=fetched),
    )

    assert reads == []
    assert preview.requests[0].preview_base_sha == fetched
    assert preview.requests[0].expect_main_sha == fetched


@pytest.mark.asyncio
async def test_an_offer_with_a_preview_reads_the_target_once(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole offer, with a preview: one read of the target branch, and the
    card's pin is the very commit the preview was joined onto."""
    from forge.pipeline import merge_join

    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    fetched = _somebody_lands_on_main(repo_root)
    reads: list[Any] = []
    real = merge_join.target_branch_now

    async def counted(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(merge_join, "target_branch_now", counted)
    preview = _OnePreview()

    async def _nothing(*_a: Any) -> None:
        return None

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_nothing),
        raw_publish=_nothing,
        review_preview=preview,
        commit_reader=lambda _repo, _root: object(),
    )

    assert await service.offer(build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=_words)

    offer = [
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ][0].details[MERGE_OFFER_DETAILS_KEY]
    assert len(reads) == 1
    assert len(preview.requests) == 1
    assert offer["expect_main_sha"] == preview.requests[0].preview_base_sha == fetched
    assert preview.requests[0].preview_base_refusal is None
    # The merge-check reading was made at the same commit.
    assert offer["merge_check_reading"]["declared_at"] == fetched


@pytest.mark.asyncio
async def test_a_failed_read_is_shared_and_never_fetched_again(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex R2: the offer's read fails and a second read would have
    succeeded. There is no second read: the card still goes out with the old
    pin, and the preview and the merge-check reading say the shared read
    failed, with its own refusal."""
    from forge.pipeline import merge_join
    from forge.pipeline.merge_join import TargetNow

    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    local_main = _git(repo_root, "rev-parse", "main")
    _somebody_lands_on_main(repo_root)
    reads: list[Any] = []
    real = merge_join.target_branch_now
    said = "the project's recorded address did not answer just now"

    async def fails_once(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        if len(reads) == 1:
            return TargetNow(branch="main", refusal=said)
        return await real(*args, **kwargs)

    monkeypatch.setattr(merge_join, "target_branch_now", fails_once)
    preview = _OnePreview()

    async def _nothing(*_a: Any) -> None:
        return None

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_nothing),
        raw_publish=_nothing,
        review_preview=preview,
        commit_reader=lambda _repo, _root: object(),
    )

    assert await service.offer(build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=_words)

    offer = [
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ][0].details[MERGE_OFFER_DETAILS_KEY]
    assert len(reads) == 1
    # The ordinary card still goes out, pinned the way it always was.
    assert offer["expect_main_sha"] == local_main
    # The preview was told the shared read failed, and why.
    assert preview.requests[0].preview_base_sha is None
    assert preview.requests[0].preview_base_refusal == said
    # And so was the merge-check reading.
    reading = offer["merge_check_reading"]
    assert reading["declared_at"] is None and reading["read"] is False
    assert said in reading["reason"]


class _AFailedPreview(_OnePreview):
    """The real service's answer for a base it could not get: failed evidence
    with no ``preview_base_sha`` (``review_preview._record``)."""

    async def prepare(self, request: Any) -> Any:
        from datetime import UTC, datetime, timedelta

        from nats_core.events import ReviewEvidence, ReviewOutcome

        self.requests.append(request)
        now = datetime.now(UTC)
        return ReviewEvidence(
            state="failed",
            app_url=None,
            report_url=None,
            media_urls=[],
            diagnostics_url=None,
            failure_reason=(
                f"{request.target_branch} could not be fetched from the remote, "
                f"so there is nothing to join the candidate onto: "
                f"{request.preview_base_refusal}"
            ),
            candidate_sha=request.candidate_sha,
            candidate_tree=request.candidate_tree,
            preview_tree=request.candidate_tree,
            preview_base_sha=None,
            run_id="run-1",
            created_at=now,
            app_expires_at=now + timedelta(hours=1),
            evidence_expires_at=now + timedelta(days=14),
            outcome=ReviewOutcome(),
        )


@pytest.mark.asyncio
async def test_failed_preview_evidence_carries_the_shared_refusal_to_the_merge_check(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round 2, the rest of R2: the real preview answers failed evidence
    with no base when the shared read failed; the merge-check reading's reason
    is that read's refusal, not "the preview names no commit"."""
    from forge.pipeline import merge_join
    from forge.pipeline.merge_join import TargetNow

    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    _somebody_lands_on_main(repo_root)
    reads: list[Any] = []
    real = merge_join.target_branch_now
    said = "the project's recorded address did not answer just now"

    async def fails_once(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        if len(reads) == 1:
            return TargetNow(branch="main", refusal=said)
        return await real(*args, **kwargs)

    monkeypatch.setattr(merge_join, "target_branch_now", fails_once)
    preview = _AFailedPreview()

    async def _nothing(*_a: Any) -> None:
        return None

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_nothing),
        raw_publish=_nothing,
        review_preview=preview,
        commit_reader=lambda _repo, _root: object(),
    )

    assert await service.offer(build_id=BUILD_ID, feature_id=FEATURE_ID, card_words=_words)

    offer = [
        s for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ][-1].details[MERGE_OFFER_DETAILS_KEY]
    assert len(reads) == 1
    assert offer["review_evidence"]["state"] == "failed"
    assert offer["review_evidence"]["preview_base_sha"] is None
    reading = offer["merge_check_reading"]
    assert reading["declared_at"] is None and reading["read"] is False
    assert said in reading["reason"]
    assert "the preview names no commit" not in reading["reason"]
