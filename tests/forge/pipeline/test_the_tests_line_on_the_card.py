"""What the branch did to the tests, counted and said on the merge card.

Rich's ruling, 2026-09-10: ordinary unit tests are NOT fenced — legs write
and change tests constantly and legitimately, including tests a review asked
for — so this reports instead. The risk points one way: an assertion that
quietly goes away makes bad code pass and nothing goes red, so the card names
what went away and invites the look before the merge word.

This file pins the rules and the words. The reading itself — real git, a real
branch, a real sidecar — is pinned in
``tests/cli/test_the_branch_s_test_changes_reach_the_card.py``.

What is pinned:

* the counts: files changed, test functions deleted, assertions removed, and
  the files to look in;
* the differences that keep the counts honest — a test moved or a file
  renamed has lost nothing, an assertion whose words CHANGED has;
* what a repository's tests are: its own declared test command's paths, and
  the plain default beside them;
* the card's line in each of its four shapes, including the two quiet ones,
  and that it carries no house word and no internal identifier.
"""

from __future__ import annotations

from forge.cli._serve_gate_activation import merge_card_words, card_line_about_tests
from forge.pipeline.merge_ready_checkpoint import (
    DEFAULT_TEST_PATHS,
    GatesReport,
    GateStatus,
    ChangedTestsReport,
    count_test_changes,
    is_assertion,
    parse_changed_files,
    path_is_test,
    name_of_test_function,
    paths_in_test_command,
)

ROUTER = "tests/users/test_router.py"


def _patch(*hunks: str) -> str:
    return "\n".join(hunks) + "\n"


def _file(path: str, *lines: str) -> str:
    """One file's worth of a ``-U0`` unified diff."""
    return "\n".join(
        [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@"]
        + list(lines)
    )


class TestWhatCountsAsATest:
    def test_the_plain_default_is_a_tests_directory_and_the_usual_file_names(
        self,
    ) -> None:
        for path in (
            "tests/test_router.py",
            "tests/users/test_router.py",
            "src/app/tests/test_router.py",
            "test/widget_test.dart",
            "lib/users_test.go",
            "web/users.test.ts",
            "web/users.spec.js",
        ):
            assert path_is_test(path, DEFAULT_TEST_PATHS), path

    def test_ordinary_code_is_not_a_test(self) -> None:
        for path in ("src/users/router.py", "docs/testing.md", "latest/app.py"):
            assert not path_is_test(path, DEFAULT_TEST_PATHS), path

    def test_a_repository_says_which_paths_are_its_tests_in_its_own_command(
        self,
    ) -> None:
        assert paths_in_test_command(
            "uv run --no-sync python -m pytest -q tests/"
        ) == ("tests",)
        assert paths_in_test_command("bundle exec rspec spec/") == ("spec",)
        assert paths_in_test_command("uv run pytest ./qa/acceptance") == (
            "qa/acceptance",
        )

    def test_a_tools_own_subcommand_is_never_read_as_a_path(self) -> None:
        """``npm test`` names no directory; reading its ``test`` as one would
        put a word on the card the repository never said."""
        assert paths_in_test_command("npm test") == ()
        assert paths_in_test_command("flutter test") == ()
        assert paths_in_test_command("uv run --no-sync python -m pytest -q") == ()
        assert paths_in_test_command(None) == ()


class TestReadingOneLine:
    def test_a_test_function_is_recognised_in_the_shapes_tools_write_it(
        self,
    ) -> None:
        assert name_of_test_function("def test_delete_by_email():") == (
            "test_delete_by_email"
        )
        assert name_of_test_function("    async def test_it_answers(self):") == (
            "test_it_answers"
        )
        assert name_of_test_function("func TestDeleteByEmail(t *testing.T) {") == (
            "TestDeleteByEmail"
        )
        assert name_of_test_function("  it('deletes by email', () => {") == (
            "deletes by email"
        )
        assert name_of_test_function("def helper(x):") == ""

    def test_an_assertion_is_recognised_in_the_shapes_tools_write_it(self) -> None:
        for line in (
            "    assert response.status_code == 200",
            "    assert(found)",
            "    self.assertEqual(a, b)",
            "    expect(found).toBe(true);",
        ):
            assert is_assertion(line), line
        assert not is_assertion("    response = client.get('/users')")


class TestTheCounts:
    def test_a_branch_that_deletes_a_test_and_removes_assertions_is_counted(
        self,
    ) -> None:
        changes = parse_changed_files(f"M\0{ROUTER}\0M\0src/users/router.py\0")
        patch = _file(
            ROUTER,
            "-def test_deleted_user_is_absent():",
            "-    assert response.status_code == 404",
            "-    assert body == {}",
            "-    assert body['detail'] == 'gone'",
        )

        counted = count_test_changes(changes=changes, patch=patch)

        assert counted.files_changed == 1
        assert counted.tests_deleted == 1
        assert counted.assertions_removed == 3
        assert counted.files == (ROUTER,)
        assert counted.lost_something is True

    def test_a_branch_that_only_adds_tests_has_lost_nothing(self) -> None:
        changes = parse_changed_files(f"A\0{ROUTER}\0")
        patch = _file(
            ROUTER,
            "+def test_deleted_user_is_absent():",
            "+    assert response.status_code == 404",
        )

        counted = count_test_changes(changes=changes, patch=patch)

        assert counted.files_changed == 1
        assert counted.lost_something is False
        assert counted.files == ()

    def test_a_branch_that_touches_no_test_at_all_counts_nothing(self) -> None:
        counted = count_test_changes(
            changes=parse_changed_files("M\0src/users/router.py\0"), patch=""
        )

        assert counted == ChangedTestsReport()

    def test_a_renamed_test_file_is_one_file_and_no_deletion(self) -> None:
        """git detects the rename, so nothing was deleted — and the file is
        counted once, not once at each end."""
        changes = parse_changed_files(
            f"R100\0tests/test_router.py\0{ROUTER}\0M\0src/users/router.py\0"
        )

        counted = count_test_changes(changes=changes, patch="")

        assert counted.files_changed == 1
        assert counted.lost_something is False

    def test_a_test_moved_to_another_file_is_not_a_deletion(self) -> None:
        patch = _patch(
            _file(
                ROUTER,
                "-def test_deleted_user_is_absent():",
                "-    assert response.status_code == 404",
            ),
            _file(
                "tests/users/test_absence.py",
                "+def test_deleted_user_is_absent():",
                "+    assert response.status_code == 404",
            ),
        )
        changes = parse_changed_files(
            f"M\0{ROUTER}\0A\0tests/users/test_absence.py\0"
        )

        counted = count_test_changes(changes=changes, patch=patch)

        assert counted.files_changed == 2
        assert counted.lost_something is False

    def test_a_reindented_assertion_is_not_a_removal_but_a_weakened_one_is(
        self,
    ) -> None:
        patch = _file(
            ROUTER,
            "-    assert response.status_code == 404",
            "-        assert body['detail'] == 'gone'",
            "+        assert response.status_code == 404",
            "+        assert body is not None",
        )

        counted = count_test_changes(
            changes=parse_changed_files(f"M\0{ROUTER}\0"), patch=patch
        )

        assert counted.tests_deleted == 0
        assert counted.assertions_removed == 1
        assert counted.files == (ROUTER,)

    def test_lines_taken_out_of_files_that_are_not_tests_are_not_counted(
        self,
    ) -> None:
        patch = _file(
            "src/users/router.py",
            "-    assert email is not None",
            "-def test_helper():",
        )

        counted = count_test_changes(
            changes=parse_changed_files("M\0src/users/router.py\0"), patch=patch
        )

        assert counted == ChangedTestsReport()

    def test_a_repositorys_own_declared_path_is_honoured(self) -> None:
        patch = _file("acceptance/users.py", "-    assert found")
        changes = parse_changed_files("M\0acceptance/users.py\0")

        default = count_test_changes(changes=changes, patch=patch)
        declared = count_test_changes(
            changes=changes,
            patch=patch,
            test_paths=DEFAULT_TEST_PATHS + ("acceptance",),
        )

        assert default == ChangedTestsReport()
        assert declared.files_changed == 1
        assert declared.assertions_removed == 1

    def test_a_diff_nobody_could_read_whole_keeps_the_file_count_and_says_so(
        self,
    ) -> None:
        counted = count_test_changes(
            changes=parse_changed_files(f"M\0{ROUTER}\0"),
            patch="",
            read_whole=False,
        )

        assert counted.files_changed == 1
        assert counted.read_whole is False
        assert counted.lost_something is False


class TestTheLineOnTheCard:
    def test_something_removed_says_what_where_and_invites_the_look(self) -> None:
        line = card_line_about_tests(
            ChangedTestsReport(
                files_changed=1,
                tests_deleted=2,
                assertions_removed=3,
                files=(ROUTER,),
            )
        )

        assert line == (
            "This branch deleted 2 tests and removed 3 assertions in "
            "tests/users/test_router.py — worth a look before you merge."
        )

    def test_one_of_each_is_said_in_the_singular(self) -> None:
        line = card_line_about_tests(
            ChangedTestsReport(
                files_changed=1,
                tests_deleted=1,
                assertions_removed=1,
                files=(ROUTER,),
            )
        )

        assert "deleted 1 test and removed 1 assertion in" in line

    def test_only_assertions_removed_says_only_that(self) -> None:
        line = card_line_about_tests(
            ChangedTestsReport(files_changed=1, assertions_removed=2, files=(ROUTER,))
        )

        assert line.startswith("This branch removed 2 assertions in")
        assert "deleted" not in line

    def test_many_files_are_named_three_at_a_time_and_then_counted(self) -> None:
        line = card_line_about_tests(
            ChangedTestsReport(
                files_changed=5,
                assertions_removed=5,
                files=("tests/a.py", "tests/b.py", "tests/c.py", "tests/d.py"),
            )
        )

        assert "tests/a.py, tests/b.py and tests/c.py and 1 more file" in line

    def test_a_branch_that_only_added_tests_says_nothing_alarming(self) -> None:
        line = card_line_about_tests(ChangedTestsReport(files_changed=4))

        assert line == (
            "This branch changed 4 test files and removed no tests or "
            "assertions."
        )
        assert "0" not in line
        assert "worth a look" not in line

    def test_a_branch_that_touched_no_test_gets_one_short_clause(self) -> None:
        assert card_line_about_tests(ChangedTestsReport()) == "This branch changed no test files."

    def test_a_reading_that_did_not_happen_never_reads_as_nothing(self) -> None:
        assert card_line_about_tests(ChangedTestsReport(read_whole=False)) == (
            "What this branch changed in the tests could not be read here."
        )
        assert card_line_about_tests(ChangedTestsReport(files_changed=9, read_whole=False)) == (
            "This branch changed 9 test files, and what it changed in them "
            "could not be read here."
        )

    def test_nobody_counted_means_the_card_says_nothing_about_tests(self) -> None:
        assert card_line_about_tests(None) == ""


class TestTheCardItself:
    def _card(self, counts: ChangedTestsReport | None) -> str:
        return merge_card_words(
            feature_id="FEAT-39F6",
            branch="fix/TASK-FEAT39F6FIX1-10141815",
            gates=GatesReport(
                status=GateStatus.GREEN,
                detail="the tests this repository declares came back green",
                test_changes=counts,
            ),
        )

    def test_the_line_is_on_the_card_between_the_checks_and_the_choice(
        self,
    ) -> None:
        card = self._card(
            ChangedTestsReport(
                files_changed=1,
                tests_deleted=2,
                assertions_removed=3,
                files=(ROUTER,),
            )
        )

        assert "worth a look before you merge." in card
        assert card.index("What was checked") < card.index("This branch deleted")
        assert card.index("This branch deleted") < card.index("Approve =")

    def test_a_card_with_no_count_is_the_card_it_always_was(self) -> None:
        assert self._card(None) == (
            "FEAT-39F6 is ready to merge on branch "
            "fix/TASK-FEAT39F6FIX1-10141815. What was checked: the tests "
            "this repository declares came back green. Approve = check the "
            "candidate in the sandbox, merge the branch into main and "
            "promote it. Reject = nothing changes; the branch is kept "
            "either way."
        )

    def test_the_card_says_nothing_this_estate_only_says_to_itself(self) -> None:
        """He reads this in Slack. No house shorthand, and no identifier that
        means something only inside the factory."""
        for counts in (
            ChangedTestsReport(),
            ChangedTestsReport(files_changed=3),
            ChangedTestsReport(files_changed=3, read_whole=False),
            ChangedTestsReport(
                files_changed=1, tests_deleted=1, assertions_removed=2, files=(ROUTER,)
            ),
        ):
            card = self._card(counts).lower()
            for word in (
                "fence",
                "checkpoint",
                "gate",
                "lane",
                "leg",
                "ruling",
                "toolchain",
                "worktree",
                "build_id",
                "name_status",
                "patch",
                "diff",
                "pytest",
                "git",
                "sha",
                "head",
                "mode-c",
                "specification",
            ):
                assert word not in card, (word, card)
