"""Production ``StreamSource`` — adapts ``langgraph_sdk.runs.join_stream``.

Referenced by :mod:`forge.lifecycle_bridge.wireup` (line 52 docstring) as
the canonical production factory for the
:class:`~forge.lifecycle_bridge.wireup.StreamSource` Protocol. The
factory was originally scoped under TASK-FRR-PEB-005 but that task's
actual delivery shipped only the :class:`TerminalPublishLedger` —
TASK-FORGE-FRR-PEBR-WIREUP closes this gap so the bridge wireup can be
composed in :func:`forge.cli._serve_production.bind_production_serve`.

What this module exposes
------------------------

* :func:`langgraph_stream_source` — factory closing over a
  ``runner_url`` and returning an async-callable that satisfies the
  :class:`StreamSource` Protocol. Each call opens a fresh
  ``langgraph_sdk`` client and returns the
  :meth:`runs.join_stream(thread_id, run_id, stream_mode="values")`
  async iterator, which the wireup's observer loop drives until a
  terminal envelope is observed or the iterator exits cleanly.

Verified against the installed ``langgraph_sdk`` 0.3.13 surface:

* ``langgraph_sdk.get_client(url=...)`` returns a
  :class:`langgraph_sdk._async.client.LangGraphClient` with a ``runs``
  attribute exposing ``join_stream(thread_id, run_id, *, stream_mode,
  ...)`` typed as ``-> AsyncIterator[StreamPart]``.

Contract for the Protocol
-------------------------

The wireup's :class:`StreamSource` Protocol contracts:
``__call__(*, feature_id, thread_id, run_id) -> AsyncIterator[StreamPart]``.
Implementations MUST NOT raise on missing/late stream starts — yielding
zero events is a legitimate "no live SSE yet" signal that the observer
treats as a clean exit (the JetStream ``ack_wait`` redelivery re-triggers
registration). When ``thread_id`` or ``run_id`` is ``None`` (the
identity provider has not yet resolved), the factory returns an empty
async iterator so the observer's reconnect loop can retry.

Transport errors raised out of the iterator (``httpx.ConnectError``,
``httpx.ReadError``, malformed JSON) are caught by the wireup's
reconnect loop (:data:`forge.lifecycle_bridge.wireup.TRANSIENT_STREAM_ERRORS`).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from forge.lifecycle_bridge.wireup import StreamSource

__all__ = ["langgraph_stream_source"]


def langgraph_stream_source(*, runner_url: str) -> StreamSource:
    """Return a :class:`StreamSource` bound to a real ``langgraph-runner`` sidecar.

    The returned callable captures ``runner_url`` and, on each
    invocation, opens a fresh ``langgraph_sdk`` client via
    :func:`langgraph_sdk.get_client` and returns
    ``client.runs.join_stream(thread_id, run_id, stream_mode="values")``.

    A new client per call is intentional — the SDK client is cheap to
    construct and tying its lifetime to the per-build observer loop
    keeps the connection scoped to that loop's reconnect/shutdown
    semantics. Sharing one client across observers would couple their
    lifetimes and complicate shutdown.

    Args:
        runner_url: Validated URL of the ``langgraph-runner`` sidecar
            (per :class:`ServeConfig`'s fail-fast guard). Forwarded to
            :func:`langgraph_sdk.get_client` unchanged.

    Returns:
        A callable conforming to
        :class:`forge.lifecycle_bridge.wireup.StreamSource`:
        ``__call__(*, feature_id, thread_id, run_id) -> AsyncIterator[StreamPart]``.
    """

    def _source(
        *,
        feature_id: str,
        thread_id: str | None,
        run_id: str | None,
    ) -> AsyncIterator[Any]:
        # Note: this is a sync def by design. It returns an
        # async-iterator *object* (already in motion, not awaited),
        # matching the StreamSource Protocol shape at
        # forge.lifecycle_bridge.wireup.StreamSource.__call__. The
        # wireup's observer drives the returned object via
        # ``async for event in self._stream_source(...)`` — never
        # ``await`` — so this function is never a coroutine.

        # Identity not yet resolved — yield zero events so the observer's
        # reconnect loop can sleep + retry without raising.
        if thread_id is None or run_id is None:
            return _empty_async_iterator()

        # Imported lazily so the module stays importable when
        # ``langgraph_sdk`` is not installed (e.g. lint runners that
        # touch the lifecycle_bridge __init__ but never call this
        # factory).
        from langgraph_sdk import get_client

        client = get_client(url=runner_url)
        return client.runs.join_stream(
            thread_id,
            run_id,
            stream_mode="values",
        )

    return _source


async def _empty_async_iterator() -> AsyncIterator[Any]:
    """Yield zero events.

    Matches the :class:`StreamSource` Protocol's "yielding zero events
    is a legitimate no-live-SSE signal" contract. The observer treats
    this as a clean exit and falls back to JetStream redelivery.
    """
    return
    yield  # unreachable, but makes this an async generator
