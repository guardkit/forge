# FEAT-SSH — Shell Script Step Handlers

Two concrete runbook step handlers that wrap existing shell scripts as
subprocesses and register into the FEAT-RBX step-type registry: `deploy_compose`
and `run_smoke_tests`. Each runs a named script in a working directory with an
env-file available **by path only**, captures combined stdout/stderr, scrubs
postgres DSNs and passwords at the capture boundary, and maps the script's exit
status to a verdict (`0 → passed`, non-zero → `failed`).

- **Review:** TASK-REV-SSH1
- **Spec:** [`features/shell-script-step-handlers/`](../../../features/shell-script-step-handlers/) (20 scenarios, 14 assumptions)
- **Upstream contract:** `StepHandler` / `StepOutcome` / `StepTypeRegistry` in
  [`src/forge/executor/registry.py`](../../../src/forge/executor/registry.py) (FEAT-RBX)
- **Guide:** [IMPLEMENTATION-GUIDE.md](./IMPLEMENTATION-GUIDE.md) (diagrams + §4 contracts)

## Tasks

| Task | Title | cx | Wave | Deps |
|------|-------|----|------|------|
| [TASK-SSH-001](./TASK-SSH-001-scrub-process-output.md) | `scrub_process_output` (DSN + password) | 4 | 1 | — |
| [TASK-SSH-002](./TASK-SSH-002-subprocess-runner-core.md) | Subprocess core (timeout + size-cap + scrub) | 5 | 2 | 001 |
| [TASK-SSH-003](./TASK-SSH-003-deploy-compose-handler.md) | `deploy_compose` handler | 3 | 3 | 002 |
| [TASK-SSH-004](./TASK-SSH-004-run-smoke-tests-handler.md) | `run_smoke_tests` handler | 3 | 3 | 002 |
| [TASK-SSH-005](./TASK-SSH-005-register-shell-handlers.md) | `register_shell_handlers` wiring | 2 | 4 | 003,004 |
| [TASK-SSH-006](./TASK-SSH-006-integration-fleet-memory-smoke.md) | Integration test vs fleet-memory `smoke.sh` | 3 | 5 | 005 |

## Key decisions

- **Shared subprocess core** + two thin handlers → one credential-scrub site.
- **New sibling scrubber** `scrub_process_output` (not a rewrite of
  `redact_credentials`).
- **Hardened** subprocess timeout + output size-cap (extends ASSUM-008/009).
- **Deferred** env-file pre-validation (ASSUM-013) — missing file → script's own
  non-zero exit.
- Step-type keys: `deploy_compose` / `run_smoke_tests`.

## Build

```bash
/feature-build FEAT-SSH
```
