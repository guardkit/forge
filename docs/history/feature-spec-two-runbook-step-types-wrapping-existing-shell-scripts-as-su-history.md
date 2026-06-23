# /feature-spec — two-runbook-step-types-wrapping-existing-shell-scripts-as-su — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-spec two-runbook-step-types-wrapping-existing-shell-scripts-as-su — 2026-06-21T20:49:32Z

### Prompt

```
/feature-spec "Two runbook step types wrapping existing shell scripts as subprocesses: deploy_compose(cwd, script, env_file) and run_smoke_tests(cwd, script, env_file) — each runs the named script in cwd with the env_file available, captures exit_code + stdout/stderr, maps exit 0 to passed and non-zero to failed (run_smoke_tests' exit code is the step verdict); preserves the script's own idempotency; credential-scoped — env_file is a path only, and captured output is scrubbed of postgres DSN and password patterns before being stored in the step result or published; both registered into the executor's step_type registry; unit tests with a fake script exercising the exit-0 and exit-1 paths plus an output-scrub assertion on a planted secret; one marker-gated integration test invoking fleet-memory deploy/nas/smoke.sh against a throwaway target"
```

### Assistant response

I'll execute the `/feature-spec` Propose-Review methodology. Starting with Phase 1: Context Gathering (silent).

Let me look at the existing runbook-executor feature and find the step_type registry implementation.

Let me look at the redaction helper and check the fleet-memory smoke.sh referenced for the integration test.

Context loaded. I have everything I need — the upstream `Step`/`Runbook` model, the executor's registry-dispatch contract, and the existing redaction helper. Proceeding to the proposal.

```
Context loaded: stack=python; Step/Runbook models found (status enum
pending/running/passed/failed/awaiting_approval, result = exit_code +
captured_output + timestamps); 14 existing .feature files — runbook-executor
and runbook-and-step-persistence are the direct upstream; 1 redaction helper
(redact_credentials) covering GitHub/bearer/hex ONLY — no postgres-DSN or
password coverage exists yet, so this feature introduces a new scrub pattern set.
```

A note before the proposal: the executor (runbook-executor.feature) already owns the dispatch loop, status persistence, and lifecycle events. This feature is **only the two concrete handlers** registered into that registry — so scenarios are scoped to handler behaviour (run script → map exit → scrub → result), not the executor's loop, which is specified upstream.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Shell Script Step Handlers (deploy_compose, run_smoke_tests)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

```gherkin
@shell-script-step-handlers
Feature: Shell Script Step Handlers
  As the Forge output-side loop
  I want deploy and smoke-test runbook steps that wrap existing shell scripts as
  subprocesses, mapping each script's exit status to a step verdict and scrubbing
  credentials out of captured output
  So that real deploy/verify scripts run as registry-dispatched steps without the
  executor knowing their internals and without leaking secrets into results or events

  Background:
    Given the deploy and smoke-test handlers are registered in the executor's step-type registry
```

```
━━ GROUP A: Key Examples (5 scenarios) ━━
```

```gherkin
  # Why: Core deploy path — a deploy script that exits cleanly is a passed step
  @key-example @smoke
  Scenario: A deploy step whose script exits cleanly is recorded as passed
    Given a deploy step naming a script that exits with status zero
    When the executor runs the step
    Then the script should run once in the step's working directory
    And the step should be recorded as passed
    And the script's exit status and captured output should be recorded in the step result

  # Why: Core smoke path — the smoke script's exit status IS the step's verdict
  @key-example @smoke
  Scenario: A smoke-test step whose script exits cleanly is recorded as passed
    Given a smoke-test step naming a script that exits with status zero
    When the executor runs the step
    Then the step should be recorded as passed
    And the smoke script's exit status should be the step's verdict

  # Why: The smoke script's non-zero exit is the failing verdict — this is the gate
  @key-example @smoke
  Scenario: A smoke-test step whose script exits non-zero is recorded as failed
    Given a smoke-test step naming a script that exits with a non-zero status
    When the executor runs the step
    Then the step should be recorded as failed
    And the failing exit status should be the step's verdict

  # Why: Both handlers must be reachable by the executor through the registry
  @key-example
  Scenario: The deploy and smoke handlers are registered under their step types
    Given the executor's step-type registry
    When the registry is consulted for the deploy and smoke-test step types
    Then a handler should be registered for the deploy step type
    And a handler should be registered for the smoke-test step type

  # Why: The script runs in the named working directory with the env file available
  @key-example
  Scenario: The script runs in its working directory with the environment file available
    Given a deploy step naming a script, a working directory, and an environment file path
    When the executor runs the step
    Then the script should run with that working directory as its current directory
    And the environment file should be available to the script by its path
```

```
━━ GROUP B: Boundary Conditions (2 scenarios) ━━
```

```gherkin
  # Why: Just-inside / just-outside the pass boundary — only exit status zero passes
  # [ASSUMPTION: confidence=high] Exit status zero maps to passed; every non-zero status maps to failed.
  @boundary
  Scenario Outline: A step's verdict follows the script's exit status
    Given a smoke-test step naming a script that exits with status <exit_status>
    When the executor runs the step
    Then the step should be recorded as <verdict>
    Examples:
      | exit_status | verdict |
      | 0           | passed  |
      | 1           | failed  |
      | 137         | failed  |

  # Why: Zero-length boundary — a script that prints nothing still yields a recorded result
  @boundary
  Scenario: A script that produces no output still records a result
    Given a deploy step naming a script that exits with status zero and prints nothing
    When the executor runs the step
    Then the step should be recorded as passed
    And the step result should record empty captured output
```

```
━━ GROUP C: Negative Cases (3 scenarios) ━━
```

```gherkin
  # Why: Negative deploy path — a deploy script that exits non-zero fails the step
  @negative
  Scenario: A deploy step whose script exits non-zero is recorded as failed
    Given a deploy step naming a script that exits with a non-zero status
    When the executor runs the step
    Then the step should be recorded as failed
    And the script's exit status and captured output should be recorded in the step result

  # Why: A script that cannot be run fails the step rather than crashing the executor
  # [ASSUMPTION: confidence=medium] A missing or non-executable script is contained as a step failure, not an executor crash.
  @negative
  Scenario: A step naming a script that cannot be run is recorded as failed
    Given a deploy step naming a script that does not exist or cannot be executed
    When the executor runs the step
    Then the step should be recorded as failed
    And the executor should not crash

  # Why: Credential scope — the env file is passed as a path; its contents are never captured
  # [ASSUMPTION: confidence=high] The handler passes env_file as a path only; it never reads the file contents into the step result or any event.
  @negative @edge-case
  Scenario: The environment file contents are never captured into the step result
    Given a smoke-test step whose environment file holds credentials
    When the executor runs the step
    Then the environment file's contents should not appear in the step result
    And the environment file should be referenced only by its path
```

```
━━ GROUP D: Edge Cases (4 scenarios) ━━
```

```gherkin
  # Why: The handler adds no idempotency of its own — the script's own idempotency governs re-runs
  # [ASSUMPTION: confidence=high] Re-running a step re-invokes the script unchanged; idempotency is the script's responsibility, not the handler's.
  @edge-case
  Scenario: Re-running a deploy step re-invokes the script and preserves its own idempotency
    Given a deploy step whose script is idempotent
    When the executor runs the step twice
    Then the script should run on each execution
    And the handler should add no idempotency guard of its own
    And the end state should match the script's own idempotent behaviour

  # Why: Output-scrub on store — a postgres DSN in script output is scrubbed before it reaches the result
  @edge-case @smoke
  Scenario: A postgres DSN in the script output is scrubbed before being stored
    Given a deploy step whose script prints a postgres connection string to its output
    When the executor runs the step
    Then the stored step result should not contain the postgres connection string
    And the captured output should show the connection string replaced by a redaction marker

  # Why: Output-scrub on publish — a password in script output is scrubbed before the result is announced
  @edge-case
  Scenario: A password in the script output is scrubbed before the result is published
    Given a smoke-test step whose script prints a password to its output
    When the executor runs the step
    Then the published step result should not contain the password
    And the published captured output should show the password replaced by a redaction marker

  # Why: Both streams captured — a secret on the error stream is captured and scrubbed too
  # [ASSUMPTION: confidence=medium] stdout and stderr are both captured into the single captured-output field; scrubbing applies to both.
  @edge-case
  Scenario: A secret printed to the error stream is captured and scrubbed
    Given a smoke-test step whose script prints a postgres DSN to its error stream and exits non-zero
    When the executor runs the step
    Then the step should be recorded as failed
    And the captured output should include the error stream
    And the postgres DSN should not appear anywhere in the captured output
```

```
━━ GROUP E: Integration (1 scenario) ━━
```

```gherkin
  # Why: Real-script proof — the actual fleet-memory smoke script is the verdict against a throwaway target
  # [ASSUMPTION: confidence=medium] The integration test is marker-gated and runs only against a disposable target, never production.
  @integration @slow
  Scenario: The fleet-memory smoke script runs against a throwaway target and its exit status is the verdict
    Given the fleet-memory smoke-test script and a throwaway target
    When the executor runs a smoke-test step naming that script
    Then the smoke script should run to completion against the throwaway target
    And the step's verdict should be the script's exit status
    And no credentials from the script output should appear in the step result
```

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 15 scenarios across 5 groups
Inferred assumptions: 8 (will be resolved in Phase 5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review each group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [A] Accept   [R] Reject   [M] Modify   [+] Add   [?] Defer

Fast path: "A A A A A" accepts all five groups.

GROUP A — Key Examples (5):        _
GROUP B — Boundary Conditions (2): _
GROUP C — Negative Cases (3):      _
GROUP D — Edge Cases (4):          _
GROUP E — Integration (1):         _
```

Two things worth flagging for your curation decision:

1. **The scrubber doesn't exist yet.** [redaction.py](src/forge/memory/redaction.py)'s `redact_credentials` covers GitHub tokens / bearer / hex but **not** postgres DSN or password patterns. This feature needs a new pattern set (e.g. `postgresql://user:pass@host/db` and `password=` / `PGPASSWORD=` shapes). I'll surface the exact patterns as a Phase 5 assumption — decide whether to extend `redaction.py` or add a sibling scrubber.

2. **No timeout is specified** in your description. I'll raise "handler imposes no subprocess timeout this phase" as a low-confidence Phase 5 assumption — flag now if you want a timeout scenario added to Group D instead.

Tell me your five group decisions (and any M/+ details) and I'll run Phase 4 → 5 → file generation.
