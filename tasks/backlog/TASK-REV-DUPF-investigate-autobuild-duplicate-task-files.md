---
id: TASK-REV-DUPF
title: "Investigate autobuild duplicate task files"
task_type: review
status: backlog
priority: normal
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
complexity: 4
tags: [review, investigation, guardkit, autobuild, task-files, hygiene]
related_features: [FEAT-FORGE-002, FEAT-FORGE-003, FEAT-FORGE-004]
related_commits:
  - 91f4de5  # FEAT-FORGE-002 merge
  - f63bcf5  # FEAT-FORGE-003 merge
  - 9774351  # FEAT-FORGE-004 merge
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Investigate autobuild duplicate task files

## Description

Each `guardkit autobuild feature` run that has been merged into `main` so far
(FEAT-FORGE-002, FEAT-FORGE-003, FEAT-FORGE-004) has produced a second copy
of every task file in flat directories alongside the authoritative copies that
live in `tasks/backlog/<feature-slug>/`. The duplicates have **divergent
content** (different YAML frontmatter), are committed by the autobuild branch,
and persist in `main` after merge.

This is a quiet bug — nothing breaks immediately — but it pollutes the task
inventory, splits any future task-status tooling across two paths, and the
divergence means it is no longer obvious which file is authoritative.

## Observed Pattern

### Duplicate locations

For every feature, the autobuild branch creates one of:

- `tasks/backlog/TASK-XXX-*.md` (flat)
- `tasks/design_approved/TASK-XXX-*.md` (flat)

…in addition to the authoritative copy at:

- `tasks/backlog/<feature-slug>/TASK-XXX-*.md` (the file `feature-plan` produced)

### Distribution rule (empirical)

Looking at FEAT-FORGE-002/003/004, the split appears to be:

- **Wave 1 tasks + BDD/wiring tasks** → `tasks/backlog/` (flat)
- **All other tasks** → `tasks/design_approved/` (flat)

| Feature | Authoritative dir | `backlog/` flat | `design_approved/` flat |
|---|---|---|---|
| FEAT-FORGE-002 (NFI) | `tasks/backlog/nats-fleet-integration/` | NFI-001, NFI-002 | NFI-003 … NFI-011 |
| FEAT-FORGE-003 (SAD) | `tasks/backlog/specialist-agent-delegation/` | SAD-001 | SAD-002 … SAD-012 |
| FEAT-FORGE-004 (CGCP) | `tasks/backlog/confidence-gated-checkpoint-protocol/` | CGCP-001, 002, 003, 012 | CGCP-004 … CGCP-011 |

This roughly correlates with `status: in_progress` (wave 1 / BDD-wiring tasks
that the orchestrator hadn't yet flipped to design-approved) vs.
`status: design_approved` (middle waves the orchestrator had advanced).
Worth confirming.

### Content divergence

A spot diff of the SAD-002 pair shows the duplicates are **not** byte-identical
copies:

```
< id: TASK-SAD-002                               (in subdir copy only)
< title: Resolution-record persistence + ...     (in subdir copy only)
< task_type: feature                             (in subdir copy only)
< status: in_review                              (in subdir copy only)
< priority: high                                 (in subdir copy only)
< updated: 2026-04-25 00:00:00+00:00             (in subdir copy only)
< parent_review: TASK-REV-SAD3                   (in subdir copy only)
< feature_id: FEAT-FORGE-003                     (in subdir copy only)
> complexity: 5                                  (top-level in flat copy, different position in subdir)
> consumer_context: ...                          (different shape in flat copy)
```

Implication: the autobuild orchestrator is reading/writing a **different
representation** of the task than what `feature-plan` originally wrote. It's
not just a `cp` — it's a regenerated/rewritten copy.

## Investigation Scope

The investigation should answer:

1. **Which guardkit code path creates the flat-directory copies?**
   - Suspect: a step in the autobuild orchestrator (Phase 2 design-approval
     transition? wave kickoff?) that writes a normalized task file using a
     different schema and forgets to honor the `<feature-slug>/` subdir.
   - Check: `guardkit.orchestrator.*` writers, anywhere a task file is `Write`
     or `move`d during autobuild.

2. **Why the schema diverges.**
   - Are the flat copies a different model serialization (e.g.,
     pydantic `.model_dump()` of an internal task representation that drops
     fields like `id`, `title`, `parent_review`)?
   - Or is there a second `feature-plan`-like writer inside autobuild that
     uses a stripped-down template?

3. **Whether the flat copies serve any runtime purpose.**
   - Does autobuild *read back* the flat copies during later waves or
     review-summary generation? (If yes, removing them naively breaks the
     orchestrator.)
   - Check by: grepping guardkit for reads against `tasks/backlog/TASK-*` and
     `tasks/design_approved/TASK-*` (as opposed to subdir paths).

4. **Whether the orchestrator should be writing back to the subdir.**
   - Likely the right fix: when promoting a task through statuses, update the
     existing subdir file rather than spawn a flat duplicate.

5. **How to clean up the existing duplicates** without losing useful content.
   - The flat copies may carry `consumer_context` blocks the subdir copies
     don't have — needs reconciliation, not blind deletion.

## Acceptance Criteria

- [ ] Root cause identified: specific guardkit module + function that emits the
      flat-directory copies, with file:line reference.
- [ ] Schema diff explained: why the duplicate has different frontmatter (which
      writer / which model).
- [ ] Read-side audit: confirmed list of guardkit call sites that read tasks
      from flat `tasks/backlog/TASK-*` or `tasks/design_approved/TASK-*`
      paths (vs. subdir paths). Determines whether flat copies are load-bearing.
- [ ] Recommendation document produced (in this task's body or a sibling
      `.claude/reviews/TASK-REV-DUPF-review-report.md`) covering:
  - Whether to fix forward (patch guardkit to write to subdir) or accept the
    flat layout as canonical.
  - Whether the divergent fields in the flat copies should be merged back into
    the subdir copies before deletion.
  - Cleanup batch plan for FEAT-FORGE-002/003/004 (and any in-flight
    FEAT-FORGE-005 already showing the same pattern).
- [ ] Follow-up implementation task(s) created in `tasks/backlog/` (one for
      the guardkit fix, one for the cleanup batch — or a single task if the
      reviewer judges them inseparable).

## Out of Scope

- Actually fixing the guardkit code (becomes a follow-up implementation task).
- Cleaning up the existing duplicates in `main` (becomes a follow-up cleanup
  task). This task is investigation-only.
- Other autobuild artefact issues (e.g. the `TASK-XXX/checkpoints.json` that
  gets left behind in the worktree for the last task of the last wave — that
  is a separate, unrelated bug observed during cleanup of FEAT-FORGE-003 and
  FEAT-FORGE-004).

## Evidence / Reproduction

The clearest reproduction is to inspect the merge commits:

```bash
# FEAT-FORGE-003 merge
git show --stat f63bcf5 | grep -E '^\s+create mode .* tasks/(backlog|design_approved)/TASK-SAD-'

# FEAT-FORGE-004 merge
git show --stat 9774351 | grep -E '^\s+create mode .* tasks/(backlog|design_approved)/TASK-CGCP-'

# Compare subdir vs flat content for one task
diff tasks/backlog/specialist-agent-delegation/TASK-SAD-002-resolution-record-persistence.md \
     tasks/design_approved/TASK-SAD-002-resolution-record-persistence.md
```

## Implementation Notes

(Reviewer: leave findings, code citations, and recommendation here, or move to
`.claude/reviews/TASK-REV-DUPF-review-report.md` if it grows large.)

## Test Execution Log

(Populated by review tooling if applicable — for an investigation task this
is likely empty.)
