---
id: TASK-RBX-007
title: Concurrency & real-broker integration tests
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: testing
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 5
implementation_mode: task-work
complexity: 4
estimated_minutes: 60
dependencies:
- TASK-RBX-004
- TASK-RBX-005
tags:
- forge
- runbook
- executor
- testing
- concurrency
- integration
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-22T09:00:12.660095'
  last_updated: '2026-06-22T09:55:26.634944'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file src/forge/executor/executor.py. Actual:\
      \ Path is tracked in git but 'git status --porcelain' shows no change for it\
      \ \u2014 the Player claimed work on a file it did not actually modify this turn.\
      \ Most likely cause: the report writer swept an orchestrator-managed path (e.g.\
      \ a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file tests/bdd/test_runbook_executor_integration.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- AC-001: Player explicitly states the two-executor\
      \ atomic execution guarantee cannot be achieved without architectural changes.\
      \ Player's evidence states: 'cannot prevent handler double-execution in race\
      \ conditions without violating the no application-level locks constraint. This\
      \ requires architectural discussion on whether to: (1) modify repository API\
      \ to support atomic check-and-update, (2) use BEGIN EXCLUSIVE instead of BEGIN\
      \ IMMEDIATE, or (3) accept best-effort concurrency with idempotent handlers.':\
      \ This requires architectural decision before proceeding: either (1) accept\
      \ the limitation and document that handlers must be idempotent, (2) modify the\
      \ repository API to provide atomic check-and-update, or (3) escalate to architect\
      \ for guidance on transaction isolation level changes.\n... and 2 more issues"
    timestamp: '2026-06-22T09:00:12.660095'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: '- Direct-mode evidence gate blocked the turn (direct_mode_ac_unverified).
      Direct mode relaxes coverage/arch gates but still requires verifiable AC delivery,
      resolved wiring, and runnable registered producers:

      - [direct_mode_ac_unverified] Direct mode: 5/5 acceptance criteria have no disk
      evidence (unmet: [''AC-001'', ''AC-002'', ''AC-003'', ''AC-004'', ''AC-005'']).
      Direct mode relaxes coverage/arch but NOT AC delivery.'
    timestamp: '2026-06-22T09:15:47.440883'
    player_summary: '[RECOVERED via player_report] Original error: SDK timeout after
      2065s: task-work execution exceeded 2065s timeout'
    player_success: true
    coach_success: true
---

# Concurrency & real-broker integration tests

## TL;DR

Lock in the Concurrency (Group F) property and the single real-NATS
Integration-Boundary scenario (Group H). The concurrency test is in-memory and
runs by default; the real-broker test is `@integration @slow` and excluded from
the default `pytest` run.

## Architectural Decision (RESOLVED — implement this)

The two-executor "each handler runs exactly once" guarantee is **not**
achievable with `BEGIN IMMEDIATE` alone: it serialises the *writes* but not
the read-then-run, so two executors can both read a step as `pending` and both
run its handler (a TOCTOU race). The resolved approach is an **atomic
check-and-update claim (compare-and-swap)** — no application-level locks:

- Add `RunbookRepository.claim_step(runbook_id, sequence_index)`: inside a
  `BEGIN IMMEDIATE` transaction run
  `UPDATE runbook_steps SET status='running'
   WHERE runbook_id=? AND sequence_index=? AND status='pending'`,
  then return `cursor.rowcount == 1`. The atomicity lives in the single
  conditional UPDATE, so exactly one executor's claim matches; the loser gets
  `rowcount == 0`.
- In the executor dispatch loop, **claim before running**: call
  `claim_step(...)` and only run the handler when the claim succeeds. A failed
  claim means another executor already owns/ran that step — skip it and
  advance (do not run the handler, do not error).
- This satisfies the "no application-level locks" constraint: the guard is a
  DB-level conditional write, not an in-process mutex/semaphore.

Scope add: this touches `src/forge/persistence/repositories/runbook.py` (new
`claim_step`) and `src/forge/executor/executor.py` (claim-gate the handler).
Keep `update_step_status` for the result/terminal write.

## Scope

`tests/bdd/test_runbook_executor.py` (Concurrency binding) and
`tests/bdd/test_runbook_executor_integration.py` (real-broker binding).

**Concurrency (Group F) — default suite:**
- "Two executors running the same runbook do not run the same step twice" —
  two executors started on one persisted runbook; each step's handler runs
  exactly once *across both*, the runbook completes, and neither re-runs a step
  the other completed. Relies on the repository's `BEGIN IMMEDIATE` serialising
  committed progress (FEAT-RSP Group F).

**Integration boundary (Group H) — `@integration @slow`, excluded by default:**
- "Lifecycle events are published to a real NATS broker and observed by a
  subscriber" — with a real broker available and a subscriber listening, a
  two-step runbook run produces, on the wire: `runbook-started`, then
  `step-started` + `step-result` per step in order, then `runbook-complete`.

## Acceptance Criteria

- [ ] The two-executor scenario asserts each handler ran exactly once across
      both executors, the runbook completed, and no step was double-run —
      guaranteed by the atomic `claim_step` compare-and-swap (see
      **Architectural Decision**), not application-level locks.
- [ ] The concurrency test runs in the default `pytest` invocation (no broker,
      no subprocess) and is deterministic (no sleeps-as-synchronisation).
- [ ] The real-broker scenario is tagged `@integration @slow` and is excluded
      from the default run; `pytest -m "integration and slow"` exercises it
      when a broker is available.
- [ ] The subscriber observes the full lifecycle in order over the wire
      (runbook-started → per-step started/result → runbook-complete).
- [ ] No unknown-mark warnings for `concurrency` / `integration` / `slow`.

## Coach Validation

```bash
# Default suite (concurrency included, real-broker excluded)
python -m pytest tests/bdd/test_runbook_executor.py -q -m concurrency
# Real broker (run explicitly where a NATS broker is available)
python -m pytest tests/bdd/test_runbook_executor_integration.py -q -m "integration and slow"
```

## Implementation Notes

- Drive the two executors with threads/tasks sharing one SQLite file; assert on
  per-handler call counts collected in a thread-safe counter. The single-run
  guarantee comes from the `claim_step` compare-and-swap (see **Architectural
  Decision**) plus the repository's busy-timeout — do not add application-level
  locks (that would mask the real concurrency contract).
- For the real-broker test, reuse the integration harness pattern other forge
  `@integration @slow` NATS tests use (see `tests/forge/test_fleet_publisher.py`
  / the pipeline integration tests) for broker discovery + subscriber setup.
- The `slow` mark already gates docker/live-NATS tests out of the default run;
  no pyproject change needed beyond the `runbook-executor` marks registered in
  TASK-RBX-001.
