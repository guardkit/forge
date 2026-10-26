"""The one line on the merge card about the files this build changed beyond
its plan — word for word, against the design of record.

The design is ``ai-transition/docs/planner-fix-design-2026-09-15.md`` §2f, and
these tests compare the card's sentences with it CHARACTER FOR CHARACTER,
built by the same helper the repair card's tests line already uses. They also
pin the rule that keeps the line honest: a count nobody took is never
published as a count of nothing — the empty string, and the card is byte for
byte the card that shipped before this.

Facts, not verdicts (step 2, 26 October 2026): the card no longer says a
build "answers at a web address the request did not name" or "added" a
capability "which the request did not ask for". Those lines read an ``echo``
line and a file path as routes and CORS headers as permissions.
"""

from __future__ import annotations

from forge.cli._serve_gate_activation import card_line_about_scope, merge_card_words
from forge.pipeline.scope_report import ScopeReport

#: §2f, the first shape, with the five files the design worked through.
SOMETHING_OUTSIDE_THE_PLAN = (
    "This build also changed 5 files the plan did not name: "
    "src/analytics/schema.py, src/analytics/crud.py and "
    "src/analytics/router.py and 2 more files — worth a look before you merge."
)

#: Nothing outside the plan.
NOTHING_OUTSIDE_THE_PLAN = "Every file this build changed was named in the plan."

#: The branch could not be read.
COULD_NOT_BE_READ = "Which files this build changed could not be read here."


def _clean() -> ScopeReport:
    return ScopeReport(read=True, plan_read=True)


class TestTheShapes:
    def test_files_outside_the_plan_read_exactly_as_the_design_says(self) -> None:
        report = _clean()
        report.files_the_plan_did_not_name = [
            "src/analytics/schema.py",
            "src/analytics/crud.py",
            "src/analytics/router.py",
            "src/analytics/service.py",
            "src/analytics/models.py",
        ]
        assert card_line_about_scope(report) == SOMETHING_OUTSIDE_THE_PLAN

    def test_one_file_outside_the_plan_says_file_not_files(self) -> None:
        report = _clean()
        report.files_the_plan_did_not_name = ["src/analytics/schema.py"]
        assert card_line_about_scope(report) == (
            "This build also changed 1 file the plan did not name: "
            "src/analytics/schema.py — worth a look before you merge."
        )

    def test_nothing_outside_the_plan(self) -> None:
        assert card_line_about_scope(_clean()) == NOTHING_OUTSIDE_THE_PLAN

    def test_the_branch_could_not_be_read(self) -> None:
        report = ScopeReport(read=False, why_not="git is not there")
        assert card_line_about_scope(report) == COULD_NOT_BE_READ

    def test_nobody_counted_is_the_empty_string(self) -> None:
        assert card_line_about_scope(None) == ""


class TestNeverAcountNobodyTook:
    def test_a_plan_that_named_no_files_claims_nothing_about_files(self) -> None:
        """Every plan written before task documents declared their files says
        nothing about files, and the card must not read that as a clean
        build."""
        report = ScopeReport(read=True, plan_read=False)
        report.files_changed = 8
        assert card_line_about_scope(report) == ""

    def test_files_outside_the_plan_are_not_claimed_when_the_plan_named_none(
        self,
    ) -> None:
        report = ScopeReport(read=True, plan_read=False)
        report.files_the_plan_did_not_name = ["src/analytics/schema.py"]
        assert card_line_about_scope(report) == ""


class TestFactsNotVerdicts:
    def test_the_card_says_nothing_about_routes_or_capabilities(self) -> None:
        """First step 1 run, feature 2: the card said the build serves
        ``${folder}/node_modules`` (read from an ``echo`` line) and the local
        merge judge declined good work over it. The report has no such field
        any more, and the line names only files."""
        report = _clean()
        assert not hasattr(report, "routes_the_request_did_not_name")
        assert not hasattr(report, "capabilities_the_request_did_not_name")
        line = card_line_about_scope(report)
        assert "web address" not in line
        assert "did not ask for" not in line
        assert "routes_the_request_did_not_name" not in report.to_dict()
        assert "capabilities_the_request_did_not_name" not in report.to_dict()


class TestTheRepairCardSaysTheSameSentence:
    def test_the_repair_card_carries_the_scope_line(self) -> None:
        report = _clean()
        report.files_the_plan_did_not_name = ["src/analytics/schema.py"]
        words = merge_card_words(
            feature_id="FEAT-CARD", branch="fix/TASK-X-0001", scope=report
        )
        assert "src/analytics/schema.py — worth a look before you merge." in words

    def test_the_repair_card_without_a_report_is_what_it_always_was(self) -> None:
        before = merge_card_words(feature_id="FEAT-CARD", branch="fix/TASK-X-0001")
        after = merge_card_words(
            feature_id="FEAT-CARD", branch="fix/TASK-X-0001", scope=None
        )
        assert before == after
        assert "could not be read here" not in before
