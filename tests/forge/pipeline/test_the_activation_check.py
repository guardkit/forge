"""Publication cannot be switched on until three things hold.

1. nothing is built inside the coordinator (a setting);
2. the publisher's credential file is named, and named in no other settings;
3. the publisher passed its start-up self-check (asked of the publisher).

Publication switches on only when every one answers yes, and refuses in plain
words naming the ones that do not. A publisher nobody could ask is a refusal.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge.pipeline.publication_activation import (
    THE_QUESTIONS,
    WhatTheMachineSays,
    run_the_activation_check,
    what_is_true_here,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    say_where_publication_stands_at_boot,
    the_activation_check,
    the_machine_now,
    why_publication_is_off,
)

#: The publisher said it passed its start-up self-check.
IT_PASSED = WhatTheMachineSays(the_publisher_passed_its_self_check=True)


def a_config(**publication: object) -> SimpleNamespace:
    """A settings object with publication's fields and nothing else set."""
    fields = {
        "enabled": True,
        "publisher_url": "http://127.0.0.1:0",
        "builds_may_run_inside_the_coordinator": False,
        "publisher_credential_file": "/etc/forge-publisher/credential",
        "request_timeout_seconds": 300,
        "send_attempts": 3,
    }
    fields.update(publication)
    return SimpleNamespace(
        publication=SimpleNamespace(**fields),
        planning=SimpleNamespace(sandboxes={}, target_repo_paths={}),
        conductor=SimpleNamespace(launch_settings=[]),
    )


class TestEveryOneHasToHold:
    def test_they_all_hold_and_publication_switches_on(self) -> None:
        verdict = run_the_activation_check(a_config(), IT_PASSED)
        assert verdict.all_hold is True
        assert verdict.refusals == ()
        assert "checked and holds" in verdict.sentence
        assert publication_is_switched_on(a_config(), IT_PASSED) is True

    def test_there_are_exactly_three(self) -> None:
        verdict = run_the_activation_check(a_config(), IT_PASSED)
        assert len(THE_QUESTIONS) == 3
        assert [answer.name for answer in verdict.answers] == [
            "nothing-is-built-inside-the-coordinator",
            "the-credential-is-named-in-no-other-settings",
            "the-publisher-passed-its-self-check",
        ]


class TestEachOneRefusesOnItsOwn:
    def test_the_coordinator_may_still_build_inside_itself(self) -> None:
        verdict = run_the_activation_check(
            a_config(builds_may_run_inside_the_coordinator=True), IT_PASSED
        )
        assert [answer.name for answer in verdict.refusals] == [
            "nothing-is-built-inside-the-coordinator"
        ]
        assert "write the very record the publisher trusts" in verdict.sentence

    def test_the_credential_file_is_named_in_a_sandboxs_settings(self) -> None:
        config = a_config()
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(
                name="the-sandbox",
                some_file_it_is_given="/etc/forge-publisher/credential",
            )
        }
        verdict = run_the_activation_check(config, IT_PASSED)
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert "planning.sandboxes.the-sandbox.some_file_it_is_given" in (
            verdict.sentence
        )

    def test_the_credential_file_is_in_the_runners_launch_list(self) -> None:
        config = a_config()
        config.conductor.launch_settings = ["/etc/forge-publisher/credential"]
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is False
        assert "conductor.launch_settings" in verdict.sentence

    def test_the_coordinators_own_note_of_the_path_is_not_a_finding(self) -> None:
        assert run_the_activation_check(a_config(), IT_PASSED).all_hold is True

    @pytest.mark.parametrize("unset", [None, "   "])
    def test_a_credential_file_nobody_named_is_a_refusal(self, unset) -> None:
        verdict = run_the_activation_check(
            a_config(publisher_credential_file=unset), IT_PASSED
        )
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert "publication.publisher_credential_file is not set" in verdict.sentence

    def test_the_publisher_did_not_pass_its_self_check(self) -> None:
        machine = WhatTheMachineSays(the_publisher_passed_its_self_check=False)
        verdict = run_the_activation_check(a_config(), machine)
        assert [answer.name for answer in verdict.refusals] == [
            "the-publisher-passed-its-self-check"
        ]
        assert "without having passed" in verdict.sentence
        assert publication_is_switched_on(a_config(), machine) is False


class TestAPublisherNobodyCouldAskIsNotAPass:
    def test_nobody_asked_at_all(self) -> None:
        verdict = run_the_activation_check(a_config(), None)
        assert [answer.name for answer in verdict.refusals] == [
            "the-publisher-passed-its-self-check"
        ]
        assert "could not be asked" in verdict.sentence
        assert publication_is_switched_on(a_config()) is False

    def test_the_reason_it_could_not_be_asked_is_said(self) -> None:
        machine = WhatTheMachineSays(why_nobody_has_looked="it did not answer")
        assert "it did not answer" in why_publication_is_off(a_config(), machine)

    def test_a_reader_that_breaks_has_not_looked(self) -> None:
        def broken() -> WhatTheMachineSays:
            raise RuntimeError("boom")

        said = the_machine_now(broken)
        assert said.the_publisher_passed_its_self_check is None
        assert "RuntimeError" in (said.why_nobody_has_looked or "")
        assert publication_is_switched_on(a_config(), broken) is False

    def test_a_reader_is_asked_afresh_each_time(self) -> None:
        answers = [IT_PASSED, WhatTheMachineSays()]
        reader = lambda: answers.pop(0)  # noqa: E731
        assert publication_is_switched_on(a_config(), reader) is True
        assert publication_is_switched_on(a_config(), reader) is False

    def test_a_settings_object_of_an_unexpected_shape_is_not_a_pass(self) -> None:
        assert publication_is_switched_on(object(), IT_PASSED) is False
        assert publication_is_switched_on(None, IT_PASSED) is False

        class _AnswersYesToEverything:
            def __getattr__(self, name: str) -> bool:  # noqa: D105
                return True

        assert publication_is_switched_on(_AnswersYesToEverything()) is False


class TestTheSettingIsNotPermission:
    def test_no_setting_no_publication(self) -> None:
        assert publication_is_switched_on(a_config(enabled=False), IT_PASSED) is False
        assert "no setting turns publication on" in why_publication_is_off(
            a_config(enabled=False), IT_PASSED
        )

    def test_with_the_setting_off_the_publisher_is_not_asked(self) -> None:
        asked: list[int] = []

        def reader() -> WhatTheMachineSays:
            asked.append(1)
            return IT_PASSED

        assert publication_is_switched_on(a_config(enabled=False), reader) is False
        assert asked == []

    def test_the_check_is_asked_again_every_time(self) -> None:
        config = a_config()
        assert publication_is_switched_on(config, IT_PASSED) is True
        config.publication.builds_may_run_inside_the_coordinator = True
        assert publication_is_switched_on(config, IT_PASSED) is False
        assert "may still start builds" in why_publication_is_off(config, IT_PASSED)


class TestWhatIsTrueIsGatheredInOnePlace:
    def test_no_setting_at_all_reads_as_the_old_behaviour(self) -> None:
        true = what_is_true_here(SimpleNamespace(), None)
        assert true.builds_may_run_inside_the_coordinator is True
        assert true.the_credential_file is None
        assert true.where_the_credential_file_is_named == ()

    def test_the_switch_and_the_check_agree(self) -> None:
        assert the_activation_check(a_config(), IT_PASSED).all_hold is True
        assert the_activation_check(a_config(), None).all_hold is False

    def test_every_answer_says_something_a_person_can_act_on(self) -> None:
        verdict = run_the_activation_check(
            a_config(builds_may_run_inside_the_coordinator=True), None
        )
        for answer in verdict.answers:
            assert answer.said
            assert answer.question.endswith("?")
        assert verdict.to_wire()["all_hold"] is False


class TestTheBootLine:
    def test_off_and_why_when_no_setting_turns_it_on(self) -> None:
        line = say_where_publication_stands_at_boot(a_config(enabled=False), IT_PASSED)
        assert line.startswith("publication is OFF: ")
        assert "no setting turns publication on" in line

    def test_off_and_which_condition_failed(self) -> None:
        line = say_where_publication_stands_at_boot(
            a_config(builds_may_run_inside_the_coordinator=True), IT_PASSED
        )
        assert line.startswith("publication is OFF: ")
        assert "may still start builds" in line

    def test_off_when_the_publisher_could_not_be_asked(self) -> None:
        line = say_where_publication_stands_at_boot(a_config(), None)
        assert line.startswith("publication is OFF: ")
        assert "could not be asked" in line

    def test_on_when_every_condition_holds(self) -> None:
        line = say_where_publication_stands_at_boot(a_config(), IT_PASSED)
        assert line.startswith("publication is ON: ")

    def test_a_settings_object_of_an_unexpected_shape_does_not_stop_a_boot(
        self,
    ) -> None:
        assert say_where_publication_stands_at_boot(object()).startswith(
            "publication is OFF: "
        )
        assert say_where_publication_stands_at_boot(None).startswith(
            "publication is OFF: "
        )
