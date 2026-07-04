---
id: TASK-FWD-003
title: "Duplicate-active-build guard + un-acked redelivery = permanent dispatch wedge"
status: backlog
created: 2026-07-04T11:00:00Z
priority: high
task_type: feature
tags: [wire-dispatch, pipeline-consumer, found-2026-07-04]
complexity: 5
---

# Duplicate-guard/redelivery wedge

When a run's thread state is evicted (in-mem backend, TASK-ABW-004) the
message stays un-acked and redelivers; `dispatch_build` then refuses every
redelivery as "duplicate active build" while nothing ever terminalises the
SQLite row. Also: `exists_active_build` matches ANY active row, so stale
QUEUED rows (7 found for FEAT-9E59) block dispatch indefinitely; and
cancel-then-redeliver acks-as-terminal without re-dispatching (fresh envelope
required). Design decision needed: redelivery of an active-but-runless build
should re-dispatch, terminalise, or escalate — never spin silently.
Related: TASK-ABW-003 (identity provider), TASK-ABW-004 (backend persistence).

## Acceptance Criteria
- [ ] Documented decision + implementation for redelivery-vs-active-build.
- [ ] Stale-row hygiene: startup reconcile terminalises QUEUED rows older than
      a threshold (or equivalent).
- [ ] Integration test reproducing the 2026-07-04 wedge passes.
- [ ] All modified files pass project-configured lint/format checks with zero errors
