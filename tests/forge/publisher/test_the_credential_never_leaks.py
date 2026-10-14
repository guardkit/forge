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
        # HOME is the folder handed in, which the publisher makes FRESH AND
        # EMPTY for each command (14 October 2026), so git reads no person's
        # configuration and finds no stored credentials; no system or
        # personal settings file is read at all.
        assert given["HOME"] == str(tmp_path / "state")
        assert given["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert given["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_it_is_built_from_nothing_not_copied_from_this_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child handed this process's environment is handed whatever is in it."""
        monkeypatch.setenv("SOMETHING_ELSE_ENTIRELY", "should not travel")
        given = the_environment_git_is_given(
            None, state_dir=tmp_path / "state", home=tmp_path / "state"
        )
        assert "SOMETHING_ELSE_ENTIRELY" not in given
        # The isolated network environment (14 October 2026): a fixed PATH,
        # a fresh home, no settings files, one transport, no askpass but the
        # factory's own.
        assert set(given) <= {
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "GIT_TERMINAL_PROMPT",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "LC_ALL",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_NO_LAZY_FETCH",
            "GIT_ALLOW_PROTOCOL",
        }
        assert given["PATH"].endswith(":/usr/bin:/bin")


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

        # Every publisher git command starts at the network boundary
        # (14 October 2026).
        import forge.network_git as network_git

        original = network_git.subprocess.run
        network_git.subprocess.run = watch  # type: ignore[assignment]
        try:
            publisher = Publisher(settings)
            answer = publisher.publish(a_request(project))
        finally:
            network_git.subprocess.run = original  # type: ignore[assignment]

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
        # No environment is read anywhere in the module (14 October 2026: the
        # child's PATH is now the network boundary's fixed one) — and never a
        # credential.
        assert reads == []
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
        # Since 14 October 2026 git's own words are never kept at all: the
        # refusal is a fixed sentence (which step, git's exit code).
        assert "git fetch stopped with exit code" in str(answer.refusal)

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
        # Since 14 October 2026 git's own words are never kept at all: the
        # refusal is a fixed sentence (which step, git's exit code).
        assert "git fetch stopped with exit code" in str(answer.refusal)


# ---------------------------------------------------------------------------
# One credential per route (13 October 2026)
# ---------------------------------------------------------------------------

#: The made-up texts of two route keys. Nothing anywhere accepts them.
ROUTE_SECRET_A = "not-a-real-route-key-A-TESTONLY-71c4"
ROUTE_SECRET_B = "not-a-real-route-key-B-TESTONLY-c09a"
SECOND = "bench/second-shop"
SECOND_BUILD = "build-FEAT-PUB2-1"

#: Every file opened in this process while ``_OPENS_ON`` holds a list. An
#: audit hook sees every open — ``open``, ``Path.read_text`` and ``os.open``
#: alike — and cannot be removed, so it records only while a test asks it to.
_OPENS_ON: list[list[str]] = []


def _record_opens(event: str, args: tuple) -> None:
    if event == "open" and _OPENS_ON and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENS_ON[-1].append(os.fsdecode(args[0]))


import sys as _sys  # noqa: E402 - beside the hook it serves

_sys.addaudithook(_record_opens)


def _two_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, str | None],
) -> dict:
    """Projects with real bare remotes on disk, named by route; each route
    names a key (or ``None`` to inherit). Key ``a`` holds secret A, any other
    key name secret B."""
    from forge.publisher.settings import ProjectRoute
    from tests.forge.publisher.a_project_and_a_ledger import (
        a_route_key,
        a_route_keys_folder,
    )

    root = tmp_path / "world"
    folder = a_route_keys_folder(tmp_path, monkeypatch)
    projects = {}
    worlds = {}
    for index, (project_name, key_name) in enumerate(sorted(routes.items())):
        world = make_the_project(root / f"p{index}")
        make_the_ledger(
            root / "forge.db",
            project=world,
            build_id=f"build-{index}",
            repo=project_name,
        )
        if key_name is not None and not (folder / key_name).exists():
            a_route_key(
                folder, key_name, ROUTE_SECRET_A if key_name == "a" else ROUTE_SECRET_B
            )
        projects[project_name] = ProjectRoute(
            name=project_name,
            source=str(world["copy"]),
            remote=str(world["bare"]),
            credential_name=key_name,
        )
        worlds[project_name] = (world, f"build-{index}")
    first = next(iter(worlds.values()))[0]
    settings = settings_for(root, first, ledger=root / "forge.db", projects=projects)
    return {"settings": settings, "worlds": worlds, "folder": folder}


def _request_for(setup: dict, project_name: str) -> dict:
    world, build = setup["worlds"][project_name]
    return {**a_request(world, build_id=build), "project": project_name}


def _health_once(publisher: Publisher, settings, tmp_path: Path) -> str:
    """One real health request, over the loopback, with the real self-check."""
    import urllib.request

    from forge.publisher import __main__ as entry
    from forge.publisher.service import serve

    interfaces = tmp_path / "net"
    for name in ("lo", "eth0"):
        (interfaces / name).mkdir(parents=True, exist_ok=True)
    publisher.self_check = entry.the_self_check(settings, publisher, interfaces=interfaces)
    server, _thread = serve(settings, publisher=publisher)
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=10) as answer:
            return json.loads(answer.read())["self_check"]
    finally:
        server.shutdown()
        server.server_close()


class TestEachCredentialHasItsOwnProgram:
    def test_two_https_credentials_get_two_programs(self, tmp_path: Path) -> None:
        a = tmp_path / "key-a"
        a.write_text(ROUTE_SECRET_A + "\n", encoding="utf-8")
        b = tmp_path / "key-b"
        b.write_text(ROUTE_SECRET_B + "\n", encoding="utf-8")
        held_a, _ = read_the_credential(a, no_follow=True, own_program=True)
        held_b, _ = read_the_credential(b, no_follow=True, own_program=True)
        assert held_a is not None and held_b is not None
        state = tmp_path / "state"

        program_a = the_askpass_program(held_a, state_dir=state)
        program_b = the_askpass_program(held_b, state_dir=state)

        assert program_a != program_b
        # Each program names its own file, after both are written: the second
        # did not overwrite the first.
        assert str(a) in program_a.read_text(encoding="utf-8")
        assert str(b) not in program_a.read_text(encoding="utf-8")
        assert str(b) in program_b.read_text(encoding="utf-8")
        # And each, RUN, prints its own credential and no other.
        import subprocess

        said_a = subprocess.run([str(program_a)], capture_output=True, text=True, check=True)
        said_b = subprocess.run([str(program_b)], capture_output=True, text=True, check=True)
        assert said_a.stdout.strip() == ROUTE_SECRET_A
        assert said_b.stdout.strip() == ROUTE_SECRET_B
        assert ROUTE_SECRET_A not in program_a.read_text(encoding="utf-8")


class TestEachFileIsReadOnce:
    def _count(self, setup: dict, tmp_path: Path) -> dict[str, int]:
        settings = setup["settings"]
        files = {"publisher-wide": str(Path(settings.credential_file))}
        for key in setup["folder"].iterdir():
            files[f"route:{key.name}"] = str(key)
        opened: list[str] = []
        _OPENS_ON.append(opened)
        try:
            publisher = Publisher(settings)
            for project_name in sorted(setup["worlds"]):
                answer = publisher.publish(_request_for(setup, project_name))
                assert answer.published is True, answer.refusal
            assert _health_once(publisher, settings, tmp_path) == "passed"
        finally:
            _OPENS_ON.remove(opened)
        return {label: opened.count(path) for label, path in files.items()}

    def test_each_named_file_is_read_once_at_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup = _two_routes(
            tmp_path, monkeypatch, {"bench/one": "a", "bench/two": "b"}
        )
        counts = self._count(setup, tmp_path)
        assert counts == {"publisher-wide": 1, "route:a": 1, "route:b": 1}

    def test_one_read_per_distinct_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two routes sharing one name, one inheriting, one with its own."""
        setup = _two_routes(
            tmp_path,
            monkeypatch,
            {
                "bench/one": "a",
                "bench/also-one": "a",
                "bench/inherits": None,
                "bench/own": "b",
            },
        )
        counts = self._count(setup, tmp_path)
        assert counts == {"publisher-wide": 1, "route:a": 1, "route:b": 1}


class TestTwoEntriesThatAreOneFileRefuse:
    """By file identity, not by path text, and before anything is read."""

    def _settings(self, tmp_path: Path, monkeypatch, routes, wide: Path | None = None):
        setup = _two_routes(tmp_path, monkeypatch, routes)
        settings = setup["settings"]
        if wide is not None:
            import dataclasses

            settings = dataclasses.replace(settings, credential_file=str(wide))
        return setup, settings

    def _refuses_before_reading(self, setup, settings, *said: str) -> None:
        from forge.publisher.service import PublisherWillNotStart

        opened: list[str] = []
        _OPENS_ON.append(opened)
        try:
            with pytest.raises(PublisherWillNotStart) as refused:
                Publisher(settings)
        finally:
            _OPENS_ON.remove(opened)
        sentence = str(refused.value)
        for part in said:
            assert part in sentence, sentence
        for secret in (ROUTE_SECRET_A, ROUTE_SECRET_B, THE_MADE_UP_CREDENTIAL):
            assert secret not in sentence
        # Nothing was read: no credential file, of any kind, was opened.
        credentials = {str(settings.credential_file)} | {
            str(key) for key in setup["folder"].iterdir()
        }
        assert credentials.isdisjoint(opened), opened

    def test_a_route_name_aliasing_the_publisher_wide_file_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = tmp_path / "route-keys"
        setup, settings = self._settings(
            tmp_path, monkeypatch, {"bench/one": "a"}, wide=folder / "a"
        )
        self._refuses_before_reading(
            setup,
            settings, "publisher-wide credential_file", "route key 'a'", "bench/one"
        )

    def test_two_hard_linked_names_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup, settings = self._settings(
            tmp_path, monkeypatch, {"bench/one": "a", "bench/two": "b"}
        )
        (setup["folder"] / "b").unlink()
        os.link(setup["folder"] / "a", setup["folder"] / "b")
        self._refuses_before_reading(setup, settings, "route key 'a'", "route key 'b'")

    def test_a_publisher_wide_symlink_to_a_route_file_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = tmp_path / "the-publisher-wide-link"
        link.symlink_to(tmp_path / "route-keys" / "a")
        setup, settings = self._settings(
            tmp_path, monkeypatch, {"bench/one": "a"}, wide=link
        )
        self._refuses_before_reading(
            setup,
            settings, "publisher-wide credential_file", "route key 'a'"
        )

    def test_main_refuses_to_start_for_an_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from forge.publisher import __main__ as entry

        folder = tmp_path / "route-keys"
        _setup, settings = self._settings(
            tmp_path, monkeypatch, {"bench/one": "a"}, wide=folder / "a"
        )
        monkeypatch.setattr(entry, "load_settings", lambda _path: settings)
        monkeypatch.setattr(entry, "why_it_will_not_start", lambda *_a, **_k: None)
        monkeypatch.setattr(
            entry, "serve", lambda *_a, **_k: pytest.fail("it started with an alias")
        )
        assert entry.main(["--settings", "unused"]) == 2
        assert "same file" in capsys.readouterr().err


class TestTwoRoutesLeakNothing:
    def test_grep_everything_a_two_route_run_produced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole-run grep above, for two routes with two keys of their own
        and the publisher-wide credential beside them: all three secrets."""
        setup = _two_routes(
            tmp_path, monkeypatch, {"bench/one": "a", "bench/two": "b"}
        )
        settings = setup["settings"]
        planted = (ROUTE_SECRET_A, ROUTE_SECRET_B, THE_MADE_UP_CREDENTIAL)
        caplog.set_level(logging.DEBUG)
        what_every_child_was_given: list[dict[str, str]] = []
        real_run = __import__("subprocess").run

        def watch(argv, **kwargs):  # noqa: ANN001, ANN003
            what_every_child_was_given.append(dict(kwargs.get("env") or {}))
            return real_run(argv, **kwargs)

        import forge.network_git as network_git

        monkeypatch.setattr(network_git.subprocess, "run", watch)
        publisher = Publisher(settings)
        answers = [
            publisher.publish(_request_for(setup, name)) for name in sorted(setup["worlds"])
        ]
        assert all(answer.published for answer in answers), [a.refusal for a in answers]
        assert _health_once(publisher, settings, tmp_path) == "passed"

        for secret in planted:
            for answer in answers:
                assert secret not in json.dumps(answer.to_wire())
            for record in caplog.records:
                assert secret not in record.getMessage()
                assert secret not in str(record.args)
            assert what_every_child_was_given
            for environment in what_every_child_was_given:
                for name, value in environment.items():
                    assert secret not in str(value), name
            assert secret not in json.dumps(settings.without_secrets())
            looked_at = 0
            for path in Path(settings.state_dir).rglob("*"):
                if not path.is_file():
                    continue
                looked_at += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                assert secret not in text, str(path)
            assert looked_at > 0
        # Each route has a program of its own, and the publisher-wide one is
        # still where it always was.
        programs = sorted(p.name for p in Path(settings.state_dir).glob("ask-for-the-credential*"))
        assert len(programs) == 3, programs
        assert "ask-for-the-credential" in programs
        # The one place each secret is allowed to be: the file it was put in.
        assert (setup["folder"] / "a").read_text(encoding="utf-8").strip() == ROUTE_SECRET_A
        assert (setup["folder"] / "b").read_text(encoding="utf-8").strip() == ROUTE_SECRET_B
