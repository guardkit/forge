"""Planning dispatch for a project with a sandbox (28 October 2026).

A run whose repository has a sandbox sends each planning step to that
sandbox's helper (``POST /planning-step``) instead of over the bus; every
other run keeps the bus orchestrator. What this file pins, with a recording
stand-in for the HTTP post (nothing is reached):

* the choice: helper only for a repository in ``planning.sandboxes`` (the run's
  own repository, or the default), the bus otherwise;
* the request: the bus command and arguments unchanged, plus the seat's time
  budget, the role for the verb, and for the spec and plan steps the recorded
  start commit and memory name;
* the answer: the planner's ResultPayload read by the bus path's own parser,
  so the stage result is the same as a bus reply's; a stop at the limit is the
  bus path's ``local_timeout``, and the helper is asked to stop the step.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from forge.adapters.sandbox_planning import (
    PLANNING_STEP_ENDPOINT,
    SandboxPlanningDispatch,
    planning_dispatch_surface,
)
from forge.config.models import ForgeConfig
from forge.dispatch.models import DispatchError, SyncResult
from forge.dispatch.reply_parser import parse_reply

REPO = "example-org/notes-app"
OTHER_REPO = "example-org/no-sandbox"
HELPER = "http://127.0.0.1:19125"
RUN = "0c1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
START = "a" * 40

REPLY = {
    "command": "feature_plan",
    "result": {
        "role_id": "architect",
        "coach_score": 0.9,
        "criterion_breakdown": [
            {"criterion": "fit", "score": 0.9, "weight": 1.0, "rationale": "ok"}
        ],
        "detection_findings": [],
        "role_output": {"feature_id": "FEAT-0C1A", "tasks": [{"id": "T1"}]},
    },
    "correlation_id": "ab" * 16,
    "success": True,
}


def _config(
    sandboxes: dict[str, Any] | None = None, default: str | None = None, in_sandbox: bool = True
) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {REPO: "/srv/notes-app", OTHER_REPO: "/srv/other"},
        "planner_in_sandbox": in_sandbox,
    }
    if sandboxes is not None:
        planning["sandboxes"] = sandboxes
    if default is not None:
        planning["default_target_repo"] = default
    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv"]}}, "planning": planning}
    )


SANDBOXES = {
    REPO: {"name": "notes-app", "sidecar_url": HELPER, "runner_url": "http://127.0.0.1:19126"}
}


class _Store:
    def __init__(self, target_repo: str | None = REPO) -> None:
        self.target_repo = target_repo

    def get_run(self, correlation_id: str) -> dict[str, Any]:
        return {"target_repo": self.target_repo}

    def get_start_point(self, correlation_id: str) -> tuple[str | None, str | None]:
        return START, "main"

    def get_memory_project(self, correlation_id: str) -> str | None:
        return "notes_app"


BUS = object()


class TestTheChoice:
    def test_no_sandboxes_keeps_the_bus(self) -> None:
        surface = planning_dispatch_surface(
            config=_config(), store=_Store(), correlation_id=RUN, bus=BUS,
            reads_project=True,
        )
        assert surface is BUS

    def test_the_switch_off_keeps_the_bus_even_with_a_sandbox(self) -> None:
        surface = planning_dispatch_surface(
            config=_config(SANDBOXES, in_sandbox=False), store=_Store(), correlation_id=RUN,
            bus=BUS, reads_project=True,
        )
        assert surface is BUS
        # And it is off unless forge.yaml says otherwise.
        assert ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv"]}}, "planning": {}}
        ).planning.planner_in_sandbox is False

    def test_a_repository_without_a_sandbox_keeps_the_bus(self) -> None:
        surface = planning_dispatch_surface(
            config=_config(SANDBOXES), store=_Store(OTHER_REPO), correlation_id=RUN,
            bus=BUS, reads_project=True,
        )
        assert surface is BUS

    def test_a_repository_with_a_sandbox_uses_its_helper(self) -> None:
        surface = planning_dispatch_surface(
            config=_config(SANDBOXES), store=_Store(), correlation_id=RUN, bus=BUS,
            reads_project=True,
        )
        assert isinstance(surface, SandboxPlanningDispatch)
        assert (surface.base_url, surface.repo, surface.build) == (HELPER, REPO, RUN)
        assert (surface.start_commit, surface.memory_project) == (START, "notes_app")
        assert surface.timeout_seconds == 3600.0

    def test_the_default_repository_counts_when_the_run_names_none(self) -> None:
        surface = planning_dispatch_surface(
            config=_config(SANDBOXES, default=REPO), store=_Store(None),
            correlation_id=RUN, bus=BUS, reads_project=False,
        )
        assert isinstance(surface, SandboxPlanningDispatch)
        # greenfield: no start commit and no memory are sent.
        assert (surface.start_commit, surface.memory_project) == (None, None)


class _Poster:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def __call__(self, url: str, body: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((url, body, timeout))
        if isinstance(self.answer, BaseException) and url.endswith(PLANNING_STEP_ENDPOINT):
            raise self.answer
        if url.endswith("/guardkit-leg/cancel"):
            return 200, {"cancelled": []}
        return self.answer


def _surface(poster: _Poster, **kwargs: Any) -> SandboxPlanningDispatch:
    kwargs.setdefault("start_commit", START)
    kwargs.setdefault("memory_project", "notes_app")
    return SandboxPlanningDispatch(
        base_url=HELPER, repo=REPO, build=RUN, post=poster, **kwargs
    )


def _dispatch(surface: SandboxPlanningDispatch, command: str = "feature_plan") -> Any:
    return asyncio.run(
        surface.dispatch(
            capability="architecture",
            parameters=[],
            build_id=RUN,
            stage_label="feature_plan",
            command=command,
            command_args={"feature_id": "FEAT-0C1A", "spec_feature": "Feature: x"},
        )
    )


class TestTheRequest:
    def test_the_bus_command_with_the_seat_budget_and_whose_work_it_is(self) -> None:
        poster = _Poster((200, {"result": REPLY, "exit_code": 0, "timed_out": False}))
        _dispatch(_surface(poster))
        [(url, body, timeout)] = poster.calls
        assert url == f"{HELPER}/planning-step"
        assert timeout == 3600.0 + 30.0
        request = body.pop("request")
        assert body == {
            "repo": REPO,
            "build": RUN,
            "role": "architect",
            "timeout": 3600.0,
            "start_commit": START,
            "declared_at": START,
            "memory_project": "notes_app",
        }
        assert request["command"] == "feature_plan"
        assert request["args"] == {
            "feature_id": "FEAT-0C1A",
            "spec_feature": "Feature: x",
            "time_budget_seconds": 3480,
        }
        assert len(request["correlation_id"]) == 32

    @pytest.mark.parametrize(
        ("command", "role"),
        [("greenfield", "product-owner"), ("feature_spec", "product-owner")],
    )
    def test_the_product_owner_answers_its_two_verbs(self, command: str, role: str) -> None:
        poster = _Poster((200, {"result": REPLY}))
        _dispatch(_surface(poster, start_commit=None, memory_project=None), command)
        [(_url, body, _timeout)] = poster.calls
        assert body["role"] == role
        assert "start_commit" not in body and "memory_project" not in body


class TestTheAnswer:
    def test_the_reply_is_parsed_exactly_as_a_bus_reply(self) -> None:
        poster = _Poster((200, {"result": REPLY, "exit_code": 0, "timed_out": False}))
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, SyncResult)
        bus = parse_reply(REPLY, resolution_id=outcome.resolution_id, attempt_no=1)
        assert outcome == bus

    def test_a_failure_reply_is_the_planners_own_error(self) -> None:
        failed = {**REPLY, "success": False, "result": {"error": "no spec given"}}
        outcome = _dispatch(_surface(_Poster((200, {"result": failed}))))
        bus = parse_reply(failed, resolution_id=outcome.resolution_id, attempt_no=1)
        assert outcome == bus
        assert isinstance(outcome, DispatchError)

    def test_a_stop_at_the_limit_is_a_local_timeout(self) -> None:
        poster = _Poster((200, {"result": None, "timed_out": True}))
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, DispatchError)
        assert outcome.error_explanation == "local_timeout"

    def test_no_answer_in_time_is_a_local_timeout_and_the_step_is_stopped(self) -> None:
        poster = _Poster(TimeoutError("timed out"))
        outcome = _dispatch(_surface(poster))
        assert outcome.error_explanation == "local_timeout"
        assert poster.calls[-1][0] == f"{HELPER}/guardkit-leg/cancel"
        assert poster.calls[-1][1] == {"build": RUN}

    def test_a_refusal_is_said(self) -> None:
        poster = _Poster((500, {"error": "planner image not configured: ..."}))
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, DispatchError)
        assert "planner image not configured" in outcome.error_explanation
        assert "HTTP 500" in outcome.error_explanation

    def test_no_result_is_said(self) -> None:
        poster = _Poster(
            (200, {"result": None, "error": "the planning step exited 1 and wrote no result.json"})
        )
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, DispatchError)
        assert "wrote no result.json" in outcome.error_explanation

    def test_a_cancelled_step_is_not_a_result(self) -> None:
        poster = _Poster((200, {"result": None, "cancelled": True}))
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, DispatchError)
        assert "cancelled" in outcome.error_explanation

    def test_an_unreachable_helper_is_said(self) -> None:
        poster = _Poster(ConnectionRefusedError("refused"))
        outcome = _dispatch(_surface(poster))
        assert isinstance(outcome, DispatchError)
        assert outcome.error_explanation != "local_timeout"
        assert HELPER in outcome.error_explanation
