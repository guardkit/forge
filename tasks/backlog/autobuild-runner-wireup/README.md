# Feature: autobuild-runner-wireup (FEAT-ABW1)

Closes the stub introduced by FOLLOWUP-B-FIX in
[`src/forge/subagents/autobuild_runner.py`](../../../src/forge/subagents/autobuild_runner.py)
by wiring `_node_running_wave` to invoke `guardkit autobuild feature` as
an async subprocess. Today the SSE bridge emits the full lifecycle wire
envelopes within ~1 second of a `pipeline.build-queued` arriving — but
no code is ever written into the target repo. This feature closes that
gap.

**Source plan**: [`docs/research/ideas/autobuild-runner-wireup-plan.md`](../../../docs/research/ideas/autobuild-runner-wireup-plan.md)
**Demo target**: FEAT-9E59 in `~/Projects/appmilla_github/api_test` on
the `ddd-demo` branch — DDDSW 2026-05-16.

## Tasks

| ID | Title | Type | Complexity | Wave | Est. |
|---|---|---|---|---|---|
| [TASK-ABW-001](TASK-ABW-001-wire-up-running-wave-node.md) | Wire up `_node_running_wave` to invoke `guardkit autobuild` | feature | 6 | 1 | 180 min |
| [TASK-ABW-OPS](TASK-ABW-OPS-operator-handoff.md) | Operator handoff — GB10 allowlist + sidecar restart workflow | operator_handoff | 2 | 2 | 30 min |

## Execution order

1. **Wave 1** — TASK-ABW-001 (code change, runs through AutoBuild /
   `/task-work`).
2. **Wave 2** — TASK-ABW-OPS (operator runs manually on the GB10 after
   merge; AutoBuild skips it because `task_type: operator_handoff`).

## Next steps

```bash
# Review the implementation guide first
cat tasks/backlog/autobuild-runner-wireup/IMPLEMENTATION-GUIDE.md

# Then implement TASK-ABW-001 — pick one:
/task-work TASK-ABW-001       # interactive
/feature-build FEAT-ABW1      # autonomous (see .guardkit/features/FEAT-ABW1.yaml)
```

After TASK-ABW-001 merges, run `/feature-complete` to surface the
operator follow-up checklist from TASK-ABW-OPS.
