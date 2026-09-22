"""Publication cannot be switched on until the isolation is proven.

One-true-copy design pass, item 1, third revision section G. Six named
questions; publication switches on only when every one of them answers yes,
and refuses in plain words naming the ones that do not.

WHICH OF THESE ARE PROVEN HERE, AND WHICH ONLY AT ROLLOUT — the honest split,
pinned by a test of its own below so that it cannot quietly change:

* **here** — question 1 (the setting that permits builds inside the
  coordinator) and the settings half of question 6 (the credential file is
  named at all, and named in no other settings);
* **only at rollout** — questions 2, 3, 4 and 5 (a sandbox writing the
  settings file, seeing the ledger, reaching the publisher, and what else is
  on the publisher's network) and the readability half of question 6. Each is
  asked of a stand-in here, one field at a time, which proves the REFUSAL
  works; what it cannot prove is the real machine.
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
    why_publication_is_off,
)

#: A machine somebody has looked at, and every wall is where it should be.
ALL_WALLS_STAND = WhatTheMachineSays(
    a_sandbox_can_write_the_coordinators_settings_file=False,
    a_sandbox_can_see_the_ledger=False,
    a_sandbox_can_reach_the_publisher=False,
    the_credential_file_can_be_read_by_them=False,
    only_the_coordinator_is_on_the_publishers_network=True,
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


class TestEveryOneHasToHold:
    def test_they_all_hold_and_publication_switches_on(self) -> None:
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        assert verdict.all_hold is True
        assert verdict.refusals == ()
        assert "checked and holds" in verdict.sentence
        assert publication_is_switched_on(a_config(), ALL_WALLS_STAND) is True

    def test_there_are_exactly_six(self) -> None:
        verdict = run_the_activation_check(a_config(), ALL_WALLS_STAND)
        assert len(verdict.answers) == 6
        assert len(THE_QUESTIONS) == 6
        assert [answer.name for answer in verdict.answers] == [
            "nothing-is-built-inside-the-coordinator",
            "no-sandbox-can-write-the-coordinators-settings-file",
            "no-sandbox-can-see-the-ledger",
            "no-sandbox-can-reach-the-publisher",
            "only-the-coordinator-is-on-the-publishers-network",
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
                    "only_the_coordinator_is_on_the_publishers_network": True,
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
            "only-the-coordinator-is-on-the-publishers-network",
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
            "only-the-coordinator-is-on-the-publishers-network": False,
            # The one with two halves reports the half that decided it: with
            # every wall standing the answer came from the machine, so it is
            # a rollout answer.
            "the-credential-is-out-of-their-reach": False,
        }

    def test_the_settings_half_of_the_sixth_IS_provable_here(self) -> None:
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


class TestWhoElseIsOnThePublishersNetwork:
    """The sixth question, added 22 September 2026 with the way it is made true.

    The publisher listens on every address inside its own container and
    publishes no port, so what can reach it is exactly what is on its network.
    "No sandbox can reach the publisher" is therefore a thing somebody can
    COUNT, and this is the counting.
    """

    def _machine(self, **fields: object) -> WhatTheMachineSays:
        return WhatTheMachineSays(
            **{
                **{
                    "a_sandbox_can_write_the_coordinators_settings_file": False,
                    "a_sandbox_can_see_the_ledger": False,
                    "a_sandbox_can_reach_the_publisher": False,
                    "the_credential_file_can_be_read_by_them": False,
                    "only_the_coordinator_is_on_the_publishers_network": True,
                },
                **fields,
            }
        )

    def test_something_else_is_on_it(self) -> None:
        verdict = run_the_activation_check(
            a_config(),
            self._machine(only_the_coordinator_is_on_the_publishers_network=False),
        )
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [
            "only-the-coordinator-is-on-the-publishers-network"
        ]
        assert "network of its own" in verdict.sentence
        assert publication_is_switched_on(
            a_config(),
            self._machine(only_the_coordinator_is_on_the_publishers_network=False),
        ) is False

    def test_nobody_has_counted(self) -> None:
        verdict = run_the_activation_check(
            a_config(),
            self._machine(only_the_coordinator_is_on_the_publishers_network=None),
        )
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [
            "only-the-coordinator-is-on-the-publishers-network"
        ]
        assert "nobody has counted" in verdict.sentence
        assert "at rollout" in verdict.sentence

    def test_the_coordinator_alone_is_on_it(self) -> None:
        verdict = run_the_activation_check(a_config(), self._machine())
        assert verdict.all_hold is True


class TestACredentialFileNobodyNamedIsARefusal:
    """The reviewer's second finding, 22 September 2026.

    The settings half of the sixth question is "is this exact path named
    anywhere else?". With no path there is nothing to look for, the search
    finds nothing, and nothing-found used to read as an all-clear: the check
    passed with the setting unset. It does not any more.
    """

    def test_with_no_path_the_question_refuses(self) -> None:
        verdict = run_the_activation_check(
            a_config(publisher_credential_file=None), ALL_WALLS_STAND
        )
        assert verdict.all_hold is False
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-out-of-their-reach"
        ]
        assert "publication.publisher_credential_file is not set" in verdict.sentence

    def test_it_refuses_even_when_the_machine_says_nobody_can_read_it(self) -> None:
        """The machine's answer is about a file nobody named. It cannot stand in."""
        assert (
            publication_is_switched_on(
                a_config(publisher_credential_file=None), ALL_WALLS_STAND
            )
            is False
        )

    def test_an_empty_setting_is_the_same_as_no_setting(self) -> None:
        verdict = run_the_activation_check(
            a_config(publisher_credential_file="   "), ALL_WALLS_STAND
        )
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-out-of-their-reach"
        ]

    def test_the_settings_half_settles_it_without_the_machine(self) -> None:
        verdict = run_the_activation_check(
            a_config(publisher_credential_file=None), None
        )
        sixth = next(
            answer
            for answer in verdict.answers
            if answer.name == "the-credential-is-out-of-their-reach"
        )
        assert sixth.provable_here is True
        assert sixth.holds is False


class TestTheBootLine:
    """One line at coordinator start, saying where publication stands.

    Section G: *the check is run again each time the coordinator starts*. The
    press asks per press; this is the line the person who started the
    coordinator reads.
    """

    def test_off_and_why_when_no_setting_turns_it_on(self) -> None:
        line = say_where_publication_stands_at_boot(
            a_config(enabled=False), ALL_WALLS_STAND
        )
        assert line.startswith("publication is OFF: ")
        assert "no setting turns publication on" in line

    def test_off_and_which_condition_failed(self) -> None:
        line = say_where_publication_stands_at_boot(
            a_config(builds_may_run_inside_the_coordinator=True), ALL_WALLS_STAND
        )
        assert line.startswith("publication is OFF: ")
        assert "may still start builds" in line

    def test_off_because_nobody_has_looked_which_is_today(self) -> None:
        line = say_where_publication_stands_at_boot(a_config(), None)
        assert line.startswith("publication is OFF: ")
        assert "nobody has looked" in line

    def test_on_when_every_condition_holds(self) -> None:
        line = say_where_publication_stands_at_boot(a_config(), ALL_WALLS_STAND)
        assert line.startswith("publication is ON: ")

    def test_it_is_said_once_and_in_the_log(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="forge.pipeline.publication_switch"):
            say_where_publication_stands_at_boot(a_config(), ALL_WALLS_STAND)
        said = [
            record.getMessage()
            for record in caplog.records
            if "publication at boot" in record.getMessage()
        ]
        assert len(said) == 1

    def test_a_settings_object_of_an_unexpected_shape_does_not_stop_a_boot(
        self,
    ) -> None:
        assert say_where_publication_stands_at_boot(object()).startswith(
            "publication is OFF: "
        )
        assert say_where_publication_stands_at_boot(None).startswith(
            "publication is OFF: "
        )
