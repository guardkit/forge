---
complexity: 4
dependencies: []
feature_id: FEAT-UBS1C
id: TASK-UBS1C-002
implementation_mode: task-work
status: in_review
task_type: feature
title: planning_waves reads the feature task graph
wave: 1
autobuild_state:
  current_turn: 3
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-UBS1C
  base_branch: main
  started_at: '2026-07-26T08:00:39.439616'
  last_updated: '2026-07-26T08:39:18.824742'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit, severity=critical): Player\
      \ claim: Player claimed file `src/forge/subagents/autobuild_runner.py. Actual:\
      \ Path absent from 'git status --porcelain' so 'git add -A' would not stage\
      \ it. Probes: path_exists=False; gitignore_match=no rule matched; tracked=no.\
      \ Most likely cause: the Player claimed work on a file that does not exist on\
      \ disk..\n- Deterministic honesty record (claim_audit, severity=critical): Player\
      \ claim: Player claimed file `tests/forge/test_autobuild_runner_planning_waves.py.\
      \ Actual: Path absent from 'git status --porcelain' so 'git add -A' would not\
      \ stage it. Probes: path_exists=False; gitignore_match=no rule matched; tracked=no.\
      \ Most likely cause: the Player claimed work on a file that does not exist on\
      \ disk..\n- gathering_status=\"partial_honesty_abort\" \u2014 evidence gathering\
      \ aborted on a critical claim_audit discrepancy (honesty_score=0.8: claimed\
      \ \"`src/forge/subagents/autobuild_runner.py\" and \"`tests/forge/test_autobuild_runner_planning_waves.py\"\
      \ but path_exists=False; gitignore_match=no rule matched; tracked=no)"
    timestamp: '2026-07-26T08:00:39.439616'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit, severity=critical): Player\
      \ claim: Player claimed file `src/forge/subagents/autobuild_runner.py. Actual:\
      \ Path absent from 'git status --porcelain' so 'git add -A' would not stage\
      \ it. Probes: path_exists=False; gitignore_match=no rule matched; tracked=no.\
      \ Most likely cause: the Player claimed work on a file that does not exist on\
      \ disk..\n- Deterministic honesty record (claim_audit, severity=critical): Player\
      \ claim: Player claimed file `tests/forge/test_autobuild_runner_planning_waves.py.\
      \ Actual: Path absent from 'git status --porcelain' so 'git add -A' would not\
      \ stage it. Probes: path_exists=False; gitignore_match=no rule matched; tracked=no.\
      \ Most likely cause: the Player claimed work on a file that does not exist on\
      \ disk..\n- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file conversation_history/session_8095739d.md.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 8 more issues"
    timestamp: '2026-07-26T08:26:48.067523'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-07-26T08:34:14.439939'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# planning_waves reads the feature task graph

`_node_planning_waves` (src/forge/subagents/autobuild_runner.py, ~line 973) is a true
placeholder: it emits a `planning_waves` snapshot and does no work. Per the UBS scope §4
(docs/research/ideas/unattended-build-service-scope.md): it must READ the feature's task graph
so the run's snapshots carry real totals. Source of truth = the TARGET repo's
`.guardkit/features/<feature_id>.yaml` (resolve the repo path exactly the way
`_node_running_wave`'s `_resolve_repo_path` does — reuse it, do not duplicate): read
`tasks[]` (count) and `orchestration.parallel_groups` (wave count and per-wave task ids).
Populate the EXISTING AutobuildState fields only (wave/task totals/indices as the schema
already defines them — FROZEN, no new fields; graph shape unchanged). Failure honesty: a
missing/unreadable/malformed feature yaml must NOT crash the run — emit the planning_waves
snapshot as today plus a WARNING log naming the path (the run proceeds; running_wave is the
authority on actual execution). Do not touch the SSE bridge, run.py, or graph registration.

## Acceptance Criteria
- [ ] With a fixture feature yaml (3 tasks, parallel_groups [[a,b],[c]]), the planning_waves snapshot carries the correct totals in the existing schema fields (hermetic test, tmp repo dir)
- [ ] Missing yaml / malformed yaml / feature id absent from the file each produce the current placeholder snapshot + a WARNING naming the resolved path — never an exception (three hermetic tests)
- [ ] The repo path is resolved via the same helper running_wave uses (asserted by test — no duplicated resolution logic); AutobuildState schema and graph shape byte-unchanged; existing runner tests pass unmodified
