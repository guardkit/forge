"""Tests for daemon-boot SQLite migration application (TASK-FORGE-FRR-F010A).

Each ``Test*`` class mirrors one acceptance criterion of TASK-FORGE-FRR-F010A
so the criterion → verifier mapping stays explicit. Unlike the sibling
:mod:`test_cli_serve_production` suite (which mostly mocks
``connect_writer`` to keep idempotency assertions cheap), these tests run
against a real SQLite file under ``tmp_path`` so the canonical 5-table
schema is observable post-bind.

AAA pattern throughout. ``tmp_path`` keeps filesystem side effects
test-local. The autouse ``_reset_binding_state`` fixture rewinds the
:mod:`forge.cli._serve_production` module-level binding between tests so
re-entrant assertions in TestRebindIdempotency are deterministic.
"""

from __future__ import annotations

import logging
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

    Carries ``.name`` (for the by-name lookup in
    ``_resolve_async_task_starter``), a no-op ``.func`` (legacy sync
    duck-type check), and a no-op ``.coroutine`` (the async path
    duck-type check added by TASK-FORGE-FRR-F010G). Tests in this
    module exercise the migrations boot path only and never invoke the
    starter; the placeholder callables raise if they ever fire so a
    future refactor that does invoke them surfaces a test failure
    rather than silently passing.
    """

    def __init__(self, name: str = "start_async_task") -> None:
        self.name = name

    def func(
        self, *, description: str, subagent_type: str, runtime: Any
    ) -> Any:  # pragma: no cover - placeholder, never invoked by these tests
        raise AssertionError(
            "_FakeStartAsyncTaskTool.func was invoked unexpectedly in "
            "test_serve_production_migrations; these tests exercise the "
            "boot-time migration path only"
        )

    async def coroutine(
        self, *, description: str, subagent_type: str, runtime: Any
    ) -> Any:  # pragma: no cover - placeholder, never invoked by these tests
        raise AssertionError(
            "_FakeStartAsyncTaskTool.coroutine was invoked unexpectedly "
            "in test_serve_production_migrations; these tests exercise "
            "the boot-time migration path only"
        )


class _FakeMiddleware:
    """Stand-in for :class:`AsyncSubAgentMiddleware` exposing a tools tuple."""

    def __init__(self, tool_names: tuple[str, ...] = ()) -> None:
        self.tools = tuple(_FakeStartAsyncTaskTool(n) for n in tool_names)


@pytest.fixture(autouse=True)
def _reset_binding_state() -> Any:
    """Reset wrapper module state and the dispatch-chain seam between tests.

    Mirrors the autouse fixture in
    :mod:`tests.forge.test_cli_serve_production`. Without it,
    re-bind-twice assertions can carry binding state across tests in the
    same process.
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
    """Deterministic SQLite path under the test's tmp dir."""
    return tmp_path / "forge.db"


@pytest.fixture
def serve_config(tmp_db_path: Path) -> Any:
    from forge.cli._serve_config import ServeConfig

    return ServeConfig(db_path=tmp_db_path)


@pytest.fixture
def fake_forge_config() -> Any:
    """Cheap stand-in for :class:`ForgeConfig` — only identity matters here."""
    return MagicMock(name="ForgeConfig")


@pytest.fixture
def stub_serve_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the dispatch-chain factory + middleware so the real DB path is exercised.

    The migration-bootstrap tests want a real ``connect_writer`` +
    ``apply_at_boot`` against ``tmp_db_path``, but the production
    middleware factory and :func:`bind_production_dispatch_chain` pull
    the heavyweight DeepAgents graph — out of scope for this AC.
    """
    from forge.cli import serve as serve_module

    monkeypatch.setattr(
        serve_module,
        "_build_async_subagent_middleware",
        lambda: _FakeMiddleware(tool_names=("start_async_task",)),
    )
    monkeypatch.setattr(
        serve_module,
        "bind_production_dispatch_chain",
        lambda **kw: lambda client: None,
    )


#: Tables produced by :func:`forge.lifecycle.migrations.apply_at_boot`
#: against a fresh DB. The full operator-facing "canonical 5"
#: (``async_tasks`` / ``builds`` / ``stage_log`` / ``sqlite_sequence`` /
#: ``schema_version``) cited in the TASK-FORGE-FRR-F010A description
#: includes ``async_tasks``, but that table is provisioned by
#: :func:`forge.cli._serve_deps_state_channel.ensure_async_tasks_schema`
#: at dispatcher-construction time (Step 7 of
#: :func:`bind_production_serve`, inside the real
#: ``bind_production_dispatch_chain``). The migration tests stub the
#: dispatcher-chain factory to keep the test surface minimal, so they
#: assert the 4 tables ``apply_at_boot`` itself owns:
#:
#: - ``builds`` and ``stage_log`` come from ``schema.sql``.
#: - ``schema_version`` is the migration ledger.
#: - ``sqlite_sequence`` is auto-created by SQLite when ``builds``'s
#:   ``AUTOINCREMENT`` column is provisioned.
MIGRATION_TABLES: frozenset[str] = frozenset(
    {
        "builds",
        "stage_log",
        "sqlite_sequence",
        "schema_version",
    }
)


def _table_names(db_path: Path) -> set[str]:
    """Return the table names present in ``db_path`` via a fresh reader."""
    cx = sqlite3.connect(str(db_path))
    try:
        rows = cx.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table';"
        ).fetchall()
    finally:
        cx.close()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# AC-2 — fresh DB volume gets the migration-managed tables on first bind
# ---------------------------------------------------------------------------


class TestFreshDbBootstrapsMigrationTables:
    """AC-2: ``bind_production_serve`` against a fresh DB applies the migrations."""

    def test_bind_production_serve_creates_migration_tables_on_fresh_db(
        self,
        serve_config: Any,
        fake_forge_config: Any,
        stub_serve_module: None,
    ) -> None:
        from forge.cli import _serve_production as serve_production

        # Sanity — the file does not exist before bind.
        assert not serve_config.db_path.exists()

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        assert serve_config.db_path.exists()
        tables = _table_names(serve_config.db_path)
        assert MIGRATION_TABLES.issubset(tables), (
            "Expected migration-managed tables after fresh-DB bind; "
            f"missing: {sorted(MIGRATION_TABLES - tables)!r}"
        )


# ---------------------------------------------------------------------------
# AC-3 — boot log emits ``applied N SQLite migration(s) at boot``
# ---------------------------------------------------------------------------


class TestBootLogEmitsAppliedCount:
    """AC-3: an INFO log line records the count of newly-applied migrations."""

    def test_fresh_db_logs_nonzero_applied_count(
        self,
        caplog: pytest.LogCaptureFixture,
        serve_config: Any,
        fake_forge_config: Any,
        stub_serve_module: None,
    ) -> None:
        from forge.cli import _serve_production as serve_production

        with caplog.at_level(
            logging.INFO, logger="forge.cli._serve_production"
        ):
            serve_production.bind_production_serve(serve_config, fake_forge_config)

        applied_lines = [
            record.getMessage()
            for record in caplog.records
            if "SQLite migration" in record.getMessage()
        ]
        assert applied_lines, (
            "Expected an INFO log line containing 'SQLite migration' "
            f"after fresh-DB bind; got: {[r.getMessage() for r in caplog.records]!r}"
        )
        # Two bundled migrations (schema.sql + schema_v2.sql) → applied=2.
        assert "applied 2" in applied_lines[0], applied_lines[0]


# ---------------------------------------------------------------------------
# AC-4 — re-bind against the same DB is a no-op (``applied 0``)
# ---------------------------------------------------------------------------


class TestRebindIdempotency:
    """AC-4: re-binding against an already-migrated DB applies zero migrations."""

    def test_second_bind_logs_applied_zero_migrations(
        self,
        caplog: pytest.LogCaptureFixture,
        serve_config: Any,
        fake_forge_config: Any,
        stub_serve_module: None,
    ) -> None:
        from forge.cli import _serve_production as serve_production

        # First bind — applies the bundled migrations.
        serve_production.bind_production_serve(serve_config, fake_forge_config)
        assert MIGRATION_TABLES.issubset(_table_names(serve_config.db_path))

        # Second bind — capture only this call's log records.
        caplog.clear()
        with caplog.at_level(
            logging.INFO, logger="forge.cli._serve_production"
        ):
            serve_production.bind_production_serve(serve_config, fake_forge_config)

        applied_lines = [
            record.getMessage()
            for record in caplog.records
            if "SQLite migration" in record.getMessage()
        ]
        assert applied_lines, (
            "Expected an INFO log line containing 'SQLite migration' on "
            "re-bind; got nothing"
        )
        assert "applied 0" in applied_lines[0], applied_lines[0]

        # And the canonical schema is still intact.
        assert MIGRATION_TABLES.issubset(_table_names(serve_config.db_path))
