---
id: TASK-MP-006
title: PLANNED-HANDOFF terminal + registry (idempotent, GitRunner-injected, sanitised
  notification)
task_type: feature
status: in_review
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 2
implementation_mode: task-work
complexity: 6
estimated_minutes: 80
dependencies:
- TASK-MP-001
- TASK-MP-002
tags:
- mode-p
- handoff
- terminal
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T13:13:47.119938'
  last_updated: '2026-07-06T13:31:35.324842'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- 8 tests failed with ''Expected mock to have been awaited once. Awaited
      0 times.'' This indicates async protocol violations - methods expected to be
      awaited were called synchronously or not called at all. File: tests/forge/planning/test_handoff.py
      (TestProtocolSatisfaction.test_reposi...): Review async/await usage in src/forge/planning/handoff.py.
      Ensure: (1) async methods are defined with ''async def'', (2) all async method
      calls use ''await'', (3) test mocks correctly expect async or sync based on
      actual implementation. Run pytest locally to reproduce the 8 failures and fix
      each systematically.

      - Evidence gathering aborted (gathering_status: partial_gate_abort) before independent
      verification, coverage measurement, or BDD oracle execution. No independent
      confirmation of Player''s self-reported test results.: Fix test failures first.
      Once tests pass, the orchestrator will complete full evidence gathering including
      independent test verification and coverage measurement.'
    timestamp: '2026-07-06T13:13:47.119938'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file src/forge/adapters/nats/planning_consumer.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/planning/gate_adapters.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/planning/handoff.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 5 more issues"
    timestamp: '2026-07-06T13:19:54.275842'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-07-06T13:24:27.601832'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-006 — PLANNED-HANDOFF terminal + terminal registry

## Description

The v1 terminal, registry-indirected so the FEAT-SPL-007/008 target terminal
replaces it by configuration (ASSUM-008; StepTypeRegistry precedent,
executor/registry.py). The handler: resolve the target repo's local working copy
via `PlanningConfig.target_repo_paths` (default from `default_target_repo`) ->
prepare a worktree branch `planning/{correlation_id}` via an injected `GitRunner`
protocol over `adapters/git/operations` (never the repo's primary working copy;
structured never-raise GitOpResult) -> write `feature_spec_inputs/{correlation_id}.md`
-> commit, NO push (v1) -> transition PLANNED_HANDOFF -> publish the notification
(NotificationPayload, `jarvis.notification.slack` — exists in frozen nats-core
0.5.0 with a live jarvis subscriber; mint NO new pipeline.* subjects) carrying the
exact attended `/feature-spec` command. **Idempotent re-execution** (RT-08) and
**sanitised notification content** (RT-09).

## BDD Scenarios

- "Approval by the originator advances the run to the planned handoff"
- "Rejection at the checkpoint cancels the run without committing anything" (nothing-committed half)
- "A planning request without a target repository hands off to the configured default repository"
- "A handoff that cannot commit to the target repository fails visibly"
- "Re-executing an approved handoff that already committed is idempotent"

## Files

- Creates: `src/forge/planning/terminal_registry.py` (name -> `PlanningTerminalHandler` Protocol map; v1 registers "planned-handoff"; lookup keyed by `PlanningConfig.terminal`), `src/forge/planning/handoff.py`, `src/forge/planning/notifications.py` (payload builder)
- Tests: `tests/forge/planning/test_terminal_registry.py`, `tests/forge/planning/test_handoff.py`, `tests/forge/planning/test_notifications.py`

## Acceptance Criteria

- [ ] Registry lookup is by string key from PlanningConfig with "planned-handoff" default; a test swaps in a fake handler purely via config — zero edits to planner/checkpoint modules required (registry-indirection proof)
- [ ] Approved run -> `feature_spec_inputs/{cid}.md` exists on branch `planning/{cid}` in a tmp_path-initialised real git repo; run row = PLANNED_HANDOFF with handoff_branch/handoff_path recorded; commit happens via the injected GitRunner, and no test touches any path outside tmp_path
- [ ] Notification payload contains the literal committed path and a command string starting `/feature-spec` referencing it (exact-substring predicate); the notification is constructed ONLY from validated components (repo, path, correlation_id) — raw `request_text` is never interpolated into the rendered text or the copy-pasteable command (RT-09; injection guard test with a hostile request_text)
- [ ] `target_repo=None` -> commit lands in the configured `default_target_repo`'s local path; unresolvable repo (no target_repo_paths entry) -> run FAILED with a structured reason, no GitRunner invocation
- [ ] GitRunner failure -> run FAILED with the handoff failure as the reason + failure notification published + row NOT PLANNED_HANDOFF
- [ ] Idempotency (RT-08): re-executing the handoff when branch + file already exist verifies content and proceeds to PLANNED_HANDOFF without a duplicate commit (recording-fake commit call-count stays at prior value); order pinned commit -> record -> notify with notify best-effort (DDR-007)
- [ ] Cancelled/rejected runs produce zero GitRunner invocations (recording-fake call-count == 0)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit + tmp_path real-git fixture for the happy path; recording fake GitRunner for
  failure/idempotency/zero-invocation paths (PS-008 — no environment-sensitive git).

## Implementation Notes

- The file content (approved product docs as spec inputs) mirrors specialist-agent's
  `feature_spec_inputs/<id>.md` shape (handler.py write_feature_spec_files) — keep a
  minimal v1: PO product docs JSON/markdown + originator + approval provenance.
- correlation_id reaching this module is already sanitised at intake (TASK-MP-008);
  still resolve the final path against the repo root and refuse escapes
  (mirror `_path_inside_allowlist`, defence in depth).
