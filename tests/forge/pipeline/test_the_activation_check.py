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


#: The host folder the publisher's per-route keys are in (13 October 2026).
ROUTE_KEYS = "/srv/publisher/route-keys"


class TestTheRouteKeysFolderIsNamedNowhereElse:
    """The coordinator records the folder, and the same search that protects
    the publisher-wide file protects the folder and every path inside it — by
    path text alone, never opening anything. A key added to the folder later
    is covered with no change here."""

    def _refused_in(self, config, where: str) -> None:
        verdict = run_the_activation_check(config, IT_PASSED)
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert (
            "names a path component equal to the protected folder's name "
            "'route-keys'" in verdict.sentence
        )
        assert where in verdict.sentence
        assert publication_is_switched_on(config, IT_PASSED) is False

    def test_a_route_key_path_in_launch_settings_refuses(self) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.conductor.launch_settings = [f"{ROUTE_KEYS}/widget-shop-key"]
        self._refused_in(config, "conductor.launch_settings")

    def test_a_route_key_written_inside_a_longer_launch_setting_refuses(self) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.conductor.launch_settings = [f"KEY_FILE={ROUTE_KEYS}/a"]
        self._refused_in(config, "conductor.launch_settings")

    def test_a_route_key_path_in_sandbox_settings_refuses(self) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(
                name="the-sandbox", mounts=[f"{ROUTE_KEYS}//a"]
            )
        }
        self._refused_in(config, "planning.sandboxes.the-sandbox.mounts")

    @pytest.mark.parametrize("written", [ROUTE_KEYS, ROUTE_KEYS + "/", ROUTE_KEYS + "/."])
    def test_the_route_keys_dir_itself_named_refuses(self, written: str) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_folder=written)
        }
        self._refused_in(config, "planning.sandboxes.the-sandbox.some_folder")

    @pytest.mark.parametrize(
        "elsewhere",
        [ROUTE_KEYS + "2", ROUTE_KEYS + "2/a", ROUTE_KEYS + "-old/a", ROUTE_KEYS + ".bak/a"],
    )
    def test_a_sibling_folder_with_a_longer_name_does_not_match(self, elsewhere: str) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.conductor.launch_settings = [elsewhere]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_folder=elsewhere)
        }
        # The folder WAS searched for, and found nowhere.
        true = what_is_true_here(config, IT_PASSED)
        assert true.the_route_keys_dir == ROUTE_KEYS
        assert true.where_the_route_keys_dir_is_named == ()
        assert run_the_activation_check(config, IT_PASSED).all_hold is True

    def test_every_path_isolated_activates(self) -> None:
        """The positive control: the folder recorded, named nowhere else."""
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.conductor.launch_settings = ["/srv/forge/launch.env"]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", mounts=["/srv/projects"])
        }
        true = what_is_true_here(config, IT_PASSED)
        assert true.the_route_keys_dir == ROUTE_KEYS
        assert true.where_the_route_keys_dir_is_named == ()
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is True, verdict.sentence
        assert publication_is_switched_on(config, IT_PASSED) is True

    def test_without_the_setting_nothing_changes(self) -> None:
        config = a_config()
        config.conductor.launch_settings = [f"{ROUTE_KEYS}/a"]
        assert what_is_true_here(config, IT_PASSED).the_route_keys_dir is None
        assert run_the_activation_check(config, IT_PASSED).all_hold is True

    def test_the_real_settings_model_takes_the_field(self) -> None:
        from forge.config.models import ForgeConfig

        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "publication": {
                    "publisher_credential_file": "/etc/forge-publisher/credential",
                    "publisher_route_keys_dir": ROUTE_KEYS,
                    "builds_may_run_inside_the_coordinator": False,
                },
            }
        )
        assert config.publication.publisher_route_keys_dir == ROUTE_KEYS
        true = what_is_true_here(config, IT_PASSED)
        assert true.the_route_keys_dir == ROUTE_KEYS
        assert true.where_the_route_keys_dir_is_named == ()


class TestEveryWayOfSpellingTheFolderIsCaught:
    """Codex, implementation round 1 (R1): path text has many spellings of one
    folder. Each path in a value is taken out and normalised, and so is the
    recorded folder, before they are compared."""

    @pytest.mark.parametrize(
        "written",
        [
            "KEY_FILE=/srv/publisher/./route-keys/a",
            "//srv/publisher/route-keys/a",
            "/srv/publisher/route-keys/../route-keys/a",
            "/srv//publisher/route-keys/a",
            "/opt/a:/srv/publisher/route-keys:/opt/b",
            "--mount='/srv/publisher/route-keys/a'",
            "a=1, b=/srv/publisher/route-keys/k; c=2",
        ],
    )
    def test_an_equivalent_spelling_refuses(self, written: str) -> None:
        for recorded in (ROUTE_KEYS, ROUTE_KEYS + "/", "//srv/publisher/./route-keys/"):
            config = a_config(publisher_route_keys_dir=recorded)
            config.conductor.launch_settings = [written]
            config.planning.sandboxes = {
                "the-sandbox": SimpleNamespace(name="the-sandbox", some_file=written)
            }
            verdict = run_the_activation_check(config, IT_PASSED)
            assert verdict.all_hold is False, (recorded, written)
            assert "conductor.launch_settings" in verdict.sentence
            assert "planning.sandboxes.the-sandbox.some_file" in verdict.sentence

    @pytest.mark.parametrize(
        "elsewhere",
        [
            "/srv/publisher/route-keys2/a",
            "KEY=/srv/publisher/route-keys-old/a",
            "/opt/a:/srv/publisher/route-keys2",
            "/srv/publisher/route-keys.bak/a",
        ],
    )
    def test_a_different_folder_still_does_not_match(self, elsewhere: str) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS + "/")
        config.conductor.launch_settings = [elsewhere]
        true = what_is_true_here(config, IT_PASSED)
        assert true.where_the_route_keys_dir_is_named == ()
        assert run_the_activation_check(config, IT_PASSED).all_hold is True


class TestEveryWayOfSpellingTheCredentialFileIsCaught:
    """The same token extraction and normalisation for the publisher-wide
    file: a path in a setting has to BE that file, by any spelling."""

    @pytest.mark.parametrize(
        "written",
        ["/x/./credential", "//x/credential", "KEY=/x/credential"],
    )
    def test_an_equivalent_spelling_refuses(self, written: str) -> None:
        config = a_config(publisher_credential_file="/x/credential")
        config.conductor.launch_settings = [written]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_file=written)
        }
        verdict = run_the_activation_check(config, IT_PASSED)
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert "conductor.launch_settings" in verdict.sentence
        assert "planning.sandboxes.the-sandbox.some_file" in verdict.sentence

    @pytest.mark.parametrize(
        "elsewhere", ["/x/credential2", "/x/credential.bak", "KEY=/x/credential.bak"]
    )
    def test_a_different_file_does_not_match(self, elsewhere: str) -> None:
        config = a_config(publisher_credential_file="/x/credential")
        config.conductor.launch_settings = [elsewhere]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_file=elsewhere)
        }
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is True, verdict.sentence


class TestAProtectedPathCannotHideASeparator:
    """Codex, implementation round 2 (R2). Paths are found inside settings by
    splitting on whitespace, '=', ':', ',', ';' and quotes, so a protected
    path containing one could never be found. The settings model refuses
    such a path at load, naming the field; and the whole value is also
    compared as one path, so an identical standalone path is always found."""

    @pytest.mark.parametrize("field", ["publisher_route_keys_dir", "publisher_credential_file"])
    @pytest.mark.parametrize(
        "value", ["/srv/publisher/route keys", "/srv/a=b/keys", "/srv/a:b/keys", "relative/keys"]
    )
    def test_the_settings_refuse_a_path_with_a_separator(self, field: str, value: str) -> None:
        from pydantic import ValidationError

        from forge.config.models import ForgeConfig

        with pytest.raises(ValidationError) as refused:
            ForgeConfig.model_validate(
                {
                    "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                    "publication": {field: value},
                }
            )
        assert f"publication.{field}" in str(refused.value)

    @pytest.mark.parametrize(
        "value",
        [None, "", "/etc/forge-publisher/credential", "/srv/factory-live/publisher/credential.deploy-key"],
    )
    def test_the_settings_take_a_plain_absolute_path(self, value) -> None:
        from forge.config.models import ForgeConfig

        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "publication": {
                    "publisher_credential_file": value,
                    "publisher_route_keys_dir": value,
                },
            }
        )
        assert config.publication.publisher_credential_file == value

    def test_a_standalone_route_keys_path_with_a_space_is_still_found(self) -> None:
        """The model refuses it; this is the belt and braces behind that."""
        config = a_config(publisher_route_keys_dir="/srv/publisher/route keys")
        config.conductor.launch_settings = ["/srv/publisher/route keys/a"]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_folder="//srv/publisher/route keys")
        }
        verdict = run_the_activation_check(config, IT_PASSED)
        assert "conductor.launch_settings" in verdict.sentence
        assert "planning.sandboxes.the-sandbox.some_folder" in verdict.sentence

    def test_a_standalone_credential_path_with_a_space_is_still_found(self) -> None:
        config = a_config(publisher_credential_file="/srv/the credential")
        config.conductor.launch_settings = ["/srv/./the credential"]
        verdict = run_the_activation_check(config, IT_PASSED)
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert "conductor.launch_settings" in verdict.sentence

    @pytest.mark.parametrize(
        "elsewhere", ["/srv/publisher/route keys2/a", "/srv/the credential.bak"]
    )
    def test_siblings_of_a_spaced_path_still_do_not_match(self, elsewhere: str) -> None:
        config = a_config(
            publisher_route_keys_dir="/srv/publisher/route keys",
            publisher_credential_file="/srv/the credential",
        )
        config.conductor.launch_settings = [elsewhere]
        assert run_the_activation_check(config, IT_PASSED).all_hold is True


class TestTheRuleIsANameAsAWholeComponent:
    """Codex, implementation round 3. Normalising path text kept losing to
    crafted spellings, so the rule is now conservative: a setting is refused
    when the protected path's own NAME appears in it as a whole component.
    Every spelling that reaches the protected place has to write that name;
    an unrelated path that shares the name is refused too, and said so."""

    @pytest.mark.parametrize(
        "written",
        [
            "KEY_FILE='/srv/publisher/staging dir/../route-keys/a'",
            "/srv/publisher/a=b/../route-keys/a",
            "KEY_FILE=/srv/publisher/./route-keys/a",
            "//srv/publisher/route-keys/a",
            "/srv/publisher/route-keys/../route-keys/a",
            "/srv//publisher/route-keys/a",
            "/opt/a:/srv/publisher/route-keys:/opt/b",
            "route-keys",
        ],
    )
    def test_every_spelling_of_the_folder_refuses(self, written: str) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS + "/")
        config.conductor.launch_settings = [written]
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is False, written
        assert (
            "names a path component equal to the protected folder's name "
            "'route-keys'" in verdict.sentence
        )

    @pytest.mark.parametrize(
        "written",
        [
            "KEY_FILE='/x/staging dir/../credential'",
            "/x/a=b/../credential",
            "/x/./credential",
            "//x/credential",
            "KEY=/x/credential",
        ],
    )
    def test_every_spelling_of_the_credential_file_refuses(self, written: str) -> None:
        config = a_config(publisher_credential_file="/x/credential")
        config.conductor.launch_settings = [written]
        verdict = run_the_activation_check(config, IT_PASSED)
        assert [answer.name for answer in verdict.refusals] == [
            "the-credential-is-named-in-no-other-settings"
        ]
        assert (
            "names a path component equal to the protected file's name "
            "'credential'" in verdict.sentence
        )

    @pytest.mark.parametrize(
        "same_name_elsewhere",
        ["/home" + ROUTE_KEYS, "/srv/publisher/route-keys/../other/a", "srv/route-keys/a"],
    )
    def test_a_different_folder_of_the_same_name_fails_closed(
        self, same_name_elsewhere: str
    ) -> None:
        config = a_config(publisher_route_keys_dir=ROUTE_KEYS)
        config.conductor.launch_settings = [same_name_elsewhere]
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is False
        assert "rename one of the two folders" in verdict.sentence

    @pytest.mark.parametrize(
        "elsewhere",
        [
            ROUTE_KEYS + "2/a",
            ROUTE_KEYS + ".bak/a",
            "/srv/publisher/credential.deploy-key.pub",
            "KEY=/srv/publisher/credential.deploy-key.bak",
        ],
    )
    def test_names_that_only_begin_the_same_do_not_match(self, elsewhere: str) -> None:
        config = a_config(
            publisher_route_keys_dir=ROUTE_KEYS,
            publisher_credential_file="/srv/publisher/credential.deploy-key",
        )
        config.conductor.launch_settings = [elsewhere]
        config.planning.sandboxes = {
            "the-sandbox": SimpleNamespace(name="the-sandbox", some_file=elsewhere)
        }
        verdict = run_the_activation_check(config, IT_PASSED)
        assert verdict.all_hold is True, verdict.sentence
