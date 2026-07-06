---
complexity: 6
dependencies:
- TASK-MP-001
- TASK-MP-002
estimated_minutes: 80
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
id: TASK-MP-006
implementation_mode: task-work
parent_review: TASK-REV-83E4
status: design_approved
tags:
- mode-p
- handoff
- terminal
task_type: feature
title: PLANNED-HANDOFF terminal + registry (idempotent, GitRunner-injected, sanitised
  notification)
wave: 2
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