---
complexity: 5
dependencies: []
feature_id: FEAT-UBS1C
id: TASK-UBS1C-001
implementation_mode: task-work
status: backlog
task_type: feature
title: Populate coach scores from the proven decision grammar
wave: 1
---

# Populate coach scores from the proven decision grammar

`_node_running_wave` (src/forge/subagents/autobuild_runner.py, the TASK-ABW-001 wiring) drains
the `guardkit autobuild` subprocess line-by-line counting `[guardkit-checkpoint]` markers, but
NEVER populates `AutobuildState.last_coach_score` / `aggregate_coach_score` (they stay None —
the ADR-ARCH-033 gap, prerequisite for FEAT-UBS-002's budget guards). The evidence gate is now
CLOSED: real transcripts are archived in
docs/research/evidence/autobuild-transcripts-2026-07-26/ with a proven line grammar and a
LOAD-BEARING NEGATIVE finding — guardkit emits NO numeric score, so the semantics are
decision-derived: last_coach_score = 1.0 for a `success` decision line / 0.0 for `feedback`;
aggregate_coach_score = success ratio over decision-bearing turns. Parse the
`INFO:guardkit.orchestrator.progress:[...] Completed turn <N>: success|feedback - ...` family
in the EXISTING drain loop (extend the same line handler that counts checkpoints — no second
reader, no buffering change). Graph shape and the AutobuildState schema are FROZEN (DDR-006;
the fields already exist). The verdict-emission-failure edge (WARNING then a normal feedback
decision line) and the timeout shape (NO decision line at all — scores stay at their last
value / None) are both in the archived transcripts and MUST be covered by tests. Do not touch
adapters/guardkit/run.py (one-shot contract), the SSE bridge, or the graph registration.

## Acceptance Criteria
- [ ] After a drained run whose lines include `Completed turn 1: feedback - ...` then `Completed turn 2: success - ...`, the emitted running_wave/completed snapshots carry last_coach_score 0.0 then 1.0 and aggregate_coach_score 0.5 (hermetic test feeding the archived-transcript lines through the drain handler)
- [ ] The verdict-emission-failure sequence from the CV4M archive (WARNING + synthetic feedback decision line) parses as a feedback turn — no crash, no skipped update; the timeout shape (no decision lines) leaves scores None and the run still maps to its exit-code lifecycle exactly as today
- [ ] Snapshots still validate against the FROZEN AutobuildState schema; no new state fields, no graph-shape change (existing test_autobuild_runner_subprocess.py passes unmodified except where it asserts scores stay None — update ONLY those assertions to the new semantics with a comment citing the evidence README)
- [ ] A short note is appended to ADR-ARCH-033 marking the coach-score gap CLOSED with decision-derived semantics, citing docs/research/evidence/autobuild-transcripts-2026-07-26/README.md
