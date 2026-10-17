"""``forge register-repo`` for a private repository (17 October 2026).

Design "Building production features from a signed-off prototype", section
2.8:

* ``--via-git-door`` records the project's fetch address through its own
  sandbox's Git door port and makes the sandbox's read through it, with no
  credential (one sandbox per project; test_register_repo_sandbox.py);
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

# ---------------------------------------------------------------------------
# --via-git-door: since one sandbox per project (17 October 2026) a project is
# read through its own sandbox's Git door port, and the shared sandbox never
# reads through the door; tests/forge/cli/test_register_repo_sandbox.py.


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
    """The route the design describes: a private repository registered in a
    sandbox of its own, through its Git door port, with its own write key, the
    publisher private-only."""
    from tests.forge.cli.test_register_repo_sandbox import (
        DOOR_ADDRESS,
        OWN,
        OWN_SETTINGS,
        OWN_YAML,
        WORKSPACE,
        _run_own,
    )

    estate.sandbox_name, estate.clone_root = OWN, WORKSPACE
    estate.sandbox_files = {OWN_SETTINGS: OWN_YAML}
    estate.modes[OWN_SETTINGS] = "600"
    _private_only(publisher_file)
    monkeypatch.setattr(register_repo, "visibility_lookup", _Lookup(404))
    result = _run_own(
        "--publish", "--publisher-settings", str(publisher_file),
        "--publish-credential-name", "widget-shop-key",
        "--estate-env-file", str(route_key_estate), "--via-git-door",
    )
    assert result.exit_code == 0, result.output
    staged = json.loads((publisher_file.parent / f"settings.json.{LEAF}-pending").read_text())
    assert staged["projects"][KEY]["remote"] == f"git@github.com:{KEY}.git"
    coordinator = yaml.safe_load(estate.volume[f"forge.yaml.{LEAF}-pending"])
    assert coordinator["planning"]["fetch_addresses"] == {KEY: DOOR_ADDRESS}
    assert estate.clones == {}


@pytest.mark.parametrize("github", [
    f"https://example.invalid/guardkit/{LEAF}.git",
    f"https://github.com.example.invalid/guardkit/{LEAF}.git",
])
def test_private_only_a_repository_not_on_github_cannot_be_confirmed(
    estate, publisher_file, route_key_estate, monkeypatch, github  # noqa: F811
):
    """Coach review: the key read went to the --github host, so it proves a
    GitHub repository only when that host is github.com."""
    from click.testing import CliRunner

    from forge.cli.main import main
    from tests.forge.cli.test_register_repo import SANDBOX_SETTINGS

    _private_only(publisher_file)
    lookup = _Lookup(404)
    monkeypatch.setattr(register_repo, "visibility_lookup", lookup)
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", github, "--sandbox-settings", SANDBOX_SETTINGS,
        "--publish", "--publisher-settings", str(publisher_file),
        "--publish-credential-name", "widget-shop-key",
        "--estate-env-file", str(route_key_estate),
    ], catch_exceptions=False)
    assert result.exit_code == 1
    assert "github.com" in result.output and "another host" in result.output
    assert lookup.asked == []
    assert estate.writes == [] and estate.clones == {}


def test_private_only_the_github_host_is_matched_whatever_its_case(
    estate, publisher_file, route_key_estate, monkeypatch  # noqa: F811
):
    from click.testing import CliRunner

    from forge.cli.main import main
    from tests.forge.cli.test_register_repo import SANDBOX_SETTINGS

    _private_only(publisher_file)
    monkeypatch.setattr(register_repo, "visibility_lookup", _Lookup(404))
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", f"https://GitHub.com/guardkit/{LEAF}.git",
        "--sandbox-settings", SANDBOX_SETTINGS,
        "--publish", "--publisher-settings", str(publisher_file),
        "--publish-credential-name", "widget-shop-key",
        "--estate-env-file", str(route_key_estate),
    ], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "confirmed private" in result.output
