"""The join, the pick-up, and the words that can no longer be produced.

One-true-copy design pass, item 1: "The merge word", "Every step happens in a
working folder of its own", the first revision's item 1 (the joined result is
what gets checked) and the second revision's A (picking up by looking).

Real git in a temporary directory, with a bare repository standing in for the
project's remote. No network, no sandbox, no model seat, and nothing here
knows what a project contains.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.deploy.candidate_tree import InContainerCandidateGit
from forge.pipeline.merge_join import (
    INTEGRATION_BRANCH_PREFIX,
    integration_branch,
    look_at_a_join,
    look_at_the_leftover_join,
    make_the_working_folder,
    target_branch_now,
    working_folder_leaf,
)

FEATURE = "FEAT-JN1"


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
    """A project, its build branch, and a bare repository as its remote."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE}")
    (root / "the-feature").write_text("built\n", encoding="utf-8")
    _git(root, "add", "the-feature")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    return root


def _venue(repo: Path) -> InContainerCandidateGit:
    return InContainerCandidateGit(repo)


class TestOneTargetBranchDecidedOnce:
    @pytest.mark.asyncio
    async def test_the_recorded_branch_is_fetched_and_its_commit_is_g(
        self, repo: Path
    ) -> None:
        where = await target_branch_now(_venue(repo), recorded_branch="main")
        assert where.ok
        assert where.branch == "main"
        assert where.commit == _git(repo, "rev-parse", "main")

    @pytest.mark.asyncio
    async def test_a_build_with_nothing_recorded_is_refused_in_plain_words(
        self, repo: Path
    ) -> None:
        where = await target_branch_now(_venue(repo), recorded_branch=None)
        assert not where.ok
        assert "has no target branch on its record" in where.refusal

    @pytest.mark.asyncio
    async def test_a_remote_whose_default_branch_changed_is_said_not_guessed(
        self, repo: Path
    ) -> None:
        where = await target_branch_now(_venue(repo), recorded_branch="trunk")
        assert not where.ok
        assert "this work is aimed at the branch 'trunk'" in where.refusal
        assert "now says its default branch is 'main'" in where.refusal

    @pytest.mark.asyncio
    async def test_a_copy_with_no_remote_is_refused(self, tmp_path: Path) -> None:
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        _git(lonely, "init", "-b", "main", "-q")
        (lonely / "f").write_text("x\n", encoding="utf-8")
        _git(lonely, "add", "f")
        _git(lonely, "commit", "-q", "-m", "only")
        where = await target_branch_now(_venue(lonely), recorded_branch="main")
        assert not where.ok
        assert "has no remote named 'origin'" in where.refusal


class TestTheWorkingFolderOfItsOwn:
    @pytest.mark.asyncio
    async def test_it_is_made_at_g_and_the_main_copy_is_not_switched(
        self, repo: Path
    ) -> None:
        g = _git(repo, "rev-parse", "main")
        branch_before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

        made = await make_the_working_folder(
            _venue(repo), repo_root=repo, feature_id=FEATURE, attempt=1, at_commit=g
        )

        assert made.ok
        assert made.branch == f"{INTEGRATION_BRANCH_PREFIX}{FEATURE}"
        assert Path(made.path).is_dir()
        assert _git(repo, "rev-parse", made.branch) == g
        # The project's main copy: untouched.
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
        assert _git(repo, "rev-parse", "main") == g

    @pytest.mark.asyncio
    async def test_each_attempt_has_a_name_of_its_own(self, repo: Path) -> None:
        assert integration_branch(FEATURE, 1) == f"factory-integration/{FEATURE}"
        assert (
            integration_branch(FEATURE, 2)
            == f"factory-integration/{FEATURE}-attempt-2"
        )
        assert working_folder_leaf(FEATURE, 1) == f"integration-{FEATURE}"
        assert working_folder_leaf(FEATURE, 2) == f"integration-{FEATURE}-2"


class TestLookingAtWhatAnInterruptedJoinLeft:
    @pytest.mark.asyncio
    async def test_a_real_join_is_recognised_as_the_join(self, repo: Path) -> None:
        g = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")
        made = await make_the_working_folder(
            _venue(repo), repo_root=repo, feature_id=FEATURE, attempt=1, at_commit=g
        )
        _git(
            Path(made.path),
            "merge",
            "--no-ff",
            "-m",
            "the join",
            f"autobuild/{FEATURE}",
        )

        leftover = await look_at_the_leftover_join(
            _venue(repo),
            feature_id=FEATURE,
            attempt=1,
            g_commit=g,
            build_tip=tip,
        )

        assert leftover.exists is True
        assert leftover.is_the_join is True
        assert leftover.set_aside is False
        assert leftover.commit == _git(repo, "rev-parse", made.branch)

    @pytest.mark.asyncio
    async def test_a_branch_that_was_made_but_never_merged_is_set_aside(
        self, repo: Path
    ) -> None:
        g = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")
        await make_the_working_folder(
            _venue(repo), repo_root=repo, feature_id=FEATURE, attempt=1, at_commit=g
        )

        leftover = await look_at_the_leftover_join(
            _venue(repo), feature_id=FEATURE, attempt=1, g_commit=g, build_tip=tip
        )

        assert leftover.exists is True
        assert leftover.is_the_join is False
        assert leftover.set_aside is True
        assert "not a merge commit" in leftover.why

    @pytest.mark.asyncio
    async def test_a_join_of_the_wrong_commits_is_set_aside(
        self, repo: Path
    ) -> None:
        g = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")
        made = await make_the_working_folder(
            _venue(repo), repo_root=repo, feature_id=FEATURE, attempt=1, at_commit=g
        )
        _git(
            Path(made.path),
            "merge",
            "--no-ff",
            "-m",
            "the join",
            f"autobuild/{FEATURE}",
        )

        leftover = await look_at_the_leftover_join(
            _venue(repo),
            feature_id=FEATURE,
            attempt=1,
            g_commit=g,
            build_tip="0" * 40,  # a different build's tip
        )

        assert leftover.is_the_join is False
        assert leftover.set_aside is True
        assert "is a merge of" in leftover.why
        # Kept, never deleted: the leftover still stands under its own name.
        assert _git(repo, "rev-parse", integration_branch(FEATURE, 1))
        assert tip

    @pytest.mark.asyncio
    async def test_a_branch_that_does_not_exist_is_simply_not_there(
        self, repo: Path
    ) -> None:
        leftover = await look_at_the_leftover_join(
            _venue(repo),
            feature_id=FEATURE,
            attempt=7,
            g_commit="a" * 40,
            build_tip="b" * 40,
        )
        assert leftover.exists is False
        assert leftover.is_the_join is False


class TestAskingTheSameQuestionOfACommit:
    """``look_at_a_join`` — the question asked of a commit written down earlier.

    A press picking a build up holds a joined commit on the record, not a
    branch name, and it has to ask whether that commit is a join of what is
    true NOW. That is the same question by the same two parents, so it is the
    same code; only what it is pointed at differs.
    """

    @staticmethod
    async def _joined(repo: Path) -> tuple[str, str, str]:
        g = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")
        made = await make_the_working_folder(
            _venue(repo), repo_root=repo, feature_id=FEATURE, attempt=1, at_commit=g
        )
        _git(Path(made.path), "merge", "--no-ff", "-m", "the join", f"autobuild/{FEATURE}")
        return g, tip, _git(repo, "rev-parse", made.branch)

    @pytest.mark.asyncio
    async def test_a_commit_that_is_the_join_of_both_is_the_join(
        self, repo: Path
    ) -> None:
        g, tip, joined = await self._joined(repo)

        answer = await look_at_a_join(
            _venue(repo), ref=joined, g_commit=g, build_tip=tip
        )

        assert answer.is_the_join is True
        assert answer.commit == joined

    @pytest.mark.asyncio
    async def test_a_commit_joined_onto_an_older_tip_is_not_the_join(
        self, repo: Path
    ) -> None:
        """The build gained a fix after the join was made."""
        g, _old_tip, joined = await self._joined(repo)
        _git(repo, "checkout", "-q", f"autobuild/{FEATURE}")
        (repo / "the-fix").write_text("fixed\n", encoding="utf-8")
        _git(repo, "add", "the-fix")
        _git(repo, "commit", "-q", "-m", "the fix")
        new_tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")
        _git(repo, "checkout", "-q", "main")

        answer = await look_at_a_join(
            _venue(repo), ref=joined, g_commit=g, build_tip=new_tip
        )

        assert answer.is_the_join is False
        assert answer.set_aside is True
        assert "is a merge of" in answer.why
        # And it is still there, under the name its own attempt gave it.
        assert _git(repo, "rev-parse", integration_branch(FEATURE, 1)) == joined

    @pytest.mark.asyncio
    async def test_a_commit_that_is_not_a_merge_at_all_is_not_the_join(
        self, repo: Path
    ) -> None:
        tip = _git(repo, "rev-parse", f"autobuild/{FEATURE}")

        answer = await look_at_a_join(
            _venue(repo),
            ref=tip,
            g_commit=_git(repo, "rev-parse", "main"),
            build_tip=tip,
        )

        assert answer.is_the_join is False
        assert "not a merge commit" in answer.why

    @pytest.mark.asyncio
    async def test_a_commit_nobody_has_is_not_the_join_either(self, repo: Path) -> None:
        """A recorded commit this repository does not hold is set aside.

        Git answers a full forty-character name with itself without checking
        that it holds the object, so this does not come back as "not there" —
        it comes back as "it has no second parent", which is the same safe
        answer: it is not the join, so it is set aside and one is made afresh.
        """
        answer = await look_at_a_join(
            _venue(repo), ref="c" * 40, g_commit="a" * 40, build_tip="b" * 40
        )

        assert answer.is_the_join is False
        assert answer.set_aside is True
        assert "not a merge commit" in answer.why

    @pytest.mark.asyncio
    async def test_a_name_nobody_has_is_simply_not_there(self, repo: Path) -> None:
        answer = await look_at_a_join(
            _venue(repo), ref="no-such-branch", g_commit="a" * 40, build_tip="b" * 40
        )

        assert answer.exists is False
        assert answer.is_the_join is False
