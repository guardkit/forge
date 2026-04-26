"""Nine ``guardkit_*`` LangChain ``@tool`` wrappers (TASK-GCI-009).

Each wrapper is a thin async ``@tool(parse_docstring=True)`` shim around
:func:`forge.adapters.guardkit.run.run` (TASK-GCI-008) that:

- Composes a NATS progress subscription via
  :func:`forge.adapters.guardkit.progress_subscriber.subscribe_progress`
  (TASK-GCI-005) for the duration of the call. A ``None`` client (or any
  broker error) is recorded as a structured warning on the sink and
  **never** fails the wrapper — telemetry is non-authoritative.
- Returns a ``str`` containing JSON: either
  :class:`~forge.adapters.guardkit.models.GuardKitResult.model_dump_json`
  on success / failed / timeout, or the ADR-ARCH-025 error shape
  ``{"status":"error","error":"<ExcType>: <msg>","tool":"<name>"}`` when
  an unexpected exception is raised inside the wrapper itself.
- Logs a single structured line per call with ``tool_name``,
  ``duration_ms``, and ``status`` (per
  ``docs/design/contracts/API-tool-layer.md`` §2). The standard library
  :mod:`logging` is used because ``structlog`` is not a project
  dependency; the key/value shape is preserved so downstream log
  processors can still parse it.

Per the universal error contract (ADR-ARCH-025) and the BDD scenarios

- "A failing GuardKit subprocess is reported as a structured error, not
  an exception", and
- "An unexpected error inside a wrapper is returned as a structured
  error, not raised",

**no wrapper in this module ever raises out** — every body is wrapped in
``try / except Exception``. The single exception is
:class:`asyncio.CancelledError`, which propagates out of the inner
:func:`forge.adapters.guardkit.run.run` so the surrounding async task
unwinds cleanly (Implementation Notes, TASK-GCI-008).

Tool inventory and the underlying subcommand each wraps
(``docs/design/contracts/API-tool-layer.md`` §6.1):

==========================  ====================  ==========================
Tool                        Subcommand            Parameters
==========================  ====================  ==========================
``guardkit_system_arch``    ``system-arch``       repo, feature_id, scope
``guardkit_system_design``  ``system-design``     repo, focus, protocols
``guardkit_system_plan``    ``system-plan``       repo, feature_description
``guardkit_feature_spec``   ``feature-spec``      repo, feature_description,
                                                  context_paths
``guardkit_feature_plan``   ``feature-plan``      repo, feature_id
``guardkit_task_review``    ``task-review``       repo, task_id
``guardkit_task_work``      ``task-work``         repo, task_id
``guardkit_task_complete``  ``task-complete``     repo, task_id
``guardkit_autobuild``      ``autobuild``         repo, feature_id
==========================  ====================  ==========================

PR creation is **not** in this file — that lives in a separate
``version_control`` tool layer (out of scope for TASK-GCI-009).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from langchain.tools import tool

from forge.adapters.guardkit.progress_subscriber import (
    ProgressSink,
    subscribe_progress,
)
from forge.adapters.guardkit.run import run as guardkit_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_id_for(repo: str, subcommand: str) -> str:
    """Compose a stable per-call ``build_id`` for the progress subscriber.

    The exact format is not part of the contract — what matters is that
    two concurrent invocations against the same repo get distinct IDs so
    their progress streams are isolated (AC-006 of TASK-GCI-005). We use
    the repo basename, the subcommand, and a millisecond monotonic
    timestamp.
    """
    return (
        f"{Path(repo).name}-"
        f"{subcommand.replace(' ', '_')}-"
        f"{int(time.monotonic() * 1000)}"
    )


def _error_json(exc: BaseException, tool_name: str) -> str:
    """Return the ADR-ARCH-025 error envelope as a JSON string.

    The shape is ``{"status":"error","error":"<ExcType>: <msg>",
    "tool":"<name>"}``. Using :func:`json.dumps` (rather than an
    f-string with manual escaping, as the task brief sketched) is
    safer: the exception message can contain quotes, backslashes, and
    newlines that would corrupt a raw f-string envelope.
    """
    return json.dumps(
        {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "tool": tool_name,
        }
    )


async def _invoke(
    *,
    tool_name: str,
    subcommand: str,
    repo: str,
    args: list[str],
    extra_context_paths: list[str] | None = None,
    nats_client: Any = None,
    sink: ProgressSink | None = None,
) -> str:
    """Shared invocation helper for all nine wrappers.

    This is the single place that:

    1. Composes :func:`subscribe_progress` so progress events stream on
       the bus while the subprocess is still running (Scenario "GuardKit
       progress is streamed on the bus while the subprocess is still
       running"); a missing or broken subscriber is non-fatal (Scenario
       "The authoritative result still returns when progress streaming
       is unavailable").
    2. Calls :func:`forge.adapters.guardkit.run.run` with the literal
       ``subcommand`` token — never an f-string-built command — and
       threads ``extra_context_paths`` for the explicit-context retry
       case (Scenario "A failed invocation can be retried with
       additional explicit context").
    3. Logs one structured line per call with ``tool_name``,
       ``duration_ms``, and ``status``.
    4. Wraps everything in ``try / except Exception`` so the wrapper
       never raises (ADR-ARCH-025); the body returns
       :meth:`GuardKitResult.model_dump_json` on success / failed /
       timeout, or :func:`_error_json` on internal exception.
    """
    started_at = time.monotonic()
    repo_path = Path(repo)
    sink = sink if sink is not None else ProgressSink()
    build_id = _build_id_for(repo, subcommand)

    try:
        async with subscribe_progress(nats_client, build_id, subcommand, sink):
            result = await guardkit_run(
                subcommand=subcommand,
                args=list(args),
                repo_path=repo_path,
                read_allowlist=[repo_path],
                extra_context_paths=extra_context_paths,
            )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "guardkit_tool_invocation tool_name=%s status=%s duration_ms=%d",
            tool_name,
            result.status,
            duration_ms,
        )
        return result.model_dump_json()
    except Exception as exc:  # noqa: BLE001 — boundary swallow per ADR-ARCH-025
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.exception(
            "guardkit_tool_invocation tool_name=%s status=error duration_ms=%d",
            tool_name,
            duration_ms,
        )
        return _error_json(exc, tool_name)


# ---------------------------------------------------------------------------
# 1) System-tier wrappers
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_system_arch(
    repo: str,
    feature_id: str,
    scope: str | None = None,
) -> str:
    """Run ``guardkit system-arch`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_id: Feature identifier to architect (e.g. ``FEAT-FORGE-007``).
        scope: Optional architectural scope hint (e.g. ``"adapters"``).

    Returns:
        JSON string ``{"status":"success|failed|timeout", "artefacts":[...],
        "coach_score":..., "duration_secs":..., "stderr":"...",
        "warnings":[...]}`` per the canonical ``GuardKitResult`` shape, or
        ``{"status":"error","error":"...","tool":"guardkit_system_arch"}``
        if the wrapper itself raised.
    """
    try:
        args: list[str] = ["--feature-id", feature_id]
        if scope is not None:
            args.extend(["--scope", scope])
        return await _invoke(
            tool_name="guardkit_system_arch",
            subcommand="system-arch",
            repo=repo,
            args=args,
        )
    except Exception as exc:  # noqa: BLE001 — defensive double-guard
        return _error_json(exc, "guardkit_system_arch")


@tool(parse_docstring=True)
async def guardkit_system_design(
    repo: str,
    focus: str,
    protocols: list[str] | None = None,
) -> str:
    """Run ``guardkit system-design`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        focus: Design focus area (e.g. ``"event-bus"``).
        protocols: Optional list of protocols to scope the design pass.

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        args: list[str] = ["--focus", focus]
        for proto in protocols or []:
            args.extend(["--protocol", proto])
        return await _invoke(
            tool_name="guardkit_system_design",
            subcommand="system-design",
            repo=repo,
            args=args,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_system_design")


@tool(parse_docstring=True)
async def guardkit_system_plan(repo: str, feature_description: str) -> str:
    """Run ``guardkit system-plan`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_description: One-line description of the feature whose
            system plan should be generated.

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_system_plan",
            subcommand="system-plan",
            repo=repo,
            args=["--description", feature_description],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_system_plan")


# ---------------------------------------------------------------------------
# 2) Feature-tier wrappers
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_feature_spec(
    repo: str,
    feature_description: str,
    context_paths: list[str] | None = None,
) -> str:
    """Run ``guardkit feature-spec`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_description: One-line description for the
            ``/feature-spec`` session.
        context_paths: Optional explicit ``--context`` overrides. When
            ``None`` the context-manifest resolver picks them
            automatically. When supplied they are threaded through to
            ``run(extra_context_paths=...)`` for the explicit-context
            retry case (Scenario "A failed invocation can be retried
            with additional explicit context").

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_feature_spec",
            subcommand="feature-spec",
            repo=repo,
            args=["--description", feature_description],
            extra_context_paths=context_paths,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_feature_spec")


@tool(parse_docstring=True)
async def guardkit_feature_plan(repo: str, feature_id: str) -> str:
    """Run ``guardkit feature-plan`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_id: Feature identifier whose plan should be generated.

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_feature_plan",
            subcommand="feature-plan",
            repo=repo,
            args=["--feature-id", feature_id],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_feature_plan")


# ---------------------------------------------------------------------------
# 3) Task-tier wrappers
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_task_review(repo: str, task_id: str) -> str:
    """Run ``guardkit task-review`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        task_id: Task identifier to review (e.g. ``TASK-GCI-009``).

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_task_review",
            subcommand="task-review",
            repo=repo,
            args=["--task-id", task_id],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_task_review")


@tool(parse_docstring=True)
async def guardkit_task_work(repo: str, task_id: str) -> str:
    """Run ``guardkit task-work`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        task_id: Task identifier to implement (e.g. ``TASK-GCI-009``).

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_task_work",
            subcommand="task-work",
            repo=repo,
            args=["--task-id", task_id],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_task_work")


@tool(parse_docstring=True)
async def guardkit_task_complete(repo: str, task_id: str) -> str:
    """Run ``guardkit task-complete`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        task_id: Task identifier whose completion ceremony should run.

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_task_complete",
            subcommand="task-complete",
            repo=repo,
            args=["--task-id", task_id],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_task_complete")


# ---------------------------------------------------------------------------
# 4) Build-tier wrapper
# ---------------------------------------------------------------------------


@tool(parse_docstring=True)
async def guardkit_autobuild(repo: str, feature_id: str) -> str:
    """Run ``guardkit autobuild`` in the target repo with NATS streaming.

    Args:
        repo: Absolute path to the target repo (worktree root).
        feature_id: Feature identifier whose autobuild loop should run.

    Returns:
        JSON ``GuardKitResult`` shape on success/failed/timeout, or the
        ADR-ARCH-025 error envelope on internal exception.
    """
    try:
        return await _invoke(
            tool_name="guardkit_autobuild",
            subcommand="autobuild",
            repo=repo,
            args=["--feature-id", feature_id],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_json(exc, "guardkit_autobuild")


__all__ = [
    "guardkit_autobuild",
    "guardkit_feature_plan",
    "guardkit_feature_spec",
    "guardkit_system_arch",
    "guardkit_system_design",
    "guardkit_system_plan",
    "guardkit_task_complete",
    "guardkit_task_review",
    "guardkit_task_work",
]
