"""A route may name a key of its own, by bare name, and nothing else.

13 October 2026, the publisher's per-route credentials. A route's
``credential_name`` is absent (the route uses the publisher-wide credential,
as before) or a bare key name inside the one protected folder. Anything else
is refused naming the route, never quietly read as "absent" — because absent
means "use the publisher-wide key", which for a route that meant its own key
is exactly the fallback that must never happen.

Nothing here contacts anything: the keys are made-up strings in a test
folder, and the program git would run is run here by hand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge.publisher import __main__ as entry
from forge.publisher.service import Publisher
from forge.publisher.settings import (
    ProjectRoute,
    PublisherSettings,
    SettingsRefused,
    load_settings,
)
from tests.forge.publisher.a_project_and_a_ledger import (
    a_route_key,
    a_route_keys_folder,
    the_credential_file,
)

SECRET_A = "not-a-real-route-key-A-TESTONLY-2b7e"
SECRET_B = "not-a-real-route-key-B-TESTONLY-e41d"


def _a_settings_file(tmp_path: Path, route: dict) -> Path:
    where = tmp_path / "settings.json"
    where.write_text(
        json.dumps(
            {
                "credential_file": "/etc/forge-publisher/credential",
                "ledger": "/var/lib/forge/forge.db",
                "state_dir": "/home/publisher/state",
                "projects": {
                    "guardkit/fixture": {
                        "source": "git://192.0.2.1:9418/fixture",
                        "remote": "https://example.invalid/guardkit/fixture.git",
                        **route,
                    },
                    "guardkit/other": {
                        "source": "git://192.0.2.1:9418/other",
                        "remote": "https://example.invalid/guardkit/other.git",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return where


def test_a_route_may_name_its_own_credential(tmp_path: Path) -> None:
    loaded = load_settings(
        _a_settings_file(tmp_path, {"credential_name": "widget-shop-key"})
    )
    assert loaded.projects["guardkit/fixture"].credential_name == "widget-shop-key"
    # A route that names nothing inherits, as before.
    assert loaded.projects["guardkit/other"].credential_name is None
    shown = loaded.without_secrets()["projects"]
    assert shown["guardkit/fixture"]["credential_name"] == "widget-shop-key"
    assert "credential_name" not in shown["guardkit/other"]


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, 7, ["a"], {"a": 1}, True, ".", "..", "a/b", "/etc/x", "a\n",
     "a b", "-leading-hyphen", ".hidden", "a" * 101],
)
def test_a_malformed_route_credential_name_is_refused(tmp_path: Path, value) -> None:
    with pytest.raises(SettingsRefused) as refused:
        load_settings(_a_settings_file(tmp_path, {"credential_name": value}))
    said = str(refused.value)
    assert "guardkit/fixture" in said
    assert "not a key name" in said
    # The value itself is not repeated (it may be something pasted by mistake).
    if isinstance(value, str) and value.strip() and len(value) > 2:
        assert value.strip() not in said.replace("guardkit/fixture", "")


@pytest.mark.parametrize("value", ["a", "widget-shop-key", "k.1_x-2", "A" * 100])
def test_a_well_formed_name_loads(tmp_path: Path, value: str) -> None:
    loaded = load_settings(_a_settings_file(tmp_path, {"credential_name": value}))
    assert loaded.projects["guardkit/fixture"].credential_name == value


def _publisher_settings(tmp_path: Path, routes: dict[str, str | None]) -> PublisherSettings:
    return PublisherSettings(
        credential_file=str(the_credential_file(tmp_path)),
        ledger=str(tmp_path / "forge.db"),
        state_dir=str(tmp_path / "state"),
        projects={
            name: ProjectRoute(
                name=name,
                source=f"git://192.0.2.1:9418/{name}",
                remote=f"https://example.invalid/{name}.git",
                credential_name=key,
            )
            for name, key in routes.items()
        },
    )


def _interfaces(tmp_path: Path) -> Path:
    folder = tmp_path / "net"
    for name in ("lo", "eth0"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    return folder


def test_a_route_key_that_is_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = a_route_keys_folder(tmp_path, monkeypatch)
    key = a_route_key(folder, "routed", SECRET_A)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(SECRET_B + "\n", encoding="utf-8")
    elsewhere.chmod(0o600)
    settings = _publisher_settings(tmp_path, {"bench/routed": "routed"})

    # The start-up check passes on the real file ...
    assert entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path)) is None
    # ... and refuses a link.
    key.unlink()
    key.symlink_to(elsewhere)
    said = entry.why_it_will_not_start(settings, interfaces=_interfaces(tmp_path))
    assert said and "symbolic link" in said and "bench/routed" in said

    # A link swapped in AFTER the check is refused by the read itself
    # (O_NOFOLLOW): the publisher holds nothing for the route, and says why.
    publisher = Publisher(settings)
    unread = publisher.why_route_keys_refuse_to_start()
    assert unread and "symbolic link" in unread and "bench/routed" in unread
    credential, refusal = publisher._the_credential_for(settings.projects["bench/routed"])
    assert credential is None
    assert refusal and SECRET_B not in refusal and SECRET_A not in refusal


def _the_line_git_would_get(publisher: Publisher, route: ProjectRoute, monkeypatch) -> str:
    """Run the program git would be handed for this route's copy, by hand."""
    from forge.publisher import git_work

    given: list[dict] = []
    with monkeypatch.context() as patched:
        def capture(*_args, env=None, **_kwargs):  # noqa: ANN001, ANN003
            given.append(env)
            raise OSError("not run in this test")

        patched.setattr(git_work.subprocess, "run", capture)
        publisher._its_own_copy(route).where_the_remotes_branch_is("main")
    assert given
    programs = {env["GIT_ASKPASS"] for env in given}
    assert len(programs) == 1
    done = subprocess.run([programs.pop(), "Password:"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_two_names_never_share_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = a_route_keys_folder(tmp_path, monkeypatch)
    a_route_key(folder, "key-a", SECRET_A)
    a_route_key(folder, "key-b", SECRET_B)
    settings = _publisher_settings(
        tmp_path, {"bench/a": "key-a", "bench/b": "key-b", "bench/inherits": None}
    )
    publisher = Publisher(settings)

    assert _the_line_git_would_get(publisher, settings.projects["bench/a"], monkeypatch) == SECRET_A
    assert _the_line_git_would_get(publisher, settings.projects["bench/b"], monkeypatch) == SECRET_B
    from tests.forge.publisher.a_project_and_a_ledger import THE_MADE_UP_CREDENTIAL

    assert (
        _the_line_git_would_get(publisher, settings.projects["bench/inherits"], monkeypatch)
        == THE_MADE_UP_CREDENTIAL
    )
