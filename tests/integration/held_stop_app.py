"""The runner's real stop route, for a runner whose build cannot be stopped yet.

Test scaffolding for ``test_build_stop_runners``. The design's cross-runner
check needs a build whose processes cannot be stopped *yet*. A process kept
alive from outside the build (the test re-starts one carrying the build's
owner marker every time it dies) races the stop's own read of ``/proc``, so on
its own it would make the check flaky rather than prove anything. So while the
file named by ``FORGE_TEST_HOLD_FILE`` names the build, this wrapper runs the
real stop and then, after a moment for the re-start, answers with the real
stateless check — which then sees the re-started process. Once the file is
gone the answer is exactly the real route's.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from forge import build_processes
from forge.subagents.runner_http import STOP_ROUTE

_HOLD = Path(os.environ.get("FORGE_TEST_HOLD_FILE", "/nonexistent"))


def _held(build_id: str) -> bool:
    try:
        return _HOLD.read_text().strip() == build_id
    except OSError:
        return False


async def _stop(request: Request) -> JSONResponse:
    build_id = str(request.path_params["build_id"])
    report = await build_processes.stop_build(build_id)
    if _held(build_id):
        await asyncio.sleep(0.5)
        report = await build_processes.still_running(build_id)
    return JSONResponse(report.as_json())


app = Starlette(routes=[Route(STOP_ROUTE, _stop, methods=["POST"])])
