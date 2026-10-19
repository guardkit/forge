"""The helper's ``/healthz`` names its process instance (stage S2).

A random id made once when the helper process starts: two starts give two
ids, so a recreated helper (a VM restart recreates it) is told apart, and the
sandbox starter binds a deployment's readiness to it. The answer still says
"healthy", which is what the sandbox's start-up script checks for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from forge.config.models import ForgeConfig
from forge.deploy_sidecar import service
from forge.deploy_sidecar.service import build_server

SRC = Path(__file__).resolve().parents[3] / "src"


def _instance_of_a_new_helper_process() -> str:
    done = subprocess.run(
        [sys.executable, "-c", "from forge.deploy_sidecar.service import HELPER_INSTANCE; print(HELPER_INSTANCE)"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=120,
        check=True,
    )
    return done.stdout.strip()


def test_two_helper_starts_give_different_ids() -> None:
    first = _instance_of_a_new_helper_process()
    second = _instance_of_a_new_helper_process()
    assert len(first) == 32 and len(second) == 32
    assert first != second


def test_healthz_reports_the_instance_and_still_says_healthy() -> None:
    server = build_server(
        port=0,
        config_loader=lambda: ForgeConfig.model_validate({"permissions": {"filesystem": {"allowlist": ["/tmp"]}}}),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as answer:
            text = answer.read().decode("utf-8")
        body = json.loads(text)
        # The start-up script's check: the answer contains "healthy".
        assert '"healthy"' in text
        assert body["status"] == "healthy"
        assert body["rev"] == service.SIDECAR_CODE_VERSION
        assert body["instance"] == service.HELPER_INSTANCE
        # The same process answers with the same id every time.
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as again:
            assert json.loads(again.read())["instance"] == body["instance"]
    finally:
        server.shutdown()
        server.server_close()
