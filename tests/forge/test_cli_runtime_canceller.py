"""FEAT-FCT — the production interrupt canceller (register 2b, RUNNING half).

All tests mock ``langgraph_sdk.get_client`` — NO sidecar, NO network, NO
broker (the standing isolation posture). The seam under test is
``_langgraph_interrupt_canceller``: best-effort by design — every failure
mode returns ``False`` without raising, so the CLI cancel's Group D row
transition is never stranded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from forge.cli.runtime import _langgraph_interrupt_canceller


def _client_with_runs(runs: list) -> Mock:
    client = Mock()
    client.runs = Mock()
    client.runs.list = AsyncMock(return_value=runs)
    client.runs.cancel = AsyncMock(return_value=None)
    return client


class TestInterruptCanceller:
    def test_issues_interrupt_for_the_threads_run(self) -> None:
        client = _client_with_runs([{"run_id": "run-42"}])
        with patch("langgraph_sdk.get_client", return_value=client):
            cancel = _langgraph_interrupt_canceller("http://sidecar:1")
            assert cancel("thread-7") is True
        client.runs.list.assert_awaited_once_with("thread-7", limit=1)
        client.runs.cancel.assert_awaited_once_with(
            "thread-7", "run-42", action="interrupt"
        )

    def test_no_runs_is_false_without_cancel(self) -> None:
        client = _client_with_runs([])
        with patch("langgraph_sdk.get_client", return_value=client):
            assert _langgraph_interrupt_canceller("http://s:1")("t") is False
        client.runs.cancel.assert_not_awaited()

    def test_missing_run_id_is_false_without_cancel(self) -> None:
        client = _client_with_runs([{"status": "pending"}])
        with patch("langgraph_sdk.get_client", return_value=client):
            assert _langgraph_interrupt_canceller("http://s:1")("t") is False
        client.runs.cancel.assert_not_awaited()

    def test_transport_error_is_false_never_raises(self) -> None:
        client = _client_with_runs([{"run_id": "r"}])
        client.runs.cancel = AsyncMock(side_effect=OSError("unreachable"))
        with patch("langgraph_sdk.get_client", return_value=client):
            assert _langgraph_interrupt_canceller("http://s:1")("t") is False

    def test_no_url_configured_is_false_without_sdk_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FORGE_AUTOBUILD_RUNNER_URL", raising=False)
        with patch("langgraph_sdk.get_client") as gc:
            assert _langgraph_interrupt_canceller()("t") is False
        gc.assert_not_called()

    def test_env_url_is_used_when_no_explicit_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", "http://env-sidecar:1")
        client = _client_with_runs([{"run_id": "r1"}])
        with patch("langgraph_sdk.get_client", return_value=client) as gc:
            assert _langgraph_interrupt_canceller()("t") is True
        gc.assert_called_once_with(url="http://env-sidecar:1")
