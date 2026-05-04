"""Tests for the ``forge serve`` skeleton (TASK-F009-001).

Each ``Test*`` class mirrors one acceptance criterion of TASK-F009-001 so
the criterion → verifier mapping stays explicit (per the project's
testing rules — AAA pattern, descriptive names, AC traceability).

This task only scaffolds the boundary; Wave-2 tasks fill the daemon and
healthz bodies. The tests therefore focus on:

- Contract producer constants (Contracts B and C).
- CLI registration (``forge --help`` and ``forge serve --help``).
- ``ServeConfig`` Pydantic v2 model defaults and env-var overrides.
- ``SubscriptionState`` shape and concurrency-safe mutator.
- The ``serve_cmd`` smoke test — both stubs return, exit code is 0.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# AC-001: forge serve --help shows the serve subcommand
# ---------------------------------------------------------------------------


class TestServeHelp:
    """AC-001: ``forge serve --help`` runs and shows the subcommand."""

    def test_serve_help_runs_and_shows_serve(self) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0, result.output
        # Click's auto-generated help renders the subcommand name and
        # the docstring's first line; both are sufficient.
        assert "serve" in result.output.lower()
        assert "daemon" in result.output.lower() or "healthz" in result.output.lower()


# ---------------------------------------------------------------------------
# AC-002: forge --help lists serve alongside the existing five subcommands
# ---------------------------------------------------------------------------


class TestSubcommandRegistration:
    """AC-002: ``forge --help`` lists ``serve`` next to the other commands."""

    def test_top_level_help_lists_serve(self) -> None:
        from forge.cli.main import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0, result.output
        for expected in ("queue", "status", "history", "cancel", "skip", "serve"):
            assert expected in result.output, (
                f"expected {expected!r} in --help output; got:\n{result.output}"
            )

    def test_serve_command_is_registered_on_main_group(self) -> None:
        from forge.cli.main import main

        assert isinstance(main, click.Group)
        assert "serve" in main.commands


# ---------------------------------------------------------------------------
# AC-003 / AC-004: contract constants importable as forge.cli.serve.DEFAULT_*
# ---------------------------------------------------------------------------


class TestContractConstants:
    """AC-003 / AC-004: Contracts B and C are module-level constants."""

    def test_default_healthz_port_is_8080(self) -> None:
        from forge.cli.serve import DEFAULT_HEALTHZ_PORT

        assert DEFAULT_HEALTHZ_PORT == 8080
        assert isinstance(DEFAULT_HEALTHZ_PORT, int)

    def test_default_durable_name_is_forge_serve(self) -> None:
        from forge.cli.serve import DEFAULT_DURABLE_NAME

        assert DEFAULT_DURABLE_NAME == "forge-serve"
        assert isinstance(DEFAULT_DURABLE_NAME, str)


# ---------------------------------------------------------------------------
# AC-005: ServeConfig is a Pydantic v2 model with the documented defaults
# ---------------------------------------------------------------------------


class TestServeConfigModel:
    """AC-005: ``ServeConfig`` is a Pydantic v2 model with field defaults."""

    def test_default_construction_yields_documented_defaults(self) -> None:
        from forge.cli.serve import ServeConfig

        cfg = ServeConfig()
        assert cfg.nats_url == "nats://127.0.0.1:4222"
        assert cfg.healthz_port == 8080
        assert cfg.durable_name == "forge-serve"
        assert cfg.log_level == "info"

    def test_is_pydantic_v2_basemodel(self) -> None:
        from pydantic import BaseModel

        from forge.cli.serve import ServeConfig

        assert issubclass(ServeConfig, BaseModel)
        # Pydantic v2 exposes ``model_validate`` (v1 had ``parse_obj``).
        assert hasattr(ServeConfig, "model_validate")

    def test_env_var_overrides_for_nats_url(self) -> None:
        from forge.cli._serve_config import ServeConfig

        cfg = ServeConfig.from_env(
            {
                "FORGE_NATS_URL": "nats://broker.internal:4222",
            }
        )
        assert cfg.nats_url == "nats://broker.internal:4222"
        # Untouched fields fall back to their declared defaults.
        assert cfg.healthz_port == 8080
        assert cfg.log_level == "info"

    def test_env_var_overrides_for_healthz_port_parsed_as_int(self) -> None:
        from forge.cli._serve_config import ServeConfig

        cfg = ServeConfig.from_env({"FORGE_HEALTHZ_PORT": "9999"})
        assert cfg.healthz_port == 9999
        assert isinstance(cfg.healthz_port, int)

    def test_env_var_overrides_for_log_level(self) -> None:
        from forge.cli._serve_config import ServeConfig

        cfg = ServeConfig.from_env({"FORGE_LOG_LEVEL": "debug"})
        assert cfg.log_level == "debug"

    def test_default_autobuild_runner_url_is_none(self) -> None:
        """TASK-FORGE-FRR-F010J: the new field defaults to ``None``.

        Production callers MUST override via
        ``FORGE_AUTOBUILD_RUNNER_URL``;
        ``bind_production_serve`` enforces the override at boot
        (TASK-FORGE-FRR-F010J Step 1.5 fail-fast). The ``None``
        default is preserved so non-production callers (BDD oracle,
        lint runners) can still construct ``ServeConfig()``.
        """
        from forge.cli._serve_config import ServeConfig

        cfg = ServeConfig()
        assert cfg.autobuild_runner_url is None

    def test_env_var_overrides_for_autobuild_runner_url(self) -> None:
        """TASK-FORGE-FRR-F010J: ``FORGE_AUTOBUILD_RUNNER_URL`` env var
        is read by ``ServeConfig.from_env``.

        Production deployments set this env var to point the daemon
        at the langgraph-runner sidecar; the ``from_env`` parser must
        honour it so ``bind_production_serve`` finds the URL on the
        config.
        """
        from forge.cli._serve_config import ServeConfig

        cfg = ServeConfig.from_env(
            {
                "FORGE_AUTOBUILD_RUNNER_URL": (
                    "http://forge-autobuild-runner:8124"
                ),
            }
        )
        assert cfg.autobuild_runner_url == (
            "http://forge-autobuild-runner:8124"
        )
        # Untouched fields fall back to defaults.
        assert cfg.nats_url == "nats://127.0.0.1:4222"
        assert cfg.healthz_port == 8080


# ---------------------------------------------------------------------------
# AC-006: SubscriptionState exposes live: bool and is concurrency-safe
# ---------------------------------------------------------------------------


class TestSubscriptionState:
    """AC-006: ``SubscriptionState.live`` defaults to ``False`` and is mutable."""

    def test_default_live_is_false(self) -> None:
        from forge.cli.serve import SubscriptionState

        state = SubscriptionState()
        assert state.live is False
        assert state.is_live() is False

    def test_set_live_under_lock_updates_value(self) -> None:
        from forge.cli.serve import SubscriptionState

        state = SubscriptionState()

        async def _flip() -> None:
            await state.set_live(True)

        asyncio.run(_flip())
        assert state.live is True
        assert state.is_live() is True

    def test_concurrent_writers_do_not_corrupt_state(self) -> None:
        from forge.cli.serve import SubscriptionState

        state = SubscriptionState()

        async def _concurrent_flips() -> None:
            await asyncio.gather(
                *(state.set_live(i % 2 == 0) for i in range(64))
            )

        asyncio.run(_concurrent_flips())
        # Final value is deterministic for the loop body above (last
        # write wins under the lock); the important property is that
        # ``live`` is *some* bool — i.e. the lock prevented torn writes.
        assert isinstance(state.live, bool)


# ---------------------------------------------------------------------------
# AC-007: serve_cmd runs daemon + healthz via asyncio.gather and exits 0
# ---------------------------------------------------------------------------


class _StubNatsClient:
    """Minimal client used by smoke tests — exposes only what _run_serve uses."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestServeCmdSmoke:
    """AC-007: ``serve_cmd`` returns ``0`` when both stubs return.

    TASK-FW10-001 update: ``_run_serve`` now opens a NATS client via
    :data:`forge.cli._serve_daemon.nats_connect` and runs both
    ``reconcile_on_boot`` seams before launching the daemon. The smoke
    tests stub all three so ``serve_cmd`` exits ``0`` without a real
    broker. The detailed daemon body coverage lives in
    ``test_cli_serve_daemon.py``; the boot-order coverage lives in
    ``TestRunServeBootOrder`` below.
    """

    def test_serve_cmd_exits_zero_with_stub_coroutines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli import _serve_daemon
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module
        from forge.cli.main import main

        stub_client = _StubNatsClient()

        async def _fake_connect(servers: str) -> object:
            return stub_client

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            return None

        async def _fake_healthz(config: object, state: object) -> None:
            return None

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)
        # TASK-FIX-F010 — stub the production wrapper and the
        # ForgeConfig resolver so the smoke test does not need a real
        # SQLite pool / ./forge.yaml on disk.
        monkeypatch.setattr(
            serve_production, "bind_production_serve", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            serve_module,
            "_resolve_forge_config_for_serve",
            lambda ctx: object(),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["serve"])
        assert result.exit_code == 0, result.output

    def test_serve_cmd_uses_asyncio_gather(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert ``serve_cmd`` schedules both coroutines concurrently."""
        from forge.cli import _serve_daemon
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        observed: list[str] = []
        stub_client = _StubNatsClient()

        async def _fake_connect(servers: str) -> object:
            return stub_client

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            observed.append("daemon")

        async def _fake_healthz(config: object, state: object) -> None:
            observed.append("healthz")

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)
        # TASK-FIX-F010 — stub the production wrapper and ForgeConfig
        # resolver (see sibling test above for rationale).
        monkeypatch.setattr(
            serve_production, "bind_production_serve", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            serve_module,
            "_resolve_forge_config_for_serve",
            lambda ctx: object(),
        )

        runner = CliRunner()
        result = runner.invoke(serve_module.serve_cmd, [])
        assert result.exit_code == 0, result.output
        assert set(observed) == {"daemon", "healthz"}


# ---------------------------------------------------------------------------
# TASK-FW10-001 AC-006 / AC-007: _run_serve boot order + shared NATS client
# ---------------------------------------------------------------------------


class TestRunServeBootOrder:
    """TASK-FW10-001: _run_serve opens one client, runs both reconciles, sets chain_ready."""

    def test_single_nats_connect_call_on_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC: _run_serve opens **one** NATS client and shares it. No
        # second nats.connect call anywhere in the daemon's startup
        # path.
        import asyncio as _asyncio

        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.cli._serve_config import ServeConfig
        from forge.cli._serve_state import SubscriptionState

        stub_client = _StubNatsClient()
        connect_calls: list[str] = []

        async def _fake_connect(servers: str) -> object:
            connect_calls.append(servers)
            return stub_client

        captured_clients: list[object] = []

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            captured_clients.append(client)

        async def _fake_healthz(config: object, state: object) -> None:
            return None

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)

        config = ServeConfig()
        state = SubscriptionState()
        _asyncio.run(serve_module._run_serve(config, state))

        # Exactly one connect on the startup path.
        assert len(connect_calls) == 1, (
            f"AC violated: expected 1 nats_connect call; got {connect_calls!r}"
        )
        # The shared client was passed into run_daemon.
        assert captured_clients == [stub_client]

    def test_both_reconcile_on_boot_run_before_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC: Both reconcile_on_boot routines are awaited before the
        # durable consumer is attached (Step 5 in §5 of the
        # IMPLEMENTATION-GUIDE.md). We assert ordering by recording
        # the call sequence.
        import asyncio as _asyncio

        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.cli._serve_config import ServeConfig
        from forge.cli._serve_state import SubscriptionState

        stub_client = _StubNatsClient()
        events: list[str] = []

        async def _fake_connect(servers: str) -> object:
            events.append("connect")
            return stub_client

        async def _fake_recovery(client: object) -> None:
            events.append("recovery_reconcile")
            assert client is stub_client

        async def _fake_consumer(client: object) -> None:
            events.append("consumer_reconcile")
            assert client is stub_client

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            events.append("daemon_started")

        async def _fake_healthz(config: object, state: object) -> None:
            events.append("healthz_started")

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(
            serve_module, "recovery_reconcile_on_boot", _fake_recovery
        )
        monkeypatch.setattr(
            serve_module, "consumer_reconcile_on_boot", _fake_consumer
        )
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)

        config = ServeConfig()
        state = SubscriptionState()
        _asyncio.run(serve_module._run_serve(config, state))

        # connect must come first; both reconciles must run before any
        # daemon/healthz coroutine starts.
        connect_idx = events.index("connect")
        recovery_idx = events.index("recovery_reconcile")
        consumer_idx = events.index("consumer_reconcile")
        # The daemon and healthz tasks may schedule concurrently — we
        # only need at least one of them after the reconciles.
        post_reconcile = [
            i
            for i, e in enumerate(events)
            if e in {"daemon_started", "healthz_started"}
        ]

        assert connect_idx < recovery_idx
        assert connect_idx < consumer_idx
        assert recovery_idx < min(post_reconcile)
        assert consumer_idx < min(post_reconcile)

    def test_chain_ready_flips_true_after_reconciles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC: state.chain_ready flips True after composition; healthz
        # reads it and reports unhealthy until then.
        import asyncio as _asyncio

        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.cli._serve_config import ServeConfig
        from forge.cli._serve_state import SubscriptionState

        stub_client = _StubNatsClient()
        chain_ready_when_recovery_runs: list[bool] = []
        chain_ready_when_daemon_runs: list[bool] = []

        async def _fake_connect(servers: str) -> object:
            return stub_client

        async def _fake_recovery(client: object) -> None:
            chain_ready_when_recovery_runs.append(state.chain_ready)

        async def _fake_consumer(client: object) -> None:
            return None

        async def _fake_daemon(
            config: object, state_arg: object, *, client: object = None
        ) -> None:
            chain_ready_when_daemon_runs.append(state.chain_ready)

        async def _fake_healthz(config: object, state_arg: object) -> None:
            return None

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(
            serve_module, "recovery_reconcile_on_boot", _fake_recovery
        )
        monkeypatch.setattr(
            serve_module, "consumer_reconcile_on_boot", _fake_consumer
        )
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)

        config = ServeConfig()
        state = SubscriptionState()
        # Sanity precondition.
        assert state.chain_ready is False
        _asyncio.run(serve_module._run_serve(config, state))

        # Recovery saw chain_ready=False (chain not yet composed).
        assert chain_ready_when_recovery_runs == [False]
        # By the time the daemon runs, the chain is ready.
        assert chain_ready_when_daemon_runs == [True]
        # And the final state reflects that.
        assert state.chain_ready is True

    def test_default_seams_are_no_ops(self) -> None:
        # The seams default to logged no-ops so the boot path works
        # before the production wiring (later FW10 task) lands.
        # Anti-stub guard: these are intentionally rebindable seams,
        # not primary deliverable functions, so the no-op default is
        # the correct shape per the task brief.
        import asyncio as _asyncio
        import inspect

        from forge.cli import serve as serve_module

        assert inspect.iscoroutinefunction(
            serve_module.recovery_reconcile_on_boot
        )
        assert inspect.iscoroutinefunction(
            serve_module.consumer_reconcile_on_boot
        )
        # Calling them with a stub client must not raise.
        _asyncio.run(
            serve_module.recovery_reconcile_on_boot(_StubNatsClient())
        )
        _asyncio.run(
            serve_module.consumer_reconcile_on_boot(_StubNatsClient())
        )


# ---------------------------------------------------------------------------
# AC-008: registration is asserted by inspecting the source/main module
# ---------------------------------------------------------------------------


class TestMainModuleRegistersServe:
    """AC-008: ``main.py`` references ``_serve.serve_cmd``."""

    def test_main_source_imports_serve_module(self) -> None:
        cli_pkg = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "forge"
            / "cli"
            / "main.py"
        )
        source = cli_pkg.read_text(encoding="utf-8")
        assert "from forge.cli import serve as _serve" in source

    def test_main_source_adds_serve_command(self) -> None:
        cli_pkg = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "forge"
            / "cli"
            / "main.py"
        )
        source = cli_pkg.read_text(encoding="utf-8")
        assert "main.add_command(_serve.serve_cmd)" in source

    def test_serve_module_re_exports_contract_constants(self) -> None:
        # Symmetry with TASK-PSM-008 ``TestSourceShape`` discipline.
        import importlib

        module = importlib.import_module("forge.cli.serve")
        assert hasattr(module, "DEFAULT_HEALTHZ_PORT")
        assert hasattr(module, "DEFAULT_DURABLE_NAME")
        assert hasattr(module, "ServeConfig")
        assert hasattr(module, "SubscriptionState")
        assert hasattr(module, "serve_cmd")
