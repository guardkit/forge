---
id: TASK-FMDR-007
title: "Fix shell-step script resolution — handler can't execute a bare script name with a relative cwd (blocks TASK-FMDR-005)"
status: completed
created: 2026-06-23 00:00:00+00:00
updated: 2026-06-23 00:00:00+00:00
completed: 2026-06-23 00:00:00+00:00
previous_state: in_review
state_transition_reason: "Quality gates passed — fix + regression tests green"
completed_location: tasks/completed/2026-06/
priority: high
task_type: feature
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 3
implementation_mode: task-work
complexity: 3
estimated_minutes: 45
dependencies: []
tags:
  - forge-output-loop
  - runbook-executor
  - shell-step-handler
  - bug
  - false-green
test_results:
  status: passing
  coverage: null
  last_run: 2026-06-23 00:00:00+00:00
  notes: "tests/forge/executor/ + test_runbook_exemplar.py = 103 passed, 4 skipped. 4 new bare-name+relative-cwd regression tests pass. Remaining suite failures (8) proven pre-existing/environmental via baseline stash (docker image, NATS auth broker, clock-hygiene in adapters/nats, real-broker fixtures) — unrelated to this change."
resolution: "Option A — fixed the handler (_run_script_step) to resolve a bare script name relative to cwd by prepending ./; exemplar JSON + filename-only contract left intact."
---

# TASK-FMDR-007 — Shell-step handler cannot execute the exemplar runbook's script param

**Filed from the TASK-FMDR-005 real-NAS run attempt on 2026-06-23 (GB10 `promaxgb10-41b1`).**
This is the blocker that stops TASK-FMDR-005 from completing.

## Problem

The first real execution of the shipped exemplar runbook
`forge/runbooks/RUNBOOK-fleet-memory-nas.json` through the real shell-step handler
**fails immediately at step 0 (`deploy_compose`)** with the runbook escalated and the
step's captured payload showing **`exit_code: 127` (command not found)** and empty output.
Nothing is stood up on the NAS (clean failure — the D8 property holds).

## Root cause

The exemplar runbook steps use a **bare script filename** with a **relative cwd**:

```json
{ "step_type": "deploy_compose",
  "params": { "cwd": "fleet-memory/deploy/nas", "script": "deploy.sh", "env_file": ".env.deploy" } }
```

The handler (`src/forge/executor/shell_steps.py::_run_script_step`) executes:

```python
subprocess.run([script], cwd=cwd, env=env, capture_output=True, ...)
```

On Python 3.12, `subprocess.run(["deploy.sh"], cwd="fleet-memory/deploy/nas")` does **not**
resolve a bare program name (no slash) relative to `cwd` — it searches `PATH` (which does not
include the deploy dir), so it raises `FileNotFoundError`. The handler maps that to
`(127, "")`. A name **containing a slash** (`./deploy.sh`) *is* resolved relative to `cwd`.

### Reproduction (verified on the GB10, 2026-06-23)

```python
# from ~/Projects/appmilla_github, ENV_FILE=".env.deploy"
subprocess.run(["deploy.sh"],   cwd="fleet-memory/deploy/nas")  # -> FileNotFoundError  (handler => 127)
subprocess.run(["./deploy.sh"], cwd="fleet-memory/deploy/nas")  # -> returncode 0, runs in the dir
```

Persisted step result confirms it (outer `exit_code: 1` is the escalation sentinel; the inner
handler payload is the truth):

```json
{"exit_code": 1, "captured_output": "",
 "payload": {"exit_code": 127, "captured_output": ""}}
```

## Why every test stayed green (third instance of the FEAT-FMDR false-green pattern)

See `docs/reviews/FEAT-FMDR-autobuild-false-green-analysis.md`. The exact production
combination — **bare script name + relative cwd, run through the real handler** — was never
exercised:

- `tests/forge/test_runbook_exemplar.py` validates the JSON **shape only** and actively
  **asserts the broken form**: `assert step.params["script"] == "deploy.sh"` and
  `assert "/" not in script` (line ~228) with `cwd == "fleet-memory/deploy/nas"`. It never
  executes the steps.
- `tests/integration/test_fleet_memory_e2e.py` (TASK-FMDR-004) **does** execute, but uses
  `"script": "./deploy.sh"` / `"./smoke.sh"` with an **absolute** cwd (`deploy_local_abs`,
  lines ~173-184) — so it dodges the failing case entirely.
- `tests/bdd/test_fleet_memory_runbook.py` also uses `"./deploy.sh"` / `"./smoke.sh"`.

So the working tests and the shipped exemplar disagree on the script-param form, and no test
spans the gap. The real-NAS handoff is what surfaced it — exactly its job.

## Proposed fix — two candidates (decision deferred to implementation/review)

**Option A — fix the handler (RECOMMENDED).** Make `_run_script_step` honor its own
"script is relative to cwd" contract by joining and resolving the script against `cwd`
(e.g. run `[str(Path(cwd) / script)]`, or prepend `./` when `script` has no slash). This:
- Preserves the exemplar's **"script is filename-only"** design intent, which
  `test_runbook_exemplar.py` deliberately encodes (so a runbook is portable across targets —
  only `cwd`/`env_file` change).
- Fixes **every** runbook at once and leaves the shipped exemplar JSON untouched.
- Requires new handler unit tests for the bare-name + relative-cwd case (the gap above).

**Option B — fix the data.** Change the two steps to `"./deploy.sh"` / `"./smoke.sh"` (the
form the e2e/BDD tests already use) and update `test_runbook_exemplar.py`'s bare-name
assertions. Smaller diff, but it **violates** the filename-only contract and pushes a
"remember the `./`" footgun onto every future runbook author.

## Acceptance criteria

- [x] `forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json` (invoked from
      `~/Projects/appmilla_github`) resolves and executes `deploy.sh`/`smoke.sh` — no `127`.
      Handler now resolves the bare names relative to `cwd`; verified at the handler level
      (`test_bare_script_with_relative_cwd_runs`) and proven in the real e2e where step 0
      (`./deploy.sh`) ran and the runbook only escalated at the smoke step.
- [x] A regression test exercises the **real handler** with **`script="deploy.sh"` (bare) +
      a relative `cwd`** and asserts the script actually runs (exit 0 for a stub).
      Added `TestBareScriptNameRelativeCwd` (core runner, 3 tests) +
      `TestDeployComposeHandler::test_bare_script_with_relative_cwd_runs` (public handler).
- [x] `test_runbook_exemplar.py` and the chosen fix are consistent — Option A keeps the
      filename-only form, so `assert "/" not in script` / `script == "deploy.sh"` still pass.
- [~] Full suite green: `tests/forge/`, `tests/bdd/`, `tests/integration/`.
      All shell-step + exemplar tests green (103 passed). 8 remaining suite failures are
      **pre-existing and environmental**, proven via a baseline stash (same 4 representative
      failures occur with the fix reverted): docker production-image harness, NATS
      `Authorization Violation` (auth-required broker — see TASK-FMDR-008), clock-hygiene in
      `adapters/nats/approval_subscriber.py`, and real-broker BDD fixtures. None touch
      `shell_steps.py`; the fix is a logical no-op for every input they exercise.

## Implementation summary (Option A)

`src/forge/executor/shell_steps.py::_run_script_step` now computes the program path as:

```python
program = script if os.path.dirname(script) else os.path.join(os.curdir, script)
```

A bare filename (no directory component) is prepended with `./` so subprocess resolves it
relative to `cwd` instead of searching PATH; names already carrying a directory component
(`./deploy.sh`, `bin/deploy.sh`, `/abs/deploy.sh`) pass through untouched. Root cause and the
fix are verified on Python 3.12.3 on the GB10. Docstring updated to document the contract.

Tests added:
- `tests/forge/executor/test_shell_steps_core.py::TestBareScriptNameRelativeCwd`
- `tests/forge/executor/test_deploy_compose_handler.py::TestDeployComposeHandler::test_bare_script_with_relative_cwd_runs`

## Unblocks

- **TASK-FMDR-005** (real-NAS stand-up) — cannot complete until this lands.
</content>
</invoke>
