# WS2-B8 → B10 reconcile note — `deploy/profile.yaml` format

**Date:** 2026-07-09 · **Session:** WS2-B8 (deploy stage machinery, forge lane) ·
**For:** WS2-B10 (Tier-2/3 format schemas — authors the canonical deploy-profile
schema per scope-design §4 / §2 F-deploy-profile).

## Why this note exists

B10 has **not** run. Per the B4/B5 hedge pattern (build the consumer against
scope-design §4's field list now, reconcile with B10 later), B8 built the
deploy-profile **loader** (`src/forge/deploy/profile.py`) against scope-design
§4's field list verbatim. This note records the two deliberate deviations /
realizations so B10 canonizes them (or overrides them with a dated supersession)
rather than silently diverging.

## Field list consumed (scope-design §4, verbatim)

```
{env_id, hosts: [{host, role}], compose: {file, profile},
 secret_injection: [register-key refs — names only, WS5 owns values],
 seed_fixture_contract: [{script, golden_state_ref}], realm_import,
 models_required: [llama-swap model + warm-up action],
 health_checks: [{cmd, expected}], broker_contract_ref (F6 broker section),
 reservation: {resource, quiet_window}}
```

All of the above parse to `forge.deploy.profile.DeployProfile`. `env_id` and
`compose.file` are the only strictly-required fields; every other section is
optional (a minimal profile is a single-service compose deploy).

## Deviations / realizations for B10 to reconcile

1. **`compose` gained optional `script` + `env_file` (the deploy-wrapper
   bridge).** scope-§4 lists `compose: {file, profile}`. But the shipped FMDR
   `deploy_compose` step type (the one B8 reuses — "extend the executor, never a
   second one") wraps a **vetted deploy script** (e.g. `deploy.sh`), not inline
   `docker compose` — this is the D12 safety property (typed steps, never
   freehand shell). So `ComposeSpec` carries an optional `script` (the deploy
   wrapper) and `env_file`. The fleet-memory exemplar sets `compose.script:
   deploy.sh`. **B10 decision needed:** keep `compose.{file,profile,script,
   env_file}`, or model the compose invocation differently (e.g. a first-class
   `deploy_script` field, or a runner that synthesizes `docker compose -f
   <file> --profile <p> up -d` from `file`/`profile` directly). B8 does not
   invent a live `docker compose` invocation — when `compose.script` is absent
   the builder renders a dry-run/deferred `deploy_compose`.

2. **`secret_injection` refs are validated as a conservative key-name charset
   `^[A-Za-z0-9._-]+$`.** The scope guardrail is "names only, WS5 owns values."
   B8 enforces this by refusing any entry that is not a bare register-key name —
   an `=` (assignment), a `:`/`@`/`//` (URL/DSN shape), or whitespace all mark a
   smuggled value and raise `DeployProfileError`. Mapping form `{ref: NAME}` /
   `{name: NAME}` / `{key: NAME}` is accepted but rejected if it carries any
   other (value-bearing) key. **B10 decision needed:** confirm the charset (does
   any real register key use `/` for namespacing? B8 excluded it to catch DSNs).
   The F16 register schema (WS5) is the source of truth for key-name shape;
   B8/B10 only need the *shape a profile may reference*.

## Non-deviations (consumed as-is, no reconcile needed)

- `hosts`, `seed_fixture_contract`, `realm_import`, `models_required`,
  `health_checks`, `broker_contract_ref`, `reservation` all parse to the §4
  shapes verbatim. `models_required` also accepts a bare string (model name with
  no warm-up action) as a convenience — a superset of the §4 `{model,
  warm_up_action}` shape, still valid.
- `cwd` (working directory for the wrapped scripts) is an addition B8 needs to
  run the vetted scripts; it is not a §4 field but is operationally required and
  harmless. B10 may fold it into `compose` or keep it top-level.

## Where the exemplar + loader live

- Exemplar: `forge/deploy/profile.yaml` (the fleet-memory NAS profile — the
  FEAT-FMDR subject, the B8 dry-run gate fixture).
- Loader: `forge/src/forge/deploy/profile.py` (`load_deploy_profile` /
  `parse_deploy_profile`).
- Tests: `forge/tests/forge/deploy/test_deploy_profile.py`.

When B10 lands the canonical schema, either (a) confirm this shape and point the
loader at B10's pydantic model, or (b) file a dated supersession here and adjust
`profile.py` + the exemplar to match.
