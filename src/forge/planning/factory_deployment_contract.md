# Factory deployment interface reference

Document ID: factory-deployment-contract
Version: 1

This is factory-owned interface evidence. It guides deployment work only when
that work is already required by the request and approved specification. It does
not request a new feature, override a prohibition, or enlarge an unrelated
feature. Project documents precede this reference; the original request remains
the authority for scope. The factory records this document's exact UTF-8 bytes,
version, byte count and SHA-256 separately from project-owned documents.

## Discovery and three levels of validity

The coordinator discovers **deploy/profile.yaml**, at the relevant committed
revision. A differently named file is not discovered. A first profile must be
an ordinary file in an ordinary deploy directory, equal in the approved candidate
and joined commit. A profile already present at the recorded start remains the
authority; a feature cannot replace it by declaring another first deployment.

1. Loader requirements: a YAML mapping with non-empty `env_id` and a `compose`
   mapping containing non-empty `file`. This establishes parseability only.
2. Checked first deployment also requires a `candidate` mapping (an empty
   mapping is valid), `live_gate.driver` as a non-empty argv list, and an
   explicitly present `identity` block with non-empty effective `asked_with`.
   The other identity names have defaults listed below. The loader retains
   `identity` in `extra` without validating it. Loader success is therefore not
   proof of deployability. Passing this declaration check also does not replace
   exact candidate approval, publication, artifact checks or running identity.
3. Other fields are optional in the loader, with their actual conditions below.
   A working deployment still needs an executable repository entry point and
   honest service checks. Absent `compose.script` cannot perform a real scripted
   deployment; absent `live_gate` leaves the real live-gate seam unconfigured.
   A project must implement the modes and effective markers it declares.

A generic profile illustrating the checked path (interface declarations, not a
service implementation or a task plan):

```yaml
env_id: sample-service
compose:
  file: deploy/compose.yaml
  script: deploy/deploy.sh
candidate:
  env: {}
  keep: false
live_gate:
  driver: ["./qa/live-gate"]
  timeout_seconds: 600
health_checks:
  - cmd: qa/check-health
identity:
  asked_with: RUNNING_IDENTITY
```

These paths name project-authored executables. They must exist, be executable
and implement their contracts; the example alone is not a runnable product.
No language, application framework, host path, port assignment or packaging
technology is prescribed by this reference.

## Complete profile surface

Top-level fields recognized by the installed loader are:

| Field | Shape, default and condition |
| --- | --- |
| `env_id` | Required non-empty string, the deployment environment identifier. |
| `compose` | Required mapping: `file` required non-empty string; optional string `profile`, `script`, `env_file`. No script default. `env_file` is passed as `ENV_FILE`; the script reads it, the runner does not source it. |
| `hosts` | Optional list of mappings with non-empty `host` and `role`; default empty. |
| `secret_injection` | Optional list of register key references, never values; default empty. A reference can be a name string or a mapping with `ref`, `name` or `key`. Names contain letters, digits, dot, underscore or hyphen. No assignments, whitespace or secret-bearing value keys. The operator provides values at execution time. |
| `seed_fixture_contract` | Optional list of mappings with non-empty executable `script`, optional `golden_state_ref`; default empty. |
| `realm_import` | Optional import reference. Without an importer script the stage records the reference only. |
| `models_required` | Optional list of model-name strings or mappings with non-empty `model` and optional executable `warm_up_action`; default empty. |
| `health_checks` | Optional list of mappings with non-empty executable-path `cmd` and optional `expected`; default empty. The current handler uses the script's exit code, not text comparison with `expected`. The script itself must test health and fail honestly. |
| `broker_contract_ref` | Optional reference to the broker contract; no default value. |
| `reservation` | Optional mapping requiring non-empty `resource`, with optional `quiet_window`; absent means no declared reservation. |
| `rollback_image_ref` | Optional non-empty string naming the retained rollback artifact. A failing live gate without this reference fails explicitly because the generic revert cannot restore anything. |
| `cwd` | Optional working directory. The coordinator's supplied candidate/joined checkout overrides it where those scripts are run. Do not assume a host checkout path. |
| `live_gate` | Optional mapping, but required for checked first deployment: `driver` non-empty list of non-empty argv strings; `gates` optional list of gate IDs (empty means all registered gates); `timeout_seconds` positive integer, default 600; `env` optional map of non-secret UPPER_SNAKE_CASE names to strings, default empty. |
| `candidate` | Optional mapping, but required for checked first deployment: `env` non-secret UPPER_SNAKE_CASE string map, default empty; `keep` boolean, default false. Its addressing overlay must distinguish the candidate from live. |
| `sandbox` | Optional operator-controlled mapping detailed below. Omission means no sandbox declaration in the profile. |
| `preview` | Optional mapping: `enabled` boolean default false; `slots` list default empty, required non-empty when enabled; each slot needs integer `port` and `internal_port` in the valid TCP range, no duplicate port across slots, optional plain HTTPS `public_url` with no credentials/query/fragment. Positive integer `app_ttl_hours`, `evidence_days`, `slot_wait_minutes` default to 24, 14, 30. Port allocation belongs to the operator. |
| `identity` | Retained as extra data by the loader, consumed by the coordinator as described next. Other unknown top-level keys are also retained as `extra`; this does not grant them execution semantics. |

When `sandbox` is present, `name` is required (2–63 lowercase letters, digits or
hyphens, starting with a letter or digit). Optional `memory` is a non-empty size
string and `cpus` a positive integer. `publish` is a list of port-rule strings
`[[HOST_ADDRESS:]HOST_PORT:]SANDBOX_PORT`; `allow_network` a list of host names
or addresses, optionally `:port`, allowing a leading wildcard but no URL scheme
or path. Both lists default empty. Optional `sidecar_publish` and
`runner_publish` must be provided together as full host-address publish
rules. Optional `env_file`, `forge_path`, `guardkit_path`, `receipts_path` are
non-empty path strings without commas used by the operator's sandbox setup. Product authors must
not invent these values. The operator owns sandbox creation, exposure routes,
reserved ports and access; project authors own deployment files and checks.

## Identity names and evidence

Only the identity block itself and non-empty `asked_with` are required by the
first-deployment declaration check. Defaulted names are usable without spelling
them out, but scripts must emit the names actually in effect:

| Identity key | Default | Meaning |
| --- | --- | --- |
| `setting` | `DEPLOY_IDENTITY` | Environment setting carrying the factory's immutable checked identity. |
| `reported_as` (alias `marker`) | `DEPLOYED_IDENTITY` | Deploy output marker reporting what is actually running. |
| `checked_as` (alias `checked_marker`) | `CHECKED_ARTIFACT` | Candidate deploy output marker identifying the immutable artifact actually checked. |
| `artifact_setting` (alias `artifact`) | `DEPLOY_ARTIFACT` | Environment setting carrying that recorded artifact to promotion. |
| `asked_with` (aliases `ask_with`, `running_setting`) | No default | Environment setting set to `1` to ask the deploy entry point, read-only, what is running. |
| `running_as` (alias `running_marker`) | `RUNNING_IDENTITY` | Output marker answering the read-only identity query. |

Output markers use one line `NAME=value`. The parser takes the last matching
line, trims whitespace and one surrounding quote pair. Scripts must report
observed identities, never merely echo the desired input as supposed evidence.
The factory identity binds the joined commit and a content fingerprint; the
checked artifact is the project's own immutable, non-reusable identifier for
the actual built thing. Retain both and their association.

For the default query marker, `RUNNING_IDENTITY=none` explicitly means no service
is running. An empty value, missing line or failed query means unknown, never
none. `RUNNING_IDENTITY_UNKNOWN=reason` explicitly reports uncertainty and takes
precedence over a normal answer. A declared custom marker uses the same
`_UNKNOWN` suffix. A query must change nothing and return the target's actual
state. The coordinator does not deploy over an unestablished target.

## Calls, directories and lifecycle

`compose.script` is an executable path resolved in the supplied working
directory, not a shell command string with inline arguments. The runner calls
it without mode arguments; environment settings select the mode. A bare name
is resolved relative to that directory, not searched on PATH. The same rule
applies to health-check `cmd` and seed/warm-up script paths. A script exits zero
only for success; non-zero signals failure. Environment files are exposed via
`ENV_FILE`. The execution environment is filtered; project launch-setting
names come from its committed declarations, not arbitrary inherited secrets.

For sandbox deployments using a `deploy/sandbox-deploy.sh` wrapper, the stage
runs its corresponding `deploy/deploy.sh` inside an existing sandbox. Sandbox
provisioning remains the operator's responsibility. Outside an existing sandbox,
profile sandbox settings reach the wrapper as `SANDBOX_NAME`, `SANDBOX_MEMORY`,
`SANDBOX_CPUS`, `SANDBOX_PUBLISH`, `SANDBOX_ALLOW_NETWORK` (lists joined with
commas), and, when set, `SANDBOX_SIDECAR_PUBLISH`, `SANDBOX_RUNNER_PUBLISH`,
`SANDBOX_ENV_FILE`, `SANDBOX_FORGE_PATH`, `SANDBOX_GUARDKIT_PATH`,
`SANDBOX_RECEIPTS_PATH`. Inside the existing sandbox these provisioning settings
are omitted. Candidate scripts and their
live-gate driver run in the candidate/joined tree supplied by the coordinator;
promotion, query and teardown use the supplied scripts checkout, falling back
to profile `cwd` where no override is supplied. Relative paths must work there.

- Candidate: the entry point receives `CANDIDATE=1`, `candidate.env`, and the
  declared identity setting. Build and start an isolated candidate without
  modifying live. Emit `CHECKED_ARTIFACT=<immutable-artifact>` (or the effective
  custom marker) in the candidate deploy output. Candidate health scripts and
  the live-gate driver receive the candidate addressing overlay too. A failure
  leaves live untouched and triggers identity-scoped candidate cleanup.
- Live gate: execute the declared driver argv followed by `--feature <feature>`
  and `--target <env_id>`, plus `--gates <comma-separated-ids>` when specified.
  No `--repo` is appended: the supplied working directory selects the tree.
  The driver must run real registered gates, with their prerequisite provider,
  against the addressed service. It can emit a JSON results envelope on stdout
  with `verdict` (`pass`, `fail`, `instrument_fail`, `environment_fail`),
  `run_id`, `gates`, assertions and evidence references. The installed parser
  accepts a valid envelope's verdict; otherwise exit codes map 0 to pass, 1 to
  fail, 3 to instrument_fail, 4 to environment_fail, other codes to fail.
  A fabricated success or an empty check is not service evidence.
- Promote: receive `PROMOTE=1`, the declared identity setting and artifact
  setting. Deploy exactly the immutable artifact that passed checks, without
  rebuilding or resolving a mutable shared tag. Preserve the actual previous
  deployment for rollback before changing live. Emit
  `DEPLOYED_IDENTITY=<observed-running-identity>` using the effective marker.
  The coordinator compares it with the checked identity; missing or different
  evidence fails. Health and live gates check the promoted service as well.
- Query: the same entry point receives the declared `asked_with` setting as `1`.
  Inspect only; print the effective `running_as` marker or its `_UNKNOWN` form.
- Candidate cleanup: the entry point receives `CANDIDATE_DOWN=1`, candidate
  overlay and the identity of the specific candidate. Remove only that
  candidate's resources. Missing identity refuses cleanup; never enumerate and
  remove other candidates. `candidate.keep=true` retains a successful candidate
  after promotion, not permission to leave failed live work running.
- Revert: the entry point receives `REVERT=1` and
  `ROLLBACK_IMAGE_REF=<retained-reference>`. Restore the actual previous
  deployment, not a newly rebuilt approximation, and verify its health before
  returning success. The generic revert runbook is one deploy step; it does
  not independently rerun the profile health checks or live gate, so the revert
  entry point must do that verification itself. Missing rollback reference or
  failed revert is an explicit failed deployment, never a claimed recovery.

A failed first deployment has no previous live deployment to restore. Before
promotion, candidate failure must leave live absent and clean up that candidate.
If first promotion partially starts a failing live instance, the project's
entry point must remove or stop that failed instance safely and establish its
actual resulting state, reporting failure honestly. It must not invent a
previous image, claim a successful rollback, or confuse candidate cleanup with
restoring live. These are distinct cases for the project's deployment checks.

This reference supplies the installed interface. It supplies no product code,
no task list, no private infrastructure values and no additional approval.
