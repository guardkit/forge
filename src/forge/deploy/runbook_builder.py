"""Render a deploy profile into a typed runbook (WS2-B8, scope-design §4).

"executor consumes the deploy profile and renders a typed runbook
(deploy_compose, new import_realm, inject_secrets, seed_fixtures, warm_models,
health_check, broker_preflight, run_live_gate step types)" — this module IS that
renderer. It emits :class:`forge.persistence.repositories.runbook_models.Runbook`
values the **shipped** FMDR :class:`forge.executor.RunbookExecutor` runs
verbatim — never a second executor.

Two runbooks are produced so the DEPLOY and LIVE_GATE stages emit distinct
lifecycle events (scope §4 event flow steps 2 and 3):

- :func:`build_deploy_runbook` — the DEPLOY stage: pre-flight → typed deploy
  steps → health checks. Order (scope §4): broker_preflight → import_realm →
  inject_secrets → seed_fixtures → warm_models → deploy_compose → health_check.
  A section is emitted only when the profile carries it (a minimal profile is a
  single deploy_compose step).
- :func:`build_live_gate_runbook` — the LIVE_GATE stage: a single
  run_live_gate step shelling ``guardkit qa live-gate``.

Steps are ``pending`` with ``sequence_index`` in emission order; ``params`` carry
exactly what each handler reads (see :mod:`forge.deploy.steps`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from forge.deploy.profile import DeployProfile
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus

__all__ = [
    "build_deploy_runbook",
    "build_live_gate_runbook",
    "deploy_runbook_step_types",
]


def _step(step_type: str, params: dict[str, Any], index: int) -> Step:
    return Step(
        step_type=step_type,
        params=params,
        status=StepStatus.pending,
        sequence_index=index,
    )


def deploy_runbook_step_types(profile: DeployProfile) -> list[str]:
    """Return the ordered step types a DEPLOY runbook for ``profile`` will emit.

    Pure — lets callers/tests assert the rendered shape without constructing a
    Runbook (which needs a clock).
    """
    types: list[str] = []
    if profile.broker_contract_ref:
        types.append("broker_preflight")
    if profile.realm_import:
        types.append("import_realm")
    if profile.secret_injection:
        types.append("inject_secrets")
    if profile.seed_fixture_contract:
        types.append("seed_fixtures")
    if profile.models_required:
        types.append("warm_models")
    types.append("deploy_compose")
    if profile.health_checks:
        types.append("health_check")
    return types


def build_deploy_runbook(
    profile: DeployProfile,
    *,
    runbook_id: str,
    target: str,
    now: datetime,
) -> Runbook:
    """Render the DEPLOY-stage runbook for ``profile``.

    Args:
        profile: The parsed deploy profile.
        runbook_id: Unique id for this runbook (typically the deploy_run_id).
        target: The runbook target (typically the profile ``env_id``).
        now: Creation timestamp (injected — the executor forbids argless
            ``datetime.now`` in some call sites; the caller supplies the clock).

    Returns:
        A :class:`Runbook` of typed, ordered, ``pending`` steps.
    """
    cwd = profile.cwd
    steps: list[Step] = []
    idx = 0

    if profile.broker_contract_ref:
        steps.append(
            _step(
                "broker_preflight",
                {"broker_contract_ref": profile.broker_contract_ref},
                idx,
            )
        )
        idx += 1

    if profile.realm_import:
        steps.append(
            _step(
                "import_realm",
                {"realm_import": profile.realm_import, "cwd": cwd},
                idx,
            )
        )
        idx += 1

    if profile.secret_injection:
        steps.append(
            _step(
                "inject_secrets",
                {"refs": list(profile.secret_injection)},
                idx,
            )
        )
        idx += 1

    if profile.seed_fixture_contract:
        steps.append(
            _step(
                "seed_fixtures",
                {
                    "cwd": cwd,
                    "fixtures": [
                        {
                            "script": f.script,
                            "golden_state_ref": f.golden_state_ref,
                        }
                        for f in profile.seed_fixture_contract
                    ],
                },
                idx,
            )
        )
        idx += 1

    if profile.models_required:
        steps.append(
            _step(
                "warm_models",
                {
                    "cwd": cwd,
                    "models": [
                        {"model": m.model, "warm_up_action": m.warm_up_action}
                        for m in profile.models_required
                    ],
                },
                idx,
            )
        )
        idx += 1

    # deploy_compose is always present (compose is a required profile field).
    compose_params: dict[str, Any] = {
        "cwd": cwd,
        "compose_file": profile.compose.file,
        "compose_profile": profile.compose.profile,
    }
    if profile.compose.script is not None:
        compose_params["script"] = profile.compose.script
    if profile.compose.env_file is not None:
        compose_params["env_file"] = profile.compose.env_file
    steps.append(_step("deploy_compose", compose_params, idx))
    idx += 1

    if profile.health_checks:
        steps.append(
            _step(
                "health_check",
                {
                    "cwd": cwd,
                    "checks": [
                        {"cmd": h.cmd, "expected": h.expected}
                        for h in profile.health_checks
                    ],
                },
                idx,
            )
        )
        idx += 1

    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=tuple(steps),
        current_step_index=0,
        status=StepStatus.pending,
        created_at=now,
    )


def build_live_gate_runbook(
    profile: DeployProfile,
    *,
    runbook_id: str,
    target: str,
    feature: str,
    now: datetime,
    gates: tuple[str, ...] = (),
) -> Runbook:
    """Render the LIVE_GATE-stage runbook (a single run_live_gate step).

    Args:
        profile: The parsed deploy profile (its ``env_id`` is the gate target).
        runbook_id: Unique id for this runbook.
        target: The runbook target.
        feature: The feature id passed to ``guardkit qa live-gate --feature``.
        now: Creation timestamp (injected clock).
        gates: Optional explicit gate-id list.
    """
    step = _step(
        "run_live_gate",
        {
            "feature": feature,
            "target": profile.env_id,
            "gates": list(gates),
        },
        0,
    )
    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=(step,),
        current_step_index=0,
        status=StepStatus.pending,
        created_at=now,
    )
