---
id: TASK-MAG7-008
title: Wire dispatch_subprocess_stage for system-arch, system-design, feature-spec,
  feature-plan
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 3
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MAG7-001
- TASK-MAG7-006
tags:
- dispatcher
- subprocess
- guardkit
- feat-forge-005
- feat-forge-007
consumer_context:
- task: TASK-MAG7-006
  consumes: forward_context
  framework: Python forge.pipeline.forward_context_builder
  driver: Internal call
  format_note: Receives list[ContextEntry] from ForwardContextBuilder.build_for()
    and converts to --context flag arguments for the GuardKit subprocess invocation
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:00:26.635432'
  last_updated: '2026-04-26T19:15:15.720770'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:00:26.635432'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-008: feature-rollup completed"
---

# Task: Wire dispatch_subprocess_stage

## Description

Compose FEAT-FORGE-005's GuardKit subprocess engine with the
`ForwardContextBuilder` to dispatch the four subprocess stages
(`SYSTEM_ARCH`, `SYSTEM_DESIGN`, `FEATURE_SPEC`, `FEATURE_PLAN`). Constructs
the `--context` flag argument list, invokes the appropriate GuardKit slash
command via the subprocess engine, captures artefact paths, threads
correlation_id, and records the result in `stage_log`.

Covers Group A scenarios: "Forge invokes architecture, design, per-feature
specification, per-feature planning, and autobuild in order" and
"Architecture outputs are supplied as context for system design".

## Acceptance Criteria

- [ ] `dispatch_subprocess_stage(stage, build_id, feature_id=None, ...) -> StageDispatchResult`
      function exists at `src/forge/pipeline/dispatchers/subprocess.py`
- [ ] Maps each stage to its GuardKit slash command:
      `SYSTEM_ARCH` → `/system-arch`,
      `SYSTEM_DESIGN` → `/system-design`,
      `FEATURE_SPEC` → `/feature-spec`,
      `FEATURE_PLAN` → `/feature-plan`
- [ ] Calls `ForwardContextBuilder.build_for` and converts the returned
      entries into `--context <path>` flag arguments
- [ ] Delegates to FEAT-FORGE-005's subprocess engine for execution under
      worktree allowlist confinement
- [ ] Records artefact paths returned by the subprocess in `stage_log`
      `artefact_path` column, attributed by feature_id when applicable
      (Group G @data-integrity scenario "Per-feature artefact paths")
- [ ] Threads correlation_id through subprocess envelope and any pipeline
      events the subprocess publishes (Group I @data-integrity scenario)
- [ ] Subprocess hard-stop / non-zero exit is converted to
      `StageDispatchResult.FAILED` with structured rationale — never
      raises past the function boundary (universal error contract)
- [ ] On `/feature-spec` failure, the result rationale records the
      failed-spec rationale so the supervisor can halt that feature's inner
      loop (Group C @negative scenario)
- [ ] Unit tests cover all four stages with mocked subprocess engine
- [ ] Unit test: subprocess hard-stop converted to structured failure
- [ ] Unit test: artefact path outside allowlist refused
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

The four stages share the same dispatch shape; differentiating only by which
GuardKit slash command is invoked and (for per-feature stages) which feature
the dispatch targets. Worktree confinement, allowlist enforcement, and the
universal error contract all live in FEAT-FORGE-005 — this dispatcher is a
thin composition layer.

The `forge.pipeline.dispatchers` package will hold three dispatcher modules
by the end of Wave 3: `specialist`, `subprocess`, and `autobuild_async`.

## Test Execution Log

[Automatically populated by /task-work]
