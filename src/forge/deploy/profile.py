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

__all__ = [
    "DeployProfile",
    "DeployHost",
    "ComposeSpec",
    "SeedFixture",
    "ModelRequirement",
    "HealthCheck",
    "Reservation",
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
