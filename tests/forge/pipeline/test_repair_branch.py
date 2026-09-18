"""The repair branch — real git, real worktrees, a temporary repository.

Rewrite-on-refusal spec Part L, rules 48 and 50, the git half:

- the branch is cut from the base branch and carries the files in one commit;
- the work happens under ``.forge/`` and leaves the checkout's working tree
  and index exactly as they were (``git status --porcelain`` hashed before
  and after; HEAD unchanged; the worktree list unchanged);
- ``.forge/`` goes into ``info/exclude`` once;
- a second materialisation reuses the branch and commits nothing when
  nothing changed;
- a failure leaves no branch and no worktree that the call made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.pipeline.repair_branch import (
    FORGE_EXCLUDE_LINE,
    RepairBranchError,
    branch_exists,
    ensure_forge_excluded,
    find_task_file_on_branch,
    materialise_repair_branch,
    read_branch_file,
    repair_branch_name,
    repair_task_ids_on_branches,
    repair_worktree_path,
)

from ._repair_repo import (
    branches,
    commit_count,
    git,
    head,
    isolate_git,
    make_feature_repo,
    porcelain_hash,
    show,
    worktrees,
)

TASK_ID = "TASK-FEAT44A8FIX1"
FILES = {
    "tasks/backlog/add-the-thing/TASK-FEAT44A8FIX1-repair.md": "---\nid: TASK-FEAT44A8FIX1\n---\n\n# repair\n",
    ".guardkit/features/TASK-FEAT44A8FIX1.yaml": "id: TASK-FEAT44A8FIX1\nname: repair\nparent_feature: FEAT-44A8\n",
}


@pytest.fixture(autouse=True)
def _git_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_git(monkeypatch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_feature_repo(tmp_path / "api_test")


def _materialise(
    repo: Path, files=FILES, *, base: str = "main", expected: str | None = None
):
    return materialise_repair_branch(
        repo,
        task_id=TASK_ID,
        base_branch=base,
        expected_base_commit=expected,
        files=files,
        message="repair task for build-1: repair",
    )


class TestCuttingTheBranch:
    def test_the_files_land_in_one_commit_on_the_repair_branch(self, repo: Path) -> None:
        before = commit_count(repo, "main")

        result = _materialise(repo)

        assert result.branch == repair_branch_name(TASK_ID) == "repair/TASK-FEAT44A8FIX1"
        assert result.created_branch is True
        assert result.committed is True
        assert result.files == tuple(FILES)
        assert branch_exists(repo, result.branch)
        assert commit_count(repo, result.branch) == before + 1
        assert head(repo, result.branch) == result.commit
        assert git(repo, "merge-base", "--is-ancestor", "main", result.branch).returncode == 0
        for path, text in FILES.items():
            assert show(repo, result.branch, path) == text
        subject = git(repo, "log", "-1", "--format=%s", result.branch).stdout.strip()
        assert subject == "repair task for build-1: repair"

    def test_the_shared_checkout_is_untouched(self, repo: Path) -> None:
        (repo / "scratch.txt").write_text("an operator's own untracked file\n")
        status_before = porcelain_hash(repo)
        head_before = head(repo)
        trees_before = worktrees(repo)

        _materialise(repo)

        assert porcelain_hash(repo) == status_before
        assert head(repo) == head_before
        assert git(repo, "diff", "--cached", "--quiet").returncode == 0
        assert worktrees(repo) == trees_before
        assert not repair_worktree_path(repo, TASK_ID).exists()
        # main itself did not move
        assert show(repo, "main", "README.md") == "the feature, merged\n"

    def test_the_exclude_line_is_written_once(self, repo: Path) -> None:
        _materialise(repo)
        _materialise(repo)

        exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").stdout.strip())
        if not exclude.is_absolute():
            exclude = repo / exclude
        lines = [line.strip() for line in exclude.read_text().splitlines()]
        assert lines.count(FORGE_EXCLUDE_LINE) == 1
        assert ensure_forge_excluded(repo) is False

    def test_the_loader_s_rule_finds_the_file_on_the_branch(self, repo: Path) -> None:
        assert find_task_file_on_branch(repo, "main", TASK_ID) is None

        result = _materialise(repo)

        assert find_task_file_on_branch(repo, result.branch, TASK_ID) == (
            "tasks/backlog/add-the-thing/TASK-FEAT44A8FIX1-repair.md"
        )
        assert repair_task_ids_on_branches(repo) == {TASK_ID}
        assert read_branch_file(repo, result.branch, "nope.md") is None


class TestDoingItAgain:
    def test_the_same_files_again_reuse_the_branch_and_commit_nothing(self, repo: Path) -> None:
        first = _materialise(repo)

        second = _materialise(repo)

        assert second.created_branch is False
        assert second.committed is False
        assert second.commit == first.commit
        assert commit_count(repo, second.branch) == commit_count(repo, "main") + 1

    def test_changed_files_add_one_commit_on_the_same_branch(self, repo: Path) -> None:
        first = _materialise(repo)
        changed = dict(FILES)
        changed[".guardkit/features/TASK-FEAT44A8FIX1.yaml"] += "# changed\n"

        second = _materialise(repo, changed)

        assert second.created_branch is False
        assert second.committed is True
        assert second.commit != first.commit
        assert git(repo, "rev-parse", f"{second.commit}^").stdout.strip() == first.commit

    def test_a_leftover_worktree_from_a_crash_is_cleared_first(self, repo: Path) -> None:
        stale = repair_worktree_path(repo, TASK_ID)
        stale.mkdir(parents=True)
        (stale / "junk").write_text("left behind\n")

        result = _materialise(repo)

        assert result.committed is True
        assert not stale.exists()

    def test_a_stale_base_ref_is_refused_before_a_branch_is_cut(
        self, repo: Path
    ) -> None:
        expected = head(repo, "main")
        (repo / "later.txt").write_text("the ref moved\n", encoding="utf-8")
        git(repo, "add", "later.txt")
        git(repo, "commit", "-q", "-m", "move the base")

        with pytest.raises(RepairBranchError, match="not the retained candidate"):
            _materialise(repo, expected=expected)

        assert branches(repo) == ["main"]

    def test_an_existing_wrong_base_repair_branch_is_not_reused(
        self, repo: Path
    ) -> None:
        expected_branch = "autobuild/FEAT-44A8"
        wrong = _materialise(repo)
        (repo / "candidate.txt").write_text("retained work\n", encoding="utf-8")
        git(repo, "add", "candidate.txt")
        git(repo, "commit", "-q", "-m", "retained candidate")
        git(repo, "branch", expected_branch)
        expected = head(repo, expected_branch)

        with pytest.raises(
            RepairBranchError, match="does not contain the retained candidate"
        ):
            _materialise(repo, base=expected_branch, expected=expected)

        assert head(repo, wrong.branch) == wrong.commit


class TestFailingCleanly:
    def test_a_missing_base_branch_refuses_and_makes_nothing(self, repo: Path) -> None:
        with pytest.raises(RepairBranchError, match="no local branch called 'release'"):
            _materialise(repo, base="release")

        assert branches(repo) == ["main"]
        assert not repair_worktree_path(repo, TASK_ID).exists()

    def test_a_directory_that_is_not_a_checkout_refuses(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        with pytest.raises(RepairBranchError, match="not a git checkout"):
            _materialise(plain)

    def test_no_files_refuses(self, repo: Path) -> None:
        with pytest.raises(RepairBranchError, match="no files"):
            _materialise(repo, {})

    def test_a_write_that_fails_leaves_no_branch_and_no_worktree(self, repo: Path) -> None:
        """``tasks`` is a file on main, so the task folder cannot be made."""
        (repo / "tasks").rename(repo / "tasks-was-here")
        (repo / "tasks").write_text("not a directory\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "tasks is a file now")
        status_before = porcelain_hash(repo)

        with pytest.raises(RepairBranchError, match="could not be written"):
            _materialise(repo)

        assert branches(repo) == ["main"]
        assert not repair_worktree_path(repo, TASK_ID).exists()
        assert worktrees(repo) == [str(repo.resolve())]
        assert porcelain_hash(repo) == status_before

    def test_a_path_that_escapes_the_worktree_refuses(self, repo: Path) -> None:
        with pytest.raises(RepairBranchError, match="escape"):
            _materialise(repo, {"../outside.md": "no\n"})

        assert branches(repo) == ["main"]
        assert not (repo / "outside.md").exists()
        assert not (repo / ".forge" / "outside.md").exists()

    def test_a_failure_on_an_existing_branch_leaves_the_branch_as_it_was(self, repo: Path) -> None:
        first = _materialise(repo)

        with pytest.raises(RepairBranchError):
            _materialise(repo, {"/absolute.md": "no\n"})

        assert head(repo, first.branch) == first.commit
        assert not repair_worktree_path(repo, TASK_ID).exists()
