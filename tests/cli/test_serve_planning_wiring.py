"""Call-site pin tests for Mode P planning wiring into serve boot (TASK-MP-011).

These tests verify:
1. AC-001: Call-site pin with monkeypatching proves production wiring
2. AC-002: Recovery order (sweep and rearm after composition, rearm once)
3. AC-003: Default config (enabled=False) means zero invocations
4. AC-004: Soft-fail - composition errors don't break daemon boot
5. AC-005: Additive-only changes (existing tests pass)

All tests use fakes and monkeypatching to verify the production composition path
in serve.py is wired correctly, following patterns from test_serve_deps_gating.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.sqlite import connect_writer
from forge.cli.serve import bind_production_dispatch_chain
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations as lifecycle_migrations

logger = logging.getLogger(__name__)


class RecordingFake:
    """Recording fake for tracking invocation order and arguments."""

    def __init__(self, name: str, should_raise: bool = False) -> None:
        self.name = name
        self.invocations: list[tuple[str, Any]] = []
        self.should_raise = should_raise

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Record invocation and optionally raise."""
        self.invocations.append((self.name, {"args": args, "kwargs": kwargs}))
        if self.should_raise:
            raise RuntimeError(f"{self.name} fake error")
        return None

    def was_called(self) -> bool:
        """Check if fake was invoked at least once."""
        return len(self.invocations) > 0

    def call_count(self) -> int:
        """Return number of invocations."""
        return len(self.invocations)


class FakeNatsClient:
    """Minimal fake NATS client for composition testing."""

    def __init__(self) -> None:
        self.subscriptions: list[str] = []
        self.publishes: list[tuple[str, bytes]] = []

    async def subscribe(self, subject: str, callback: Any) -> Any:
        """Record subscription."""
        self.subscriptions.append(subject)
        return MagicMock()

    async def publish(self, subject: str, body: bytes) -> None:
        """Record publish."""
        self.publishes.append((subject, body))


def _make_planning_config(enabled: bool = True, **overrides: Any) -> ForgeConfig:
    """Build ForgeConfig with planning enabled/disabled and optional overrides."""
    doc: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
        "planning": {
            "enabled": enabled,
            "escalation_approver": "alice",
            **overrides,
        },
    }
    return ForgeConfig.model_validate(doc)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Temporary SQLite database for testing."""
    db_path = tmp_path / "test_serve_wiring.db"
    pool = connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(pool)
    return db_path


class TestCallSitePin:
    """AC-001: Call-site pin proves production wiring with monkeypatching."""

    @pytest.mark.asyncio
    async def test_planning_enabled_invokes_composition_with_config(
        self, tmp_db: Path, caplog: Any
    ) -> None:
        """planning.enabled=True -> composition fake invoked exactly once with PlanningConfig."""
        caplog.set_level(logging.INFO)

        # Arrange: recording fakes for the three planning functions
        compose_fake = RecordingFake("compose")
        sweep_fake = RecordingFake("sweep")
        rearm_fake = RecordingFake("rearm")

        config = _make_planning_config(enabled=True)
        pool = connect_writer(tmp_db)
        fake_client = FakeNatsClient()

        # Act: monkeypatch and drive production composition path
        with (
            patch(
                "forge.cli.serve.compose_planning_consumer_and_dispatch", compose_fake
            ),
            patch("forge.cli.serve.sweep_interrupted_planning_runs", sweep_fake),
            patch("forge.cli.serve.rearm_paused_planning_runs", rearm_fake),
            patch("forge.cli._serve_deps_gating.bind_gate_parts") as mock_gate_parts,
            patch(
                "forge.cli._serve_deps.build_pipeline_consumer_deps"
            ) as mock_build_deps,
            patch(
                "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
            ) as mock_publisher,
        ):
            # Gate parts and deps mocks return minimal fakes
            mock_gate_parts.return_value = None
            mock_build_deps.return_value = MagicMock()
            mock_publisher.return_value = (MagicMock(), MagicMock())

            # Create composition function and invoke
            compose_fn = bind_production_dispatch_chain(
                forge_config=config, sqlite_pool=pool
            )
            await compose_fn(fake_client)

        # Assert: composition fake was invoked exactly once
        assert compose_fake.was_called(), (
            "compose_planning_consumer_and_dispatch not called"
        )
        assert compose_fake.call_count() == 1, "compose called more than once"

        # Assert: PlanningConfig was passed (check kwargs has config-like keys)
        invocation = compose_fake.invocations[0]
        kwargs = invocation[1]["kwargs"]
        assert "planning_config" in kwargs or "config" in kwargs, (
            "PlanningConfig not passed to compose"
        )


class TestRecoveryOrder:
    """AC-002: Recovery order - sweep and rearm after composition, rearm once."""

    @pytest.mark.asyncio
    async def test_recovery_functions_invoked_after_composition(
        self, tmp_db: Path
    ) -> None:
        """Sweep and rearm are invoked after composition, in correct order."""
        # Arrange: recording fakes
        compose_fake = RecordingFake("compose")
        sweep_fake = RecordingFake("sweep")
        rearm_fake = RecordingFake("rearm")

        # Collect invocation order across all fakes
        invocation_order: list[str] = []

        async def ordered_compose(*args: Any, **kwargs: Any) -> Any:
            invocation_order.append("compose")
            return await compose_fake(*args, **kwargs)

        async def ordered_sweep(*args: Any, **kwargs: Any) -> Any:
            invocation_order.append("sweep")
            return await sweep_fake(*args, **kwargs)

        async def ordered_rearm(*args: Any, **kwargs: Any) -> Any:
            invocation_order.append("rearm")
            return await rearm_fake(*args, **kwargs)

        config = _make_planning_config(enabled=True)
        pool = connect_writer(tmp_db)
        fake_client = FakeNatsClient()

        # Act: monkeypatch and drive production path
        with (
            patch(
                "forge.cli.serve.compose_planning_consumer_and_dispatch",
                ordered_compose,
            ),
            patch("forge.cli.serve.sweep_interrupted_planning_runs", ordered_sweep),
            patch("forge.cli.serve.rearm_paused_planning_runs", ordered_rearm),
            patch("forge.cli._serve_deps_gating.bind_gate_parts") as mock_gate_parts,
            patch(
                "forge.cli._serve_deps.build_pipeline_consumer_deps"
            ) as mock_build_deps,
            patch(
                "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
            ) as mock_publisher,
        ):
            mock_gate_parts.return_value = None
            mock_build_deps.return_value = MagicMock()
            mock_publisher.return_value = (MagicMock(), MagicMock())

            compose_fn = bind_production_dispatch_chain(
                forge_config=config, sqlite_pool=pool
            )
            await compose_fn(fake_client)

        # Assert: order is compose -> sweep -> rearm
        assert invocation_order == [
            "compose",
            "sweep",
            "rearm",
        ], f"Wrong order: {invocation_order}"

        # Assert: rearm invoked at most once
        assert rearm_fake.call_count() == 1, "rearm called more than once"


class TestDefaultConfigZeroInvocations:
    """AC-003: planning.enabled=False (default) -> zero planning invocations."""

    @pytest.mark.asyncio
    async def test_planning_disabled_means_zero_invocations(self, tmp_db: Path) -> None:
        """planning.enabled=False -> no planning functions invoked."""
        # Arrange: recording fakes
        compose_fake = RecordingFake("compose")
        sweep_fake = RecordingFake("sweep")
        rearm_fake = RecordingFake("rearm")

        config = _make_planning_config(enabled=False)
        pool = connect_writer(tmp_db)
        fake_client = FakeNatsClient()

        # Act: monkeypatch and drive production path
        with (
            patch(
                "forge.cli.serve.compose_planning_consumer_and_dispatch", compose_fake
            ),
            patch("forge.cli.serve.sweep_interrupted_planning_runs", sweep_fake),
            patch("forge.cli.serve.rearm_paused_planning_runs", rearm_fake),
            patch("forge.cli._serve_deps_gating.bind_gate_parts") as mock_gate_parts,
            patch(
                "forge.cli._serve_deps.build_pipeline_consumer_deps"
            ) as mock_build_deps,
            patch(
                "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
            ) as mock_publisher,
        ):
            mock_gate_parts.return_value = None
            mock_build_deps.return_value = MagicMock()
            mock_publisher.return_value = (MagicMock(), MagicMock())

            compose_fn = bind_production_dispatch_chain(
                forge_config=config, sqlite_pool=pool
            )
            await compose_fn(fake_client)

        # Assert: zero invocations of any planning function
        assert not compose_fake.was_called(), (
            "compose called when planning.enabled=False"
        )
        assert not sweep_fake.was_called(), "sweep called when planning.enabled=False"
        assert not rearm_fake.was_called(), "rearm called when planning.enabled=False"


class TestSoftFail:
    """AC-004: Soft-fail - composition errors don't break daemon boot."""

    @pytest.mark.asyncio
    async def test_composition_error_does_not_break_dispatch_chain_binding(
        self, tmp_db: Path, caplog: Any
    ) -> None:
        """Composition raising -> build dispatch chain still binds, error logged."""
        caplog.set_level(logging.ERROR)

        # Arrange: composition fake that raises
        compose_fake = RecordingFake("compose", should_raise=True)
        sweep_fake = RecordingFake("sweep")
        rearm_fake = RecordingFake("rearm")

        config = _make_planning_config(enabled=True)
        pool = connect_writer(tmp_db)
        fake_client = FakeNatsClient()

        # Act: monkeypatch and drive production path
        with (
            patch(
                "forge.cli.serve.compose_planning_consumer_and_dispatch", compose_fake
            ),
            patch("forge.cli.serve.sweep_interrupted_planning_runs", sweep_fake),
            patch("forge.cli.serve.rearm_paused_planning_runs", rearm_fake),
            patch("forge.cli._serve_deps_gating.bind_gate_parts") as mock_gate_parts,
            patch(
                "forge.cli._serve_deps.build_pipeline_consumer_deps"
            ) as mock_build_deps,
            patch(
                "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
            ) as mock_publisher,
        ):
            mock_gate_parts.return_value = None
            mock_build_deps.return_value = MagicMock()
            mock_publisher.return_value = (MagicMock(), MagicMock())

            compose_fn = bind_production_dispatch_chain(
                forge_config=config, sqlite_pool=pool
            )
            # Should NOT raise
            await compose_fn(fake_client)

        # Assert: error was logged
        assert any("planning" in rec.message.lower() for rec in caplog.records), (
            "Planning error not logged"
        )

        # Assert: build dispatch chain was still bound (mock_build_deps was called)
        assert mock_build_deps.called, (
            "build_pipeline_consumer_deps not called after planning error"
        )


class TestAdditiveOnly:
    """AC-005: serve.py diff is additive-only - existing tests pass."""

    def test_existing_serve_module_structure_preserved(self) -> None:
        """Verify serve.py still exports expected symbols."""
        from forge.cli.serve import (
            DEFAULT_DURABLE_NAME,
            DEFAULT_HEALTHZ_PORT,
            bind_production_dispatch_chain,
        )

        # Assert: key exports still exist
        assert DEFAULT_DURABLE_NAME == "forge-serve"
        assert DEFAULT_HEALTHZ_PORT == 8080
        assert callable(bind_production_dispatch_chain)
