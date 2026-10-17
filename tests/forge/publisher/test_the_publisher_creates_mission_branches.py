"""The publisher creates a mission branch, and recovers a lost answer honestly.

Missions design (17 October 2026), section 4, "Creating the mission branch,
with recovery": the request kind ``create-mission-branch {project,
mission_id, branch, commit}``. The publisher refuses unless the name is a
mission branch name, the remote has no such branch, and the commit is the
remote's default branch's tip as it reads it itself. It writes its own record
before pushing, pushes without forcing, and reads the remote back. A repeated
request for a branch that exists is "created" with nothing pushed only when
its record shows it created exactly that branch for that mission at that
commit and the remote's tip is still it.

THE "REMOTE" IS A BARE REPOSITORY ON DISK, as in every publisher test. Nothing
here contacts a real remote, starts a daemon, builds an image or touches a
sandbox. Pushes are counted on the remote's own reflog.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline import publisher_client
from forge.publisher import git_work
from forge.publisher.mission_branches import CONFIRMED, INTENDED, TheMissionBranchRecords
from forge.publisher import service as the_service
from forge.publisher.service import MISSION_BRANCH_ROUTE, Publisher, serve
from tests.forge.publisher.a_project_and_a_ledger import (
    PROJECT,
    git,
    make_the_ledger,
    make_the_project,
    settings_for,
    somebody_else_lands_work,
)

MISSION = "m-3fa21c"
BRANCH = f"mission/{MISSION}"


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "world"
    project = make_the_project(root)
    make_the_ledger(root / "forge.db", project=project)
    settings = settings_for(root, project, ledger=root / "forge.db")
    return {"root": root, "project": project, "settings": settings}


def _ask(world: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    asked = {
        "project": PROJECT,
        "mission_id": MISSION,
        "branch": BRANCH,
        "commit": world["project"]["g"],
    }
    asked.update(overrides)
    return asked


def _the_remote_branch(bare: Path, branch: str = BRANCH) -> str | None:
    done = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() or None


def _every_move_of(bare: Path, branch: str = BRANCH) -> list[str]:
    log = bare / "logs" / "refs" / "heads" / branch
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _the_remotes_branches(bare: Path) -> list[str]:
    out = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref", "--format=%(refname)", "refs/heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line.strip())


def _another_hand_makes(bare: Path, root: Path, branch: str, at: str) -> None:
    other = root / f"other-{branch.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    git(other, "push", "-q", "origin", f"{at}:refs/heads/{branch}")


class TestItRefuses:
    @pytest.mark.parametrize(
        "branch, mission",
        [
            ("feature/x", MISSION),
            ("main", MISSION),
            ("mission/m-3FA21C", MISSION),
            ("mission/m-3fa21", MISSION),
            ("mission/m-3fa21c0", MISSION),
            ("mission/m-3fa21c/x", MISSION),
            ("+mission/m-3fa21c", MISSION),
            ("mission/m-aaaaaa", MISSION),  # another mission's branch
        ],
    )
    def test_a_name_that_is_not_this_missions_branch(
        self, world: dict[str, Any], branch: str, mission: str
    ) -> None:
        before = _the_remotes_branches(world["project"]["bare"])
        answer = Publisher(world["settings"]).create_mission_branch(
            _ask(world, branch=branch, mission_id=mission)
        )
        assert answer.created is False
        assert answer.refusal_kind == "not-a-mission-branch-name"
        assert _the_remotes_branches(world["project"]["bare"]) == before

    def test_a_mission_id_that_is_not_one(self, world: dict[str, Any]) -> None:
        answer = Publisher(world["settings"]).create_mission_branch(
            _ask(world, mission_id="mission-1")
        )
        assert answer.created is False
        assert answer.refusal_kind == "the-request-made-no-sense"

    def test_an_existing_branch_it_did_not_make(self, world: dict[str, Any]) -> None:
        """Even at exactly the commit asked for: no record, no "created"."""
        bare = world["project"]["bare"]
        _another_hand_makes(bare, world["root"], BRANCH, world["project"]["g"])
        moves = _every_move_of(bare)

        answer = Publisher(world["settings"]).create_mission_branch(_ask(world))

        assert answer.created is False
        assert answer.refusal_kind == "the-branch-already-exists"
        assert _every_move_of(bare) == moves
        assert TheMissionBranchRecords(Path(world["settings"].state_dir)).read(
            PROJECT, BRANCH
        ) is None

    def test_a_commit_that_is_not_mains_tip(self, world: dict[str, Any]) -> None:
        bare = world["project"]["bare"]
        new_tip = somebody_else_lands_work(bare, world["root"], "other-work")
        before = _the_remotes_branches(bare)

        answer = Publisher(world["settings"]).create_mission_branch(_ask(world))

        assert answer.created is False
        assert answer.refusal_kind == "not-the-default-branch-tip"
        assert answer.remote_tip == new_tip
        assert _the_remotes_branches(bare) == before
        assert _the_remote_branch(bare) is None

    def test_a_commit_the_remote_does_not_have_at_all(self, world: dict[str, Any]) -> None:
        answer = Publisher(world["settings"]).create_mission_branch(
            _ask(world, commit=world["project"]["j"])
        )
        assert answer.created is False
        assert answer.refusal_kind == "not-the-default-branch-tip"
        assert _the_remote_branch(world["project"]["bare"]) is None

    def test_a_project_it_was_not_told_about(self, world: dict[str, Any]) -> None:
        answer = Publisher(world["settings"]).create_mission_branch(
            _ask(world, project="somebody/else")
        )
        assert answer.created is False
        assert answer.refusal_kind == "the-project-is-not-one-of-mine"


class TestItCreates:
    def test_at_mains_tip_and_read_back(self, world: dict[str, Any]) -> None:
        bare = world["project"]["bare"]
        g = world["project"]["g"]

        answer = Publisher(world["settings"]).create_mission_branch(_ask(world))

        assert answer.created is True
        assert answer.already_there is False
        assert answer.remote_tip == g
        assert answer.refusal is None
        assert _the_remote_branch(bare) == g
        assert len(_every_move_of(bare)) == 1
        record = TheMissionBranchRecords(Path(world["settings"].state_dir)).read(
            PROJECT, BRANCH
        )
        assert record is not None
        assert (record.mission_id, record.commit, record.state) == (MISSION, g, CONFIRMED)

    def test_the_record_is_written_before_the_push(self, world: dict[str, Any]) -> None:
        publisher = Publisher(world["settings"])
        real = publisher._commits_for  # noqa: SLF001
        seen: dict[str, Any] = {}
        records = TheMissionBranchRecords(Path(world["settings"].state_dir))

        class _Looks:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                seen["record"] = records.read(PROJECT, branch)
                return self._inner.send(commit, branch)

        publisher._commits_for = lambda route: _Looks(real(route))  # noqa: SLF001
        assert publisher.create_mission_branch(_ask(world)).created is True
        assert seen["record"] is not None
        assert seen["record"].state == INTENDED
        assert seen["record"].commit == world["project"]["g"]


class TestItReadsTheRemoteBack:
    def test_a_send_that_says_it_worked_and_pushed_nothing(
        self, world: dict[str, Any]
    ) -> None:
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        real = publisher._commits_for  # noqa: SLF001

        class _SaysItWorked:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                return git_work.GitSaid(ok=True)

        publisher._commits_for = lambda route: _SaysItWorked(real(route))  # noqa: SLF001

        answer = publisher.create_mission_branch(_ask(world))

        assert answer.created is False
        assert answer.refusal_kind == "the-remote-does-not-have-it"
        assert _the_remote_branch(bare) is None
        record = TheMissionBranchRecords(Path(world["settings"].state_dir)).read(
            PROJECT, BRANCH
        )
        assert record is not None
        assert record.state == INTENDED  # never "confirmed"


    def test_a_push_that_says_it_failed_and_landed(self, world: dict[str, Any]) -> None:
        """The reply was lost: the remote has the branch, so it was created."""
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        real = publisher._commits_for  # noqa: SLF001

        class _ItsReplyWasLost:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                self._inner.send(commit, branch)
                return git_work.GitSaid(ok=False, said="git push ended with exit code 128")

        publisher._commits_for = lambda route: _ItsReplyWasLost(real(route))  # noqa: SLF001

        answer = publisher.create_mission_branch(_ask(world))

        assert answer.created is True
        assert answer.remote_tip == world["project"]["g"]
        assert _the_remote_branch(bare) == world["project"]["g"]
        record = TheMissionBranchRecords(Path(world["settings"].state_dir)).read(
            PROJECT, BRANCH
        )
        assert record is not None and record.state == CONFIRMED

    def test_a_push_that_failed_and_did_not_land_is_refused(
        self, world: dict[str, Any]
    ) -> None:
        publisher = Publisher(world["settings"])
        real = publisher._commits_for  # noqa: SLF001

        class _Refused:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                return git_work.GitSaid(ok=False, said="git push ended with exit code 1")

        publisher._commits_for = lambda route: _Refused(real(route))  # noqa: SLF001

        answer = publisher.create_mission_branch(_ask(world))

        assert answer.created is False
        assert answer.refusal_kind == "the-remote-refused-the-branch"
        assert _the_remote_branch(world["project"]["bare"]) is None


    def test_a_push_that_failed_and_a_read_back_that_failed_is_not_known(
        self, world: dict[str, Any]
    ) -> None:
        """The network went for both: the answer says ask again, not stop."""
        publisher = Publisher(world["settings"])
        real = publisher._commits_for  # noqa: SLF001

        class _TheNetworkWent:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self._pushed = False

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                self._pushed = True
                return git_work.GitSaid(ok=False, said="git push ended with exit code 128")

            def what_the_remote_lists(self, branch: str) -> Any:
                if self._pushed:
                    return git_work.TheRemotesList(ok=False, said="the remote could not be listed")
                return self._inner.what_the_remote_lists(branch)

        publisher._commits_for = lambda route: _TheNetworkWent(real(route))  # noqa: SLF001

        answer = publisher.create_mission_branch(_ask(world))

        assert answer.created is False
        assert answer.refusal_kind == "the-remote-could-not-be-read-back"
        assert answer.refusal_kind in publisher_client._OUTCOME_NOT_KNOWN  # noqa: SLF001


class TestItRecovers:
    def test_a_lost_reply_then_the_same_request(self, world: dict[str, Any]) -> None:
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True

        again = publisher.create_mission_branch(_ask(world))

        assert again.created is True
        assert again.already_there is True
        assert again.remote_tip == world["project"]["g"]
        assert len(_every_move_of(bare)) == 1  # no second push

    def test_a_restart_after_the_push_and_before_the_answer(
        self, world: dict[str, Any]
    ) -> None:
        bare = world["project"]["bare"]
        first = Publisher(world["settings"])
        real = first._commits_for  # noqa: SLF001

        class _ThePowerWent:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def send(self, commit: str, branch: str) -> Any:
                self._inner.send(commit, branch)
                raise RuntimeError("the power went")

        first._commits_for = lambda route: _ThePowerWent(real(route))  # noqa: SLF001
        lost = first.create_mission_branch(_ask(world))
        assert lost.created is False
        assert lost.refusal_kind == "the-publisher-could-not-finish"
        assert _the_remote_branch(bare) == world["project"]["g"]

        # A new process: only the folder on disk carries over.
        restarted = Publisher(world["settings"])
        record = TheMissionBranchRecords(Path(world["settings"].state_dir)).read(
            PROJECT, BRANCH
        )
        assert record is not None and record.state == INTENDED

        again = restarted.create_mission_branch(_ask(world))

        assert again.created is True
        assert again.already_there is True
        assert len(_every_move_of(bare)) == 1

    def test_main_moving_on_after_creation_does_not_matter(
        self, world: dict[str, Any]
    ) -> None:
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True
        somebody_else_lands_work(bare, world["root"], "later-work")

        again = publisher.create_mission_branch(_ask(world))

        assert again.created is True and again.already_there is True
        assert len(_every_move_of(bare)) == 1

    def test_its_own_branch_that_has_moved_is_refused(self, world: dict[str, Any]) -> None:
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True
        landed = somebody_else_lands_work(bare, world["root"], "mission-work", branch="main")
        _another_hand_moves = world["root"] / "mover"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(_another_hand_moves)],
            check=True,
            capture_output=True,
        )
        git(_another_hand_moves, "push", "-q", "origin", f"{landed}:refs/heads/{BRANCH}")

        again = publisher.create_mission_branch(_ask(world))

        assert again.created is False
        assert again.refusal_kind == "the-branch-already-exists"
        assert again.remote_tip == landed

    def test_a_record_for_another_commit_is_not_this_request(
        self, world: dict[str, Any]
    ) -> None:
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True

        other = publisher.create_mission_branch(_ask(world, commit="a" * 40))

        assert other.created is False
        assert other.refusal_kind == "the-branch-already-exists"
        assert len(_every_move_of(bare)) == 1

    def test_its_record_at_one_commit_does_not_vouch_for_another(
        self, world: dict[str, Any]
    ) -> None:
        """Record at X, another hand moves the branch to Y, a request at Y."""
        bare = world["project"]["bare"]
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True
        y = somebody_else_lands_work(bare, world["root"], "y-work", branch="main")
        mover = world["root"] / "mover-y"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(mover)], check=True, capture_output=True
        )
        git(mover, "push", "-q", "origin", f"{y}:refs/heads/{BRANCH}")
        assert _the_remote_branch(bare) == y

        at_y = publisher.create_mission_branch(_ask(world, commit=y))

        assert at_y.created is False
        assert at_y.refusal_kind == "the-branch-already-exists"
        assert at_y.remote_tip == y

    def test_an_unreadable_record_is_refused(self, world: dict[str, Any]) -> None:
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True
        records = TheMissionBranchRecords(Path(world["settings"].state_dir))
        records.where(PROJECT, BRANCH).write_text("{half a rec", encoding="utf-8")

        again = publisher.create_mission_branch(_ask(world))

        assert again.created is False
        assert again.refusal_kind == "the-mission-record-could-not-be-read"


class TestItNeverForces:
    def test_every_push_it_makes_is_the_one_plain_push(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []
        real = git_work.run_network_git

        def watched(args: Any, **kwargs: Any) -> Any:
            commands.append([str(a) for a in args])
            return real(args, **kwargs)

        monkeypatch.setattr(git_work, "run_network_git", watched)
        publisher = Publisher(world["settings"])
        assert publisher.create_mission_branch(_ask(world)).created is True
        assert publisher.create_mission_branch(_ask(world)).created is True

        pushes = [c for c in commands if c and c[0] == "push"]
        assert pushes == [
            ["push", str(world["project"]["bare"]), f"{world['project']['g']}:refs/heads/{BRANCH}"]
        ]
        for command in commands:
            assert not any(word.startswith("--force") or word == "-f" for word in command)
        assert not pushes[0][2].startswith("+")


class _Config:
    def __init__(self, url: str | None, timeout: int = 30) -> None:
        self.publication = type(
            "publication",
            (),
            {"publisher_url": url, "request_timeout_seconds": timeout},
        )()


@pytest.fixture()
def running(world: dict[str, Any]):
    server, _thread = serve(world["settings"], publisher=Publisher(world["settings"]))
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield {**world, "url": f"http://{host}:{port}", "server": server}
    finally:
        server.shutdown()
        server.server_close()


class TestTheCoordinatorAsksOverTheWire:
    def test_the_two_sides_name_the_same_route(self) -> None:
        assert publisher_client.MISSION_BRANCH_ROUTE == MISSION_BRANCH_ROUTE
        assert MISSION_BRANCH_ROUTE == "/create-mission-branch"
        assert the_service.PUBLISH_ROUTE == "/publish"

    def test_created_then_asked_again(self, running: dict[str, Any]) -> None:
        g = running["project"]["g"]

        async def twice() -> list[Any]:
            out = []
            for _ in range(2):
                out.append(
                    await publisher_client.ask_the_publisher_to_create_mission_branch(
                        _Config(running["url"]),
                        project=PROJECT,
                        mission_id=MISSION,
                        branch=BRANCH,
                        commit=g,
                    )
                )
            return out

        first, second = asyncio.run(twice())
        assert first.outcome == "created" and first.created
        assert first.already_there is False and first.remote_tip == g
        assert second.outcome == "created" and second.already_there is True
        assert second.created
        assert len(_every_move_of(running["project"]["bare"])) == 1

    def test_a_refusal_comes_back_whole(self, running: dict[str, Any]) -> None:
        answer = asyncio.run(
            publisher_client.ask_the_publisher_to_create_mission_branch(
                _Config(running["url"]),
                project=PROJECT,
                mission_id=MISSION,
                branch="feature/not-a-mission",
                commit=running["project"]["g"],
            )
        )
        assert answer.outcome == "refused"
        assert answer.created is False
        assert answer.refusal_kind == "not-a-mission-branch-name"
        assert answer.refusal

    def test_no_publisher_and_a_dead_one_are_unreachable(
        self, world: dict[str, Any]
    ) -> None:
        for config in (_Config(None), _Config("http://127.0.0.1:9", timeout=2)):
            answer = asyncio.run(
                publisher_client.ask_the_publisher_to_create_mission_branch(
                    config,
                    project=PROJECT,
                    mission_id=MISSION,
                    branch=BRANCH,
                    commit=world["project"]["g"],
                )
            )
            assert answer.outcome == "unreachable"
            assert answer.created is False
            assert "127.0.0.1" not in str(answer.refusal)

    def test_a_created_without_the_commit_is_not_taken_on_trust(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = {"created": True, "remote_tip": "b" * 40, "refusal": None}
        monkeypatch.setattr(
            publisher_client, "_mission_branch_post", lambda *_a, **_k: (said, "")
        )
        answer = asyncio.run(
            publisher_client.ask_the_publisher_to_create_mission_branch(
                _Config("http://127.0.0.1:1"),
                project=PROJECT,
                mission_id=MISSION,
                branch=BRANCH,
                commit="a" * 40,
            )
        )
        assert answer.outcome == "refused"
        assert answer.created is False
        assert answer.refusal_kind == "the-answer-did-not-match"

    @pytest.mark.parametrize(
        "said",
        [
            {"created": False, "refusal": "stopped", "refusal_kind": "the-publisher-could-not-finish"},
            {"created": False, "refusal": "pushed, then", "refusal_kind": "the-remote-could-not-be-read-back"},
        ],
    )
    def test_an_answer_that_does_not_know_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, said: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(
            publisher_client, "_mission_branch_post", lambda *_a, **_k: (said, "")
        )
        answer = asyncio.run(
            publisher_client.ask_the_publisher_to_create_mission_branch(
                _Config("http://127.0.0.1:1"),
                project=PROJECT,
                mission_id=MISSION,
                branch=BRANCH,
                commit="a" * 40,
            )
        )
        assert answer.outcome == "unreachable"
        assert answer.created is False
        assert answer.refusal_kind == said["refusal_kind"]

    def test_a_server_error_is_unreachable(self, running: dict[str, Any]) -> None:
        def explodes(_request: Any) -> Any:
            raise RuntimeError("the disk went away")

        # The handler's own last resort: an error escaping the publisher.
        running["server"].publisher.create_mission_branch = explodes
        answer = asyncio.run(
            publisher_client.ask_the_publisher_to_create_mission_branch(
                _Config(running["url"]),
                project=PROJECT,
                mission_id=MISSION,
                branch=BRANCH,
                commit=running["project"]["g"],
            )
        )
        assert answer.outcome == "unreachable"
        assert answer.created is False
        assert "500" in str(answer.refusal)

    def test_an_older_publisher_without_the_route_is_refused(self) -> None:
        """A stub answering as an older release does: 404 and no answer."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class _AnOlderPublisher(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.dumps({"error": f"no such path: {self.path}"}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        stub = ThreadingHTTPServer(("127.0.0.1", 0), _AnOlderPublisher)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        try:
            answer = asyncio.run(
                publisher_client.ask_the_publisher_to_create_mission_branch(
                    _Config(f"http://127.0.0.1:{stub.server_address[1]}"),
                    project=PROJECT,
                    mission_id=MISSION,
                    branch=BRANCH,
                    commit="a" * 40,
                )
            )
        finally:
            stub.shutdown()
            stub.server_close()
        assert answer.outcome == "refused"
        assert answer.created is False
        assert (
            answer.refusal_kind
            == publisher_client.THE_PUBLISHER_CANNOT_CREATE_MISSION_BRANCHES
        )
        assert "does not support creating mission branches" in str(answer.refusal)
        assert "127.0.0.1" not in str(answer.refusal)

    def test_the_wire_answer_carries_no_credential(self, running: dict[str, Any]) -> None:
        import urllib.request

        from tests.forge.publisher.a_project_and_a_ledger import THE_MADE_UP_CREDENTIAL

        post = urllib.request.Request(
            running["url"] + MISSION_BRANCH_ROUTE,
            data=json.dumps(_ask(running)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post, timeout=30) as said:
            raw = said.read().decode("utf-8")
        assert json.loads(raw)["created"] is True
        assert THE_MADE_UP_CREDENTIAL not in raw
        stored = TheMissionBranchRecords(Path(running["settings"].state_dir)).where(
            PROJECT, BRANCH
        )
        assert THE_MADE_UP_CREDENTIAL not in stored.read_text(encoding="utf-8")

    def test_a_send_with_no_kind_is_still_a_send(self, running: dict[str, Any]) -> None:
        """The send route is untouched: it still answers in its own shape."""
        from tests.forge.publisher.a_project_and_a_ledger import a_request

        answer = asyncio.run(
            publisher_client.ask_the_publisher(
                _Config(running["url"]), a_request(running["project"])
            )
        )
        assert answer["published"] is True
        assert "created" not in answer
