from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from forge.launch_environment import build_launch_env
from forge.lifecycle.feature_routing import FeatureRoutingBlocked, FeatureRoutingReceipt
from forge.pipeline.dispatchers.autobuild_async import dispatch_autobuild_async
from forge.subagents.autobuild_runner import _validated_feature_routing
from forge.subagents import autobuild_runner
from forge.deploy_sidecar.service import process_guardkit_merge_request


def test_launch_environment_routing_is_protected_from_parent_and_project():
    env = build_launch_env(
        parent={
            "PATH": "/bin",
            "GUARDKIT_FEATURE_ROUTING_ID": "stale",
            "GUARDKIT_FEATURE_ROUTING_REQUIRED": "0",
        },
        declared=["GUARDKIT_FEATURE_ROUTING_ID", "GUARDKIT_FEATURE_ROUTING_REQUIRED"],
        feature_routing_id="admitted_A",
        feature_routing_required=True,
    )
    assert env["GUARDKIT_FEATURE_ROUTING_ID"] == "admitted_A"
    assert env["GUARDKIT_FEATURE_ROUTING_REQUIRED"] == "1"
    with pytest.raises(ValueError):
        build_launch_env(feature_routing_required=True)


class _Gate:
    async def ensure_seeded(self, key, **_kwargs):
        return FeatureRoutingReceipt(key, "token", 1)


class _Context:
    def build_for(self, **_kwargs):
        return []


class _Starter:
    def __init__(self):
        self.context = None

    async def astart_async_task(self, *, subagent_name, context):
        self.context = context
        return "task-1"


class _Recorder:
    def record_running(self, **_kwargs):
        pass


class _State:
    def initialise_autobuild_state(self, *_args, **_kwargs):
        pass


def test_autobuild_payload_carries_committed_receipt_before_dispatch():
    starter = _Starter()
    asyncio.run(
        dispatch_autobuild_async(
            "build-A",
            "FEAT-A",
            "planning_A-build",
            forward_context_builder=_Context(),
            async_task_starter=starter,
            stage_log_recorder=_Recorder(),
            state_channel=_State(),
            feature_routing_id="planning_A-build",
            feature_routing_gate=_Gate(),
            feature_routing_required=True,
        )
    )
    assert starter.context["feature_routing_id"] == "planning_A-build"
    assert starter.context["feature_routing_required"] is True
    assert starter.context["feature_routing_receipt"] == {
        "feature_routing_id": "planning_A-build",
        "attempt_id": "token",
        "server_id": 1,
    }


def test_runner_refuses_required_checkpoint_before_supersession_without_receipt():
    with pytest.raises(FeatureRoutingBlocked, match="receipt is missing"):
        _validated_feature_routing(
            {
                "feature_routing_id": "planning_A-build",
                "feature_routing_required": True,
            }
        )

    with pytest.raises(FeatureRoutingBlocked, match="shape is invalid"):
        _validated_feature_routing(
            {
                "feature_routing_id": "planning_A-build",
                "feature_routing_required": True,
                "feature_routing_receipt": {
                    "feature_routing_id": "planning_A-build",
                    "attempt_id": "token",
                    "server_id": 1,
                    "untrusted_extra": True,
                },
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"feature_routing_id": "other", "attempt_id": "token", "server_id": 1},
        {
            "feature_routing_id": "planning_A-build",
            "attempt_id": "token",
            "server_id": "1",
        },
    ],
)
async def test_runner_routing_refusal_is_a_failed_terminal_before_supersession(
    monkeypatch: pytest.MonkeyPatch, receipt: object
) -> None:
    stop = AsyncMock()
    body = AsyncMock()
    monkeypatch.setattr(autobuild_runner, "_stop_an_earlier_run_of", stop)
    monkeypatch.setattr(autobuild_runner, "_running_wave_body", body)
    payload = {
        "build_id": "build-A",
        "feature_id": "FEAT-A",
        "correlation_id": "planning_A-build",
        "feature_routing_id": "planning_A-build",
        "feature_routing_required": True,
        "feature_routing_receipt": receipt,
    }
    state = {
        "messages": [
            {
                "role": "human",
                "content": "RUN_AUTOBUILD subagent=autobuild_runner payload="
                + json.dumps(payload),
            }
        ]
    }

    update = await autobuild_runner._node_running_wave(state)  # type: ignore[arg-type]

    assert update["async_tasks"]["FEAT-A"]["lifecycle"] == "failed"
    assert "feature routing refused" in update["async_tasks"]["FEAT-A"]["error_message"]
    stop.assert_not_awaited()
    body.assert_not_awaited()

def test_merge_sidecar_validates_dedicated_raw_carrier_before_any_subprocess():
    status, body = process_guardkit_merge_request(
        {"feature_routing_id": " routed ", "feature_routing_required": True},
        config=object(),
    )
    assert status == 400
    assert "unmodified ASCII" in body["error"]

    status, body = process_guardkit_merge_request(
        {"feature_routing_required": True},
        config=object(),
    )
    assert status == 400
    assert body["error"] == "required merge has no feature_routing_id"
