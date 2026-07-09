"""Tests for the deploy step-type handlers (WS2-B8, scope-design §4).

Each handler satisfies the StepHandler protocol (``(step) -> StepOutcome``,
never raises). Covers dry-run recording, the secrets-are-refs guardrail, the
loud-raising unconfigured seams (FEAT-DD4F), and the run_live_gate verdict
mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge.deploy.live_gate import (
    BrokerDiff,
    LiveGateInvocation,
    UnconfiguredBrokerInspector,
    UnconfiguredLiveGateInvoker,
)
from forge.deploy.steps import (
    make_broker_preflight_handler,
    make_inject_secrets_handler,
    make_run_live_gate_handler,
    register_deploy_handlers,
)
from forge.executor.registry import StepTypeRegistry
from forge.persistence.repositories.runbook_models import Step, StepStatus


def _step(step_type: str, params: dict[str, Any]) -> Step:
    return Step(
        step_type=step_type,
        params=params,
        status=StepStatus.pending,
        sequence_index=0,
    )


class _FakeInvoker:
    def __init__(self, invocation: LiveGateInvocation) -> None:
        self._invocation = invocation
        self.calls: list[dict[str, Any]] = []

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        self.calls.append({"feature": feature, "target": target, "gates": gates})
        return self._invocation


class _FakeBroker:
    def __init__(self, diff: BrokerDiff) -> None:
        self._diff = diff

    def diff(self, broker_contract_ref: str) -> BrokerDiff:
        return self._diff


class TestInjectSecrets:
    """inject_secrets: register REFS only; fails closed on absent refs; no values."""

    def test_dry_run_records_names_only(self) -> None:
        handler = make_inject_secrets_handler(dry_run=True)
        out = handler(_step("inject_secrets", {"refs": ["A", "B"]}))
        assert out.status == StepStatus.passed
        assert out.result == {"dry_run": True, "would_inject_refs": ["A", "B"]}

    def test_value_bearing_ref_fails(self) -> None:
        handler = make_inject_secrets_handler(dry_run=True)
        out = handler(_step("inject_secrets", {"refs": ["A=secret"]}))
        assert out.status == StepStatus.failed
        # The value never appears in the result beyond the offending token echo,
        # and the step refuses rather than injecting.
        assert "REFS only" in out.result["error"]

    def test_live_fails_closed_on_missing_ref(self) -> None:
        handler = make_inject_secrets_handler(
            dry_run=False, presence_resolver=lambda name: name == "PRESENT"
        )
        out = handler(_step("inject_secrets", {"refs": ["PRESENT", "MISSING"]}))
        assert out.status == StepStatus.failed
        assert out.result["missing_refs"] == ["MISSING"]

    def test_live_records_presence_never_value(self) -> None:
        handler = make_inject_secrets_handler(
            dry_run=False, presence_resolver=lambda name: True
        )
        out = handler(_step("inject_secrets", {"refs": ["A", "B"]}))
        assert out.status == StepStatus.passed
        assert out.result == {"injected_refs": ["A", "B"], "all_present": True}


class TestBrokerPreflight:
    def test_dry_run_matches(self) -> None:
        handler = make_broker_preflight_handler(
            broker_inspector=_FakeBroker(BrokerDiff(matches=True, dry_run=True))
        )
        out = handler(_step("broker_preflight", {"broker_contract_ref": "c.yaml"}))
        assert out.status == StepStatus.passed
        assert out.result["matches"] is True

    def test_drift_fails(self) -> None:
        handler = make_broker_preflight_handler(
            broker_inspector=_FakeBroker(
                BrokerDiff(matches=False, drifts=("stream X missing",))
            )
        )
        out = handler(_step("broker_preflight", {"broker_contract_ref": "c.yaml"}))
        assert out.status == StepStatus.failed
        assert out.result["drifts"] == ["stream X missing"]

    def test_unconfigured_inspector_fails_not_raises(self) -> None:
        # FEAT-DD4F: an unconfigured seam RAISES; the handler maps that to an
        # honest failed outcome (never a silent green, never a crash).
        handler = make_broker_preflight_handler(
            broker_inspector=UnconfiguredBrokerInspector()
        )
        out = handler(_step("broker_preflight", {"broker_contract_ref": "c.yaml"}))
        assert out.status == StepStatus.failed
        assert "no broker inspector is configured" in out.result["error"]


class TestRunLiveGate:
    def test_passes_and_carries_verdict(self) -> None:
        inv = LiveGateInvocation(
            verdict="pass", run_id="r1", gate_ids=("g1",), evidence_index_ref="ev"
        )
        handler = make_run_live_gate_handler(live_gate_invoker=_FakeInvoker(inv))
        out = handler(_step("run_live_gate", {"feature": "FEAT-FMDR", "target": "env"}))
        assert out.status == StepStatus.passed
        assert out.result["verdict"] == "pass"
        assert out.result["run_id"] == "r1"

    def test_environment_fail_verdict_still_passes_step(self) -> None:
        # instrument/environment verdicts never indict the feature — the step
        # ran and produced an honest verdict, carried in the result.
        inv = LiveGateInvocation(verdict="environment_fail", run_id="r2")
        handler = make_run_live_gate_handler(live_gate_invoker=_FakeInvoker(inv))
        out = handler(_step("run_live_gate", {"feature": "F", "target": "e"}))
        assert out.status == StepStatus.passed
        assert out.result["verdict"] == "environment_fail"

    def test_unconfigured_invoker_fails_not_raises(self) -> None:
        handler = make_run_live_gate_handler(
            live_gate_invoker=UnconfiguredLiveGateInvoker()
        )
        out = handler(_step("run_live_gate", {"feature": "F", "target": "e"}))
        assert out.status == StepStatus.failed
        assert "no live-gate invoker is configured" in out.result["error"]

    def test_missing_params_fail(self) -> None:
        inv = LiveGateInvocation(verdict="pass", run_id="r")
        handler = make_run_live_gate_handler(live_gate_invoker=_FakeInvoker(inv))
        out = handler(_step("run_live_gate", {"feature": "F"}))
        assert out.status == StepStatus.failed


class TestRegisterCoverage:
    def test_all_step_types_registered(self) -> None:
        reg = StepTypeRegistry()
        register_deploy_handlers(
            reg,
            dry_run=True,
            live_gate_invoker=UnconfiguredLiveGateInvoker(),
            broker_inspector=UnconfiguredBrokerInspector(),
        )
        for st in (
            "deploy_compose",
            "run_smoke_tests",
            "import_realm",
            "inject_secrets",
            "seed_fixtures",
            "warm_models",
            "health_check",
            "broker_preflight",
            "run_live_gate",
        ):
            assert reg.resolve(st) is not None, st

    def test_invalid_verdict_rejected(self) -> None:
        with pytest.raises(ValueError, match="verdict"):
            LiveGateInvocation(verdict="green", run_id="r")
