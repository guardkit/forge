"""The coordinator's read-only answer, stood in for, on loopback.

The helper the factory sends work to reads a project's own declaration files
at a commit, and it will not take that commit on a request's own word: the
request names the BUILD, and the helper asks the coordinator what commit IT
recorded that build as starting from. So any test that drives a real request
through a real helper route needs something to answer that question.

This is that something: a child of the test process on 127.0.0.1, on a port
the kernel picks, answering out of a plain mapping the test writes. No real
coordinator, ledger, service or network is anywhere near it — the address is
put in this process's own environment with ``monkeypatch`` and taken out
again when the test ends.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from forge.deploy_sidecar.service import COORDINATOR_OWNER_ENV

__all__ = ["a_coordinator_that_recorded", "COORDINATOR_OWNER_ENV"]


class _TheCoordinatorsAnswer(BaseHTTPRequestHandler):
    """One question, answered read-only: what does this build start from?"""

    records: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — the base class's spelling
        from urllib.parse import parse_qs, urlparse

        asked = parse_qs(urlparse(self.path).query)
        build = (asked.get("build") or [""])[0]
        answer: dict[str, Any] = {}
        if build in self.records:
            answer = {"build": build, "start_commit": self.records[build]}
        payload = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # noqa: ANN401 — quiet in tests
        return


@contextlib.contextmanager
def a_coordinator_that_recorded(
    records: dict[str, str], monkeypatch: pytest.MonkeyPatch
):
    """Point the helper at a stand-in coordinator holding ``records``.

    ``records`` maps a build id to the commit this stand-in says the
    coordinator recorded it as starting from. A build that is not in the
    mapping gets the empty answer, which is how "the coordinator said
    nothing" is driven.
    """
    handler = type("_Answer", (_TheCoordinatorsAnswer,), {"records": dict(records)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    monkeypatch.setenv(
        COORDINATOR_OWNER_ENV, f"http://{host}:{port}/what-did-you-record"
    )
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
