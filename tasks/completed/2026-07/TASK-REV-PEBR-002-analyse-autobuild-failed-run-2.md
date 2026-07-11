---
id: TASK-REV-PEBR-002
title: Analyse FEAT-PEBR autobuild failed-run-2 plan-audit phantom-file stall
status: review_complete
created: 2026-05-07T00:00:00Z
updated: 2026-05-07T00:00:00Z
priority: high
task_type: review
review_mode: failure-analysis
review_depth: comprehensive
decision_required: true
review_results:
  mode: failure-analysis
  depth: comprehensive
  revision: 2
  decision: implement
  decision_recommendation: implement
  acs_satisfied: 8
  acs_total: 8
  report_path: docs/reviews/FEAT-PEBR-failed-run-2-analysis.md
  resume_recommendation: block-then-resume
  fastest_workaround: TASK-FRR-PEB-FM-002
  bugs_identified:
    bug_a:
      name: AC-fallback scanner ingests qualified prose paths
      location: guardkit/orchestrator/agent_invoker.py:6054-6228
      fix_task: TASK-GK-PA-002
      fix_repo: guardkit
    bug_b:
      name: _strip_criterion_prefix strips AC ID before _extract_ac_id extracts it
      location: guardkit/orchestrator/quality_gates/coach_validator.py:3243-3246
      fix_task: TASK-GK-CV-001
      fix_repo: guardkit
    bug_c:
      name: Stall extender uniformity check straddles 0 → N transition
      location: guardkit/orchestrator/autobuild.py:3935-4022
      fix_task: TASK-GK-COACH-001
      fix_repo: guardkit
  spawned_tasks:
    forge_repo:
      feature_folder: tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/
      tasks:
        - TASK-FRR-PEB-FM-002
    guardkit_repo:
      feature_folder: tasks/backlog/autobuild-feat-pebr-failure-recovery-rev2/
      tasks:
        - TASK-GK-CV-001
        - TASK-GK-PA-002
        - TASK-GK-COACH-001
  unblocking_followups:
    - TASK-FRR-PEB-FM-002
  recommended_set:
    - TASK-FRR-PEB-FM-002
    - TASK-GK-CV-001
    - TASK-GK-PA-002
    - TASK-GK-COACH-001
  primary_root_cause:
    location: guardkit/orchestrator/quality_gates/coach_validator.py:3243-3246
    function: _strip_criterion_prefix
    bug: regex strips ^AC-\d+:\s* before _extract_ac_id can extract it; Coach falls back to f"AC-{i+1:03d}" lookup keys; Players using natural-label criterion_id="AC-N" fail to match
  diff_against_rev1:
    landed_and_works:
      - TASK-GK-AC-001
      - TASK-GK-CR-001
      - TASK-FRR-PEB-FM-001
    not_yet_exercised:
      - TASK-GK-PA-001
    side_effects_surfaced:
      - bug_b_was_hidden_behind_gk_cr_001_zero
      - bug_c_was_hidden_behind_run_1_uniform_zero_count
    withdrawn_or_replaced:
      - reset_turn_exemption_replaced_by_TASK-GK-COACH-001
parent_feature: FEAT-PEBR
related_tasks:
  - TASK-FRR-PEB-003
  - TASK-REV-PEBR-001
  - TASK-FRR-PEB-FM-001
complexity: 5
estimated_minutes: 90
dependencies: []
tags:
  - review
  - autobuild
  - failure-analysis
  - quality-gates
  - plan-audit
  - phantom-file
  - max-turns-exceeded
  - investigation
test_results:
  status: not_applicable
  coverage: null
  last_run: null
artefacts:
  log: docs/history/autobuild-FEAT-PEBR-failed-run-2.md
  review_summary: .guardkit/autobuild/FEAT-PEBR/review-summary.md
  worktree: .guardkit/worktrees/FEAT-PEBR
  branch: autobuild/FEAT-PEBR
  failed_task: TASK-FRR-PEB-003
  failed_task_file: tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-003-sse-to-envelope-translation.md
  player_reports:
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/player_turn_1.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/player_turn_2.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/player_turn_3.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/player_turn_4.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/player_turn_5.json
  coach_decisions:
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/coach_turn_1.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/coach_turn_2.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/coach_turn_3.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/coach_turn_4.json
    - .guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/coach_turn_5.json
  implementation_plan_stub: .guardkit/worktrees/FEAT-PEBR/.claude/task-plans/TASK-FRR-PEB-003-implementation-plan.md
---

# Task: Analyse FEAT-PEBR autobuild failed-run-2 plan-audit phantom-file stall

## Description

The second autobuild run of FEAT-PEBR (`Forge autobuild_runner pipeline-emitter
bridge`) terminated with `FEATURE RESULT: FAILED` after Wave 3, when
TASK-FRR-PEB-003 hit `MAX_TURNS_EXCEEDED` (5/5 turns). The full transcript is at
[docs/history/autobuild-FEAT-PEBR-failed-run-2.md](../../../docs/history/autobuild-FEAT-PEBR-failed-run-2.md).

This is a **review/analysis task**. It produces a written diagnosis and a
recommended remediation plan, not implementation code. Implementation tasks
will be spawned separately based on the conclusions.

## Observed Failure Signature

From the log and the persisted coach decisions
(`coach_turn_{1..5}.json` for TASK-FRR-PEB-003):

- Across all 5 turns the same identical Coach feedback is emitted:
  - Advisory (non-blocking): `task-work produced a report with 2 of 3 expected
    agent invocations. Missing phases: 3 (Implementation)`.
  - **Must-fix**: `Plan audit detected high-severity discrepancies — 1
    missing file(s): src/forge/dispatch/autobuild_async.py`.
- Quality-gate evaluation each turn:
  `tests=True, coverage=True, arch=True, plan_audit=False (required=True),
  ALL_PASSED=False`. Plan-audit is the **only** failing gate.
- Per `coach_turn_5.json`, the Coach reports **7/7 acceptance criteria
  verified** and `validation_results.requirements.all_criteria_met=true`,
  yet the run still fails because `plan_audit_passed=false` and
  `plan_audit_required=true`.
- The Player's task-work report records `Files planned: 0, Files actual: 0`
  on every turn — the implementation plan is the auto-generated stub at
  `.claude/task-plans/TASK-FRR-PEB-003-implementation-plan.md` (16 lines, no
  Files-to-Create / Files-to-Modify section), so the Player parsed no plan
  files. Coach's plan-audit nonetheless produced a *missing file* claim.
- The phantom path `src/forge/dispatch/autobuild_async.py` originates from a
  **prose reference** in the task body's `## Implementation notes` section
  (line 205): "Reference: `src/forge/dispatch/autobuild_async.py`'s existing
  `LifecycleEmitterAdapter` does the analogous in-process mapping…". This
  file already exists in the repo and is **not** a deliverable of this task.
- The Coach feedback from turn 1 was then persisted back into the task
  frontmatter (`autobuild_state.turns[*].feedback`), so on every subsequent
  turn the task body itself contained 5 more occurrences of
  `src/forge/dispatch/autobuild_async.py` — potentially compounding the
  scanner's false-positive surface area.
- The previous review (TASK-REV-PEBR-001) spawned remediations
  TASK-GK-PA-001 (plan-audit accuracy) and TASK-FRR-PEB-FM-001 (add explicit
  `## Files to Create` / `## Files to Modify` sections to FRR-PEB tasks).
  Commit `02aac9c` confirms FM-001 landed (lines 187–198 of TASK-FRR-PEB-003
  do declare exactly the 5 created + 2 modified files the Player produced).
  Despite the explicit lists, plan-audit still treated the prose reference
  on line 205 as a planned deliverable. **The remediation did not close the
  scanner gap.**

## Run-2 Outcome Snapshot

| Wave | Tasks | Status | Notes |
|------|-------|--------|-------|
| 1 | TASK-FRR-PEB-001 | ✓ PASS (2 turns) | Approved |
| 2 | TASK-FRR-PEB-002 | ✓ PASS (2 turns) | Approved |
| 3 | TASK-FRR-PEB-003, TASK-FRR-PEB-010 | ✗ FAIL | -010 approved (2 turns); -003 max_turns_exceeded (5 turns) |

`stop_on_failure=True` halted the run; 11 of 14 tasks (waves 4–8) never
executed. Total duration 82m 29s; only 3/14 tasks completed.

## Acceptance Criteria

This task is complete when the review document below answers, with citations
to specific log lines / report paths / source-file lines, **all** of the
following:

- [x] **AC-1 — Root cause of the phantom-file plan-audit violation.**
  Trace the code path that produces `missing_files:
  ['src/forge/dispatch/autobuild_async.py']` for TASK-FRR-PEB-003. Inspect
  the plan-audit implementation in `guardkit/orchestrator/` (likely the
  scanner from TASK-GK-PA-001 / `_scan_*_for_missing_paths` family) and
  identify the exact extraction step that ingests prose paths from the task
  body (line 205) instead of the explicit `## Files to Create` / `## Files
  to Modify` sections (lines 187–198). Determine whether the scanner reads:
  (a) the implementation plan only,
  (b) the task markdown as a whole,
  (c) the union of (a) + (b),
  (d) something else (Player report `completion_promises` evidence paths,
       autobuild_state turn feedback, etc.).
- [x] **AC-2 — Why the explicit Files-to-Create/Modify sections did not
  override the prose scan.** TASK-FRR-PEB-FM-001 added an explicit declared
  set; explain why the audit still merges in prose paths. Recommend whether
  the explicit section should be **authoritative** (prose ignored when
  present) or **additive but filtered** (prose paths checked against
  explicit allow/deny lists, against existing-file presence, or against
  fenced-code-only inclusion).
- [x] **AC-3 — Self-amplifying feedback loop in the task frontmatter.**
  The Coach feedback for turn N is persisted into
  `autobuild_state.turns[N].feedback` inside the task markdown itself.
  Determine whether a subsequent turn's plan-audit then re-scans the task
  body and re-ingests the same phantom path from the persisted feedback,
  and quantify the impact (does this turn one phantom path into many?).
  Reference task file lines 44, 56, 68, 80, 92.
- [x] **AC-4 — Why max_turns_exceeded fires when 7/7 ACs are verified.**
  In `coach_turn_5.json`, `validation_results.requirements.all_criteria_met`
  is `true` and every `criteria_verification[*].result` is `verified`, yet
  the decision is `feedback` and the run hits the 5-turn ceiling. Determine
  the override rule (likely
  `quality_gates.plan_audit_required=True` short-circuits the criteria
  verdict) and recommend whether plan-audit should be **demoted to
  advisory** when ACs are 100% satisfied, or kept as a hard gate with a
  more accurate scanner. Cite
  `guardkit.orchestrator.quality_gates.coach_validator` log lines.
- [x] **AC-5 — Stall-detector silence on identical feedback.** Run-1 fired
  `unrecoverable_stall` after 3 identical-signature turns; run-2 ran the
  full 5 turns despite the feedback string being byte-identical from turn 1
  to turn 5. Determine whether the stall detector regressed, was disabled
  by a remediation, or never armed because the `must_fix` content embedded
  the file path (giving it superficially different bytes between runs but
  identical between turns). Recommend the correct behaviour for run-3.
- [x] **AC-6 — Wave-3 partial-progress preservation.** TASK-FRR-PEB-010
  succeeded in the same wave; the worktree is preserved at
  `.guardkit/worktrees/FEAT-PEBR` on branch `autobuild/FEAT-PEBR`. Confirm
  whether `--resume` semantics will pick up correctly from waves 1–3
  (1, 2, 010 approved) and re-attempt 003 only, or whether a fresh start
  is needed. Note any state cleanup required (the persisted
  `autobuild_state.turns[*]` block on TASK-FRR-PEB-003 contains the phantom
  feedback string and may need scrubbing before resume).
- [x] **AC-7 — Concrete remediation plan.** Produce an ordered list of
  follow-up actions, each tagged as one of:
  `[guardkit-plan-audit]`, `[guardkit-coach]`, `[guardkit-state-bridge]`,
  `[forge-task-frontmatter]`, `[forge-task-prose]`, `[no-change]`. Each
  item must name the file(s) to edit (or explicitly say "config-only / no
  code change") and the task id prefix to use when the implementation task
  is created. Distinguish from TASK-REV-PEBR-001's recommendations — this
  review must explain why FM-001 / GK-PA-001 did not close the gap and
  what additional surface needs covering.
- [x] **AC-8 — Resume vs. fresh-start vs. block recommendation.** State
  whether the next attempt should:
  (a) `guardkit autobuild feature FEAT-PEBR --resume` after only the
      task-prose fix,
  (b) `--resume` after both task-prose and guardkit fixes,
  (c) `--fresh` (full restart),
  (d) block on remediation tasks landing first.
  Justify with reference to the worktree state and what tasks 4–9 and
  11–14 still need.

## Out of Scope

- Implementing any of the recommendations (those become separate tasks).
- Modifying TASK-FRR-PEB-003 source/test code beyond noting required prose
  edits in the remediation plan.
- Re-running autobuild. The review uses captured artefacts only.
- Re-litigating run-1 conclusions; assume TASK-REV-PEBR-001 outputs stand
  and only diff against them where run-2 contradicts them.

## Inputs

- Full transcript: [docs/history/autobuild-FEAT-PEBR-failed-run-2.md](../../../docs/history/autobuild-FEAT-PEBR-failed-run-2.md)
- Generated review summary: `.guardkit/autobuild/FEAT-PEBR/review-summary.md`
- Per-turn reports for TASK-FRR-PEB-003:
  `.guardkit/worktrees/FEAT-PEBR/.guardkit/autobuild/TASK-FRR-PEB-003/{player,coach}_turn_{1..5}.json`
- Stub plan: `.guardkit/worktrees/FEAT-PEBR/.claude/task-plans/TASK-FRR-PEB-003-implementation-plan.md`
- Source task (post-FM-001):
  [TASK-FRR-PEB-003-sse-to-envelope-translation.md](./TASK-FRR-PEB-003-sse-to-envelope-translation.md)
  — note the `## Files to Create` (l. 187) and `## Files to Modify` (l. 195)
  sections, and the prose reference on l. 205.
- Prior review: [TASK-REV-PEBR-001-analyse-autobuild-failed-run-1.md](./TASK-REV-PEBR-001-analyse-autobuild-failed-run-1.md)
- Feature definition: [.guardkit/features/FEAT-PEBR.yaml](../../../.guardkit/features/FEAT-PEBR.yaml)

## Deliverable

A single review document written to
`docs/reviews/FEAT-PEBR-failed-run-2-analysis.md` containing:

1. Executive summary (≤ 10 lines).
2. Per-AC findings (AC-1 … AC-8) with log-line / file citations.
3. Remediation plan table (action, owner-area, target file, follow-up
   task id prefix).
4. Diff against TASK-REV-PEBR-001's conclusions: what changed, what
   remediations partially worked, what remains uncovered.
5. Decision checkpoint — one of: **[A]ccept** the diagnosis as-is,
   **[I]mplement** by spawning follow-up tasks, **[R]evise** with deeper
   investigation, **[C]ancel**.

## Implementation Notes

- This task is intended for `/task-review`, **not** `/task-work`. Do not
  generate code; produce analysis and decision artefacts only.
- When citing log evidence, prefer line numbers from
  `docs/history/autobuild-FEAT-PEBR-failed-run-2.md` so the review remains
  reproducible.
- The phantom path is **mechanically identical** across all 5 turns —
  great anchor for tracing the scanner code path.
- Coach reports 7/7 ACs verified at turn 5 — the failure is purely a
  plan-audit-vs-AC adjudication issue, not a real implementation gap.
- Wave 3's other task (TASK-FRR-PEB-010) succeeded, so the worktree
  contains a half-good state that should be salvageable with `--resume`
  once the gate logic / task prose is fixed.
