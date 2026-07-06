---
id: TASK-FWD-004
title: "GB10 ops cleanup: disable duplicate forge-autobuild-runner unit; revert attended-run overrides for P2"
status: backlog
created: 2026-07-04T11:00:00Z
priority: normal
task_type: operator_handoff
tags: [gb10, ops, found-2026-07-04]
complexity: 2
---

# GB10 ops cleanup (operator)

## Required operator follow-up

This task is task_type: operator_handoff — AutoBuild will not attempt it.
The operator must perform these on the GB10, then mark complete via
/task-complete.

- **Disable the duplicate unit**: `forge-autobuild-runner.service` races
  `forge-langgraph-sidecar` for port 8124 at every boot (no env contract;
  idles when it loses). `systemctl --user disable forge-autobuild-runner`.
  - ✅ **DONE 2026-07-06** — unit disabled on the GB10 (recorded here per the
    house lesson: runtime re-pins need committed artifacts; supersedes the
    "still enabled" line in the D659 deploy-verification of the same date).
- **Before P2 local-inference validation, revert the attended-run overrides**:
  restore the two `--coach-model coach-ft-v3` argv lines in
  `src/forge/subagents/autobuild_runner.py` (uncommitted deletion on the GB10
  forge checkout — plain `git checkout` works), remove
  `Environment=GUARDKIT_HARNESS=sdk` from forge-langgraph-sidecar.service,
  daemon-reload + stop/sleep 5/start.
- **Rotate JARVIS_NATS_PASSWORD** (leaked in chat 2026-07-04):
  nats-infrastructure/.env on GB10 + docker compose restart nats +
  ~/.config/guardkit/jarvis.env + restart jarvis-serve-nats.
