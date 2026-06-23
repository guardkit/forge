---
id: TASK-FMDR-008
title: "Wire NATS auth into `forge runbook run` so lifecycle events publish to an auth-required broker"
status: completed
created: 2026-06-23 00:00:00+00:00
updated: 2026-06-23 00:00:00+00:00
completed: 2026-06-23 00:00:00+00:00
previous_state: in_review
completed_location: tasks/completed/2026-06/
priority: medium
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
  - nats
  - lifecycle-events
test_results:
  status: passing
  coverage: 87
  last_run: 2026-06-23 00:00:00+00:00
---

# TASK-FMDR-008 — `forge runbook run` cannot authenticate to an auth-required NATS broker

**Filed from the TASK-FMDR-005 real-NAS run attempt on 2026-06-23 (GB10 `promaxgb10-41b1`).**

## Problem

The NATS broker on the GB10 (`nats-server`, `127.0.0.1:4222`) **requires authorization**.
The `forge runbook run` CLI connects with **no credentials**, so the broker rejects it with
`nats: 'Authorization Violation'` and the runbook's lifecycle events never publish — leaving
the TASK-FMDR-005 harvest AC ("lifecycle events fired in order") unsatisfiable on the live
broker. The workaround for the 2026-06-23 attempt is `--no-events`; the chosen proper fix is
to wire auth so events publish for real.

Secondary symptom: with the broker reachable-but-rejecting, the run **did not fall back
cleanly** — instead of the intended 2-second fail-then-NoOp, it **spun in a reconnect loop**
(50+ repeated `Authorization Violation` tracebacks in ~40s) and never reached the deploy step.
A best-effort connect against an *auth-rejecting* broker behaves differently from an
*unreachable* one. This should be fixed alongside the auth wiring (or as a sub-item).

## Evidence

`src/forge/cli/runbook.py::_connect_nats_best_effort` calls:

```python
client = await nats.connect(
    servers=servers,          # FORGE_NATS_URL or nats://127.0.0.1:4222
    connect_timeout=2,
    max_reconnect_attempts=0, # intended "fail fast"; did NOT prevent the reconnect spin
)
```

There is **no** `user` / `password` / `token` / `user_credentials` / nkey plumbing — the CLI
cannot satisfy an auth-required broker by any configuration. (`~/forge-state/forge.yaml` carries
no NATS config either; `/etc/nats/nats-server.conf` is not readable as the deploy user.)

## Acceptance criteria

- [x] `forge runbook run` can authenticate to the GB10 broker via an operator-supplied
      credential (token / user+password / `.creds` file), sourced from env (e.g. a
      `FORGE_NATS_CREDS` / `FORGE_NATS_TOKEN`) or `forge.yaml`, **without** logging the secret
      (route through the existing `forge.memory.redaction` scrubber).
      → `_resolve_nats_auth` resolves `FORGE_NATS_CREDS` / `FORGE_NATS_TOKEN` /
      `FORGE_NATS_USER`+`FORGE_NATS_PASSWORD` (env path; see Notes re: `forge.yaml`).
      Secrets are redacted from every log line **by value** (deterministic, shape-
      independent) plus the existing `forge.memory.redaction` shape scrubbers.
- [x] Against an **auth-rejecting** broker the connect **fails fast to the NoOp client** (no
      reconnect spin) — verified with a test that asserts no unbounded retry and that the
      runbook still executes its steps.
      → `allow_reconnect=False` makes the auth violation raise on the first attempt; tests
      assert a single connect attempt + the runbook still completes against the NoOp client.
- [~] With valid creds, a real run publishes `runbook_started → step_started → step_result →
      runbook_complete` **in order** to the live broker.
      → **Operator-verified only.** Needs the broker's real credentials + a live
      `auth_required` broker. In-order publishing is already proven at the unit level
      (`test_ordered_event_stream_and_queryable_record`); the auth kwargs now flow into
      `nats.connect`, so a valid-creds run exercises that same proven path.
- [x] `--no-events` still works as the credential-free escape hatch.
      → `--no-events` short-circuits to the NoOp client and never touches the connect seam.

## Implementation summary (2026-06-23)

**Changed:** `src/forge/cli/runbook.py` (+ tests in `tests/forge/test_cli_runbook.py`).

- `_resolve_nats_auth(environ)` — resolves connect kwargs from `FORGE_NATS_*` env vars
  (precedence: `.creds` file → token → user+password; lone user/password ignored; values
  stripped so a trailing newline can't corrupt the credential).
- `_connect_nats_best_effort` now passes the resolved auth kwargs plus
  **`allow_reconnect=False`** (the real fix for the reconnect-spin — `max_reconnect_attempts=0`
  alone did not prevent it) and `connect_timeout=2`. Any failure (auth reject, unreachable,
  `nats-py` absent) degrades to the NoOp client so the runbook still runs.
- `nats_connect` module-level **test seam** (mirrors `forge.cli._serve_daemon.nats_connect`)
  so auth-reject / unreachable brokers are testable without a live server.
- Secret-safe logging: `_sanitise_servers` strips inline `user:pass@` userinfo (incl.
  scheme-less URLs) from the logged server display, and `_scrub_for_log` redacts the
  **known secret values by literal** before applying `forge.memory.redaction` shape scrubbers —
  so a short opaque NATS token (which matches no shape pattern) is still never logged.

**Tests:** 65 pass (CLI runbook + fleet-memory BDD regression + exemplar); module line
coverage 87%. Independent security review run (security-specialist); its two HIGH findings
(scrubber shape-gaps for opaque NATS tokens; scheme-less URL userinfo) are addressed by the
by-value redaction + scheme-less stripping above.

**Scope note — `forge.yaml`:** credentials are sourced from env (the mechanism the AC names
explicitly and the one the GB10 operator needs). A `nats:` auth section in the `forge.yaml`
schema was left out of scope: `ForgeConfig` is a frozen, `extra="forbid"` schema with a
boundary rule against importing the NATS stack, and the runbook CLI does not currently load
`forge.yaml`; adding both is disproportionate to a complexity-3 task. Env fully satisfies the
"sourced from env … or forge.yaml" criterion.

## Notes

- The in-order publishing behavior itself is already proven at the unit level
  (`tests/bdd/test_fleet_memory_runbook.py` asserts `publish_*` ordering via a mock publisher);
  this task is purely about making it work against the **real, auth-required** broker.
- Needs the broker's actual credentials from the operator to verify end-to-end.

## Relates to

- **TASK-FMDR-005** — its harvest AC ("lifecycle events fired in order") needs this to be
  demonstrated on the live broker. (Core deploy ACs are unblocked by TASK-FMDR-007 alone.)
</content>
</invoke>
