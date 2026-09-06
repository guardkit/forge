"""Deploy-profile loader (WS2-B8, scope-design §4).

A deploy profile (``deploy/profile.yaml`` per target repo) is the tier-2 format
(F-deploy-profile) the DEPLOY stage consumes to render a typed runbook. This
module is the *consumer* of that format — B10 authors the canonical schema; B8
builds the loader against scope-design §4's field list and files a dated
reconcile note for B10 (the B4/B5 hedge pattern).

Field list (scope-design §4, verbatim):

    {env_id, hosts: [{host, role}], compose: {file, profile},
     secret_injection: [register-key refs — names only, WS5 owns values],
     seed_fixture_contract: [{script, golden_state_ref}], realm_import,
     models_required: [llama-swap model + warm-up action],
     health_checks: [{cmd, expected}], broker_contract_ref (F6 broker section),
     reservation: {resource, quiet_window}}

Guardrail (WS2-B8): **secrets are register REFS only.** ``secret_injection``
entries are register *key names*, never values. The loader refuses any entry
that looks like it carries a value (contains ``=`` or whitespace, or is a
mapping with a value-bearing key) so a secret value can never enter a profile,
a payload, or a deploy record. Values are resolved at execution time by the
step handler from the register/environment — never read here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: A register-key NAME: a conservative identifier (letters, digits, and the
#: ``.`` ``_`` ``-`` separators of env-var / dotted / scoped keys). Anything
#: outside this set (``=``, ``:``, ``@``, ``/``, whitespace) marks a smuggled
#: VALUE — refused (secrets are register REFS ONLY, WS2-B8 guardrail).
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: An environment-variable NAME for the optional ``live_gate.env`` map: strict
#: UPPER_SNAKE_CASE (leading letter, then upper/digits/underscore). Values are
#: non-secret strings only (base URLs and the like); secrets stay register REFS.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: A deployment sandbox NAME (``sandbox.name``): the name ``sbx`` is given for
#: the repository's one long-lived sandbox. Lower-case letters, digits and
#: hyphens, 2 to 63 characters, starting with a letter or a digit — the shape
#: ``sbx`` accepts and the shape a host name may take.
_SANDBOX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

#: One host in a ``sandbox.allow_network`` rule: a name (``pypi.org``), a
#: leading-wildcard name (``*.debian.org``) or an address (``172.30.1.253``).
#: No scheme, no path, no spaces — the policy takes a host, not a URL.
_SANDBOX_HOST_RE = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

__all__ = [
    "DeployProfile",
    "DeployHost",
    "ComposeSpec",
    "SeedFixture",
    "ModelRequirement",
    "HealthCheck",
    "Reservation",
    "DeployLiveGate",
    "DeployCandidate",
    "DeploySandbox",
    "DeployProfileError",
    "load_deploy_profile",
]


class DeployProfileError(ValueError):
    """Raised when a deploy profile is missing, malformed, or unsafe.

    Domain-shaped so callers can distinguish a bad profile from an arbitrary
    ``ValueError``. Carries a clear message naming the offending field.
    """


# ---------------------------------------------------------------------------
# Value types (frozen — a profile is immutable once loaded)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeployHost:
    """One host in the deploy target set."""

    host: str
    role: str


@dataclass(frozen=True, slots=True)
class ComposeSpec:
    """The compose invocation for ``deploy_compose``.

    ``file``/``profile`` are the scope-§4 fields (the compose file + optional
    compose profile). ``script``/``env_file`` are the B8→B10 bridge: the shipped
    FMDR ``deploy_compose`` step wraps a *vetted deploy script* (e.g.
    ``deploy.sh``) rather than inline ``docker compose`` (typed steps, never
    freehand shell — the D12 safety property). ``script`` names that wrapper;
    when absent the builder records a dry-run/deferred deploy_compose (it cannot
    invent a live invocation). Flagged for B10's canonical schema (dated note).
    """

    file: str
    profile: str | None = None
    script: str | None = None
    env_file: str | None = None


@dataclass(frozen=True, slots=True)
class SeedFixture:
    """One seed-fixture contract entry (``seed_fixtures``)."""

    script: str
    golden_state_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequirement:
    """A required llama-swap model plus its warm-up action (``warm_models``)."""

    model: str
    warm_up_action: str | None = None


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One health-check command + its expected signal (``health_check``)."""

    cmd: str
    expected: str | None = None


@dataclass(frozen=True, slots=True)
class Reservation:
    """The environment-reservation lease request (scope §4 reservation)."""

    resource: str
    quiet_window: str | None = None


@dataclass(frozen=True, slots=True)
class DeployLiveGate:
    """The per-target live-gate driver spec (the F16 real backend, C4-prep).

    Names the target repo's own live-gate driver — the honest per-target
    command that injects a minimal F16 perishable-prereq provider into the
    UNMODIFIED guardkit ``LiveGateRunner`` (see
    :class:`forge.deploy.live_gate.RepoDriverLiveGateInvoker` for the full F16
    story). Absent from a profile ⇒ the deploy stage's live-gate seam stays
    ``Unconfigured`` and loud-fails (deny by default — a fake pass is worse than
    none).

    Attributes:
        driver: The driver argv (min 1 element), e.g.
            ``["python3", "qa/gates/local_live_gate.py"]`` — resolved relative
            to the target repo (the subprocess ``cwd``).
        gates: Optional explicit gate-id subset; empty ⇒ all registered gates.
        timeout_seconds: Hard wall on the driver subprocess (default 600).
        env: Optional NON-SECRET env overlay for the driver (UPPER_SNAKE names,
            string values — base URLs and the like). Secrets stay register REFS
            (``secret_injection``), never inlined here.
    """

    driver: tuple[str, ...]
    gates: tuple[str, ...] = ()
    timeout_seconds: int = 600
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeployCandidate:
    """The optional candidate-then-promote overlay (S2F, execution-surface design).

    When a profile carries a ``candidate`` section, the DEPLOY stage stands the
    build up first under a SEPARATE compose project (``<live project>-cand``),
    gates it, and only re-tags-and-promotes it to the live name on a PASS — the
    live name is never touched by a candidate that fails its gate. Absent ⇒
    ``None`` ⇒ byte-identical to today's direct-live flow.

    Attributes:
        env: NON-SECRET env overlay (UPPER_SNAKE names, string values — e.g.
            ``CANDIDATE_PORT=8902``, ``API_TEST_BASE_URL=http://localhost:8902``)
            threaded, alongside ``CANDIDATE=1``, to the candidate-leg
            ``deploy_compose`` step, to the candidate-leg ``health_check``
            scripts, and to the candidate-leg live-gate driver env — so those
            three all address the candidate instance, not the live one. Secrets
            stay register REFS (``secret_injection``), never inlined here. Same
            validation idiom as :class:`DeployLiveGate.env`.
        keep: When True, leave the candidate project up after a successful
            promote (for manual poking); when False (the default), tear it down.
    """

    env: dict[str, str] = field(default_factory=dict)
    keep: bool = False


@dataclass(frozen=True, slots=True)
class DeploySandbox:
    """The Docker Sandbox this repository deploys into (2026-09-06 decision).

    Every merge deploys the feature into a Docker Sandbox — a small virtual
    machine with its own kernel and its own Docker engine, made by Docker's
    ``sbx`` tool. Each deployable repository owns one long-lived sandbox that
    bind-mounts the checkout at its host path, so the profile's absolute
    ``cwd`` is the same inside the sandbox and out. The repository's existing
    deploy script then runs unchanged against the sandbox's own Docker engine.

    This block is the single source for that sandbox's settings. Absent from a
    profile ⇒ ``None`` ⇒ everything behaves exactly as it did before this
    block existed (the host's own Docker engine, no sandbox anywhere).

    Attributes:
        name: The sandbox's name, e.g. ``api-test-deploy``. Lower-case
            letters, digits and hyphens, 2 to 63 characters.
        memory: How much memory the sandbox gets, written the way ``sbx``
            takes it (``6g``, ``512m``). None ⇒ ``sbx``'s own default.
        cpus: How many processors the sandbox gets. None ⇒ ``sbx``'s default.
        publish: The ports the sandbox publishes to the host, each written
            ``[[HOST_ADDRESS:]HOST_PORT:]SANDBOX_PORT`` — for example
            ``127.0.0.1:8901:8901``. The health checks and the live gate keep
            running from the host against these published ports.
        allow_network: The hosts the sandbox is allowed to reach, each a host
            name or an address, optionally with ``:port``. A sandbox reaches
            nothing off its own network without a rule here — the Debian
            mirrors, the Python index, and (for a repository whose app talks to
            a model) the model door's address and port on this box.
    """

    name: str
    memory: str | None = None
    cpus: int | None = None
    publish: tuple[str, ...] = ()
    allow_network: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeployProfile:
    """A parsed, validated deploy profile — the DEPLOY stage's input.

    Mirrors the scope-design §4 field list. ``env_id`` and ``compose`` are the
    only strictly-required fields (a profile must name its environment and how
    services stand up); every other section is optional so a minimal profile
    (a single-service compose deploy) is valid, and a rich profile (realm
    import, secrets, seeds, models, broker contract, reservation) parameterizes
    the full stage. Secret refs are *names only* — validated on load.

    Attributes:
        env_id: The deploy environment id (⇐ the B7 payload ``env_id``).
        compose: The docker-compose invocation.
        hosts: Ordered host set (host + role).
        secret_injection: Register key *names* (never values) to inject.
        seed_fixture_contract: Seed-fixture scripts + golden-state refs.
        realm_import: Path/ref of a realm export to import (e.g. Keycloak), or None.
        models_required: llama-swap models to warm before the gate.
        health_checks: Post-deploy health checks.
        broker_contract_ref: Ref to the F6 broker-contract section (pre-flight).
        reservation: The environment-reservation lease request, or None.
        rollback_image_ref: The kept ``:rollback-*`` image tag re-deployed on a
            FAILED post-deploy live-gate (O-32). Names only the image ref (e.g.
            ``study-tutor:rollback-20260713``); the profile's deploy script
            consumes it (env/compose IMAGE var) to bring the prior build back up.
            When absent, a revert is a LOUD terminal failure — never a silent
            keep-serving of the failed build.
        cwd: Working directory for subprocess steps (repo-relative or absolute).
        live_gate: The per-target live-gate driver spec (the F16 real backend),
            or None. Absent ⇒ the live-gate seam stays ``Unconfigured`` (deny by
            default — the stage loud-fails rather than synthesize a verdict).
        candidate: The optional candidate-then-promote overlay, or None. Absent
            ⇒ byte-identical to the direct-live flow (deploy → gate → O-32
            revert). Present ⇒ the stage stands the build up under a ``-cand``
            project, gates it, and promotes only on a PASS (S2F).
        sandbox: The Docker Sandbox this repository deploys into, or None.
            Absent ⇒ the deploy runs against the host's own Docker engine
            exactly as it did before sandboxes existed.
        source_ref: Path the profile was loaded from (for deploy_profile_ref).
    """

    env_id: str
    compose: ComposeSpec
    hosts: tuple[DeployHost, ...] = ()
    secret_injection: tuple[str, ...] = ()
    seed_fixture_contract: tuple[SeedFixture, ...] = ()
    realm_import: str | None = None
    models_required: tuple[ModelRequirement, ...] = ()
    health_checks: tuple[HealthCheck, ...] = ()
    broker_contract_ref: str | None = None
    reservation: Reservation | None = None
    rollback_image_ref: str | None = None
    cwd: str | None = None
    live_gate: DeployLiveGate | None = None
    candidate: DeployCandidate | None = None
    sandbox: DeploySandbox | None = None
    source_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def host_names(self) -> list[str]:
        """The bare host names (for the B7 payload ``hosts`` list)."""
        return [h.host for h in self.hosts]

    @property
    def reservation_resource(self) -> str | None:
        """The reservation resource name (for the B7 payload), or None."""
        return self.reservation.resource if self.reservation else None

    @property
    def rollback_ref(self) -> str | None:
        """The rollback image ref to re-deploy on a gate fail (O-32), or None.

        Carries the explicit ``rollback_image_ref`` when the profile sets one.
        A property (not just the field) so a future derivation rule (e.g. a
        ``<image>:rollback-*`` convention keyed on ``env_id``) has one seam to
        land in without touching the runner.
        """
        return self.rollback_image_ref


# ---------------------------------------------------------------------------
# Secret-ref safety (WS2-B8 guardrail)
# ---------------------------------------------------------------------------


#: Characters/shapes that mark a ``secret_injection`` entry as value-bearing
#: rather than a bare register-key ref. A ref is a NAME
#: (e.g. ``NATS_PASSWORD``, ``moneyhub.client_secret``); an entry containing an
#: assignment (``=``), whitespace, or a mapping with a non-null value looks like
#: it smuggles a value and is refused.
def _assert_ref_only(entry: Any, index: int) -> str:
    """Return the register-key name for a ``secret_injection`` entry, or raise.

    Refuses anything that looks like it carries a value so a secret value can
    never enter a profile/payload/record (WS2-B8 guardrail; the FEAT-DD4F
    "no silent unsafe path" discipline applied to secrets).
    """
    if isinstance(entry, str):
        name = entry
    elif isinstance(entry, dict):
        # Accept {"ref": "NAME"} / {"name": "NAME"} / {"key": "NAME"} shapes,
        # but ONLY if they carry no value field.
        value_bearing = {k for k in entry if k not in {"ref", "name", "key"}}
        if value_bearing:
            raise DeployProfileError(
                f"secret_injection[{index}] carries value-bearing key(s) "
                f"{sorted(value_bearing)!r}; secrets are register REFS ONLY "
                "(names, never values). WS5 owns the values."
            )
        ref = entry.get("ref") or entry.get("name") or entry.get("key")
        if not isinstance(ref, str) or not ref.strip():
            raise DeployProfileError(
                f"secret_injection[{index}] has no register-key name "
                "(expected 'ref'/'name'/'key')"
            )
        name = ref
    else:
        raise DeployProfileError(
            f"secret_injection[{index}] must be a register-key name (str) or "
            f"a {{ref: NAME}} mapping, got {type(entry).__name__}"
        )

    stripped = name.strip()
    if not stripped:
        raise DeployProfileError(
            f"secret_injection[{index}] is an empty register-key name"
        )
    # A register key NAME is a conservative identifier (see _REF_NAME_RE).
    # Anything else — ``=`` (assignment), an ``@``/``:``/``//`` URL shape (a
    # DSN), whitespace — marks a smuggled VALUE and is refused: secrets are
    # register REFS ONLY (names, never values).
    if not _REF_NAME_RE.match(stripped):
        raise DeployProfileError(
            f"secret_injection[{index}]={name!r} is not a bare register-key name "
            "(it looks like a value — contains an assignment, URL, or whitespace). "
            "Secrets are register REFS ONLY: put the KEY NAME here; WS5 resolves "
            "the value at run time."
        )
    return stripped


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _require_mapping(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeployProfileError(f"{what} must be a mapping, got {type(raw).__name__}")
    return raw


def _parse_hosts(raw: Any) -> tuple[DeployHost, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError("hosts must be a list of {host, role} mappings")
    hosts: list[DeployHost] = []
    for i, item in enumerate(raw):
        m = _require_mapping(item, f"hosts[{i}]")
        host = m.get("host")
        if not isinstance(host, str) or not host.strip():
            raise DeployProfileError(f"hosts[{i}] requires a non-empty 'host'")
        role = m.get("role")
        if not isinstance(role, str) or not role.strip():
            raise DeployProfileError(f"hosts[{i}] requires a non-empty 'role'")
        hosts.append(DeployHost(host=host, role=role))
    return tuple(hosts)


def _parse_compose(raw: Any) -> ComposeSpec:
    m = _require_mapping(raw, "compose")
    file = m.get("file")
    if not isinstance(file, str) or not file.strip():
        raise DeployProfileError("compose.file is required (the compose file path)")
    profile = m.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise DeployProfileError("compose.profile must be a string when present")
    script = m.get("script")
    if script is not None and not isinstance(script, str):
        raise DeployProfileError("compose.script must be a string when present")
    env_file = m.get("env_file")
    if env_file is not None and not isinstance(env_file, str):
        raise DeployProfileError("compose.env_file must be a string when present")
    return ComposeSpec(file=file, profile=profile, script=script, env_file=env_file)


def _parse_seed_fixtures(raw: Any) -> tuple[SeedFixture, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError(
            "seed_fixture_contract must be a list of {script, golden_state_ref}"
        )
    out: list[SeedFixture] = []
    for i, item in enumerate(raw):
        m = _require_mapping(item, f"seed_fixture_contract[{i}]")
        script = m.get("script")
        if not isinstance(script, str) or not script.strip():
            raise DeployProfileError(
                f"seed_fixture_contract[{i}] requires a non-empty 'script'"
            )
        out.append(
            SeedFixture(script=script, golden_state_ref=m.get("golden_state_ref"))
        )
    return tuple(out)


def _parse_models(raw: Any) -> tuple[ModelRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError(
            "models_required must be a list of {model, warm_up_action}"
        )
    out: list[ModelRequirement] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append(ModelRequirement(model=item))
            continue
        m = _require_mapping(item, f"models_required[{i}]")
        model = m.get("model")
        if not isinstance(model, str) or not model.strip():
            raise DeployProfileError(
                f"models_required[{i}] requires a non-empty 'model'"
            )
        out.append(
            ModelRequirement(model=model, warm_up_action=m.get("warm_up_action"))
        )
    return tuple(out)


def _parse_health_checks(raw: Any) -> tuple[HealthCheck, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError("health_checks must be a list of {cmd, expected}")
    out: list[HealthCheck] = []
    for i, item in enumerate(raw):
        m = _require_mapping(item, f"health_checks[{i}]")
        cmd = m.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise DeployProfileError(f"health_checks[{i}] requires a non-empty 'cmd'")
        out.append(HealthCheck(cmd=cmd, expected=m.get("expected")))
    return tuple(out)


def _parse_reservation(raw: Any) -> Reservation | None:
    if raw is None:
        return None
    m = _require_mapping(raw, "reservation")
    resource = m.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise DeployProfileError(
            "reservation.resource is required when reservation is set"
        )
    return Reservation(resource=resource, quiet_window=m.get("quiet_window"))


def _parse_live_gate(raw: Any) -> DeployLiveGate | None:
    if raw is None:
        return None
    m = _require_mapping(raw, "live_gate")

    driver = m.get("driver")
    if not isinstance(driver, list) or not driver:
        raise DeployProfileError(
            "live_gate.driver is required and must be a non-empty argv list "
            "(e.g. ['python3', 'qa/gates/local_live_gate.py'])"
        )
    argv: list[str] = []
    for i, part in enumerate(driver):
        if not isinstance(part, str) or not part.strip():
            raise DeployProfileError(
                f"live_gate.driver[{i}] must be a non-empty string"
            )
        argv.append(part)

    gates_raw = m.get("gates")
    gates: tuple[str, ...] = ()
    if gates_raw is not None:
        if not isinstance(gates_raw, list):
            raise DeployProfileError(
                "live_gate.gates must be a list of gate-id strings when present"
            )
        gate_ids: list[str] = []
        for i, g in enumerate(gates_raw):
            if not isinstance(g, str) or not g.strip():
                raise DeployProfileError(
                    f"live_gate.gates[{i}] must be a non-empty gate-id string"
                )
            gate_ids.append(g)
        gates = tuple(gate_ids)

    timeout_seconds = m.get("timeout_seconds", 600)
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise DeployProfileError(
            "live_gate.timeout_seconds must be a positive integer when present"
        )

    env_raw = m.get("env")
    env: dict[str, str] = {}
    if env_raw is not None:
        env_map = _require_mapping(env_raw, "live_gate.env")
        for name, value in env_map.items():
            if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
                raise DeployProfileError(
                    f"live_gate.env key {name!r} must be UPPER_SNAKE_CASE "
                    "(a non-secret env-var NAME, e.g. API_TEST_BASE_URL)"
                )
            if not isinstance(value, str):
                raise DeployProfileError(
                    f"live_gate.env[{name!r}] must be a string value "
                    "(non-secret, e.g. a base URL; secrets stay register REFS)"
                )
            env[name] = value

    return DeployLiveGate(
        driver=tuple(argv),
        gates=gates,
        timeout_seconds=timeout_seconds,
        env=env,
    )


def _parse_candidate(raw: Any) -> DeployCandidate | None:
    if raw is None:
        return None
    m = _require_mapping(raw, "candidate")

    env_raw = m.get("env")
    env: dict[str, str] = {}
    if env_raw is not None:
        env_map = _require_mapping(env_raw, "candidate.env")
        for name, value in env_map.items():
            if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
                raise DeployProfileError(
                    f"candidate.env key {name!r} must be UPPER_SNAKE_CASE "
                    "(a non-secret env-var NAME, e.g. CANDIDATE_PORT)"
                )
            if not isinstance(value, str):
                raise DeployProfileError(
                    f"candidate.env[{name!r}] must be a string value "
                    "(non-secret, e.g. a port/base URL; secrets stay register REFS)"
                )
            env[name] = value

    keep = m.get("keep", False)
    if not isinstance(keep, bool):
        raise DeployProfileError(
            "candidate.keep must be a boolean when present "
            "(True keeps the candidate project up after promote; default False)"
        )

    return DeployCandidate(env=env, keep=keep)


def _parse_port(value: str, *, what: str) -> str:
    """Return ``value`` when it is a port number, or raise with one sentence."""
    if not value.isdigit() or not (1 <= int(value) <= 65535):
        raise DeployProfileError(
            f"{what} is {value!r}, which is not a port number — a port is a "
            "whole number from 1 to 65535"
        )
    return value


def _parse_sandbox_publish(raw: Any) -> tuple[str, ...]:
    """Parse ``sandbox.publish`` — the ports the sandbox opens to the host."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError(
            "sandbox.publish must be a list of port rules, each written "
            "'[[HOST_ADDRESS:]HOST_PORT:]SANDBOX_PORT' (for example "
            "'127.0.0.1:8901:8901')"
        )
    rules: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise DeployProfileError(
                f"sandbox.publish[{i}] must be a non-empty port rule written "
                "'[[HOST_ADDRESS:]HOST_PORT:]SANDBOX_PORT'"
            )
        rule = item.strip()
        parts = rule.split(":")
        if len(parts) == 1:
            _parse_port(parts[0], what=f"sandbox.publish[{i}]")
        elif len(parts) == 2:
            _parse_port(parts[0], what=f"sandbox.publish[{i}] host port")
            _parse_port(parts[1], what=f"sandbox.publish[{i}] sandbox port")
        elif len(parts) == 3:
            if not _SANDBOX_HOST_RE.match(parts[0]):
                raise DeployProfileError(
                    f"sandbox.publish[{i}] starts with {parts[0]!r}, which is "
                    "not a host address — write the rule as "
                    "'HOST_ADDRESS:HOST_PORT:SANDBOX_PORT', for example "
                    "'127.0.0.1:8901:8901'"
                )
            _parse_port(parts[1], what=f"sandbox.publish[{i}] host port")
            _parse_port(parts[2], what=f"sandbox.publish[{i}] sandbox port")
        else:
            raise DeployProfileError(
                f"sandbox.publish[{i}]={rule!r} has too many parts — a port "
                "rule is written '[[HOST_ADDRESS:]HOST_PORT:]SANDBOX_PORT'"
            )
        if "," in rule:
            raise DeployProfileError(
                f"sandbox.publish[{i}]={rule!r} contains a comma — the rules "
                "are joined with commas when they are handed to the deploy "
                "script, so a comma inside one would split it in two"
            )
        rules.append(rule)
    return tuple(rules)


def _parse_sandbox_allow_network(raw: Any) -> tuple[str, ...]:
    """Parse ``sandbox.allow_network`` — the hosts the sandbox may reach."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeployProfileError(
            "sandbox.allow_network must be a list of hosts, each a host name "
            "or an address, optionally with ':port' (for example 'pypi.org' "
            "or '172.30.1.253:4000')"
        )
    rules: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise DeployProfileError(
                f"sandbox.allow_network[{i}] must be a non-empty host name or "
                "address, optionally with ':port'"
            )
        rule = item.strip()
        if "," in rule:
            raise DeployProfileError(
                f"sandbox.allow_network[{i}]={rule!r} contains a comma — the "
                "rules are joined with commas when they are handed to the "
                "deploy script, so a comma inside one would split it in two"
            )
        if "/" in rule:
            raise DeployProfileError(
                f"sandbox.allow_network[{i}]={rule!r} looks like a web address "
                "— write just the host (and ':port' if you need one), with no "
                "'http://' and no path, for example 'pypi.org'"
            )
        host, sep, port = rule.partition(":")
        if sep:
            _parse_port(port, what=f"sandbox.allow_network[{i}] port")
        if not _SANDBOX_HOST_RE.match(host):
            raise DeployProfileError(
                f"sandbox.allow_network[{i}]={rule!r} is not a host — write a "
                "host name or an address (a leading '*.' is allowed), with no "
                "scheme and no path, for example 'pypi.org', '*.debian.org' "
                "or '172.30.1.253:4000'"
            )
        rules.append(rule)
    return tuple(rules)


def _parse_sandbox(raw: Any) -> DeploySandbox | None:
    """Parse the optional ``sandbox`` block (the 2026-09-06 decision).

    Absent ⇒ ``None`` ⇒ the deploy behaves exactly as it did before Docker
    Sandboxes existed. Present ⇒ every setting is checked here, so a bad name,
    port or rule is refused on load with one plain sentence rather than
    surfacing as a puzzling failure inside a deploy.
    """
    if raw is None:
        return None
    m = _require_mapping(raw, "sandbox")

    name = m.get("name")
    if not isinstance(name, str) or not _SANDBOX_NAME_RE.match(name):
        raise DeployProfileError(
            f"sandbox.name must be the sandbox's name — 2 to 63 characters of "
            "lower-case letters, digits and hyphens, starting with a letter or "
            f"a digit (for example 'api-test-deploy'); got {name!r}"
        )

    memory = m.get("memory")
    if memory is not None:
        if not isinstance(memory, str) or not memory.strip():
            raise DeployProfileError(
                "sandbox.memory must be written the way sbx takes it — a size "
                "string such as '6g' or '512m'"
            )
        memory = memory.strip()

    cpus = m.get("cpus")
    if cpus is not None and (
        not isinstance(cpus, int) or isinstance(cpus, bool) or cpus <= 0
    ):
        raise DeployProfileError(
            "sandbox.cpus must be a whole number of processors greater than "
            "zero when present"
        )

    return DeploySandbox(
        name=name,
        memory=memory,
        cpus=cpus,
        publish=_parse_sandbox_publish(m.get("publish")),
        allow_network=_parse_sandbox_allow_network(m.get("allow_network")),
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def parse_deploy_profile(
    raw: dict[str, Any], *, source_ref: str | None = None
) -> DeployProfile:
    """Parse an already-loaded mapping into a validated :class:`DeployProfile`.

    Separated from :func:`load_deploy_profile` so callers holding a dict (tests,
    an intake payload) validate through the same path as a file load.
    """
    data = _require_mapping(raw, "deploy profile")

    env_id = data.get("env_id")
    if not isinstance(env_id, str) or not env_id.strip():
        raise DeployProfileError("env_id is required (the deploy environment id)")

    if "compose" not in data:
        raise DeployProfileError("compose is required (compose.file at minimum)")
    compose = _parse_compose(data["compose"])

    secret_injection = tuple(
        _assert_ref_only(entry, i)
        for i, entry in enumerate(data.get("secret_injection") or [])
    )

    rollback_image_ref = data.get("rollback_image_ref")
    if rollback_image_ref is not None and (
        not isinstance(rollback_image_ref, str) or not rollback_image_ref.strip()
    ):
        raise DeployProfileError(
            "rollback_image_ref must be a non-empty string when present "
            "(the kept :rollback-* image tag re-deployed on a gate fail)"
        )

    known_keys = {
        "env_id",
        "compose",
        "hosts",
        "secret_injection",
        "seed_fixture_contract",
        "realm_import",
        "models_required",
        "health_checks",
        "broker_contract_ref",
        "reservation",
        "rollback_image_ref",
        "cwd",
        "live_gate",
        "candidate",
        "sandbox",
    }
    extra = {k: v for k, v in data.items() if k not in known_keys}

    return DeployProfile(
        env_id=env_id,
        compose=compose,
        hosts=_parse_hosts(data.get("hosts")),
        secret_injection=secret_injection,
        seed_fixture_contract=_parse_seed_fixtures(data.get("seed_fixture_contract")),
        realm_import=data.get("realm_import"),
        models_required=_parse_models(data.get("models_required")),
        health_checks=_parse_health_checks(data.get("health_checks")),
        broker_contract_ref=data.get("broker_contract_ref"),
        reservation=_parse_reservation(data.get("reservation")),
        rollback_image_ref=rollback_image_ref.strip()
        if isinstance(rollback_image_ref, str)
        else None,
        cwd=data.get("cwd"),
        live_gate=_parse_live_gate(data.get("live_gate")),
        candidate=_parse_candidate(data.get("candidate")),
        sandbox=_parse_sandbox(data.get("sandbox")),
        source_ref=source_ref,
        extra=extra,
    )


def load_deploy_profile(path: str | Path) -> DeployProfile:
    """Load and validate a ``deploy/profile.yaml`` from disk.

    Args:
        path: Path to the deploy profile YAML file.

    Returns:
        A validated :class:`DeployProfile`.

    Raises:
        DeployProfileError: If the file is missing, is not valid YAML, is not a
            mapping, is missing a required field, or carries a value-bearing
            secret entry (register refs only).
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DeployProfileError(f"deploy profile not found: {p}") from exc
    except OSError as exc:
        raise DeployProfileError(f"could not read deploy profile {p}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeployProfileError(
            f"deploy profile {p} is not valid YAML: {exc}"
        ) from exc

    if raw is None:
        raise DeployProfileError(f"deploy profile {p} is empty")

    return parse_deploy_profile(raw, source_ref=str(p))
