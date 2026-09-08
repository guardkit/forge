"""The candidate's tree — laid out from a real git repository, excluded once, removed.

Protect-main (rule 38): the feature branch's tree is laid out inside the
checkout at ``.forge-candidates/<FEAT-id>/`` so the sandbox, which bind-mounts
the checkout, can build it; the directory is written into
``.git/info/exclude`` once; and it is removed when the run ends. Everything
here runs against a real repository in a temporary directory — git is the
boundary, nothing is faked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.deploy.candidate_tree import (
    CANDIDATE_TREES_DIRNAME,
    CANDIDATE_TREES_EXCLUDE_LINE,
    CandidateTreeError,
    InContainerCandidateGit,
    candidate_tree_path,
    candidate_trees_root,
    ensure_candidate_trees_excluded,
    git_is_ancestor,
    git_rev_parse,
    is_candidate_tree_path,
    materialise_candidate_tree,
    remove_candidate_tree,
)

FEATURE_ID = "FEAT-TR33"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main with one commit; ``autobuild/FEAT-TR33`` adds a file and changes one."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    (root / "deploy").mkdir()
    (root / "deploy" / "deploy.sh").write_text("#!/bin/sh\necho main\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    (root / "deploy" / "deploy.sh").write_text("#!/bin/sh\necho branch\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root


class TestThePaths:
    def test_the_tree_lives_directly_under_the_checkout(self, tmp_path: Path) -> None:
        assert candidate_trees_root(tmp_path) == tmp_path / CANDIDATE_TREES_DIRNAME
        assert candidate_tree_path(tmp_path, "FEAT-1") == (
            tmp_path / ".forge-candidates" / "FEAT-1"
        )

    @pytest.mark.parametrize("bad", ["", "a/b", "..", ".", "x\\y"])
    def test_a_feature_id_that_is_not_one_segment_is_refused(
        self, tmp_path: Path, bad: str
    ) -> None:
        with pytest.raises(CandidateTreeError):
            candidate_tree_path(tmp_path, bad)

    def test_only_a_directory_directly_under_the_root_counts(self, tmp_path: Path) -> None:
        root = tmp_path / ".forge-candidates"
        assert is_candidate_tree_path(tmp_path, root / "FEAT-1")
        assert not is_candidate_tree_path(tmp_path, root)
        assert not is_candidate_tree_path(tmp_path, root / "FEAT-1" / "deeper")
        assert not is_candidate_tree_path(tmp_path, tmp_path / "elsewhere")
        # ``..`` cannot walk out and back in to a different place.
        assert not is_candidate_tree_path(tmp_path, root / "FEAT-1" / ".." / ".." / "x")
        assert is_candidate_tree_path(tmp_path, root / "FEAT-1" / ".." / "FEAT-2")


class TestLayingTheTreeOut:
    @pytest.mark.asyncio
    async def test_the_branch_tip_and_its_tree_are_read(self, repo: Path) -> None:
        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        assert tip == _git(repo, "rev-parse", f"autobuild/{FEATURE_ID}")
        tree = await git_rev_parse(repo, f"{tip}^{{tree}}")
        assert tree == _git(repo, "rev-parse", f"{tip}^{{tree}}")
        assert await git_rev_parse(repo, "autobuild/FEAT-NOPE") is None

    @pytest.mark.asyncio
    async def test_the_tree_is_the_branch_not_main(self, repo: Path) -> None:
        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        assert tip
        laid_out = await materialise_candidate_tree(repo, FEATURE_ID, tip)
        assert laid_out == repo / ".forge-candidates" / FEATURE_ID
        # The branch's file is there and the changed file is the branch's copy;
        # the checkout itself is still main.
        assert (laid_out / "feature.txt").read_text(encoding="utf-8") == "the feature\n"
        assert "echo branch" in (laid_out / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        assert not (repo / "feature.txt").exists()
        assert "echo main" in (repo / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        # No git metadata rides along: nothing inside the sandbox can reach
        # the shared checkout's git state through the laid-out tree.
        assert not (laid_out / ".git").exists()

    @pytest.mark.asyncio
    async def test_a_tree_left_behind_is_replaced(self, repo: Path) -> None:
        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        stale = repo / ".forge-candidates" / FEATURE_ID
        stale.mkdir(parents=True)
        (stale / "stale.txt").write_text("from a run that died\n", encoding="utf-8")
        laid_out = await materialise_candidate_tree(repo, FEATURE_ID, tip or "")
        assert not (laid_out / "stale.txt").exists()
        assert (laid_out / "feature.txt").exists()

    @pytest.mark.asyncio
    async def test_a_commit_git_does_not_know_is_refused(self, repo: Path) -> None:
        with pytest.raises(CandidateTreeError):
            await materialise_candidate_tree(repo, FEATURE_ID, "f" * 40)
        with pytest.raises(CandidateTreeError):
            await materialise_candidate_tree(repo, FEATURE_ID, "")

    @pytest.mark.asyncio
    async def test_a_refused_commit_leaves_no_directory_behind(self, repo: Path) -> None:
        """Rule 38: a lay-out that fails is the end of the run, and nothing
        stays in the shared checkout — not even an empty directory (the
        coach's refutation script, 2026-09-07)."""
        dest = repo / ".forge-candidates" / FEATURE_ID
        with pytest.raises(CandidateTreeError, match="git archive"):
            await materialise_candidate_tree(repo, FEATURE_ID, "f" * 40)
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_an_extraction_that_dies_half_way_leaves_no_directory_behind(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy import candidate_tree as module

        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        dest = repo / ".forge-candidates" / FEATURE_ID

        def _dies_half_way(*_args: object, **_kwargs: object) -> object:
            # Half a tree is already on disk when the stream breaks.
            (dest / "half.txt").write_text("half\n", encoding="utf-8")
            raise module.tarfile.TarError("the stream ended half way")

        monkeypatch.setattr(module.tarfile, "open", _dies_half_way)
        with pytest.raises(CandidateTreeError):
            await materialise_candidate_tree(repo, FEATURE_ID, tip or "")
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_the_tree_is_removed(self, repo: Path) -> None:
        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        laid_out = await materialise_candidate_tree(repo, FEATURE_ID, tip or "")
        assert laid_out.is_dir()
        assert await remove_candidate_tree(laid_out) is True
        assert not laid_out.exists()
        # Removing what is not there is not a failure.
        assert await remove_candidate_tree(laid_out) is True


class TestTheExcludeLine:
    @pytest.mark.asyncio
    async def test_written_once_and_only_once(self, repo: Path) -> None:
        assert await ensure_candidate_trees_excluded(repo) is True
        assert await ensure_candidate_trees_excluded(repo) is False
        assert await ensure_candidate_trees_excluded(repo) is False
        exclude = repo / ".git" / "info" / "exclude"
        lines = exclude.read_text(encoding="utf-8").splitlines()
        assert lines.count(CANDIDATE_TREES_EXCLUDE_LINE) == 1

    @pytest.mark.asyncio
    async def test_a_laid_out_tree_leaves_the_checkout_clean(self, repo: Path) -> None:
        tip = await git_rev_parse(repo, f"autobuild/{FEATURE_ID}")
        await ensure_candidate_trees_excluded(repo)
        await materialise_candidate_tree(repo, FEATURE_ID, tip or "")
        # The merge command's dirty-tree check reads git status; the tree
        # must not show up there.
        assert _git(repo, "status", "--porcelain") == ""

    @pytest.mark.asyncio
    async def test_an_existing_exclude_file_keeps_its_lines(self, repo: Path) -> None:
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.log", encoding="utf-8")  # no trailing newline
        await ensure_candidate_trees_excluded(repo)
        assert exclude.read_text(encoding="utf-8").splitlines() == [
            "*.log",
            CANDIDATE_TREES_EXCLUDE_LINE,
        ]

    @pytest.mark.asyncio
    async def test_not_a_repository_is_refused_plainly(self, tmp_path: Path) -> None:
        with pytest.raises(CandidateTreeError):
            await ensure_candidate_trees_excluded(tmp_path / "not-a-repo")


# ---------------------------------------------------------------------------
# The venue seam's in-container half (sandbox first, 2026-09-07, rule 89)
# ---------------------------------------------------------------------------
#
# The merge press asks one surface for its five git operations so it can be
# TOLD where they happen. This is the half that happens here, against the
# checkout — which is where every repository without a sandbox is pressed, and
# so must be the very functions above, doing the very same things.


class TestTheAncestryQuestion:
    @pytest.mark.asyncio
    async def test_yes_no_and_could_not_say(self, repo: Path) -> None:
        main = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE_ID}")

        assert await git_is_ancestor(repo, main, tip) is True
        assert await git_is_ancestor(repo, tip, main) is False
        # A commit this repository has never heard of is not a "no".
        assert await git_is_ancestor(repo, "b" * 40, main) is None

    @pytest.mark.asyncio
    async def test_a_place_that_is_not_a_repository_could_not_say(
        self, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "not-a-repo"
        elsewhere.mkdir()
        assert await git_is_ancestor(elsewhere, "main", "main") is None


class TestTheInContainerVenue:
    @pytest.mark.asyncio
    async def test_the_five_operations_are_this_files_own_functions(
        self, repo: Path
    ) -> None:
        venue = InContainerCandidateGit(repo)
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE_ID}")

        assert await venue.rev_parse(f"autobuild/{FEATURE_ID}") == tip
        assert await venue.rev_parse("autobuild/FEAT-NOPE") is None
        assert await venue.is_ancestor("main", f"autobuild/{FEATURE_ID}") is True

        # The exclude line is written once, by its own call, as before.
        assert await venue.ensure_candidate_trees_excluded() is True
        assert await venue.ensure_candidate_trees_excluded() is False

        laid_out = await venue.materialise_candidate_tree(FEATURE_ID, tip)
        assert Path(laid_out.path) == candidate_tree_path(repo, FEATURE_ID)
        assert (Path(laid_out.path) / "feature.txt").is_file()
        # This venue reads no tree id and writes no exclude line while it lays
        # a tree out: the press asks for both separately, exactly as before.
        assert laid_out.tree is None and laid_out.exclude_written is None

        assert await venue.remove_candidate_tree(FEATURE_ID) is True
        assert not Path(laid_out.path).exists()
        # A second removal is still a success.
        assert await venue.remove_candidate_tree(FEATURE_ID) is True

    @pytest.mark.asyncio
    async def test_a_lay_out_that_fails_raises_as_it_always_did(
        self, repo: Path
    ) -> None:
        venue = InContainerCandidateGit(repo)
        with pytest.raises(CandidateTreeError):
            await venue.materialise_candidate_tree(FEATURE_ID, "f" * 40)
        assert not candidate_tree_path(repo, FEATURE_ID).exists()

    def test_it_says_where_it_is_in_words_a_person_reads(self, repo: Path) -> None:
        assert InContainerCandidateGit(repo).venue == f"in {repo}"
