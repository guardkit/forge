---
id: TASK-FWD-001
title: "Identity provider returns stale async_tasks row (LIMIT 1 without ORDER BY)"
status: completed
created: 2026-07-04T11:00:00Z
priority: high
task_type: feature
tags: [wire-dispatch, lifecycle-bridge, found-2026-07-04]
complexity: 3
reconciled: "2026-07-11 WS3-S8 sweep — fix shipped in prod d962425 (newest-first identity resolution, _serve_production.py); §4 named item"
---

# Identity provider returns stale async_tasks row

`_async_tasks_identity_provider` does `SELECT task_id FROM async_tasks ...
LIMIT 1` with no ORDER BY. With stale rows present it polled a May-era
thread_id that 404s forever; the observer exited and the queued message was
never acked (silent deadlock — found live 2026-07-04 during the FEAT-UBS-003
checkpoint; GB10 operator deleted 7 stale rows by hand to unwedge).

## Acceptance Criteria
- [ ] Provider selects the newest row (ORDER BY started_at DESC) or rows are
      purged on terminal transition — pick one, document why.
- [ ] Regression test: two rows for one feature_id, newest wins.
- [ ] All modified files pass project-configured lint/format checks with zero errors
