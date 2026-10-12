"""The merge card names every file the branch changed that was already there.

Repair 2 part B of the factory reliability repairs (12 October 2026). In the
four-worker trial both builds that counted had rewritten an existing URL test
and changed their dependency declarations, and the card showed only a passing
suite, measured WITH those changes. The card now says which files that already
existed were changed, and that the result above is therefore not evidence that
the original tests pass unchanged. Disclosure only: nothing here refuses.

Driven against real git repositories: a real base commit, a real branch off
it, and the same ``diff --name-status`` reading the scope pass already takes.
The wording is pinned character for character.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge.cli._serve_gate_activation import (
    EXISTING_FILES_NAMED_ON_THE_CARD,
    card_line_about_files_that_already_existed,
    merge_card_words,
)
from forge.pipeline.branch_scope import BranchScopeReading, read_branch_scope
from forge.pipeline.merge_ready_checkpoint import (
    ExistingFileChange,
    files_that_already_existed,
    parse_changed_files,
)
from forge.pipeline.scope_report import ScopeReport, scope_of_the_build

FEATURE_ID = "FEAT-EXIST"
BRANCH = f"autobuild/{FEATURE_ID}"
URL_TEST = "tests/test_conftest_database_url.py"

#: The design's own sentence (factory-reliability-repairs-2026-10-12.md,
#: repair 2 part B), as it reads on a plain-text card. Its second sentence
#: says "any passing result on this card" rather than "the test result above",
#: because the line above can be one saying the evidence was unavailable.
THE_DESIGNS_SENTENCE = (
    "This branch changes 3 files that already existed: pyproject.toml, "
    "requirements.txt, tests/test_conftest_database_url.py (a test file). Any "
    "passing result on this card was measured with these changes, so it is not "
    "evidence that the original tests pass unchanged."
)

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


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
        ["git", *args],
        cwd=root,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repository(tmp_path: Path) -> Path:
    """A base with a test, two dependency files and some code, and a branch."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, URL_TEST, "def test_url():\n    assert url() == 'sqlite:///x'\n")
    _write(root, "pyproject.toml", "[project]\nname = 'x'\ndependencies = []\n")
    _write(root, "requirements.txt", "sqlalchemy\n")
    _write(root, "src/app.py", "def url():\n    return 'sqlite:///x'\n")
    _write(root, "tests/test_old_name.py", "def test_one():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", BRANCH)
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _scope(root: Path) -> ScopeReport:
    reading = read_branch_scope(
        repo_root=root, base="main", head=BRANCH, feature_id=FEATURE_ID
    )
    return scope_of_the_build(reading=reading, request=None)


def _paths(report: ScopeReport) -> list[str]:
    return [item.path for item in report.files_that_already_existed]


class TestWhichFilesAreListed:
    def test_an_existing_test_and_manifest_are_listed_and_a_new_file_is_not(
        self, tmp_path: Path
    ) -> None:
        root = _repository(tmp_path)
        _write(
            root,
            URL_TEST,
            "def test_url():\n    assert make_url(url()).database == 'x'\n",
        )
        _write(
            root,
            "pyproject.toml",
            "[project]\nname = 'x'\ndependencies = ['sqlalchemy[asyncio]']\n",
        )
        _write(root, "src/created_per_day.py", "def counts():\n    return []\n")
        _commit(root, "the build")

        report = _scope(root)

        assert report.read is True
        assert sorted(_paths(report)) == ["pyproject.toml", URL_TEST]
        by_path = {item.path: item for item in report.files_that_already_existed}
        assert by_path[URL_TEST].is_test is True
        assert by_path["pyproject.toml"].is_test is False
        assert all(item.deleted is False for item in by_path.values())

    def test_a_branch_that_only_adds_files_has_no_such_line(
        self, tmp_path: Path
    ) -> None:
        root = _repository(tmp_path)
        _write(root, "src/created_per_day.py", "def counts():\n    return []\n")
        _write(root, "tests/test_created_per_day.py", "def test_counts():\n    assert True\n")
        _commit(root, "the build")

        report = _scope(root)

        assert report.read is True
        assert report.files_that_already_existed == []
        assert card_line_about_files_that_already_existed(report) == ""

    def test_a_rename_counts_once_under_its_new_name(self, tmp_path: Path) -> None:
        root = _repository(tmp_path)
        _git(root, "mv", "tests/test_old_name.py", "tests/test_new_name.py")
        _commit(root, "rename")

        report = _scope(root)

        assert _paths(report) == ["tests/test_new_name.py"]
        assert report.files_that_already_existed[0].is_test is True
        assert "tests/test_old_name.py" not in card_line_about_files_that_already_existed(
            report
        )

    def test_a_deleted_file_is_listed_and_marked(self, tmp_path: Path) -> None:
        root = _repository(tmp_path)
        _git(root, "rm", "-q", "requirements.txt")
        _commit(root, "drop it")

        report = _scope(root)

        assert report.files_that_already_existed == [
            ExistingFileChange(path="requirements.txt", deleted=True, is_test=False)
        ]
        assert card_line_about_files_that_already_existed(report) == (
            "This branch changes 1 file that already existed: requirements.txt "
            "(deleted). Any passing result on this card was measured with this "
            "change, so it is not evidence that the original tests pass unchanged."
        )

    def test_a_copied_file_is_new_and_its_original_is_untouched(self) -> None:
        changes = parse_changed_files("C100\0src/app.py\0src/app_copy.py\0")

        assert files_that_already_existed(changes) == ()

    def test_any_stack_any_file_type(self) -> None:
        """No file-type knowledge: whatever was there and changed is listed."""
        changes = parse_changed_files(
            "M\0go.sum\0M\0package-lock.json\0T\0bin/run\0A\0lib/new.dart\0"
            "M\0spec/user_spec.rb\0"
        )

        assert [item.path for item in files_that_already_existed(changes)] == [
            "go.sum",
            "package-lock.json",
            "bin/run",
            "spec/user_spec.rb",
        ]

    def test_the_repositorys_declared_test_paths_mark_its_tests(self) -> None:
        changes = parse_changed_files("M\0spec/checks_users.py\0")

        plain = files_that_already_existed(changes)
        declared = files_that_already_existed(changes, ("spec",))

        assert plain[0].is_test is False
        assert declared[0].is_test is True


class TestTheWordsOnTheCard:
    def test_the_designs_example_reads_exactly_as_the_design_says(self) -> None:
        report = ScopeReport(read=True)
        report.files_that_already_existed = [
            ExistingFileChange(path="pyproject.toml"),
            ExistingFileChange(path="requirements.txt"),
            ExistingFileChange(path=URL_TEST, is_test=True),
        ]

        assert card_line_about_files_that_already_existed(report) == THE_DESIGNS_SENTENCE

    def test_a_long_list_is_cut_and_counted(self) -> None:
        report = ScopeReport(read=True)
        report.files_that_already_existed = [
            ExistingFileChange(path=f"src/module_{index}.py") for index in range(9)
        ]

        line = card_line_about_files_that_already_existed(report)

        assert line.startswith("This branch changes 9 files that already existed: ")
        shown = EXISTING_FILES_NAMED_ON_THE_CARD
        assert f"src/module_{shown - 1}.py and {9 - shown} more." in line
        assert f"src/module_{shown}.py" not in line

    def test_an_unreadable_branch_says_so_instead_of_listing(
        self, tmp_path: Path
    ) -> None:
        nowhere = tmp_path / "not-a-repository"
        nowhere.mkdir()
        reading = read_branch_scope(
            repo_root=nowhere, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        assert reading.error

        report = scope_of_the_build(reading=reading, request=None)

        assert report.read is False
        assert card_line_about_files_that_already_existed(report) == (
            "Whether this branch changes files that already existed could not "
            "be read here, so any passing result on this card is not evidence "
            "that the original tests pass unchanged."
        )

    def test_nobody_read_the_branch_means_nothing_is_said(self) -> None:
        assert card_line_about_files_that_already_existed(None) == ""

    def test_the_receipt_keeps_the_whole_list(self) -> None:
        report = scope_of_the_build(
            reading=BranchScopeReading(
                name_status=(
                    "M\0pyproject.toml\0D\0requirements.txt\0"
                    f"R087\0tests/test_old.py\0{URL_TEST}\0A\0src/new.py\0"
                )
            ),
            request=None,
        )

        assert report.to_dict()["files_that_already_existed"] == [
            {"path": "pyproject.toml", "deleted": False, "is_test": False},
            {"path": "requirements.txt", "deleted": True, "is_test": False},
            {"path": URL_TEST, "deleted": False, "is_test": True},
        ]

    def test_the_line_says_nothing_this_estate_only_says_to_itself(self) -> None:
        report = ScopeReport(read=True)
        report.files_that_already_existed = [ExistingFileChange(path="a.txt")]
        line = card_line_about_files_that_already_existed(report).lower()
        for word in ("fence", "checkpoint", "gate", "lane", "baseline", "scope"):
            assert word not in line


class TestTheRepairCardCarriesTheLine:
    def test_it_follows_the_test_result_and_comes_before_the_choice(self) -> None:
        report = ScopeReport(read=True)
        report.files_that_already_existed = [
            ExistingFileChange(path="pyproject.toml"),
            ExistingFileChange(path="requirements.txt"),
            ExistingFileChange(path=URL_TEST, is_test=True),
        ]

        card = merge_card_words(
            feature_id="FEAT-EXIST", branch="fix/TASK-X-0001", scope=report
        )

        assert THE_DESIGNS_SENTENCE in card
        assert card.index("What was checked") < card.index(THE_DESIGNS_SENTENCE)
        assert card.index(THE_DESIGNS_SENTENCE) < card.index("Approve =")
