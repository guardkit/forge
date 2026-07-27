"""Tests for the ``forge serve`` production-wiring wrapper (TASK-FIX-F010).

Each ``Test*`` class mirrors one acceptance criterion of TASK-FIX-F010 so
the criterion → verifier mapping stays explicit. The wrapper module
:mod:`forge.cli._serve_production` is the single seam the smoke tests
monkeypatch to bypass the production deps graph; these tests cover its
real behaviour against an in-memory SQLite handle.

AAA pattern throughout. ``tmp_path`` fixtures keep filesystem side
effects test-local. ``_reset_for_tests`` is invoked between every test
via an autouse fixture so the module-level idempotency state cannot leak
across cases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeStartAsyncTaskTool:
    """Minimal stand-in for the LangChain ``StructuredTool`` shape.

    Exposes ``.name`` (used by ``_resolve_async_task_starter``'s
    by-name lookup), ``.func`` (legacy sync path), and ``.coroutine``
    (the async path used by production per TASK-FORGE-FRR-F010G). Both
    callables are no-op placeholders — none of the tests in this module
    actually drive the starter; they only assert composition-time
    behaviour. See ``tests/forge/test_serve_async_task_starter.py``
    for the unit coverage of the adapter's translation behaviour.
    """

    def __init__(self, name: str = "start_async_task") -> None:
        self.name = name

    def func(
        self, *, description: str, subagent_type: str, runtime: Any
    ) -> Any:  # pragma: no cover - placeholder, never invoked by these tests
        raise AssertionError(
            "_FakeStartAsyncTaskTool.func was invoked unexpectedly; the "
            "tests in this module exercise composition only, not the "
            "adapter's translation path"
        )

    async def coroutine(
        self, *, description: str, subagent_type: str, runtime: Any
    ) -> Any:  # pragma: no cover - placeholder, never invoked by these tests
        raise AssertionError(
            "_FakeStartAsyncTaskTool.coroutine was invoked unexpectedly; "
            "the tests in this module exercise composition only, not "
            "the adapter's translation path"
        )


class _FakeMiddleware:
    """Stand-in for :class:`AsyncSubAgentMiddleware` exposing a tools tuple."""

    def __init__(self, tool_names: tuple[str, ...] = ()) -> None:
        self.tools = tuple(_FakeStartAsyncTaskTool(n) for n in tool_names)


@pytest.fixture(autouse=True)
def _reset_binding_state() -> None:
    """Reset the wrapper's module-level binding and ``serve.compose_dispatch_chain``.

    Several tests in this module rebind
    :data:`forge.cli.serve.compose_dispatch_chain` to a non-awaitable
    sentinel; without an explicit reset the rebind would leak into
    sibling test modules whose ``_run_serve`` invocations await the seam.
    """
    from forge.cli import _serve_production as serve_production
    from forge.cli import serve as serve_module

    original_seam = serve_module.compose_dispatch_chain
    serve_production._reset_for_tests()
    yield
    serve_production._reset_for_tests()
    serve_module.compose_dispatch_chain = original_seam


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a deterministic SQLite path under the test's tmp dir."""
    return tmp_path / "forge.db"


@pytest.fixture
def serve_config(tmp_db_path: Path):
    """Production-shaped ``ServeConfig`` fixture.

    TASK-FORGE-FRR-F010J: ``bind_production_serve`` fail-fasts at
    Step 1.5 when ``autobuild_runner_url`` is missing/empty. This
    fixture sets the URL to a stub sidecar address so existing
    AC-2/AC-3/.../AC-7 tests can keep exercising
    ``bind_production_serve`` end-to-end. The missing-URL case is
    covered by ``TestF010JBindProductionServeFailsFastOnMissingUrl``
    below via the ``serve_config_without_runner_url`` fixture.
    """
    from forge.cli._serve_config import ServeConfig

    return ServeConfig(
        db_path=tmp_db_path,
        autobuild_runner_url="http://forge-autobuild-runner:8124",
    )


@pytest.fixture
def serve_config_without_runner_url(tmp_db_path: Path):
    """``ServeConfig`` with ``autobuild_runner_url=None``.

    TASK-FORGE-FRR-F010J: feeds
    ``TestF010JBindProductionServeFailsFastOnMissingUrl`` so the
    fail-fast guard at Step 1.5 of ``bind_production_serve`` is
    exercised. Production-shaped tests should use the ``serve_config``
    fixture instead.
    """
    from forge.cli._serve_config import ServeConfig

    return ServeConfig(db_path=tmp_db_path, autobuild_runner_url=None)


@pytest.fixture
def fake_forge_config() -> Any:
    """Cheap stand-in for :class:`ForgeConfig` — only identity matters here."""
    return MagicMock(name="ForgeConfig")


# ---------------------------------------------------------------------------
# AC-2 — middleware is constructed eagerly inside the wrapper
# ---------------------------------------------------------------------------


class TestEagerMiddlewareConstruction:
    """AC-2: ``_build_async_subagent_middleware`` is invoked exactly once."""

    def test_bind_production_serve_constructs_middleware_eagerly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        middleware_factory = MagicMock(
            return_value=_FakeMiddleware(tool_names=("start_async_task",))
        )
        monkeypatch.setattr(
            serve_module, "_build_async_subagent_middleware", middleware_factory
        )

        # Avoid touching the real dispatch-chain factory; it would try
        # to import the deps graph.
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: lambda client: None,
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        # TASK-FORGE-FRR-F010J: the factory now receives the
        # ``autobuild_runner_url`` from the production-shaped fixture
        # — assert it's called exactly once with the expected kwarg.
        middleware_factory.assert_called_once_with(
            autobuild_runner_url="http://forge-autobuild-runner:8124"
        )


# ---------------------------------------------------------------------------
# AC-2 — compose_dispatch_chain is rebound after the wrapper runs
# ---------------------------------------------------------------------------


class TestComposeDispatchChainRebind:
    """AC-2: ``compose_dispatch_chain`` is rebound away from the no-op default."""

    def test_bind_production_serve_rebinds_compose_dispatch_chain(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        original_default = serve_module._default_compose_dispatch_chain
        # Reset the seam to the documented default so the assertion is
        # meaningful even if a sibling test left it rebound.
        serve_module.compose_dispatch_chain = original_default

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        sentinel_composer = MagicMock(name="composer")
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: sentinel_composer,
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        assert serve_module.compose_dispatch_chain is sentinel_composer
        assert serve_module.compose_dispatch_chain is not original_default


# ---------------------------------------------------------------------------
# AC-3 — idempotency: previous SQLite writer connection is closed cleanly
# ---------------------------------------------------------------------------


class TestIdempotency:
    """AC-3: re-binding closes the previous SQLite connection cleanly."""

    def test_bind_production_serve_is_idempotent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        # TASK-FORGE-FRR-PEBR-WIREUP — bind_production_serve now
        # constructs a BridgeRegistry whose ``isinstance`` check
        # requires a real :class:`sqlite3.Connection`. Wrap each real
        # connection with a thin proxy that records close() calls;
        # the proxy passes ``isinstance(sqlite3.Connection)`` because
        # we register it as a virtual subclass-like by inheriting the
        # real connection class via __class__ assignment is not
        # possible — instead, monkey-patch the wrapper's
        # ``_close_previous_connection_quietly`` so the assertion
        # shape (``connections[0].close.assert_called_once``) survives.
        # Track each real connection alongside a recording proxy so
        # the existing assertion shape (``connections[i].close.*``)
        # survives. ``id()`` keys the side-table because real
        # ``sqlite3.Connection`` instances do not accept arbitrary
        # attributes.
        connections: list[Any] = []
        proxy_by_id: dict[int, Any] = {}

        class _ConnectionProxy:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real
                self.close = MagicMock(wraps=real.close)

        def _fake_connect_writer(db_path: Path) -> Any:
            real = sqlite3.connect(":memory:")
            proxy = _ConnectionProxy(real)
            connections.append(proxy)
            proxy_by_id[id(real)] = proxy
            return real

        original_close_quietly = serve_production._close_previous_connection_quietly

        def _patched_close_quietly(previous: Any) -> None:
            cx = previous.connection
            proxy = proxy_by_id.get(id(cx))
            if proxy is not None:
                proxy.close()
            else:
                original_close_quietly(previous)

        monkeypatch.setattr(serve_production, "connect_writer", _fake_connect_writer)
        monkeypatch.setattr(
            serve_production,
            "_close_previous_connection_quietly",
            _patched_close_quietly,
        )

        def _fake_pool(**kw: Any) -> Any:
            pool = MagicMock(name="pool")
            pool.connection = kw["connection"]
            return pool

        monkeypatch.setattr(serve_production, "SqliteLifecyclePersistence", _fake_pool)
        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: lambda client: None,
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)
        # First call must not have closed anything yet.
        assert connections[0].close.call_count == 0

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        assert len(connections) == 2
        # The first connection must have been closed exactly once on
        # the rebind.
        connections[0].close.assert_called_once()
        # The second connection is the live one — it must remain open.
        connections[1].close.assert_not_called()

    def test_bind_production_serve_swallows_close_errors_on_rebind(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        # AC-3 cont'd — a stale handle whose close raises must not crash
        # the rebind path.
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        # TASK-FORGE-FRR-PEBR-WIREUP — real connection so the
        # BridgeRegistry isinstance() check passes; the rebind close
        # path is monkey-patched to raise.
        def _fake_connect_writer(db_path: Path) -> Any:
            return sqlite3.connect(":memory:")

        monkeypatch.setattr(serve_production, "connect_writer", _fake_connect_writer)

        def _patched_close_quietly(previous: Any) -> None:
            # Simulate the legacy "stale handle close raises"
            # behaviour the test pins, but swallow it like the real
            # helper does so the rebind path does not crash.
            try:
                raise sqlite3.Error("already closed")
            except sqlite3.Error:
                pass

        monkeypatch.setattr(
            serve_production,
            "_close_previous_connection_quietly",
            _patched_close_quietly,
        )

        def _fake_pool(**kw: Any) -> Any:
            pool = MagicMock(name="pool")
            pool.connection = kw["connection"]
            return pool

        monkeypatch.setattr(serve_production, "SqliteLifecyclePersistence", _fake_pool)
        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: lambda client: None,
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)
        # No raise on rebind despite the prior connection's close error.
        serve_production.bind_production_serve(serve_config, fake_forge_config)


# ---------------------------------------------------------------------------
# AC-3 — db_path.parent is auto-created
# ---------------------------------------------------------------------------


class TestDbParentDirectoryAutoCreate:
    """AC-3: ``db_path.parent`` is created when missing."""

    def test_bind_production_serve_creates_db_parent_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli._serve_config import ServeConfig
        from forge.cli import serve as serve_module

        nested = tmp_path / "nested" / "deeper" / "forge.db"
        assert not nested.parent.exists()
        # TASK-FORGE-FRR-F010J: ``bind_production_serve`` fail-fasts at
        # Step 1.5 when ``autobuild_runner_url`` is missing; this test
        # exercises the Step 2 mkdir behaviour, so the URL must be set
        # for the wrapper to reach Step 2.
        config = ServeConfig(
            db_path=nested,
            autobuild_runner_url="http://forge-autobuild-runner:8124",
        )

        # TASK-FORGE-FRR-PEBR-WIREUP — bind_production_serve now
        # constructs a BridgeRegistry whose ``isinstance`` check
        # rejects MagicMock(spec=sqlite3.Connection); use a real
        # in-memory connection so Step 6.5 can construct the wireup
        # parts.
        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            lambda db_path: sqlite3.connect(":memory:"),
        )

        def _fake_pool(**kw: Any) -> Any:
            pool = MagicMock(name="pool")
            pool.connection = kw["connection"]
            return pool

        monkeypatch.setattr(serve_production, "SqliteLifecyclePersistence", _fake_pool)
        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: lambda client: None,
        )

        serve_production.bind_production_serve(config, fake_forge_config)

        assert nested.parent.exists()
        assert nested.parent.is_dir()


# ---------------------------------------------------------------------------
# AC-2 — missing forge_config raises ValueError
# ---------------------------------------------------------------------------


class TestRaisesOnMissingForgeConfig:
    """AC-2: ``forge_config=None`` raises :class:`ValueError`."""

    def test_bind_production_serve_raises_on_missing_forge_config(
        self, serve_config
    ) -> None:
        from forge.cli import _serve_production as serve_production

        with pytest.raises(ValueError, match="forge_config"):
            serve_production.bind_production_serve(serve_config, None)


# ---------------------------------------------------------------------------
# AC-2 — missing start_async_task tool raises RuntimeError
# ---------------------------------------------------------------------------


class TestRaisesOnMissingAsyncTaskStarterTool:
    """AC-2: middleware without ``start_async_task`` is a fail-fast at boot."""

    def test_bind_production_serve_raises_on_missing_async_task_starter_tool(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        # Middleware exposes other tool names but NOT start_async_task.
        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(
                tool_names=("check_async_task", "list_async_tasks")
            ),
        )
        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            lambda db_path: MagicMock(spec=sqlite3.Connection),
        )
        monkeypatch.setattr(
            serve_production,
            "SqliteLifecyclePersistence",
            lambda **kw: MagicMock(name="pool"),
        )

        with pytest.raises(RuntimeError, match="start_async_task"):
            serve_production.bind_production_serve(serve_config, fake_forge_config)


# ---------------------------------------------------------------------------
# AC-2 — async_task_starter is threaded through to bind_production_dispatch_chain
# ---------------------------------------------------------------------------


class TestAsyncTaskStarterThreading:
    """AC-2: ``async_task_starter`` derived from middleware is forwarded."""

    def test_bind_production_serve_threads_async_task_starter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        starter_tool = _FakeStartAsyncTaskTool("start_async_task")

        class _MiddlewareWithKnownStarter:
            tools = (
                _FakeStartAsyncTaskTool("check_async_task"),
                starter_tool,
                _FakeStartAsyncTaskTool("list_async_tasks"),
            )

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _MiddlewareWithKnownStarter(),
        )
        # TASK-FORGE-FRR-PEBR-WIREUP — real connection so the
        # BridgeRegistry isinstance() check inside Step 6.5 passes.
        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            lambda db_path: sqlite3.connect(":memory:"),
        )
        sentinel_pool = MagicMock(name="pool")

        def _fake_pool(**kw: Any) -> Any:
            sentinel_pool.connection = kw["connection"]
            return sentinel_pool

        monkeypatch.setattr(serve_production, "SqliteLifecyclePersistence", _fake_pool)

        captured: dict[str, Any] = {}

        def _capture(**kwargs: Any):
            captured.update(kwargs)
            return lambda client: None

        monkeypatch.setattr(serve_module, "bind_production_dispatch_chain", _capture)

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        assert captured["forge_config"] is fake_forge_config
        assert captured["sqlite_pool"] is sentinel_pool
        # TASK-FORGE-FRR-F010E: the resolver now wraps the raw
        # StructuredTool in a _StructuredToolAsyncTaskStarter adapter
        # before threading it through to bind_production_dispatch_chain.
        # The threading invariant is that the adapter is shaped against
        # the AsyncTaskStarter Protocol (so the dispatcher's call site
        # at autobuild_async.py:473 succeeds) AND that the adapter wraps
        # the very tool we put on the middleware (so the dispatch
        # actually launches the resolved subagent, not some other
        # arbitrary tool).
        from forge.cli._serve_async_task_starter import (
            _StructuredToolAsyncTaskStarter,
        )
        from forge.pipeline.dispatchers.autobuild_async import (
            AsyncTaskStarter,
        )

        threaded = captured["async_task_starter"]
        assert isinstance(threaded, _StructuredToolAsyncTaskStarter)
        assert isinstance(threaded, AsyncTaskStarter)
        assert threaded._tool is starter_tool


# ---------------------------------------------------------------------------
# AC-4 — ServeConfig.from_env honours FORGE_DB_PATH
# ---------------------------------------------------------------------------


class TestServeConfigDbPath:
    """AC-4: ``ServeConfig`` extends with ``db_path`` + ``FORGE_DB_PATH``."""

    def test_serve_config_from_env_honours_forge_db_path(self) -> None:
        from forge.cli._serve_config import ServeConfig

        config = ServeConfig.from_env({"FORGE_DB_PATH": "/tmp/x.db"})
        assert config.db_path == Path("/tmp/x.db")

    def test_serve_config_from_env_default_db_path_expands_home(self) -> None:
        from forge.cli._serve_config import ServeConfig

        config = ServeConfig.from_env({})
        assert config.db_path == Path("~/.forge/forge.db").expanduser()
        # And ~ must have been expanded — no leading tilde survives.
        assert "~" not in str(config.db_path)

    def test_serve_config_from_env_expands_home_in_explicit_value(self) -> None:
        from forge.cli._serve_config import ServeConfig

        config = ServeConfig.from_env({"FORGE_DB_PATH": "~/forge/foo.db"})
        # ``~`` must be expanded; the resolved path equals the explicit
        # expansion of the input.
        assert config.db_path == Path("~/forge/foo.db").expanduser()
        assert "~" not in str(config.db_path)

    def test_serve_config_default_construction_yields_default_db_path(self) -> None:
        from forge.cli._serve_config import ServeConfig

        config = ServeConfig()
        assert config.db_path == Path("~/.forge/forge.db").expanduser()


# ---------------------------------------------------------------------------
# TASK-FORGE-FRR-F010J — bind_production_serve fail-fasts on missing URL
# ---------------------------------------------------------------------------


class TestF010JBindProductionServeFailsFastOnMissingUrl:
    """TASK-FORGE-FRR-F010J AC-3: ``bind_production_serve`` fails fast
    when ``autobuild_runner_url`` is missing.

    The in-process ASGI fallback path
    (``langgraph_sdk.get_client(url=None)`` →
    ``ASGITransport(app=None)``) raises ``'NoneType' object is not
    callable`` on every dispatch in the current forge venv (the F010H
    investigation confirmed ``langgraph_api`` is not installed). F010I
    picked Option B.1 (sidecar URL); this guard makes the missing-URL
    case fail at boot with an actionable error message — naming the
    env var and the F010I/J task IDs — rather than failing at first
    build dispatch with a low-level transport exception.

    The guard MUST fire BEFORE any filesystem / database resource is
    allocated (no orphan SQLite handle to leak), so the fail-fast
    invariant is verifiable: a recording stand-in for
    ``connect_writer`` MUST NOT be reached.
    """

    def test_bind_production_serve_raises_value_error_on_missing_autobuild_runner_url(
        self,
        serve_config_without_runner_url,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production

        with pytest.raises(ValueError, match="autobuild_runner_url"):
            serve_production.bind_production_serve(
                serve_config_without_runner_url, fake_forge_config
            )

    def test_bind_production_serve_error_message_references_env_var_and_review_id(
        self,
        serve_config_without_runner_url,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production

        with pytest.raises(ValueError) as exc_info:
            serve_production.bind_production_serve(
                serve_config_without_runner_url, fake_forge_config
            )

        message = str(exc_info.value)
        # Operator-actionable: name the env var and the F010I/J task
        # IDs so the operator can grep the runbook for context.
        assert "FORGE_AUTOBUILD_RUNNER_URL" in message
        assert "F010I" in message or "F010J" in message

    def test_bind_production_serve_fail_fast_does_not_open_sqlite_writer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config_without_runner_url,
        fake_forge_config,
    ) -> None:
        """The fail-fast guard must run BEFORE Step 3 (connect_writer).

        Asserting that ``connect_writer`` is never called is the
        signal that no orphan SQLite handle was created when the
        guard fired. If the guard fired AFTER Step 3, this
        monkeypatched stand-in would record the call and the test
        would fail with the embedded AssertionError.
        """
        from forge.cli import _serve_production as serve_production

        connect_writer_calls: list[Any] = []

        def _recording_connect_writer(*args: Any, **kwargs: Any) -> Any:
            connect_writer_calls.append((args, kwargs))
            raise AssertionError(
                "connect_writer was reached despite the fail-fast guard "
                "for autobuild_runner_url=None — the guard must fire "
                "before any resource is allocated (TASK-FORGE-FRR-F010J)"
            )

        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            _recording_connect_writer,
        )

        with pytest.raises(ValueError, match="autobuild_runner_url"):
            serve_production.bind_production_serve(
                serve_config_without_runner_url, fake_forge_config
            )

        assert connect_writer_calls == []

    def test_bind_production_serve_rejects_empty_string_url(
        self,
        tmp_db_path: Path,
        fake_forge_config,
    ) -> None:
        """Empty-string ``autobuild_runner_url`` is rejected.

        Defensive against ``FORGE_AUTOBUILD_RUNNER_URL=`` (env var
        set to empty string) — the ``not config.autobuild_runner_url``
        guard treats both ``None`` and ``""`` as "no URL".
        """
        from forge.cli import _serve_production as serve_production
        from forge.cli._serve_config import ServeConfig

        config = ServeConfig(db_path=tmp_db_path, autobuild_runner_url="")

        with pytest.raises(ValueError, match="autobuild_runner_url"):
            serve_production.bind_production_serve(config, fake_forge_config)


# ---------------------------------------------------------------------------
# TASK-FORGE-FRR-F010J — bind_production_serve threads URL into middleware
# ---------------------------------------------------------------------------


class TestF010JBindProductionServeThreadsAutobuildRunnerUrl:
    """TASK-FORGE-FRR-F010J AC-2 (production wiring): the URL stored on
    ``ServeConfig.autobuild_runner_url`` is forwarded to
    ``_build_async_subagent_middleware`` so the
    ``AsyncSubAgent`` registration carries it.

    This is the Step 5 wiring inside ``bind_production_serve``. The
    factory-level test that the URL ends up in the spec dict is in
    ``tests/forge/test_serve_async_task_starter.py``.
    """

    def test_bind_production_serve_passes_autobuild_runner_url_to_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        captured: dict[str, Any] = {}

        def _recording_factory(
            *,
            autobuild_runner_url: str | None = None,
        ) -> Any:
            captured["autobuild_runner_url"] = autobuild_runner_url
            return _FakeMiddleware(tool_names=("start_async_task",))

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            _recording_factory,
        )
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: lambda client: None,
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        # The fixture sets autobuild_runner_url to the stub sidecar URL;
        # the wrapper MUST thread it through to the factory.
        assert captured["autobuild_runner_url"] == "http://forge-autobuild-runner:8124"


# ---------------------------------------------------------------------------
# TASK-FORGE-FRR-PEBR-WIREUP — Gap PEBR-WIREUP regression-protection seam tests
# ---------------------------------------------------------------------------


class TestLifecycleBridgeWireupComposition:
    """Gap PEBR-WIREUP regression-protection seam tests.

    Pinned by the 2026-05-08 jarvis runbook walkthrough on GB10
    (correlation_id=5673965b-e302-4a10-89cb-ceb430e64995). Before the
    fix, ``bind_production_serve`` did not compose any LifecycleBridge /
    LifecycleBridgeWireup / TerminalPublishLedger; the daemon's deps
    composer logged ``ack_bridge=deferred (TASK-FRR-PEB-002)``,
    ``terminal_publish_ledger=deferred (TASK-FRR-PEB-005)`` on every
    boot, no outbound lifecycle envelopes reached JetStream, and the
    inbound build-queued message was redelivered every 30s without
    ever being acked. See TASK-REV-PEBR-003 for the full diagnosis.

    Two complementary tests:

    * threading-capture: kwargs into ``bind_production_dispatch_chain``
      include ``bridge_wireup_parts``, and that the parts carry a
      :class:`TerminalPublishLedger` and an
      :class:`IdentityProvider`.
    * boot-log: drive the rebound ``compose_dispatch_chain(client)``
      with a fake NATS client and assert the deps composer's log line
      reports ``ack_bridge=wired`` and ``terminal_publish_ledger=wired``
      (NOT ``deferred``).
    """

    def test_bind_production_serve_threads_register_ack_handle_and_terminal_publish_ledger(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        """Threading-capture: bridge_wireup_parts reach the composer."""
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            lambda db_path: sqlite3.connect(":memory:"),
        )

        def _fake_pool(**kw: Any) -> Any:
            pool = MagicMock(name="pool")
            pool.connection = kw["connection"]
            return pool

        monkeypatch.setattr(serve_production, "SqliteLifecyclePersistence", _fake_pool)

        captured: dict[str, Any] = {}

        def _capture(**kwargs: Any):
            captured.update(kwargs)
            return lambda client: None

        monkeypatch.setattr(serve_module, "bind_production_dispatch_chain", _capture)

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        # bridge_wireup_parts must be threaded through with ALL the
        # SQLite-bound dependencies the closure needs to finalise the
        # LifecycleBridgeWireup.
        from forge.cli._serve_production import LifecycleBridgeWireupParts
        from forge.lifecycle_bridge import (
            LifecycleBridge,
            StreamEventTranslator,
            TerminalPublishLedger,
        )

        parts = captured["bridge_wireup_parts"]
        assert isinstance(parts, LifecycleBridgeWireupParts)
        assert isinstance(parts.bridge, LifecycleBridge)
        assert isinstance(parts.translator, StreamEventTranslator)
        assert isinstance(parts.terminal_publish_ledger, TerminalPublishLedger)
        # stream_source / identity_provider are callables.
        assert callable(parts.stream_source)
        assert callable(parts.identity_provider)

    def test_bind_production_serve_logs_wired_not_deferred(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
        fake_forge_config,
    ) -> None:
        """Boot-log assertion: deps composer reports ack_bridge=wired."""
        import asyncio
        import logging

        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module
        from forge.cli._serve_config import ServeConfig

        # Use a real on-disk DB so SqliteLifecyclePersistence (which
        # we now do NOT mock) can derive a non-None db_path via
        # ``PRAGMA database_list`` — ``build_pipeline_consumer_deps``
        # composes the wave-2 collaborators against a real persistence
        # facade, so we let the real construction run.
        config = ServeConfig(
            db_path=tmp_path / "forge.db",
            autobuild_runner_url="http://forge-autobuild-runner:8124",
        )

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )

        # Build a real ForgeConfig-shape — ``build_pipeline_consumer_deps``
        # reads ``forge_config.pipeline`` AND
        # ``forge_config.permissions.filesystem.allowlist`` (via the
        # forward-context builder). The MagicMock auto-generates
        # those attributes; the fake_forge_config fixture is enough.
        from forge.config.models import PipelineConfig

        fake_forge_config.pipeline = PipelineConfig()

        # Fake NATS client passed into the composed
        # ``compose_dispatch_chain(client)`` closure. The
        # PipelinePublisher only uses the client lazily inside its
        # ``publish_*`` methods, so a MagicMock is fine here — we
        # never reach a real publish in this test.
        fake_client = MagicMock(name="nats-client")

        # Run the production binding (invokes the real
        # ``bind_production_dispatch_chain``).
        serve_production.bind_production_serve(config, fake_forge_config)

        # Drive the rebound composer with the fake client. The
        # composer is async, so ``asyncio.run``.
        composer = serve_module.compose_dispatch_chain
        with caplog.at_level(logging.INFO, logger="forge.cli._serve_deps"):
            asyncio.run(composer(fake_client))

        log_text = "\n".join(record.getMessage() for record in caplog.records)

        assert "ack_bridge=wired" in log_text, (
            "expected 'ack_bridge=wired' in deps composer log; "
            "Gap PEBR-WIREUP regression — got:\n" + log_text
        )
        assert "terminal_publish_ledger=wired" in log_text, (
            "expected 'terminal_publish_ledger=wired' in deps composer "
            "log; Gap PEBR-WIREUP regression — got:\n" + log_text
        )
        # Negative assertions — the legacy 'deferred' markers MUST NOT
        # appear after the fix.
        assert "deferred (TASK-FRR-PEB-002)" not in log_text, (
            "ack_bridge is not wired by bind_production_serve "
            "— Gap PEBR-WIREUP regression"
        )
        assert "deferred (TASK-FRR-PEB-005)" not in log_text, (
            "terminal_publish_ledger is not wired by bind_production_serve "
            "— Gap PEBR-WIREUP regression"
        )

    @pytest.mark.asyncio
    async def test_compose_wires_budget_breach_observer_into_wireup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """FEAT-UBS-002 stage-3 production-wiring seam.

        The mid-run budget-breach observer is fully built + tested and
        :class:`LifecycleBridgeWireup` accepts ``budget_observer=``, but the
        production ``_compose`` site did NOT pass it — so the observer
        defaulted to ``None`` and mid-run breach detection was dark in
        production (``builds.budget_breach`` never written; the stage-3
        pre-dispatch gate never fired). This pins that ``_compose`` composes a
        NON-None :class:`BudgetBreachObserver` wired with the REAL serve
        collaborators (the pool's own ``record_budget_breach`` /
        ``read_last_coach_score`` bound methods plus the approval re-emit
        adapter) and threads it into the wireup. It is the production-wiring
        test class the Supervisor lesson already taught once.
        """
        from types import SimpleNamespace

        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli import _serve_daemon
        from forge.cli import _serve_deps_gating
        from forge.cli import serve as serve_module
        from forge.config.models import ForgeConfig
        from forge.lifecycle import migrations
        from forge.lifecycle.persistence import SqliteLifecyclePersistence
        from forge.lifecycle_bridge.budget_observer import BudgetBreachObserver

        # Capturing stand-in for the wireup — records the constructor kwargs
        # so we can inspect the ``budget_observer`` the composer injected. The
        # local ``from ...wireup import LifecycleBridgeWireup`` inside _compose
        # resolves this replacement at call time.
        captured: dict[str, Any] = {}

        class _CapturingWireup:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                self.register_ack_handle = object()

        monkeypatch.setattr(
            "forge.lifecycle_bridge.wireup.LifecycleBridgeWireup",
            _CapturingWireup,
        )

        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        try:
            migrations.apply_at_boot(cx)
            pool = SqliteLifecyclePersistence(connection=cx)
            config = ForgeConfig.model_validate(
                {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
            )
            client = MagicMock(name="nats-client")

            # The wireup is captured, so the SQLite-bound parts are only passed
            # through — sentinels are sufficient. The guard under test is the
            # ``bridge_wireup_parts is not None`` branch that gates the wireup.
            parts = SimpleNamespace(
                bridge=object(),
                translator=object(),
                stream_source=object(),
                identity_provider=object(),
                run_state_fetcher=object(),
                build_state_recorder=object(),
                build_id_resolver=object(),
                terminal_publish_ledger=object(),
            )

            previous_dispatch = _serve_daemon.dispatch_payload
            try:
                compose = serve_module.bind_production_dispatch_chain(
                    forge_config=config,
                    sqlite_pool=pool,
                    bridge_wireup_parts=parts,
                )
                await compose(client)
            finally:
                _serve_daemon.dispatch_payload = previous_dispatch
                _serve_deps_gating._reset_for_tests()

            observer = captured["budget_observer"]
            assert observer is not None, (
                "the wireup received budget_observer=None — mid-run budget "
                "breach detection is dark in production (FEAT-UBS-002 stage-3 "
                "wiring gap)"
            )
            assert isinstance(observer, BudgetBreachObserver)
            # Wired with the REAL serve collaborators, not fakes: the record
            # + coach-score readers are the pool's own bound methods.
            assert observer._record_breach == pool.record_budget_breach
            assert observer._read_coach_score == pool.read_last_coach_score
            # The approval re-emit is the ApprovalPublisher-backed adapter,
            # and the resolve / elapsed / clock collaborators are all wired.
            assert callable(observer._publish_approval_request)
            assert callable(observer._resolve_budget)
            assert callable(observer._elapsed_seconds)
            assert callable(observer._clock)
        finally:
            cx.close()

    def test_bind_production_serve_creates_lifecycle_bridge_registry_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_db_path: Path,
        serve_config,
        fake_forge_config,
    ) -> None:
        """Step 3.5b applies the ``lifecycle_bridge_registry`` migration.

        Pinned by the 2026-05-08 PEBR-WIREUP runbook revalidation
        (TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A). Before the fix, Step
        3.5b applied only the coexistence migration so a fresh
        ``FORGE_DB_PATH`` volume shipped without the
        ``lifecycle_bridge_registry`` table; the first
        ``BridgeRegistry.register_ack_handle`` call raised ``no such
        table: lifecycle_bridge_registry`` and the wireup silently
        fell back to the legacy ``ack_callback`` ack-on-dispatch-
        return path — exactly the redelivery-storm closure the
        bridge was built to replace. This test pins that BOTH
        bridge tables exist in ``sqlite_master`` after a fresh
        ``bind_production_serve`` so the migration cannot regress
        without the suite catching it.
        """
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        # The composer factory itself is not under test here — we
        # only need ``bind_production_serve`` to drive the Step 3.5b
        # migration block end-to-end against a real on-disk DB.
        monkeypatch.setattr(
            serve_module,
            "bind_production_dispatch_chain",
            lambda **kw: (lambda client: None),
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        # Open an independent connection to the on-disk DB and
        # confirm BOTH bridge tables exist in ``sqlite_master``. The
        # writer connection is still held by ``_bound_resources``;
        # SQLite supports multiple-readers + single-writer so this
        # introspection query is safe alongside the live writer.
        inspector = sqlite3.connect(tmp_db_path)
        try:
            tables = {
                row[0]
                for row in inspector.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
        finally:
            inspector.close()

        assert "lifecycle_bridge_terminal_publishes" in tables, (
            "Step 3.5b must apply the coexistence migration "
            "(TASK-FORGE-FRR-PEBR-WIREUP); table missing — got: "
            f"{sorted(tables)!r}"
        )
        assert "lifecycle_bridge_registry" in tables, (
            "Step 3.5b must apply the registry migration "
            "(TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A); without this "
            "the first BridgeRegistry SQL touch raises 'no such "
            "table' and the wireup silently falls back to the "
            "legacy ack_callback redelivery-storm path — got: "
            f"{sorted(tables)!r}"
        )
