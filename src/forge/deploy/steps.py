"""Deploy step-type handlers (WS2-B8, scope-design §4).

Extends the FEAT-FMDR runbook step library with the seven step types the deploy
+ live-gate stages force into existence — **never a second executor** (D13:
harvest the engine, don't pre-build it). Each handler satisfies the
:class:`forge.executor.registry.StepHandler` protocol (``(step) -> StepOutcome``,
never raises) and is registered into a :class:`StepTypeRegistry` so the shipped
:class:`forge.executor.RunbookExecutor` dispatches it with zero executor edits.

The seven types:

    import_realm      — import a realm export (e.g. Keycloak) — subprocess
    inject_secrets    — inject register-key REFS (names only) — never values
    seed_fixtures     — run seed scripts against golden state — subprocess
    warm_models       — warm llama-swap models before the gate — subprocess
    health_check      — run health checks, compare to expected — subprocess
    broker_preflight  — diff live broker vs the F6 contract — BrokerInspector seam
    run_live_gate     — shell ``guardkit qa live-gate`` — LiveGateInvoker seam

Handlers are built as closures over injected dependencies (dry-run flag, the
live-gate invoker, the broker inspector, a secret-presence resolver) so the
same registry works for a dry-run, a unit test (fake seams), and a live run
(real seams) without changing the executor.

Dry-run discipline: a dry-run step records what it *would* do (``dry_run=True``)
and passes — an explicitly-labelled non-action, never a fabricated success on a
live system. Secret VALUES never enter a result dict (WS2-B8 guardrail): the
secret handler records only ref NAMES and whether each is *present*, never the
value.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from forge.deploy.live_gate import (
    BrokerInspector,
    LiveGateInvoker,
    LiveGateSeamError,
)
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.executor.shell_steps import (
    DEFAULT_OUTPUT_CAP_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ScriptRunner,
    _run_script_step,
)
from forge.persistence.repositories.runbook_models import Step, StepStatus

__all__ = [
    "DEPLOY_STEP_TYPES",
    "register_deploy_handlers",
    "make_deploy_compose_handler",
    "make_run_smoke_tests_handler",
    "make_import_realm_handler",
    "make_inject_secrets_handler",
    "make_seed_fixtures_handler",
    "make_warm_models_handler",
    "make_health_check_handler",
    "make_broker_preflight_handler",
    "make_run_live_gate_handler",
]

#: The seven NEW step types this module adds (scope-design §4). The two FMDR
#: shell steps (deploy_compose, run_smoke_tests) are reused, not re-invented —
#: wrapped only to add the dry-run guard. Exported so tests and the runbook
#: builder can assert coverage without re-listing the strings.
DEPLOY_STEP_TYPES: tuple[str, ...] = (
    "import_realm",
    "inject_secrets",
    "seed_fixtures",
    "warm_models",
    "health_check",
    "broker_preflight",
    "run_live_gate",
)

#: Present-check resolver: True iff the register key is available in the
#: environment. NEVER returns the value (WS2-B8 secrets-are-refs guardrail).
SecretPresenceResolver = Callable[[str], bool]


def _default_secret_presence(name: str) -> bool:
    return name in os.environ


def _run_scripts(
    scripts: list[dict[str, Any]],
    *,
    default_cwd: str | None,
    default_env_file: str | None,
    dry_run: bool,
    runner: ScriptRunner = _run_script_step,
    extra_env: dict[str, str] | None = None,
) -> StepOutcome:
    """Run a list of ``{script, cwd?, env_file?}`` entries as subprocess steps.

    Shared core for the subprocess-shaped deploy steps. In dry-run, records the
    scripts it would run and passes. Live, runs each via ``runner`` (default =
    the FMDR ``_run_script_step`` core, credential-scrubbed, never raises); the
    first non-zero exit fails the step. The ``runner`` seam lets the
    docker-touching ``health_check`` step route through the deploy sidecar (S1)
    while the DB/model-touching steps stay on the in-process default.

    ``extra_env`` (candidate-then-promote sequencing, S2F): a non-secret env
    overlay threaded to EVERY script's subprocess (e.g. the candidate.env
    overlay pointing the candidate-leg health checks at the ``-cand`` port).
    ``None`` (every legacy call) ⇒ ``extra_env=None`` to the runner ⇒
    byte-identical to before the overlay existed.
    """
    if dry_run:
        return StepOutcome(
            status=StepStatus.passed,
            result={"dry_run": True, "would_run": scripts},
        )
    if not scripts:
        return StepOutcome(status=StepStatus.passed, result={"ran": []})
    ran: list[dict[str, Any]] = []
    for entry in scripts:
        cwd = entry.get("cwd", default_cwd)
        script = entry["script"]
        env_file = entry.get("env_file", default_env_file)
        if cwd is None:
            return StepOutcome(
                status=StepStatus.failed,
                result={
                    "error": f"no cwd for script {script!r}",
                    "ran": ran,
                },
            )
        exit_code, output = runner(
            cwd=cwd,
            script=script,
            env_file=env_file,
            timeout=entry.get("timeout", DEFAULT_TIMEOUT_SECONDS),
            output_cap=entry.get("output_cap", DEFAULT_OUTPUT_CAP_BYTES),
            extra_env=extra_env,
        )
        ran.append(
            {"script": script, "exit_code": exit_code, "captured_output": output}
        )
        if exit_code != 0:
            return StepOutcome(status=StepStatus.failed, result={"ran": ran})
    return StepOutcome(status=StepStatus.passed, result={"ran": ran})


# ---------------------------------------------------------------------------
# deploy_compose / run_smoke_tests (FMDR shell steps, dry-run-guarded reuse)
# ---------------------------------------------------------------------------


def make_deploy_compose_handler(
    *, dry_run: bool, script_runner: ScriptRunner | None = None
):
    """Dry-run-aware wrapper over the shipped FMDR ``deploy_compose`` handler.

    Live, it delegates VERBATIM to :func:`forge.executor.shell_steps.deploy_compose`
    (the shipped step — no logic duplicated, the FMDR executor is extended not
    replaced). Dry-run records the params it would run and passes.

    When ``script_runner`` is supplied (deploy.execution_surface='sidecar'), it
    is threaded into the shipped handler as its execution seam, so the O-32
    revert-env threading is preserved and NOT duplicated. ``script_runner=None``
    (the local default) is byte-identical to before the seam existed.
    """

    def deploy_compose_step(step: Step) -> StepOutcome:
        if dry_run:
            return StepOutcome(
                status=StepStatus.passed,
                result={"dry_run": True, "would_deploy_compose": dict(step.params)},
            )
        from forge.executor.shell_steps import deploy_compose as _fmdr_deploy_compose

        if script_runner is None:
            return _fmdr_deploy_compose(step)
        return _fmdr_deploy_compose(step, runner=script_runner)

    return deploy_compose_step


def make_run_smoke_tests_handler(*, dry_run: bool):
    """Dry-run-aware wrapper over the shipped FMDR ``run_smoke_tests`` handler."""

    def run_smoke_tests_step(step: Step) -> StepOutcome:
        if dry_run:
            return StepOutcome(
                status=StepStatus.passed,
                result={"dry_run": True, "would_run_smoke_tests": dict(step.params)},
            )
        from forge.executor.shell_steps import run_smoke_tests as _fmdr_run_smoke

        return _fmdr_run_smoke(step)

    return run_smoke_tests_step


# ---------------------------------------------------------------------------
# import_realm
# ---------------------------------------------------------------------------


def make_import_realm_handler(*, dry_run: bool):
    """Build the ``import_realm`` handler.

    Params: ``{realm_import: <ref>, script?, cwd?, env_file?}``. The realm
    export is imported by running ``script`` (a vetted importer) — the ref is
    the export path, passed to the script, never inlined shell. Dry-run records
    the realm ref it would import.
    """

    def import_realm(step: Step) -> StepOutcome:
        realm_ref = step.params.get("realm_import")
        if not realm_ref:
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "import_realm requires 'realm_import'"},
            )
        script = step.params.get("script")
        if dry_run or script is None:
            return StepOutcome(
                status=StepStatus.passed,
                result={
                    "dry_run": True,
                    "would_import_realm": realm_ref,
                    **(
                        {"note": "no importer script; recorded only"}
                        if script is None and not dry_run
                        else {}
                    ),
                },
            )
        return _run_scripts(
            [
                {
                    "script": script,
                    "cwd": step.params.get("cwd"),
                    "env_file": step.params.get("env_file"),
                }
            ],
            default_cwd=step.params.get("cwd"),
            default_env_file=step.params.get("env_file"),
            dry_run=False,
        )

    return import_realm


# ---------------------------------------------------------------------------
# inject_secrets (register REFS only)
# ---------------------------------------------------------------------------


def make_inject_secrets_handler(
    *,
    dry_run: bool,
    presence_resolver: SecretPresenceResolver | None = None,
):
    """Build the ``inject_secrets`` handler (register REFS only — never values).

    Params: ``{refs: [<name>, ...]}``. The handler validates that every entry is
    a bare register-key NAME (no ``=``/whitespace), records the NAMES and, live,
    whether each is *present* in the register/environment — the VALUE is never
    read into the result, a payload, or a record (WS2-B8 guardrail). Injection
    itself (writing an env file the compose step sources) is the operator/WS5
    boundary; this step's job is to fail closed if a required ref is absent so a
    deploy never silently proceeds with a missing secret.
    """
    resolve = presence_resolver or _default_secret_presence

    def inject_secrets(step: Step) -> StepOutcome:
        refs = step.params.get("refs", [])
        if not isinstance(refs, (list, tuple)):
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "inject_secrets 'refs' must be a list of key names"},
            )
        names: list[str] = []
        for r in refs:
            if (
                not isinstance(r, str)
                or "=" in r
                or any(c.isspace() for c in r)
                or not r.strip()
            ):
                return StepOutcome(
                    status=StepStatus.failed,
                    result={
                        "error": (
                            f"secret ref {r!r} is not a bare key name; secrets are "
                            "register REFS only (names, never values)"
                        )
                    },
                )
            names.append(r.strip())

        if dry_run:
            return StepOutcome(
                status=StepStatus.passed,
                result={"dry_run": True, "would_inject_refs": names},
            )

        # Live: fail closed on any absent ref. Record NAMES + presence only.
        presence = {name: bool(resolve(name)) for name in names}
        missing = [name for name, present in presence.items() if not present]
        if missing:
            return StepOutcome(
                status=StepStatus.failed,
                result={"injected_refs": names, "missing_refs": missing},
            )
        return StepOutcome(
            status=StepStatus.passed,
            result={"injected_refs": names, "all_present": True},
        )

    return inject_secrets


# ---------------------------------------------------------------------------
# seed_fixtures
# ---------------------------------------------------------------------------


def make_seed_fixtures_handler(*, dry_run: bool):
    """Build the ``seed_fixtures`` handler.

    Params: ``{fixtures: [{script, golden_state_ref?, cwd?, env_file?}], cwd?}``.
    Runs each seed script (break-glass golden-state restore + seed SQL). Dry-run
    records the fixtures it would seed.
    """

    def seed_fixtures(step: Step) -> StepOutcome:
        fixtures = step.params.get("fixtures", [])
        if not isinstance(fixtures, (list, tuple)):
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "seed_fixtures 'fixtures' must be a list"},
            )
        scripts = [
            {
                "script": f["script"],
                "cwd": f.get("cwd", step.params.get("cwd")),
                "env_file": f.get("env_file", step.params.get("env_file")),
            }
            for f in fixtures
            if isinstance(f, dict) and f.get("script")
        ]
        return _run_scripts(
            scripts,
            default_cwd=step.params.get("cwd"),
            default_env_file=step.params.get("env_file"),
            dry_run=dry_run,
        )

    return seed_fixtures


# ---------------------------------------------------------------------------
# warm_models
# ---------------------------------------------------------------------------


def make_warm_models_handler(*, dry_run: bool):
    """Build the ``warm_models`` handler.

    Params: ``{models: [{model, warm_up_action?}], cwd?, env_file?}``. The
    warm-up action is a vetted script that issues the cold-load turn (cold loads
    ~22–66s, once >120s — study-tutor retro L6). Dry-run records the models it
    would warm.
    """

    def warm_models(step: Step) -> StepOutcome:
        models = step.params.get("models", [])
        if not isinstance(models, (list, tuple)):
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "warm_models 'models' must be a list"},
            )
        if dry_run:
            return StepOutcome(
                status=StepStatus.passed,
                result={
                    "dry_run": True,
                    "would_warm": [
                        m.get("model") if isinstance(m, dict) else m for m in models
                    ],
                },
            )
        scripts = [
            {
                "script": m["warm_up_action"],
                "cwd": step.params.get("cwd"),
                "env_file": step.params.get("env_file"),
            }
            for m in models
            if isinstance(m, dict) and m.get("warm_up_action")
        ]
        return _run_scripts(
            scripts,
            default_cwd=step.params.get("cwd"),
            default_env_file=step.params.get("env_file"),
            dry_run=False,
        )

    return warm_models


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def make_health_check_handler(
    *, dry_run: bool, script_runner: ScriptRunner | None = None
):
    """Build the ``health_check`` handler.

    Params: ``{checks: [{cmd (script), expected?, cwd?}], cwd?}``. Each check is
    a vetted script whose exit code is the health verdict (0 = healthy). Dry-run
    records the checks it would run.

    ``script_runner`` (deploy.execution_surface='sidecar') routes the health
    scripts through the deploy sidecar; ``None`` (the local default) keeps the
    in-process subprocess core — byte-identical to before the seam existed.

    A ``extra_env`` step param (candidate-then-promote sequencing, S2F) is a
    non-secret env overlay threaded to every health-check subprocess — the
    candidate.env addressing overlay so the candidate-leg checks hit the
    ``-cand`` port. Absent ⇒ ``None`` ⇒ byte-identical.
    """
    runner = script_runner or _run_script_step

    def health_check(step: Step) -> StepOutcome:
        checks = step.params.get("checks", [])
        if not isinstance(checks, (list, tuple)):
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "health_check 'checks' must be a list"},
            )
        scripts = [
            {
                "script": c["cmd"],
                "cwd": c.get("cwd", step.params.get("cwd")),
                "env_file": c.get("env_file", step.params.get("env_file")),
            }
            for c in checks
            if isinstance(c, dict) and c.get("cmd")
        ]
        params_extra_env = step.params.get("extra_env")
        extra_env = (
            {
                k: v
                for k, v in params_extra_env.items()
                if isinstance(k, str) and isinstance(v, str)
            }
            if isinstance(params_extra_env, dict)
            else None
        )
        return _run_scripts(
            scripts,
            default_cwd=step.params.get("cwd"),
            default_env_file=step.params.get("env_file"),
            dry_run=dry_run,
            runner=runner,
            extra_env=extra_env,
        )

    return health_check


# ---------------------------------------------------------------------------
# broker_preflight
# ---------------------------------------------------------------------------


def make_broker_preflight_handler(*, broker_inspector: BrokerInspector):
    """Build the ``broker_preflight`` handler (LPA-16).

    Params: ``{broker_contract_ref: <ref>}``. Diffs live broker state against
    the F6 contract via the injected :class:`BrokerInspector`. Broker drift
    fails the step (deploy pre-flight refuses to start services against a
    drifted broker). An unconfigured inspector RAISES (FEAT-DD4F) — caught here
    and mapped to an honest failed outcome, never a silent green.
    """

    def broker_preflight(step: Step) -> StepOutcome:
        ref = step.params.get("broker_contract_ref")
        if not ref:
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "broker_preflight requires 'broker_contract_ref'"},
            )
        try:
            diff = broker_inspector.diff(ref)
        except LiveGateSeamError as exc:
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": str(exc), "broker_contract_ref": ref},
            )
        if not diff.matches:
            return StepOutcome(
                status=StepStatus.failed,
                result={
                    "broker_contract_ref": ref,
                    "drifts": list(diff.drifts),
                },
            )
        return StepOutcome(
            status=StepStatus.passed,
            result={
                "broker_contract_ref": ref,
                "matches": True,
                "dry_run": diff.dry_run,
            },
        )

    return broker_preflight


# ---------------------------------------------------------------------------
# run_live_gate
# ---------------------------------------------------------------------------


def make_run_live_gate_handler(*, live_gate_invoker: LiveGateInvoker):
    """Build the ``run_live_gate`` handler (shells ``guardkit qa live-gate``).

    Params: ``{feature: <id>, target: <env>, gates?: [<id>]}``. Delegates to the
    injected :class:`LiveGateInvoker` (which shells the FROZEN guardkit seam) and
    packs the results-envelope verdict + refs into the step result so the
    DeployStageRunner can build the B7 ``QAVerdictPayload`` /
    ``LiveGateResultPayload``.

    Step-status mapping: the step **passes** whenever the invoker produced a
    verdict — the four-for-four verdict itself (pass/fail/instrument_fail/
    environment_fail) is carried in ``result['verdict']`` and routed by the
    stage per DF-017 (instrument/environment never indict the feature). Only an
    invoker error (e.g. an unconfigured seam raising) fails the step. This keeps
    the honest verdict on the record instead of collapsing an
    ``environment_fail`` into a runbook escalation.
    """

    def run_live_gate(step: Step) -> StepOutcome:
        feature = step.params.get("feature")
        target = step.params.get("target")
        if not feature or not target:
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": "run_live_gate requires 'feature' and 'target'"},
            )
        gates = tuple(step.params.get("gates", []) or [])
        try:
            invocation = live_gate_invoker.invoke(
                feature=feature, target=target, gates=gates
            )
        except LiveGateSeamError as exc:
            return StepOutcome(
                status=StepStatus.failed,
                result={"error": str(exc), "feature": feature, "target": target},
            )
        return StepOutcome(
            status=StepStatus.passed,
            result={
                "verdict": invocation.verdict,
                "run_id": invocation.run_id,
                "gate_ids": list(invocation.gate_ids),
                "assertions": list(invocation.assertions),
                "evidence_index_ref": invocation.evidence_index_ref,
                "app_url": invocation.app_url,
                "screenshot_refs": list(invocation.screenshot_refs),
                "trace_refs": list(invocation.trace_refs),
                "dispositions_ref": invocation.dispositions_ref,
                "attempts_ledger_ref": invocation.attempts_ledger_ref,
                "leak_sweep_findings": invocation.leak_sweep_findings,
                "dry_run": invocation.dry_run,
            },
        )

    return run_live_gate


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def register_deploy_handlers(
    registry: StepTypeRegistry,
    *,
    dry_run: bool,
    live_gate_invoker: LiveGateInvoker,
    broker_inspector: BrokerInspector,
    presence_resolver: SecretPresenceResolver | None = None,
    script_runner: ScriptRunner | None = None,
) -> None:
    """Register all seven deploy step-type handlers into ``registry``.

    Mirrors :func:`forge.executor.shell_steps.register_shell_handlers`. The
    injected seams (``live_gate_invoker``/``broker_inspector``) and ``dry_run``
    flag close over the handlers, so a dry-run, a test (fake seams), and a live
    run share one registry and one executor. Registers the two reused FMDR
    shell steps (dry-run-guarded) plus the seven new deploy step types.

    ``script_runner`` (deploy.execution_surface='sidecar') routes ONLY the
    docker-touching steps — ``deploy_compose`` and ``health_check`` — through the
    deploy sidecar (S1). The DB/model/secret-touching steps (seed/warm/import/
    smoke) always run in-process. ``script_runner=None`` (the local default) is a
    byte-identical no-op: every step keeps the in-process subprocess core.
    """
    registry.register(
        "deploy_compose",
        make_deploy_compose_handler(dry_run=dry_run, script_runner=script_runner),
    )
    registry.register("run_smoke_tests", make_run_smoke_tests_handler(dry_run=dry_run))
    registry.register("import_realm", make_import_realm_handler(dry_run=dry_run))
    registry.register(
        "inject_secrets",
        make_inject_secrets_handler(
            dry_run=dry_run, presence_resolver=presence_resolver
        ),
    )
    registry.register("seed_fixtures", make_seed_fixtures_handler(dry_run=dry_run))
    registry.register("warm_models", make_warm_models_handler(dry_run=dry_run))
    registry.register(
        "health_check",
        make_health_check_handler(dry_run=dry_run, script_runner=script_runner),
    )
    registry.register(
        "broker_preflight",
        make_broker_preflight_handler(broker_inspector=broker_inspector),
    )
    registry.register(
        "run_live_gate",
        make_run_live_gate_handler(live_gate_invoker=live_gate_invoker),
    )
