"""Fire-and-forget Graphiti write wrapper for pipeline-history entities.

This module is the **execution side** of FEAT-FORGE-006 — it consumes the
typed entities produced by :mod:`forge.memory.models` (TASK-IC-001) and
writes them into one of the two Graphiti groups owned by FEAT-FORGE-006:

* ``forge_pipeline_history`` — :class:`GateDecision`,
  :class:`CapabilityResolution`, :class:`OverrideEvent`,
  :class:`CalibrationAdjustment`, :class:`SessionOutcome`.
* ``forge_calibration_history`` — :class:`CalibrationEvent`.

Two public entry points are exposed:

* :func:`write_entity` — the synchronous-failure variant. It awaits the
  Graphiti write, raises on any error, and is intended for the
  reconcile-backfill path (TASK-IC-004) where the caller wants to know
  whether the write succeeded.

* :func:`fire_and_forget_write` — the resilience variant. It schedules
  :func:`write_entity` without blocking the caller and **never** raises
  to the caller. Errors are routed to a structured log line tagged with
  ``entity_id`` / ``group_id`` / ``entity_type`` / ``error_class`` /
  ``error_message`` so downstream alerting can correlate write failures
  with pipeline runs. This is the only entry point used by all
  stage-completion hooks (TASK-IC-003).

Why fire-and-forget
-------------------

Prior incidents — recorded in Graphiti's ``guardkit__task_outcomes``
group — established that *post-acceptance* write failures cause wasted
LLM-token spend, because correct work that has already been accepted is
discarded when the pipeline aborts on a memory write. The lesson, also
mirrored from ``_write_to_graphiti()`` in ``run_greenfield()`` (success
path), is to dispatch every stage-completion write fire-and-forget and
log the failure so the pipeline keeps going.

Backend selection (3-tier MCP-first)
------------------------------------

Mirrors the LLM-side pattern in
``docs/internals/commands-lib/graphiti-preamble.md``:

* **Tier 0 — MCP/in-process** (``graphiti_core`` is importable): use it
  directly via :func:`_write_via_mcp`. Lowest overhead.
* **Tier 1 — CLI fallback** (``guardkit`` on PATH): shell out to
  ``guardkit graphiti add-context`` via
  :func:`asyncio.create_subprocess_exec`. The ``guardkit`` invocation
  goes through the project's own resolver-skip path (per DDR-005, see
  ``forge.tools.graphiti``) so no ``--context`` flag is ever threaded.
* **Tier 2 — unavailable**: raise :class:`GraphitiUnavailableError`.
  In :func:`fire_and_forget_write` this becomes a logged line, never a
  raised exception.

The selection is deliberately encapsulated behind :func:`_dispatch_write`
so a future "async Graphiti write" shared library (per Graphiti
``project_decisions``) can replace the body without touching the public
API. Both public entry points call into :func:`_dispatch_write`; tests
patch the dispatcher to assert the correct backend was selected.

Purity / boundary contract
--------------------------

* Every text field of every entity is run through
  :func:`forge.memory.redaction.redact_credentials` *before* the payload
  leaves this module. The redaction is performed on the serialised dict
  (``model.model_dump(mode="json")``) by recursing through nested
  ``dict`` / ``list`` / ``str`` leaves, so we cannot accidentally miss
  a freshly added text field.
* :func:`fire_and_forget_write` MUST NOT raise. The Graphiti
  ``task_outcomes`` lesson is explicit that silent swallow is the
  failure mode — every caught exception is funneled to
  :func:`_log_failure`, which uses ``logger.error(..., exc_info=...)``
  so the traceback is preserved in the log.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
import threading
from typing import Any, Union

from pydantic import BaseModel

from .models import (
    CalibrationAdjustment,
    CalibrationEvent,
    CapabilityResolution,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
)
from .redaction import redact_credentials

#: Module logger. The structured-log call sites use ``extra=...`` rather
#: than f-string interpolation so log aggregators can index on
#: ``entity_id`` / ``group_id`` / ``entity_type`` / ``error_class``
#: without parsing free-text.
logger = logging.getLogger("forge.memory.writer")

#: Union of every typed entity that may be written via this module.
#: Kept in sync with :mod:`forge.memory.models` exports — adding a new
#: pipeline-history entity must update this alias.
PipelineHistoryEntity = Union[
    GateDecision,
    CapabilityResolution,
    OverrideEvent,
    CalibrationAdjustment,
    SessionOutcome,
    CalibrationEvent,
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GraphitiUnavailableError(RuntimeError):
    """Neither the MCP nor CLI Graphiti backend is available.

    Raised by :func:`write_entity` when neither tier can be selected.
    :func:`fire_and_forget_write` catches and logs this error like any
    other backend failure; the pipeline keeps running.
    """


class GraphitiCLIError(RuntimeError):
    """The ``guardkit graphiti add-context`` subprocess exited non-zero.

    Carries the captured stderr in ``args[0]`` for diagnostics. Same
    handling as :class:`GraphitiUnavailableError` — surfaced by
    :func:`write_entity`, swallowed-and-logged by
    :func:`fire_and_forget_write`.
    """


# ---------------------------------------------------------------------------
# Redaction over a serialised entity dict
# ---------------------------------------------------------------------------


def _redact_payload(value: Any) -> Any:
    """Return a deep-copied structure with every ``str`` leaf redacted.

    Recursing the serialised dict (rather than enumerating known fields
    on each entity model) ensures that adding a new free-text field to
    any model in :mod:`forge.memory.models` does not require a paired
    edit here — the new field is automatically scrubbed because every
    string leaf is scrubbed.

    Non-string scalars (``int``, ``float``, ``bool``, ``None``) and
    UUID/datetime values (after ``model_dump(mode='json')`` they arrive
    as strings, so they *are* scrubbed — but the redaction patterns
    don't match canonical ISO-8601 timestamps or UUID hex, which is
    deliberate: the patterns require ``[0-9a-fA-F]{40,}`` or a token
    prefix, so 36-char UUIDs and 19-char timestamps survive verbatim).
    """
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _entity_to_redacted_payload(entity: BaseModel) -> dict[str, Any]:
    """Serialise the entity to a JSON-mode dict with every string redacted.

    ``model_dump(mode="json")`` is preferred over plain ``model_dump()``
    because UUIDs, datetimes, and Enums become JSON-safe strings — the
    payload is then directly serialisable for the CLI/MCP transport
    without a second pass.
    """
    serialised = entity.model_dump(mode="json")
    redacted = _redact_payload(serialised)
    # ``_redact_payload`` preserves the top-level dict shape; this is
    # an invariant of pydantic's ``model_dump()`` output, but assert
    # it explicitly so a future regression surfaces here instead of
    # at the transport boundary.
    if not isinstance(redacted, dict):  # pragma: no cover — defensive
        raise TypeError(
            f"Redacted payload must be a dict, got {type(redacted).__name__}"
        )
    return redacted


# ---------------------------------------------------------------------------
# Backend availability / dispatch
# ---------------------------------------------------------------------------


def _mcp_backend_available() -> bool:
    """Return ``True`` when ``graphiti_core`` can be imported.

    Uses :func:`importlib.util.find_spec` rather than a bare ``import``
    so the check is side-effect-free: we never actually import the
    module here, we only ask the import system whether a module of
    that name is locatable on ``sys.path``. Tests can monkeypatch this
    function (or the underlying ``find_spec``) to simulate either tier.
    """
    return importlib.util.find_spec("graphiti_core") is not None


def _cli_backend_available() -> bool:
    """Return ``True`` when ``guardkit`` is on the current PATH.

    The CLI tier shells out to ``guardkit graphiti add-context``; if
    the binary is not discoverable then this tier cannot be used. The
    check is performed once per write so a freshly-installed CLI is
    picked up without restarting the pipeline.
    """
    return shutil.which("guardkit") is not None


async def _write_via_mcp(
    payload: dict[str, Any], group_id: str, episode_name: str
) -> None:
    """Tier 0 — write via the in-process ``graphiti_core`` client.

    Imported lazily so this module imports cleanly in environments
    where ``graphiti_core`` is not installed (the CLI fallback still
    works).

    The ``Graphiti()`` no-argument constructor is the canonical entry
    point per the ``graphiti-core`` README — connection details
    (``GRAPHITI_HOST``, ``FALKORDB_HOST``, etc.) are picked up from
    environment variables. We pass the redacted payload as the
    ``episode_body`` so every text field has already been scrubbed
    before the network call.
    """
    # ``importlib`` lazy import — the ``# type: ignore`` is required
    # because ``graphiti_core`` is an optional runtime dependency that
    # is not declared in the project's static type-check environment.
    import importlib

    graphiti_core = importlib.import_module("graphiti_core")
    client = graphiti_core.Graphiti()  # type: ignore[attr-defined]
    add_episode = getattr(client, "add_episode", None)
    if add_episode is None:  # pragma: no cover — API drift guard
        raise GraphitiUnavailableError(
            "graphiti_core.Graphiti() does not expose add_episode; "
            "MCP tier is unusable."
        )
    await add_episode(
        name=episode_name,
        episode_body=json.dumps(payload),
        group_id=group_id,
        source="json",
    )


async def _write_via_cli(
    payload: dict[str, Any], group_id: str, episode_name: str
) -> None:
    """Tier 1 — write via ``guardkit graphiti add-context`` subprocess.

    The redacted payload is passed as ``--json <serialised>``. The
    subcommand starts with ``"graphiti "``, so the resolver-skip
    branch in ``forge.adapters.guardkit.run`` fires and no
    ``--context`` flag is threaded (DDR-005).

    On non-zero exit the captured stderr is wrapped in
    :class:`GraphitiCLIError` so callers (and the fire-and-forget
    log line) can attribute the failure.
    """
    proc = await asyncio.create_subprocess_exec(
        "guardkit",
        "graphiti",
        "add-context",
        "--group",
        group_id,
        "--name",
        episode_name,
        "--json",
        json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode(errors="replace").strip()
        raise GraphitiCLIError(
            f"guardkit graphiti add-context exited " f"{proc.returncode}: {stderr_text}"
        )


async def _dispatch_write(
    payload: dict[str, Any], group_id: str, episode_name: str
) -> None:
    """Choose the highest-tier available backend and await the write.

    Tests patch this function directly to inject a fake backend
    without going through the availability checks.
    """
    if _mcp_backend_available():
        await _write_via_mcp(payload, group_id, episode_name)
        return
    if _cli_backend_available():
        await _write_via_cli(payload, group_id, episode_name)
        return
    raise GraphitiUnavailableError(
        "Neither MCP (graphiti_core import) nor CLI (guardkit on "
        "PATH) is available; cannot write pipeline-history entity."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def write_entity(entity: PipelineHistoryEntity, group_id: str) -> None:
    """Redact every text field and write the entity to Graphiti.

    The "synchronous-failure" variant — every error propagates so the
    caller can decide whether to retry, queue, or fail. Used by
    reconcile-backfill (TASK-IC-004) where the caller MUST know
    whether the write succeeded.

    Args:
        entity: A pydantic-validated pipeline-history entity (see
            :data:`PipelineHistoryEntity` for the union of accepted
            types). Must be a :class:`pydantic.BaseModel` instance.
        group_id: Graphiti group_id, e.g. ``"forge_pipeline_history"``
            or ``"forge_calibration_history"``. Must be a non-empty
            string.

    Raises:
        TypeError: ``entity`` is not a pydantic ``BaseModel``.
        ValueError: ``group_id`` is not a non-empty string.
        GraphitiUnavailableError: No backend tier is available.
        GraphitiCLIError: The CLI tier exited non-zero.
        Exception: Any error raised by the MCP backend is propagated
            verbatim; this surface is intentional so the
            reconcile-backfill caller sees the underlying cause.
    """
    if not isinstance(entity, BaseModel):
        raise TypeError(
            "write_entity expected a pydantic BaseModel, got "
            f"{type(entity).__name__}"
        )
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("group_id must be a non-empty string")

    payload = _entity_to_redacted_payload(entity)
    entity_type = type(entity).__name__
    # The episode name is "<EntityType>:<entity_id>" so the Graphiti
    # operator can locate the episode by either coordinate. The
    # entity_id is already a string after ``mode="json"`` serialisation.
    episode_name = f"{entity_type}:{payload.get('entity_id', '<unknown>')}"

    await _dispatch_write(payload, group_id, episode_name)


def fire_and_forget_write(entity: PipelineHistoryEntity, group_id: str) -> None:
    """Schedule :func:`write_entity` and return immediately.

    The "resilience" variant — the function never raises to the
    caller, even when ``entity`` validation, payload serialisation,
    or the underlying transport fails. Errors are routed through
    :func:`_log_failure` which emits a structured log line carrying
    every field downstream alerting needs to correlate the failure
    with the originating pipeline run.

    Dispatch strategy:

    * If an asyncio loop is currently running in the calling thread
      (the typical pipeline path), the coroutine is scheduled on it
      via :func:`asyncio.ensure_future`. A done-callback then
      surfaces any exception to the logger when the future settles.
    * If no loop is running (sync entry point — e.g. CLI tools or
      synchronous calibration replay), a daemon background thread
      is spawned that owns its own event loop via
      :func:`asyncio.run`. This is the "thread-pool variant"
      called out in the task implementation notes.

    Args:
        entity: A pipeline-history entity. If validation or
            serialisation fails synchronously here, the failure is
            logged — never raised.
        group_id: Graphiti group_id (see :func:`write_entity`).
    """
    # Best-effort metadata extraction — used only for log enrichment,
    # so we never let getattr/repr failures escape.
    entity_id = getattr(entity, "entity_id", None)
    entity_type = type(entity).__name__

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop in the current thread — fall through to the
        # background-thread path. ``RuntimeError`` is the documented
        # signal from ``get_running_loop`` for "not running".
        loop = None

    if loop is not None:
        try:
            future = asyncio.ensure_future(write_entity(entity, group_id), loop=loop)
        except Exception as exc:  # noqa: BLE001 — boundary swallow
            # ``ensure_future`` itself can raise (e.g. if ``entity``
            # is the wrong type and ``write_entity`` raises before
            # awaiting). Coerce the synchronous failure into the
            # same structured log shape so callers see one telemetry
            # surface regardless of when the failure happened.
            _log_failure(
                exc,
                entity_id=entity_id,
                entity_type=entity_type,
                group_id=group_id,
            )
            return
        future.add_done_callback(
            lambda fut: _log_future_outcome(
                fut,
                entity_id=entity_id,
                entity_type=entity_type,
                group_id=group_id,
            )
        )
        return

    def _runner() -> None:
        """Daemon-thread runner for the sync-entry-point path."""
        try:
            asyncio.run(write_entity(entity, group_id))
        except Exception as exc:  # noqa: BLE001 — boundary swallow
            _log_failure(
                exc,
                entity_id=entity_id,
                entity_type=entity_type,
                group_id=group_id,
            )

    threading.Thread(
        target=_runner,
        name="forge-graphiti-fire-and-forget",
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Failure logging
# ---------------------------------------------------------------------------


def _log_future_outcome(
    future: "asyncio.Future[Any]",
    *,
    entity_id: Any,
    entity_type: str,
    group_id: str,
) -> None:
    """Done-callback that funnels any future failure into the logger.

    The callback runs on the event loop's thread when the future
    settles; we do not re-raise — the caller has already moved on.
    """
    try:
        exc = future.exception()
    except asyncio.CancelledError as cancelled:
        # The future was cancelled before completion. Log the
        # cancellation so it's visible in telemetry; downstream
        # alerting can de-prioritise CancelledError if desired.
        _log_failure(
            cancelled,
            entity_id=entity_id,
            entity_type=entity_type,
            group_id=group_id,
        )
        return
    if exc is not None:
        _log_failure(
            exc,
            entity_id=entity_id,
            entity_type=entity_type,
            group_id=group_id,
        )


def _log_failure(
    exc: BaseException,
    *,
    entity_id: Any,
    entity_type: str,
    group_id: str,
) -> None:
    """Emit the structured failure log line.

    The ``extra`` dict carries the indexable fields; ``exc_info``
    carries the traceback so log aggregators with stack-trace
    rendering pick it up. We use ``logger.error`` (not
    ``logger.exception``) so the call works correctly outside a
    ``try/except`` block — ``exc_info`` is set explicitly to the
    captured exception triple.
    """
    logger.error(
        "graphiti_fire_and_forget_write_failed",
        extra={
            "entity_id": str(entity_id) if entity_id is not None else None,
            "group_id": group_id,
            "entity_type": entity_type,
            "error_class": type(exc).__name__,
            "error_message": str(exc),
        },
        exc_info=(type(exc), exc, exc.__traceback__),
    )


__all__ = [
    "GraphitiCLIError",
    "GraphitiUnavailableError",
    "PipelineHistoryEntity",
    "fire_and_forget_write",
    "write_entity",
]
