"""WS3-S6 (2026-07-09) — gate refresh-on-timeout is wired LIVE in production.

D659 shipped the real SQLite ``gate_repository`` at
``build_sqlite_gate_adapters``, but ``serve.py``'s ``_compose`` passed
``repository=None`` into ``build_approval_gate_parts`` — so
``ApprovalSubscriberDeps.publish_refresh`` stayed ``None`` and every pause
gave up after ONE ``default_wait_seconds`` window instead of waiting the
full ``max_wait_seconds`` with periodic re-publishes.

These tests drive the REAL production compose closure
(:func:`forge.cli.serve.bind_production_dispatch_chain`) over a real SQLite
pool + the in-memory NATS double and assert the composed gate parts now
carry a live ``publish_refresh`` callback. A regression to ``repository=None``
turns the refresh callback back to ``None`` and fails
``test_compose_threads_gate_repository_so_refresh_is_live``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating, serve
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations

from .conftest import InMemoryNats


def _forge_config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
    )


@pytest.fixture()
def pool(tmp_path: Path):
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    p = SqliteLifecyclePersistence(connection=cx)
    yield p
    cx.close()


@pytest.fixture(autouse=True)
def _reset_gate_parts():
    _serve_deps_gating._reset_for_tests()
    yield
    _serve_deps_gating._reset_for_tests()


class _FakeStarter:
    async def __call__(self, **_: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("async_task_starter must not run during compose")


class TestRefreshWiredInProductionCompose:
    """The production compose threads the D659 SQLite adapter into the parts."""

    @pytest.mark.asyncio
    async def test_compose_threads_gate_repository_so_refresh_is_live(
        self, pool: Any
    ) -> None:
        nats = InMemoryNats()
        composer = serve.bind_production_dispatch_chain(
            forge_config=_forge_config(),
            sqlite_pool=pool,
            async_task_starter=_FakeStarter(),
        )

        await composer(nats)

        parts = _serve_deps_gating.bound_gate_parts()
        assert parts is not None, "compose must bind the approval gate parts"
        # The refresh-on-timeout loop is LIVE: a callable publish_refresh means
        # the subscriber waits the full max_wait_seconds with periodic
        # re-publishes instead of expiring after one default_wait window.
        assert callable(parts.subscriber._deps.publish_refresh), (
            "WS3-S6: serve._compose must thread the D659 gate_repository into "
            "build_approval_gate_parts so publish_refresh is wired (regression: "
            "repository=None leaves it None and pauses expire single-window)"
        )
