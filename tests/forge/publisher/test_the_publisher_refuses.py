"""What the publisher will not do, and the sentence it says when it will not.

One-true-copy design pass, item 1, second revision section D and third
revision section E. The publisher reads the publication record ITSELF, and
refuses unless that record says, of its own accord, that this exact joined
commit was joined and checked by the worker whose turn this is.

Every case here ends with NOTHING SENT: the bare repository's branch is where
it was, and its own reflog shows nobody moved it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.publisher.service import Publisher
from forge.publisher.settings import ProjectRoute
from tests.forge.publisher.a_project_and_a_ledger import (
    BUILD,
    PROJECT,
    a_request,
    every_push_the_remote_saw,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)


@pytest.fixture()
def project(tmp_path: Path) -> dict:
    return make_the_project(tmp_path / "world")


def _publisher(tmp_path: Path, project: dict, **ledger: object) -> Publisher:
    root = tmp_path / "world"
    make_the_ledger(root / "forge.db", project=project, **ledger)  # type: ignore[arg-type]
    return Publisher(settings_for(root, project, ledger=root / "forge.db"))


def _nothing_moved(project: dict, before: str) -> None:
    assert what_the_remote_has(project["bare"], project["branch"]) == before
    assert every_push_the_remote_saw(project["bare"], project["branch"]) == []


class TestTheRecordHasToSayItItself:
    def test_a_build_with_no_record_at_all(self, tmp_path: Path, project: dict) -> None:
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(settings_for(root, project, ledger=root / "forge.db"))
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project, build_id="build-nobody-pressed"))

        assert answer.published is False
        assert answer.refusal_kind == "there-is-no-record"
        assert "no publication record" in str(answer.refusal)
        _nothing_moved(project, before)

    def test_a_stale_turn(self, tmp_path: Path, project: dict) -> None:
        """The worker was replaced: the record is on a later turn than this."""
        publisher = _publisher(tmp_path, project, turn_takes=3)
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project, turn=1))

        assert answer.published is False
        assert answer.refusal_kind == "the-turn-has-moved-on"
        assert "turn 1" in str(answer.refusal)
        assert "turn 3" in str(answer.refusal)
        assert "has been replaced" in str(answer.refusal)
        _nothing_moved(project, before)

    def test_a_turn_from_the_future_is_refused_too(
        self, tmp_path: Path, project: dict
    ) -> None:
        """Equality, not "at least": a number nobody granted is not a turn."""
        publisher = _publisher(tmp_path, project)
        answer = publisher.publish(a_request(project, turn=99))
        assert answer.published is False
        assert answer.refusal_kind == "the-turn-has-moved-on"

    def test_a_joined_commit_the_record_does_not_name(
        self, tmp_path: Path, project: dict
    ) -> None:
        publisher = _publisher(tmp_path, project)
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project, j_commit=project["tip"]))

        assert answer.published is False
        assert answer.refusal_kind == "that-is-not-the-recorded-join"
        assert project["tip"][:10] in str(answer.refusal)
        _nothing_moved(project, before)

    def test_a_branch_the_record_does_not_name(
        self, tmp_path: Path, project: dict
    ) -> None:
        publisher = _publisher(tmp_path, project)
        answer = publisher.publish(a_request(project, target_branch="somewhere-else"))
        assert answer.published is False
        assert answer.refusal_kind == "that-is-not-the-recorded-branch"
        assert "somewhere-else" in str(answer.refusal)


class TestBothKindsOfCheckHaveToHavePassedOnThisCommit:
    def test_the_build_systems_checks_never_ran(
        self, tmp_path: Path, project: dict
    ) -> None:
        """The GATED case the merge word leaves when it picks a join up.

        A press that reuses a join does not run the merge command again, and
        the build system's own checks live inside it — so they have never run
        on that joined commit. The merge word says so and does not send; the
        publisher refuses it independently, which is the point of it checking
        for itself.
        """
        publisher = _publisher(tmp_path, project, merge_checks=None)
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-checks-are-not-recorded-as-passed"
        assert "the build system's own checks after the join" in str(answer.refusal)
        _nothing_moved(project, before)

    def test_the_build_systems_checks_ran_and_went_red(
        self, tmp_path: Path, project: dict
    ) -> None:
        """A ``done`` line is not enough. A red run writes one too."""
        publisher = _publisher(tmp_path, project, merge_checks=False)
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-checks-are-not-recorded-as-passed"
        assert "A step that was written down is not a step that passed" in str(
            answer.refusal
        )
        _nothing_moved(project, before)

    def test_the_live_check_never_ran(self, tmp_path: Path, project: dict) -> None:
        publisher = _publisher(tmp_path, project, candidate_check=None)
        answer = publisher.publish(a_request(project))
        assert answer.published is False
        assert answer.refusal_kind == "the-checks-are-not-recorded-as-passed"
        assert "the factory's own live check" in str(answer.refusal)

    def test_the_live_check_went_red(self, tmp_path: Path, project: dict) -> None:
        publisher = _publisher(tmp_path, project, candidate_check=False)
        answer = publisher.publish(a_request(project))
        assert answer.published is False
        assert answer.refusal_kind == "the-checks-are-not-recorded-as-passed"

    def test_the_checks_passed_on_a_DIFFERENT_commit(
        self, tmp_path: Path, project: dict
    ) -> None:
        """Somebody else's answer to somebody else's question.

        This is the forged-record shape as well: a ``done`` line written by
        hand, saying "passed", about a commit that is not this one.
        """
        publisher = _publisher(tmp_path, project, ran_on=project["g"])
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-checks-are-not-recorded-as-passed"
        assert "is not about this one" in str(answer.refusal)
        _nothing_moved(project, before)


class TestWhatItWasNotToldAbout:
    def test_a_project_it_has_no_addresses_for(
        self, tmp_path: Path, project: dict
    ) -> None:
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(
            settings_for(root, project, ledger=root / "forge.db", projects={})
        )
        before = what_the_remote_has(project["bare"], project["branch"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-project-is-not-one-of-mine"
        assert PROJECT in str(answer.refusal)
        _nothing_moved(project, before)

    def test_no_credential_at_all(self, tmp_path: Path, project: dict) -> None:
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(
            settings_for(
                root,
                project,
                ledger=root / "forge.db",
                credential_file=root / "there-is-no-such-file",
            )
        )
        before = what_the_remote_has(project["bare"], project["branch"])

        assert publisher.holds_a_credential is False
        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "there-is-no-credential"
        assert "is not there" in str(answer.refusal)
        _nothing_moved(project, before)

    def test_an_empty_credential_file_is_no_credential(
        self, tmp_path: Path, project: dict
    ) -> None:
        root = tmp_path / "world"
        root.mkdir(parents=True, exist_ok=True)
        empty = root / "empty"
        empty.write_text("   \n", encoding="utf-8")
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(
            settings_for(root, project, ledger=root / "forge.db", credential_file=empty)
        )
        assert publisher.holds_a_credential is False
        assert publisher.publish(a_request(project)).refusal_kind == (
            "there-is-no-credential"
        )

    def test_a_ledger_that_is_not_there(self, tmp_path: Path, project: dict) -> None:
        root = tmp_path / "world"
        publisher = Publisher(
            settings_for(root, project, ledger=root / "no-ledger-here.db")
        )
        answer = publisher.publish(a_request(project))
        assert answer.published is False
        assert answer.refusal_kind == "the-record-could-not-be-read"


class TestThingsItWillNotPutOnACommandLine:
    @pytest.mark.parametrize(
        "asked",
        [
            {"j_commit": "--upload-pack=touch /tmp/x"},
            {"j_commit": "HEAD"},
            {"j_commit": ""},
            {"target_branch": "--exec=whatever"},
            {"target_branch": "-not-a-branch"},
            {"target_branch": "a/../b"},
            {"turn": 0},
            {"turn": "one"},
            {"project": ""},
            {"build_id": ""},
        ],
    )
    def test_each_is_refused_before_anything_happens(
        self, tmp_path: Path, project: dict, asked: dict
    ) -> None:
        publisher = _publisher(tmp_path, project)
        before = what_the_remote_has(project["bare"], project["branch"])
        request = a_request(project)
        request.update(asked)

        answer = publisher.publish(request)

        assert answer.published is False
        assert answer.refusal_kind == "the-request-made-no-sense"
        _nothing_moved(project, before)

    def test_something_that_is_not_a_request_at_all(
        self, tmp_path: Path, project: dict
    ) -> None:
        publisher = _publisher(tmp_path, project)
        assert publisher.publish("send it").refusal_kind == "the-request-made-no-sense"
        assert publisher.publish(None).refusal_kind == "the-request-made-no-sense"


class TestEveryRefusalIsOneSentenceAPersonCanRead:
    def test_they_all_end_in_a_full_stop_and_say_nothing_was_sent(
        self, tmp_path: Path, project: dict
    ) -> None:
        publisher = _publisher(tmp_path, project, merge_checks=None)
        answer = publisher.publish(a_request(project, turn=1))
        said = str(answer.refusal)
        assert said.endswith(".")
        assert "Nothing was sent" in said or "nothing was sent" in said

    def test_the_route_for_a_project_is_read_only_by_contract(
        self, tmp_path: Path, project: dict
    ) -> None:
        """Nothing in the publisher writes to a project's copy.

        The source address is only ever fetched FROM. This reads the module's
        own source for the one thing that could break that: a push, or any
        argument list naming the source.
        """
        from forge.publisher import git_work

        source = Path(git_work.__file__).read_text(encoding="utf-8")
        pushes = [
            line.strip()
            for line in source.splitlines()
            if '"push"' in line and not line.strip().startswith("#")
        ]
        # EXACTLY ONE line in the whole module builds a push, and it is the
        # body of ``the_send_argv``.
        assert pushes == [
            'return ["push", str(remote), f"{commit}:refs/heads/{branch}"]'
        ], pushes
        # And the one place a push IS built names the REMOTE, never the source.
        argv = git_work.the_send_argv("the-remote", "a" * 40, "main")
        assert argv[1] == "the-remote"

    def test_a_route_is_two_addresses_and_a_name(self) -> None:
        route = ProjectRoute(name="p", source="s", remote="r")
        assert (route.name, route.source, route.remote) == ("p", "s", "r")
        assert BUILD  # the build the tests press, named once
