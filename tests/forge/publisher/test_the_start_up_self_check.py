"""The publisher's self-check, its health route, and the coordinator asking it.

What the check proves: the credential file is a regular file owned by the
publisher's UID with no group or other access, and there is exactly one
network interface besides loopback. It does not prove which network that is,
who else is on it, or that no other container running as the same UID can
read the file. It runs at start and again on every health request.

Nothing here starts a container: the interface count is pointed at a folder
standing in for ``/sys/class/net``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.pipeline import publisher_client
from forge.pipeline.publication_switch import publication_is_switched_on
from forge.publisher import __main__ as entry
from forge.publisher.service import Publisher, serve
from forge.publisher.settings import PublisherSettings
from tests.forge.pipeline.test_the_activation_check import a_config


def _settings(tmp_path: Path, *, mode: int = 0o600) -> PublisherSettings:
    credential = tmp_path / "credential"
    credential.write_text("not-a-real-credential-TESTONLY\n", encoding="utf-8")
    credential.chmod(mode)
    return PublisherSettings(
        credential_file=str(credential),
        ledger=str(tmp_path / "forge.db"),
        state_dir=str(tmp_path / "state"),
    )


def _interfaces(tmp_path: Path, *names: str) -> Path:
    folder = tmp_path / "net"
    folder.mkdir()
    for name in ("lo", *names):
        (folder / name).mkdir()
    return folder


class TestTheSelfCheck:
    def test_its_own_file_and_one_network_passes(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        assert entry.why_it_will_not_start(
            settings, interfaces=_interfaces(tmp_path, "eth0")
        ) is None

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
    def test_a_credential_others_can_read_refuses(
        self, tmp_path: Path, mode: int
    ) -> None:
        said = entry.why_it_will_not_start(
            _settings(tmp_path, mode=mode), interfaces=_interfaces(tmp_path, "eth0")
        )
        assert said and "readable by the publisher's own user alone" in said

    def test_a_credential_that_is_not_there_refuses(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        Path(settings.credential_file).unlink()
        said = entry.why_it_will_not_start(
            settings, interfaces=_interfaces(tmp_path, "eth0")
        )
        assert said and "could not be looked at" in said

    def test_a_credential_owned_by_someone_else_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        someone_else = os.geteuid() + 1
        monkeypatch.setattr(entry.os, "geteuid", lambda: someone_else)
        said = entry.why_it_will_not_start(
            settings, interfaces=_interfaces(tmp_path, "eth0")
        )
        assert said and "not to the publisher's own user" in said

    def test_several_interfaces_refuse(self, tmp_path: Path) -> None:
        """Like the host's own list. A host with ONE interface would pass."""
        said = entry.why_it_will_not_start(
            _settings(tmp_path),
            interfaces=_interfaces(tmp_path, "eth0", "docker0", "wlan0"),
        )
        assert said and "3 network interfaces besides loopback" in said

    def test_a_second_interface_refuses(self, tmp_path: Path) -> None:
        said = entry.why_it_will_not_start(
            _settings(tmp_path), interfaces=_interfaces(tmp_path, "eth0", "eth1")
        )
        assert said and "2 network interfaces besides loopback" in said

    def test_main_refuses_to_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        settings = _settings(tmp_path, mode=0o644)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            '{"credential_file": "%s", "ledger": "%s", "state_dir": "%s", '
            '"private_path_prefixes": ["/home/example"]}'
            % (settings.credential_file, settings.ledger, settings.state_dir),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            entry,
            "serve",
            lambda *_a, **_k: pytest.fail("it started without passing"),
        )
        assert entry.main(["--settings", str(settings_file)]) == 2
        assert "will not start" in capsys.readouterr().err


@pytest.fixture()
def a_running_publisher(tmp_path: Path):
    settings = _settings(tmp_path)
    publisher = Publisher(settings)
    server, _thread = serve(settings, publisher=publisher)
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield publisher, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


class TestTheCoordinatorAsksTheHealthRoute:
    def test_a_publisher_that_passed_switches_publication_on(
        self, a_running_publisher
    ) -> None:
        publisher, url = a_running_publisher
        publisher.self_check = lambda: None
        config = a_config(publisher_url=url)
        said = publisher_client.the_publishers_self_check(config)
        assert said.the_publisher_passed_its_self_check is True
        assert publication_is_switched_on(
            config, lambda: publisher_client.the_publishers_self_check(config)
        ) is True

    def test_the_check_is_run_again_on_every_health_request(
        self, a_running_publisher, tmp_path: Path
    ) -> None:
        """"passed" cannot go stale: a mode changed after start turns it off."""
        publisher, url = a_running_publisher
        settings = publisher.settings
        interfaces = _interfaces(tmp_path, "eth0")
        publisher.self_check = lambda: entry.why_it_will_not_start(
            settings, interfaces=interfaces
        )
        config = a_config(publisher_url=url)
        assert publisher_client.the_publishers_self_check(config).the_publisher_passed_its_self_check is True
        Path(settings.credential_file).chmod(0o640)
        assert publisher_client.the_publishers_self_check(config).the_publisher_passed_its_self_check is False
        Path(settings.credential_file).chmod(0o600)
        (interfaces / "eth1").mkdir()
        assert publisher_client.the_publishers_self_check(config).the_publisher_passed_its_self_check is False

    def test_a_publisher_that_never_ran_the_check_keeps_it_off(
        self, a_running_publisher
    ) -> None:
        _publisher, url = a_running_publisher
        config = a_config(publisher_url=url)
        said = publisher_client.the_publishers_self_check(config)
        assert said.the_publisher_passed_its_self_check is False
        assert publication_is_switched_on(config, said) is False

    def test_a_publisher_that_does_not_answer_keeps_it_off(self) -> None:
        config = a_config(publisher_url="http://127.0.0.1:9")
        said = publisher_client.the_publishers_self_check(config, timeout=2)
        assert said.the_publisher_passed_its_self_check is None
        assert "did not answer" in (said.why_nobody_has_looked or "")

    def test_no_publisher_configured_keeps_it_off(self) -> None:
        said = publisher_client.the_publishers_self_check(a_config(publisher_url=None))
        assert said.the_publisher_passed_its_self_check is None
        assert "publisher_url" in (said.why_nobody_has_looked or "")

    def test_the_two_sides_spell_passed_the_same(self) -> None:
        source = Path(entry.__file__).with_name("service.py").read_text()
        assert f'"{publisher_client.SELF_CHECK_PASSED}"' in source

    def test_the_coordinators_listener_is_given_this_reader(
        self, a_running_publisher
    ) -> None:
        from forge.cli.serve import compose_merge_executor_deps
        from forge.config.models import ForgeConfig

        publisher, url = a_running_publisher
        publisher.self_check = lambda: None
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "merge_executor": {"enabled": True},
                "publication": {"publisher_url": url},
            }
        )
        deps = compose_merge_executor_deps(
            forge_config=config,
            sqlite_pool=object(),
            pipeline_publisher=object(),
            nats_client=object(),
            db_path=None,
        )
        assert deps.what_the_machine_says().the_publisher_passed_its_self_check is True


# ---------------------------------------------------------------------------
# Every route's own key gets the same checks (13 October 2026)
# ---------------------------------------------------------------------------

#: The made-up text in a route's key. No sentence may contain it.
ROUTE_SECRET = "not-a-real-route-key-TESTONLY-5d0e"
ROUTED = "bench/routed-shop"


def _with_a_route_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: int = 0o600, text: str = ROUTE_SECRET
) -> tuple[PublisherSettings, Path]:
    from tests.forge.publisher.a_project_and_a_ledger import (
        a_route_key,
        a_route_keys_folder,
    )
    from forge.publisher.settings import ProjectRoute

    folder = a_route_keys_folder(tmp_path, monkeypatch)
    key = a_route_key(folder, "routed", text)
    if not text:
        key.write_text("", encoding="utf-8")
    key.chmod(mode)
    settings = _settings(tmp_path)
    settings = PublisherSettings(
        credential_file=settings.credential_file,
        ledger=settings.ledger,
        state_dir=settings.state_dir,
        projects={
            ROUTED: ProjectRoute(
                name=ROUTED,
                source="git://192.0.2.1:9418/p",
                remote="https://example.invalid/o/p.git",
                credential_name="routed",
            )
        },
    )
    return settings, key


def _said_about_the_route(said: str | None, key: Path) -> str:
    assert said, "a bad route key passed the self-check"
    assert ROUTED in said
    assert str(key) in said
    assert ROUTE_SECRET not in said
    return said


class TestEachRouteKeyIsCheckedToo:
    def test_a_good_route_key_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, _key = _with_a_route_key(tmp_path, monkeypatch)
        assert entry.why_it_will_not_start(
            settings, interfaces=_interfaces(tmp_path, "eth0")
        ) is None

    def test_a_route_credential_that_is_not_there_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, key = _with_a_route_key(tmp_path, monkeypatch)
        key.unlink()
        said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path, "eth0"))
        assert "could not be looked at" in _said_about_the_route(said, key)

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
    def test_a_route_credential_others_can_read_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
    ) -> None:
        settings, key = _with_a_route_key(tmp_path, monkeypatch, mode=mode)
        said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path, "eth0"))
        assert "readable by the publisher's own user alone" in _said_about_the_route(said, key)

    def test_a_route_credential_owned_by_someone_else_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, key = _with_a_route_key(tmp_path, monkeypatch)
        # The publisher-wide file is looked at with stat and stays this
        # user's; only what lstat says about the route key is made foreign.
        real = os.geteuid()
        monkeypatch.setattr(
            entry.os,
            "lstat",
            lambda where, _real=os.lstat: _as_owned_by(_real(where), real + 1),
        )
        said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path, "eth0"))
        assert "not to the publisher's own user" in _said_about_the_route(said, key)

    def test_a_route_credential_that_is_empty_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, key = _with_a_route_key(tmp_path, monkeypatch, text="")
        said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path, "eth0"))
        assert "is empty" in _said_about_the_route(said, key)

    def test_a_route_key_that_is_a_symbolic_link_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings, key = _with_a_route_key(tmp_path, monkeypatch)
        real = tmp_path / "elsewhere"
        real.write_text(ROUTE_SECRET, encoding="utf-8")
        real.chmod(0o600)
        key.unlink()
        key.symlink_to(real)
        said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path, "eth0"))
        assert "symbolic link" in _said_about_the_route(said, key)

    def test_the_check_never_opens_a_route_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It runs again on every health request, so it looks and never reads."""
        settings, key = _with_a_route_key(tmp_path, monkeypatch)
        key.chmod(0o000)  # unreadable even by its owner; looking still works
        try:
            assert entry.why_it_will_not_start(
                settings, interfaces=_interfaces(tmp_path, "eth0")
            ) is None
        finally:
            key.chmod(0o600)

    def test_main_refuses_to_start_for_a_bad_route_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        import json

        settings, key = _with_a_route_key(tmp_path, monkeypatch, mode=0o644)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "credential_file": settings.credential_file,
                    "ledger": settings.ledger,
                    "state_dir": settings.state_dir,
                    "private_path_prefixes": ["/home/example"],
                    "projects": {
                        ROUTED: {
                            "source": "git://192.0.2.1:9418/p",
                            "remote": "https://example.invalid/o/p.git",
                            "credential_name": "routed",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        # The route keys are looked at before the interfaces are counted, so
        # this machine's own interfaces do not decide the answer here.
        monkeypatch.setattr(
            entry,
            "serve",
            lambda *_a, **_k: pytest.fail("it started with a bad route key"),
        )
        assert entry.main(["--settings", str(settings_file)]) == 2
        err = capsys.readouterr().err
        assert "will not start" in err
        _said_about_the_route(err, key)

    def test_main_refuses_to_start_when_a_route_key_cannot_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Past the look, a key that will not READ (whitespace only) still
        stops the start: a route that names a key never reaches a send
        without it."""
        import json

        settings, key = _with_a_route_key(tmp_path, monkeypatch, text="   ")
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "credential_file": settings.credential_file,
                    "ledger": settings.ledger,
                    "state_dir": settings.state_dir,
                    "private_path_prefixes": ["/home/example"],
                    "projects": {
                        ROUTED: {
                            "source": "git://192.0.2.1:9418/p",
                            "remote": "https://example.invalid/o/p.git",
                            "credential_name": "routed",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            entry, "why_it_will_not_start", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            entry,
            "serve",
            lambda *_a, **_k: pytest.fail("it started with an unreadable route key"),
        )
        assert entry.main(["--settings", str(settings_file)]) == 2
        err = capsys.readouterr().err
        assert "will not start" in err and "is empty" in err
        _said_about_the_route(err, key)


def _as_owned_by(found: os.stat_result, uid: int) -> os.stat_result:
    fields = list(found)
    fields[4] = uid  # st_uid
    return os.stat_result(fields)
