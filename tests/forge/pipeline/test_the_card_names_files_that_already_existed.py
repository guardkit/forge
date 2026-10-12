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
#: because the line above can be one saying the evidence was unavailable, and
#: it names the test file first, so a long list can never cut it off.
THE_DESIGNS_SENTENCE = (
    "This branch changes 3 files that already existed: "
    "tests/test_conftest_database_url.py (a test file), pyproject.toml, "
    "requirements.txt. Any "
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

    def test_every_changed_test_and_dependency_file_is_named(self) -> None:
        """Seven changed tests must not push the dependency files off the
        card: every file that already existed is named."""
        tests = [f"tests/test_{index}.py" for index in range(7)]
        manifests = ["package-lock.json", "pyproject.toml", "requirements.txt", "uv.lock"]
        report = scope_of_the_build(
            reading=BranchScopeReading(
                name_status="".join(f"M\0{path}\0" for path in manifests + tests)
            ),
            request=None,
        )

        line = card_line_about_files_that_already_existed(report)

        assert line.startswith("This branch changes 11 files that already existed: ")
        for path in tests:
            assert f"{path} (a test file)" in line
        for path in manifests:
            assert path in line
        assert "more" not in line
        assert "complete list" not in line

    def test_past_the_bound_the_card_says_where_the_complete_list_is(self) -> None:
        bound = EXISTING_FILES_NAMED_ON_THE_CARD
        report = ScopeReport(read=True)
        report.files_that_already_existed = [
            ExistingFileChange(path=f"src/module_{index:02d}.py")
            for index in range(bound + 5)
        ]
        report.receipt_path = "/var/forge/receipts/build-FEAT-EXIST-1/scope_report.json"

        line = card_line_about_files_that_already_existed(report)

        assert line.startswith(
            f"This branch changes {bound + 5} files that already existed (the "
            f"first {bound} named here): src/module_00.py, "
        )
        assert f"src/module_{bound - 1:02d}.py" in line
        assert f"src/module_{bound:02d}.py" not in line
        assert line.endswith(
            f"The complete list of all {bound + 5} is in "
            "/var/forge/receipts/build-FEAT-EXIST-1/scope_report.json."
        )
        assert "more" not in line

    def test_past_the_bound_with_no_receipt_it_names_the_offer_record(self) -> None:
        bound = EXISTING_FILES_NAMED_ON_THE_CARD
        report = ScopeReport(read=True)
        report.files_that_already_existed = [
            ExistingFileChange(path=f"src/module_{index:02d}.py")
            for index in range(bound + 1)
        ]

        line = card_line_about_files_that_already_existed(report)

        assert line.endswith(
            f"The complete list of all {bound + 1} is kept on this build's "
            "merge offer record."
        )

    def test_a_test_file_that_sorts_last_is_still_named_and_marked(self) -> None:
        """git lists paths in order, and tests usually sort late. Cut in that
        order, the one changed test would vanish into "and N more"."""
        report = scope_of_the_build(
            reading=BranchScopeReading(
                name_status="".join(
                    f"M\0src/app/{name}.py\0" for name in "abcdefg"
                )
                + "D\0src/app/old.py\0"
                + f"M\0{URL_TEST}\0"
            ),
            request=None,
        )
        assert [item.path for item in report.files_that_already_existed][-1] == URL_TEST

        line = card_line_about_files_that_already_existed(report)

        assert line.startswith(
            "This branch changes 9 files that already existed: "
            f"{URL_TEST} (a test file), src/app/old.py (deleted), src/app/a.py, "
            "src/app/b.py, src/app/c.py, src/app/d.py, src/app/e.py, "
            "src/app/f.py, src/app/g.py. "
        )

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
