"""Tests for ``_build_async_tasks_identity_provider`` (TASK-FORGE-FRR-PEBR-WIREUP).

The factory returns an :data:`IdentityProvider` (per
:mod:`forge.lifecycle_bridge.wireup`) that performs hybrid resolution:

1. ``SELECT task_id FROM async_tasks WHERE feature_id = ?`` against a
   shared SQLite writer connection. ``task_id == thread_id`` per the
   FW10-005 dispatcher contract.
2. ``langgraph_sdk.get_client(url=runner_url).runs.list(thread_id,
   limit=1)`` to fetch the latest run; the provider returns
   ``(thread_id, run.run_id)``.

Six load-bearing scenarios (per TASK-REV-PEBR-003 §AC-3 enumeration):

* SQLite miss → ``None``.
* SQLite hit → SDK call with the thread_id.
* SDK returns runs → ``(thread_id, run_id)`` returned.
* SDK returns empty list → ``None``.
* SDK raises → ``None`` and a warning is logged.
* SQLite raises → ``None`` and a warning is logged.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_pool_with_async_tasks_table() -> Any:
    """Build a real ``SqliteLifecyclePersistence`` over an in-memory DB.

    The ``async_tasks`` table mirrors the production schema's columns
    we depend on: ``feature_id``, ``task_id``, and ``started_at`` (the
    provider's SELECT orders newest-first on ``started_at``). Other
    columns are omitted.
    """
    cx = sqlite3.connect(":memory:")
    cx.row_factory = sqlite3.Row
    cx.execute("""
        CREATE TABLE async_tasks (
            task_id      TEXT PRIMARY KEY,
            feature_id   TEXT NOT NULL,
            build_id     TEXT,
            correlation_id TEXT,
            started_at   TEXT
        )
        """)
    cx.commit()

    pool = MagicMock(name="pool")
    pool.connection = cx
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_identity_provider_returns_none_when_async_tasks_row_missing(
    sqlite_pool_with_async_tasks_table: Any,
) -> None:
    """No row for ``feature_id`` → ``None`` (no SDK call)."""
    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=sqlite_pool_with_async_tasks_table,
        autobuild_runner_url="http://sidecar:8124",
    )

    result = asyncio.run(provider("FEAT-MISSING", "corr-1"))
    assert result is None


def test_identity_provider_reads_thread_id_from_async_tasks_row(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row's ``task_id`` is read as ``thread_id`` and threaded into the SDK."""
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id) VALUES (?, ?, 'corr-1')",
        ("thread-XYZ", "FEAT-XYZ"),
    )
    pool.connection.commit()

    captured: dict[str, Any] = {}

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            captured["thread_id"] = thread_id
            captured["limit"] = limit
            return []  # no runs — provider returns None, SDK was called

    class _FakeClient:
        def __init__(self) -> None:
            self.runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    asyncio.run(provider("FEAT-XYZ", "corr-1"))

    assert captured == {"thread_id": "thread-XYZ", "limit": 1}


def test_identity_provider_returns_thread_id_and_latest_run_id(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK returns runs → provider returns ``(thread_id, run_id)``."""
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id) VALUES (?, ?, 'corr-1')",
        ("thread-LATEST", "FEAT-LATEST"),
    )
    pool.connection.commit()

    class _Run:
        run_id = "run-019e062a-6b8c-7be0-986c-ce9243734e22"

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            return [_Run()]

    class _FakeClient:
        runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    result = asyncio.run(provider("FEAT-LATEST", "corr-1"))
    assert result == (
        "thread-LATEST",
        "run-019e062a-6b8c-7be0-986c-ce9243734e22",
    )


def test_identity_provider_handles_dict_shaped_runs(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runs.list`` may return ``list[dict]``; provider extracts ``run_id``."""
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id) VALUES (?, ?, 'corr-1')",
        ("thread-DICT", "FEAT-DICT"),
    )
    pool.connection.commit()

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            return [{"run_id": "run-from-dict"}]

    class _FakeClient:
        runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    result = asyncio.run(provider("FEAT-DICT", "corr-1"))
    assert result == ("thread-DICT", "run-from-dict")


def test_identity_provider_returns_none_when_runs_list_empty(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK returns ``[]`` → provider returns ``None``."""
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id) VALUES (?, ?, 'corr-1')",
        ("thread-EMPTY", "FEAT-EMPTY"),
    )
    pool.connection.commit()

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            return []

    class _FakeClient:
        runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    result = asyncio.run(provider("FEAT-EMPTY", "corr-1"))
    assert result is None


def test_identity_provider_returns_none_when_sdk_raises(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SDK raises → ``None`` returned and a warning is logged."""
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id) VALUES (?, ?, 'corr-1')",
        ("thread-ERR", "FEAT-ERR"),
    )
    pool.connection.commit()

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            raise RuntimeError("sidecar unreachable")

    class _FakeClient:
        runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    with caplog.at_level(logging.WARNING, logger="forge.cli._serve_production"):
        result = asyncio.run(provider("FEAT-ERR", "corr-1"))

    assert result is None
    assert any(
        "failed to resolve" in record.getMessage() and "FEAT-ERR" in record.getMessage()
        for record in caplog.records
    )


def test_identity_provider_returns_none_when_sqlite_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLite read raises → ``None`` returned and a warning is logged."""
    pool = MagicMock(name="pool")
    pool.connection.execute.side_effect = sqlite3.Error("disk I/O error")

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    with caplog.at_level(logging.WARNING, logger="forge.cli._serve_production"):
        result = asyncio.run(provider("FEAT-ERR", "corr-1"))

    assert result is None
    assert any("SQLite read failed" in record.getMessage() for record in caplog.records)


def test_identity_provider_prefers_newest_async_tasks_row(
    sqlite_pool_with_async_tasks_table: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple rows for a feature → the newest ``started_at`` wins.

    Regression for the 2026-07-04 GB10 incident: ``async_tasks``
    accumulates one row per dispatched build, and the unordered
    ``LIMIT 1`` pinned resolution to the *oldest* row — a thread that no
    longer existed on the restarted sidecar — so identity resolution
    404-looped while the live run streamed unobserved. The oldest row
    is inserted FIRST so an unordered ``LIMIT 1`` would return it and
    fail this test.
    """
    pool = sqlite_pool_with_async_tasks_table
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id, started_at) " "VALUES (?, ?, 'corr-1', ?)",
        ("thread-STALE", "FEAT-DUP", "2026-05-15T10:13:50+00:00"),
    )
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id, started_at) " "VALUES (?, ?, 'corr-1', ?)",
        ("thread-LIVE", "FEAT-DUP", "2026-07-04T08:51:40+00:00"),
    )
    pool.connection.commit()

    captured: dict[str, Any] = {}

    class _Run:
        run_id = "run-LIVE"

    class _FakeRuns:
        async def list(self, thread_id: str, *, limit: int = 10) -> list[Any]:
            captured["thread_id"] = thread_id
            return [_Run()]

    class _FakeClient:
        def __init__(self) -> None:
            self.runs = _FakeRuns()

    import langgraph_sdk

    monkeypatch.setattr(langgraph_sdk, "get_client", lambda *, url: _FakeClient())

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool,
        autobuild_runner_url="http://sidecar:8124",
    )

    result = asyncio.run(provider("FEAT-DUP", "corr-1"))

    assert captured["thread_id"] == "thread-LIVE"
    assert result == ("thread-LIVE", "run-LIVE")


def test_stale_same_feature_row_is_a_miss_not_a_hit(
    sqlite_pool_with_async_tasks_table: Any,
) -> None:
    """FEAT-FTR regression pin (live receipt: FEAT-UDBE requeue 2026-07-28).

    A same-feature requeue overlaps the sidecar's async state-channel write
    lag: the newest EXISTING async_tasks row belongs to the PREVIOUS build.
    Resolving it made the observer replay the prior run's terminal as a
    false BuildFailed while the new build ran healthy. The provider must
    treat the stale row as a MISS (None — the wireup keeps polling) and
    resolve only when THIS dispatch's correlation_id lands.
    """
    from unittest.mock import AsyncMock, Mock, patch

    from forge.cli._serve_production import _build_async_tasks_identity_provider

    pool = sqlite_pool_with_async_tasks_table
    # The PREVIOUS build's row for the same feature (older correlation).
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id, "
        "started_at) VALUES ('thread-OLD', 'FEAT-REQ', 'corr-old', "
        "'2026-07-28T10:18:01')",
    )
    pool.connection.commit()
    provider = _build_async_tasks_identity_provider(
        sqlite_pool=pool, autobuild_runner_url="http://localhost:9"
    )

    # THIS dispatch's row has not landed yet: the stale row must NOT
    # resolve — feature-newest would have returned thread-OLD here.
    assert asyncio.run(provider("FEAT-REQ", "corr-new")) is None

    # THIS dispatch's row lands: resolution now picks the NEW thread.
    pool.connection.execute(
        "INSERT INTO async_tasks (task_id, feature_id, correlation_id, "
        "started_at) VALUES ('thread-NEW', 'FEAT-REQ', 'corr-new', "
        "'2026-07-28T10:41:06')",
    )
    pool.connection.commit()
    with patch("langgraph_sdk.get_client") as get_client:
        runs = AsyncMock()
        runs.list.return_value = [{"run_id": "run-NEW"}]
        get_client.return_value = Mock(runs=runs)
        result = asyncio.run(provider("FEAT-REQ", "corr-new"))
    assert result == ("thread-NEW", "run-NEW")
    runs.list.assert_awaited_once_with("thread-NEW", limit=1)
