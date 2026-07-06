---
id: TASK-MP-013
title: "WorktreeGitRunner: --force re-attach can silently advance the handoff branch under an operator's live checkout"
status: backlog
created: 2026-07-06T22:30:00Z
updated: 2026-07-06T22:30:00Z
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

- [ ] Detect the collision before writing: if the handoff branch is checked
      out in ANY existing worktree of the target repo (`git worktree list
      --porcelain` / `git branch --show-current` in each), refuse the
      `--force` re-attach and fail the handoff loudly (never-raises contract
      preserved: HandoffResult failure + ERROR log, run stays recoverable).
- [ ] The refusal is a distinct, greppable event naming the branch and the
      blocking worktree path, so the operator knows to release the checkout.
- [ ] Idempotency probe unchanged: byte-identical re-handoff still returns
      the existing tip without touching the branch.
- [ ] Regression test reproducing the review's scenario: target repo with the
      handoff branch checked out in a second worktree → handoff refuses, the
      operator's worktree is byte-identical before/after (no phantom staged
      modification).
- [ ] Existing planning_runner tests stay green.

## Evidence / references

- Pre-commit review: `docs/reviews/task-mp-012-jnb-109-pre-commit-review-2026-07-06.md`
  (fresh-defects finding #1, empirically reproduced).
- RT-08 idempotency + path-containment contracts already in
  `planning_runner.py` — extend, don't weaken.
