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
        config = SimpleNamespace(approval=SimpleNamespace(expected_approver="pinned"))
        assert config_expected_approver(config) == "pinned"

    def test_returns_none_when_no_config_loaded(self) -> None:
        assert config_expected_approver(None) is None

    def test_returns_none_when_config_has_no_approval(self) -> None:
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
# pending_gate_request / send_paused_cancel — responder threading (break #3)
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

        # A PAUSED row carrying a parseable pending_approval_request_id.
        from forge.gating.identity import derive_request_id
        from forge.lifecycle.state_machine import BuildState

        rid = derive_request_id(
            build_id="build-X", stage_label="Implementation", attempt_count=2
        )
        row = SimpleNamespace(
            status=BuildState.PAUSED,
            pending_approval_request_id=rid,
            correlation_id="corr",
        )
        runtime = SimpleNamespace(
            persistence=SimpleNamespace(get_build_row=lambda _b: row)
        )

        request = gate_inject.pending_gate_request(runtime, build_id="build-X")
        assert request is not None
        gate_inject.send_paused_cancel(
            build_id="build-X",
            request=request,
            reason="cli cancel",
            responder="U0PINNED",
        )

        assert captured["responder"] == "U0PINNED"
        assert captured["attempt_count"] == 2

    def test_no_pending_id_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.lifecycle.state_machine import BuildState

        row = SimpleNamespace(
            status=BuildState.PAUSED,
            pending_approval_request_id=None,
            correlation_id=None,
        )
        runtime = SimpleNamespace(
            persistence=SimpleNamespace(get_build_row=lambda _b: row)
        )
        assert gate_inject.pending_gate_request(runtime, build_id="b") is None


# ---------------------------------------------------------------------------
# forge cancel at a card: the signer check mirrors the card's own rule,
# approval.additional_approvers included (Linear front door, 18 October 2026)
# ---------------------------------------------------------------------------


class TestCancelAtCardSignerCheck:
    COLLABORATOR = "linear:user-0001"

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        responder: str,
        additional: list[str],
    ) -> list[str]:
        from forge.cli import _cancel_run as cancel_run
        from forge.config.models import ForgeConfig
        from forge.lifecycle.state_machine import BuildState

        sent: list[str] = []
        monkeypatch.setattr(
            cancel_run, "pending_gate_request", lambda runtime, *, build_id: object()
        )
        monkeypatch.setattr(
            cancel_run,
            "send_paused_cancel",
            lambda *, build_id, request, reason, responder: sent.append(responder),
        )
        monkeypatch.setattr(
            cancel_run, "_status_after_cancel", lambda *a: BuildState.CANCELLED
        )
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "approval": {
                    "expected_approver": "rich",
                    "additional_approvers": additional,
                },
            }
        )
        assert cancel_run._cancel_at_card(
            object(),
            ctx=SimpleNamespace(obj=config),
            build_id="build-X",
            reason="stop",
            responder=responder,
            confirm_seconds=0,
            poll_seconds=0,
        )
        return sent

    def test_a_listed_collaborator_may_sign_the_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._run(
            monkeypatch, responder=self.COLLABORATOR, additional=[self.COLLABORATOR]
        )
        assert sent == [self.COLLABORATOR]

    @pytest.mark.parametrize(
        ("responder", "additional"),
        [("linear:user-0002", [COLLABORATOR]), (COLLABORATOR, [])],
    )
    def test_an_unlisted_signer_is_refused_before_sending(
        self, monkeypatch: pytest.MonkeyPatch, responder: str, additional: list[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, responder=responder, additional=additional)
        assert exc.value.code == 2
