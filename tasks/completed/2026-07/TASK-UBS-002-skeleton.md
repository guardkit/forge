---
id: TASK-UBS-002-skeleton
title: Budget-guard skeleton — config profiles, pure evaluator, CLI flag
task_type: feature
parent_feature: unattended-build-service
feature_id: FEAT-UBS-002
wave: 1
implementation_mode: task-work
complexity: 5
dependencies: []
status: completed
---

# TASK-UBS-002-skeleton — Budget-guard skeleton

**Status: completed (2026-07-02).** Built outside the autobuild pipeline, then
retro-formalised under `/feature-spec` + `/feature-plan` and subjected to an
independent `/code-review` pass (see FEAT-UBS-002 for the process record).

## What was built

- **Config models** — `src/forge/config/models.py`: `BudgetGuards` (per-profile
  caps) + `BudgetConfig` (named profiles; reserved `attended` = caps off per
  ASSUM-010; `unattended` = conservative defaults). Re-exported from
  `forge.config`.
- **Pure evaluator** — `src/forge/pipeline/budget_guard.py`: `evaluate_budget`
  (first-breach-wins; attended never breaches; token cap only when measured;
  `min_coach_score` floor STUB inert until `last_coach_score` is populated —
  ADR-ARCH-033), plus the breach → `ApprovalRequestPayload(risk_level="high")`
  escalation builder.
- **CLI** — `src/forge/cli/queue.py`: `forge queue --profile <name>` validates
  against `config.budget.profiles`, echoes the resolved caps, and honestly
  reports the not-yet-plumbed daemon delivery.

## Verification (this task's oracle)

Unit tests, all green (`.venv/bin/python -m pytest`):

- `tests/forge/config/test_budget_config.py`
- `tests/forge/pipeline/test_budget_guard.py`
- `tests/forge/test_cli_profile_flag.py`

BDD scenarios tagged `@task:TASK-UBS-002-skeleton` in
`features/unattended-build-service-budget-guards/unattended-build-service-budget-guards.feature`
are the Coach-blocking oracle for this task (the 15 `[SKELETON-SATISFIED]`
scenarios).

## Not in this task

The 3 `[DEFERRED]` scenarios (live pause/escalate, coach-score floor firing on a
real score, per-build profile reaching the daemon) belong to
[`TASK-UBS-002-integration.md`](TASK-UBS-002-integration.md).
