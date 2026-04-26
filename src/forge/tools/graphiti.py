"""Two ``guardkit_graphiti_*`` LangChain ``@tool`` wrappers (TASK-GCI-010).

These are the only GuardKit tool wrappers that bypass context-manifest
resolution per ``docs/design/decisions/DDR-005-cli-context-manifest-resolution.md``
("GuardKit graphiti subcommands don't take ``--context``; skip resolution
entirely.") and ``docs/design/contracts/API-tool-layer.md`` §6.1 (rows
for ``graphiti add-context`` / ``graphiti query``).

Both wrappers:

- call :func:`forge.adapters.guardkit.run.run` with a ``subcommand``
  literal that **starts with** ``"graphiti "`` so the resolver-skip
  branch in :func:`run.run` fires (TASK-GCI-008's
  :func:`_is_graphiti_subcommand` keys off the head token);
- never thread ``--context`` flags into ``args`` and never set
  ``extra_context_paths`` — the wrappers are resolver-blind by design;
- return a JSON ``str`` on every code path (success / failed / timeout
  via :meth:`GuardKitResult.model_dump_json`, or
  ``{"status":"error","error":"...","tool":"<name>"}`` per
  ADR-ARCH-025 on internal exception);
- log a single structured record per call carrying ``tool_name``,
  ``duration_ms`` and ``status`` (per
  ``docs/design/contracts/API-tool-layer.md`` §2). The module prefers
  :mod:`structlog` when available and falls back to a kwargs-accepting
  shim around :mod:`logging` so the call shape stays identical.

This module **must not** import
:mod:`forge.adapters.guardkit.context_resolver` — Graphiti subcommands
must remain resolver-blind. The companion test ``test_graphiti.py``
asserts the absence of that import via source inspection.

Tool inventory and the underlying subcommand each wraps
(``docs/design/contracts/API-tool-layer.md`` §6.1, last two rows):

================================  =========================  ====================
Tool                              Subcommand                 Parameters
================================  =========================  ====================
``guardkit_graphiti_add_context`` ``graphiti add-context``   doc_path, group
``guardkit_graphiti_query``       ``graphiti query``         query, group
================================  =========================  ====================
"""

from __future__ import annotations

import json
import logging as _stdlib_logging
import time
from pathlib import Path
from typing import Any

from langchain.tools import tool

from forge.adapters.guardkit.run import run as guardkit_run

# ---------------------------------------------------------------------------
# Logger — structlog if available, kwargs-accepting stdlib shim otherwise.
# ---------------------------------------------------------------------------


class _StdlibStructLogger:
    """Tiny stdlib :mod:`logging` shim that accepts ``**kwargs``.

    The acceptance criteria require ``structlog``-style structured logs
    keyed by ``tool_name``, ``duration_ms`` and ``status``. ``structlog``
    itself is not declared in :file:`pyproject.toml`, so to keep the
    wrapper functional in either environment we fall back to a shim that
    accepts the same keyword-argument call shape and folds the kwargs
    into the rendered log line.

    The shim is deliberately minimal: it only implements the two methods
    the wrapper actually calls (``info``, ``exception``).
    """

    def __init__(self, name: str) -> None:
        self._inner = _stdlib_logging.getLogger(name)

    def info(self, event: str, **kwargs: Any) -> None:
        self._inner.info("%s %s", event, kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        self._inner.exception("%s %s", event, kwargs)


def _build_logger(name: str) -> Any:
    """Return a structlog logger when available, else the stdlib shim."""
    try:
        import structlog  # type: ignore[import-not-found]

        return structlog.get_logger(name)
    except ImportError:  # pragma: no cover — exercised only in stdlib-only envs
        return _StdlibStructLogger(name)


logger: Any = _build_logger("forge.tools.graphiti")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _error_json(exc: BaseException, tool_name: str) -> str:
    """Return the ADR-ARCH-025 error envelope as a JSON string.

    The shape is ``{"status":"error","error":"<ExcType>: <msg>",
    "tool":"<name>"}``. Using :func:`json.dumps` (rather than an
    f-string with manual escaping, as the task brief sketched) is
    safer: the exception message can contain quotes, backslashes, and
    newlines that would corrupt a hand-rolled f-string envelope.
    """
    return json.dumps(
        {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "tool": tool_name,
        }
    )


async def _invoke_graphiti(
    *,
    tool_name: str,
    subcommand: str,
    repo: str,
    args: list[str],
) -> str:
    """Shared invocation helper for both Graphiti wrappers.

    This is the single point that:

    1. Calls :func:`guardkit_run` with the literal Graphiti
       ``subcommand`` token — never built via f-string. Because the
       subcommand starts with ``"graphiti "``, ``run()``'s
       :func:`_is_graphiti_subcommand` detector fires and the
       resolver-skip branch is taken (Scenario "Graphiti GuardKit
       subcommands skip context-manifest resolution entirely",
       DDR-005).
    2. Confines the subprocess to the supplied ``repo`` by passing
       ``read_allowlist=[repo_path]`` — Graphiti operates on server
       state, not on repo files, but :func:`run` still requires an
       absolute, allowlist-eligible cwd (defence-in-depth atop
       DeepAgents' working-directory check, TASK-GCI-008 AC-003).
    3. Logs one structured record per call — ``tool_name``,
       ``duration_ms`` and ``status`` — per the universal tool-layer
       observability contract (``docs/design/contracts/API-tool-layer.md``
       §2).
    4. Folds every outcome (including unexpected internal exceptions)
       into a JSON string so the wrapper's boundary contract holds:
       it never raises out (ADR-ARCH-025).

    Notably, this helper **does not** thread an ``extra_context_paths``
    argument through to ``run()`` — Graphiti tools must never inject a
    ``--context`` flag (TASK-GCI-010 AC: "No ``--context`` flags appear
    in the assembled command line for either wrapper, even when a
    manifest exists in the target repo").
    """
    started_at = time.monotonic()
    repo_path = Path(repo)

    try:
        result = await guardkit_run(
            subcommand=subcommand,
            args=list(args),
            repo_path=repo_path,
            read_allowlist=[repo_path],
        )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "guardkit_graphiti_tool_invocation",
            tool_name=tool_name,
            duration_ms=duration_ms,
            status=result.status,
        )
        return result.model_dump_json()
    except Exception as exc:  # noqa: BLE001 — boundary swallow per ADR-ARCH-025
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.exception(
            "guardkit_graphiti_tool_invocation",
            tool_name=tool_name,
            duration_ms=duration_ms,
            status="error",
        )
        return _error_json(exc, tool_name)


# ---------------------------------------------------------------------------
# Tool 1 — guardkit graphiti add-context
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_graphiti_add_context(
    doc_path: str,
    group: str,
    repo: str = ".",
) -> str:
    """Run `guardkit graphiti add-context` to seed a doc into the knowledge graph.

    Skips context-manifest resolution — Graphiti subcommands do not
    consume ``--context`` flags (DDR-005). The ``subcommand`` passed to
    :func:`forge.adapters.guardkit.run.run` is the literal
    ``"graphiti add-context"``; ``run()``'s Graphiti detector keys off
    the leading ``"graphiti "`` token to skip the resolver entirely.

    Args:
        doc_path: Absolute path to the markdown document to add to the
            Graphiti knowledge graph.
        group: Graphiti group_id (e.g. ``"guardkit__feature_specs"``,
            ``"architecture_decisions"``) — namespaces the seed under
            a stable bucket so subsequent queries can scope to it.
        repo: Working-directory anchor for the subprocess (worktree
            root). Graphiti operates on server state, so this only
            needs to satisfy worktree-confinement; a build's worktree
            is the canonical value.

    Returns:
        JSON string ``{"status":"success|failed|timeout",
        "duration_secs":..., "stderr":"...", ...}`` per the canonical
        :class:`GuardKitResult` shape, or
        ``{"status":"error","error":"...","tool":"guardkit_graphiti_add_context"}``
        if the wrapper itself raised.
    """
    try:
        return await _invoke_graphiti(
            tool_name="guardkit_graphiti_add_context",
            subcommand="graphiti add-context",
            repo=repo,
            args=["--doc", doc_path, "--group", group],
        )
    except Exception as exc:  # noqa: BLE001 — defensive double-guard
        return _error_json(exc, "guardkit_graphiti_add_context")


# ---------------------------------------------------------------------------
# Tool 2 — guardkit graphiti query
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_graphiti_query(
    query: str,
    group: str,
    repo: str = ".",
) -> str:
    """Run `guardkit graphiti query` against the knowledge graph.

    Skips context-manifest resolution — Graphiti subcommands do not
    consume ``--context`` flags (DDR-005). The ``subcommand`` passed to
    :func:`forge.adapters.guardkit.run.run` is the literal
    ``"graphiti query"``; ``run()``'s Graphiti detector keys off the
    leading ``"graphiti "`` token to skip the resolver entirely.

    Args:
        query: Natural-language query string the Graphiti server should
            evaluate against the knowledge graph.
        group: Graphiti group_id (e.g. ``"guardkit__feature_specs"``,
            ``"architecture_decisions"``) — scopes the query to a
            specific namespace.
        repo: Working-directory anchor for the subprocess (worktree
            root). Graphiti operates on server state, so this only
            needs to satisfy worktree-confinement; a build's worktree
            is the canonical value.

    Returns:
        JSON string ``{"status":"success|failed|timeout",
        "duration_secs":..., "stderr":"...", ...}`` per the canonical
        :class:`GuardKitResult` shape, or
        ``{"status":"error","error":"...","tool":"guardkit_graphiti_query"}``
        if the wrapper itself raised.
    """
    try:
        return await _invoke_graphiti(
            tool_name="guardkit_graphiti_query",
            subcommand="graphiti query",
            repo=repo,
            args=["--query", query, "--group", group],
        )
    except Exception as exc:  # noqa: BLE001 — defensive double-guard
        return _error_json(exc, "guardkit_graphiti_query")


__all__ = [
    "guardkit_graphiti_add_context",
    "guardkit_graphiti_query",
]
