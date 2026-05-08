"""Production ``RunStateFetcher`` — adapts ``langgraph_sdk`` run + thread state.

Companion to :mod:`forge.lifecycle_bridge.stream_source`. Where ``StreamSource``
yields live SSE events for an in-flight run, ``RunStateFetcher`` reads a
finished run's terminal status plus the thread's current state values. The
:class:`~forge.lifecycle_bridge.wireup.LifecycleBridgeWireup` observer uses it
as a **fetch-on-empty fallback**: when ``runs.join_stream`` returns an empty
iterator (the canonical Signature C symptom of TASK-REV-PEBR-005 — the
placeholder-body run finished in ~16 ms before the bridge could subscribe),
the observer asks the fetcher for the run's final state and replays the
translator against a synthetic ``StreamPart`` so the canonical envelope shape
is preserved without ad-hoc payload synthesis.

Why fetch-on-empty rather than subscribe-before-dispatch
--------------------------------------------------------

The originally-scoped fix shapes (``runs.stream(...)``,
``runs.create(stream_resumable=True)``) require the run-creation path to opt
in. ``forge``'s autobuild dispatcher routes through DeepAgents'
``AsyncSubAgentMiddleware.astart_async_task`` (deepagents 0.5.6,
``deepagents/middleware/async_subagents.py:332``) which calls
``client.runs.create(...)`` with no resumability flag and no stream-mode
passthrough. Modifying that middleware is out of forge's modify-able surface,
and replacing it for autobuild dispatch is a multi-hour architectural change.

Fetch-on-empty closes the race deterministically without touching the
dispatch path:

1. Bridge observer opens ``runs.join_stream`` as before.
2. If the stream yields events, the existing translator path handles them.
3. If the stream is empty AND identity has resolved AND the run is in a
   terminal status (``success`` / ``error`` / ``interrupted``), the observer
   fetches the thread's state values and replays them through the translator.
4. Result: the consumer state advances (``ack_floor`` un-blocks) regardless
   of subscription timing.

What this module exposes
------------------------

* :func:`langgraph_run_state_fetcher` — factory closing over a ``runner_url``
  and returning an async callable that satisfies the
  :class:`~forge.lifecycle_bridge.wireup.RunStateFetcher` Protocol. Each call
  opens a fresh ``langgraph_sdk`` client (matching ``stream_source.py``'s
  per-call client lifetime), reads the run's status via ``runs.get`` and —
  iff the status is terminal — reads the thread's state values via
  ``threads.get_state``. Returns a :class:`RunStateSnapshot` carrying the
  run status and the values dict, or ``None`` when the run is still running
  / unknown / fetch errors.

Verified against the installed ``langgraph_sdk`` 0.3.13 surface:

* ``runs.get(thread_id, run_id) -> Run`` — TypedDict with ``run_id``,
  ``thread_id``, ``status`` (``RunStatus``), ``created_at``, ``updated_at``,
  ``metadata``, ``multitask_strategy``.
* ``threads.get_state(thread_id) -> ThreadState`` — TypedDict whose
  ``values`` field is the full ``StateGraph`` snapshot (the same shape the
  ``stream_mode="values"`` SSE channel would have streamed).

Contract for the Protocol
-------------------------

Implementations MUST NOT raise on any failure mode (missing run, transport
error, SDK shape drift). The wireup observer treats ``None`` as "no terminal
state available — leave the inbound un-acked and rely on JetStream
redelivery + recover_in_flight on next boot". This mirrors
:class:`~forge.lifecycle_bridge.wireup.StreamSource`'s "yielding zero events
is a legitimate clean exit" contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

__all__ = [
    "RUN_STATUS_TERMINAL",
    "RunStateFetcher",
    "RunStateSnapshot",
    "langgraph_run_state_fetcher",
]

logger = logging.getLogger(__name__)


#: ``langgraph_sdk`` ``RunStatus`` values the fetcher treats as terminal.
#:
#: When ``runs.get`` returns one of these the run is no longer streaming;
#: the bridge is safe to fetch ``threads.get_state`` and replay it through
#: the translator. Anything else (``pending`` / ``running``) means the
#: run is still in flight — the fetcher returns ``None`` and the
#: observer falls back to JetStream redelivery so the live SSE path
#: gets another chance.
RUN_STATUS_TERMINAL: frozenset[str] = frozenset(
    {"success", "error", "interrupted", "timeout"}
)


@dataclass(frozen=True, slots=True)
class RunStateSnapshot:
    """A finished run's status plus the thread's terminal state values.

    Attributes:
        status: The run's terminal :class:`langgraph_sdk.schema.RunStatus`
            string. Always one of :data:`RUN_STATUS_TERMINAL`.
        values: The thread's full ``StateGraph`` values snapshot — the
            mapping the ``stream_mode="values"`` SSE channel would have
            streamed. Mapping shape is opaque to this module; the
            :class:`~forge.lifecycle_bridge.translation.StreamEventTranslator`
            owns the schema (``async_tasks[feature_id]`` → AutobuildState).
    """

    status: str
    values: Mapping[str, Any]


#: ``async (*, feature_id, thread_id, run_id) -> RunStateSnapshot | None``.
#:
#: The wireup observer awaits this when ``_consume_with_reconnect`` returns
#: ``terminal_seen=False, ended_cleanly=True``. ``None`` means "no terminal
#: state available" — observer leaves the inbound un-acked.
RunStateFetcher = Callable[..., Awaitable["RunStateSnapshot | None"]]


def langgraph_run_state_fetcher(*, runner_url: str) -> RunStateFetcher:
    """Return a :class:`RunStateFetcher` bound to a real ``langgraph-runner`` sidecar.

    The returned callable, on each invocation, opens a fresh
    ``langgraph_sdk`` client via :func:`langgraph_sdk.get_client`, calls
    ``client.runs.get(thread_id, run_id)`` and — iff the status is
    terminal — calls ``client.threads.get_state(thread_id)`` to read the
    full state values.

    A new client per call is intentional (mirrors
    :func:`forge.lifecycle_bridge.stream_source.langgraph_stream_source`):
    the SDK client is cheap to construct, and tying its lifetime to the
    per-build observer call keeps connection scope tight.

    Args:
        runner_url: Validated URL of the ``langgraph-runner`` sidecar
            (per :class:`ServeConfig`'s fail-fast guard). Forwarded to
            :func:`langgraph_sdk.get_client` unchanged.

    Returns:
        A callable conforming to :class:`RunStateFetcher`:
        ``async (*, feature_id, thread_id, run_id) -> RunStateSnapshot | None``.
    """

    async def _fetcher(
        *,
        feature_id: str,
        thread_id: str | None,
        run_id: str | None,
    ) -> RunStateSnapshot | None:
        # Identity not yet resolved — nothing to fetch. Observer will
        # leave the inbound un-acked; JetStream redelivery is the
        # recovery path.
        if thread_id is None or run_id is None:
            return None

        # Imported lazily so the module stays importable when
        # ``langgraph_sdk`` is not installed (e.g. lint runners that
        # touch the lifecycle_bridge __init__ but never call this
        # factory). Mirrors stream_source.py's lazy import discipline.
        try:
            from langgraph_sdk import get_client
        except ImportError as exc:  # pragma: no cover - production has it
            logger.warning(
                "langgraph_run_state_fetcher: langgraph_sdk is not "
                "installed (%s); fetch-on-empty fallback disabled for "
                "feature_id=%s",
                exc,
                feature_id,
            )
            return None

        client = get_client(url=runner_url)

        try:
            run = await client.runs.get(thread_id, run_id)
        except Exception as exc:  # noqa: BLE001 — SDK raises untyped errors
            logger.warning(
                "langgraph_run_state_fetcher: runs.get raised (%s) for "
                "feature_id=%s thread_id=%s run_id=%s; treating as "
                "no-snapshot and leaving inbound un-acked",
                exc,
                feature_id,
                thread_id,
                run_id,
            )
            return None

        status = _run_status(run)
        if status not in RUN_STATUS_TERMINAL:
            # Run is still running / pending / unknown. The bridge's
            # JetStream redelivery path is the right recovery — letting
            # this fall through risks acking a run that has not yet
            # produced its terminal envelope, which would corrupt the
            # ack_floor invariant under retry.
            logger.debug(
                "langgraph_run_state_fetcher: run not terminal "
                "(status=%s) for feature_id=%s thread_id=%s run_id=%s; "
                "leaving inbound un-acked",
                status,
                feature_id,
                thread_id,
                run_id,
            )
            return None

        try:
            thread_state = await client.threads.get_state(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "langgraph_run_state_fetcher: threads.get_state raised "
                "(%s) for feature_id=%s thread_id=%s; treating as "
                "no-snapshot",
                exc,
                feature_id,
                thread_id,
            )
            return None

        values = _extract_values(thread_state)
        if values is None:
            logger.warning(
                "langgraph_run_state_fetcher: thread state has no "
                "'values' field for feature_id=%s thread_id=%s; "
                "treating as no-snapshot",
                feature_id,
                thread_id,
            )
            return None

        return RunStateSnapshot(status=status, values=values)

    return _fetcher


def _run_status(run: Any) -> str:
    """Extract the terminal status string from a ``Run`` SDK response.

    The SDK's ``Run`` is a ``TypedDict`` so it presents as a ``Mapping``
    in practice, but we tolerate dataclass-shaped responses too — the
    same defensive pattern :func:`_build_async_tasks_identity_provider`
    in ``_serve_production.py`` uses against SDK-shape drift.
    """
    if isinstance(run, Mapping):
        status = run.get("status")
    else:
        status = getattr(run, "status", None)
    return str(status) if status is not None else ""


def _extract_values(thread_state: Any) -> Mapping[str, Any] | None:
    """Pull the ``values`` dict out of a ``ThreadState`` SDK response.

    The SDK's ``ThreadState`` is a ``TypedDict`` with ``values`` carrying
    the full ``StateGraph`` snapshot. Same Mapping-vs-dataclass tolerance
    as :func:`_run_status`.
    """
    if isinstance(thread_state, Mapping):
        values = thread_state.get("values")
    else:
        values = getattr(thread_state, "values", None)
    if not isinstance(values, Mapping):
        return None
    return values
