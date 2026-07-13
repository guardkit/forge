---
id: TASK-FWD-005
title: "forge cancel is unusable under docker exec: os.getlogin() OSError + a stale forge.db shadows the live one"
status: done
created: 2026-07-11T21:35:00Z
resolved: 2026-07-13
priority: medium
task_type: bug
found_by: JNB-009 live-probe lane (2026-07-11) — the probe driver could not perform its mandatory toy-build cleanup
resolved_by: E2-S2 ops pass (Phase E, factory close-out) — 2026-07-13
feature_ref: FEAT-28FF
tags: [cli, cancel, ops, docker, gb10, found-2026-07-11, fixed-2026-07-13]
complexity: 2
---

# forge cancel cannot run inside the production container

## Resolution (2026-07-13, E2-S2 ops pass — all three breaks closed)

All three diagnosed breaks fixed, hermetically gated (stubbed sessions / the
in-memory test bus — the live NATS bus, forge-prod, and every running container
were untouched):

1. **`os.getlogin()` crash → terminal-independent responder.** New
   `src/forge/cli/_responder.py::resolve_responder` resolves the identity via
   `--responder` flag → `$FORGE_RESPONDER` → gate approver → `$USER`/`$LOGNAME`
   → `getpass.getuser()` → guarded `os.getlogin()` → `"unknown"` sentinel. Never
   an unhandled `OSError` under `docker exec`. `forge cancel` gains a
   `--responder` flag; `forge skip` gets the same treatment (identical bug).
2. **Stale-DB shadowing → canonical resolution + loud failure.** New
   `src/forge/cli/_db_resolve.py::resolve_db_path` resolves the SAME ledger
   `forge serve` boots against (`$FORGE_DB_PATH` → `~/.forge/forge.db`); `--db`
   is now optional and only overrides. A missing ledger or a no-such-run
   identifier both **exit 2, naming the resolved DB** — no silent no-op.
3. **`SYNTHETIC_RESPONDER='rich'` vs the identity-pinned gate → pinned inject.**
   The paused path threads the resolved responder (default = the gate's
   `approval.expected_approver` from `forge.yaml`) down through
   `try_inject_paused_cancel` → `SyntheticResponseInjector.inject_cli_cancel`
   → `_publish_synthetic`, stamping it onto `ApprovalResponsePayload.decided_by`.
   An identity-pinned subscriber now ACCEPTS the reject (proven on the test bus)
   instead of refusing the old hardcoded `"rich"`. One nats-core contract: the
   reject rides the existing `agents.approval.{agent}.{task}.response` subject
   (nats-core `topics.py`) — no invented topics.

### Files
- `src/forge/cli/_responder.py` (new), `src/forge/cli/_db_resolve.py` (new),
  `src/forge/cli/_cancel_run.py` (new — orchestration extracted so `cancel.py`
  stays a thin Click shell, AC-007).
- `src/forge/cli/cancel.py`, `src/forge/cli/skip.py`,
  `src/forge/cli/_cancel_gate_inject.py`,
  `src/forge/adapters/nats/synthetic_response_injector.py`,
  `src/forge/cli/main.py` (docstring).

### Hermetic gate (receipts)
- `tests/integration/test_cli_cancel_ops_o02.py` — the live-shaped round-trip:
  fixture run → `forge cancel --responder <id>` → run reaches **CANCELLED** on a
  real migrated SQLite ledger (publish seam stubbed); the identity-pinned
  round-trip on the test bus (pinned responder accepted / wrong responder
  refused, build stays paused); canonical-DB resolution; and the no-such-run /
  missing-ledger loud-failure tests.
- `tests/forge/test_cli_responder_and_db_resolve.py` — resolver + DB-resolution
  units + the `os.getlogin()`-raising regression + responder-threading spy.
- Updated `tests/forge/test_cli_cancel_skip.py` (AC-006 → responder resolution)
  and `tests/bdd/test_pipeline_state_machine_cancel_skip.py` (repaired a
  pre-existing red: the paused fake lacked `get_build_row`).

# forge cancel cannot run inside the production container

## Problem (observed live, 2026-07-11, forge-prod on the GB10)

The natural ops path — `docker exec forge-prod forge cancel <FEAT-ID> --db …` — fails
two independent ways:

1. **Stale db shadowing.** `/var/forge/forge.db` exists inside the container (via the
   `~/forge-state` mount) but is NOT the live ledger — the daemon boots against
   `/home/forge/.forge/forge.db` (the `~/forge-prod-state/.forge` mount). Cancel
   against the stale path exits "no active or recent build" for builds that plainly
   exist: a silent wrong-db no-op with a confusing message.
2. **`os.getlogin()` crash.** With the correct db, the non-paused cancel path
   (`cancel.py` → `handle_cancel(responder=os.getlogin())`) raises `OSError -6`
   under `docker exec` (no utmp/loginuid; `-t` does not help), and there is no
   `--responder` flag to bypass it.

3. **`SYNTHETIC_RESPONDER='rich'` vs the identity-pinned gate (the deepest one —
   observed live 20:44:55Z).** For a PAUSED-at-gate build, `try_inject_paused_cancel`
   avoids `os.getlogin()` but publishes the reject with the hardcoded constant
   `SYNTHETIC_RESPONDER = "rich"` (`synthetic_response_injector.py:89`). A gate
   deployed with `expected_approver` (forge-prod pins `U03QR8WKT29`) REJECTS it:
   `approval_subscriber: unrecognised responder 'rich' (expected 'U03QR8WKT29') —
   anomaly, NOT resuming`. The build stays PAUSED. **Worse, the CLI reports
   success** ("synthetic reject injected") — a false-positive cancel: fire-and-forget
   publish with no confirmation the daemon accepted it.

Net: against an identity-pinned gate there is NO working `forge cancel` at all —
in-container OR host-side — for either QUEUED or PAUSED builds.

## Workaround (validated live on FEAT-3CC2, 2026-07-11 21:46 BST)

Host-side, patch the responder to the pinned approver and drive the CLI's own
injector (the Session-A synthetic identity-pinned response pattern):

```
set -a; . ~/.config/forge/nats.env; set +a   # FORGE_NATS_URL for the one-shot publish
cd ~/Projects/appmilla_github/forge && .venv/bin/python - <<'EOF'
import asyncio, os, sqlite3
import forge.adapters.nats.synthetic_response_injector as sri
from forge.gating.identity import parse_request_id
sri.SYNTHETIC_RESPONDER = "U03QR8WKT29"        # the pinned approver of record
build_id, req_id, corr = ...                    # from the LIVE db: ~/forge-prod-state/.forge/forge.db
_b, stage, attempt = parse_request_id(req_id)
async def go():
    import nats
    nc = await nats.connect(servers=os.environ["FORGE_NATS_URL"])
    try:
        await sri.SyntheticResponseInjector(nats_client=nc).inject_cli_cancel(
            build_id=build_id, stage_label=stage, attempt_count=attempt, correlation_id=corr)
        await nc.flush()
    finally: await nc.close()
asyncio.run(go())
EOF
```

Result on FEAT-3CC2: gate decided CANCELLED, slot acked, nothing launched,
`build-cancelled` terminal notification delivered (`Cancelled by: U03QR8WKT29`).

## Acceptance criteria

- `forge cancel` works under `docker exec` for both QUEUED and PAUSED builds:
  replace `os.getlogin()` with a fallback chain (e.g. `--responder` flag →
  `$FORGE_RESPONDER`/`$USER`/`getpass.getuser()` → `os.getlogin()` last), never an
  unhandled OSError.
- The paused-path synthetic reject satisfies an identity-pinned gate: the responder
  identity comes from config/flag (default = the gate's `expected_approver`), not the
  hardcoded `SYNTHETIC_RESPONDER='rich'`; and the CLI must not report success on a
  fire-and-forget publish the daemon then rejects (await the state transition or say
  "injected, unconfirmed").
- The wrong-db footgun is removed or fenced: either the container stops shipping a
  reachable stale `/var/forge/forge.db`, or `forge cancel` warns when the db it was
  given belongs to no running daemon (e.g. boot-log path mismatch), or `--db` gains
  a documented default pointing at the live ledger.
- A regression test covers cancel-without-controlling-terminal (`os.getlogin()`
  raising) on both the paused and non-paused paths.

## Notes

- Same-family nit: `WorktreeGitRunner` ambient `GIT_*` dependency (GITMOUNT follow-up)
  — both are "the CLI assumes an interactive login environment" bugs.
- The JNB-009 probe runbook (ai-transition handoff §8) has been corrected to use the
  host-side recipe and the real feature-yaml path (`api_test/.guardkit/features/`).
