"""Fetching a private repository through the Git door (17 October 2026).

Design "Building production features from a signed-off prototype", section
2.8. The sandbox holds no GitHub credential; a private repository is read
through a door on the factory's own machine, at one declared
``planning.git_door`` base (``http://<address>:<port>/``). Forge takes an
``http`` fetch address only when it is exactly that base followed by
``<owner>/<name>.git``; with no door declared, the rule is today's (https
only).

The real fetches here go to a small smart-HTTP server on 127.0.0.1 standing
in for the door. Nothing here reaches GitHub, the door, the sandbox or any
service; every name and address is made up.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from forge import network_git as ng
from forge.config.models import ForgeConfig, PlanningConfig
from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import process_git_remote_start_point_request
from forge.planning.sidecar_git_runner import SidecarGitRunner
from tests.forge.git.git_marker_lab import SmartHttp
from tests.forge.git.network_cases import NetLab, netlab  # noqa: F401

DOOR = "http://192.0.2.1:31825/"
REPO = "example-owner/a-repo"


# ---------------------------------------------------------------------------
# The door's own address


@pytest.mark.parametrize("door", [
    "http://192.0.2.1:31825/",
    "http://[2001:db8::1]:31825/",
    "http://gateway.example.test:1/",
])
def test_a_door_base_is_one_http_address_a_port_and_a_slash(door: str) -> None:
    assert ng.git_door_problem(door) is None


@pytest.mark.parametrize("door, why", [
    ("https://192.0.2.1:31825/", "http://"),
    ("http://192.0.2.1/", "no port"),
    ("http://192.0.2.1:31825", "path"),
    ("http://192.0.2.1:31825/x/", "path"),
    ("http://someone@192.0.2.1:31825/", "@"),
    ("http://192.0.2.1:31825/?a=1", "?"),
    ("http://192.0.2.1:31825/#x", "#"),
    ("http://0.0.0.0:31825/", "every address"),
    ("http://[::]:31825/", "every address"),
    ("http://192.0.2.1:0/", "port"),
    ("http://192.0.2.1:031825/", "port"),
    ("http://192.0.2.1:99999/", "port"),
    ("http://192.0.2:31825/", "address"),
    (" http://192.0.2.1:31825/", "space"),
    ("", "empty"),
])
def test_anything_else_is_not_a_door(door: str, why: str) -> None:
    problem = ng.git_door_problem(door)
    assert problem and why in problem, problem
    if door.strip():
        assert door.strip() not in problem


def test_the_door_address_of_a_repository() -> None:
    assert ng.git_door_address(DOOR, REPO) == f"{DOOR}{REPO}.git"
    for bad in ("one-part", "a/b/c", "../x", "a/.git", ""):
        with pytest.raises(ValueError):
            ng.git_door_address(DOOR, bad)
    with pytest.raises(ValueError):
        ng.git_door_address("http://0.0.0.0:1/", REPO)


# ---------------------------------------------------------------------------
# Which fetch addresses are taken


def test_with_the_door_declared_only_its_own_addresses_are_taken_over_http() -> None:
    assert ng.check_fetch_address(f"{DOOR}{REPO}.git", git_door=DOOR) is None
    for bad in (
        f"{DOOR}{REPO}",                         # no .git
        f"{DOOR}{REPO}/more.git",                # three parts
        f"{DOOR}../a-repo.git",                  # not a name
        f"http://192.0.2.1:31826/{REPO}.git",    # another port
        f"http://192.0.2.2:31825/{REPO}.git",    # another address
        f"http://github.com/{REPO}.git",         # plain http elsewhere
        f"http://x@192.0.2.1:31825/{REPO}.git",  # a user name
        f"{DOOR}{REPO}.git?x=1",
    ):
        sentence = ng.check_fetch_address(bad, git_door=DOOR)
        assert sentence and "Git door" in sentence, bad
        assert bad not in sentence
    # https stays as it was, with or without a door.
    assert ng.check_fetch_address("https://github.com/o/r.git", git_door=DOOR) is None
    assert ng.check_fetch_address("https://t@github.com/o/r.git", git_door=DOOR)
    for other in ("git://192.0.2.1/o/r.git", "ssh://git@github.com/o/r.git", "/srv/r.git"):
        assert ng.check_fetch_address(other, git_door=DOOR), other


def test_without_a_door_the_rule_is_todays() -> None:
    door_address = f"{DOOR}{REPO}.git"
    assert ng.check_fetch_address(door_address) == ng.check_fetch_address(
        "http://example.invalid/o/r"
    )
    assert "https://" in (ng.check_fetch_address(door_address) or "")
    assert ng.check_fetch_address("https://github.com/o/r.git") is None


def test_a_broken_door_refuses_the_address_and_says_why() -> None:
    sentence = ng.check_fetch_address("http://0.0.0.0:1/o/r.git", git_door="http://0.0.0.0:1/")
    assert sentence and "planning.git_door" in sentence and "every address" in sentence


# ---------------------------------------------------------------------------
# The coordinator's settings


def test_the_settings_take_a_door_address_only_with_the_door_declared() -> None:
    taken = PlanningConfig(git_door=DOOR, fetch_addresses={REPO: f"{DOOR}{REPO}.git"})
    assert taken.fetch_addresses == {REPO: f"{DOOR}{REPO}.git"}
    assert PlanningConfig().git_door is None
    with pytest.raises(ValueError, match="https://"):
        PlanningConfig(fetch_addresses={REPO: f"{DOOR}{REPO}.git"})
    with pytest.raises(ValueError, match="Git door"):
        PlanningConfig(git_door=DOOR, fetch_addresses={REPO: f"http://192.0.2.9:1/{REPO}.git"})
    with pytest.raises(ValueError, match="planning.git_door"):
        PlanningConfig(git_door="http://192.0.2.1:31825/x/")


# ---------------------------------------------------------------------------
# A real fetch through a door-shaped address, with no credential


@pytest.fixture()
def door(netlab: NetLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    """A smart-HTTP server standing in for the door, serving one repository
    at ``<owner>/<name>.git``. Production's rule only: https, plus the door."""
    served = tmp_path / "door-served"
    shutil.copytree(netlab.good, served / "example-owner" / "a-repo.git")
    server = SmartHttp(served)
    monkeypatch.setattr(ng, "FETCH_TRANSPORTS", ("https",))
    try:
        yield server
    finally:
        server.close()


def test_the_start_point_is_fetched_through_the_door_with_no_credential(
    netlab: NetLab, door: SmartHttp  # noqa: F811
) -> None:
    base = door.base + "/"
    address = f"{base}{REPO}.git"
    clone = netlab.clone()

    refused = ng.fetch_start_point(clone, address, repo=REPO)
    assert refused.refusal and "https://" in refused.refusal
    assert door.seen == [], "without the door declared nothing is fetched"

    start = ng.fetch_start_point(clone, address, repo=REPO, git_door=base)
    assert start.refusal is None, start.refusal
    assert (start.branch, start.commit) == ("main", netlab.good_commit)
    assert door.seen, "the fetch went to the door"
    assert all(r["path"].startswith(f"/{REPO}.git/") for r in door.seen)
    assert door.header_values("authorization") == []
    assert door.passwords() == []

    every = ng.fetch_all_branches(netlab.clone("again"), address, repo=REPO, git_door=base)
    assert every.refusal is None, every.refusal
    assert every.branches.get("main") == netlab.good_commit


def test_the_sandbox_route_takes_the_door_the_coordinator_sends(
    netlab: NetLab, door: SmartHttp  # noqa: F811
) -> None:
    base = door.base + "/"
    clone = netlab.clone()
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )
    address = f"{base}{REPO}.git"

    status, body = process_git_remote_start_point_request(
        {"repo": REPO, "fetch_address": address}, config=config
    )
    assert status == 400 and "https://" in body["error"]

    status, body = process_git_remote_start_point_request(
        {"repo": REPO, "fetch_address": address, "git_door": "http://0.0.0.0:1/"},
        config=config,
    )
    assert status == 400 and "git_door" in body["error"]

    status, body = process_git_remote_start_point_request(
        {"repo": REPO, "fetch_address": address, "git_door": base}, config=config
    )
    assert status == 200, body
    assert body["refusal"] is None
    assert body["commit"] == netlab.good_commit
    assert door.header_values("authorization") == []


# ---------------------------------------------------------------------------
# The coordinator sends its door beside the address, and only then


class _Post:
    def __init__(self, answer: tuple[int, Any]) -> None:
        self.answer = answer
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        self.sent.append(body)
        return self.answer


START = (200, {"branch": "main", "commit": "a" * 40, "refusal": None})


@pytest.mark.asyncio
async def test_the_sandbox_surfaces_send_the_door_beside_the_address() -> None:
    address = f"{DOOR}{REPO}.git"
    for make in (
        lambda post, **kw: SidecarCandidateGit("http://127.0.0.1:8225", repo=REPO, post=post, **kw),
    ):
        post = _Post(START)
        await make(post, fetch_address=address, git_door=DOOR).fetch_remote_start_point()
        assert post.sent[-1] == {"repo": REPO, "fetch_address": address, "git_door": DOOR}
        post = _Post(START)
        await make(post, fetch_address="https://github.com/o/r.git").fetch_remote_start_point()
        assert "git_door" not in post.sent[-1]
        post = _Post(START)
        await make(post, git_door=DOOR).fetch_remote_start_point()
        assert post.sent[-1] == {"repo": REPO}

    post = _Post(START)
    runner = SidecarGitRunner(
        "http://127.0.0.1:8225", repo=REPO, post=post, fetch_address=address, git_door=DOOR
    )
    await runner.fetch_remote_start_point("/srv/repos/a-repo")
    assert post.sent[-1]["git_door"] == DOOR

    reader_post = _Post((200, {"files": [], "commit": "b" * 40}))
    reader = SidecarGitRunner(
        "http://127.0.0.1:8225", repo=REPO, post=reader_post, fetch_address=address,
        git_door=DOOR,
    ).code_reader("b" * 40)
    try:
        reader.list_files()
    except Exception:  # noqa: BLE001 - only what was sent matters here
        pass
    assert reader_post.sent and reader_post.sent[0]["git_door"] == DOOR
    assert reader_post.sent[0]["fetch_address"] == address
