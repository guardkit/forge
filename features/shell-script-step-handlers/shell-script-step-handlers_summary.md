# Feature Spec Summary: Shell Script Step Handlers

**Stack**: python
**Generated**: 2026-06-22T06:49:07Z
**Scenarios**: 20 total (4 smoke, 0 regression)
**Assumptions**: 14 total (4 high / 7 medium / 3 low confidence)
**Review required**: Yes

## Scope

The two concrete runbook step handlers — `deploy_compose(cwd, script, env_file)`
and `run_smoke_tests(cwd, script, env_file)` — that wrap existing shell scripts as
subprocesses and register into the executor's step-type registry. Each handler runs
a named script in a working directory with an environment file available by path,
captures stdout and stderr, and maps the script's exit status to a verdict (zero →
passed, non-zero → failed; for `run_smoke_tests` the exit status is the verdict
itself). The handler adds no idempotency of its own and is credential-scoped:
`env_file` is a path only and is never read into the result, and captured output is
scrubbed of postgres DSN and password patterns before being stored or published.

The executor's dispatch loop, status persistence, resume pointer, and lifecycle
events are owned upstream by the **Runbook Executor** feature and are not
re-specified here — these scenarios cover only handler behaviour.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 2 |
| Negative cases (@negative) | 6 |
| Edge cases (@edge-case) | 10 |
| Smoke (@smoke) | 4 |
| Integration (@integration @slow) | 1 |
| Regression (@regression) | 0 |

(Categories overlap: several edge cases are also tagged `@negative`. The 20 distinct
scenarios break down as 5 Key Examples, 2 Boundary, 3 Negative, 4 Edge, 5 Phase-4
expansion (credential-scope & failure-recovery), and 1 Integration.)

## Key implementation note (carry into /feature-plan)

The required output scrubber **does not exist yet**.
[`src/forge/memory/redaction.py`](../../src/forge/memory/redaction.py)'s
`redact_credentials` covers GitHub tokens / bearer / hex only and is scoped to
Graphiti entity payloads — it has **no** postgres-DSN or password coverage. Per
ASSUM-002/003/004, this feature introduces a new sibling scrubber (e.g.
`scrub_process_output`) with `postgresql://`/`postgres://` DSN and `password=`/
`PGPASSWORD=` patterns, replacing matches with `***REDACTED-DSN***` /
`***REDACTED-PASSWORD***`, applied once at the capture boundary (ASSUM-006).

## Integration target

The `@integration @slow` scenario is grounded in a real script confirmed present:
`~/Projects/appmilla_github/fleet-memory/deploy/nas/smoke.sh`. It must run only
against a disposable target, never production (ASSUM-010).

## Deferred Items

None. All five proposed groups plus the edge-case expansion were accepted.

## Open Assumptions (low confidence — human verification required)

- **ASSUM-008** — No subprocess timeout this phase; scripts run to completion.
  *A long-running or hung deploy/smoke script would block the step indefinitely.*
- **ASSUM-009** — No captured-output size cap; full output captured then scrubbed.
  *A script with very large output would store/publish the whole of it.*
- **ASSUM-013** — The handler does not pre-validate the env-file path; a missing
  env file surfaces as the script's own non-zero exit.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Shell Script Step Handlers" \
      --context features/shell-script-step-handlers/shell-script-step-handlers_summary.md
