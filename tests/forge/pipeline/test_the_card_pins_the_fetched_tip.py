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
async def test_a_target_that_cannot_be_fetched_gives_no_card_and_may_pass(
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
