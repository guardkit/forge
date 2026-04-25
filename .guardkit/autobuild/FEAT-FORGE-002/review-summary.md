# Autobuild Review Summary: FEAT-FORGE-002

**Status:** FAILED  
**Generated:** 2026-04-25 12:55 UTC

## Metrics

| Metric | Value |
|--------|-------|
| Total tasks | 11 |
| Total turns | 11 |
| Avg turns/task | 2.20 |
| Waves executed | 2 |
| First-attempt pass rate | 40% |

## Per-Task Outcomes

| Task | Wave | Turns | Outcome | Decision | Notes |
|------|------|-------|---------|----------|-------|
| TASK-NFI-001 | 1 | 1 | PASSED | approved |  |
| TASK-NFI-002 | 1 | 1 | PASSED | approved |  |
| TASK-NFI-003 | 2 | 3 | FAILED | unrecoverable_stall | coach_agent_invocations_stall + context_pollution_stall_no_checkpoint | Unrecoverable stall detected after 3 turn(s). AutoBuild cannot make forward progress. |
| TASK-NFI-006 | 2 | 3 | FAILED | unrecoverable_stall | coach_agent_invocations_stall + context_pollution_stall_no_checkpoint | Unrecoverable stall detected after 3 turn(s). AutoBuild cannot make forward progress. |
| TASK-NFI-007 | 2 | 3 | FAILED | unrecoverable_stall | coach_agent_invocations_stall + context_pollution_stall_no_checkpoint | Unrecoverable stall detected after 3 turn(s). AutoBuild cannot make forward progress. |

## Quality Metrics

- Task success rate: 40%
- First-turn approvals: 2/5
- SDK ceiling hits: 0

## Turn Efficiency

| Metric | Value |
|--------|-------|
| Avg turns/task | 2.2 |
| Single-turn tasks | 2 |
| Multi-turn tasks | 3 |
| Avg SDK turns/invocation | 24.6 |

## Key Findings

- Tasks required multiple turns before failing: TASK-NFI-003, TASK-NFI-006, TASK-NFI-007. Review coach feedback logs for recurring patterns.
