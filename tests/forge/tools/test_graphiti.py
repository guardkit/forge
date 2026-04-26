"""Unit tests for :mod:`forge.tools.graphiti` (TASK-GCI-010).

Acceptance-criteria coverage map:

- AC-001 — Both wrappers exist in ``src/forge/tools/graphiti.py`` and
  are decorated with ``@tool(parse_docstring=True)`` (verified by
  inspecting ``BaseTool`` instance type, ``args_schema`` field set,
  and the parsed docstring description).
- AC-002 — Both call ``forge.adapters.guardkit.run`` with a
  ``subcommand`` literal that **starts with** ``"graphiti "`` so the
  resolver-bypass branch fires inside ``run()`` (DDR-005).
- AC-003 — No ``--context`` token appears in the command line or in
  the kwargs the wrappers pass to ``run()``, regardless of any
  manifest in the target repo (verified with a fake ``run()`` that
  also captures the assembled argv that ``run()`` would build).
- AC-004 — Both return a ``str`` that round-trips through
  ``json.loads`` on every code path (success / failed / timeout /
  internal exception); the exception path returns the canonical
  ``{"status":"error","error":"..."}`` shape from ADR-ARCH-025 and
  never raises out of the wrapper boundary.
- AC-005 — Each wrapper body is wrapped in
  ``try/except Exception as exc:`` (verified by triggering an
  exception inside the fake ``run()`` and asserting the JSON-error
  contract is honoured).
- AC-006 — Each wrapper logs at least one ``logger`` call carrying
  ``tool_name``, ``duration_ms`` and ``status`` (verified with a
  monkeypatched ``logger.info`` that captures the kwargs).
- AC-007 — Module-level lint hygiene: no relative imports of the
  context resolver (``forge.adapters.guardkit.context_resolver`` is
  off-limits — these tools are resolver-blind by design).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.tools import graphiti as graphiti_tools
from forge.tools.graphiti import (
    guardkit_graphiti_add_context,
    guardkit_graphiti_query,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_result(subcommand: str, *, duration: float = 0.5) -> GuardKitResult:
    """Build a canned successful :class:`GuardKitResult` for a subcommand."""
    return GuardKitResult(
        status="success",
        subcommand=subcommand,
        duration_secs=duration,
        stdout_tail="ok",
        stderr=None,
        exit_code=0,
    )


def _failed_result(subcommand: str) -> GuardKitResult:
    return GuardKitResult(
        status="failed",
        subcommand=subcommand,
        duration_secs=0.1,
        stdout_tail="",
        stderr="boom",
        exit_code=1,
    )


def _timeout_result(subcommand: str) -> GuardKitResult:
    return GuardKitResult(
        status="timeout",
        subcommand=subcommand,
        duration_secs=600.0,
        stdout_tail="",
        stderr="timed out",
        exit_code=-1,
    )


def _make_fake_run(capture: dict[str, Any], result: GuardKitResult):
    """Build an async fake ``run()`` that records its kwargs into ``capture``.

    The fake records every keyword argument it is called with so the
    test can assert on the exact ``subcommand``, ``args``,
    ``read_allowlist``, ``with_nats_streaming``, and any
    ``extra_context_paths`` the wrapper passed.
    """

    async def _fake_run(**kwargs: Any) -> GuardKitResult:
        capture["kwargs"] = kwargs
        return result

    return _fake_run


def _make_raising_run(exc: Exception):
    """Build an async fake ``run()`` that raises ``exc`` on call."""

    async def _fake_run(**_kwargs: Any) -> GuardKitResult:
        raise exc

    return _fake_run


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Allowlist-eligible working directory for the wrapper to chdir to."""
    repo = tmp_path / "build"
    repo.mkdir()
    return repo


# ---------------------------------------------------------------------------
# AC-001 — Decorator surface
# ---------------------------------------------------------------------------


class TestDecoratorSurface:
    """AC-001 — Both wrappers are LangChain ``BaseTool`` instances."""

    def test_add_context_is_a_basetool_with_parsed_docstring(self) -> None:
        from langchain_core.tools import BaseTool

        assert isinstance(guardkit_graphiti_add_context, BaseTool)
        # docstring's first line becomes the description
        assert guardkit_graphiti_add_context.description.startswith(
            "Run `guardkit graphiti add-context`"
        )
        # Args section defines the schema
        fields = set(guardkit_graphiti_add_context.args_schema.model_fields)
        assert "doc_path" in fields
        assert "group" in fields

    def test_query_is_a_basetool_with_parsed_docstring(self) -> None:
        from langchain_core.tools import BaseTool

        assert isinstance(guardkit_graphiti_query, BaseTool)
        assert guardkit_graphiti_query.description.startswith(
            "Run `guardkit graphiti query`"
        )
        fields = set(guardkit_graphiti_query.args_schema.model_fields)
        assert "query" in fields
        assert "group" in fields

    def test_module_lives_under_forge_tools(self) -> None:
        module_file = inspect.getsourcefile(graphiti_tools) or ""
        assert module_file.endswith("forge/tools/graphiti.py")

    def test_underlying_callables_are_coroutine_functions(self) -> None:
        # ``@tool`` exposes the wrapped function via .coroutine for async tools.
        assert asyncio.iscoroutinefunction(
            guardkit_graphiti_add_context.coroutine
        )
        assert asyncio.iscoroutinefunction(guardkit_graphiti_query.coroutine)


# ---------------------------------------------------------------------------
# AC-002 — Subcommand prefix triggers resolver bypass
# ---------------------------------------------------------------------------


class TestSubcommandPrefixBypassesResolver:
    """AC-002 — both wrappers pass a ``"graphiti …"`` subcommand string."""

    @pytest.mark.asyncio()
    async def test_add_context_subcommand_starts_with_graphiti_space(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                capture, _success_result("graphiti add-context")
            ),
        )

        await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "guardkit__feature_specs",
                "repo": str(repo_root),
            }
        )

        subcommand = capture["kwargs"]["subcommand"]
        assert subcommand == "graphiti add-context"
        # Sanity: the resolver-bypass detector inside run() keys off
        # the head token; this string must satisfy that contract.
        assert subcommand.startswith("graphiti ")

    @pytest.mark.asyncio()
    async def test_query_subcommand_starts_with_graphiti_space(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(capture, _success_result("graphiti query")),
        )

        await guardkit_graphiti_query.ainvoke(
            {
                "query": "what changed in FEAT-FORGE-005?",
                "group": "architecture_decisions",
                "repo": str(repo_root),
            }
        )

        subcommand = capture["kwargs"]["subcommand"]
        assert subcommand == "graphiti query"
        assert subcommand.startswith("graphiti ")


# ---------------------------------------------------------------------------
# AC-003 — No --context flag is ever assembled
# ---------------------------------------------------------------------------


class TestNoContextFlagInArgv:
    """AC-003 — neither wrapper threads ``--context`` into ``run()``."""

    @pytest.mark.asyncio()
    async def test_add_context_passes_no_extra_context_paths(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                capture, _success_result("graphiti add-context")
            ),
        )

        await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "guardkit__feature_specs",
                "repo": str(repo_root),
            }
        )

        kwargs = capture["kwargs"]
        # The wrapper MUST NOT send any --context tokens, neither in
        # ``args`` nor as ``extra_context_paths``.
        assert "--context" not in kwargs["args"]
        assert kwargs.get("extra_context_paths") in (None, [])

    @pytest.mark.asyncio()
    async def test_query_passes_no_extra_context_paths(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(capture, _success_result("graphiti query")),
        )

        await guardkit_graphiti_query.ainvoke(
            {
                "query": "what changed?",
                "group": "architecture_decisions",
                "repo": str(repo_root),
            }
        )

        kwargs = capture["kwargs"]
        assert "--context" not in kwargs["args"]
        assert kwargs.get("extra_context_paths") in (None, [])

    @pytest.mark.asyncio()
    async def test_add_context_args_carry_doc_and_group_only(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                capture, _success_result("graphiti add-context")
            ),
        )

        await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "guardkit__feature_specs",
                "repo": str(repo_root),
            }
        )

        argv = capture["kwargs"]["args"]
        assert "/abs/docs/spec.md" in argv
        assert "guardkit__feature_specs" in argv

    @pytest.mark.asyncio()
    async def test_query_args_carry_query_and_group_only(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(capture, _success_result("graphiti query")),
        )

        await guardkit_graphiti_query.ainvoke(
            {
                "query": "what changed?",
                "group": "architecture_decisions",
                "repo": str(repo_root),
            }
        )

        argv = capture["kwargs"]["args"]
        assert "what changed?" in argv
        assert "architecture_decisions" in argv


# ---------------------------------------------------------------------------
# AC-004 — JSON return shape on every status path
# ---------------------------------------------------------------------------


class TestReturnIsJsonString:
    """AC-004 — wrappers return parseable JSON on every code path."""

    @pytest.mark.asyncio()
    async def test_success_returns_json_with_status_success(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                capture, _success_result("graphiti add-context")
            ),
        )

        out = await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "guardkit__feature_specs",
                "repo": str(repo_root),
            }
        )

        payload = json.loads(out)
        assert payload["status"] == "success"
        assert "duration_secs" in payload
        # Stderr is optional/nullable — must round-trip.
        assert "stderr" in payload

    @pytest.mark.asyncio()
    async def test_failed_returns_json_with_status_failed_and_stderr(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run({}, _failed_result("graphiti query")),
        )

        out = await guardkit_graphiti_query.ainvoke(
            {
                "query": "q",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        payload = json.loads(out)
        assert payload["status"] == "failed"
        assert payload["stderr"] == "boom"
        assert payload["exit_code"] == 1

    @pytest.mark.asyncio()
    async def test_timeout_returns_json_with_status_timeout(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                {}, _timeout_result("graphiti add-context")
            ),
        )

        out = await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        payload = json.loads(out)
        assert payload["status"] == "timeout"
        assert payload["duration_secs"] >= 0.0


# ---------------------------------------------------------------------------
# AC-005 — Internal exceptions are absorbed (ADR-ARCH-025)
# ---------------------------------------------------------------------------


class TestInternalExceptionsAreAbsorbed:
    """AC-005 — body is ``try/except Exception`` and returns JSON-error."""

    @pytest.mark.asyncio()
    async def test_add_context_returns_error_json_when_run_raises(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_raising_run(RuntimeError("kaboom")),
        )

        out = await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        payload = json.loads(out)
        assert payload["status"] == "error"
        assert "RuntimeError" in payload["error"]
        assert "kaboom" in payload["error"]

    @pytest.mark.asyncio()
    async def test_query_returns_error_json_when_run_raises(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_raising_run(ValueError("bad input")),
        )

        out = await guardkit_graphiti_query.ainvoke(
            {
                "query": "q",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        payload = json.loads(out)
        assert payload["status"] == "error"
        assert "ValueError" in payload["error"]
        assert "bad input" in payload["error"]

    @pytest.mark.asyncio()
    async def test_error_payload_with_quotes_round_trips_through_json(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An exception message containing JSON-control characters (quotes,
        # backslashes) must NOT corrupt the wrapper's JSON output. This
        # asserts the wrapper uses a real JSON encoder, not f-string concat.
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_raising_run(ValueError('contains "quotes" and \\ backslash')),
        )

        out = await guardkit_graphiti_query.ainvoke(
            {
                "query": "q",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        # If the wrapper used naive f-string interpolation, json.loads
        # would raise JSONDecodeError on the embedded quote.
        payload = json.loads(out)
        assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# AC-006 — Structured logging
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    """AC-006 — each wrapper logs ``tool_name``, ``duration_ms``, ``status``."""

    @pytest.mark.asyncio()
    async def test_add_context_logs_tool_name_duration_status(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(
                {}, _success_result("graphiti add-context", duration=0.123)
            ),
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        def _capture_log(event: str, **kw: Any) -> None:
            recorded.append((event, kw))

        # Whether the wrapper uses ``structlog`` or the stdlib logger, we
        # accept either by patching both names; the wrapper must reach at
        # least one of them with the contractual kwargs.
        monkeypatch.setattr(
            graphiti_tools.logger, "info", _capture_log, raising=False
        )

        await guardkit_graphiti_add_context.ainvoke(
            {
                "doc_path": "/abs/docs/spec.md",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        assert recorded, "wrapper produced no log records"
        # At least one record must carry the contractual triple.
        assert any(
            kw.get("tool_name") == "guardkit_graphiti_add_context"
            and "duration_ms" in kw
            and kw.get("status") == "success"
            for _event, kw in recorded
        )

    @pytest.mark.asyncio()
    async def test_query_logs_tool_name_duration_status(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run({}, _failed_result("graphiti query")),
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        def _capture_log(event: str, **kw: Any) -> None:
            recorded.append((event, kw))

        monkeypatch.setattr(
            graphiti_tools.logger, "info", _capture_log, raising=False
        )

        await guardkit_graphiti_query.ainvoke(
            {
                "query": "q",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        assert recorded, "wrapper produced no log records"
        assert any(
            kw.get("tool_name") == "guardkit_graphiti_query"
            and "duration_ms" in kw
            and kw.get("status") == "failed"
            for _event, kw in recorded
        )


# ---------------------------------------------------------------------------
# AC-007 — Module hygiene
# ---------------------------------------------------------------------------


class TestModuleHygiene:
    """AC-007 — the resolver is NOT imported by the graphiti tool module.

    These tools are resolver-blind by design (DDR-005). If the resolver
    is imported here, a future change could accidentally invoke it.
    """

    def test_module_does_not_import_context_resolver(self) -> None:
        # Inspect the module's *namespace* and *AST*, not the raw source —
        # docstrings may legitimately reference DDR-005 by name. What
        # matters is that no import statement binds the resolver into
        # the module so a future change cannot accidentally call it.
        import ast

        source = inspect.getsource(graphiti_tools)
        tree = ast.parse(source)
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            elif isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)

        assert not any(
            "context_resolver" in m for m in imported_modules
        ), (
            "forge.tools.graphiti must not import the context resolver — "
            "Graphiti subcommands bypass resolution entirely (DDR-005). "
            f"Found: {imported_modules}"
        )

        # Also assert the resolver name is not bound in the module
        # namespace (defensive: catches ``import …; from … import *``).
        assert not hasattr(graphiti_tools, "resolve_context_flags")
        assert not hasattr(graphiti_tools, "ResolvedContext")

    def test_module_exports_both_tools(self) -> None:
        assert "guardkit_graphiti_add_context" in graphiti_tools.__all__
        assert "guardkit_graphiti_query" in graphiti_tools.__all__


# ---------------------------------------------------------------------------
# Cross-cutting — repo defaulting still satisfies confinement
# ---------------------------------------------------------------------------


class TestRepoConfinement:
    """The wrapper must hand ``run()`` an absolute, allowlist-eligible cwd."""

    @pytest.mark.asyncio()
    async def test_repo_threaded_through_as_repo_path_and_allowlist(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            graphiti_tools,
            "guardkit_run",
            _make_fake_run(capture, _success_result("graphiti query")),
        )

        await guardkit_graphiti_query.ainvoke(
            {
                "query": "q",
                "group": "g",
                "repo": str(repo_root),
            }
        )

        kwargs = capture["kwargs"]
        # repo_path is absolute and matches the supplied repo
        repo_path = kwargs["repo_path"]
        assert isinstance(repo_path, Path)
        assert repo_path.is_absolute()
        # The allowlist must include the repo so run()'s confinement check passes.
        allowlist = kwargs["read_allowlist"]
        assert any(
            repo_path == a or repo_path.is_relative_to(a) for a in allowlist
        )
