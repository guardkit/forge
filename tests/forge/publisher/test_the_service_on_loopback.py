"""The publisher as a service, and the wire the coordinator talks to it over.

The service is bound to 127.0.0.1 on a port the kernel picks, inside this
test's own process, and shut down at the end of it. Nothing is started as a
daemon, no image is built, and the "remote" is a bare repository on disk.

WHAT IS PROVEN HERE: that the two sides agree. The coordinator's client
(:mod:`forge.pipeline.publisher_client`) posts a request the publisher
understands, reads back the answer shape the merge word branches on, and
turns every way of not getting one into the same honest "not published, and
here is why".
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline import publisher_client
from forge.publisher import service as the_service
from forge.publisher.service import PUBLISH_ROUTE, Publisher, serve
from tests.forge.publisher.a_project_and_a_ledger import (
    THE_MADE_UP_CREDENTIAL,
    a_request,
    every_push_the_remote_saw,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)


class _Config:
    def __init__(self, url: str | None, timeout: int = 30) -> None:
        self.publication = type(
            "publication",
            (),
            {"publisher_url": url, "request_timeout_seconds": timeout},
        )()


@pytest.fixture()
def running(tmp_path: Path):
    root = tmp_path / "world"
    project = make_the_project(root)
    make_the_ledger(root / "forge.db", project=project)
    settings = settings_for(root, project, ledger=root / "forge.db")
    server, _thread = serve(settings, publisher=Publisher(settings))
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield {
            "project": project,
            "settings": settings,
            "url": f"http://{host}:{port}",
            "port": port,
        }
    finally:
        server.shutdown()
        server.server_close()


class TestItIsOnLoopbackAndAnswers:
    def test_it_says_it_is_alive(self, running: dict) -> None:
        with urllib.request.urlopen(running["url"] + "/healthz", timeout=10) as said:
            assert json.loads(said.read().decode("utf-8"))["status"] == "healthy"

    def test_the_kernel_picked_the_port_and_it_is_loopback(
        self, running: dict
    ) -> None:
        assert running["url"].startswith("http://127.0.0.1:")
        assert running["port"] > 0

    def test_any_other_path_is_not_there(self, running: dict) -> None:
        post = urllib.request.Request(
            running["url"] + "/deploy",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(post, timeout=10)
        assert raised.value.code == 404

    def test_there_is_one_route_that_does_anything(self) -> None:
        assert PUBLISH_ROUTE == "/publish"
        source = Path(the_service.__file__).read_text(encoding="utf-8")
        # No route that deploys, no route that writes the ledger.
        assert "/deploy" not in source
        assert "INSERT" not in source.upper()
        assert "UPDATE " not in source.upper()


class TestTheCoordinatorAsksOverTheWire:
    @pytest.mark.asyncio
    async def test_it_publishes_and_the_remote_has_it(self, running: dict) -> None:
        answer = await publisher_client.ask_the_publisher(
            _Config(running["url"]), a_request(running["project"])
        )

        assert answer["published"] is True
        assert answer["contains_j"] is True
        assert answer["remote_now"] == running["project"]["j"]
        assert what_the_remote_has(running["project"]["bare"]) == (
            running["project"]["j"]
        )
        assert len(every_push_the_remote_saw(running["project"]["bare"])) == 1

    @pytest.mark.asyncio
    async def test_a_refusal_comes_back_whole(self, running: dict) -> None:
        asked = a_request(running["project"])
        asked["turn"] = 77
        answer = await publisher_client.ask_the_publisher(_Config(running["url"]), asked)

        assert answer["published"] is False
        assert answer["refusal_kind"] == "the-turn-has-moved-on"
        assert publisher_client.the_remote_moved(answer) is False
        assert every_push_the_remote_saw(running["project"]["bare"]) == []

    @pytest.mark.asyncio
    async def test_nothing_on_the_wire_carries_a_credential(
        self, running: dict
    ) -> None:
        asked = a_request(running["project"])
        answer = await publisher_client.ask_the_publisher(_Config(running["url"]), asked)
        assert THE_MADE_UP_CREDENTIAL not in json.dumps(asked)
        assert THE_MADE_UP_CREDENTIAL not in json.dumps(answer)

    @pytest.mark.asyncio
    async def test_no_publisher_configured(self, running: dict) -> None:
        answer = await publisher_client.ask_the_publisher(
            _Config(None), a_request(running["project"])
        )
        assert answer["published"] is False
        assert answer["refusal_kind"] == publisher_client.THERE_IS_NO_PUBLISHER
        assert "no publisher is configured" in answer["refusal"]

    @pytest.mark.asyncio
    async def test_a_publisher_that_is_not_listening(self, running: dict) -> None:
        answer = await publisher_client.ask_the_publisher(
            _Config("http://127.0.0.1:1"), a_request(running["project"])
        )
        assert answer["published"] is False
        assert answer["refusal_kind"] == (
            publisher_client.PUBLISHER_COULD_NOT_BE_REACHED
        )
        assert "could not be reached" in answer["refusal"]

    @pytest.mark.asyncio
    async def test_a_publisher_that_answers_something_else(
        self, tmp_path: Path
    ) -> None:
        """Anything that is not an answer is "nothing is known to have been sent"."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class _Nonsense(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # noqa: D102
                return

            def do_POST(self) -> None:  # noqa: N802
                body = b"the weather is nice"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Nonsense)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            answer = await publisher_client.ask_the_publisher(
                _Config(f"http://127.0.0.1:{server.server_address[1]}"),
                {"project": "p", "build_id": "b", "turn": 1},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert answer["published"] is False
        assert "not an answer" in answer["refusal"]


class TestTheTwoSidesSpellTheSameWord:
    def test_the_one_refusal_a_new_attempt_answers(self) -> None:
        """The coordinator does not import the publisher, so the word is pinned."""
        assert publisher_client.THE_REMOTE_MOVED == the_service.THE_REMOTE_MOVED
        assert publisher_client.the_remote_moved(
            {"refusal_kind": the_service.THE_REMOTE_MOVED}
        )
        assert the_service.the_remote_moved(
            {"refusal_kind": publisher_client.THE_REMOTE_MOVED}
        )

    def test_the_step_names_the_publisher_reads_off_the_ledger(self) -> None:
        from forge.pipeline import publication_record
        from forge.publisher import the_record

        assert the_record.STEP_MERGE_CHECKS == publication_record.STEP_MERGE_CHECKS
        assert (
            the_record.STEP_CANDIDATE_CHECK == publication_record.STEP_CANDIDATE_CHECK
        )
        assert the_record.LINE_DONE == publication_record.LINE_DONE

    def test_the_coordinator_does_not_import_the_publisher(self) -> None:
        """The whole point of it is that it is a process the coordinator is not."""
        for module in (publisher_client,):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "forge.publisher" not in source

    def test_the_merge_press_does_not_import_the_publisher_either(self) -> None:
        from forge.pipeline import merge_executor

        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        imports = [
            line
            for line in source.splitlines()
            if line.startswith("from ") or line.startswith("import ")
        ]
        assert not any("forge.publisher" in line for line in imports), imports


class TestTheServiceNeverCrashes:
    def test_a_body_that_is_not_json(self, running: dict) -> None:
        post = urllib.request.Request(
            running["url"] + PUBLISH_ROUTE,
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(post, timeout=10)
        said = json.loads(raised.value.read().decode("utf-8"))
        assert said["published"] is False
        assert said["refusal_kind"] == "the-request-made-no-sense"

    def test_a_publisher_whose_git_blows_up_answers_a_refusal(
        self, running: dict
    ) -> None:
        publisher = Publisher(running["settings"])

        def explodes(_route: Any) -> Any:
            raise RuntimeError("the disk went away")

        publisher._commits_for = explodes  # noqa: SLF001
        answer = publisher.publish(a_request(running["project"]))
        assert answer.published is False
        assert answer.refusal_kind == "the-publisher-could-not-finish"

    def test_it_serves_more_than_one_at_a_time(self, running: dict) -> None:
        """Two requests, both answered. The second finds it already there."""

        async def both() -> list[dict[str, Any]]:
            return list(
                await asyncio.gather(
                    publisher_client.ask_the_publisher(
                        _Config(running["url"]), a_request(running["project"])
                    ),
                    publisher_client.ask_the_publisher(
                        _Config(running["url"]), a_request(running["project"])
                    ),
                )
            )

        answers = asyncio.run(both())
        assert all(answer["published"] for answer in answers)
        # AND IT WENT ONCE. The remote's own reflog is the count.
        assert len(every_push_the_remote_saw(running["project"]["bare"])) == 1
