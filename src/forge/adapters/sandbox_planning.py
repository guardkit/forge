"""Planning steps for a project with a sandbox, run in that sandbox.

28 October 2026, planning in the sandbox (Rich, 27 October: "option A
please"). Specialist-agent's planner used to run only in the two shared seat
containers on the Dell, reached over the message bus, with no copy of the
project to read. For a project that has a sandbox, each planning step now runs
in that sandbox instead: its helper starts one short-lived container from the
seat image per step (``POST /planning-step``), with a read-only copy of the
project at the planning start commit and the project's own memory door.

This is a MOVE, not a new path. :class:`SandboxPlanningDispatch` is a second
:class:`~forge.pipeline.dispatchers.specialist.SpecialistDispatchSurface`
beside the bus orchestrator: it sends the very command and arguments the bus
orchestrator would publish (plus the ``time_budget_seconds`` the orchestrator
tells the seat), and reads the answer with the very parser a bus reply goes
through, so everything after the answer is unchanged. Projects without a
sandbox keep the bus and the seats exactly as before
(:func:`planning_dispatch_surface` chooses).

The client pattern is the fix-journey leg door's
(:mod:`forge.adapters.guardkit.run_via_sidecar`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import urllib.error
from typing import Any, Callable

from nats_core.events import CommandPayload

from forge.adapters.guardkit.run_via_sidecar import (
    HTTP_TIMEOUT_MARGIN_SECONDS,
    LEG_CANCEL_ENDPOINT,
    LEG_CANCEL_HTTP_TIMEOUT_SECONDS,
    _post,
)
from forge.dispatch.models import DispatchError, DispatchOutcome
from forge.dispatch.orchestrator import SEAT_TIME_BUDGET_ARG
from forge.dispatch.persistence import DispatchParameter
from forge.dispatch.reply_parser import parse_reply
from forge.dispatch.timeout import seat_time_budget_seconds

logger = logging.getLogger(__name__)

#: The helper's route for one planning step.
PLANNING_STEP_ENDPOINT: str = "/planning-step"

#: Which seat answers each verb Forge sends, as on the bus.
PLANNING_ROLE_BY_COMMAND: dict[str, str] = {
    "greenfield": "product-owner",
    "feature_spec": "product-owner",
    "feature_plan": "architect",
}

#: Forge's planning cut-off, the same one the bus orchestrator waits for.
PLANNING_DISPATCH_TIMEOUT_SECONDS: float = 3600.0

#: The explanation the dispatcher reads as a soft timeout, as on the bus.
LOCAL_TIMEOUT: str = "local_timeout"

Poster = Callable[..., tuple[int, Any]]


class SandboxPlanningDispatch:
    """One planning step through the project's sandbox helper.

    Built per step by :func:`planning_dispatch_surface`, with what the run's
    record says: the repository, the run (the build every request names),
    and for the spec and plan steps the start commit and the memory name.
    """

    def __init__(
        self,
        *,
        base_url: str,
        repo: str,
        build: str,
        start_commit: str | None = None,
        memory_project: str | None = None,
        timeout_seconds: float = PLANNING_DISPATCH_TIMEOUT_SECONDS,
        http_timeout_margin: float = HTTP_TIMEOUT_MARGIN_SECONDS,
        post: Poster | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.repo = repo
        self.build = build
        self.start_commit = start_commit
        self.memory_project = memory_project
        self.timeout_seconds = float(timeout_seconds)
        self.http_timeout_margin = float(http_timeout_margin)
        self._post = post or _post

    def request_body(
        self, *, command: str, command_args: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The helper request: the bus command, and whose work it is."""
        args = {
            **(command_args or {}),
            SEAT_TIME_BUDGET_ARG: seat_time_budget_seconds(self.timeout_seconds),
        }
        request = CommandPayload(
            command=command, args=args, correlation_id=secrets.token_hex(16)
        ).model_dump(mode="json")
        body: dict[str, Any] = {
            "repo": self.repo,
            "build": self.build,
            "role": PLANNING_ROLE_BY_COMMAND[command],
            "request": request,
            "timeout": self.timeout_seconds,
        }
        if self.start_commit:
            body["start_commit"] = self.start_commit
            # Where the run's declarations were said: the recorded start, which
            # the helper confirms with the coordinator before using the memory.
            body["declared_at"] = self.start_commit
        if self.memory_project:
            body["memory_project"] = self.memory_project
        return body

    async def dispatch(
        self,
        *,
        capability: str,
        parameters: list[DispatchParameter],
        attempt_no: int = 1,
        retry_of: str | None = None,
        intent_pattern: str | None = None,
        build_id: str = "unknown",
        stage_label: str = "unknown",
        command: str,
        command_args: dict[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Run the step in the sandbox; answer as the bus orchestrator does."""
        resolution_id = f"sandbox-{secrets.token_hex(8)}"

        def error(explanation: str) -> DispatchError:
            return DispatchError(
                resolution_id=resolution_id,
                attempt_no=attempt_no,
                error_explanation=explanation,
            )

        if command not in PLANNING_ROLE_BY_COMMAND:
            return error(
                f"the sandbox helper runs the planning verbs "
                f"{', '.join(PLANNING_ROLE_BY_COMMAND)}; it was asked for {command!r}"
            )
        body = self.request_body(command=command, command_args=command_args)
        endpoint = f"{self.base_url}{PLANNING_STEP_ENDPOINT}"
        logger.info(
            "planning dispatch: %s for %s runs in %s's sandbox (%s)",
            command,
            self.build,
            self.repo,
            self.base_url,
        )
        try:
            status, parsed = await asyncio.to_thread(
                self._post,
                endpoint,
                body,
                timeout=self.timeout_seconds + self.http_timeout_margin,
            )
        except asyncio.CancelledError:
            # The run stopped waiting: stop the step too, without waiting.
            threading.Thread(target=self._cancel, daemon=True).start()
            raise
        except TimeoutError:
            self._cancel()
            return error(LOCAL_TIMEOUT)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                self._cancel()
                return error(LOCAL_TIMEOUT)
            return error(
                f"the sandbox helper at {self.base_url} could not be reached, so "
                f"the {command} step did not run: {exc}"
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return error(
                f"talking to the sandbox helper at {self.base_url} went wrong, so "
                f"the {command} step's result is unknown: {type(exc).__name__}: {exc}"
            )
        if status != 200 or not isinstance(parsed, dict):
            said = parsed.get("error") if isinstance(parsed, dict) else None
            return error(
                f"the sandbox helper did not run the {command} step "
                f"(HTTP {status}): {said or parsed!r}"
            )
        if parsed.get("timed_out") is True:
            return error(LOCAL_TIMEOUT)
        if parsed.get("cancelled") is True:
            return error(f"the {command} step was cancelled in the sandbox")
        result = parsed.get("result")
        if not isinstance(result, dict):
            return error(
                f"the {command} step in the sandbox gave no answer: "
                f"{parsed.get('error') or 'no result'}"
            )
        return parse_reply(result, resolution_id=resolution_id, attempt_no=attempt_no)

    def _cancel(self) -> None:
        """Ask the helper to stop this run's step; log the answer, never raise."""
        try:
            status, parsed = self._post(
                f"{self.base_url}{LEG_CANCEL_ENDPOINT}",
                {"build": self.build},
                timeout=LEG_CANCEL_HTTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — best effort, never mask the end
            logger.warning(
                "planning dispatch: the sandbox helper at %s could not be asked to "
                "stop the step of %s: %s: %s",
                self.base_url,
                self.build,
                type(exc).__name__,
                exc,
            )
            return
        logger.warning(
            "planning dispatch: asked the sandbox helper at %s to stop the step of "
            "%s (HTTP %s): %s",
            self.base_url,
            self.build,
            status,
            parsed,
        )


def planning_dispatch_surface(
    *,
    config: Any,
    store: Any,
    correlation_id: str,
    bus: Any,
    reads_project: bool,
    timeout_seconds: float = PLANNING_DISPATCH_TIMEOUT_SECONDS,
) -> Any:
    """The bus orchestrator, or the project's sandbox helper for this step.

    With ``planning.planner_in_sandbox`` on, a run whose target repository has
    a sandbox (``planning.sandboxes``, the same lookup a fix journey's legs
    use) runs its step there; every other run, and every run while the switch
    is off, gets ``bus`` unchanged. The repository is the run's own, or the default,
    as the planning driver resolves it. ``reads_project`` (the spec and plan
    steps) also sends the run's recorded start commit and memory name.
    """
    planning = getattr(config, "planning", None)
    if not getattr(planning, "planner_in_sandbox", False):
        return bus
    sandboxes = dict(getattr(planning, "sandboxes", None) or {})
    if not sandboxes:
        return bus
    row = store.get_run(correlation_id)
    named = row["target_repo"] if row is not None else None
    repo = named or getattr(planning, "default_target_repo", None)
    entry = sandboxes.get(repo) if repo else None
    if entry is None:
        return bus
    start_commit: str | None = None
    memory_project: str | None = None
    if reads_project:
        recorded, _branch = store.get_start_point(correlation_id)
        start_commit = str(recorded) if recorded else None
        name = store.get_memory_project(correlation_id)
        memory_project = str(name) if name else None
    return SandboxPlanningDispatch(
        base_url=str(entry.sidecar_url),
        repo=str(repo),
        build=str(correlation_id),
        start_commit=start_commit,
        memory_project=memory_project,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "PLANNING_DISPATCH_TIMEOUT_SECONDS",
    "PLANNING_ROLE_BY_COMMAND",
    "PLANNING_STEP_ENDPOINT",
    "SandboxPlanningDispatch",
    "planning_dispatch_surface",
]
