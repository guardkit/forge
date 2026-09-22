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
