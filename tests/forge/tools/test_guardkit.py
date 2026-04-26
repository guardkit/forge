"""Unit and seam tests for :mod:`forge.tools.guardkit` (TASK-GCI-009).

The nine ``guardkit_*`` ``@tool`` wrappers are thin shims over
:func:`forge.adapters.guardkit.run.run` (TASK-GCI-008) composed with the
NATS progress subscriber (TASK-GCI-005). These tests verify the
acceptance criteria from the task brief, organised one class per AC
group:

- AC-1 — all nine wrappers exist, are decorated with
  ``@tool(parse_docstring=True)``, and their ``args_schema`` parses
  correctly from the docstring ``Args:`` block.
- AC-2 / AC-3 — every wrapper returns a ``str``; the body is wrapped in
  ``try / except Exception`` so an internal exception becomes the
  ADR-ARCH-025 error envelope rather than being raised.
- AC-4 — every wrapper calls ``run(subcommand=…)`` with the correct
  literal subcommand token (no f-string-built command).
- AC-5 — every wrapper composes the NATS progress subscriber and a
  missing / broken subscriber does not fail the wrapper.
- AC-6 — every wrapper logs a structured line with ``tool_name``,
  ``duration_ms``, and ``status``.
- AC-7 — only ``guardkit_feature_spec`` threads ``context_paths``
  through to ``run(extra_context_paths=…)``.
- AC-8 — on a successful call, the returned JSON contains
  ``artefacts``, ``duration_secs``, and ``coach_score`` (when the
  underlying ``GuardKitResult`` carries one).

The seam under test is ``forge.tools.guardkit.guardkit_run`` — the
re-import of :func:`forge.adapters.guardkit.run.run` inside the
wrappers' module namespace. Every test patches that name so the real
subprocess wrapper is never invoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest import mock

import pytest

from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.tools import guardkit as guardkit_tools

# ---------------------------------------------------------------------------
# Catalogue of the nine wrappers and the kwargs each accepts
# ---------------------------------------------------------------------------

#: Each entry is ``(tool, subcommand, kwargs)`` so the parametrised
#: tests can enumerate every wrapper without copy-paste. Kwargs use
#: representative values so the underlying ``args`` list is non-empty.
ALL_WRAPPERS: list[tuple[Any, str, dict[str, Any]]] = [
    (
        guardkit_tools.guardkit_system_arch,
        "system-arch",
        {"repo": "/abs/repo", "feature_id": "FEAT-001", "scope": "adapters"},
    ),
    (
        guardkit_tools.guardkit_system_design,
        "system-design",
        {
            "repo": "/abs/repo",
            "focus": "event-bus",
            "protocols": ["nats", "grpc"],
        },
    ),
    (
        guardkit_tools.guardkit_system_plan,
        "system-plan",
        {"repo": "/abs/repo", "feature_description": "Add a thing"},
    ),
    (
        guardkit_tools.guardkit_feature_spec,
        "feature-spec",
        {
            "repo": "/abs/repo",
            "feature_description": "Add a thing",
            "context_paths": None,
        },
    ),
    (
        guardkit_tools.guardkit_feature_plan,
        "feature-plan",
        {"repo": "/abs/repo", "feature_id": "FEAT-001"},
    ),
    (
        guardkit_tools.guardkit_task_review,
        "task-review",
        {"repo": "/abs/repo", "task_id": "TASK-001"},
    ),
    (
        guardkit_tools.guardkit_task_work,
        "task-work",
        {"repo": "/abs/repo", "task_id": "TASK-001"},
    ),
    (
        guardkit_tools.guardkit_task_complete,
        "task-complete",
        {"repo": "/abs/repo", "task_id": "TASK-001"},
    ),
    (
        guardkit_tools.guardkit_autobuild,
        "autobuild",
        {"repo": "/abs/repo", "feature_id": "FEAT-001"},
    ),
]


def _make_success_result(subcommand: str) -> GuardKitResult:
    """Build a representative ``status="success"`` result.

    Includes ``artefacts``, ``coach_score``, and ``duration_secs`` so
    AC-8 assertions have non-empty fields to inspect.
    """
    return GuardKitResult(
        status="success",
        subcommand=subcommand,
        artefacts=[f"docs/{subcommand}/output.md"],
        coach_score=0.92,
        duration_secs=4.2,
        stdout_tail="ok",
        stderr=None,
        exit_code=0,
        warnings=[],
    )


def _stub_run(
    *,
    return_result: GuardKitResult | None = None,
    capture: dict[str, Any] | None = None,
    raise_exc: BaseException | None = None,
):
    """Build an async stand-in for :func:`forge.adapters.guardkit.run.run`.

    If ``capture`` is provided the stub records every kwarg it received
    so tests can assert against the literal subcommand and the threaded
    ``extra_context_paths``. When ``raise_exc`` is supplied the stub
    raises it on call — this exercises the wrapper's ``try / except``.
    """

    async def _stub(**kwargs: Any) -> GuardKitResult:
        if capture is not None:
            capture.update(kwargs)
        if raise_exc is not None:
            raise raise_exc
        assert return_result is not None  # for the type checker
        return return_result

    return _stub


# ---------------------------------------------------------------------------
# AC-1 — surface and decorator wiring
# ---------------------------------------------------------------------------


class TestWrapperSurface:
    """AC-1 — every wrapper is exposed and decorated correctly."""

    def test_module_exports_all_nine_wrappers(self) -> None:
        expected = {
            "guardkit_system_arch",
            "guardkit_system_design",
            "guardkit_system_plan",
            "guardkit_feature_spec",
            "guardkit_feature_plan",
            "guardkit_task_review",
            "guardkit_task_work",
            "guardkit_task_complete",
            "guardkit_autobuild",
        }
        assert expected.issubset(set(guardkit_tools.__all__))
        for name in expected:
            assert hasattr(guardkit_tools, name)

    @pytest.mark.parametrize(
        "wrapper,_subcommand,_kwargs", ALL_WRAPPERS, ids=lambda v: getattr(v, "name", "")
    )
    def test_wrapper_is_a_structured_tool(
        self, wrapper: Any, _subcommand: str, _kwargs: dict[str, Any]
    ) -> None:
        # @tool returns a StructuredTool (or BaseTool) — the runtime
        # check we care about is that it has a name and an args_schema.
        assert wrapper.name == wrapper.name  # smoke
        assert wrapper.args_schema is not None
        # parse_docstring=True takes the first paragraph as description.
        assert wrapper.description
        assert "guardkit" in wrapper.description.lower()

    @pytest.mark.parametrize(
        "wrapper,_subcommand,_kwargs", ALL_WRAPPERS, ids=lambda v: getattr(v, "name", "")
    )
    def test_args_schema_includes_repo_param(
        self, wrapper: Any, _subcommand: str, _kwargs: dict[str, Any]
    ) -> None:
        schema = wrapper.args_schema.model_json_schema()
        assert "repo" in schema["properties"]
        assert schema["properties"]["repo"]["type"] == "string"


# ---------------------------------------------------------------------------
# AC-4 — correct subcommand literal threaded into run()
# ---------------------------------------------------------------------------


class TestSubcommandLiteralWiring:
    """AC-4 — each wrapper passes the literal subcommand token to run()."""

    @pytest.mark.parametrize(
        "wrapper,subcommand,kwargs",
        ALL_WRAPPERS,
        ids=[w.name for w, _s, _k in ALL_WRAPPERS],
    )
    @pytest.mark.asyncio()
    async def test_wrapper_calls_run_with_expected_subcommand(
        self,
        wrapper: Any,
        subcommand: str,
        kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(
                return_result=_make_success_result(subcommand),
                capture=capture,
            ),
        )
        result_json = await wrapper.ainvoke(kwargs)

        # The stub captured the call.
        assert capture.get("subcommand") == subcommand
        # repo_path was constructed from the str repo input.
        assert str(capture.get("repo_path")) == kwargs["repo"]
        # The wrapper returned a JSON-encoded GuardKitResult.
        decoded = json.loads(result_json)
        assert decoded["status"] == "success"
        assert decoded["subcommand"] == subcommand


# ---------------------------------------------------------------------------
# AC-2 + AC-8 — success JSON shape
# ---------------------------------------------------------------------------


class TestSuccessJsonShape:
    """AC-2 / AC-8 — return type is str; payload exposes the right keys."""

    @pytest.mark.parametrize(
        "wrapper,subcommand,kwargs",
        ALL_WRAPPERS,
        ids=[w.name for w, _s, _k in ALL_WRAPPERS],
    )
    @pytest.mark.asyncio()
    async def test_success_returns_json_str_with_documented_keys(
        self,
        wrapper: Any,
        subcommand: str,
        kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(return_result=_make_success_result(subcommand)),
        )
        result_json = await wrapper.ainvoke(kwargs)

        # AC-2 — return value is a str.
        assert isinstance(result_json, str)

        decoded = json.loads(result_json)
        # AC-8 — artefacts, duration_secs, coach_score present.
        assert "artefacts" in decoded
        assert decoded["artefacts"], "expected non-empty artefacts list"
        assert "duration_secs" in decoded
        assert decoded["duration_secs"] >= 0
        assert "coach_score" in decoded
        assert decoded["coach_score"] == 0.92


# ---------------------------------------------------------------------------
# AC-2 — failed and timeout pass-through
# ---------------------------------------------------------------------------


class TestFailedAndTimeoutPassThrough:
    """AC-2 — failed and timeout statuses round-trip via JSON, no raise."""

    @pytest.mark.asyncio()
    async def test_failed_status_returns_failed_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failed = GuardKitResult(
            status="failed",
            subcommand="feature-spec",
            artefacts=[],
            duration_secs=1.0,
            stdout_tail="",
            stderr="boom",
            exit_code=1,
            warnings=[
                GuardKitWarning(code="x", message="y", details={}),
            ],
        )
        monkeypatch.setattr(
            guardkit_tools, "guardkit_run", _stub_run(return_result=failed)
        )
        out = await guardkit_tools.guardkit_feature_spec.ainvoke(
            {"repo": "/abs/repo", "feature_description": "x"}
        )
        decoded = json.loads(out)
        assert decoded["status"] == "failed"
        assert decoded["stderr"] == "boom"
        assert decoded["exit_code"] == 1

    @pytest.mark.asyncio()
    async def test_timeout_status_returns_timeout_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timed = GuardKitResult(
            status="timeout",
            subcommand="autobuild",
            artefacts=[],
            duration_secs=600.0,
            stdout_tail="",
            stderr="killed after 600s",
            exit_code=-1,
            warnings=[],
        )
        monkeypatch.setattr(
            guardkit_tools, "guardkit_run", _stub_run(return_result=timed)
        )
        out = await guardkit_tools.guardkit_autobuild.ainvoke(
            {"repo": "/abs/repo", "feature_id": "FEAT-001"}
        )
        decoded = json.loads(out)
        assert decoded["status"] == "timeout"
        assert decoded["duration_secs"] == 600.0


# ---------------------------------------------------------------------------
# AC-3 — internal exception becomes ADR-ARCH-025 envelope, not a raise
# ---------------------------------------------------------------------------


class TestInternalExceptionNeverRaises:
    """AC-3 — try/except in body folds exceptions to the error envelope."""

    @pytest.mark.parametrize(
        "wrapper,_subcommand,kwargs",
        ALL_WRAPPERS,
        ids=[w.name for w, _s, _k in ALL_WRAPPERS],
    )
    @pytest.mark.asyncio()
    async def test_internal_exception_is_wrapped_not_raised(
        self,
        wrapper: Any,
        _subcommand: str,
        kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(raise_exc=RuntimeError("kaboom")),
        )
        result_json = await wrapper.ainvoke(kwargs)
        assert isinstance(result_json, str)
        decoded = json.loads(result_json)
        assert decoded["status"] == "error"
        assert "RuntimeError" in decoded["error"]
        assert "kaboom" in decoded["error"]
        assert decoded["tool"] == wrapper.name

    @pytest.mark.asyncio()
    async def test_error_envelope_uses_json_dumps_for_safe_escaping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exception messages with quotes / backslashes must round-trip
        # through json.loads — the f-string sketch in the task brief
        # would have produced invalid JSON.
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(raise_exc=RuntimeError('he said "boom"\\n')),
        )
        out = await guardkit_tools.guardkit_system_plan.ainvoke(
            {"repo": "/abs/repo", "feature_description": "x"}
        )
        decoded = json.loads(out)  # must not raise
        assert decoded["status"] == "error"


# ---------------------------------------------------------------------------
# AC-5 — progress subscriber is composed; missing client is non-fatal
# ---------------------------------------------------------------------------


class TestProgressSubscriberComposition:
    """AC-5 — subscribe_progress is invoked; broker absence is non-fatal."""

    @pytest.mark.asyncio()
    async def test_subscribe_progress_is_entered_for_each_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entered = {"count": 0, "subcommand": None, "build_id": None}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_subscribe(client, build_id, subcommand, sink):
            entered["count"] += 1
            entered["subcommand"] = subcommand
            entered["build_id"] = build_id
            yield

        monkeypatch.setattr(
            guardkit_tools, "subscribe_progress", _fake_subscribe
        )
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(return_result=_make_success_result("feature-spec")),
        )
        await guardkit_tools.guardkit_feature_spec.ainvoke(
            {"repo": "/abs/repo", "feature_description": "x"}
        )
        assert entered["count"] == 1
        assert entered["subcommand"] == "feature-spec"
        assert entered["build_id"] is not None

    @pytest.mark.asyncio()
    async def test_missing_subscriber_does_not_fail_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real subscribe_progress with a None client is the production
        # "broker unavailable" path; the wrapper must still return the
        # authoritative GuardKitResult.
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(return_result=_make_success_result("feature-spec")),
        )
        out = await guardkit_tools.guardkit_feature_spec.ainvoke(
            {"repo": "/abs/repo", "feature_description": "x"}
        )
        decoded = json.loads(out)
        assert decoded["status"] == "success"


# ---------------------------------------------------------------------------
# AC-6 — structured logging with tool_name, duration_ms, status
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    """AC-6 — one structured log line per call."""

    @pytest.mark.asyncio()
    async def test_success_call_emits_log_with_tool_name_status_duration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(return_result=_make_success_result("system-plan")),
        )
        with caplog.at_level(logging.INFO, logger=guardkit_tools.__name__):
            await guardkit_tools.guardkit_system_plan.ainvoke(
                {"repo": "/abs/repo", "feature_description": "x"}
            )
        joined = " ".join(record.message for record in caplog.records)
        assert "tool_name=guardkit_system_plan" in joined
        assert "status=success" in joined
        assert "duration_ms=" in joined

    @pytest.mark.asyncio()
    async def test_error_call_emits_log_with_status_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(raise_exc=RuntimeError("boom")),
        )
        with caplog.at_level(logging.INFO, logger=guardkit_tools.__name__):
            await guardkit_tools.guardkit_system_plan.ainvoke(
                {"repo": "/abs/repo", "feature_description": "x"}
            )
        joined = " ".join(record.message for record in caplog.records)
        assert "tool_name=guardkit_system_plan" in joined
        assert "status=error" in joined


# ---------------------------------------------------------------------------
# AC-7 — context_paths threaded through only for guardkit_feature_spec
# ---------------------------------------------------------------------------


class TestContextPathsThreading:
    """AC-7 — only guardkit_feature_spec accepts and threads context_paths."""

    @pytest.mark.asyncio()
    async def test_feature_spec_threads_context_paths_to_extra_context_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(
                return_result=_make_success_result("feature-spec"),
                capture=capture,
            ),
        )
        await guardkit_tools.guardkit_feature_spec.ainvoke(
            {
                "repo": "/abs/repo",
                "feature_description": "Add a thing",
                "context_paths": ["/abs/extra/doc1.md", "/abs/extra/doc2.md"],
            }
        )
        assert capture.get("extra_context_paths") == [
            "/abs/extra/doc1.md",
            "/abs/extra/doc2.md",
        ]

    @pytest.mark.asyncio()
    async def test_feature_spec_passes_none_when_no_context_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(
                return_result=_make_success_result("feature-spec"),
                capture=capture,
            ),
        )
        await guardkit_tools.guardkit_feature_spec.ainvoke(
            {"repo": "/abs/repo", "feature_description": "Add a thing"}
        )
        assert capture.get("extra_context_paths") is None

    def test_only_feature_spec_exposes_context_paths_param(self) -> None:
        # AC-7 is explicit: only guardkit_feature_spec exposes context_paths.
        spec_schema = (
            guardkit_tools.guardkit_feature_spec.args_schema.model_json_schema()
        )
        assert "context_paths" in spec_schema["properties"]

        for wrapper, _sub, _kw in ALL_WRAPPERS:
            if wrapper is guardkit_tools.guardkit_feature_spec:
                continue
            schema = wrapper.args_schema.model_json_schema()
            assert "context_paths" not in schema["properties"], (
                f"{wrapper.name} unexpectedly exposes context_paths"
            )


# ---------------------------------------------------------------------------
# Seam test — JSON contract holds across the matrix
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("guardkit_tool_layer_contract")
class TestGuardKitToolLayerContract:
    """Seam test for the contract documented in API-tool-layer.md §6.

    Validates that the JSON envelope is well-formed across every
    wrapper — both the success path (a parseable ``GuardKitResult``)
    and the internal-exception path (the ADR-ARCH-025 error shape).
    """

    @pytest.mark.parametrize(
        "wrapper,subcommand,kwargs",
        ALL_WRAPPERS,
        ids=[w.name for w, _s, _k in ALL_WRAPPERS],
    )
    @pytest.mark.asyncio()
    async def test_success_payload_is_parseable_json(
        self,
        wrapper: Any,
        subcommand: str,
        kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(return_result=_make_success_result(subcommand)),
        )
        out = await wrapper.ainvoke(kwargs)
        decoded = json.loads(out)
        # Documented status set per ADR-ARCH-025.
        assert decoded["status"] in {"success", "failed", "timeout"}

    @pytest.mark.parametrize(
        "wrapper,_subcommand,kwargs",
        ALL_WRAPPERS,
        ids=[w.name for w, _s, _k in ALL_WRAPPERS],
    )
    @pytest.mark.asyncio()
    async def test_error_envelope_matches_adr_arch_025_shape(
        self,
        wrapper: Any,
        _subcommand: str,
        kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            guardkit_tools,
            "guardkit_run",
            _stub_run(raise_exc=ValueError("contract!")),
        )
        out = await wrapper.ainvoke(kwargs)
        decoded = json.loads(out)
        assert decoded["status"] == "error"
        assert decoded["tool"] == wrapper.name
        assert "error" in decoded


# ---------------------------------------------------------------------------
# Concurrency — two parallel calls do not interfere (per ASSUM-006)
# ---------------------------------------------------------------------------


class TestConcurrentInvocations:
    """Two concurrent wrapper calls return independent results."""

    @pytest.mark.asyncio()
    async def test_two_parallel_calls_return_independent_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict[str, Any]] = []

        async def _stub(**kwargs: Any) -> GuardKitResult:
            captured.append(dict(kwargs))
            await asyncio.sleep(0)
            return _make_success_result(kwargs["subcommand"])

        monkeypatch.setattr(guardkit_tools, "guardkit_run", _stub)
        out1, out2 = await asyncio.gather(
            guardkit_tools.guardkit_task_review.ainvoke(
                {"repo": "/abs/repo-a", "task_id": "T-A"}
            ),
            guardkit_tools.guardkit_task_review.ainvoke(
                {"repo": "/abs/repo-b", "task_id": "T-B"}
            ),
        )
        assert json.loads(out1)["status"] == "success"
        assert json.loads(out2)["status"] == "success"
        # Two distinct repo_paths captured.
        repo_paths = {str(c["repo_path"]) for c in captured}
        assert repo_paths == {"/abs/repo-a", "/abs/repo-b"}
