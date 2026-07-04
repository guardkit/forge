# FEAT-UBS-003 — Jarvis Notification Bridge (forge-side tasks)

This folder holds the **forge-side** tasks of FEAT-UBS-003 (Jarvis Slack notification bridge). All three are **v1.1** and are **HARD-GATED on the live v1 checkpoint — TASK-JNB-004 in the jarvis repo** (toy feature from Open WebUI showing queued → running → terminal on the phone). Do not start any task below until that checkpoint has passed.

| Task | Title | Type | Wave | Complexity | Dependencies |
|---|---|---|---|---|---|
| TASK-JNB-101 | forge: ApprovalSubscriber production wiring into the serve runtime | feature | 7 | 7 | TASK-JNB-004 (jarvis) |
| TASK-JNB-102 | forge: emit build-cancelled on CANCELLED transitions (ASSUM-010 closure) | feature | 8 | 5 | TASK-JNB-101 |
| TASK-JNB-106 | forge: v1.1 scenario tests over the production wiring | testing | 9 | 5 | TASK-JNB-101, TASK-JNB-102 |

TASK-JNB-102 is sequenced after TASK-JNB-101 because both edit `gating/wrappers.py`; TASK-JNB-106 tests the combined production wiring.

## References

- **Canonical implementation guide** (covers both repos, waves 1–10): jarvis repo, `tasks/backlog/jarvis-notification-bridge/IMPLEMENTATION-GUIDE.md`. The jarvis-side tasks (TASK-JNB-001..009, 103, 104, 105, 107) live in the jarvis repo's own backlog — autobuild cannot edit sibling repos.
- **Review report**: `.claude/reviews/TASK-REV-C951-review-report.md`

## Note on the feature YAML

The forge v1.1 feature YAML is **deliberately not generated** until the v1 checkpoint (TASK-JNB-004) passes. The task frontmatter carries `feature_id: pending-v1.1` until then.
