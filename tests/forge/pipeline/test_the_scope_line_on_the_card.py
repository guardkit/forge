"""The one line on the merge card about what this build did beyond what was
asked for — word for word, against the design of record.

The design is ``ai-transition/docs/planner-fix-design-2026-09-15.md`` §2f, and
these tests compare the card's sentences with it CHARACTER FOR CHARACTER,
built by the same helper the repair card's tests line already uses. They also
pin the two rules that keep the line honest:

* a count nobody took is never published as a count of nothing — the empty
  string, and the card is byte for byte the card that shipped before this;
* the comparison with the plan and the comparison with the request are
  INDEPENDENT: a build that changed only files the plan named must still say
  it answered at a web address the request never asked for.
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

#: §2f, the second shape.
A_WEB_ADDRESS_NOBODY_ASKED_FOR = (
    "It also answers at a web address the request did not name: "
    "/stats/users-created-per-day."
)

#: §2f, the second shape's other half.
A_CAPABILITY_NOBODY_ASKED_FOR = (
    "It also added a database migration, which the request did not ask for."
)

#: §2f, the third shape.
NOTHING_OUTSIDE_EITHER = (
    "Every file this build changed was named in the plan, and it added "
    "nothing the request did not ask for."
)

#: §2f, the fourth shape.
COULD_NOT_BE_READ = "Which files this build changed could not be read here."


def _clean() -> ScopeReport:
    return ScopeReport(read=True, plan_read=True, routes_read=True)


class TestTheFiveShapes:
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

    def test_a_web_address_the_request_did_not_name(self) -> None:
        report = _clean()
        report.routes_the_request_did_not_name = ["/stats/users-created-per-day"]
        assert card_line_about_scope(report) == A_WEB_ADDRESS_NOBODY_ASKED_FOR

    def test_a_capability_the_request_did_not_ask_for(self) -> None:
        report = _clean()
        report.capabilities_the_request_did_not_name = ["a database migration"]
        assert card_line_about_scope(report) == A_CAPABILITY_NOBODY_ASKED_FOR

    def test_the_first_two_shapes_may_both_appear(self) -> None:
        report = _clean()
        report.files_the_plan_did_not_name = [
            "src/analytics/schema.py",
            "src/analytics/crud.py",
            "src/analytics/router.py",
            "src/analytics/service.py",
            "src/analytics/models.py",
        ]
        report.routes_the_request_did_not_name = ["/stats/users-created-per-day"]
        report.capabilities_the_request_did_not_name = ["a database migration"]
        assert card_line_about_scope(report) == " ".join(
            (
                SOMETHING_OUTSIDE_THE_PLAN,
                A_WEB_ADDRESS_NOBODY_ASKED_FOR,
                A_CAPABILITY_NOBODY_ASKED_FOR,
            )
        )

    def test_nothing_outside_the_plan_and_nothing_outside_the_request(self) -> None:
        assert card_line_about_scope(_clean()) == NOTHING_OUTSIDE_EITHER

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
        report = ScopeReport(read=True, plan_read=False, routes_read=True)
        report.files_changed = 8
        assert card_line_about_scope(report) == ""

    def test_the_request_half_alone_still_speaks(self) -> None:
        report = ScopeReport(read=True, plan_read=False, routes_read=True)
        report.routes_the_request_did_not_name = ["/stats/users-created-per-day"]
        assert card_line_about_scope(report) == A_WEB_ADDRESS_NOBODY_ASKED_FOR

    def test_the_plan_half_alone_still_speaks(self) -> None:
        report = ScopeReport(read=True, plan_read=True, routes_read=False)
        report.files_the_plan_did_not_name = ["src/analytics/schema.py"]
        assert card_line_about_scope(report) == (
            "This build also changed 1 file the plan did not name: "
            "src/analytics/schema.py — worth a look before you merge."
        )

    def test_a_clean_plan_half_with_no_request_half_says_nothing(self) -> None:
        report = ScopeReport(read=True, plan_read=True, routes_read=False)
        assert card_line_about_scope(report) == ""

    def test_files_outside_the_plan_are_not_claimed_when_the_plan_named_none(
        self,
    ) -> None:
        report = ScopeReport(read=True, plan_read=False, routes_read=False)
        report.files_the_plan_did_not_name = ["src/analytics/schema.py"]
        assert card_line_about_scope(report) == ""


class TestTheTwoComparisonsAreIndependent:
    def test_every_file_was_in_the_plan_and_the_web_address_still_moved(self) -> None:
        """Rich's item 5: a build that stayed inside its plan can still have
        built the wrong thing, and that is a different question from the blast
        radius."""
        report = _clean()
        report.files_changed = 2
        report.files_the_plan_named = ["src/users/router.py", "src/users/crud.py"]
        report.files_the_plan_did_not_name = []
        report.routes_the_request_did_not_name = ["/stats/users-created-per-day"]
        line = card_line_about_scope(report)
        assert line == A_WEB_ADDRESS_NOBODY_ASKED_FOR
        assert "the plan did not name" not in line


class TestTheRepairCardSaysTheSameSentence:
    def test_the_repair_card_carries_the_scope_line(self) -> None:
        report = _clean()
        report.routes_the_request_did_not_name = ["/stats/users-created-per-day"]
        words = merge_card_words(
            feature_id="FEAT-CARD", branch="fix/TASK-X-0001", scope=report
        )
        assert A_WEB_ADDRESS_NOBODY_ASKED_FOR in words

    def test_the_repair_card_without_a_report_is_what_it_always_was(self) -> None:
        before = merge_card_words(feature_id="FEAT-CARD", branch="fix/TASK-X-0001")
        after = merge_card_words(
            feature_id="FEAT-CARD", branch="fix/TASK-X-0001", scope=None
        )
        assert before == after
        assert "could not be read here" not in before
