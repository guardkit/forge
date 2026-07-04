---
id: TASK-FWD-002
title: "Observer exits on identity-unresolved without arming the deadline timer (no synthetic build-failed)"
status: backlog
created: 2026-07-04T11:00:00Z
priority: high
task_type: feature
tags: [wire-dispatch, lifecycle-bridge, found-2026-07-04]
complexity: 4
---

# Arm the deadline timer when identity resolution fails

`wireup._wait_for_identity` gives up after 3 attempts and the observer exits
WITHOUT arming the per-attach deadline timer, so the promised synthetic
build-failed never publishes. Combined with redelivery this is a silent
infinite loop: the operator's phone shows build-queued then nothing (exactly
the "notification path failure masks a stuck build" risk in the UBS scope §6).

## Acceptance Criteria
- [ ] Identity-unresolved path arms the same deadline timer as the
      sidecar-unreachable path; synthetic build-failed publishes at deadline.
- [ ] Integration test: identity 404s -> deadline fires -> build-failed envelope.
- [ ] All modified files pass project-configured lint/format checks with zero errors
