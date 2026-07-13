"""Unit tests for the O-02 / TASK-FWD-005 ops helpers.

Covers the terminal-independent responder resolver
(:mod:`forge.cli._responder`), the canonical DB resolver
(:mod:`forge.cli._db_resolve`), and the responder threading through the
paused-cancel injector seam (:mod:`forge.cli._cancel_gate_inject`).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.cli import _cancel_gate_inject as gate_inject
from forge.cli._db_resolve import DEFAULT_DB_PATH, resolve_db_path
from forge.cli._responder import (
    RESPONDER_ENV_VAR,
    UNKNOWN_RESPONDER,
    config_expected_approver,
    resolve_responder,
)


def _clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (RESPONDER_ENV_VAR, "USER", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# resolve_responder — the fallback chain (break #2)
# ---------------------------------------------------------------------------


class TestResolveResponder:
    def test_explicit_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RESPONDER_ENV_VAR, "from-env")
        assert resolve_responder("flag", pinned="pinned") == "flag"

    def test_env_beats_pinned_and_os_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RESPONDER_ENV_VAR, "from-env")
        monkeypatch.setenv("USER", "os-user")
        assert resolve_responder(None, pinned="pinned") == "from-env"

    def test_pinned_beats_os_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_identity_env(monkeypatch)
        monkeypatch.setenv("USER", "os-user")
        # The paused path passes the gate's expected_approver as ``pinned`` so
        # it beats the ambient OS user (which the pinned gate would reject).
        assert resolve_responder(None, pinned="U03QR8WKT29") == "U03QR8WKT29"

    def test_falls_back_to_user_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_identity_env(monkeypatch)
        monkeypatch.setenv("USER", "os-user")
        assert resolve_responder(None) == "os-user"

    def test_never_raises_when_getlogin_crashes_headless(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: os.getlogin() OSError must not crash cancel."""
        _clear_identity_env(monkeypatch)

        def _no_tty() -> str:
            raise OSError(6, "No such device or address")

        monkeypatch.setattr(os, "getlogin", _no_tty)
        # Force getpass.getuser() to fail too so we exercise the getlogin guard.
        import getpass

        def _no_pwd() -> str:
            raise OSError("no pwd entry")

        monkeypatch.setattr(getpass, "getuser", _no_pwd)

        assert resolve_responder(None) == UNKNOWN_RESPONDER

    def test_blank_flag_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_identity_env(monkeypatch)
        monkeypatch.setenv("USER", "os-user")
        assert resolve_responder("   ") == "os-user"


class TestConfigExpectedApprover:
    def test_reads_pinned_approver_from_forge_config(self) -> None:
        ctx = SimpleNamespace(
            obj=SimpleNamespace(approval=SimpleNamespace(expected_approver="pinned"))
        )
        assert config_expected_approver(ctx) == "pinned"

    def test_returns_none_when_no_config_loaded(self) -> None:
        assert config_expected_approver(SimpleNamespace(obj=None)) is None

    def test_returns_none_when_ctx_has_no_obj(self) -> None:
        assert config_expected_approver(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# resolve_db_path — canonical ledger resolution (break #1)
# ---------------------------------------------------------------------------


class TestResolveDbPath:
    def test_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_DB_PATH", "/env/forge.db")
        assert resolve_db_path("/explicit/forge.db") == Path("/explicit/forge.db")

    def test_env_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_DB_PATH", "/env/forge.db")
        assert resolve_db_path(None) == Path("/env/forge.db")

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FORGE_DB_PATH", raising=False)
        assert resolve_db_path(None) == DEFAULT_DB_PATH.expanduser()

    def test_tilde_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FORGE_DB_PATH", raising=False)
        resolved = resolve_db_path("~/x/forge.db")
        assert "~" not in str(resolved)


# ---------------------------------------------------------------------------
# try_inject_paused_cancel — responder threading (break #3)
# ---------------------------------------------------------------------------


class TestPausedCancelThreadsResponder:
    def test_responder_forwarded_to_the_injector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _spy(*, build_id, stage_label, attempt_count, correlation_id, responder):
            captured.update(
                build_id=build_id,
                stage_label=stage_label,
                attempt_count=attempt_count,
                responder=responder,
            )

        monkeypatch.setattr(gate_inject, "_inject_synthetic_reject", _spy)

        # A row carrying a parseable pending_approval_request_id.
        from forge.gating.identity import derive_request_id

        rid = derive_request_id(
            build_id="build-X", stage_label="Implementation", attempt_count=2
        )
        row = SimpleNamespace(pending_approval_request_id=rid, correlation_id="corr")
        runtime = SimpleNamespace(
            persistence=SimpleNamespace(get_build_row=lambda _b: row)
        )

        ok = gate_inject.try_inject_paused_cancel(
            runtime, build_id="build-X", reason="cli cancel", responder="U0PINNED"
        )

        assert ok is True
        assert captured["responder"] == "U0PINNED"
        assert captured["attempt_count"] == 2

    def test_no_pending_id_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = SimpleNamespace(pending_approval_request_id=None, correlation_id=None)
        runtime = SimpleNamespace(
            persistence=SimpleNamespace(get_build_row=lambda _b: row)
        )
        assert (
            gate_inject.try_inject_paused_cancel(
                runtime, build_id="b", reason="r", responder="U0"
            )
            is False
        )
