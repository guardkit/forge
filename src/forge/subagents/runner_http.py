"""The build runner's one route of its own: stop a build.

Served beside the runner's graph (``"http": {"app":
"forge.subagents.runner_http:app"}`` in the runner's ``langgraph.json``).

``POST /forge/builds/{build_id}/stop`` stops everything that build owns in
this runner — the processes its child started and anything carrying its owner
marker, then its labelled fixture containers — and answers only when that is
settled:

* ``{"build_id": …, "stopped": true}`` — nothing the build owns is alive here;
* ``{"build_id": …, "stopped": false, "remaining": {…}}`` — something is, or
  the container engine could not be asked; ``remaining`` names it.

When this runner is running the build, its node is told the stop was asked for
and finishes the build as cancelled once its own confirmation is in. When it
is not (a runner restart, or a build long finished), the answer comes from the
owner marker and the fixture label alone, so asking again is always safe — and
the factory asks before it lets a cancelled build's place go (design of
3 October 2026, "When the build's place is released").
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from forge import build_processes

logger = logging.getLogger(__name__)

__all__ = ["STOP_ROUTE", "app"]

#: The route, as the factory's side spells it.
STOP_ROUTE: str = "/forge/builds/{build_id}/stop"


async def _stop(request: Request) -> JSONResponse:
    build_id = str(request.path_params.get("build_id") or "").strip()
    if not build_id:
        return JSONResponse({"error": "a build id is required"}, status_code=400)
    # ``remember=0``: the factory confirming before an acknowledgement — stop
    # whatever is left, but do not cancel a run of this build not yet started.
    remember = request.query_params.get("remember", "1").lower() not in ("0", "false")
    report = await build_processes.stop_build(build_id, remember=remember)
    if report.stopped:
        logger.info("runner stop route: build %s — nothing it owns is alive", build_id)
    else:
        logger.warning(
            "runner stop route: build %s is not yet stopped: %s",
            build_id,
            report.as_json().get("remaining"),
        )
    return JSONResponse(report.as_json())


app = Starlette(routes=[Route(STOP_ROUTE, _stop, methods=["POST"])])
