"""The identity of what was checked — one that cannot be reused (section C).

What is pinned: the identity is made from the joined commit, so two joined
commits never share one; the fingerprint is of the content that was checked,
so a name that was somehow reused still does not match; the two names the
identity travels under belong to the PROJECT; and reading the step's answer
back is a matter of reading one line, with "nothing was said" treated as a
mismatch rather than a pass.
"""

from __future__ import annotations

from forge.pipeline.deployment_identity import (
    DEFAULT_REPORT_MARKER,
    DEFAULT_SETTING_NAME,
    declared_identity,
    fixed_identity,
    identity_reported_by,
    the_identities_differ,
)

J = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
TREE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class TestTheIdentityCannotBeReused:
    def test_the_name_is_made_from_the_joined_commit(self) -> None:
        made = fixed_identity(j_commit=J, content=TREE)
        assert made.name == "j-0123456789ab"
        assert made.text == f"{made.name}@{made.fingerprint}"

    def test_two_joined_commits_never_share_one(self) -> None:
        assert (
            fixed_identity(j_commit=J, content=TREE).text
            != fixed_identity(j_commit=OTHER, content=TREE).text
        )

    def test_the_same_commit_with_different_content_is_a_different_identity(
        self,
    ) -> None:
        """The fingerprint is the second half, and it is of the CONTENT."""
        one = fixed_identity(j_commit=J, content=TREE)
        two = fixed_identity(j_commit=J, content="b" * 40)
        assert one.name == two.name
        assert one.fingerprint != two.fingerprint
        assert one.text != two.text

    def test_it_is_the_same_every_time_for_the_same_inputs(self) -> None:
        assert (
            fixed_identity(j_commit=J, content=TREE).text
            == fixed_identity(j_commit=J, content=TREE).text
        )

    def test_a_name_carries_only_what_a_name_can_carry(self) -> None:
        made = fixed_identity(j_commit=J, content=TREE, prefix="release/1")
        assert "/" not in made.name

    def test_no_joined_commit_is_a_programming_mistake_and_says_so(self) -> None:
        try:
            fixed_identity(j_commit="", content=TREE)
        except ValueError as exc:
            assert "has to be made from a joined commit" in str(exc)
        else:  # pragma: no cover - the raise is the point
            raise AssertionError("it made an identity out of nothing")

    def test_with_no_content_it_still_makes_one_and_warns(self, caplog) -> None:
        made = fixed_identity(j_commit=J, content=None)
        assert made.text
        assert "fingerprint is of the commit alone" in caplog.text


class _Profile:
    def __init__(self, extra: dict) -> None:
        self.extra = extra


class TestWhatTheProjectDeclares:
    def test_a_project_that_says_nothing_is_read_as_saying_nothing(self) -> None:
        declared = declared_identity(_Profile({}))
        assert declared.declared is False
        assert declared.setting == DEFAULT_SETTING_NAME
        assert declared.marker == DEFAULT_REPORT_MARKER

    def test_a_project_names_both(self) -> None:
        declared = declared_identity(
            _Profile(
                {
                    "identity": {
                        "setting": "WIDGET_SHOP_RELEASE",
                        "reported_as": "WIDGET_SHOP_RUNNING",
                    }
                }
            )
        )
        assert declared.declared is True
        assert declared.setting == "WIDGET_SHOP_RELEASE"
        assert declared.marker == "WIDGET_SHOP_RUNNING"

    def test_a_project_that_names_half_still_gets_a_working_arrangement(
        self,
    ) -> None:
        declared = declared_identity(_Profile({"identity": {"setting": "OURS"}}))
        assert declared.declared is True
        assert declared.setting == "OURS"
        assert declared.marker == DEFAULT_REPORT_MARKER

    def test_nothing_in_this_module_knows_what_the_project_deploys(self) -> None:
        """The declaration is names, and they are never inspected further."""
        declared = declared_identity(
            _Profile({"identity": {"setting": "A", "reported_as": "B"}})
        )
        assert declared.to_wire() == {
            "setting": "A",
            "marker": "B",
            "declared": True,
            # Added 24 September 2026. The first three are given defaults so a
            # project that declares the block half-way still works; the fourth
            # is NOT, because inventing a name to ask a project a question with
            # would mean running its deploy step in a mode nobody declared.
            "checked_as": "CHECKED_ARTIFACT",
            "artifact_setting": "DEPLOY_ARTIFACT",
            "asked_with": "",
            "running_as": "RUNNING_IDENTITY",
        }

    def test_the_four_names_the_second_review_added(self) -> None:
        declared = declared_identity(
            _Profile(
                {
                    "identity": {
                        "setting": "A",
                        "reported_as": "B",
                        "checked_as": "C",
                        "artifact_setting": "D",
                        "asked_with": "E",
                        "running_as": "F",
                    }
                }
            )
        )
        assert (declared.checked_as, declared.artifact_setting) == ("C", "D")
        assert (declared.asked_with, declared.running_as) == ("E", "F")
        assert declared.can_be_asked is True

    def test_a_project_that_says_nothing_about_being_asked_cannot_be(self) -> None:
        """And the press does not deploy over a target it cannot establish."""
        declared = declared_identity(_Profile({"identity": {"setting": "A"}}))
        assert declared.declared is True
        assert declared.asked_with == ""
        assert declared.can_be_asked is False


class TestWhatTheTargetSays:
    """The read-only answer has exactly three readings, and empty is one."""

    def test_a_token_is_what_is_running(self) -> None:
        from forge.pipeline.deployment_identity import what_the_target_says

        assert what_the_target_says(
            "log line\nRUNNING_IDENTITY=j-abc@1111\n", marker="RUNNING_IDENTITY"
        ) == ("identity", "j-abc@1111")

    def test_an_empty_value_means_nothing_is_running(self) -> None:
        from forge.pipeline.deployment_identity import what_the_target_says

        assert what_the_target_says(
            "log line\nRUNNING_IDENTITY=\n", marker="RUNNING_IDENTITY"
        ) == ("nothing", None)

    def test_no_line_at_all_is_never_read_as_nothing_running(self) -> None:
        from forge.pipeline.deployment_identity import what_the_target_says

        assert what_the_target_says(
            "the step said something else entirely\n", marker="RUNNING_IDENTITY"
        ) == ("no-answer", None)
        assert what_the_target_says("", marker="RUNNING_IDENTITY") == (
            "no-answer",
            None,
        )

    def test_the_last_line_wins_here_too(self) -> None:
        from forge.pipeline.deployment_identity import what_the_target_says

        assert what_the_target_says(
            "RUNNING_IDENTITY=about-to\nRUNNING_IDENTITY=what-really-is\n",
            marker="RUNNING_IDENTITY",
        ) == ("identity", "what-really-is")


class TestReadingTheAnswerBack:
    def test_the_line_the_step_printed(self) -> None:
        said = "some progress\nDEPLOYED_IDENTITY=j-0123456789ab@ffff\ndone\n"
        assert (
            identity_reported_by(said, marker="DEPLOYED_IDENTITY")
            == "j-0123456789ab@ffff"
        )

    def test_the_last_such_line_wins(self) -> None:
        said = "X=about-to\nX=what-really-ran\n"
        assert identity_reported_by(said, marker="X") == "what-really-ran"

    def test_quotes_a_shell_added_are_not_part_of_the_identity(self) -> None:
        assert identity_reported_by('X="j-a@b"\n', marker="X") == "j-a@b"
        assert identity_reported_by("X='j-a@b'\n", marker="X") == "j-a@b"

    def test_a_line_with_the_marker_inside_it_still_reads(self) -> None:
        said = "[deploy.sh] DEPLOYED_IDENTITY=j-a@b\n"
        assert identity_reported_by(said, marker="DEPLOYED_IDENTITY") == "j-a@b"

    def test_nothing_said_reads_as_nothing(self) -> None:
        assert identity_reported_by("nothing at all\n", marker="X") is None
        assert identity_reported_by("", marker="X") is None
        assert identity_reported_by(None, marker="X") is None
        assert identity_reported_by("X=\n", marker="X") is None


class TestTheComparison:
    def test_the_same_text_is_a_match(self) -> None:
        assert the_identities_differ("j-a@b", "j-a@b") is False
        assert the_identities_differ("j-a@b", " j-a@b ") is False

    def test_a_different_identity_is_a_mismatch(self) -> None:
        assert the_identities_differ("j-a@b", "j-c@d") is True

    def test_saying_nothing_is_a_mismatch_and_not_a_pass(self) -> None:
        """A step that does not say what is running has shown nothing."""
        assert the_identities_differ("j-a@b", None) is True
        assert the_identities_differ("j-a@b", "") is True

    def test_handing_nothing_over_is_a_mismatch_too(self) -> None:
        assert the_identities_differ(None, "j-a@b") is True
