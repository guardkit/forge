"""Publication cannot be switched on until the isolation is proven.

One-true-copy design pass, item 1, third revision section G. Five named
questions; publication switches on only when every one of them answers yes,
and refuses in plain words naming the ones that do not.

WHICH OF THESE ARE PROVEN HERE, AND WHICH ONLY AT ROLLOUT — the honest split,
pinned by a test of its own below so that it cannot quietly change:

* **here** — question 1 (the setting that permits builds inside the
  coordinator) and the settings half of question 5 (the credential file is
  named in no other settings);
* **only at rollout** — questions 2, 3 and 4 (a sandbox writing the settings
  file, seeing the ledger, reaching the publisher) and the readability half
  of question 5. Each is asked of a stand-in here, one field at a time, which
  proves the REFUSAL works; what it cannot prove is the real machine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge.pipeline.publication_activation import (
    THE_FIVE_QUESTIONS,
    WhatTheMachineSays,
    run_the_activation_check,
    what_is_true_here,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    the_activation_check,
    why_publication_is_off,
)

#: A machine somebody has looked at, and every wall is where it should be.
ALL_WALLS_STAND = WhatTheMachineSays(
    a_sandbox_can_write_the_coordinators_settings_file=False,
    a_sandbox_can_see_the_ledger=False,
    a_sandbox_can_reach_the_publisher=False,
    the_credential_file_can_be_read_by_them=False,
    looked_at_by="a stand-in, in a test",
)


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


class TestAllFiveHaveToHold:
    def test_they_all_hold_and_publication_switches_on(self) -> None:
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        assert verdict.all_hold is True
        assert verdict.refusals == ()
        assert "checked and holds" in verdict.sentence
        assert publication_is_switched_on(a_config(), ALL_WALLS_STAND) is True

    def test_there_are_exactly_five(self) -> None:
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        assert len(verdict.answers) == 5
        assert len(THE_FIVE_QUESTIONS) == 5
        assert [answer.name for answer in verdict.answers] == [
            "nothing-is-built-inside-the-coordinator",
            "no-sandbox-can-write-the-coordinators-settings-file",
            "no-sandbox-can-see-the-ledger",
            "no-sandbox-can-reach-the-publisher",
            "the-credential-is-out-of-their-reach",
        ]


class TestEachOneRefusesOnItsOwn:
    def test_the_coordinator_may_still_build_inside_itself(self) -> None:
        verdict = run_the_activation_check(
            a_config(builds_may_run_inside_the_coordinator=True), ALL_WALLS_STAND
        )
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [
            "nothing-is-built-inside-the-coordinator"
        ]
        assert "write the very record the publisher trusts" in verdict.sentence

    @pytest.mark.parametrize(
        "field, name, says",
        [
            (
                "a_sandbox_can_write_the_coordinators_settings_file",
                "no-sandbox-can-write-the-coordinators-settings-file",
                "which projects are registered",
            ),
            (
                "a_sandbox_can_see_the_ledger",
                "no-sandbox-can-see-the-ledger",
                "could forge a passed record",
            ),
            (
                "a_sandbox_can_reach_the_publisher",
                "no-sandbox-can-reach-the-publisher",
                "could ask for a send",
            ),
            (
                "the_credential_file_can_be_read_by_them",
                "the-credential-is-out-of-their-reach",
                "publisher's own user",
            ),
        ],
    )
    def test_one_wall_is_down(self, field: str, name: str, says: str) -> None:
        machine = WhatTheMachineSays(
            **{
                **{
                    "a_sandbox_can_write_the_coordinators_settings_file": False,
                    "a_sandbox_can_see_the_ledger": False,
                    "a_sandbox_can_reach_the_publisher": False,
                    "the_credential_file_can_be_read_by_them": False,
                },
                field: True,
            }
        )
        verdict = run_the_activation_check(a_config(), machine)
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [name]
        assert says in verdict.sentence
        assert publication_is_switched_on(a_config(), machine) is False

    def test_the_credential_file_is_named_in_a_sandboxs_settings(self) -> None:
        config = a_config()
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(
                name="the-sandbox",
                some_file_it_is_given="/etc/forge-publisher/credential",
            )
        }
        verdict = run_the_activation_check(config, ALL_WALLS_STAND)
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-out-of-their-reach"
        ]
        assert "planning.sandboxes.the-sandbox.some_file_it_is_given" in (
            verdict.sentence
        )

    def test_the_credential_file_is_in_the_runners_launch_list(self) -> None:
        config = a_config()
        config.conductor.launch_settings = ["/etc/forge-publisher/credential"]
        verdict = run_the_activation_check(config, ALL_WALLS_STAND)
        assert verdict.all_hold is False
        assert "conductor.launch_settings" in verdict.sentence

    def test_the_coordinators_own_note_of_the_path_is_not_a_finding(self) -> None:
        """It has to know the path to look for it anywhere else."""
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        assert verdict.all_hold is True


class TestAnUnexaminedWallIsNotAWall:
    def test_nobody_has_looked_at_all(self) -> None:
        verdict = run_the_activation_check(a_config(), None)
        assert verdict.all_hold is False
        names = [answer.name for answer in verdict.refusals]
        assert names == [
            "no-sandbox-can-write-the-coordinators-settings-file",
            "no-sandbox-can-see-the-ledger",
            "no-sandbox-can-reach-the-publisher",
            "the-credential-is-out-of-their-reach",
        ]
        assert "nobody has looked" in verdict.sentence
        assert "at rollout" in verdict.sentence

    def test_that_is_where_publication_stands_today(self) -> None:
        """Nothing wires a real look yet, so publication is off. Fail closed."""
        assert publication_is_switched_on(a_config()) is False
        assert "nobody has looked" in why_publication_is_off(a_config())

    def test_a_settings_object_of_an_unexpected_shape_is_not_a_pass(self) -> None:
        assert publication_is_switched_on(object(), ALL_WALLS_STAND) is False
        assert publication_is_switched_on(None, ALL_WALLS_STAND) is False

        class _AnswersYesToEverything:
            def __getattr__(self, name: str) -> bool:  # noqa: D105
                return True

        assert publication_is_switched_on(_AnswersYesToEverything()) is False


class TestTheSettingIsNotPermission:
    def test_no_setting_no_publication(self) -> None:
        assert publication_is_switched_on(a_config(enabled=False), ALL_WALLS_STAND) is (
            False
        )
        assert "no setting turns publication on" in why_publication_is_off(
            a_config(enabled=False), ALL_WALLS_STAND
        )

    def test_the_check_is_asked_again_every_time(self) -> None:
        """A condition that becomes false while it is on takes it off again."""
        config = a_config()
        assert publication_is_switched_on(config, ALL_WALLS_STAND) is True
        config.publication.builds_may_run_inside_the_coordinator = True
        assert publication_is_switched_on(config, ALL_WALLS_STAND) is False
        assert "build or check inside itself" not in why_publication_is_off(
            config, ALL_WALLS_STAND
        )
        assert "may still start builds" in why_publication_is_off(
            config, ALL_WALLS_STAND
        )


class TestWhatCanBeProvenHereAndWhatOnlyAtRollout:
    def test_the_split_is_written_down_in_the_answers_themselves(self) -> None:
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        provable = {
            answer.name: answer.provable_here for answer in verdict.answers
        }
        assert provable == {
            "nothing-is-built-inside-the-coordinator": True,
            "no-sandbox-can-write-the-coordinators-settings-file": False,
            "no-sandbox-can-see-the-ledger": False,
            "no-sandbox-can-reach-the-publisher": False,
            # The one with two halves reports the half that decided it: with
            # every wall standing the answer came from the machine, so it is
            # a rollout answer.
            "the-credential-is-out-of-their-reach": False,
        }

    def test_the_settings_half_of_the_fifth_IS_provable_here(self) -> None:
        config = a_config()
        config.conductor.launch_settings = ["/etc/forge-publisher/credential"]
        verdict = run_the_activation_check(config, None)
        fifth = next(
            answer
            for answer in verdict.answers
            if answer.name == "the-credential-is-out-of-their-reach"
        )
        assert fifth.provable_here is True
        assert fifth.holds is False

    def test_every_answer_says_something_a_person_can_act_on(self) -> None:
        verdict = run_the_activation_check(
            a_config(builds_may_run_inside_the_coordinator=True), None
        )
        for answer in verdict.answers:
            assert answer.said
            assert answer.question.endswith("?")
        assert verdict.to_wire()["all_hold"] is False


class TestWhatIsTrueIsGatheredInOnePlace:
    def test_no_setting_at_all_reads_as_the_old_behaviour(self) -> None:
        """A forge that says nothing builds inside itself, which is today."""
        true = what_is_true_here(SimpleNamespace(), None)
        assert true.builds_may_run_inside_the_coordinator is True
        assert true.the_credential_file is None
        assert true.where_the_credential_file_is_named == ()

    def test_the_switch_and_the_check_agree(self) -> None:
        assert the_activation_check(a_config(), ALL_WALLS_STAND).all_hold is True
        assert the_activation_check(a_config(), None).all_hold is False
