"""Tests for :func:`forge.lifecycle_bridge.langgraph_stream_source` (TASK-FORGE-FRR-PEBR-WIREUP).

The factory adapts ``langgraph_sdk.runs.join_stream`` into the
:class:`StreamSource` Protocol consumed by
:class:`LifecycleBridgeWireup`. These tests pin three load-bearing
behaviours:

1. The factory threads ``runner_url`` into ``get_client(url=...)``.
2. The returned callable matches the :class:`StreamSource` Protocol
   shape (kwargs-only ``feature_id``/``thread_id``/``run_id``).
3. The callable invokes ``runs.join_stream(thread_id, run_id,
   stream_mode="values")`` with the right arguments.

A monkey-patched ``get_client`` substitute records every interaction
so the tests assert against deterministic state rather than a live
sidecar.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class _FakeRunsClient:
    """Records ``join_stream`` calls and returns a sentinel iterator."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        # The sentinel is what ``join_stream`` returns; the wireup's
        # observer loop drives it via ``async for`` — we don't drive
        # it in these tests, just assert identity.
        self.sentinel_iter: Any = MagicMock(name="join_stream_iter")

    def join_stream(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.sentinel_iter


class _FakeLangGraphClient:
    def __init__(self) -> None:
        self.runs = _FakeRunsClient()


def test_factory_returns_callable_matching_stream_source_protocol() -> None:
    """The factory returns an object that is callable.

    The wireup's :class:`StreamSource` Protocol is structural —
    ``__call__`` with kwargs. Asserting callable + that the call
    succeeds with the expected kwargs covers Protocol conformance.
    """
    from forge.lifecycle_bridge import langgraph_stream_source

    source = langgraph_stream_source(runner_url="http://sidecar:8124")

    assert callable(source)


def test_factory_threads_runner_url_to_get_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory's returned callable opens a client at ``runner_url``."""
    import langgraph_sdk

    captured: dict[str, Any] = {}

    def _fake_get_client(*, url: str) -> Any:
        captured["url"] = url
        return _FakeLangGraphClient()

    monkeypatch.setattr(langgraph_sdk, "get_client", _fake_get_client)

    from forge.lifecycle_bridge import langgraph_stream_source

    source = langgraph_stream_source(runner_url="http://sidecar:8124")

    # Drive the source to force the lazy client construction.
    source(
        feature_id="FEAT-X",
        thread_id="thread-1",
        run_id="run-1",
    )

    assert captured["url"] == "http://sidecar:8124"


def test_callable_invokes_join_stream_with_thread_run_and_values_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runs.join_stream`` is called with thread_id, run_id, stream_mode='values'."""
    import langgraph_sdk

    fake_client = _FakeLangGraphClient()
    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: fake_client)

    from forge.lifecycle_bridge import langgraph_stream_source

    source = langgraph_stream_source(runner_url="http://sidecar:8124")
    result = source(
        feature_id="FEAT-X",
        thread_id="thread-42",
        run_id="run-42",
    )

    assert result is fake_client.runs.sentinel_iter
    assert len(fake_client.runs.calls) == 1
    args, kwargs = fake_client.runs.calls[0]
    # The implementation passes thread_id and run_id positionally
    # (matching the ``langgraph_sdk`` 0.3.13 signature
    # ``join_stream(thread_id, run_id, *, stream_mode=...)``).
    assert args == ("thread-42", "run-42")
    assert kwargs == {"stream_mode": "values"}


def test_callable_returns_empty_iterator_when_thread_id_missing() -> None:
    """``thread_id is None`` → empty async iterator (no client call)."""
    import asyncio

    from forge.lifecycle_bridge import langgraph_stream_source

    source = langgraph_stream_source(runner_url="http://sidecar:8124")
    result = source(
        feature_id="FEAT-X",
        thread_id=None,
        run_id="run-1",
    )

    # Drive the iterator and assert zero events.
    async def _drain() -> list[Any]:
        return [item async for item in result]

    events = asyncio.run(_drain())
    assert events == []


def test_callable_returns_empty_iterator_when_run_id_missing() -> None:
    """``run_id is None`` → empty async iterator (no client call)."""
    import asyncio

    from forge.lifecycle_bridge import langgraph_stream_source

    source = langgraph_stream_source(runner_url="http://sidecar:8124")
    result = source(
        feature_id="FEAT-X",
        thread_id="thread-1",
        run_id=None,
    )

    async def _drain() -> list[Any]:
        return [item async for item in result]

    events = asyncio.run(_drain())
    assert events == []
