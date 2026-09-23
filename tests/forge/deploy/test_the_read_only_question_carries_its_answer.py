"""A read-only question that FAILED still brings back what the step said.

WHY (25 September 2026, the third review of the executor stage). A project's
"what are you running" step exits non-zero when its own query failed, and says
why on its last line. The stage loaded the executed runbook inside the same
guard as the run, so a failed ask answered with an empty output and the press
could only report that the step "did not finish" — losing the one sentence
that tells whoever reads the result what actually went wrong.

The other half of the same rule lives in ``deployment_identity``: an empty
answer is never read as "nothing is running".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from forge.config.models import DeployStageConfig
from forge.deploy.live_gate import DryRunBrokerInspector, DryRunLiveGateInvoker
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.stage import DeployStageRunner

FIXED = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

PROFILE = """
env_id: widget-live
compose:
  file: nothing.yml
  script: deploy/widget-deploy.sh
cwd: .
identity:
  setting: WIDGET_SHOP_RELEASE
  reported_as: WIDGET_SHOP_RUNNING
  asked_with: WIDGET_SHOP_ASK
  running_as: WIDGET_SHOP_IS_RUNNING
"""


class _ARunbookStore:
    """Hands back one executed runbook whose deploy step printed this text."""

    def __init__(self, said: str) -> None:
        self.said = said
        self.saved: list[Any] = []

    def save_runbook(self, *args: Any, **kwargs: Any) -> None:
        self.saved.append((args, kwargs))

    def load_runbook(self, runbook_id: str, *, correlation_id: str) -> Any:
        return SimpleNamespace(
            steps=(
                SimpleNamespace(
                    step_type="deploy_compose",
                    result=SimpleNamespace(payload={"captured_output": self.said}),
                ),
            )
        )


def _runner(store: Any, tmp_path: Path) -> DeployStageRunner:
    from unittest.mock import AsyncMock

    publisher = AsyncMock()
    return DeployStageRunner(
        repository=store,
        runbook_publisher=publisher,
        deploy_publisher=AsyncMock(),
        reservation=InProcessReservationLease(),
        live_gate_invoker=DryRunLiveGateInvoker(),
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=True,
        clock=lambda: FIXED,
    )


class TestWhatIsRunningCarriesTheAnswerEitherWay:
    @pytest.mark.asyncio
    async def test_a_step_that_did_not_finish_still_brings_its_reason_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = (
            "[the project] the query could not be answered\n"
            "WIDGET_SHOP_IS_RUNNING_UNKNOWN=the query failed: the daemon is "
            "not answering\n"
        )
        store = _ARunbookStore(said)
        runner = _runner(store, tmp_path)

        async def _the_step_went_red(runbook: Any, correlation_id: str) -> Any:
            return SimpleNamespace(status="failed")

        monkeypatch.setattr(runner, "_run_runbook", _the_step_went_red)

        answered = await runner.what_is_running(
            parse_deploy_profile(yaml.safe_load(PROFILE), source_ref="a-profile"),
            correlation_id="corr",
            deploy_run_id="run-1",
            ask_env={"WIDGET_SHOP_ASK": "1"},
        )

        assert answered.outcome == "failed"
        assert "the daemon is not answering" in answered.detail["deploy_output"]

    @pytest.mark.asyncio
    async def test_a_step_that_finished_brings_its_answer_back_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _ARunbookStore("WIDGET_SHOP_IS_RUNNING=j-abc@1111\n")
        runner = _runner(store, tmp_path)

        async def _the_step_was_fine(runbook: Any, correlation_id: str) -> Any:
            return SimpleNamespace(status="complete")

        monkeypatch.setattr(runner, "_run_runbook", _the_step_was_fine)

        answered = await runner.what_is_running(
            parse_deploy_profile(yaml.safe_load(PROFILE), source_ref="a-profile"),
            correlation_id="corr",
            deploy_run_id="run-1",
            ask_env={"WIDGET_SHOP_ASK": "1"},
        )

        assert answered.outcome == "complete"
        assert answered.detail["deploy_output"] == "WIDGET_SHOP_IS_RUNNING=j-abc@1111\n"

    @pytest.mark.asyncio
    async def test_an_answer_that_cannot_be_read_back_is_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Unreadable(_ARunbookStore):
            def load_runbook(self, runbook_id: str, *, correlation_id: str) -> Any:
                raise RuntimeError("the record could not be opened")

        runner = _runner(_Unreadable(""), tmp_path)

        async def _the_step_was_fine(runbook: Any, correlation_id: str) -> Any:
            return SimpleNamespace(status="complete")

        monkeypatch.setattr(runner, "_run_runbook", _the_step_was_fine)

        answered = await runner.what_is_running(
            parse_deploy_profile(yaml.safe_load(PROFILE), source_ref="a-profile"),
            correlation_id="corr",
            deploy_run_id="run-1",
            ask_env={"WIDGET_SHOP_ASK": "1"},
        )

        assert answered.outcome == "complete"
        assert answered.detail["deploy_output"] == ""
