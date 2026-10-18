"""The helper's ``GET /busy`` (one sandbox per project, stage S2, design 2.3a).

The starter asks it immediately before every stop: any request still being
executed, or no answer, refuses the stop. It lists what this process is
executing, from the moment a POST is routed until its answer is written.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import BUSY_ROUTE, SidecarConfigError, build_server


def _get(server: Any, path: str) -> tuple[int, Any]:
    host, port = server.server_address[:2]
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as answer:
        return answer.status, json.loads(answer.read().decode("utf-8"))


def _post(server: Any, path: str, body: dict[str, Any]) -> None:
    host, port = server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except Exception:  # noqa: BLE001 — the answer itself is not what is tested
        pass


def test_an_idle_helper_answers_an_empty_list() -> None:
    server = build_server(port=0, config_loader=lambda: ForgeConfig.model_validate({"permissions": {"filesystem": {"allowlist": ["/tmp"]}}}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = _get(server, BUSY_ROUTE)
        assert status == 200
        assert body["executing"] == [] and body["busy"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_a_request_in_progress_is_listed_until_it_answers() -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow_loader() -> Any:
        entered.set()
        release.wait(10)
        raise SidecarConfigError("no settings in this test")

    server = build_server(port=0, config_loader=slow_loader)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        caller = threading.Thread(
            target=_post,
            args=(server, "/run", {"repo": "example-org/notes-app", "script": "x.sh", "env": {"SECRET": "s"}}),
            daemon=True,
        )
        caller.start()
        assert entered.wait(5)
        status, body = _get(server, BUSY_ROUTE)
        assert status == 200 and body["busy"] is True
        assert [(e["route"], e["repo"]) for e in body["executing"]] == [
            ("/run", "example-org/notes-app")
        ]
        assert "SECRET" not in json.dumps(body)  # only the route and repository
        release.set()
        caller.join(5)
        status, body = _get(server, BUSY_ROUTE)
        assert body["executing"] == []
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_an_unknown_route_is_never_counted() -> None:
    server = build_server(port=0, config_loader=lambda: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _post(server, "/no-such-route", {})
        assert _get(server, BUSY_ROUTE)[1]["executing"] == []
    finally:
        server.shutdown()
        server.server_close()
