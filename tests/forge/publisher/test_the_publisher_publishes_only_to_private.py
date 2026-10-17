"""The publisher publishes only to repositories confirmed private, when told so.

Design "Building production features from a signed-off prototype", section
2.8 (17 October 2026, Codex R6). A repository is confirmed private only when
the route's own key reads it AND an anonymous GitHub lookup answers 404.
Every other answer refuses, with its own plain reason. With ``private_only``
off (the default) the publisher is exactly what it was.

NOTHING HERE CONTACTS ANYTHING REAL. The anonymous lookup is a stand-in, the
"remote" is a bare repository on disk (its key read is real git against it),
and every owner, name and key is made up.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from forge.publisher import private_remote, service
from forge.publisher.private_remote import (
    CONFIRMED_PRIVATE,
    IS_PUBLIC,
    LOOKUP_FAILED,
    NOT_ON_GITHUB,
    UNREACHABLE,
    anonymous_lookup,
    confirmed_private,
    github_repository_of,
)
from forge.publisher.service import NOT_CONFIRMED_PRIVATE, Publisher
from forge.publisher.settings import ProjectRoute, SettingsRefused, load_settings
from tests.forge.publisher.a_project_and_a_ledger import (
    a_request,
    every_push_the_remote_saw,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)

GITHUB_REMOTE = "git@github.com:example-owner/widget-shop.git"


# ---------------------------------------------------------------------------
# The one test, on its own


class _Lookup:
    """The anonymous lookup, answering what it is told, counting each ask."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def __call__(self, owner: str, name: str) -> int | None:
        self.asked.append((owner, name))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_private_repository_is_confirmed() -> None:
    lookup = _Lookup(404)
    answer = confirmed_private(GITHUB_REMOTE, key_reads_the_remote=lambda: True, lookup=lookup)
    assert answer.private is True and answer.kind == CONFIRMED_PRIVATE
    assert lookup.asked == [("example-owner", "widget-shop")]


def test_a_public_repository_is_refused() -> None:
    answer = confirmed_private(GITHUB_REMOTE, key_reads_the_remote=lambda: True, lookup=_Lookup(200))
    assert answer.private is False and answer.kind == IS_PUBLIC
    assert "public" in str(answer.reason)


@pytest.mark.parametrize("key_reads", [lambda: False, lambda: (_ for _ in ()).throw(OSError("x"))],
                         ids=["the-key-cannot-read", "the-read-fails-to-run"])
def test_a_missing_or_unreachable_repository_is_refused_and_never_looked_up(key_reads) -> None:
    lookup = _Lookup(404)
    answer = confirmed_private(GITHUB_REMOTE, key_reads_the_remote=key_reads, lookup=lookup)
    assert answer.private is False and answer.kind == UNREACHABLE
    assert "missing" in str(answer.reason)
    assert lookup.asked == []


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 301, None, OSError("no route")],
                         ids=["403", "429", "500", "502", "503", "301", "no-answer", "raised"])
def test_a_failed_lookup_is_refused(status: Any) -> None:
    answer = confirmed_private(GITHUB_REMOTE, key_reads_the_remote=lambda: True, lookup=_Lookup(status))
    assert answer.private is False and answer.kind == LOOKUP_FAILED
    assert "could not be told" in str(answer.reason)


def test_a_remote_that_is_not_on_github_cannot_be_confirmed() -> None:
    lookup = _Lookup(404)
    for remote in ("git@example.invalid:o/r.git", "/srv/remote.git", "https://example.invalid/o/r"):
        answer = confirmed_private(remote, key_reads_the_remote=lambda: True, lookup=lookup)
        assert answer.private is False and answer.kind == NOT_ON_GITHUB, remote
    assert lookup.asked == []


@pytest.mark.parametrize("remote, found", [
    ("git@github.com:example-owner/widget-shop.git", ("example-owner", "widget-shop")),
    ("ssh://git@github.com/example-owner/widget-shop.git", ("example-owner", "widget-shop")),
    ("https://github.com/example-owner/widget-shop", ("example-owner", "widget-shop")),
    ("https://github.com/example-owner/widget-shop.git", ("example-owner", "widget-shop")),
    ("git@GitHub.com:example-owner/w.s.git", ("example-owner", "w.s")),
    ("git@github.com:example-owner/widget-shop/extra.git", None),
    ("git@example.invalid:example-owner/widget-shop.git", None),
    ("https://github.com.example.invalid/o/r", None),
])
def test_which_remotes_name_a_github_repository(remote: str, found: Any) -> None:
    assert github_repository_of(remote) == found


class _Answer(io.BytesIO):
    def __init__(self, status: int) -> None:
        super().__init__(b"{}")
        self.status = status

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_the_anonymous_lookup_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def urlopen(request, timeout):  # noqa: ANN001, ANN202
        seen.append((request, timeout))
        return _Answer(200)

    monkeypatch.setattr(private_remote.urllib.request, "urlopen", urlopen)
    assert anonymous_lookup("example-owner", "widget-shop") == 200
    request, timeout = seen[0]
    assert request.full_url == "https://api.github.com/repos/example-owner/widget-shop"
    assert request.get_method() == "GET"
    assert not any(name.lower() in ("authorization", "cookie") for name in request.headers)
    assert timeout > 0


@pytest.mark.parametrize("raised, expected", [
    (urllib.error.HTTPError("u", 404, "Not Found", {}, None), 404),
    (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), 403),
    (urllib.error.URLError("no route"), None),
    (TimeoutError("slow"), None),
])
def test_the_anonymous_lookup_answers_a_status_or_nothing(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: Any
) -> None:
    def urlopen(request, timeout):  # noqa: ANN001, ANN202
        raise raised

    monkeypatch.setattr(private_remote.urllib.request, "urlopen", urlopen)
    assert anonymous_lookup("example-owner", "widget-shop") == expected


# ---------------------------------------------------------------------------
# The setting


def _settings_file(tmp_path: Path, **extra: Any) -> Path:
    where = tmp_path / "settings.json"
    where.write_text(json.dumps({
        "credential_file": "/k", "ledger": "/l", "state_dir": "/s",
        "private_path_prefixes": ["/home/example"],
        **extra,
    }))
    return where


def test_the_setting_is_off_unless_the_file_says_true(tmp_path: Path) -> None:
    assert load_settings(_settings_file(tmp_path)).private_only is False
    assert load_settings(_settings_file(tmp_path, private_only=False)).private_only is False
    on = load_settings(_settings_file(tmp_path, private_only=True))
    assert on.private_only is True
    assert on.without_secrets()["private_only"] is True


@pytest.mark.parametrize("value", ["true", 1, None, [], "yes"])
def test_the_setting_is_true_or_false_and_nothing_else(tmp_path: Path, value: Any) -> None:
    with pytest.raises(SettingsRefused, match="private_only"):
        load_settings(_settings_file(tmp_path, private_only=value))


def test_the_sample_settings_still_load_and_say_off() -> None:
    sample = Path(service.__file__).with_name("settings.sample.json")
    assert json.loads(sample.read_text())["private_only"] is False


# ---------------------------------------------------------------------------
# The publisher: at start-up, and immediately before every push


@pytest.fixture()
def project(tmp_path: Path) -> dict:
    return make_the_project(tmp_path / "world")


@pytest.fixture()
def as_github(monkeypatch: pytest.MonkeyPatch, project: dict):
    """The publisher's routes reach a bare repository on disk; for the test,
    each bare remote stands for a made-up GitHub repository of the same last
    name. The key read is still the real ``git ls-remote`` against it."""
    real = private_remote.confirmed_private

    def as_if_on_github(remote: str, **kw: Any):  # noqa: ANN202
        name = Path(str(remote)).name.removesuffix(".git")
        return real(f"git@github.com:example-owner/{name}.git", **kw)

    monkeypatch.setattr(service, "confirmed_private", as_if_on_github)


def _publisher(tmp_path: Path, project: dict, *, private_only: bool, lookup: Any,
               extra_routes: dict[str, ProjectRoute] | None = None) -> Publisher:
    import dataclasses

    root = tmp_path / "world"
    make_the_ledger(root / "forge.db", project=project)
    settings = settings_for(root, project, ledger=root / "forge.db")
    if extra_routes:
        settings = dataclasses.replace(settings, projects={**settings.projects, **extra_routes})
    settings = dataclasses.replace(settings, private_only=private_only)
    return Publisher(settings, visibility_lookup=lookup)


def _never(owner: str, name: str) -> int | None:
    raise AssertionError("with private_only off nothing is looked up")


def test_off_it_is_todays_publisher_and_looks_nothing_up(tmp_path: Path, project: dict) -> None:
    publisher = _publisher(tmp_path, project, private_only=False, lookup=_never)
    assert publisher.why_routes_are_not_private() == {}
    answer = publisher.publish(a_request(project))
    assert answer.published is True, answer.refusal
    assert what_the_remote_has(project["bare"]) == project["j"]


def test_on_a_confirmed_private_route_is_sent_and_asked_twice(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    lookup = _Lookup(404)
    publisher = _publisher(tmp_path, project, private_only=True, lookup=lookup)
    assert publisher.why_routes_are_not_private() == {}
    answer = publisher.publish(a_request(project))
    assert answer.published is True, answer.refusal
    assert what_the_remote_has(project["bare"]) == project["j"]
    assert lookup.asked == [("example-owner", "remote")] * 2, "once at start, once before the push"


def test_on_a_public_route_is_refused_from_the_start_and_the_others_still_work(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    other_remote = tmp_path / "world" / "public-one.git"
    subprocess.run(["git", "init", "--bare", "-q", str(other_remote)], check=True)
    other = ProjectRoute(name="bench/public-one", source=str(project["copy"]), remote=str(other_remote))

    def lookup(owner: str, name: str) -> int:
        return 200 if name == "public-one" else 404

    publisher = _publisher(tmp_path, project, private_only=True, lookup=lookup,
                           extra_routes={other.name: other})
    refused = publisher.why_routes_are_not_private()
    assert list(refused) == ["bench/public-one"]
    assert "public" in refused["bench/public-one"]

    answer = publisher.publish({**a_request(project), "project": "bench/public-one"})
    assert answer.published is False
    assert answer.refusal_kind == NOT_CONFIRMED_PRIVATE
    assert "public" in str(answer.refusal) and "restart" in str(answer.refusal)

    sent = publisher.publish(a_request(project))
    assert sent.published is True, sent.refusal


def test_on_a_route_its_key_cannot_read_at_start_is_not_kept_and_the_push_check_decides(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    """Follow-up to the coach review: a failed key read at start may only be
    the network not being up yet, so it is logged and not kept."""
    gone = ProjectRoute(name="bench/gone", source=str(project["copy"]),
                        remote=str(tmp_path / "world" / "no-such-remote.git"))
    lookup = _Lookup(404)
    publisher = _publisher(tmp_path, project, private_only=True, lookup=lookup,
                           extra_routes={gone.name: gone})
    assert publisher.why_routes_are_not_private() == {}
    assert ("example-owner", "no-such-remote") not in lookup.asked


class _KeyReadsOnlyLater:
    """The publisher's own copy, whose key read fails the first time (the
    network not up yet) and works after that, or never."""

    def __init__(self, real: Any, *, fails: int) -> None:
        self._real = real
        self.fails = fails

    def the_key_reads_the_remote(self):  # noqa: ANN201
        if self.fails > 0:
            self.fails -= 1
            from forge.publisher.git_work import GitSaid

            return GitSaid(ok=False, said="git ls-remote stopped with exit code 128")
        return self._real.the_key_reads_the_remote()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _flaky_publisher(tmp_path: Path, project: dict, *, fails: int, lookup: Any) -> Publisher:
    import dataclasses

    root = tmp_path / "world"
    make_the_ledger(root / "forge.db", project=project)
    settings = dataclasses.replace(
        settings_for(root, project, ledger=root / "forge.db"), private_only=True
    )
    holder: dict[str, Any] = {}

    def commits_for(route: ProjectRoute) -> Any:
        if "copy" not in holder:
            holder["copy"] = _KeyReadsOnlyLater(publisher_ref["p"]._its_own_copy(route), fails=fails)
        return holder["copy"]

    publisher_ref: dict[str, Any] = {}

    class _Late(Publisher):
        def __init__(self, *a: Any, **k: Any) -> None:
            publisher_ref["p"] = self
            super().__init__(*a, **k)

    return _Late(settings, visibility_lookup=lookup, commits_for=commits_for)


def test_a_key_read_that_fails_at_start_then_works_lets_the_push_through_after_a_404(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    lookup = _Lookup(404)
    publisher = _flaky_publisher(tmp_path, project, fails=1, lookup=lookup)
    assert publisher.why_routes_are_not_private() == {}
    assert lookup.asked == [], "a route its key could not read is never looked up"

    answer = publisher.publish(a_request(project))

    assert answer.published is True, answer.refusal
    assert lookup.asked == [("example-owner", "remote")]
    assert what_the_remote_has(project["bare"]) == project["j"]


def test_a_key_read_that_fails_at_start_and_at_the_push_sends_nothing(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    publisher = _flaky_publisher(tmp_path, project, fails=2, lookup=_Lookup(404))
    assert publisher.why_routes_are_not_private() == {}

    answer = publisher.publish(a_request(project))

    assert answer.published is False and answer.refusal_kind == NOT_CONFIRMED_PRIVATE
    assert "missing" in str(answer.refusal)
    assert every_push_the_remote_saw(project["bare"]) == []


@pytest.mark.parametrize("later", [200, 403, 429, 503, None],
                         ids=["now-public", "403", "429", "503", "no-answer"])
def test_on_the_check_before_the_push_refuses_the_push(
    tmp_path: Path, project: dict, as_github: None, later: Any
) -> None:
    lookup = _Lookup(404, later)
    publisher = _publisher(tmp_path, project, private_only=True, lookup=lookup)
    assert publisher.why_routes_are_not_private() == {}
    before = what_the_remote_has(project["bare"])

    answer = publisher.publish(a_request(project))

    assert answer.published is False
    assert answer.refusal_kind == NOT_CONFIRMED_PRIVATE
    assert "nothing was sent" in str(answer.refusal)
    assert what_the_remote_has(project["bare"]) == before
    assert every_push_the_remote_saw(project["bare"]) == []


def test_on_a_route_with_no_credential_cannot_be_confirmed(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    import dataclasses

    root = tmp_path / "world"
    make_the_ledger(root / "forge.db", project=project)
    settings = dataclasses.replace(
        settings_for(root, project, ledger=root / "forge.db",
                     credential_file=root / "no-credential-here"),
        private_only=True,
    )
    lookup = _Lookup(404)
    publisher = Publisher(settings, visibility_lookup=lookup)
    # Not kept at start (a key read that fails is left to the push check),
    # and never confirmed: with no credential there is no key read at all.
    assert publisher.why_routes_are_not_private() == {}
    assert lookup.asked == []
    assert publisher.publish(a_request(project)).published is False
    assert every_push_the_remote_saw(project["bare"]) == []


# ---------------------------------------------------------------------------
# Coach review, 17 October 2026: a failed lookup at start is passing trouble


@pytest.mark.parametrize("at_start", [403, 429, 500, 503, None],
                         ids=["403", "429", "500", "503", "no-answer"])
def test_a_failed_lookup_at_start_is_not_kept_and_the_push_check_decides(
    tmp_path: Path, project: dict, as_github: None, at_start: Any
) -> None:
    lookup = _Lookup(at_start, 404)
    publisher = _publisher(tmp_path, project, private_only=True, lookup=lookup)
    assert publisher.why_routes_are_not_private() == {}, "a failed lookup is not kept"

    answer = publisher.publish(a_request(project))

    assert answer.published is True, answer.refusal
    assert len(lookup.asked) == 2


def test_a_failed_lookup_at_start_and_again_before_the_push_sends_nothing(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    publisher = _publisher(tmp_path, project, private_only=True, lookup=_Lookup(429))
    assert publisher.why_routes_are_not_private() == {}
    answer = publisher.publish(a_request(project))
    assert answer.published is False and answer.refusal_kind == NOT_CONFIRMED_PRIVATE
    assert "could not be told" in str(answer.refusal)
    assert every_push_the_remote_saw(project["bare"]) == []


def test_the_health_answer_names_the_routes_refused_at_start(
    tmp_path: Path, project: dict, as_github: None
) -> None:
    import urllib.request

    other_remote = tmp_path / "world" / "public-one.git"
    subprocess.run(["git", "init", "--bare", "-q", str(other_remote)], check=True)
    other = ProjectRoute(name="bench/public-one", source=str(project["copy"]), remote=str(other_remote))
    publisher = _publisher(
        tmp_path, project, private_only=True,
        lookup=lambda owner, name: 200 if name == "public-one" else 404,
        extra_routes={other.name: other},
    )
    server, _thread = service.serve(publisher.settings, publisher=publisher)
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=10) as said:
            body = json.loads(said.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
    assert list(body["routes_refused_at_start"]) == ["bench/public-one"]
    assert "public" in body["routes_refused_at_start"]["bench/public-one"]
