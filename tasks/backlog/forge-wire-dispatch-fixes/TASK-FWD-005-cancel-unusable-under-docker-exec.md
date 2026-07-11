---
id: TASK-FWD-005
title: "forge cancel is unusable under docker exec: os.getlogin() OSError + a stale forge.db shadows the live one"
status: backlog
created: 2026-07-11T21:35:00Z
priority: medium
task_type: bug
found_by: JNB-009 live-probe lane (2026-07-11) — the probe driver could not perform its mandatory toy-build cleanup
feature_ref: FEAT-28FF
tags: [cli, cancel, ops, docker, gb10, found-2026-07-11]
complexity: 2
---

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

Net: for a QUEUED build there is NO working in-container cancel. (For a PAUSED-at-gate
build the `try_inject_paused_cancel` synthetic-reject path avoids `os.getlogin()` and
works — the crash only hits the fallback.)

## Workaround (validated, JNB-009)

Run cancel HOST-side against the bind-mounted live db (host `os.getlogin()` works):

```
set -a; . ~/.config/forge/nats.env; set +a   # FORGE_NATS_URL for the synthetic-reject one-shot publish
~/Projects/appmilla_github/forge/.venv/bin/forge cancel <FEAT-ID> \
  --db /home/richardwoollcott/forge-prod-state/.forge/forge.db \
  --reason "<why>"
```

## Acceptance criteria

- `forge cancel` works under `docker exec` for both QUEUED and PAUSED builds:
  replace `os.getlogin()` with a fallback chain (e.g. `--responder` flag →
  `$FORGE_RESPONDER`/`$USER`/`getpass.getuser()` → `os.getlogin()` last), never an
  unhandled OSError.
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
