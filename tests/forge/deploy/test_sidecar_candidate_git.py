"""The merge press's git spoken to a sandbox's sidecar (sandbox first, rule 89).

:class:`~forge.deploy.sidecar_git.SidecarCandidateGit` is the other half of the
venue seam: the same five operations the in-container venue performs, said over
HTTP to the deploy sidecar inside a repository's sandbox. Here it talks to the
REAL service on a real ephemeral loopback port, against a real git repository,
so what is proved is the two halves agreeing — not a mock's idea of them.

The failure half matters as much: a sidecar that cannot be reached must never
crash a press. Every read answers ``None``, a removal answers ``False``, and
only laying a tree out raises — which is the one place the press already
expects a raise and turns into a plain refusal before the merge.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import CandidateTreeError, InContainerCandidateGit
from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import build_server

REPO = "guardkit/api_test"
FEATURE_ID = "FEAT-SG1"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root.resolve()


@pytest.fixture
def git(clone: Path) -> SidecarCandidateGit:
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield SidecarCandidateGit(f"http://{host}:{port}", repo=REPO)
    finally:
        srv.shutdown()
        srv.server_close()


class TestTheTwoHalvesOfTheSeamAgree:
    @pytest.mark.asyncio
    async def test_the_same_five_operations_give_the_same_answers(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        here = InContainerCandidateGit(clone)
        branch = f"autobuild/{FEATURE_ID}"
        tip = _git(clone, "rev-parse", branch)

        assert await git.rev_parse(branch) == await here.rev_parse(branch) == tip
        assert await git.rev_parse(f"{tip}^{{tree}}") == await here.rev_parse(
            f"{tip}^{{tree}}"
        )
        assert await git.rev_parse("autobuild/FEAT-NOPE") is None
        assert await git.is_ancestor("main", branch) is True
        assert await git.is_ancestor(branch, "main") is False
        assert await git.is_ancestor("b" * 40, "main") is None

        laid_out = await git.materialise_candidate_tree(FEATURE_ID, tip)
        assert Path(laid_out.path) == clone / ".forge-candidates" / FEATURE_ID
        assert (Path(laid_out.path) / "feature.txt").is_file()
        # This venue answers both while it is in there, so the press does not
        # have to ask a second time.
        assert laid_out.tree == _git(clone, "rev-parse", f"{tip}^{{tree}}")
        assert laid_out.exclude_written is True

        assert await git.remove_candidate_tree(FEATURE_ID) is True
        assert not Path(laid_out.path).exists()

    @pytest.mark.asyncio
    async def test_the_path_it_gives_back_is_the_one_it_laid_out(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """The deploy leg and the live gate are pointed at exactly this path."""
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        laid_out = await git.materialise_candidate_tree(FEATURE_ID, tip)
        assert Path(laid_out.path).is_dir()

    @pytest.mark.asyncio
    async def test_a_commit_the_clone_does_not_have_raises_the_press_can_read(
        self, git: SidecarCandidateGit
    ) -> None:
        with pytest.raises(CandidateTreeError) as raised:
            await git.materialise_candidate_tree(FEATURE_ID, "b" * 40)
        assert "git archive" in str(raised.value)

    @pytest.mark.asyncio
    async def test_no_path_from_this_side_reaches_git_in_there(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """A removal names the feature; where its tree is, is the sandbox's to know."""
        other = clone / ".forge-candidates" / "FEAT-OTHER"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("keep\n", encoding="utf-8")

        assert await git.remove_candidate_tree(FEATURE_ID, str(other)) is True
        assert (other / "keep.txt").is_file()


class TestASidecarThatCannotBeReached:
    """Never a crash: an honest answer, and the press says what it could not do."""

    @staticmethod
    def _unreachable() -> SidecarCandidateGit:
        # Port 1 on loopback: nothing listens, and nothing is asked to.
        return SidecarCandidateGit("http://127.0.0.1:1", repo=REPO)

    @pytest.mark.asyncio
    async def test_reads_answer_none_and_a_removal_answers_false(self) -> None:
        git = self._unreachable()

        assert await git.rev_parse("main") is None
        assert await git.is_ancestor("main", "main") is None
        assert await git.ensure_candidate_trees_excluded() is None
        assert await git.remove_candidate_tree(FEATURE_ID) is False

    @pytest.mark.asyncio
    async def test_a_lay_out_raises_with_the_address_in_the_sentence(self) -> None:
        with pytest.raises(CandidateTreeError) as raised:
            await self._unreachable().materialise_candidate_tree(FEATURE_ID, "a" * 40)
        assert "127.0.0.1:1" in str(raised.value)

    def test_it_refuses_to_be_built_without_the_repositorys_key(self) -> None:
        with pytest.raises(ValueError):
            SidecarCandidateGit("http://127.0.0.1:1", repo="")

    def test_it_says_where_it_is_in_words_a_person_reads(self) -> None:
        assert self._unreachable().venue == f"in the sandbox that holds {REPO}"

class TestRetainedAutobuildWorktreeLifecycle:
    @pytest.mark.asyncio
    async def test_sidecar_inspects_and_retires_only_the_offer_pinned_tree(
        self,
        git: SidecarCandidateGit,
        clone: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        build_id = "build-FEAT-SG1-20260918"
        base = tmp_path / "autobuild-worktrees"
        outer = base / build_id
        monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))
        _git(clone, "worktree", "add", "--detach", str(outer), "main")
        inner = outer / ".guardkit/worktrees/TASK-SG1-001"
        inner.parent.mkdir(parents=True)
        _git(clone, "worktree", "add", str(inner), f"autobuild/{FEATURE_ID}")
        (inner / "ignored-by-owner.txt").write_text("retained\n")

        offered = await git.inspect_autobuild_worktree(build_id, str(outer))
        assert offered["ok"] is True
        assert offered["nested_registrations"][0]["path"] == str(inner)
        assert outer.is_dir() and inner.is_dir()
        offered["cleanup_registrations"] = offered["nested_registrations"]

        result = await git.retire_autobuild_worktree(
            build_id, str(outer), offered
        )
        assert result["status"] == "removed", result
        assert not outer.exists() and not inner.exists()

    @pytest.mark.asyncio
    async def test_sidecar_refuses_a_path_outside_the_configured_base(
        self,
        git: SidecarCandidateGit,
        clone: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "FORGE_AUTOBUILD_WORKTREE_BASE", str(tmp_path / "owned-base")
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")

        answer = await git.inspect_autobuild_worktree("build-X", str(outside))
        assert answer["ok"] is False
        assert "configured autobuild worktree" in answer["detail"]
        assert (outside / "keep").is_file()
