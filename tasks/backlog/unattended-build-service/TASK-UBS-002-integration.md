---
id: TASK-UBS-002-integration
title: Wire the budget-guard skeleton into the supervisor + plumb the profile
task_type: feature_work
parent_feature: unattended-build-service
feature_id: FEAT-UBS-002
wave: 2
implementation_mode: pipeline
complexity: 6
dependencies:
  - FEAT-UBS-002-skeleton  # config models + budget_guard module + CLI flag (landed)
  - TASK-ABW-OPS           # produces the autobuild transcript → coach-score gap
status: pending
---

# TASK-UBS-002-integration — connect the budget guard to the live build loop

## Context

The FEAT-UBS-002 **skeleton** has landed (2026-07-02):

- `forge.config.models.BudgetGuards` / `BudgetConfig` — named profiles;
  `attended` reserved as caps-off (ASSUM-010); `unattended` conservative caps.
- `forge.pipeline.budget_guard` — pure `evaluate_budget`, the escalation
  details dict, and the `risk_level="high"` `ApprovalRequestPayload` builder.
- `forge queue --profile` — validates the name + echoes the caps.

Three integration pieces were **deliberately deferred** because they touch the
live supervisor loop and/or the frozen `nats-core` seam and cannot be
end-to-end validated without a real Mode C run. This task closes them.

## Scope

### 1. Supervisor enforcement seam
Wire `evaluate_budget` into `ForgeSupervisor._next_turn_mode_c`
(`src/forge/pipeline/supervisor.py`, planner call at ~L1269):

- Inject the resolved `BudgetGuards` for the build (see §2) + a wall-clock
  source into the supervisor's constructor (dependency injection, mirroring
  `mode_c_planner` / `mode_c_history_reader`).
- After the planner returns a **follow-up** `TASK_REVIEW` (the cyclic step),
  compute `BuildBudgetMetrics`:
  - `review_cycles` via `budget_guard.count_review_cycles(history,
    is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW)`;
  - `elapsed_wallclock_seconds` from the build's `started_at`;
  - `tokens_used` if available (else `None`);
  - `last_coach_score` from the build (see §3 — `None` until the gap closes).
- On a breach: do **not** dispatch. Add a `TurnOutcome.PAUSED_BUDGET`, emit the
  `build_budget_breach_approval_payload(...)` via the approval publisher, then
  pause via `PipelineLifecycleEmitter.emit_paused_then_interrupt` (ADR-ARCH-021
  ordering: publish `build-paused` before `interrupt()`). Never a silent stop,
  never a silent continue (scope §4).
- The guard is a no-op for attended profiles (`caps_enabled is False`) — do not
  touch the ModeCCyclePlanner (ASSUM-010 stays intact).

### 2. Carry the profile across the queue→daemon boundary
`--profile` is validated at queue time but not yet delivered to the daemon —
`BuildQueuedPayload` (nats-core) has no profile field and the `builds` table has
no profile column. Pick one:
- **(a)** Add `builds.profile TEXT` (schema.sql migration + `record_pending_build`
  INSERT + `_row_to_build_row` + `BuildRow`); the daemon reads it and resolves
  caps via `config.budget.resolve(row.profile)`. Forge-only; preferred.
- **(b)** Add `BuildQueuedPayload.profile` in nats-core — a **seam change**, so
  it goes through an ADR + coordinated release, not a forge session.
Until then the daemon applies `config.budget.default_profile`.

### 3. Activate the coach-score floor
The `min_coach_score` branch in `evaluate_budget` is inert while
`last_coach_score` is `None` (ADR-ARCH-033). This unblocks once the coach-score
gap closes (the runner populates `AutobuildState.last_coach_score` from the
transcript captured by TASK-ABW-OPS AC-OPS-06). No change to `budget_guard` is
needed — just feed a real score into `BuildBudgetMetrics`.

## Acceptance criteria
- [ ] AC-01: A Mode C build under an unattended profile that reaches
  `max_review_cycles` pauses and emits a `risk_level="high"` approval request
  whose `details.reason == "budget_guard_breach"`; SQLite shows PAUSED.
- [ ] AC-02: A wall-clock breach on a stalled stage pauses + notifies.
- [ ] AC-03: An attended build (caps off) is never paused by the guard —
  ModeCCyclePlanner behaviour is byte-for-byte unchanged.
- [ ] AC-04: `forge queue --profile unattended` delivers the profile to the
  daemon (via §2), verified by the daemon resolving the unattended caps.
- [ ] AC-05: With a real `last_coach_score` present, a score below the floor
  pauses; at/above the floor proceeds.

## Out of scope
- The QA-Verifier fine-tune and the coach-score *parser* itself (guardkit /
  ADR-ARCH-033 — this task only *consumes* a populated score).
- Editing `nats-core` unless option §2(b) is explicitly chosen via ADR.
