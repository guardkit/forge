---
id: TASK-MP-013
title: "WorktreeGitRunner: --force re-attach can silently advance the handoff branch under an operator's live checkout"
status: completed
created: 2026-07-06T22:30:00Z
updated: 2026-07-06T23:30:00Z
completed: 2026-07-06T23:30:00Z
completed_location: tasks/completed/TASK-MP-013/
previous_state: in_review
state_transition_reason: "Completed via /task-complete; all quality gates passed"
intensity: minimal
quality_gates:
  tests: "9/9 planning_runner, 400/400 adapters suite"
  lint: "ruff clean, black clean"
  coverage: "skipped (minimal intensity)"
priority: high
task_type: implementation
feature_id: FEAT-SPL-002
repo: forge
implementation_mode: task-work
complexity: 3
dependencies: []
tags: [mode-p, handoff, gitrunner, found-2026-07-06, pre-commit-review]
---

# Task: WorktreeGitRunner --force re-attach vs a live operator checkout

## Defect (MEDIUM, empirically reproduced in the 2026-07-06 pre-commit review)

`src/forge/adapters/git/planning_runner.py` uses `git worktree add --force`
to (re-)attach the `planning/{cid}` branch. If a human operator has the
target repo checked out ON that branch (or a worktree already attached to
it), the `--force` re-attach lets the handoff commit **silently advance the
branch under the operator's live checkout** — reproduced as a phantom staged
modification appearing in the operator's working tree. For a 2-person fleet
where the target repos are actively worked (study-tutor, fleet-memory), the
first real PLANNED-HANDOFF into a repo a human also has open can corrupt
their in-progress state.

Land before the first real Mode P handoff into a human-worked repo
(TASK-MP-010 uses a toy target, so it does not gate MP-010).

## Acceptance criteria

- [x] Detect the collision before writing: if the handoff branch is checked
      out in ANY existing worktree of the target repo (`git worktree list
      --porcelain` / `git branch --show-current` in each), refuse the
      `--force` re-attach and fail the handoff loudly (never-raises contract
      preserved: HandoffResult failure + ERROR log, run stays recoverable).
- [x] The refusal is a distinct, greppable event naming the branch and the
      blocking worktree path, so the operator knows to release the checkout.
- [x] Idempotency probe unchanged: byte-identical re-handoff still returns
      the existing tip without touching the branch.
- [x] Regression test reproducing the review's scenario: target repo with the
      handoff branch checked out in a second worktree → handoff refuses, the
      operator's worktree is byte-identical before/after (no phantom staged
      modification).
- [x] Existing planning_runner tests stay green.

## Evidence / references

- Pre-commit review: `docs/reviews/task-mp-012-jnb-109-pre-commit-review-2026-07-06.md`
  (fresh-defects finding #1, empirically reproduced).
- RT-08 idempotency + path-containment contracts already in
  `planning_runner.py` — extend, don't weaken.

## Implementation Summary

New probe `WorktreeGitRunner._blocking_checkout(repo, branch)` parses
`git worktree list --porcelain` (blank-line-separated entries: `worktree
<path>`, `branch refs/heads/<name>`, `prunable`). A live worktree (directory
exists on disk, not marked prunable) holding the handoff branch blocks the
`--force` re-attach; orphaned/prunable leftovers from a crashed prior attempt
do not block (they are the reason `--force` exists). The guard runs strictly
AFTER the RT-08 idempotency probe (byte-identical re-handoff stays
unblocked — zero mutations) and BEFORE any git mutation. On collision the
runner logs at ERROR and returns a failed `GitOpResult` whose stderr carries
the greppable marker `handoff-branch-checked-out` plus the branch name and
blocking worktree path. If the porcelain probe itself fails, the guard fails
safe: refuse with an `<unverifiable: ...>` message rather than mutate an
unverifiable branch. Never-raises contract (ADR-ARCH-025) preserved.

Regression tests (`TestCheckoutCollisionGuard`, real git repos in tmp_path):
- `test_rehandoff_refused_when_branch_checked_out_elsewhere` — reproduces the
  review scenario; asserts failed result, marker + branch + path in stderr,
  branch tip SHA unchanged, operator worktree byte-identical with clean
  `git status --porcelain` (no phantom staged modification).
- `test_idempotent_rehandoff_succeeds_despite_checkout` — RT-08 path still
  returns the existing tip while the branch is checked out elsewhere.

Result: 9/9 planning_runner tests, 400/400 full adapters suite, ruff + black
clean.

## Notes

Lesson: `git worktree list --porcelain` also reports the main working copy
as a worktree entry, so a single parse covers both the "operator's primary
checkout" and "second worktree" collision cases; prunable/missing-directory
entries must be skipped or the guard would block the crash-recovery path
`--force` was added for.
