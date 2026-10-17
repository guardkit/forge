"""The frozen local copy of a mission's tip, made inside the sandbox.

Missions design, 17 October 2026, section 7 ("The frozen source"):
``POST /git/source-branch`` makes ``forge/source/<build_id>`` at a commit
already in the clone, by the prepared route's rule (never fetched, forced so a
stale branch is put back), and answers what the branch resolves to. The
coordinator's :class:`SidecarCandidateGit` speaks it.

Real git in ``tmp_path``, the real service on a loopback port. Nothing live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import GIT_SOURCE_BRANCH_ROUTE
from tests.forge.deploy_sidecar.test_git_candidate_tree_routes import (  # noqa: F401
    REPO,
    _git,
    _post,
    clone,
    config,
    sidecar,
)


class TestTheSourceBranchRoute:
    def test_it_makes_the_branch_at_the_commit_and_puts_a_stale_one_back(
        self, sidecar: str, clone: Path  # noqa: F811
    ) -> None:
        main = _git(clone, "rev-parse", "main")
        _git(clone, "branch", "forge/source/build-FEAT-L3E-1", "autobuild/FEAT-L3E")

        status, body = _post(
            f"{sidecar}{GIT_SOURCE_BRANCH_ROUTE}",
            {"repo": REPO, "build_id": "build-FEAT-L3E-1", "commit": main},
        )

        assert status == 200 and body["refusal"] is None
        assert body == {
            "branch": "forge/source/build-FEAT-L3E-1",
            "commit": main,
            "refusal": None,
        }
        assert _git(clone, "rev-parse", "forge/source/build-FEAT-L3E-1") == main

    def test_a_commit_not_in_the_clone_is_refused_and_not_fetched(
        self, sidecar: str  # noqa: F811
    ) -> None:
        status, body = _post(
            f"{sidecar}{GIT_SOURCE_BRANCH_ROUTE}",
            {"repo": REPO, "build_id": "build-FEAT-L3E-1", "commit": "e" * 40},
        )
        assert status == 200 and "refusing to fetch" in body["refusal"]

    @pytest.mark.parametrize(
        "body",
        [
            {"repo": REPO, "build_id": "../x", "commit": "main"},
            {"repo": REPO, "build_id": "build-1"},
            {"repo": "someone/else", "build_id": "build-1", "commit": "a" * 40},
        ],
    )
    def test_a_shape_it_will_not_pass_to_git_is_refused(
        self, sidecar: str, body: dict  # noqa: F811
    ) -> None:
        status, answer = _post(f"{sidecar}{GIT_SOURCE_BRANCH_ROUTE}", body)
        assert status == 400 and answer["error"]


class TestTheCoordinatorsSide:
    @pytest.mark.asyncio
    async def test_the_sandbox_venue_makes_and_reads_back_the_branch(
        self, sidecar: str, clone: Path  # noqa: F811
    ) -> None:
        main = _git(clone, "rev-parse", "main")
        git = SidecarCandidateGit(sidecar, repo=REPO)

        made = await git.make_source_branch("build-FEAT-L3E-2", main)

        assert made.ok and made.commit == main
        assert await git.rev_parse("refs/heads/forge/source/build-FEAT-L3E-2") == main

    @pytest.mark.asyncio
    async def test_an_unreachable_sandbox_is_a_refusal(self) -> None:
        git = SidecarCandidateGit("http://127.0.0.1:1", repo=REPO)
        made = await git.make_source_branch("build-FEAT-L3E-2", "a" * 40)
        assert not made.ok and made.refusal
