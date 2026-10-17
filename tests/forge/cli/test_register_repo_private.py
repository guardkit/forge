"""``forge register-repo`` for a private repository (17 October 2026).

Design "Building production features from a signed-off prototype", section
2.8:

* ``--via-git-door`` records the project's fetch address through the Git
  door (``<planning.git_door><org>/<name>.git``) and makes the sandbox's read
  and clone through it, with no credential;
* when the publisher's settings say ``private_only``, a route is written only
  for a repository confirmed private: the route's own key read it (step 1's
  check clone) and an anonymous lookup answers 404.

The estate is the same stand-in the main register-repo tests use
(:class:`~tests.forge.cli.test_register_repo.FakeEstate`): nothing here
reaches git, docker, sbx or GitHub, and every name is made up.
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from forge.cli import register_repo
from tests.forge.cli.test_register_repo import (  # noqa: F401
    CLONE_PATH,
    COORDINATOR_YAML,
    KEY,
    LEAF,
    URL,
    _run,
    estate,
    publisher_file,
    route_key_estate,
)

DOOR = "http://192.0.2.1:31825/"
DOOR_ADDRESS = f"{DOOR}{KEY}.git"


def _with_door(estate, door: str = DOOR) -> None:  # noqa: F811
    estate.volume["forge.yaml"] = COORDINATOR_YAML.replace(
        "planning:\n  enabled: true\n", f"planning:\n  enabled: true\n  git_door: {door}\n"
    )


def _sandbox_git(estate, command: str) -> list[list[str]]:  # noqa: F811
    return [c for c in estate.calls if c[:2] == ["sbx", "exec"] and "git" in c and command in c]


# ---------------------------------------------------------------------------
# --via-git-door


def test_through_the_door_the_address_and_the_sandbox_clone_go_through_it(estate):  # noqa: F811
    _with_door(estate)
    result = _run("--via-git-door")
    assert result.exit_code == 0, result.output

    staged = yaml.safe_load(estate.volume[f"forge.yaml.{LEAF}-pending"])
    assert staged["planning"]["fetch_addresses"] == {KEY: DOOR_ADDRESS}
    assert staged["planning"]["git_door"] == DOOR
    assert estate.clones == {CLONE_PATH: DOOR_ADDRESS}

    reads = _sandbox_git(estate, "ls-remote") + _sandbox_git(estate, "clone")
    assert reads, "the sandbox read and cloned"
    for call in reads:
        assert DOOR_ADDRESS in call
        assert "GIT_ALLOW_PROTOCOL=http" in call
        assert not any(part.startswith(("GIT_SSH_COMMAND=", "GIT_ASKPASS=/")) for part in call)
        assert "credential.helper=" in call, "credential helpers are still cleared"
    assert "allow-list" in result.output and KEY in result.output


def test_without_the_flag_nothing_about_the_door_changes(estate):  # noqa: F811
    _with_door(estate)
    result = _run()
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume[f"forge.yaml.{LEAF}-pending"])
    assert staged["planning"]["fetch_addresses"] == {KEY: URL}
    assert estate.clones == {CLONE_PATH: URL}
    assert "allow-list" not in result.output


@pytest.mark.parametrize("door, said", [
    (None, "add planning.git_door"),
    ("${FACTORY_GIT_DOOR_URL}", "address itself"),
    ("http://0.0.0.0:31825/", "every address"),
])
def test_the_door_must_be_declared_plainly_or_nothing_is_written(estate, door, said):  # noqa: F811
    if door is not None:
        _with_door(estate, door)
    result = _run("--via-git-door")
    assert result.exit_code == 1
    assert said in result.output
    assert estate.writes == [] and estate.clones == {}


def test_a_repository_the_door_will_not_read_is_refused_with_the_allow_list_reminder(estate):  # noqa: F811
    _with_door(estate)
    estate.sandbox_can_read = False
    result = _run("--via-git-door")
    assert result.exit_code == 1
    assert "through the Git door" in result.output and "allow-list" in result.output
    assert estate.writes == [] and estate.clones == {}


# ---------------------------------------------------------------------------
# The publisher private-only


class _Lookup:
    def __init__(self, status):  # noqa: ANN001
        self.status = status
        self.asked: list[tuple[str, str]] = []

    def __call__(self, owner: str, name: str):  # noqa: ANN204
        self.asked.append((owner, name))
        return self.status


def _private_only(publisher_file, on: bool = True) -> None:  # noqa: F811
    data = json.loads(publisher_file.read_text())
    data["private_only"] = on
    publisher_file.write_text(json.dumps(data))


def _publish_with_key(publisher_file, route_key_estate, *extra: str):  # noqa: F811
    return _run(
        "--publish", "--publisher-settings", str(publisher_file),
        "--publish-credential-name", "widget-shop-key",
        "--estate-env-file", str(route_key_estate), *extra,
    )


def test_private_only_a_confirmed_private_repository_gets_its_route(
    estate, publisher_file, route_key_estate, monkeypatch  # noqa: F811
):
    _private_only(publisher_file)
    lookup = _Lookup(404)
    monkeypatch.setattr(register_repo, "visibility_lookup", lookup)
    result = _publish_with_key(publisher_file, route_key_estate)
    assert result.exit_code == 0, result.output
    assert lookup.asked == [("guardkit", LEAF)]
    assert "confirmed private" in result.output
    assert (publisher_file.parent / f"settings.json.{LEAF}-pending").is_file()
    assert estate.host_clone_env["GIT_ALLOW_PROTOCOL"] == "ssh", "its own key read it"


@pytest.mark.parametrize("status, said", [
    (200, "public"),
    (403, "could not be told"),
    (429, "could not be told"),
    (500, "could not be told"),
    (None, "could not be told"),
])
def test_private_only_anything_but_confirmed_private_is_refused_before_any_write(
    estate, publisher_file, route_key_estate, monkeypatch, status, said  # noqa: F811
):
    _private_only(publisher_file)
    monkeypatch.setattr(register_repo, "visibility_lookup", _Lookup(status))
    result = _publish_with_key(publisher_file, route_key_estate)
    assert result.exit_code == 1
    assert said in result.output
    assert estate.writes == [] and estate.clones == {}
    assert sorted(os.listdir(publisher_file.parent)) == ["settings.json"]


def test_private_only_a_repository_its_key_cannot_read_is_refused_and_never_looked_up(
    estate, publisher_file, route_key_estate, monkeypatch  # noqa: F811
):
    _private_only(publisher_file)
    lookup = _Lookup(404)
    monkeypatch.setattr(register_repo, "visibility_lookup", lookup)
    estate.host_can_read = False
    result = _publish_with_key(publisher_file, route_key_estate)
    assert result.exit_code == 1
    assert "route key" in result.output
    assert lookup.asked == []
    assert estate.writes == []


def test_private_only_needs_the_route_s_own_key(estate, publisher_file, monkeypatch):  # noqa: F811
    _private_only(publisher_file)
    lookup = _Lookup(404)
    monkeypatch.setattr(register_repo, "visibility_lookup", lookup)
    result = _run("--publish", "--publisher-settings", str(publisher_file))
    assert result.exit_code == 1
    assert "--publish-credential-name" in result.output and "private" in result.output
    assert lookup.asked == []
    assert estate.writes == []


@pytest.mark.parametrize("on", [False, None], ids=["false", "absent"])
def test_private_only_off_nothing_is_looked_up(
    estate, publisher_file, route_key_estate, monkeypatch, on  # noqa: F811
):
    if on is not None:
        _private_only(publisher_file, on)

    def never(owner, name):  # noqa: ANN001, ANN202
        raise AssertionError("nothing is looked up with private_only off")

    monkeypatch.setattr(register_repo, "visibility_lookup", never)
    result = _publish_with_key(publisher_file, route_key_estate)
    assert result.exit_code == 0, result.output
    assert "confirmed private" not in result.output


def test_private_only_and_the_door_together(
    estate, publisher_file, route_key_estate, monkeypatch  # noqa: F811
):
    """The route the design describes: a private repository registered
    through the Git door, with its own write key, the publisher private-only."""
    _with_door(estate)
    _private_only(publisher_file)
    monkeypatch.setattr(register_repo, "visibility_lookup", _Lookup(404))
    result = _publish_with_key(publisher_file, route_key_estate, "--via-git-door")
    assert result.exit_code == 0, result.output
    staged = json.loads((publisher_file.parent / f"settings.json.{LEAF}-pending").read_text())
    assert staged["projects"][KEY]["remote"] == f"git@github.com:{KEY}.git"
    assert estate.clones == {CLONE_PATH: DOOR_ADDRESS}
