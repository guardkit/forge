---
id: TASK-FRR-PEB-FM-002
title: Fix prose phantom-path typo and scrub autobuild_state from TASK-FRR-PEB-003
status: completed
created: 2026-05-07T00:00:00Z
updated: 2026-05-07T12:30:00Z
completed: 2026-05-07T12:30:00Z
completed_location: tasks/completed/TASK-FRR-PEB-FM-002/
priority: high
priority_band: P0
task_type: feature
parent_review: TASK-REV-PEBR-002
parent_review_repo: forge
review_report: docs/reviews/FEAT-PEBR-failed-run-2-analysis.md
parent_feature: FEAT-PEBR
feature_id: FEAT-PEBR
related_tasks:
  - TASK-FRR-PEB-003
  - TASK-FRR-PEB-FM-001
  - TASK-REV-PEBR-002
implementation_mode: direct
wave: 0
complexity: 1
estimated_minutes: 15
dependencies: []
tags:
  - forge-task-prose
  - forge-task-frontmatter
  - autobuild
  - feat-pebr
  - unblock
  - P0
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Fix prose phantom-path typo and scrub autobuild_state from TASK-FRR-PEB-003

## Description

The FEAT-PEBR autobuild run-2 failed with `MAX_TURNS_EXCEEDED` on
TASK-FRR-PEB-003. Five identical Coach feedback turns flagged a phantom
file `src/forge/dispatch/autobuild_async.py` as missing. The full
trace is in
[docs/reviews/FEAT-PEBR-failed-run-2-analysis.md](../../../docs/reviews/FEAT-PEBR-failed-run-2-analysis.md).

Two issues need fixing in the source-of-truth task file
[`TASK-FRR-PEB-003-sse-to-envelope-translation.md`](./TASK-FRR-PEB-003-sse-to-envelope-translation.md):

1. **Line 205** contains a prose `Reference:` bullet with a typo path
   `src/forge/dispatch/autobuild_async.py`. The real file is
   `src/forge/pipeline/dispatchers/autobuild_async.py`. The bullet is
   informational ("LifecycleEmitterAdapter does the analogous in-process
   mapping"). The implementation is complete; the cross-reference is
   no longer load-bearing. The qualified-path typo is what the AC-fallback
   scanner picks up (Bug A in the rev-2 review).
2. **Frontmatter lines 30-97** contain a stale `autobuild_state.turns`
   block with 5 persisted Coach feedback strings, each containing the
   phantom path. The orchestrator overwrites this on each run, but a
   clean start avoids stale phantom-path text leaking into the
   Player's first prompt before the overwrite.

This task is a **single forge commit** with two file edits in one
file. No code, no tests. It is the minimum-unblock workaround per
the rev-2 review's AC-8 finding.

## Acceptance Criteria

- [ ] **AC-1 — Prose bullet removed.** Lines 205-208 of
  TASK-FRR-PEB-003 (the `## Implementation notes` bullet starting
  with `- Reference: `src/forge/dispatch/autobuild_async.py``) are
  deleted in their entirety. Verify with
  `grep -n "src/forge/dispatch/" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md`
  → expected: no matches.
- [ ] **AC-2 — Cross-task audit.** Run
  `grep -nR "src/forge/dispatch/" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/`
  → expected: no matches across any FRR-PEB task. (If any other task
  has the same typo, fix it too with a follow-up bullet in this PR.)
- [ ] **AC-3 — Frontmatter scrub.** The `autobuild_state` key in
  TASK-FRR-PEB-003's YAML frontmatter is either removed entirely or
  set to `autobuild_state: {}`. The 5 turn records (lines 30-97) are
  no longer in the file.
- [ ] **AC-4 — Status reset.** Frontmatter `status:` field on
  TASK-FRR-PEB-003 is set back to `backlog` (currently `blocked`).
  This signals to autobuild's `state_bridge` that the task is ready
  to be re-attempted.
- [ ] **AC-5 — File integrity.** The body of TASK-FRR-PEB-003
  (everything after the second `---`) is unchanged except for the
  removed bullet. The `## Files to Create`, `## Files to Modify`,
  `## Acceptance criteria`, `## §4 Integration Contract`, and other
  sections are byte-identical post-edit.
- [ ] **AC-6 — Frontmatter validity.** The resulting file parses as
  valid YAML in the frontmatter. Verify with
  `python -c "import yaml; yaml.safe_load(open('tasks/.../TASK-FRR-PEB-003-...md').read().split('---')[1])"`
  → expected: no exception.

## Out of Scope

- Modifying any FRR-PEB task other than -003 (unless AC-2 turns up
  a duplicate typo).
- Re-running autobuild. That is gated on the guardkit-side fixes
  (TASK-GK-CV-001, TASK-GK-PA-002, TASK-GK-COACH-001) per the
  recommended set in the rev-2 review.
- Touching the worktree at `.guardkit/worktrees/FEAT-PEBR/` —
  preserve as-is for run-3 verification.
- Adding any code or test files.

## Files to Modify

- `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md`

## Implementation notes

- This is `implementation_mode: direct` — no Player/Coach loop
  needed. A single commit suffices.
- The autobuild_state block in YAML is what makes the `Read`/`Edit`
  more annoying than it should be. Suggested approach:
  1. Read the whole file.
  2. Parse the YAML between the first two `---` markers.
  3. Delete the `autobuild_state` key (or set to `{}`).
  4. Set `status: backlog`.
  5. Re-emit the YAML and the body.
- Or, simpler: use `Edit` to delete the `autobuild_state:` block by
  finding the unique start (`autobuild_state:`) and end (the line
  immediately before `---` closing the frontmatter), then a second
  `Edit` to replace `status: blocked` → `status: backlog`, then a
  third `Edit` to remove the `Reference:` bullet on line 205.
- Verify after each edit:
  `head -10 TASK-FRR-PEB-003-sse-to-envelope-translation.md` to
  confirm frontmatter still starts with `---` and is well-formed.

## Test requirements

No automated tests. Manual verification:

1. `grep -n "src/forge/dispatch/" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md`
   → 0 matches.
2. `grep -n "autobuild_state" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md`
   → 0 matches OR a single line `autobuild_state: {}`.
3. `grep -n "^status:" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md`
   → `status: backlog`.
4. Python YAML parse check (AC-6).

## Coach validation commands

```bash
grep -nc "src/forge/dispatch/" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md   # expect 0
grep -nc "autobuild_state" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md      # expect 0 or 1
python3 -c "import yaml; print(yaml.safe_load(open('tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md').read().split('---')[1])['status'])"  # expect: backlog
```
