# Autobuild Review Summary: FEAT-PEBR

**Status:** FAILED  
**Generated:** 2026-05-06 21:00 UTC

## Metrics

| Metric | Value |
|--------|-------|
| Total tasks | 14 |
| Total turns | 3 |
| Avg turns/task | 3.00 |
| Waves executed | 1 |
| First-attempt pass rate | 0% |

## Per-Task Outcomes

| Task | Wave | Turns | Outcome | Decision | Notes |
|------|------|-------|---------|----------|-------|
| TASK-FRR-PEB-001 | 1 | 3 | FAILED | unrecoverable_stall | coach_feedback_stall | Unrecoverable stall detected after 3 turn(s). AutoBuild cannot make forward progress. |

## Quality Metrics

- Task success rate: 0%
- First-turn approvals: 0/1
- SDK ceiling hits: 0

## Turn Efficiency

| Metric | Value |
|--------|-------|
| Avg turns/task | 3.0 |
| Single-turn tasks | 0 |
| Multi-turn tasks | 1 |
| Avg SDK turns/invocation | 20.0 |

## Key Findings

- Tasks required multiple turns before failing: TASK-FRR-PEB-001. Review coach feedback logs for recurring patterns.
