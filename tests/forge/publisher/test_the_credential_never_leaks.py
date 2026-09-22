"""The credential is read once, from one file, and appears nowhere else.

One-true-copy design pass, item 1, first revision item 3 and second revision
section D: *nothing that builds or checks can see its credential*.

HOW THIS IS PROVEN. A recognisable made-up string is planted in the one
named file, a whole publish is driven end to end against a bare repository on
disk, and then everything the run produced is read and searched for that
string: the answer, every log line, every file under the publisher's own
folder, and the ENVIRONMENT of every child process the publisher started —
captured by standing a real child in git's place and writing down what it was
given.

The credential file itself is the one place the string is allowed to be. It
is the file a person put it in, and the whole design is that it exists there
and nowhere else.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from forge.publisher.credential import (
    CREDENTIAL_IS_NOT_SHOWN,
    Credential,
    read_the_credential,
    the_askpass_program,
    the_environment_git_is_given,
)
from forge.publisher.git_work import (
    THE_ADDRESS_IS_NOT_WRITTEN_DOWN,
    without_the_addresses,
)
from forge.publisher.service import Publisher
from tests.forge.publisher.a_project_and_a_ledger import (
    THE_MADE_UP_CREDENTIAL,
    a_request,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)


@pytest.fixture()
def project(tmp_path: Path) -> dict:
    return make_the_project(tmp_path / "world")


class TestItWillNotPrintItself:
    def test_every_way_python_prints_a_thing(self) -> None:
        held = Credential(THE_MADE_UP_CREDENTIAL, path="/somewhere/the-file")
        assert repr(held) == CREDENTIAL_IS_NOT_SHOWN
        assert str(held) == CREDENTIAL_IS_NOT_SHOWN
        assert f"{held}" == CREDENTIAL_IS_NOT_SHOWN
        assert f"{held!r}" == CREDENTIAL_IS_NOT_SHOWN
        assert "%s" % (held,) == CREDENTIAL_IS_NOT_SHOWN  # noqa: UP031
        assert THE_MADE_UP_CREDENTIAL not in json.dumps(str(held))
        # Its FILE is a path, and a path is not a secret.
        assert held.file == "/somewhere/the-file"
        assert held.held is True

    def test_it_is_read_once_from_the_one_named_file(self, tmp_path: Path) -> None:
        where = tmp_path / "the-credential"
        where.write_text(THE_MADE_UP_CREDENTIAL + "\n", encoding="utf-8")
        held, refusal = read_the_credential(where)
        assert refusal is None
        assert held is not None and held.held
        # The file is then changed. The publisher does not re-read it; a
        # Credential is a thing that was read at start, once.
        where.write_text("something else entirely\n", encoding="utf-8")
        assert held.file == str(where)

    @pytest.mark.parametrize(
        "what, says",
        [
            (None, "was given no credential file"),
            ("/nowhere/at/all", "is not there"),
        ],
    )
    def test_the_refusals_name_the_file_and_never_its_contents(
        self, what: str | None, says: str
    ) -> None:
        held, refusal = read_the_credential(what)
        assert held is None
        assert refusal is not None
        assert says in refusal.sentence
        assert THE_MADE_UP_CREDENTIAL not in refusal.sentence


class TestTheProgramGitAsksThrough:
    def test_it_holds_the_path_and_not_the_credential(self, tmp_path: Path) -> None:
        where = tmp_path / "the-credential"
        where.write_text(THE_MADE_UP_CREDENTIAL + "\n", encoding="utf-8")
        held, _ = read_the_credential(where)
        assert held is not None

        program = the_askpass_program(held, state_dir=tmp_path / "state")

        text = program.read_text(encoding="utf-8")
        assert THE_MADE_UP_CREDENTIAL not in text
        assert str(where) in text
        # Owner only: readable and runnable by the publisher's own user.
        assert oct(program.stat().st_mode)[-3:] == "700"

    def test_the_environment_git_is_given_carries_a_path_not_a_secret(
        self, tmp_path: Path
    ) -> None:
        where = tmp_path / "the-credential"
        where.write_text(THE_MADE_UP_CREDENTIAL + "\n", encoding="utf-8")
        held, _ = read_the_credential(where)
        assert held is not None

        given = the_environment_git_is_given(
            held, state_dir=tmp_path / "state", home=tmp_path / "state"
        )

        assert all(THE_MADE_UP_CREDENTIAL not in value for value in given.values())
        assert given["GIT_ASKPASS"].endswith("ask-for-the-credential")
        assert given["GIT_TERMINAL_PROMPT"] == "0"
        # HOME is the publisher's own folder, so git reads no person's
        # configuration and finds no person's stored credentials.
        assert given["HOME"] == str(tmp_path / "state")

    def test_it_is_built_from_nothing_not_copied_from_this_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child handed this process's environment is handed whatever is in it."""
        monkeypatch.setenv("SOMETHING_ELSE_ENTIRELY", "should not travel")
        given = the_environment_git_is_given(
            None, state_dir=tmp_path / "state", home=tmp_path / "state"
        )
        assert "SOMETHING_ELSE_ENTIRELY" not in given
        assert set(given) <= {
            "PATH",
            "HOME",
            "GIT_TERMINAL_PROMPT",
            "GIT_CONFIG_NOSYSTEM",
            "LC_ALL",
            "GIT_ASKPASS",
        }


class TestItIsWrittenOnceAndNotOncePerGitCommand:
    """The reviewer's seventh finding, 22 September 2026.

    It used to be written on the way into EVERY git command. The publisher
    works on different projects at the same time — its one-at-a-time lock is
    per project, exactly so that it can — so two requests wrote the same file
    at the same moment, and a file being written is momentarily a file with
    nothing in it. Git, running for the other request, could read a truncated
    program, get no credential, and be refused by the remote for a reason
    that had nothing to do with the remote.
    """

    def _a_credential(self, tmp_path: Path) -> object:
        where = tmp_path / "the-credential"
        where.write_text(THE_MADE_UP_CREDENTIAL + "\n", encoding="utf-8")
        held, _ = read_the_credential(where)
        assert held is not None
        return held

    def test_the_file_is_written_once_and_then_left_alone(
        self, tmp_path: Path
    ) -> None:
        held = self._a_credential(tmp_path)
        state = tmp_path / "state"

        first = the_askpass_program(held, state_dir=state)  # type: ignore[arg-type]
        was = (first.stat().st_ino, first.stat().st_mtime_ns)
        for _ in range(50):
            again = the_askpass_program(held, state_dir=state)  # type: ignore[arg-type]
            assert again == first
        assert (first.stat().st_ino, first.stat().st_mtime_ns) == was

    def test_every_git_command_finds_a_whole_program(self, tmp_path: Path) -> None:
        """Many at once, and not one of them ever reads a piece of one."""
        import threading

        held = self._a_credential(tmp_path)
        state = tmp_path / "state"
        ends_with = "sys.stdout.write(handle.read().strip() + chr(10))\n"
        what_they_saw: list[str] = []
        trouble: list[BaseException] = []

        def one_request() -> None:
            try:
                for _ in range(25):
                    given = the_environment_git_is_given(
                        held, state_dir=state, home=state  # type: ignore[arg-type]
                    )
                    what_they_saw.append(
                        Path(given["GIT_ASKPASS"]).read_text(encoding="utf-8")
                    )
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                trouble.append(exc)

        threads = [threading.Thread(target=one_request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert trouble == []
        assert len(what_they_saw) == 8 * 25
        # EVERY read was of a whole program. A truncated one is the failure
        # this is about, and an empty one is the shape it took.
        assert all(text.endswith(ends_with) for text in what_they_saw)
        assert all(THE_MADE_UP_CREDENTIAL not in text for text in what_they_saw)

    def test_a_publisher_writes_it_at_start(self, tmp_path: Path) -> None:
        """Before any request arrives, so no request is the one that writes it."""
        root = tmp_path / "world"
        project = make_the_project(root)
        make_the_ledger(root / "forge.db", project=project)
        settings = settings_for(root, project, ledger=root / "forge.db")

        Publisher(settings)

        program = Path(settings.state_dir) / "ask-for-the-credential"
        assert program.is_file()
        assert THE_MADE_UP_CREDENTIAL not in program.read_text(encoding="utf-8")
        # And nothing half-written is left lying about beside it.
        assert list(Path(settings.state_dir).glob("*being-written*")) == []


class TestAWholePublishLeaksNothing:
    def test_grep_everything_the_run_produced(
        self, tmp_path: Path, project: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        settings = settings_for(root, project, ledger=root / "forge.db")
        assert Path(settings.credential_file).read_text(encoding="utf-8").strip() == (
            THE_MADE_UP_CREDENTIAL
        )

        caplog.set_level(logging.DEBUG)
        what_every_child_was_given: list[dict[str, str]] = []
        real_run = __import__("subprocess").run

        def watch(argv, **kwargs):  # noqa: ANN001, ANN003
            what_every_child_was_given.append(dict(kwargs.get("env") or {}))
            return real_run(argv, **kwargs)

        import forge.publisher.git_work as git_work

        original = git_work.subprocess.run
        git_work.subprocess.run = watch  # type: ignore[assignment]
        try:
            publisher = Publisher(settings)
            answer = publisher.publish(a_request(project))
        finally:
            git_work.subprocess.run = original  # type: ignore[assignment]

        assert answer.published is True
        assert what_the_remote_has(project["bare"]) == project["j"]

        # 1. THE ANSWER.
        assert THE_MADE_UP_CREDENTIAL not in json.dumps(answer.to_wire())

        # 2. EVERY LOG LINE.
        for record in caplog.records:
            assert THE_MADE_UP_CREDENTIAL not in record.getMessage()
            assert THE_MADE_UP_CREDENTIAL not in str(record.args)

        # 3. EVERY CHILD'S ENVIRONMENT — and there WERE children.
        assert what_every_child_was_given
        for environment in what_every_child_was_given:
            for name, value in environment.items():
                assert THE_MADE_UP_CREDENTIAL not in str(value), name

        # 4. EVERY FILE THE PUBLISHER WROTE, under its own folder.
        state = Path(settings.state_dir)
        looked_at = 0
        for path in state.rglob("*"):
            if not path.is_file():
                continue
            looked_at += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - a file that will not open
                continue
            assert THE_MADE_UP_CREDENTIAL not in text, str(path)
        assert looked_at > 0

        # 5. AND THE ONE PLACE IT IS ALLOWED TO BE is still the only one: the
        # file a person put it in.
        assert Path(settings.credential_file).read_text(encoding="utf-8").strip() == (
            THE_MADE_UP_CREDENTIAL
        )

    def test_the_settings_a_person_may_read_carry_the_path_only(
        self, tmp_path: Path, project: dict
    ) -> None:
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        settings = settings_for(root, project, ledger=root / "forge.db")

        said = json.dumps(settings.without_secrets())

        assert THE_MADE_UP_CREDENTIAL not in said
        assert settings.credential_file in said

    def test_nothing_the_coordinator_sends_or_is_sent_carries_one(
        self, tmp_path: Path, project: dict
    ) -> None:
        """The wire between the coordinator and the publisher, both ways."""
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(settings_for(root, project, ledger=root / "forge.db"))
        asked = a_request(project)

        answer = publisher.publish(asked)

        assert THE_MADE_UP_CREDENTIAL not in json.dumps(asked)
        assert THE_MADE_UP_CREDENTIAL not in json.dumps(answer.to_wire())
        assert set(asked) == {
            "project",
            "build_id",
            "turn",
            "j_commit",
            "target_branch",
        }

    def test_this_process_s_own_environment_was_never_read(self) -> None:
        """No credential comes from the environment, so none is looked for.

        Named here because it is a rule as well as a fact: the credential has
        ONE source, the named file. A second source is a second place it can
        be read from, and the design says there is one.
        """
        from forge.publisher import credential as the_module

        source = Path(the_module.__file__).read_text(encoding="utf-8")
        reads = [
            line.strip()
            for line in source.splitlines()
            if "os.environ" in line and not line.strip().startswith("#")
        ]
        # The only environment read anywhere in the module is PATH, for the
        # child's own environment — never a credential.
        assert reads == ['"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),']
        assert os.environ is not None  # the module under test, not this one


#: A recognisable string planted in an address. Nothing anywhere accepts it;
#: it stands for whatever somebody setting the publisher up at rollout might
#: put in an address, and the test greps for it.
THE_PLANTED_STRING = "notarealtoken-TESTONLY-b41f"


class TestNoAddressIsEverWrittenDown:
    """The reviewer's sixth finding, 22 September 2026.

    Git names the address it was working with in its own messages, and those
    messages go into a refusal a person reads, a receipt and a row of the
    ledger. An address is also the easiest place for a credential to end up:
    the ordinary way to give git one without a helper is to put it in the
    address. Nothing in this estate does that today, and the addresses come
    out of a settings file somebody fills in at rollout — so the rule cannot
    be "nobody will".
    """

    def test_an_address_this_publisher_knows_is_taken_out(self) -> None:
        said = (
            f"fatal: \'/somewhere/{THE_PLANTED_STRING}/remote.git\' does not "
            "appear to be a git repository"
        )
        cleaned = without_the_addresses(
            said, (f"/somewhere/{THE_PLANTED_STRING}/remote.git",)
        )
        assert THE_PLANTED_STRING not in cleaned
        assert THE_ADDRESS_IS_NOT_WRITTEN_DOWN in cleaned
        # What went wrong still reads.
        assert "does not appear to be a git repository" in cleaned

    def test_who_is_asking_is_taken_out_of_an_address_nobody_declared(
        self,
    ) -> None:
        """Even an address this publisher was never told about."""
        said = (
            f"fatal: could not read from \'x://somebody:{THE_PLANTED_STRING}"
            "@elsewhere.invalid/a/b\'"
        )
        cleaned = without_the_addresses(said, ())
        assert THE_PLANTED_STRING not in cleaned
        assert "could not read from" in cleaned

    def _a_world_with_the_string_in_its_paths(self, tmp_path: Path) -> tuple:
        root = tmp_path / f"world-{THE_PLANTED_STRING}"
        project = make_the_project(root)
        make_the_ledger(root / "forge.db", project=project)
        settings = settings_for(root, project, ledger=root / "forge.db")
        assert THE_PLANTED_STRING in settings.projects[
            list(settings.projects)[0]
        ].remote
        return root, project, Publisher(settings)

    def test_the_remotes_address_is_not_in_a_refusal(self, tmp_path: Path) -> None:
        """A real git failure, with a real address, said out loud by git."""
        import shutil

        _root, project, publisher = self._a_world_with_the_string_in_its_paths(
            tmp_path
        )
        # The remote is taken away, so git has to say something about it.
        shutil.rmtree(project["bare"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-remote-could-not-be-read"
        assert THE_PLANTED_STRING not in str(answer.refusal)
        assert THE_ADDRESS_IS_NOT_WRITTEN_DOWN in str(answer.refusal)

    def test_the_source_address_is_not_in_a_refusal_either(
        self, tmp_path: Path
    ) -> None:
        """The read-only address a project's copy is reached at, the same way."""
        import shutil

        _root, project, publisher = self._a_world_with_the_string_in_its_paths(
            tmp_path
        )
        shutil.rmtree(project["copy"])

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-joined-commit-is-not-there"
        assert THE_PLANTED_STRING not in str(answer.refusal)
        assert THE_ADDRESS_IS_NOT_WRITTEN_DOWN in str(answer.refusal)
