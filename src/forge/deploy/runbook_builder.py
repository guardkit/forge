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
    "build_revert_runbook",
    "build_candidate_teardown_runbook",
    "deploy_runbook_step_types",
    "sandbox_env",
]


def _step(step_type: str, params: dict[str, Any], index: int) -> Step:
    return Step(
        step_type=step_type,
        params=params,
        status=StepStatus.pending,
        sequence_index=index,
    )


def sandbox_env(
    profile: DeployProfile, *, inside_sandbox: bool = False
) -> dict[str, str]:
    """The sandbox settings, as the environment the deploy script reads.

    A repository that deploys into a Docker Sandbox carries a ``sandbox`` block
    in its profile (the 2026-09-06 decision). Its vetted wrapper reads the
    sandbox's settings from the environment — never by reading YAML in bash —
    so the settings are threaded into every step that runs a script: the
    deploy, the promote, the revert, the candidate teardown, and the health
    checks. The two lists are joined with commas.

    The five settings every sandbox has always ride, empty when unset. The six
    that make the sandbox carry the factory's own services (the two service
    ports, the environment file, the forge and guardkit mounts, the receipts
    root — the spec's Part O, rule 68) ride only when the profile sets them,
    so a profile without them threads exactly what it threaded before.

    WHERE THE DEPLOY RUNS DECIDES WHETHER ANY OF THEM RIDE (2026-09-09).
    Every one of these settings says how to CREATE the sandbox: its name, how
    big it is, which ports it publishes, what it may reach, which checkouts
    are mounted into it. The one program that reads them is the host wrapper,
    which makes the sandbox and then runs the repository's own deploy script
    inside it. When the factory itself lives in that sandbox the deploy step
    runs that inner script directly (see
    :meth:`forge.deploy.stage.DeployStageRunner._profile_for_run`), so there
    is nothing left to create and not one of these settings has a reader.
    ``inside_sandbox=True`` therefore sends none of them. The caller's own
    overlay is untouched either way — the candidate's environment, the live
    gate's, and the ``CANDIDATE`` / ``PROMOTE`` / ``CANDIDATE_DOWN`` signals
    are what the deploy actually reads.

    This was found the hard way on the first real merge press (2026-09-09):
    the deploy sidecar inside the sandbox refused ``SANDBOX_SIDECAR_PUBLISH``
    as a key it does not allow, and the whole journey ended in 0.16 seconds
    without a container ever starting.

    No ``sandbox`` block ⇒ an empty mapping ⇒ nothing is added to any step and
    every runbook is exactly what it was before sandboxes existed.
    """
    sandbox = profile.sandbox
    if sandbox is None or inside_sandbox:
        return {}
    env = {
        "SANDBOX_NAME": sandbox.name,
        "SANDBOX_MEMORY": sandbox.memory or "",
        "SANDBOX_CPUS": str(sandbox.cpus) if sandbox.cpus is not None else "",
        "SANDBOX_PUBLISH": ",".join(sandbox.publish),
        "SANDBOX_ALLOW_NETWORK": ",".join(sandbox.allow_network),
    }
    factory_settings = {
        "SANDBOX_SIDECAR_PUBLISH": sandbox.sidecar_publish,
        "SANDBOX_RUNNER_PUBLISH": sandbox.runner_publish,
        "SANDBOX_ENV_FILE": sandbox.env_file,
        "SANDBOX_FORGE_PATH": sandbox.forge_path,
        "SANDBOX_GUARDKIT_PATH": sandbox.guardkit_path,
        "SANDBOX_RECEIPTS_PATH": sandbox.receipts_path,
    }
    for name, value in factory_settings.items():
        if value:
            env[name] = value
    return env


def _merged_env(
    profile: DeployProfile,
    overlay: dict[str, str] | None,
    *,
    inside_sandbox: bool = False,
) -> dict[str, str]:
    """The sandbox settings plus the caller's own overlay (the overlay wins).

    ``inside_sandbox`` is the venue: True when this step runs inside the
    repository's own sandbox, where the settings that describe how to make
    that sandbox have no reader and are not sent.
    """
    merged = sandbox_env(profile, inside_sandbox=inside_sandbox)
    if overlay:
        merged.update(overlay)
    return merged


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
    compose_extra_env: dict[str, str] | None = None,
    check_extra_env: dict[str, str] | None = None,
    cwd_override: str | None = None,
    inside_sandbox: bool = False,
) -> Runbook:
    """Render the DEPLOY-stage runbook for ``profile``.

    Args:
        profile: The parsed deploy profile.
        runbook_id: Unique id for this runbook (typically the deploy_run_id).
        target: The runbook target (typically the profile ``env_id``).
        now: Creation timestamp (injected — the executor forbids argless
            ``datetime.now`` in some call sites; the caller supplies the clock).
        compose_extra_env: Candidate-then-promote sequencing (S2F) — a
            non-secret env overlay injected into the ``deploy_compose`` step's
            params (threaded verbatim to the vetted script by
            :func:`forge.executor.shell_steps.deploy_compose`). Carries the mode
            flag + addressing overlay: ``{CANDIDATE:"1", **candidate.env}`` for
            the candidate leg, ``{PROMOTE:"1"}`` for the promote leg. ``None``
            (the direct-live flow) ⇒ no ``extra_env`` key ⇒ byte-identical.
        check_extra_env: Same, for the ``health_check`` step — the candidate.env
            overlay so the candidate-leg checks hit the ``-cand`` port. ``None``
            (direct-live + promote leg's "no overlay" health check) ⇒ unchanged.
        cwd_override: Protect-main (rule 38): the working directory every step
            of THIS runbook runs in, instead of the profile's ``cwd``. The
            candidate leg passes the feature branch's laid-out tree
            (``<checkout>/.forge-candidates/<FEAT-id>``), so the repository's
            own deploy script — found relative to that directory, and
            anchoring itself to it — builds the candidate from the exact
            commit the merge will land. The promote leg never passes it: it
            runs from the checkout and re-tags the image the candidate built.
            ``None`` ⇒ the profile's ``cwd``, exactly as before.
        inside_sandbox: True when these steps run INSIDE the repository's own
            sandbox, so the settings that say how to make that sandbox are not
            threaded onto them — there is nothing there to make and nothing
            that reads them (:func:`sandbox_env`). False (every repository
            deploying through the host wrapper, and every repository with no
            sandbox at all) ⇒ exactly what it was.

    When the profile carries a ``sandbox`` block and the deploy runs on the
    host, the sandbox's settings (:func:`sandbox_env`) are added underneath
    both overlays, so the vetted wrapper knows which Docker Sandbox to run the
    deploy inside. No block ⇒ nothing is added and both steps are exactly what
    they were.

    Returns:
        A :class:`Runbook` of typed, ordered, ``pending`` steps.
    """
    cwd = cwd_override if cwd_override is not None else profile.cwd
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
    compose_env = _merged_env(
        profile, compose_extra_env, inside_sandbox=inside_sandbox
    )
    if compose_env:
        compose_params["extra_env"] = compose_env
    steps.append(_step("deploy_compose", compose_params, idx))
    idx += 1

    if profile.health_checks:
        check_params: dict[str, Any] = {
            "cwd": cwd,
            "checks": [
                {"cmd": h.cmd, "expected": h.expected}
                for h in profile.health_checks
            ],
        }
        check_env = _merged_env(
            profile, check_extra_env, inside_sandbox=inside_sandbox
        )
        if check_env:
            check_params["extra_env"] = check_env
        steps.append(_step("health_check", check_params, idx))
        idx += 1

    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=tuple(steps),
        current_step_index=0,
        status=StepStatus.pending,
        created_at=now,
    )


def build_revert_runbook(
    profile: DeployProfile,
    *,
    runbook_id: str,
    target: str,
    rollback_image_ref: str,
    now: datetime,
    inside_sandbox: bool = False,
) -> Runbook:
    """Render the REVERT runbook (O-32) — re-deploy the kept ``:rollback-*`` tag.

    A FAILED post-deploy live-gate means the current build is NOT verified, so
    the runner rolls back by re-deploying the prior image through the SAME deploy
    seam. The revert is a single focused ``deploy_compose`` step (the prior
    build's compose invocation carrying the rollback image ref) — deliberately
    NOT the full DEPLOY pre-flight (broker_preflight/inject_secrets/seed/warm):
    a rollback re-serves a known-good image, it does not re-provision. The
    ``rollback_image_ref`` rides in the step params so a dry-run records the
    intent (the hermetic gate) and the profile's deploy script consumes it (env /
    compose IMAGE var) on a live revert.

    When the profile carries a ``sandbox`` block and the revert runs on the
    host, the sandbox's settings (:func:`sandbox_env`) ride in the step's
    ``extra_env`` too — a revert runs inside the same Docker Sandbox the
    deploy ran in.

    Args:
        profile: The parsed deploy profile (its compose invocation is reused).
        runbook_id: Unique id for this runbook (typically ``revert-<deploy_run_id>``).
        target: The runbook target (typically the profile ``env_id``).
        rollback_image_ref: The kept ``:rollback-*`` image tag to bring back up.
        now: Creation timestamp (injected clock).
        inside_sandbox: True when this step runs INSIDE the repository's own
            sandbox — the settings that say how to make that sandbox are not
            threaded onto it (:func:`sandbox_env`).
    """
    compose_params: dict[str, Any] = {
        "cwd": profile.cwd,
        "compose_file": profile.compose.file,
        "compose_profile": profile.compose.profile,
        "rollback_image_ref": rollback_image_ref,
        "revert": True,
    }
    revert_env = sandbox_env(profile, inside_sandbox=inside_sandbox)
    if revert_env:
        compose_params["extra_env"] = revert_env
    if profile.compose.script is not None:
        compose_params["script"] = profile.compose.script
    if profile.compose.env_file is not None:
        compose_params["env_file"] = profile.compose.env_file
    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=(_step("deploy_compose", compose_params, 0),),
        current_step_index=0,
        status=StepStatus.pending,
        created_at=now,
    )


def build_candidate_teardown_runbook(
    profile: DeployProfile,
    *,
    runbook_id: str,
    target: str,
    extra_env: dict[str, str],
    now: datetime,
    inside_sandbox: bool = False,
) -> Runbook:
    """Render the candidate-teardown runbook (S2F) — a single ``deploy_compose``.

    Tears the candidate compose project (``<live project>-cand``) down. Fired
    on a candidate-gate FAIL (before any promote — the LIVE name is never
    touched) and, when ``candidate.keep`` is false, after a successful promote.
    The teardown signal + candidate addressing ride in the step's ``extra_env``
    ({CANDIDATE_DOWN:"1", **candidate.env}) so the vetted script brings DOWN the
    ``-cand`` project (``down -v``) rather than re-deploying it. Deliberately a
    single focused step — no pre-flight, no health check. When the profile
    carries a ``sandbox`` block and the teardown runs on the host, the
    sandbox's settings (:func:`sandbox_env`) ride alongside, so the teardown
    happens inside the same Docker Sandbox.

    The teardown is built exactly the way the deploy is, on purpose: whatever
    the deploy step was sent, the step that takes the candidate down again is
    sent too. When they differed, a teardown was refused for a key the deploy
    had already been refused for, and the log said the candidate might still
    be up (2026-09-09).

    Args:
        profile: The parsed deploy profile (its compose invocation is reused).
        runbook_id: Unique id (typically ``teardown-cand-<deploy_run_id>``).
        target: The runbook target (typically the profile ``env_id``).
        extra_env: The teardown env overlay ({CANDIDATE_DOWN:"1", ...}).
        now: Creation timestamp (injected clock).
        inside_sandbox: True when this step runs INSIDE the repository's own
            sandbox — the settings that say how to make that sandbox are not
            threaded onto it (:func:`sandbox_env`).
    """
    compose_params: dict[str, Any] = {
        "cwd": profile.cwd,
        "compose_file": profile.compose.file,
        "compose_profile": profile.compose.profile,
        "candidate_down": True,
        "extra_env": _merged_env(
            profile, extra_env, inside_sandbox=inside_sandbox
        ),
    }
    if profile.compose.script is not None:
        compose_params["script"] = profile.compose.script
    if profile.compose.env_file is not None:
        compose_params["env_file"] = profile.compose.env_file
    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=(_step("deploy_compose", compose_params, 0),),
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
