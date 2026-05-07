---
id: TASK-FRR-PEB-FM-001
title: Add explicit Files-to-Create/Modify sections and reclassify task_type for FRR-PEB tasks
status: backlog
created: 2026-05-07 00:00:00+00:00
updated: 2026-05-07 00:00:00+00:00
priority: high
priority_band: P1
task_type: docs
parent_review: TASK-REV-PEBR-001
parent_feature: FEAT-PEBR
review_report: docs/reviews/FEAT-PEBR-failed-run-1-analysis.md
implementation_mode: direct
wave: 1
complexity: 3
estimated_minutes: 60
dependencies: []
tags:
  - autobuild
  - frontmatter
  - plan-audit-workaround
  - failure-recovery
  - P1
test_results:
  status: not_applicable
  coverage: null
  last_run: null
---

# Task: Add explicit Files-to-Create/Modify sections and reclassify task_type for FRR-PEB tasks

## Description

This task is the **forge-side workaround** for the FEAT-PEBR Wave-1
unrecoverable stall (see [TASK-REV-PEBR-001 review report](../../../docs/reviews/FEAT-PEBR-failed-run-1-analysis.md)).
The GuardKit-side fixes
([TASK-GK-AC-001](../../../../guardkit/tasks/backlog/autobuild-feat-pebr-failure-recovery/TASK-GK-AC-001-dont-flag-bare-basenames-in-ac-scanner.md)
and friends in the **guardkit** repo) address the root cause; this
task gives FEAT-PEBR an immediate workaround that does not depend on
any GuardKit code change.

By adding explicit `## Files to Create` and `## Files to Modify`
sections to each FRR-PEB task body, GuardKit's `PlanAuditor` consumes
those sections directly via `plan_markdown_parser.py` and never falls
through to the buggy `_scan_ac_for_missing_paths` path. The pre-loop
that would normally write these sections was disabled for FEAT-PEBR
(`enable_pre_loop=False`), which is why the stub plan was empty.

Reclassifying `task_type` corrects the secondary issue identified in
the review (AC-4): TASK-FRR-PEB-001 is feature-shaped work
(introduces a new module + new test package), not pure refactor, so
the `refactor` quality-gate profile is the wrong fit.

## Acceptance Criteria

- [ ] AC-1: TASK-FRR-PEB-001 gains explicit `## Files to Create` and
  `## Files to Modify` sections in its body, listing exactly:
  - **Create**:
    - `src/forge/pipeline/build_ack_handle.py`
    - `tests/forge/adapters/nats/__init__.py`
    - `tests/forge/adapters/nats/test_pipeline_consumer.py`
  - **Modify**:
    - `src/forge/adapters/nats/pipeline_consumer.py`
    - `src/forge/cli/_serve_deps.py`
- [ ] AC-2: TASK-FRR-PEB-001 frontmatter `task_type` is changed from
  `refactor` to `feature`. `documentation_level: standard` is added
  explicitly (currently implicit `minimal`).
- [ ] AC-3: TASK-FRR-PEB-002 through TASK-FRR-PEB-014 each gain
  appropriate `## Files to Create` / `## Files to Modify` sections.
  Use the existing "Touchpoints" line in each task's Implementation
  Notes as the source of truth — verify each path against the actual
  worktree (`.guardkit/worktrees/FEAT-PEBR/`) and reclassify
  create-vs-modify based on whether the file exists on `main`.
- [ ] AC-4: Reclassify `task_type` per the review's AC-7 table:
  - `feature`: 001, 002, 003, 004, 008, 009, 014
  - `refactor`: 005, 006, 007, 010, 011, 012
  - `integration-test`: 013
- [ ] AC-5: All FRR-PEB tasks gain `documentation_level: standard`
  in frontmatter (currently implicit `minimal`).
- [ ] AC-6: After the changes, running `guardkit autobuild feature
  FEAT-PEBR --resume` against the preserved worktree converges on
  Wave-1 in 1 turn (verifies the workaround). This AC can be
  deferred until at least one GuardKit P0 fix lands too —
  documented as a verification step rather than a blocking gate.
- [ ] AC-7: The 14 modified task files pass any project-side
  frontmatter linting (e.g. `tasks/templates/` schema check) with
  zero errors.

## Test requirements

Not applicable — this is a docs-only frontmatter change. Verification
is via:
1. Reading the changed task files and confirming the lists match
   the actual worktree state.
2. (Optional, if a GuardKit P0 fix is also in) running the
   `--resume` and observing convergence.

## Implementation notes

### Files to Modify

- [tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-001-defer-build-queued-ack-to-terminal.md](TASK-FRR-PEB-001-defer-build-queued-ack-to-terminal.md)
  through `TASK-FRR-PEB-014-assum-009-contract-lock-test.md` (14 files)
- [.guardkit/features/FEAT-PEBR.yaml](../../../.guardkit/features/FEAT-PEBR.yaml)
  — if `task_type` is mirrored in the feature manifest, update there
  too; otherwise leave alone.

### Section format

GuardKit's `plan_markdown_parser` expects exactly:

```markdown
## Files to Create

- `src/forge/pipeline/build_ack_handle.py`
- `tests/forge/adapters/nats/__init__.py`
- `tests/forge/adapters/nats/test_pipeline_consumer.py`

## Files to Modify

- `src/forge/adapters/nats/pipeline_consumer.py`
- `src/forge/cli/_serve_deps.py`
```

The parser regex is
`r'## Files to Create\s*\n(.*?)(?=\n##|\Z)'` — exact heading text
matters. Backticks around paths are optional but help readability.

### Source-of-truth for the file lists

For TASK-FRR-PEB-001, the file lists in AC-1 above are derived from:
- The worktree's actual file changes
  (`.guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-001/player_turn_3.json:4-43` —
  `files_modified` and `files_created` arrays, filtered to source-tree
  paths only).
- The Implementation Notes "Touchpoints" line in the task body
  (lines 131-134 of the original task file).

For TASK-FRR-PEB-002 through 014, the worktree only contains
TASK-FRR-PEB-001's actual changes (the others never ran), so the
source-of-truth is each task's "Touchpoints" line plus a
sanity-check that the named modify-targets actually exist on `main`.

### Why this works without GuardKit changes

`PlanAuditor` flow (per review's [Sequence — failure path](../../../docs/reviews/FEAT-PEBR-failed-run-1-analysis.md#sequence--the-failure-path-turn-1)):

1. Parse the task's implementation plan markdown.
2. If `## Files to Create` / `## Files to Modify` sections are
   present, build `planned_files` from them.
3. If sections are absent → `result.skipped = True` →
   AgentInvoker's `_scan_ac_for_missing_paths` AC-fallback fires
   (the bug).

Adding the sections takes the parser to step 2 and skips step 3
entirely. Even with the bug present in GuardKit, the audit produces
a clean comparison against the explicit lists.

### task_type reclassification rationale

| Task | New task_type | Reason |
|------|---------------|--------|
| 001 | `feature` | Adds new module `build_ack_handle.py` and new test package |
| 002 | `feature` | New bridge skeleton + SQLite registry module |
| 003 | `feature` | New SSE→envelope translator module |
| 004 | `feature` | New wire-up code in serve.py |
| 005 | `refactor` | F010F coexistence boundary — pure refactor |
| 006 | `refactor` | Pause-resume canonicalisation — refactor |
| 007 | `refactor` | Cancel emit ownership — refactor |
| 008 | `feature` | New backoff + deadline logic |
| 009 | `feature` | New restart-replay + sweep |
| 010 | `refactor` | Diagnostic enhancement — refactor |
| 011 | `refactor` | Non-regression test guard — refactor |
| 012 | `refactor` | Status surface enhancement — refactor |
| 013 | `integration-test` | E2E sidecar test — distinct profile |
| 014 | `feature` | New contract-lock test (small but new) |

### Implementation mode

`direct` — no full task-work loop. The change is mechanical
frontmatter + section additions across 14 files. A diff review
against the existing Touchpoints lines is the verification.

## Out of scope

- Fixing `_scan_ac_for_missing_paths` itself (TASK-GK-AC-001 in
  guardkit).
- Fixing the Coach gate-fail short-circuit (TASK-GK-CR-001 in
  guardkit).
- Modifying the existing FRR-PEB-001 implementation in the worktree —
  the implementation is correct as-is; this task only edits the
  task definition files.
- Re-running autobuild — that's the verification step after at
  least one GuardKit P0 fix also lands.

## Verification

After this task lands AND at least one GuardKit P0 fix is in:

```bash
cd /home/richardwoollcott/Projects/appmilla_github/forge
guardkit autobuild feature FEAT-PEBR --resume
# Expect:
#   Wave 1 / TASK-FRR-PEB-001 → APPROVED on turn 1
#   plan_audit_passed=True (audit consumes explicit Files-to-Create
#                           list, no AC-fallback)
#   criteria_met=6 (Player's existing implementation reported all 6 ACs)
#   decision=approve
```

If only this task lands (no GuardKit fix yet):

- The audit will still produce a clean verdict (sections present →
  no AC-fallback).
- The Coach short-circuit bug (TASK-GK-CR-001's territory) won't
  fire because gates pass — `_validate_requirements` runs normally.
- FEAT-PEBR resumes successfully purely on the back of this
  workaround. This is the **fastest unblock path**.
