"""Tests for ``forge serve`` logging wiring (TASK-FORGE-FRR-002).

Each ``Test*`` class mirrors one acceptance criterion of the FRR-002
follow-up so the criterion → verifier mapping stays explicit (per the
project's testing rules — AAA pattern, descriptive names, AC
traceability).

The fix under test attaches a stderr handler to the root logger via
``logging.basicConfig`` so every existing ``logger.info`` /
``logger.warning`` / ``logger.error`` call inside the daemon and the
healthz server reaches the container's stderr. Before this fix, those
records were silently dropped because no handler was ever attached —
see the 2026-05-01 GB10 first-real-run where ``docker logs
forge-prod`` was empty despite a successful consume + ack.
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner


def _reset_root_handlers() -> list[logging.Handler]:
    """Snapshot + clear root handlers so each test starts clean.

    ``logging.basicConfig`` is a no-op when the root logger already has
    a handler, which is the exact property AC ``no double-handler
    regression`` cares about — but it also means tests pollute each
    other if a previous run already attached one. Each test that
    cares about handler attachment calls this in its arrange step,
    then restores the originals in the finalizer.
    """
    root = logging.getLogger()
    snapshot = list(root.handlers)
    for handler in snapshot:
        root.removeHandler(handler)
    return snapshot


def _restore_root_handlers(snapshot: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in snapshot:
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# AC: at FORGE_LOG_LEVEL=info, an INFO record is delivered to a handler
# ---------------------------------------------------------------------------


class TestInfoLevelEmitsRecords:
    """AC: INFO records reach stderr when ``FORGE_LOG_LEVEL=info``."""

    def test_info_record_is_emitted_after_serve_cmd_initialises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange — point env at INFO and stub the NATS connect seam +
        # both coroutines so ``serve_cmd`` returns immediately without
        # a real broker (TASK-FW10-001 changed _run_serve to open a
        # client up-front).
        monkeypatch.setenv("FORGE_LOG_LEVEL", "info")

        from forge.cli import _serve_daemon
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        class _StubClient:
            async def close(self) -> None:
                return None

            async def register_agent(self, manifest: object) -> None:
                return None

            async def deregister_agent(
                self, agent_id: str, reason: str = "shutdown"
            ) -> None:
                return None

        async def _fake_connect(servers: str) -> object:
            return _StubClient()

        async def _fake_open_fleet(nats_url: str) -> object:
            return _StubClient()

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            logging.getLogger("forge.cli._serve_daemon").info("fake-daemon-attach")

        async def _fake_healthz(config: object, state: object) -> None:
            return None

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(serve_module, "open_fleet_client", _fake_open_fleet)
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)
        # TASK-FIX-F010 — stub the production wrapper and ForgeConfig
        # resolver so the smoke path returns 0 without a real DB.
        monkeypatch.setattr(
            serve_production, "bind_production_serve", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            serve_module,
            "_resolve_forge_config_for_serve",
            lambda ctx: object(),
        )

        # Act
        with caplog.at_level(logging.INFO):
            runner = CliRunner()
            result = runner.invoke(serve_module.serve_cmd, [])

        # Assert
        assert result.exit_code == 0, result.output
        info_records = [
            record for record in caplog.records if record.levelno == logging.INFO
        ]
        assert info_records, (
            "expected at least one INFO record to be captured after "
            "serve_cmd attached the root handler"
        )

    def test_root_logger_level_set_to_info_for_lowercase_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from forge.cli.serve import _configure_logging

        snapshot = _reset_root_handlers()
        try:
            # Act
            _configure_logging("info")
            # Assert
            assert logging.getLogger().getEffectiveLevel() == logging.INFO
        finally:
            _restore_root_handlers(snapshot)


# ---------------------------------------------------------------------------
# AC: at FORGE_LOG_LEVEL=debug, the debug threshold is honoured
# ---------------------------------------------------------------------------


class TestDebugLevelLowersThreshold:
    """AC: ``FORGE_LOG_LEVEL=debug`` sets root level to DEBUG."""

    def test_debug_value_sets_root_level_to_debug(self) -> None:
        # Arrange
        from forge.cli.serve import _configure_logging

        snapshot = _reset_root_handlers()
        try:
            # Act
            _configure_logging("debug")
            # Assert
            assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
        finally:
            _restore_root_handlers(snapshot)


# ---------------------------------------------------------------------------
# AC: invalid FORGE_LOG_LEVEL falls back to INFO with a stderr warning
# ---------------------------------------------------------------------------


class TestInvalidLevelFallsBackToInfo:
    """AC: bogus values do not crash; fall back to INFO with one-line warn."""

    def test_unrecognised_value_falls_back_to_info(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        from forge.cli.serve import _configure_logging

        snapshot = _reset_root_handlers()
        try:
            # Act
            _configure_logging("banana")

            # Assert — root level is INFO and a one-line warning was
            # written to stderr (not stdout).
            assert logging.getLogger().getEffectiveLevel() == logging.INFO
            captured = capsys.readouterr()
            assert "banana" in captured.err
            assert "INFO" in captured.err
            assert captured.out == ""
        finally:
            _restore_root_handlers(snapshot)

    def test_unrecognised_value_does_not_raise(self) -> None:
        # Arrange / Act / Assert — must not crash the daemon on a typo.
        from forge.cli.serve import _configure_logging

        snapshot = _reset_root_handlers()
        try:
            _configure_logging("not-a-real-level")
        finally:
            _restore_root_handlers(snapshot)


# ---------------------------------------------------------------------------
# AC: no double-handler regression on re-entrant calls
# ---------------------------------------------------------------------------


class TestNoDoubleHandlerRegression:
    """AC: calling ``_configure_logging`` twice does not duplicate handlers."""

    def test_second_call_is_a_noop_on_handler_count(self) -> None:
        # Arrange
        from forge.cli.serve import _configure_logging

        snapshot = _reset_root_handlers()
        try:
            # Act
            _configure_logging("info")
            handler_count_after_first = len(logging.getLogger().handlers)
            _configure_logging("info")
            handler_count_after_second = len(logging.getLogger().handlers)

            # Assert
            assert handler_count_after_first == 1
            assert handler_count_after_second == handler_count_after_first
        finally:
            _restore_root_handlers(snapshot)


# ---------------------------------------------------------------------------
# AC: format includes timestamp + logger name + level + message
# ---------------------------------------------------------------------------


class TestFormatString:
    """AC: log format is the documented ISO-8601 / level / name / message."""

    def test_format_string_constants_match_acceptance_criterion(self) -> None:
        # Arrange / Act
        from forge.cli.serve import _LOG_DATEFMT, _LOG_FORMAT

        # Assert — the four pieces the AC names must all be present.
        for token in ("%(asctime)s", "%(levelname)s", "%(name)s", "%(message)s"):
            assert (
                token in _LOG_FORMAT
            ), f"expected {token!r} in _LOG_FORMAT; got {_LOG_FORMAT!r}"
        assert _LOG_DATEFMT == "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# AC: serve_cmd wires _configure_logging into the boot path
# ---------------------------------------------------------------------------


class TestServeCmdInvokesConfigureLogging:
    """AC: ``serve_cmd`` calls ``_configure_logging`` with the parsed level."""

    def test_serve_cmd_calls_configure_logging_with_env_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from forge.cli import _serve_daemon
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        class _StubClient:
            async def close(self) -> None:
                return None

            async def register_agent(self, manifest: object) -> None:
                return None

            async def deregister_agent(
                self, agent_id: str, reason: str = "shutdown"
            ) -> None:
                return None

        async def _fake_connect(servers: str) -> object:
            return _StubClient()

        async def _fake_open_fleet(nats_url: str) -> object:
            return _StubClient()

        async def _fake_daemon(
            config: object, state: object, *, client: object = None
        ) -> None:
            return None

        async def _fake_healthz(config: object, state: object) -> None:
            return None

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(serve_module, "open_fleet_client", _fake_open_fleet)
        monkeypatch.setattr(serve_module, "run_daemon", _fake_daemon)
        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)
        # TASK-FIX-F010 — bypass production wiring + ForgeConfig load.
        monkeypatch.setattr(
            serve_production, "bind_production_serve", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            serve_module,
            "_resolve_forge_config_for_serve",
            lambda ctx: object(),
        )
        monkeypatch.setenv("FORGE_LOG_LEVEL", "warning")

        observed: list[str] = []

        def _spy(level_name: str) -> None:
            observed.append(level_name)

        monkeypatch.setattr(serve_module, "_configure_logging", _spy)

        # Act
        runner = CliRunner()
        result = runner.invoke(serve_module.serve_cmd, [])

        # Assert
        assert result.exit_code == 0, result.output
        assert observed == ["warning"]
