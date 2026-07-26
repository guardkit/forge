---
complexity: 4
dependencies: []
feature_id: FEAT-UBS1C
id: TASK-UBS1C-002
implementation_mode: task-work
status: backlog
task_type: feature
title: planning_waves reads the feature task graph
wave: 1
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
